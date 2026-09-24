"""Export one local Xiangqi game's observable decision path as JSON."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from engine import apply_uci_move  # noqa: E402


def decision_event(log: dict[str, Any], index: int) -> dict[str, Any]:
    before = log["fenBefore"]
    actual = log["finalMove"]
    response = log.get("raw", {}).get("laya", {})
    answer = response.get("answers", {}).get("move", {})
    return {
        "ply": index + 1,
        "side": log["side"],
        "before": before,
        "legalMoveCount": log.get("legalMoveCount"),
        "candidates": [
            {
                key: candidate.get(key)
                for key in ("move", "label", "score", "depth", "bound", "pv", "repetitionCount")
            }
            for candidate in log.get("candidates", [])
        ],
        "laya": {
            "requestId": response.get("requestId"),
            "choice": log.get("layaChoice"),
            "rank": log.get("layaRank"),
            "choiceScore": log.get("choiceScore"),
            "probabilities": answer.get("probabilities"),
            "confidence": answer.get("confidence"),
            "inputTokens": response.get("usage", {}).get("input_tokens"),
        },
        "decisionFactorVersion": log.get("decisionFactorVersion"),
        "decisionCriteria": log.get("decisionCriteria"),
        "engineBestMove": log.get("engineBestMove"),
        "executedMove": actual,
        "after": apply_uci_move(before, actual),
        "executedScore": log.get("score"),
        "scorePerspective": log.get("scorePerspective"),
        "guarded": bool(log.get("guarded")),
        "guardReason": log.get("guardReason"),
        "scoreLossCp": log.get("scoreLossCp"),
        "repetitionAvoided": bool(log.get("repetitionAvoided")),
        "gaveCheck": log.get("gaveCheck"),
        "timingsMs": log.get("timings"),
    }


def summarize(events: list[dict[str, Any]]) -> dict[str, Any]:
    comparable = [event for event in events if len(event["candidates"]) > 1]
    matched = [event for event in comparable if event["laya"]["choice"] == event["engineBestMove"]]
    accepted = [event for event in comparable if not event["guarded"]]
    losses = [event["scoreLossCp"] for event in comparable if isinstance(event["scoreLossCp"], (int, float))]
    risk = [
        event for event in comparable
        if isinstance(event["laya"]["choiceScore"], dict)
        and event["laya"]["choiceScore"].get("type") == "mate"
        and event["laya"]["choiceScore"].get("value", 0) < 0
        and not (
            isinstance(event["executedScore"], dict)
            and event["executedScore"].get("type") == "mate"
            and event["executedScore"].get("value", 0) < 0
        )
    ]
    return {
        "decisionCount": len(events),
        "nonForced": len(comparable),
        "exactEngineBestChosen": len(matched),
        "originalChoiceAccepted": len(accepted),
        "guardInterventions": sum(event["guarded"] for event in comparable),
        "rawChoiceRanks": dict(sorted(Counter(str(event["laya"]["rank"]) for event in comparable).items())),
        "comparableCpLossCount": len(losses),
        "meanComparableCpLoss": round(sum(losses) / len(losses), 2) if losses else None,
        "rawMateLossChoicesBlocked": len(risk),
        "bySide": {
            side: {
                "decisions": sum(event["side"] == side for event in events),
                "guardInterventions": sum(event["side"] == side and event["guarded"] for event in comparable),
            }
            for side in ("red", "black")
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("game_id")
    parser.add_argument("--store", type=Path, default=ROOT / "runtime" / "games.json")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--browser-start-ply", type=int)
    parser.add_argument("--browser-stop-ply", type=int)
    args = parser.parse_args()
    games = json.loads(args.store.read_text(encoding="utf-8"))["games"]
    game = next((item for item in games if item.get("id") == args.game_id), None)
    if game is None:
        parser.error(f"棋局不在存档中：{args.game_id}")

    events = [
        decision_event(log, index)
        for index, log in enumerate(item for item in game.get("logs", []) if item.get("kind") == "laya" and item.get("move"))
    ]
    if len(events) != len(game["moves"]):
        parser.error("此脚本目前只导出连续的双 Laya 着法；棋局含手动或系统走法")
    if events and events[-1]["after"] != game["fen"]:
        parser.error("决策路径的末尾局面与存档不一致")
    report = {
        "schema": "laya-xiangqi-decision-path/v1",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "gameId": game["id"],
        "puzzle": game["puzzle"]["title"],
        "mode": game.get("mode"),
        "status": game["status"],
        "version": game["version"],
        "browserActions": [
            {"action": "continue", "afterPly": args.browser_start_ply},
            {"action": "pause", "afterPly": args.browser_stop_ply},
        ] if args.browser_start_ply is not None and args.browser_stop_ply is not None else [],
        "summary": summarize(events),
        "path": events,
        "note": "候选和评分来自 Pikafish，laya.choice 是真实模型原选；最终落子经过守门。搜索时间有限，结果不保证数学全局最优。单步墙上时间没有历史记录，不据此推造。",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"path": str(args.output), "summary": report["summary"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
