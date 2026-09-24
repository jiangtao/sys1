import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen

import engine
import server


PUZZLE = {
    "id": "fixture",
    "title": "fixture",
    "subtitle": "fixture",
    "difficulty": "fixture",
    "fen": "4k3r/9/9/9/9/9/9/9/9/R3K4 w - - 0 1",
}


def position_key(fen):
    fields = fen.split()
    return " ".join(fields[:2])


class FakeRuntime:
    def __init__(self, move_by_position):
        self.move_by_position = move_by_position

    def legal_moves(self, fen):
        move = self.move_by_position.get(position_key(fen))
        return ([move] if move else []), []

    def in_check(self, fen):
        return False, []

    def analyze(self, fen, seconds=8, multipv=5, *, initial_fen=None, moves=None):
        move = self.move_by_position[position_key(fen)]
        return {
            "bestmove": move,
            "candidates": [{"move": move, "score": {"cp": 12, "type": "cp", "value": 12}, "depth": 3, "pv": [move]}],
            "raw": [],
        }


class FakeLaya:
    def __init__(self):
        self.requests = []

    def decide(self, fen, legal_moves, candidates):
        self.requests.append((fen, legal_moves, candidates))
        return {"answers": {"move": {"choice": candidates[0]["move"]}}}


def make_runtime(moves):
    current = PUZZLE["fen"]
    move_by_position = {}
    for move in moves:
        move_by_position[position_key(current)] = move
        current = engine.apply_uci_move(current, move)
    move_by_position[position_key(current)] = moves[0]
    return FakeRuntime(move_by_position)


def run_laya_move(game, runtime, laya):
    decision = engine.choose_laya_move(runtime, laya, game.fen, seconds=1)
    engine.append_laya_move(game, decision, runtime)


