import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import engine
import server


FEN = "4k3r/9/9/9/9/9/9/9/9/R3K4 w - - 0 1"


class FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return b'{"answers":{"move":{"choice":"a0a1"}}}'


class FastBoundaryTests(unittest.TestCase):
    def test_uci_movetime_keeps_subsecond_values(self):
        process = engine.UciProcess(Path("/tmp/pikafish-test"))
        commands = []

        def read_until(predicate, timeout):
            return ["readyok"] if timeout >= 20 else ["bestmove a0a1"]

        with patch.object(process, "start"), patch.object(process, "_send", side_effect=commands.append), patch.object(process, "_read_until", side_effect=read_until):
            process.analyze(FEN, seconds=0.3)
            process.analyze(FEN, seconds=0.1)

        self.assertIn("go movetime 300", commands)
        self.assertIn("go movetime 100", commands)

    def test_seconds_rejects_non_finite_values_and_bool(self):
        self.assertEqual(server.parse_seconds({"seconds": 0.1}), 0.1)
        for value in (float("nan"), float("inf"), float("-inf"), True, False):
            with self.subTest(value=value):
                with self.assertRaises(server.ApiError):
                    server.parse_seconds({"seconds": value})


class CompactPayloadTests(unittest.TestCase):
    def test_laya_payload_is_compact_and_does_not_mutate_candidates(self):
        candidates = [
            {
                "move": "a0a1",
                "label": "车 a0→a1",
                "score": {"cp": 123, "type": "cp", "value": 123},
                "scorePerspective": "red",
                "depth": 12,
                "pv": ["a0a1", "a9a8", "b0b1", "b9b8"],
                "selected": True,
                "bound": "lowerbound",
            },
            {
                "move": "h0h1",
                "label": "车 h0→h1",
                "score": {"mate": 3, "type": "mate", "value": 3},
                "scorePerspective": "red",
                "depth": 12,
                "pv": ["h0h1", "h9h8", "c0c1", "c9c8"],
                "selected": False,
                "bound": None,
            },
        ]
        original = copy.deepcopy(candidates)
        captured = []

        def fake_urlopen(request, timeout):
            captured.append(json.loads(request.data.decode("utf-8")))
            return FakeResponse()

        with patch("engine.urllib.request.urlopen", fake_urlopen):
            result = engine.LayaClient("http://example.test").decide(FEN, ["a0a1", "h0h1", "b0b1"], candidates)

        self.assertEqual(result["answers"]["move"]["choice"], "a0a1")
        self.assertEqual(candidates, original)
        state = captured[0]["state"]
        self.assertNotIn("legalMoves", state)
        self.assertEqual(state["legalMoveCount"], 3)
        self.assertEqual(state["side"], "red")
        self.assertEqual(state["scorePerspective"], "red")
        self.assertEqual(state["candidates"][0], {"move": "a0a1", "score": {"type": "cp", "value": 123}, "pv": ["a0a1", "a9a8", "b0b1"], "bound": "lowerbound", "repetitionCount": 0})
        self.assertEqual(state["candidates"][1]["score"], {"type": "mate", "value": 3})
        criteria = captured[0]["questions"]["move"]["criteria"]
        self.assertIn("引擎首选", criteria["a0a1"])
        self.assertIn("分值 +1.23", criteria["a0a1"])
        self.assertIn("深度 12", criteria["a0a1"])
        self.assertIn("边界 lowerbound", criteria["a0a1"])
        self.assertIn("候选2", criteria["h0h1"])
        self.assertIn("将杀 +3步", criteria["h0h1"])


class LogRetentionTests(unittest.TestCase):
    def test_decision_log_keeps_full_pv_and_timings(self):
        move = "a0a1"
        full_pv = [move, "a9a8", "b0b1", "b9b8"]

        class Runtime:
            def legal_moves(self, fen):
                return [move], []

            def analyze(self, fen, seconds=0.3, multipv=5):
                return {
                    "bestmove": move,
                    "candidates": [{"move": move, "score": {"cp": 12, "type": "cp", "value": 12}, "depth": 3, "pv": full_pv}],
                    "raw": ["info depth 3 pv " + " ".join(full_pv)],
                }

            def in_check(self, fen):
                return False, []

        class Laya:
            def decide(self, fen, legal_moves, candidates):
                return {"answers": {"move": {"choice": move}}}

        puzzle = {"id": "fast-fixture", "title": "fixture", "subtitle": "fixture", "difficulty": "fixture", "fen": FEN}
        runtime = Runtime()
        game = engine.make_game(puzzle, runtime)
        decision = engine.choose_laya_move(runtime, Laya(), FEN, seconds=0.3)
        engine.append_laya_move(game, decision, runtime)
        log = game.logs[-1]
        self.assertEqual(log["candidates"][0]["pv"], full_pv)
        self.assertEqual(log["raw"]["uci"], ["info depth 3 pv a0a1 a9a8 b0b1 b9b8"])
        self.assertEqual(set(log["timings"]), {"searchMs", "layaMs", "modelMs", "totalMs"})
        self.assertEqual(log["timings"]["modelMs"], log["timings"]["layaMs"])


if __name__ == "__main__":
    unittest.main()
