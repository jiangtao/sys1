"""Replay fixed Xiangqi positions against the local Kev-0.8B service.

This evaluates observable choices on the same saved FENs and Pikafish
candidates. It does not start a new game, measure playing strength, or report
guard adoption as a game outcome.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from engine import _extract_laya_choice, build_laya_request  # noqa: E402
from kev_client import KEV_MODEL_NAME, KEV_RUN_PREFIX, KevClient  # noqa: E402


def sampled_events(baseline: dict[str, Any], sample: str) -> list[dict[str, Any]]:
    """Return non-forced saved positions for the requested fixed split."""

    path = baseline.get("path")
    if not isinstance(path, list):
        raise ValueError("baseline.path 必须是数组")
    events = [event for event in path if isinstance(event, dict) and len(event.get("candidates", [])) > 1]
    if sample == "holdout":
        events = [event for event in events if int(event["ply"]) % 4 in (2, 3)]
    return events


def percentile(values: list[int], fraction: float) -> float | None:
    """Return an interpolated percentile using only the standard library."""

    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    weight = position - lower
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * weight, 2)


def summarize(rows: list[dict[str, Any]], run: str | None, failures: int) -> dict[str, Any]:
    latencies = [int(row["latencyMs"]) for row in rows]
    matched = sum(row["same"] for row in rows)
    return {
        "sampleCount": len(rows),
        "exactEngineBestChosen": matched,
        "exactEngineBestRate": round(matched / len(rows), 4) if rows else None,
        "latencyMedianMs": percentile(latencies, 0.5),
        "latencyP95Ms": percentile(latencies, 0.95),
        "run": run,
        "failures": failures,
    }


def result_payload(
    baseline: dict[str, Any],
    sample: str,
    service: str,
    run: str | None,
    rows: list[dict[str, Any]],
    failures: int,
    status: str,
    health: dict[str, Any] | None = None,
    failure: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "schema": "kev-xiangqi-fixed-position-evaluation/v1",
        "sourceGameId": baseline.get("gameId"),
        "sample": sample,
        "model": KEV_MODEL_NAME,
        "modelService": service,
        "run": run,
        "status": status,
        "health": health,
        "sampledPlies": [row["ply"] for row in rows],
        "summary": summarize(rows, run, failures),
        "rows": rows,
        "failure": failure,
        "adoptionRate": None,
        "note": (
            "相同保存局面、legalMoveCount 和 Pikafish 候选上的 Kev 固定位置评测；"
            "不启动新棋局、不声称棋力。未执行守门后的真实对局，因此不报告采纳率。"
        ),
    }


def write_result(
    output: Path,
    baseline: dict[str, Any],
    sample: str,
    service: str,
    run: str | None,
    rows: list[dict[str, Any]],
    failures: int,
    status: str,
    health: dict[str, Any] | None = None,
    failure: dict[str, str] | None = None,
) -> dict[str, Any]:
    result = result_payload(baseline, sample, service, run, rows, failures, status, health, failure)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline",
        type=Path,
        default=ROOT / "validation" / "decision-path-baseline-20260923.json",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--service", help="Kev 服务基地址；默认使用 XIANGQI_KEV_URL 或本机 8009")
    parser.add_argument("--sample", choices=("holdout", "all"), default="holdout")
    args = parser.parse_args()
    if args.output.exists():
        parser.error(f"输出文件已存在，请使用新路径：{args.output}")

    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    events = sampled_events(baseline, args.sample)
    client = KevClient(base_url=args.service)
    service = client.base_url
    rows: list[dict[str, Any]] = []
    run: str | None = None
    health: dict[str, Any] | None = None

    try:
        health_status, health = client.health()
        run_value = health.get("run")
        if health_status != 200 or health.get("status") != "ready":
            raise RuntimeError(f"Kev 健康检查失败：{json.dumps(health, ensure_ascii=False)}")
        if not isinstance(run_value, str) or not run_value.startswith(KEV_RUN_PREFIX):
            raise RuntimeError(f"Kev 已加载模型不是 {KEV_RUN_PREFIX}…：{run_value!r}")
        run = run_value

        for event in events:
            ply = event.get("ply")
            fen = event.get("before")
            candidates = event.get("candidates")
            legal_count = event.get("legalMoveCount")
            engine_best = event.get("engineBestMove")
            if not isinstance(ply, int) or not isinstance(fen, str) or not isinstance(candidates, list):
                raise ValueError(f"ply {ply!r}: baseline 字段无效")
            if not isinstance(legal_count, int) or legal_count < len(candidates):
                raise ValueError(f"ply {ply}: invalid legal move count")
            if not isinstance(engine_best, str):
                raise ValueError(f"ply {ply}: 缺少 engineBestMove")

            request = build_laya_request(fen, [""] * legal_count, candidates)
            criteria = request["questions"]["move"]["criteria"]
            candidate_moves = [candidate["move"] for candidate in candidates]
            if set(criteria) != set(candidate_moves):
                raise ValueError(f"ply {ply}: candidate keys changed")
            if engine_best not in criteria:
                raise ValueError(f"ply {ply}: engineBestMove 不在候选中")

            started = time.perf_counter()
            response = client.decide(request["state"], request["questions"])
            latency_ms = round((time.perf_counter() - started) * 1000)
            choice = _extract_laya_choice(response, candidates)
            if choice not in criteria:
                raise ValueError(f"ply {ply}: Kev 返回无效候选 {choice!r}")
            usage = response.get("usage") if isinstance(response, dict) else None
            input_tokens = None
            if isinstance(usage, dict):
                input_tokens = usage.get("input_tokens", usage.get("inputTokens"))
            rows.append(
                {
                    "ply": ply,
                    "side": event.get("side"),
                    "fen": fen,
                    "legalMoveCount": legal_count,
                    "candidateMoves": candidate_moves,
                    "engineBest": engine_best,
                    "choice": choice,
                    "same": choice == engine_best,
                    "latencyMs": latency_ms,
                    "inputTokens": input_tokens,
                }
            )
            write_result(args.output, baseline, args.sample, service, run, rows, 0, "running", health)
    except Exception as error:
        failure = {"type": type(error).__name__, "message": str(error)}
        result = write_result(args.output, baseline, args.sample, service, run, rows, 1, "failed", health, failure)
        print(json.dumps({"path": str(args.output), "status": result["status"], "summary": result["summary"], "failure": failure}, ensure_ascii=False))
        return 1

    result = write_result(args.output, baseline, args.sample, service, run, rows, 0, "completed", health)
    print(json.dumps({"path": str(args.output), "status": result["status"], "summary": result["summary"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
