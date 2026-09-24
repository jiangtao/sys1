"""Regressions for native engine exits and the Rule60 boundary."""

import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import engine


TARGET_FEN = "4ka1R1/9/9/9/8p/3c2P2/5r2P/B3BA3/4A4/4K4 b - - 120 109"
INITIAL_FEN = "4k3r/9/9/9/9/9/9/9/9/R3K4 w - - 0 1"


class FailingRuntime:
    def legal_moves(self, fen):
        raise engine.EngineError("injected refresh failure")

    def in_check(self, fen):
        raise AssertionError("in_check must not run after legal_moves fails")


class GeometryRuntime:
    def legal_moves(self, fen):
        return [f"m{i:02d}" for i in range(31)], []

    def in_check(self, fen):
        return False, []


def _game() -> engine.Game:
    return engine.Game(
        id="fixture",
        puzzle={"fen": INITIAL_FEN},
        fen=INITIAL_FEN,
        turn="red",
        legal_moves=["a0a1"],
        in_check=False,
    )


class EngineFailureTests(unittest.TestCase):
    def test_geometry_queries_clamp_only_the_temporary_halfmove_field(self):
        process = engine.UciProcess(Path("/tmp/pikafish-test"))
        commands = []

        with patch.object(process, "start"), patch.object(process, "_send", side_effect=commands.append), patch.object(
            process, "_read_until", return_value=["a0a1: 1", "Nodes searched: 1"]
        ):
            process.legal_moves(TARGET_FEN)
        self.assertIn("position fen 4ka1R1/9/9/9/8p/3c2P2/5r2P/B3BA3/4A4/4K4 b - - 119 109", commands)
        self.assertEqual(TARGET_FEN.split()[4], "120")

    def test_analyze_rejects_the_120_halfmove_terminal_boundary(self):
        process = engine.UciProcess(Path("/tmp/pikafish-test"))
        with self.assertRaises(engine.UciError) as raised:
            process.analyze(TARGET_FEN, seconds=0.1)
        self.assertIn("120", str(raised.exception))
        self.assertEqual(raised.exception.details["halfmove"], 120)

    def test_real_process_keeps_critical_output_and_restarts(self):
        script = """#!/usr/bin/env python3
import sys
from pathlib import Path
marker = Path({marker!r})
print('uciok', flush=True)
for raw in sys.stdin:
    command = raw.strip()
    if command == 'isready':
        print('readyok', flush=True)
    elif command == 'go perft 1':
        if not marker.exists():
            marker.write_text('failed')
            print('info string CRITICAL ERROR: Rule60 counter out of range', flush=True)
            raise SystemExit(7)
        print('a0a1: 1', flush=True)
        print('Nodes searched: 1', flush=True)
    elif command == 'quit':
        break
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "failed-once"
            binary = root / "fake-pikafish"
            binary.write_text(script.format(marker=str(marker)), encoding="utf-8")
            binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
            process = engine.UciProcess(binary)
            with self.assertRaises(engine.UciError) as raised:
                process.legal_moves(INITIAL_FEN)
            self.assertIn("CRITICAL ERROR", str(raised.exception))
            self.assertIn("Rule60", raised.exception.details["output"])
            self.assertEqual(raised.exception.details["command"], "go perft 1")
            moves, _ = process.legal_moves(INITIAL_FEN)
            self.assertEqual(moves, ["a0a1"])
            process.stop()

    def test_120_refresh_is_draw_without_mutating_real_fen(self):
        game = engine.Game(
            id="fixture",
            puzzle={"fen": TARGET_FEN},
            fen=TARGET_FEN,
            turn="black",
            legal_moves=[],
            in_check=False,
        )
        engine.refresh_game(game, GeometryRuntime())
        self.assertEqual(game.fen, TARGET_FEN)
        self.assertEqual(len(game.legal_moves), 31)
        self.assertEqual(game.status, "draw")
        self.assertEqual(game.adjudication["kind"], "no_progress")

    def test_human_append_rolls_back_when_refresh_fails(self):
        runtime = FailingRuntime()
        game = _game()
        before = game.to_dict()
        with self.assertRaises(engine.EngineError):
            engine.append_human_move(game, "a0a1", runtime)
        self.assertEqual(game.to_dict(), before)
        self.assertEqual(game.rounds, [])

    def test_undo_failure_restores_position_history_and_archive(self):
        game = _game()
        engine.append_human_move(game, "a0a1", GeometryRuntime())
        before = game.to_persist_dict()
        with self.assertRaises(engine.EngineError):
            game.undo_round(FailingRuntime())
        self.assertEqual(game.to_persist_dict(), before)

    def test_check_query_failure_restores_cached_legal_moves(self):
        class CheckFailure(GeometryRuntime):
            def in_check(self, fen):
                raise engine.UciError("checkers process exited")

        game = _game()
        before = game.to_persist_dict()
        with self.assertRaises(engine.UciError):
            engine.append_human_move(game, "a0a1", CheckFailure())
        self.assertEqual(game.to_persist_dict(), before)

    def test_laya_append_rolls_back_when_refresh_fails(self):
        runtime = FailingRuntime()
        game = _game()
        game.mode = "selfplay"
        decision = engine.Decision(
            final_move="a0a1",
            laya_choice="a0a1",
            guarded=False,
            guard_reason=None,
            candidates=[{"move": "a0a1", "selected": True}],
            raw_laya={},
            raw_uci=[],
            depth=1,
            score=None,
            elapsed_ms=1,
            timings={"searchMs": 1, "layaMs": 0, "totalMs": 1},
            legal_move_count=1,
            fen_before=INITIAL_FEN,
        )
        before = game.to_dict()
        with self.assertRaises(engine.EngineError):
            engine.append_laya_move(game, decision, runtime)
        self.assertEqual(game.to_dict(), before)
        self.assertEqual(game.rounds, [])


if __name__ == "__main__":
    unittest.main()
