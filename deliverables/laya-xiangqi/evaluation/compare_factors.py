"""Replay fixed self-play positions against the local Laya factor prompt.

This compares observable model choices on identical FENs and engine candidates;
it does not replay a new game or prove a globally optimal chess move.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from engine import build_laya_request  # noqa: E402


def summarize(rows: list[dict[str, Any]], field: str) -> dict[str, Any]:
    evaluated = [row for row in rows if len(row["candidateMoves"]) > 1]
    best_count = sum(row[field]["choice"] == row["engineBestMove"] for row in evaluated)
    probabilities = [row[field]["engineBestProbability"] for row in evaluated]
    return {
        "evaluated": len(evaluated),
        "exactEngineBestChosen": best_count,
        "exactEngineBestRate": round(best_count / len(evaluated), 4) if evaluated else None,
        "meanEngineBestProbability": round(sum(probabilities) / len(probabilities), 4) if probabilities else None,
        "bySide": {
            side: {
                "evaluated": sum(row["side"] == side for row in evaluated),
                "bestChosen": sum(row["side"] == side and row[field]["choice"] == row["engineBestMove"] for row in evaluated),
            }
            for side in ("red", "black")
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=ROOT / "validation" / "decision-path-baseline-20260923.json")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--service", default="http://127.0.0.1:8765")
    parser.add_argument("--sample", choices=("quarter", "half", "all"), default="half")
    args = parser.parse_args()

    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    events = [event for event in baseline["path"] if len(event["candidates"]) > 1]
    if args.sample == "quarter":
        events = [event for event in events if event["ply"] % 4 == 1]
    elif args.sample == "half":
        events = [event for event in events if event["ply"] % 4 in (0, 1)]

    rows: list[dict[str, Any]] = []
    for event in events:
        candidates = event["candidates"]
        # The builder only uses the legal-move list's length. The count comes
        # from the original engine response saved for this exact position.
        legal_count = event["legalMoveCount"]
        if not isinstance(legal_count, int) or legal_count < len(candidates):
            raise ValueError(f"ply {event['ply']}: invalid legal move count")
        request = build_laya_request(event["before"], [""] * legal_count, candidates)
        if set(request["questions"]["move"]["criteria"]) != {candidate["move"] for candidate in candidates}:
            raise ValueError(f"ply {event['ply']}: candidate keys changed")
        started = time.monotonic()
        http_request = urllib.request.Request(
            args.service.rstrip("/") + "/v1/decide",
            data=json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(http_request, timeout=30) as response:
            current = json.load(response)
        latency_ms = round((time.monotonic() - started) * 1000)
        answer = current.get("answers", {}).get("move", {})
        choice = answer.get("choice")
        if choice not in request["questions"]["move"]["criteria"]:
            raise ValueError(f"ply {event['ply']}: model returned an invalid choice {choice!r}")
        best = event["engineBestMove"]
        old_probabilities = event["laya"].get("probabilities") or {}
        new_probabilities = answer.get("probabilities") or {}
        rows.append(
            {
                "ply": event["ply"],
                "side": event["side"],
                "before": event["before"],
                "engineBestMove": best,
                "candidateMoves": [candidate["move"] for candidate in candidates],
                "baseline": {
                    "choice": event["laya"]["choice"],
                    "engineBestProbability": float(old_probabilities.get(best, 0)),
                    "inputTokens": event["laya"].get("inputTokens"),
                },
                "revised": {
                    "choice": choice,
                    "engineBestProbability": float(new_probabilities.get(best, 0)),
                    "inputTokens": current.get("usage", {}).get("input_tokens"),
                    "latencyMs": latency_ms,
                },
            }
        )

    result = {
        "schema": "laya-xiangqi-fixed-position-comparison/v1",
        "sourceGameId": baseline["gameId"],
        "sample": args.sample,
        "modelService": args.service,
        "sampledPlies": [row["ply"] for row in rows],
        "baseline": summarize(rows, "baseline"),
        "revised": summarize(rows, "revised"),
        "rows": rows,
        "note": "Same recorded positions and Pikafish candidates. Historical baseline choices are compared with fresh local Laya responses; this is a fixed-position comparison, not a new game or proof of global optimality.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"path": str(args.output), "baseline": result["baseline"], "revised": result["revised"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
