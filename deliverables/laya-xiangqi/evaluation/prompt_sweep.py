"""Measure local Laya choice wording on fixed saved Xiangqi positions."""

from __future__ import annotations

import argparse
import copy
import json
import sys
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from engine import _laya_candidate_factor, build_laya_request  # noqa: E402


def variant_request(event: dict, variant: str) -> dict:
    candidates = event["candidates"]
    request = copy.deepcopy(build_laya_request(event["before"], [""] * event["legalMoveCount"], candidates))
    criteria = request["questions"]["move"]["criteria"]
    best = candidates[0]["move"]
    # Freeze the ranked-v2 starting prompt used for the recorded sweep. The
    # live application may evolve independently after this evaluation.
    side_label = "红" if event["side"] == "red" else "黑"
    for index, item in enumerate(candidates):
        criteria[item["move"]] = _laya_candidate_factor(index, item, side_label)
    request["state"]["objective"] = "先保证最优评分；同等评分、同深度且无边界时，优先能推进局势且历史重复更少的分支。单方长将重复会判负。"
    request["questions"]["move"]["instructions"] = "先选引擎首选；只有评分、搜索深度相同且无边界时，优先进攻和重复次数少的候选。输出候选 move key。"
    if variant == "ranked-v2":
        pass
    elif variant == "plain":
        for item in candidates:
            criteria[item["move"]] = item.get("label") or item["move"]
    elif variant == "best-mark":
        for item in candidates:
            label = item.get("label") or item["move"]
            criteria[item["move"]] = ("Pikafish 最佳着法：" if item["move"] == best else "其他候选：") + label
        request["questions"]["move"]["instructions"] = "选择明确标注为 Pikafish 最佳着法的选项。"
    elif variant == "best-direct":
        for item in candidates:
            label = item.get("label") or item["move"]
            criteria[item["move"]] = ("正确的首选着法：" if item["move"] == best else "非首选着法：") + label
        request["state"]["objective"] = "按 Pikafish 排名选择第一名，最佳着法已在选项中明确标注。"
        request["questions"]["move"]["instructions"] = "请选择正确的首选着法。"
    elif variant == "best-direct-rich":
        for item in candidates:
            label = item.get("label") or item["move"]
            factor = criteria[item["move"]]
            criteria[item["move"]] = ("正确的首选着法：" if item["move"] == best else "非首选着法：") + label + "；" + factor
        request["state"]["objective"] = "按 Pikafish 排名选择第一名，最佳着法已在选项中明确标注。"
        request["questions"]["move"]["instructions"] = "请选择正确的首选着法。"
    elif variant == "best-direct-compact":
        for item in candidates:
            factor = criteria[item["move"]]
            criteria[item["move"]] = ("正确的首选着法：" if item["move"] == best else "非首选着法：") + factor
        request["state"]["objective"] = "按 Pikafish 排名选择第一名，最佳着法已在选项中明确标注。"
        request["questions"]["move"]["instructions"] = "请选择正确的首选着法。"
    elif variant == "rank-short":
        for index, item in enumerate(candidates):
            label = item.get("label") or item["move"]
            criteria[item["move"]] = f"引擎排序 {index + 1}/{len(candidates)} · {label}"
        request["questions"]["move"]["instructions"] = "选择引擎排序 1 的走法。"
    elif variant == "best-only":
        for item in candidates:
            label = item.get("label") or item["move"]
            criteria[item["move"]] = ("最优评分，推荐选此着：" if item["move"] == best else "备选：") + label
        request["state"]["instruction"] = "你执棋，请从候选里选择最优评分着法。"
        request["state"]["objective"] = "评分最高的走法。"
        request["questions"]["move"]["instructions"] = "选择标记为最优评分的候选。"
    else:
        raise ValueError(variant)
    return request


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=ROOT / "validation" / "decision-path-baseline-20260923.json")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--service", default="http://127.0.0.1:8765")
    parser.add_argument("--sample", choices=("train", "holdout"), default="train")
    parser.add_argument("--variants", nargs="+", default=["ranked-v2", "plain", "best-mark", "best-direct", "rank-short", "best-only"])
    args = parser.parse_args()
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    events = [event for event in baseline["path"] if len(event["candidates"]) > 1]
    # Odd and even plies are included in each split, so both playing sides are
    # represented. Keep holdout positions out of the wording selection step.
    events = [event for event in events if (event["ply"] % 4 in (0, 1)) == (args.sample == "train")]
    variants = tuple(args.variants)
    rows = []
    for event in events:
        row = {"ply": event["ply"], "side": event["side"], "best": event["engineBestMove"], "historical": event["laya"]["choice"], "variants": {}}
        for variant in variants:
            payload = variant_request(event, variant)
            request = urllib.request.Request(
                args.service.rstrip("/") + "/v1/decide",
                data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                result = json.load(response)
            answer = result["answers"]["move"]
            choice = answer["choice"]
            if choice not in payload["questions"]["move"]["criteria"]:
                raise ValueError(f"ply {event['ply']}: {variant} returned invalid choice {choice!r}")
            row["variants"][variant] = {
                "choice": choice,
                "bestProbability": answer.get("probabilities", {}).get(event["engineBestMove"]),
                "inputTokens": result.get("usage", {}).get("input_tokens"),
            }
        rows.append(row)
    summary = {
        variant: {
            "bestChosen": sum(row["variants"][variant]["choice"] == row["best"] for row in rows),
            "meanBestProbability": round(sum(row["variants"][variant]["bestProbability"] or 0 for row in rows) / len(rows), 4),
        }
        for variant in variants
    }
    result = {"schema": "laya-xiangqi-factor-wording-sweep/v1", "sourceGameId": baseline["gameId"], "sample": args.sample, "count": len(rows), "historicalBestChosen": sum(row["historical"] == row["best"] for row in rows), "summary": summary, "rows": rows}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"count": len(rows), "historicalBestChosen": result["historicalBestChosen"], "summary": summary}, ensure_ascii=False))


if __name__ == "__main__":
    main()