class SelfplayEngineTests(unittest.TestCase):
    def test_red_then_black_are_real_laya_moves_and_log_side(self):
        runtime = make_runtime(["a0a1", "i9i8"])
        laya = FakeLaya()
        game = engine.make_game(PUZZLE, runtime, mode="selfplay")

        run_laya_move(game, runtime, laya)
        self.assertEqual(game.turn, "black")
        self.assertEqual(game.logs[-1]["side"], "red")
        self.assertEqual(game.logs[-1]["agent"], "Laya · 红方")
        self.assertEqual(game.logs[-1]["scorePerspective"], "red")

        run_laya_move(game, runtime, laya)
        self.assertEqual(game.turn, "red")
        self.assertEqual(game.logs[-1]["side"], "black")
        self.assertEqual(game.logs[-1]["agent"], "Laya · 黑方")
        self.assertEqual(game.logs[-1]["scorePerspective"], "black")
        self.assertEqual(len(laya.requests), 2)

    def test_selfplay_undo_restores_the_side_before_one_ply(self):
        runtime = make_runtime(["a0a1", "i9i8"])
        laya = FakeLaya()
        game = engine.make_game(PUZZLE, runtime, mode="selfplay")
        run_laya_move(game, runtime, laya)
        run_laya_move(game, runtime, laya)

        game.undo_round(runtime)
        self.assertEqual(game.turn, "black")
        self.assertEqual(len(game.moves), 1)
        self.assertEqual(game.archive_logs[-1]["side"], "black")
        game.undo_round(runtime)
        self.assertEqual(game.turn, "red")
        self.assertEqual(len(game.moves), 0)
        self.assertIn("red", [item["side"] for item in game.archive_logs])

    def test_threefold_position_is_a_draw(self):
        cycle = ["a0a1", "i9i8", "a1a0", "i8i9"]
        runtime = make_runtime(cycle)
        laya = FakeLaya()
        game = engine.make_game(PUZZLE, runtime, mode="selfplay")
        for move_number in range(8):
            run_laya_move(game, runtime, laya)
            if move_number < 7:
                self.assertEqual(game.status, "playing")
        self.assertEqual(game.status, "draw")
        self.assertIn("三次", game.reason or "")

    def test_round_snapshots_are_compact_and_old_rounds_are_migrated(self):
        runtime = make_runtime(["a0a1", "i9i8"])
        laya = FakeLaya()
        game = engine.make_game(PUZZLE, runtime, mode="selfplay")
        run_laya_move(game, runtime, laya)
        run_laya_move(game, runtime, laya)
        self.assertTrue(all("moves" not in item and "logs" not in item for item in game.rounds))
        self.assertEqual([item["moves_count"] for item in game.rounds], [0, 1])
        self.assertEqual([item["logs_count"] for item in game.rounds], [0, 1])

        snapshot = game.to_dict()
        snapshot["rounds"] = [
            {
                "fen": game.rounds[0]["fen"],
                "turn": game.rounds[0]["turn"],
                "legal_moves": [],
                "in_check": False,
                "status": "playing",
                "reason": None,
                "moves": [],
                "logs": [],
            },
            {
                "fen": game.rounds[1]["fen"],
                "turn": game.rounds[1]["turn"],
                "legal_moves": [],
                "in_check": False,
                "status": "playing",
                "reason": None,
                "moves": game.moves[:1],
                "logs": game.logs[:1],
            },
        ]
        restored = engine.load_game(snapshot)
        self.assertEqual([item["moves_count"] for item in restored.rounds], [0, 1])
        self.assertFalse(any("moves" in item or "logs" in item for item in restored.rounds))

    def test_score_loss_requires_same_depth_and_unbounded_cp(self):
        class AnalysisRuntime(FakeRuntime):
            def __init__(self):
                super().__init__({position_key(PUZZLE["fen"]): "a0a1"})
                self.analysis = {}

            def legal_moves(self, fen):
                return ["a0a1", "b0b1"], []

            def analyze(self, fen, seconds=8, multipv=5):
                return self.analysis

        class ChoiceLaya:
            def decide(self, fen, legal_moves, candidates):
                return {"answers": {"move": {"choice": "b0b1"}}}

        runtime = AnalysisRuntime()
        runtime.analysis = {
            "bestmove": "a0a1",
            "candidates": [
                {"move": "a0a1", "score": {"cp": 100}, "depth": 3, "pv": ["a0a1"]},
                {"move": "b0b1", "score": {"cp": 50}, "depth": 3, "pv": ["b0b1"]},
            ],
        }
        self.assertEqual(engine.choose_laya_move(runtime, ChoiceLaya(), PUZZLE["fen"], 1).score_loss_cp, 50)
        runtime.analysis["candidates"][1]["depth"] = 2
        self.assertIsNone(engine.choose_laya_move(runtime, ChoiceLaya(), PUZZLE["fen"], 1).score_loss_cp)
        runtime.analysis["candidates"][1]["depth"] = 3
        runtime.analysis["candidates"][1]["bound"] = "upperbound"
        self.assertIsNone(engine.choose_laya_move(runtime, ChoiceLaya(), PUZZLE["fen"], 1).score_loss_cp)

    def test_laya_prompt_uses_fen_side(self):
        captured = []

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"answers":{"move":{"choice":"a0a1"}}}'

        def fake_urlopen(request, timeout):
            captured.append(json.loads(request.data.decode("utf-8")))
            return Response()

        client = engine.LayaClient("http://example.test")
        with patch("engine.urllib.request.urlopen", fake_urlopen):
            client.decide(PUZZLE["fen"], ["a0a1"], [{"move": "a0a1", "label": "x"}])
            black_fen = PUZZLE["fen"].replace(" w ", " b ")
            client.decide(black_fen, ["a0a1"], [{"move": "a0a1", "label": "x"}])
        self.assertIn("你执红", captured[0]["state"]["instruction"])
        self.assertIn("你执黑", captured[1]["state"]["instruction"])


