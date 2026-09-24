import unittest
from pathlib import Path
from unittest.mock import patch

import engine


BLACK_CHECK_CYCLE_FEN = "3ak4/9/4P4/9/8p/6P2/1p6P/1nR2A2B/4A4/5K1r1 w - - 12 50"
BLACK_CHECK_CYCLE = ["f0f1", "h0h1", "f1f0", "h1h0"] * 2
ORDINARY_CYCLE_FEN = "3k5/4a4/b2a2n1b/3P4p/4N2n1/4p2NP/9/B2A5/4A4/2B1K4 b - - 20 58"
ORDINARY_CYCLE = ["g7i8", "d6e6", "i8g7", "e6d6"] * 2
TIE_INITIAL_FEN = "4k3r/9/9/9/9/9/9/9/9/R3K4 w - - 0 1"
TIE_HISTORY = ["a0a1", "i9i8", "a1a0", "i8i9"] * 2


def replay(initial_fen, moves):
    current = initial_fen
    for move in moves:
        current = engine.apply_uci_move(current, move)
    return current


TIE_CURRENT_FEN = replay(TIE_INITIAL_FEN, TIE_HISTORY)


class StubRuntime:
    def legal_moves(self, fen):
        return ["a0a1"], []

    def in_check(self, fen):
        return False, []


class TieRuntime:
    def __init__(self, bestmove, depth=3, bound=None):
        self.bestmove = bestmove
        self.depth = depth
        self.bound = bound

    def legal_moves(self, fen):
        return ["a0a1", "a0a2"], []

    def in_check(self, fen):
        return False, []

    def analyze(self, fen, seconds=0.3, multipv=5, initial_fen=None, moves=None):
        source = ["a0a1", "a0a2"] if self.bestmove == "a0a1" else ["a0a2", "a0a1"]
        return {
            "bestmove": self.bestmove,
            "candidates": [
                {"move": move, "score": {"cp": 100}, "depth": self.depth, "pv": [move], "bound": self.bound if move == "a0a2" else None}
                for move in source
            ],
            "raw": [],
        }


class ChoiceLaya:
    def __init__(self, choice):
        self.choice = choice

    def decide(self, fen, legal_moves, candidates):
        return {"answers": {"move": {"choice": self.choice}}}


def game_from_cycle(initial_fen, moves, gave_check):
    current = initial_fen
    history = []
    for ply, move in enumerate(moves, 1):
        side = engine.side_name(current.split()[1])
        history.append({"ply": ply, "side": side, "move": move, "gaveCheck": gave_check(side, move)})
        current = engine.apply_uci_move(current, move)
    return engine.Game(
        id="fixture",
        puzzle={"fen": initial_fen},
        fen=current,
        turn=engine.side_name(current.split()[1]),
        legal_moves=["a0a1"],
        in_check=False,
        moves=history,
    )


