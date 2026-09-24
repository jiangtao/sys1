#!/usr/bin/env python3
"""Evaluate six-role multi-label routing on the local sessions set."""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SESSION_DIR = Path(__file__).resolve().parent
TEMPLATES_PATH = SESSION_DIR / "session_templates.json"
CASES_PATH = SESSION_DIR / "session_cases.json"
DEFAULT_OUTPUT_DIR = SESSION_DIR / "results"
MAX_INPUT_CHARS = 700


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"顶层 JSON 必须是对象：{path}")
    return value


def validate_dataset(templates: dict[str, Any], document: dict[str, Any]) -> list[dict[str, Any]]:
    roles = templates.get("roles")
    questions = templates.get("questions")
    cases = document.get("cases")
    if roles != [
        "evidence_researcher",
        "solution_designer",
        "implementation_worker",
        "backend_tester",
        "e2e_tester",
        "incremental_reviewer",
    ]:
        raise ValueError("session_templates.json 的 roles 必须是固定六角色")
    if not isinstance(questions, dict) or set(questions) != set(roles):
        raise ValueError("session_templates.json 的 questions 必须覆盖固定六角色")
    for role in roles:
        question = questions[role]
        if not isinstance(question, dict) or question.get("type") != "noul" or not isinstance(question.get("instructions"), str):
            raise ValueError(f"角色 {role} 的问题模板无效")
    if not isinstance(cases, list) or len(cases) != 20:
        raise ValueError(f"历史 sessions 评测必须正好有 20 条，实际为 {len(cases) if isinstance(cases, list) else '非数组'}")

    legal = set(roles)
    seen_ids: set[str] = set()
    seen_threads: set[str] = set()
    required = {
        "id",
        "source_thread_id",
        "title",
        "input_text",
        "task_kind",
        "outcome",
        "actual_roles",
        "recommended_roles",
        "label_confidence",
        "label_notes",
    }
    for case in cases:
        if not isinstance(case, dict) or set(case) != required:
            raise ValueError(f"case 字段必须严格匹配固定 schema：{case.get('id') if isinstance(case, dict) else case!r}")
        if not isinstance(case["id"], str) or not case["id"] or case["id"] in seen_ids:
            raise ValueError(f"id 缺失或重复：{case['id']!r}")
        if not isinstance(case["source_thread_id"], str) or not case["source_thread_id"] or case["source_thread_id"] in seen_threads:
            raise ValueError(f"source_thread_id 缺失或重复：{case['source_thread_id']!r}")
        if not isinstance(case["input_text"], str) or not case["input_text"].strip() or len(case["input_text"]) > MAX_INPUT_CHARS:
            raise ValueError(f"{case['id']} 的 input_text 为空或超过 {MAX_INPUT_CHARS} 字符")
        for field in ("actual_roles", "recommended_roles"):
            value = case[field]
            if not isinstance(value, list) or len(value) != len(set(value)) or not set(value).issubset(legal):
                raise ValueError(f"{case['id']}.{field} 含非法角色或重复角色")
        if case["label_confidence"] not in {"high", "medium", "low"}:
            raise ValueError(f"{case['id']} 的 label_confidence 无效")
        if not isinstance(case["label_notes"], str) or not case["label_notes"].strip():
            raise ValueError(f"{case['id']} 缺少 label_notes")
        seen_ids.add(case["id"])
        seen_threads.add(case["source_thread_id"])
    return cases