class SelfplayHttpTests(unittest.TestCase):
    def test_wrong_mode_and_version_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            state = server.ServiceState(Path(directory) / "games.json")
            runtime = make_runtime(["a0a1", "i9i8"])
            state.runtime = runtime
            state.puzzle_map = {PUZZLE["id"]: PUZZLE}
            state.laya = FakeLaya()
            old_state = server.STATE
            server.STATE = state
            httpd = server.XiangqiHTTPServer(("127.0.0.1", 0), server.XiangqiHandler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{httpd.server_port}"
            try:
                response = self._post(base + "/api/game", {"puzzle": "fixture", "mode": "selfplay"})
                self.assertEqual(response[0], 200)
                game = response[1]
                self.assertEqual(game["mode"], "selfplay")
                forbidden = self._post(base + "/api/move", {"gameId": game["id"], "move": "a0a1", "version": 0})
                self.assertEqual(forbidden[0], 409)
                self.assertEqual(forbidden[1]["error"]["code"], "manual_move_forbidden")
                red = self._post(base + "/api/think", {"gameId": game["id"], "seconds": 1, "version": 0})
                self.assertEqual(red[0], 200)
                self.assertEqual(red[1]["turn"], "black")
                self.assertEqual(red[1]["logs"][-1]["side"], "red")
                black = self._post(base + "/api/think", {"gameId": game["id"], "seconds": 1, "version": 1})
                self.assertEqual(black[0], 200)
                self.assertEqual(black[1]["turn"], "red")
                self.assertEqual(black[1]["logs"][-1]["side"], "black")
                stale = self._post(base + "/api/think", {"gameId": game["id"], "version": 0})
                self.assertEqual(stale[0], 409)
                self.assertEqual(stale[1]["error"]["code"], "stale_game")
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)
                server.STATE = old_state

    @staticmethod
    def _post(url, payload):
        request = Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read())
        except Exception as error:
            if hasattr(error, "code"):
                return error.code, json.loads(error.read())
            raise


class PersistenceTests(unittest.TestCase):
    def test_reload_preserves_mode_logs_and_rounds(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "games.json"
            state = server.ServiceState(path)
            runtime = make_runtime(["a0a1"])
            state.runtime = runtime
            state.puzzle_map = {PUZZLE["id"]: PUZZLE}
            game = state.create_game("fixture", mode="selfplay")
            laya = FakeLaya()
            run_laya_move(game, runtime, laya)
            state.persist_games(game)

            restored = server.ServiceState(path)
            loaded = restored.games[game.id]
            self.assertEqual(loaded.mode, "selfplay")
            self.assertEqual(len(loaded.logs), 1)
            self.assertEqual(len(loaded.rounds), 1)
            loaded.undo_round(runtime)
            self.assertEqual(len(loaded.moves), 0)
            self.assertEqual(loaded.turn, "red")

    def test_concurrent_game_commits_keep_latest_snapshot_for_each_game(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "games.json"
            state = server.ServiceState(path)
            runtime = make_runtime(["a0a1", "i9i8"])
            state.runtime = runtime
            state.puzzle_map = {PUZZLE["id"]: PUZZLE}
            first = state.create_game("fixture", mode="selfplay")
            second = state.create_game("fixture", mode="selfplay")
            barrier = threading.Barrier(2)

            def advance(game, count):
                barrier.wait()
                for _ in range(count):
                    with game.lock:
                        run_laya_move(game, runtime, FakeLaya())
                        state.persist_games(game)

            workers = [
                threading.Thread(target=advance, args=(first, 2)),
                threading.Thread(target=advance, args=(second, 1)),
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=5)
            self.assertTrue(all(not worker.is_alive() for worker in workers))
            saved = json.loads(path.read_text(encoding="utf-8"))
            by_id = {item["id"]: item for item in saved["games"]}
            self.assertEqual(by_id[first.id]["version"], first.version)
            self.assertEqual(by_id[second.id]["version"], second.version)


if __name__ == "__main__":
    unittest.main()
