import json
import unittest
from pathlib import Path

from engine import (
    apply_uci_move,
    build_candidates,
    load_puzzles,
    parse_fen,
    scores_equivalent,
)


STANDARD_FEN = "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR w - - 0 1"


class FenAndTreeTests(unittest.TestCase):
    def test_standard_fen_has_black_top_and_red_bottom(self):
        board, side, _ = parse_fen(STANDARD_FEN)
        self.assertEqual(side, "w")
        self.assertEqual(board[0][0], "r")
        self.assertEqual(board[9][0], "R")
        self.assertEqual(len(board), 10)
        self.assertTrue(all(len(row) == 9 for row in board))

    def test_apply_move_updates_side_and_halfmove(self):
        fen = "4k4/9/9/9/9/9/9/9/4R4/4K4 w - - 0 1"
        next_fen = apply_uci_move(fen, "e1e2")
        self.assertEqual(next_fen.split()[1], "b")
        self.assertEqual(next_fen.split()[4], "1")
        board, _, _ = parse_fen(next_fen)
        self.assertEqual(board[7][4], "R")
        self.assertIsNone(board[8][4])

    def test_candidates_keep_engine_bestmove_first(self):
        legal = ["a3a4", "b2e2"]
        analysis = {
            "bestmove": "b2e2",
            "candidates": [
                {"move": "a3a4", "score": {"cp": 300, "type": "cp", "value": 300}, "depth": 18, "pv": ["a3a4"]},
                {"move": "b2e2", "score": {"cp": 10, "type": "cp", "value": 10}, "depth": 18, "pv": ["b2e2"]},
            ],
        }
        candidates = build_candidates(STANDARD_FEN, analysis, legal)
        self.assertEqual(candidates[0]["move"], "b2e2")

    def test_score_equivalence_requires_same_value(self):
        self.assertTrue(scores_equivalent({"cp": 12}, {"type": "cp", "value": 12}))
        self.assertTrue(scores_equivalent({"mate": 2}, {"type": "mate", "value": 2}))
        self.assertFalse(scores_equivalent({"cp": 12}, {"cp": 13}))
        self.assertFalse(scores_equivalent({"mate": 2}, {"cp": 2000}))

    def test_puzzle_collection_contains_ten_red_first_positions(self):
        puzzles = load_puzzles(Path(__file__).with_name("puzzles.json"))
        self.assertEqual(len(puzzles), 10)
        self.assertEqual({item["id"] for item in puzzles}, {f"endgame-{index:02d}" for index in range(1, 11)})
        for item in puzzles:
            _, side, _ = parse_fen(item["fen"])
            self.assertEqual(side, "w")


if __name__ == "__main__":
    unittest.main()