def request_decision(base_url: str, input_text: str, questions: dict[str, Any], timeout: float) -> tuple[int, dict[str, Any] | None, str | None, float]:
    # Deliberately send only input_text as state. All other session fields remain labels/metadata.
    body = json.dumps({"state": input_text, "questions": questions}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/decide",
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
            status = response.status
        if not isinstance(payload, dict):
            raise ValueError("响应顶层不是 JSON 对象")
        return status, payload, None, time.perf_counter() - started
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        return error.code, None, f"HTTP {error.code}: {detail[:240]}", time.perf_counter() - started
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as error:
        return 0, None, f"{type(error).__name__}: {str(error)[:240]}", time.perf_counter() - started


def evaluate_response(case: dict[str, Any], payload: dict[str, Any], roles: list[str], threshold: float, elapsed: float) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    answers = payload.get("answers")
    if not isinstance(answers, dict):
        raise ValueError("响应缺少 answers")
    gold = set(case["recommended_roles"])
    predictions: dict[str, Any] = {}
    predicted_roles: set[str] = set()
    issues: list[dict[str, Any]] = []
    for role in roles:
        answer = answers.get(role)
        if not isinstance(answer, dict) or answer.get("type") != "noul":
            raise ValueError(f"响应缺少合法 noul 答案：{role}")
        probability = answer.get("noul")
        if not isinstance(probability, (int, float)) or isinstance(probability, bool) or not 0 <= float(probability) <= 1:
            raise ValueError(f"{case['id']}.{role} 的 noul 概率无效")
        predicted = float(probability) >= 0.5
        if predicted:
            predicted_roles.add(role)
        correct = predicted == (role in gold)
        covered = float(probability) >= threshold or float(probability) <= 1 - threshold
        predictions[role] = {
            "gold": role in gold,
            "probability": probability,
            "predicted": predicted,
            "covered": covered,
            "correct": correct,
            "confidence": answer.get("confidence"),
        }
        if not correct:
            issues.append({
                "id": case["id"],
                "role": role,
                "kind": "role_misprediction",
                "gold": role in gold,
                "predicted": predicted,
                "probability": probability,
                "confidence": answer.get("confidence"),
            })

    label_diff = sorted(set(case["actual_roles"]) ^ gold)
    result = {
        "id": case["id"],
        "source_thread_id": case["source_thread_id"],
        "gold_roles": sorted(gold),
        "predicted_roles": sorted(predicted_roles),
        "gold_main_fallback": not gold,
        "predicted_main_fallback": not predicted_roles,
        "exact_role_set_match": predicted_roles == gold,
        "actual_roles": sorted(case["actual_roles"]),
        "actual_vs_recommended_diff": label_diff,
        "label_differs": bool(label_diff),
        "elapsed_ms": round(elapsed * 1000, 1),
        "predictions": predictions,
    }
    return result, issues


def role_metrics(cases: list[dict[str, Any]], results: list[dict[str, Any]], roles: list[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    cases_by_id = {case["id"]: case for case in cases}
    results_by_id = {result["id"]: result for result in results}
    metrics: dict[str, Any] = {}
    tp_total = fp_total = fn_total = 0
    f1_values: list[float] = []
    high_conf_total = high_conf_correct = 0
    label_results: list[dict[str, Any]] = []

    for role in roles:
        tp = fp = fn = support = predicted_total = 0
        for result in results:
            gold = result["predictions"][role]["gold"]
            predicted = result["predictions"][role]["predicted"]
            support += gold
            predicted_total += predicted
            tp += predicted and gold
            fp += predicted and not gold
            fn += (not predicted) and gold
            item = result["predictions"][role]
            if item["covered"]:
                high_conf_total += 1
                high_conf_correct += item["correct"]
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        metrics[role] = {
            "support": support,
            "predicted_positive": predicted_total,
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
        }
        tp_total += tp
        fp_total += fp
        fn_total += fn
        f1_values.append(f1)

    micro_precision = tp_total / (tp_total + fp_total) if tp_total + fp_total else 0.0
    micro_recall = tp_total / (tp_total + fn_total) if tp_total + fn_total else 0.0
    micro_f1 = 2 * micro_precision * micro_recall / (micro_precision + micro_recall) if micro_precision + micro_recall else 0.0
    macro_f1 = sum(f1_values) / len(f1_values) if f1_values else 0.0
    exact = sum(result["exact_role_set_match"] for result in results)
    fallback_correct = sum(result["gold_main_fallback"] == result["predicted_main_fallback"] for result in results)
    label_results = [
        {
            "id": result["id"],
            "actual_roles": result["actual_roles"],
            "recommended_roles": result["gold_roles"],
            "symmetric_difference": result["actual_vs_recommended_diff"],
        }
        for result in results
        if result["label_differs"]
    ]
    summary = {
        "per_role": metrics,
        "micro_precision": round(micro_precision, 4),
        "micro_recall": round(micro_recall, 4),
        "micro_f1": round(micro_f1, 4),
        "macro_f1": round(macro_f1, 4),
        "exact_role_set_match": round(exact / len(results), 4) if results else None,
        "exact_role_set_match_count": exact,
        "main_fallback_accuracy": round(fallback_correct / len(results), 4) if results else None,
        "main_fallback_count": sum(result["gold_main_fallback"] for result in results),
        "high_conf_coverage": round(high_conf_total / (len(results) * len(roles)), 4) if results else None,
        "high_conf_coverage_accuracy": round(high_conf_correct / high_conf_total, 4) if high_conf_total else None,
        "high_conf_total": high_conf_total,
        "high_conf_correct": high_conf_correct,
        "label_difference_case_count": len(label_results),
        "label_difference_role_count": sum(len(item["symmetric_difference"]) for item in label_results),
        "label_differences": label_results,
    }
    return summary, {"tp": tp_total, "fp": fp_total, "fn": fn_total}


def build_report(summary: dict[str, Any]) -> str:
    metrics = summary["metrics"]
    lines = [
        "# Laya 本地 sessions Agent 调度评测报告",
        "",
        f"- 运行时间（UTC）：{summary['run_at_utc']}",
        f"- 服务地址：{summary['base_url']}",
        f"- 数据源：已授权的本地 sessions 摘要，仅 {summary['dataset']['case_count']} 条；输入已脱敏。",
        f"- 模型输入：每条请求只发送 input_text；其余字段只用于标签和分析。实际最大输入字符数：{summary['dataset']['input_char_max']}。",
        f"- 高置信阈值：{summary['threshold']}；HTTP/解析失败：{summary['http_failures']}；平均/最大延迟：{summary['latency_ms']['mean']} / {summary['latency_ms']['max']} ms。",
        "",
        "## 总体指标",
        "",
        "| 指标 | 结果 |",
        "| --- | ---: |",
        f"| micro precision / recall / F1 | {metrics['micro_precision']:.4f} / {metrics['micro_recall']:.4f} / {metrics['micro_f1']:.4f} |",
        f"| macro F1 | {metrics['macro_f1']:.4f} |",
        f"| exact role-set match | {metrics['exact_role_set_match']:.4f} ({metrics['exact_role_set_match_count']}/{summary['dataset']['case_count']}) |",
        f"| main_agent fallback accuracy | {metrics['main_fallback_accuracy']:.4f} ({metrics['main_fallback_count']} 个 gold fallback) |",
        f"| 0.9 高置信覆盖率 / 覆盖集准确率 | {metrics['high_conf_coverage']:.4f} / {metrics['high_conf_coverage_accuracy']:.4f} |",
        f"| HTTP failures | {summary['http_failures']} |",
        "",
        "## 各角色 Precision / Recall / F1",
        "",
        "| 角色 | Gold 数 | 预测正例 | Precision | Recall | F1 |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for role, item in metrics["per_role"].items():
        lines.append(f"| {role} | {item['support']} | {item['predicted_positive']} | {item['precision']:.4f} | {item['recall']:.4f} | {item['f1']:.4f} |")

    lines.extend(["", "## 实际角色与推荐标签差异", ""])
    lines.append(f"历史 actual_roles 与规则复核 recommended_roles 不同的 case：{metrics['label_difference_case_count']} 条；角色差异总数：{metrics['label_difference_role_count']}。")
    lines.append("gold 是规则复核标签，不是历史绝对事实；实际历史派发遗漏或角色不完全匹配不会被当作 gold。")
    lines.append("")
    if metrics["label_differences"]:
        lines.append("| Case | 实际角色 | 推荐角色 | 对称差异 |")
        lines.append("| --- | --- | --- | --- |")
        for item in metrics["label_differences"]:
            lines.append(f"| {item['id']} | {','.join(item['actual_roles']) or '无'} | {','.join(item['recommended_roles']) or 'main_agent fallback'} | {','.join(item['symmetric_difference'])} |")
    else:
        lines.append("本次没有 actual-vs-recommended 标签差异。")

    lines.extend(["", "## 典型误判或低置信样本", ""])
    issues = summary["typical_issues"]
    if issues:
        lines.extend([
            "| Case | 角色 | Gold | 预测 | 概率 | 置信度 |",
            "| --- | --- | --- | --- | ---: | ---: |",
        ])
        for issue in issues:
            lines.append(f"| {issue['id']} | {issue['role']} ({issue['kind']}) | {issue['gold']} | {issue['predicted']} | {issue['probability']} | {issue.get('confidence', '')} |")
    else:
        lines.append("本次没有误判或低置信样本。")

    lines.extend([
        "",
        "## 解读与限制",
        "",
        "- 本评测只有 20 条，来自已授权的本地 sessions 摘要；所有输入均已脱敏，不能代表完整历史任务分布。",
        "- recommended_roles 是人工/规则复核的 gold，不是历史 actual_roles 的无条件真值；s08、s11、s12 等记录保留了标签噪声说明。",
        "- 固定六类角色之外的专用 Skill、权限决策、需求澄清和最终验收统一按 main_agent fallback 处理。",
        "- 当前模型概率未针对这些 sessions 标签做校准；0.9 覆盖率和覆盖准确率只是本次样本上的观察，不能直接作为生产阈值。",
        "- 评测输入只发送 input_text，避免把 actual_roles、recommended_roles、结果和标签说明泄漏给模型。",
        "",
        "机器可读明细：results/latest.json",
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--threshold", type=float, default=0.9)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args()
    if not 0.5 < args.threshold < 1.0:
        parser.error("--threshold 必须在 0.5 和 1.0 之间")

    try:
        templates = load_json(TEMPLATES_PATH)
        cases = validate_dataset(templates, load_json(CASES_PATH))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"数据集校验失败：{error}", file=sys.stderr)
        return 2

    args.output_dir.mkdir(parents=True, exist_ok=True)
    roles = templates["roles"]
    questions = templates["questions"]
    results: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    latencies: list[float] = []
    print(f"开始评测 {len(cases)} 条本地 sessions，服务：{args.base_url}")
    for index, case in enumerate(cases, start=1):
        status, payload, error, elapsed = request_decision(args.base_url, case["input_text"], questions, args.timeout)
        latencies.append(elapsed)
        if error is not None or payload is None or status != 200:
            failures.append({"id": case["id"], "status": status, "error": error})
            print(f"[{index}/{len(cases)}] {case['id']} HTTP/解析失败：{error}", file=sys.stderr)
            continue
        try:
            result, case_issues = evaluate_response(case, payload, roles, args.threshold, elapsed)
        except ValueError as error:
            failures.append({"id": case["id"], "status": status, "error": str(error)})
            print(f"[{index}/{len(cases)}] {case['id']} 响应校验失败：{error}", file=sys.stderr)
            continue
        results.append(result)
        issues.extend(case_issues)
        print(f"[{index}/{len(cases)}] {case['id']} 完成，{elapsed * 1000:.0f} ms")

    metrics, _ = role_metrics(cases, results, roles)
    all_issues = list(issues)
    for result in results:
        for role, item in result["predictions"].items():
            if isinstance(item.get("confidence"), (int, float)) and item["confidence"] < 0.6:
                all_issues.append({
                    "id": result["id"],
                    "role": role,
                    "kind": "low_confidence",
                    "gold": item["gold"],
                    "predicted": item["predicted"],
                    "probability": item["probability"],
                    "confidence": item["confidence"],
                })
    unique_issues: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for issue in all_issues:
        identity = (issue["id"], issue["role"], issue["kind"])
        if identity in seen:
            continue
        seen.add(identity)
        unique_issues.append(issue)
        if len(unique_issues) == 10:
            break
    summary = {
        "run_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "base_url": args.base_url,
        "threshold": args.threshold,
        "dataset": {
            "case_count": len(cases),
            "source": "authorized local sessions, de-identified",
            "max_input_chars": MAX_INPUT_CHARS,
            "input_char_max": max(len(case["input_text"]) for case in cases),
            "roles": roles,
        },
        "http_failures": len(failures),
        "latency_ms": {
            "mean": round(sum(latencies) / len(latencies) * 1000, 1) if latencies else None,
            "max": round(max(latencies) * 1000, 1) if latencies else None,
        },
        "metrics": metrics,
        "typical_issues": unique_issues,
        "issues_total": len(issues),
        "failures": failures,
        "results": results,
    }
    latest_path = args.output_dir / "latest.json"
    report_path = args.output_dir / "REPORT.md"
    latest_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report_path.write_text(build_report(summary), encoding="utf-8")
    print(f"结果：{latest_path}")
    print(f"报告：{report_path}")
    if failures:
        print(f"完成但存在 {len(failures)} 个 HTTP/解析失败。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