class RepetitionAdjudicationTests(unittest.TestCase):
    def test_black_perpetual_check_is_black_loss(self):
        game = game_from_cycle(BLACK_CHECK_CYCLE_FEN, BLACK_CHECK_CYCLE, lambda side, move: side == "black")
        adjudication = engine._repetition_adjudication(game)
        self.assertEqual(adjudication["kind"], "perpetual_check")
        self.assertEqual(adjudication["loser"], "black")
        self.assertEqual(adjudication["occurrences"], [0, 4, 8])

        engine.refresh_game(game, StubRuntime())
        self.assertEqual(game.status, "red_win")
        self.assertEqual(game.adjudication["loser"], "black")
        self.assertEqual(sum(item.get("kind") == "system" for item in game.logs), 1)

    def test_non_checking_repetition_is_a_draw_with_explicit_reason(self):
        game = game_from_cycle(ORDINARY_CYCLE_FEN, ORDINARY_CYCLE, lambda side, move: False)
        engine.refresh_game(game, StubRuntime())
        self.assertEqual(game.status, "draw")
        self.assertEqual(game.adjudication["kind"], "repetition")
        self.assertIn("未检测到单方长将", game.reason)

    def test_red_perpetual_check_is_black_loss(self):
        game = game_from_cycle(BLACK_CHECK_CYCLE_FEN, BLACK_CHECK_CYCLE, lambda side, move: side == "red")
        engine.refresh_game(game, StubRuntime())
        self.assertEqual(game.status, "black_win")
        self.assertEqual(game.adjudication["kind"], "perpetual_check")
        self.assertEqual(game.adjudication["loser"], "red")

    def test_repeated_checks_without_repetition_are_not_adjudicated(self):
        game = game_from_cycle(BLACK_CHECK_CYCLE_FEN, BLACK_CHECK_CYCLE[:7], lambda side, move: side == "black")
        engine.refresh_game(game, StubRuntime())
        self.assertEqual(game.status, "playing")
        self.assertIsNone(game.adjudication)

    def test_partial_check_cycle_is_ordinary_repetition(self):
        game = game_from_cycle(
            BLACK_CHECK_CYCLE_FEN,
            BLACK_CHECK_CYCLE,
            lambda side, move: side == "black" and move == "h0h1",
        )
        engine.refresh_game(game, StubRuntime())
        self.assertEqual(game.status, "draw")
        self.assertEqual(game.adjudication["kind"], "repetition")
        self.assertIsNone(game.adjudication["loser"])

    def test_mutual_perpetual_check_is_a_draw(self):
        game = game_from_cycle(BLACK_CHECK_CYCLE_FEN, BLACK_CHECK_CYCLE, lambda side, move: True)
        engine.refresh_game(game, StubRuntime())
        self.assertEqual(game.status, "draw")
        self.assertEqual(game.adjudication["kind"], "mutual_check")
        self.assertIsNone(game.adjudication["loser"])

    def test_perpetual_check_precedes_120_halfmove_draw(self):
        initial = BLACK_CHECK_CYCLE_FEN.replace(" - 12 50", " - 120 50")
        game = game_from_cycle(initial, BLACK_CHECK_CYCLE, lambda side, move: side == "black")
        engine.refresh_game(game, StubRuntime())
        self.assertEqual(game.status, "red_win")
        self.assertEqual(game.adjudication["kind"], "perpetual_check")

    def test_undo_clears_adjudication_and_refresh_is_idempotent(self):
        game = game_from_cycle(BLACK_CHECK_CYCLE_FEN, BLACK_CHECK_CYCLE, lambda side, move: side == "black")
        engine.refresh_game(game, StubRuntime())
        self.assertEqual(sum(item.get("kind") == "system" for item in game.logs), 1)
        engine.refresh_game(game, StubRuntime())
        self.assertEqual(sum(item.get("kind") == "system" for item in game.logs), 1)

        before = game_from_cycle(BLACK_CHECK_CYCLE_FEN, BLACK_CHECK_CYCLE[:-1], lambda side, move: side == "black")
        before.snapshot_round()
        game.rounds = before.rounds
        engine.refresh_game(before, StubRuntime())
        game.undo_round(StubRuntime())
        self.assertIsNone(game.adjudication)
        self.assertEqual(game.status, "playing")
        engine.refresh_game(game, StubRuntime())
        self.assertEqual(
            sum(
                item.get("kind") == "system" and isinstance(item.get("raw"), dict) and "adjudication" in item["raw"]
                for item in game.logs
            ),
            0,
        )

    def test_equal_score_repetition_tiebreak_keeps_or_guards_laya_choice(self):
        cases = [
            # Engine best repeats, Laya selects the fresh equal move: no rewrite.
            ("a0a1", "a0a2", "a0a2", False, False),
            # Engine best repeats, Laya selects that repeated move: guard it away.
            ("a0a1", "a0a1", "a0a2", True, True),
            # Engine best is fresh, Laya selects the repeated equal move: guard it away.
            ("a0a2", "a0a1", "a0a2", True, True),
        ]
        for bestmove, choice, expected, guarded, repetition_avoided in cases:
            with self.subTest(bestmove=bestmove, choice=choice):
                decision = engine.choose_laya_move(
                    TieRuntime(bestmove),
                    ChoiceLaya(choice),
                    TIE_CURRENT_FEN,
                    initial_fen=TIE_INITIAL_FEN,
                    moves=TIE_HISTORY,
                )
                self.assertEqual(decision.final_move, expected)
                self.assertEqual(decision.guarded, guarded)
                self.assertEqual(decision.repetition_avoided, repetition_avoided)
                self.assertEqual(decision.score_loss_cp, 0)
                self.assertEqual(decision.engine_best_move, bestmove)

    def test_different_depth_or_bound_does_not_trigger_repetition_guard(self):
        class DifferentDepth(TieRuntime):
            def analyze(self, *args, **kwargs):
                result = super().analyze(*args, **kwargs)
                result["candidates"][1]["depth"] = 2
                return result

        decision = engine.choose_laya_move(
            DifferentDepth("a0a1"),
            ChoiceLaya("a0a2"),
            TIE_CURRENT_FEN,
            initial_fen=TIE_INITIAL_FEN,
            moves=TIE_HISTORY,
        )
        self.assertTrue(decision.guarded)
        self.assertFalse(decision.repetition_avoided)

        decision = engine.choose_laya_move(
            TieRuntime("a0a1", bound="upperbound"),
            ChoiceLaya("a0a2"),
            TIE_CURRENT_FEN,
            initial_fen=TIE_INITIAL_FEN,
            moves=TIE_HISTORY,
        )
        self.assertTrue(decision.guarded)
        self.assertFalse(decision.repetition_avoided)

    def test_history_is_sent_to_uci_and_mismatch_is_rejected(self):
        initial = "4k3r/9/9/9/9/9/9/9/9/R3K4 w - - 0 1"
        current = engine.apply_uci_move(initial, "a0a1")
        process = engine.UciProcess(Path("/tmp/pikafish-test"))
        commands = []

        def read_until(predicate, timeout):
            return ["readyok"] if timeout >= 20 else ["bestmove a0a1"]

        with patch.object(process, "start"), patch.object(process, "_send", side_effect=commands.append), patch.object(process, "_read_until", side_effect=read_until):
            process.analyze(current, seconds=0.1, initial_fen=initial, moves=["a0a1"])
        self.assertIn(f"position fen {initial} moves a0a1", commands)
        with self.assertRaises(engine.GameRuleError):
            process.analyze(initial, seconds=0.1, initial_fen=initial, moves=["a0a1"])


if __name__ == "__main__":
    unittest.main()
