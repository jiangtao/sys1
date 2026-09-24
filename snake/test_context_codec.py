"""Tests for the lossless CTX2/CTX3 text codec."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

try:
    from .context_codec import (
        CTX2_HEADER,
        CTX3_HEADER,
        MAX_PATH_STEPS,
        compress_context,
        expand_context,
    )
except ImportError:
    from context_codec import CTX2_HEADER, CTX3_HEADER, MAX_PATH_STEPS, compress_context, expand_context


HISTORY_PREFIX = "HISTORY(last8 oldest>newest move:food:penalty) "


def _fixture() -> str:
    long_path = "L" * 12 + "U" + "L" * 6
    return "\n".join(
        [
            CTX2_HEADER,
            "BOARD 4x4 rows=..../..../..../....",
            "SNAKE H>T 1,1 links=R1",
            "FOOD 3,3 LEVEL 1 HP 100 E 100 NOFOOD 0 REMAIN 1 COMPLETES False",
            "PLANS id|dir|steps|proof|fullPath(?=unknown)|E=exitsAfter/food|S=reachableAfter/food|T=tailAfter/food|R=stateVisitsAfter: "
            f"long|left|15|proved|{long_path}|E3/3|S10/10|TTrue/True|R0;"
            "mixed|up|8|proved|ULDRULDR|E2/2|S8/8|TTrue/True|R0;"
            "unknown|down|?|unproved|?|E1/1|S1/1|TTrue/True|R0;"
            "dash|right|?|unproved|-|E1/1|S1/1|TTrue/True|R0",
            HISTORY_PREFIX + "L:0:0;U:1:0;L:0:1",
            f"PREVIOUS_EXECUTED_ROUTE(source=planner,sameFood,remainingOnly) {long_path}",
            "RULE actual; CTX_END",
        ]
    )


class ContextCodecTests(unittest.TestCase):
    def test_non_ctx_inputs_are_unchanged(self) -> None:
        for value in ("", "CTX1; old", "{\"context\": \"CTX2\"}"):
            self.assertEqual(compress_context(value), value)
            self.assertEqual(expand_context(value), value)

    def test_header_and_all_targeted_fields_round_trip(self) -> None:
        original = _fixture()
        compressed = compress_context(original)
        self.assertTrue(compressed.startswith(CTX3_HEADER))
        self.assertLess(len(CTX3_HEADER), len(CTX2_HEADER))
        self.assertEqual(expand_context(compressed), original)
        self.assertIn("|L12U1L6|", compressed)
        self.assertIn("|ULDRULDR|", compressed)
        self.assertIn("|?|", compressed)
        self.assertIn("|-|", compressed)
        self.assertIn("@long", compressed)
        self.assertIn("HISTORY moves=LUL food=010 penalty=001", compressed)
        self.assertIn("BOARD 4x4 rows=..../..../..../....", compressed)
        self.assertIn("SNAKE H>T 1,1 links=R1", compressed)

    def test_short_interleaved_path_is_left_raw(self) -> None:
        original = _fixture()
        compressed = compress_context(original)
        self.assertIn("|ULDRULDR|", compressed)
        self.assertNotIn("|U1L1D1R1U1L1D1R1|", compressed)

    def test_historical_requests_round_trip(self) -> None:
        path = Path(__file__).with_name("runtime") / "games" / "c5e94efc3cca40809e1fad7f8e2d66f0" / "decisions.jsonl"
        if not path.is_file():
            self.skipTest("Historical runtime log is not present in this checkout")
        seen = 0
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                record = json.loads(line)
                context = (record.get("request") or {}).get("state", {}).get("context")
                if not context:
                    continue
                seen += 1
                compressed = compress_context(context)
                self.assertEqual(expand_context(compressed), context)
        self.assertGreater(seen, 0)

    def test_invalid_rle_reference_and_history_raise(self) -> None:
        base = compress_context(_fixture())
        cases = (
            base.replace("L12U1L6", "L0U1L6", 1),
            base.replace("@long", "@missing", 1),
            base.replace("L12U1L6", "L1X1L6", 1),
            base.replace("HISTORY moves=LUL food=010 penalty=001", "HISTORY moves=LUL food=01 penalty=001", 1),
        )
        for malformed in cases:
            with self.subTest(malformed=malformed):
                with self.assertRaises(ValueError):
                    expand_context(malformed)

    def test_path_expansion_limit(self) -> None:
        context = "\n".join(
            [
                CTX3_HEADER,
                "PLANS id|dir|steps|proof|fullPath(?=unknown): plan|left|1|proved|L10001",
            ]
        )
        self.assertGreater(MAX_PATH_STEPS, 0)
        with self.assertRaises(ValueError):
            expand_context(context)


if __name__ == "__main__":
    unittest.main()
