#!/usr/bin/env python3
"""Run the synthetic Chinese Laya evaluation set against the HTTP service."""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


EVALUATION_DIR = Path(__file__).resolve().parent
TEMPLATES_PATH = EVALUATION_DIR / "templates.json"
CASES_PATH = EVALUATION_DIR / "cases.json"
DEFAULT_OUTPUT_DIR = EVALUATION_DIR / "results"
STATE_CHAR_LIMIT = 700


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"顶层 JSON 必须是对象：{path}")
    return value


def validate_dataset(templates: dict[str, Any], cases_document: dict[str, Any]) -> list[dict[str, Any]]:
    suites = templates.get("suites")
    cases = cases_document.get("cases")
    if not isinstance(suites, dict) or not isinstance(cases, list):
        raise ValueError("templates.json 必须包含 suites，cases.json 必须包含 cases")
    if len(cases) < 45:
        raise ValueError(f"样本数不足：{len(cases)}，至少需要 45 条")

    seen_ids: set[str] = set()
    suite_counts: Counter[str] = Counter()
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("每个 case 必须是对象")
        case_id = case.get("id")
        suite_name = case.get("suite")
        if not isinstance(case_id, str) or not case_id or case_id in seen_ids:
            raise ValueError(f"case id 缺失或重复：{case_id!r}")
        if suite_name not in suites:
            raise ValueError(f"{case_id} 使用了未定义 suite：{suite_name!r}")
        if not isinstance(case.get("state"), (dict, list, str)):
            raise ValueError(f"{case_id} 的 state 类型不受支持")
        if not isinstance(case.get("expected"), dict):
            raise ValueError(f"{case_id} 缺少 expected 标签")

        state_text = json.dumps(case["state"], ensure_ascii=False, separators=(",", ":"))
        if len(state_text) > STATE_CHAR_LIMIT:
            raise ValueError(f"{case_id} 的 state 过长：{len(state_text)} 字符，限制 {STATE_CHAR_LIMIT}")
        questions = suites[suite_name].get("questions")
        if not isinstance(questions, dict):
            raise ValueError(f"suite {suite_name} 缺少 questions")
        for question_name, question in questions.items():
            if not isinstance(question, dict) or question.get("type") not in {"choice", "score", "noul"}:
                raise ValueError(f"suite {suite_name} 的问题 {question_name} 类型无效")
            if not isinstance(question.get("instructions"), str) or not question["instructions"].strip():
                raise ValueError(f"suite {suite_name} 的问题 {question_name} 缺少 instructions")
            if question["type"] in {"choice", "score"} and not isinstance(question.get("criteria"), (dict, list)):
                raise ValueError(f"suite {suite_name} 的问题 {question_name} 缺少 criteria")
        missing = set(questions) - set(case["expected"])
        extra = set(case["expected"]) - set(questions)
        if missing or extra:
            raise ValueError(f"{case_id} expected 字段不匹配，缺少={sorted(missing)} 多余={sorted(extra)}")
        for question_name, question in questions.items():
            expected_value = case["expected"][question_name]
            if question["type"] == "choice" and not isinstance(expected_value, str):
                raise ValueError(f"{case_id}.{question_name} 的 choice 标签必须是字符串")
            if question["type"] == "score" and (isinstance(expected_value, bool) or not isinstance(expected_value, int)):
                raise ValueError(f"{case_id}.{question_name} 的 score 标签必须是整数")
            if question["type"] == "noul" and not isinstance(expected_value, bool):
                raise ValueError(f"{case_id}.{question_name} 的 noul 标签必须是布尔值")
        seen_ids.add(case_id)
        suite_counts[suite_name] += 1

    if set(suite_counts) != set(suites):
        raise ValueError("每个定义的 suite 都必须有样本")
    undersized = {name: count for name, count in suite_counts.items() if count < 15}
    if undersized:
        raise ValueError(f"每个 suite 至少需要 15 条：{undersized}")
    return cases


