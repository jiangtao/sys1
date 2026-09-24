"""Verify both game modes expose the same Laya decision factors."""

import json
import unittest
from unittest.mock import patch

import engine


FEN = "4k3r/9/9/9/9/9/9/9/9/R3K4 w - - 0 1"
PUZZLE = {
    "id": "decision-factor-fixture",
    "title": "fixture",
    "subtitle": "fixture",
    "difficulty": "fixture",
    "fen": FEN,
}


def candidates():
    return [
        {
            "move": "a0a1",
            "label": "车 a0→a1",
            "score": {"type": "cp", "value": 123},
            "depth": 12,
            "pv": ["a0a1", "a9a8", "b0b1", "b9b8"],
            "bound": None,
            "repetitionCount": 0,
        },
        {
            "move": "h9h8",
            "label": "车 h9→h8",
            "score": {"type": "mate", "value": -2},
            "depth": 11,
            "pv": ["h9h8", "a1a2"],
            "bound": "upperbound",
            "repetitionCount": 2,
        },
        {
            "move": "a0a2",
            "label": "车 a0→a2",
            "score": {"cp": -45},
            "depth": 10,
            "pv": ["a0a2"],
            "bound": None,
            "repetitionCount": 1,
        },
        {"move": "a0a3", "label": "车 a0→a3", "score": None, "depth": 9, "pv": [], "bound": None, "repetitionCount": 0},
        {"move": "a0a4", "label": "车 a0→a4", "score": {"type": "cp", "value": 0}, "depth": 8, "pv": [], "bound": None, "repetitionCount": 0},
    ]


class DecisionFactorTests(unittest.TestCase):
    def test_builder_keeps_five_keys_and_auditable_factor_text(self):
        original = json.loads(json.dumps(candidates(), ensure_ascii=False))
        payload = engine.build_laya_request(FEN, [item["move"] for item in original], original)
        criteria = payload["questions"]["move"]["criteria"]

        self.assertEqual(list(criteria), [item["move"] for item in original])
        self.assertEqual(len(criteria), 5)
        self.assertTrue(criteria["a0a1"].startswith("正确的首选着法："))
        self.assertTrue(criteria["h9h8"].startswith("非首选着法："))
        self.assertIn("引擎首选", criteria["a0a1"])
        self.assertIn("分值 +1.23", criteria["a0a1"])
        self.assertIn("深度 12", criteria["a0a1"])
        self.assertIn("重复 0", criteria["a0a1"])
        self.assertIn("被将杀风险", criteria["h9h8"])
        self.assertIn("边界 upperbound", criteria["h9h8"])
        self.assertIn("深度 11", criteria["h9h8"])
        self.assertEqual(original, candidates())
        self.assertLess(len(json.dumps(payload, ensure_ascii=False)), 4000)

    def test_client_sends_the_same_builder_payload(self):
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"answers":{"move":{"choice":"a0a1"}}}'

        captured = []

        def fake_urlopen(request, timeout):
            captured.append(json.loads(request.data.decode("utf-8")))
            return Response()

        moves = [item["move"] for item in candidates()]
        with patch("engine.urllib.request.urlopen", fake_urlopen):
            engine.LayaClient("http://example.test").decide(FEN, moves, candidates())
        self.assertEqual(captured[0], engine.build_laya_request(FEN, moves, candidates()))

    def test_human_and_selfplay_laya_logs_share_factor_version_and_criteria(self):
        class Runtime:
            def legal_moves(self, fen):
                return (["a0a1"] if " w " in fen else ["i9i8"]), []

            def in_check(self, fen):
                return False, []

            def analyze(self, fen, seconds=0.3, multipv=5, *, initial_fen=None, moves=None):
                move = "a0a1" if " w " in fen else "i9i8"
                return {
                    "bestmove": move,
                    "candidates": [{"move": move, "score": {"cp": 12}, "depth": 3, "pv": [move]}],
                    "raw": [],
                }

        class Laya:
            def decide(self, fen, legal_moves, candidate_list):
                return {"answers": {"move": {"choice": candidate_list[0]["move"]}}}

        runtime = Runtime()
        laya = Laya()
        human_game = engine.make_game(PUZZLE, runtime, mode="human")
        engine.append_human_move(human_game, "a0a1", runtime)
        human_decision = engine.choose_laya_move(runtime, laya, human_game.fen, initial_fen=FEN, moves=["a0a1"])
        engine.append_laya_move(human_game, human_decision, runtime)

        selfplay_game = engine.make_game(PUZZLE, runtime, mode="selfplay")
        red_decision = engine.choose_laya_move(runtime, laya, selfplay_game.fen, initial_fen=FEN, moves=[])
        engine.append_laya_move(selfplay_game, red_decision, runtime)
        black_decision = engine.choose_laya_move(runtime, laya, selfplay_game.fen, initial_fen=FEN, moves=["a0a1"])
        engine.append_laya_move(selfplay_game, black_decision, runtime)

        human_log = human_game.logs[-1]
        selfplay_log = selfplay_game.logs[-1]
        self.assertEqual(human_log["decisionFactorVersion"], engine.LAYA_DECISION_FACTOR_VERSION)
        self.assertEqual(selfplay_log["decisionFactorVersion"], engine.LAYA_DECISION_FACTOR_VERSION)
        self.assertEqual(engine.LAYA_DECISION_FACTOR_VERSION, "best-directed-v3")
        self.assertEqual(human_log["decisionCriteria"], human_decision.factor_criteria)
        self.assertEqual(selfplay_log["decisionCriteria"], black_decision.factor_criteria)
        self.assertEqual(human_log["decisionCriteria"], selfplay_log["decisionCriteria"])


if __name__ == "__main__":
    unittest.main()