def request_decision(base_url: str, state: Any, questions: dict[str, Any], timeout: float) -> tuple[int, dict[str, Any] | None, str | None, float]:
    body = json.dumps({"state": state, "questions": questions}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/decide",
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            status = response.status
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("响应顶层不是 JSON 对象")
        return status, payload, None, time.perf_counter() - started
    except urllib.error.HTTPError as error:
        raw_error = error.read().decode("utf-8", errors="replace")
        return error.code, None, f"HTTP {error.code}: {raw_error[:240]}", time.perf_counter() - started
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as error:
        return 0, None, f"{type(error).__name__}: {str(error)[:240]}", time.perf_counter() - started


def rounded_score(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        return None
    return math.floor(float(value) + 0.5)


def evaluate_case(case: dict[str, Any], answer: dict[str, Any], threshold: float, elapsed: float) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    expected = case["expected"]
    answers = answer.get("answers")
    if not isinstance(answers, dict):
        raise ValueError("响应缺少 answers 对象")
    result: dict[str, Any] = {
        "id": case["id"],
        "suite": case["suite"],
        "elapsed_ms": round(elapsed * 1000, 1),
        "questions": {},
    }
    issues: list[dict[str, Any]] = []

    for key, expected_value in expected.items():
        actual = answers.get(key)
        item: dict[str, Any] = {"expected": expected_value, "actual": actual}
        if not isinstance(actual, dict):
            item["status"] = "missing"
            issues.append({"id": case["id"], "suite": case["suite"], "question": key, "kind": "missing", "expected": expected_value, "actual": None})
            result["questions"][key] = item
            continue

        question_type = actual.get("type")
        if question_type == "choice":
            predicted = actual.get("choice")
            item.update({"predicted": predicted, "correct": predicted == expected_value})
            if predicted != expected_value:
                issues.append({"id": case["id"], "suite": case["suite"], "question": key, "kind": "choice", "expected": expected_value, "actual": predicted, "confidence": actual.get("confidence")})
        elif question_type == "score":
            score = actual.get("score")
            predicted = rounded_score(score)
            item.update({"score": score, "predicted": predicted, "correct": predicted == expected_value})
            if predicted != expected_value:
                issues.append({"id": case["id"], "suite": case["suite"], "question": key, "kind": "score", "expected": expected_value, "actual": predicted, "raw_score": score, "confidence": actual.get("confidence")})
        elif question_type == "noul":
            probability = actual.get("noul")
            if isinstance(probability, (int, float)) and not isinstance(probability, bool):
                predicted = float(probability) >= 0.5
                covered = float(probability) >= threshold or float(probability) <= 1 - threshold
                item.update({"probability": probability, "predicted": predicted, "covered": covered, "correct": predicted == expected_value})
                if predicted != expected_value:
                    issues.append({"id": case["id"], "suite": case["suite"], "question": key, "kind": "noul", "expected": expected_value, "actual": predicted, "probability": probability, "covered": covered, "confidence": actual.get("confidence")})
            else:
                item["status"] = "invalid_probability"
                issues.append({"id": case["id"], "suite": case["suite"], "question": key, "kind": "noul", "expected": expected_value, "actual": None})
        else:
            item["status"] = "unknown_type"
            issues.append({"id": case["id"], "suite": case["suite"], "question": key, "kind": "unknown_type", "expected": expected_value, "actual": question_type})
        result["questions"][key] = item
    return result, issues


def compute_metrics(cases: list[dict[str, Any]], results: list[dict[str, Any]], threshold: float) -> dict[str, Any]:
    result_by_id = {item["id"]: item for item in results}
    choice_total = choice_correct = 0
    noul_total = noul_correct = noul_covered = noul_covered_correct = 0
    score_total = score_correct = 0
    score_abs_error = 0.0

    for case in cases:
        result = result_by_id.get(case["id"])
        if result is None:
            continue
        for key, expected in case["expected"].items():
            item = result.get("questions", {}).get(key, {})
            actual = item.get("actual")
            if not isinstance(actual, dict):
                continue
            if actual.get("type") == "choice":
                choice_total += 1
                choice_correct += actual.get("choice") == expected
            elif actual.get("type") == "noul":
                probability = actual.get("noul")
                if isinstance(probability, (int, float)) and not isinstance(probability, bool):
                    noul_total += 1
                    predicted = float(probability) >= 0.5
                    noul_correct += predicted == expected
                    covered = float(probability) >= threshold or float(probability) <= 1 - threshold
                    if covered:
                        noul_covered += 1
                        noul_covered_correct += predicted == expected
            elif actual.get("type") == "score":
                score = actual.get("score")
                predicted = rounded_score(score)
                if isinstance(score, (int, float)) and not isinstance(score, bool) and predicted is not None and isinstance(expected, int):
                    score_total += 1
                    score_abs_error += abs(float(score) - expected)
                    score_correct += predicted == expected

    return {
        "choice_exact_accuracy": round(choice_correct / choice_total, 4) if choice_total else None,
        "choice_total": choice_total,
        "noul_accuracy_at_0_5": round(noul_correct / noul_total, 4) if noul_total else None,
        "noul_total": noul_total,
        "noul_high_conf_coverage": round(noul_covered / noul_total, 4) if noul_total else None,
        "noul_high_conf_accuracy": round(noul_covered_correct / noul_covered, 4) if noul_covered else None,
        "noul_high_conf_total": noul_covered,
        "score_mae": round(score_abs_error / score_total, 4) if score_total else None,
        "score_round_accuracy": round(score_correct / score_total, 4) if score_total else None,
        "score_total": score_total,
    }


def find_low_confidence(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    low: list[dict[str, Any]] = []
    for result in results:
        for question, item in result.get("questions", {}).items():
            actual = item.get("actual")
            if not isinstance(actual, dict):
                continue
            confidence = actual.get("confidence")
            if isinstance(confidence, (int, float)) and confidence < 0.6:
                fallback = actual.get("choice", actual.get("score", actual.get("noul")))
                low.append({
                    "id": result["id"],
                    "suite": result["suite"],
                    "question": question,
                    "kind": "low_confidence",
                    "expected": item.get("expected"),
                    "actual": item.get("predicted", fallback),
                    "confidence": confidence,
                })
    return sorted(low, key=lambda item: (float(item["confidence"]), item["id"]))


def calculate_summary(
    templates: dict[str, Any],
    cases: list[dict[str, Any]],
    results: list[dict[str, Any]],
    issues: list[dict[str, Any]],
    threshold: float,
    base_url: str,
    failures: list[dict[str, Any]],
    latencies: list[float],
) -> dict[str, Any]:
    titles = {name: suite.get("title", name) for name, suite in templates["suites"].items()}
    by_suite: dict[str, Any] = {}
    for suite_name, title in titles.items():
        suite_cases = [case for case in cases if case["suite"] == suite_name]
        suite_results = [item for item in results if item["suite"] == suite_name]
        metric = compute_metrics(suite_cases, suite_results, threshold)
        metric.update({"title": title, "case_count": len(suite_cases)})
        by_suite[suite_name] = metric

    examples = issues + find_low_confidence(results)
    unique_examples: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for issue in examples:
        identity = (str(issue.get("id")), str(issue.get("question")), str(issue.get("kind")))
        if identity in seen:
            continue
        seen.add(identity)
        unique_examples.append(issue)
        if len(unique_examples) == 10:
            break

    return {
        "run_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "base_url": base_url,
        "threshold": threshold,
        "dataset": {
            "case_count": len(cases),
            "suite_counts": dict(Counter(case["suite"] for case in cases)),
            "state_char_limit": STATE_CHAR_LIMIT,
        },
        "http_failures": len(failures),
        "latency_ms": {
            "mean": round(sum(latencies) / len(latencies) * 1000, 1) if latencies else None,
            "max": round(max(latencies) * 1000, 1) if latencies else None,
        },
        "overall": compute_metrics(cases, results, threshold),
        "by_suite": by_suite,
        "typical_issues": unique_examples,
        "issues_total": len(issues),
        "failures": failures,
        "results": results,
    }


def format_metric(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def build_report(summary: dict[str, Any]) -> str:
    dataset = summary["dataset"]
    overall = summary["overall"]
    lines = [
        "# Laya 中文场景评测报告",
        "",
        f"- 运行时间（UTC）：{summary['run_at_utc']}",
        f"- 服务地址：{summary['base_url']}",
        f"- 阈值：{summary['threshold']}（noul 高置信覆盖条件为 p >= threshold 或 p <= 1-threshold）",
        f"- 数据集：合成中文业务集，共 {dataset['case_count']} 条；Agent、游戏、文档各 {dataset['suite_counts'].get('agent', 0)} / {dataset['suite_counts'].get('game', 0)} / {dataset['suite_counts'].get('document', 0)} 条",
        f"- HTTP/解析失败：{summary['http_failures']}；平均单 case 请求耗时：{summary['latency_ms']['mean']} ms，最大：{summary['latency_ms']['max']} ms",
        "",
        "## 总体指标",
        "",
        "| 指标 | 结果 |",
        "| --- | ---: |",
        f"| choice exact accuracy | {format_metric(overall['choice_exact_accuracy'])} |",
        f"| noul accuracy@0.5 | {format_metric(overall['noul_accuracy_at_0_5'])} |",
        f"| noul 高置信覆盖率 | {format_metric(overall['noul_high_conf_coverage'])} |",
        f"| 高置信覆盖集准确率 | {format_metric(overall['noul_high_conf_accuracy'])} |",
        f"| score MAE | {format_metric(overall['score_mae'])} |",
        f"| score round accuracy | {format_metric(overall['score_round_accuracy'])} |",
        "",
        "## 按场景指标",
        "",
        "| 场景 | 样本 | choice exact | noul@0.5 | noul 高置信覆盖 / 准确 | score MAE / round |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for suite_name in ("agent", "game", "document"):
        metric = summary["by_suite"].get(suite_name, {})
        lines.append(
            f"| {metric.get('title', suite_name)} | {metric.get('case_count', 0)} | "
            f"{format_metric(metric.get('choice_exact_accuracy'))} | "
            f"{format_metric(metric.get('noul_accuracy_at_0_5'))} | "
            f"{format_metric(metric.get('noul_high_conf_coverage'))} / {format_metric(metric.get('noul_high_conf_accuracy'))} | "
            f"{format_metric(metric.get('score_mae'))} / {format_metric(metric.get('score_round_accuracy'))} |"
        )

    lines.extend(["", "## 典型误判或低置信样本", ""])
    if summary["typical_issues"]:
        lines.extend([
            "以下样本用于定位问题，不代表完整错误列表：",
            "",
            "| Case | 场景/问题 | 期望 | 模型结果 | 置信/概率 |",
            "| --- | --- | --- | --- | ---: |",
        ])
        for issue in summary["typical_issues"]:
            confidence = issue.get("probability", issue.get("confidence", ""))
            expected = json.dumps(issue.get("expected"), ensure_ascii=False)
            actual = json.dumps(issue.get("actual"), ensure_ascii=False)
            lines.append(f"| {issue['id']} | {issue['question']} ({issue['kind']}) | {expected} | {actual} | {confidence} |")
    else:
        lines.append("本次没有发现误判或低置信样本。")

    lines.extend([
        "",
        "## 解读与限制",
        "",
        "- 这是为当前 Laya 中文服务准备的合成业务集，标签是人工设计的场景期望，不是生产标注，也不是生产精度结论。",
        "- 每个 suite 使用固定问题模板；choice 以选项完全匹配计分，score 用模型 score 四舍五入到等级，noul 以 0.5 为分类边界。",
        "- 高置信覆盖率只表示模型愿意在阈值下自动给出结论的比例；覆盖集准确率才反映这部分自动结论的正确性。",
        "- 当前服务仍是本机 1024 context、MPS 推理，概率未针对本业务集做温度校准；高风险动作不能只依据模型概率执行。",
        "- 文档和游戏样本使用合成名称、编号和金额，不含真实个人或敏感资料。要判断生产可用性，还需要真实分布、人工复核和分层校准集。",
        "",
        "机器可读明细：results/latest.json",
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8765", help="现有 Laya 服务地址")
    parser.add_argument("--threshold", type=float, default=0.9, help="noul 高置信自动覆盖阈值，默认 0.9")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="latest.json 和 REPORT.md 输出目录")
    parser.add_argument("--timeout", type=float, default=60.0, help="单个 HTTP 请求超时秒数")
    args = parser.parse_args()
    if not 0.5 < args.threshold < 1.0:
        parser.error("--threshold 必须在 0.5 和 1.0 之间")

    try:
        templates = load_json(TEMPLATES_PATH)
        cases_document = load_json(CASES_PATH)
        cases = validate_dataset(templates, cases_document)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"数据集校验失败：{error}", file=sys.stderr)
        return 2

    args.output_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    latencies: list[float] = []
    suites = templates["suites"]
    print(f"开始评测 {len(cases)} 条 case，服务：{args.base_url}")
    for index, case in enumerate(cases, start=1):
        questions = suites[case["suite"]]["questions"]
        status, payload, error, elapsed = request_decision(args.base_url, case["state"], questions, args.timeout)
        latencies.append(elapsed)
        if error is not None or payload is None or status != 200:
            failures.append({"id": case["id"], "status": status, "error": error})
            print(f"[{index}/{len(cases)}] {case['id']} HTTP/解析失败：{error}", file=sys.stderr)
            continue
        try:
            result, case_issues = evaluate_case(case, payload, args.threshold, elapsed)
        except ValueError as error:
            failures.append({"id": case["id"], "status": status, "error": str(error)})
            print(f"[{index}/{len(cases)}] {case['id']} 响应校验失败：{error}", file=sys.stderr)
            continue
        results.append(result)
        issues.extend(case_issues)
        print(f"[{index}/{len(cases)}] {case['id']} 完成，{elapsed * 1000:.0f} ms")

    summary = calculate_summary(templates, cases, results, issues, args.threshold, args.base_url, failures, latencies)
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
