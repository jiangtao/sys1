"""HTTP and persistence coverage for mixed Jev/Laya model duels."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import Request, urlopen

import engine
import jev_client
import server


FEN = "4k3r/9/9/9/9/9/9/9/9/R3K4 w - - 0 1"
PUZZLE = {
    "id": "duel-fixture",
    "title": "fixture",
    "subtitle": "fixture",
    "difficulty": "fixture",
    "fen": FEN,
}


class FakeRuntime:
    def health(self):
        return {"status": "ready", "binary": "fixture"}

    def legal_moves(self, fen: str):
        return (["a0a1"] if " w " in fen else ["i9i8"]), []

    def in_check(self, _fen: str):
        return False, []

    def analyze(self, fen: str, seconds: float = 0.3, multipv: int = 5, *, initial_fen=None, moves=None):
        move = "a0a1" if " w " in fen else "i9i8"
        return {
            "bestmove": move,
            "candidates": [{"move": move, "score": {"cp": 10}, "depth": 3, "pv": [move]}],
            "raw": [],
        }


class FakeProvider:
    def __init__(self, name: str, fail: bool = False, ready: bool = True) -> None:
        self.provider_name = name
        self.provider_label = "Jev" if name == "jev" else "Laya"
        self.fail = fail
        self.ready = ready
        self.calls: list[tuple[str, list[str], list[dict]]] = []

    def health(self):
        if self.ready:
            return 200, {"status": "ready", "model": self.provider_name}
        return 503, {"status": "unconfigured", "model": self.provider_name, "error": "未配置"}

    def decide(self, fen, legal_moves, candidates):
        self.calls.append((fen, list(legal_moves), candidates))
        if self.fail:
            raise jev_client.JevUnavailable("模型暂时无法推理")
        return {"model": self.provider_name, "answers": {"move": {"choice": candidates[0]["move"]}}}

    def close(self):
        return None


def post(base: str, path: str, payload: dict):
    request = Request(
        base + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read())
    except Exception as error:
        if hasattr(error, "code"):
            return error.code, json.loads(error.read())
        raise


class DuelApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_state = server.STATE
        self.directory = tempfile.TemporaryDirectory()
        self.state = server.ServiceState(Path(self.directory.name) / "games.json")
        self.state.runtime = FakeRuntime()
        self.state.puzzle_map = {PUZZLE["id"]: PUZZLE}
        self.state.laya = FakeProvider("laya")
        self.state.jev = FakeProvider("jev")
        server.STATE = self.state
        self.httpd = server.XiangqiHTTPServer(("127.0.0.1", 0), server.XiangqiHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        server.STATE = self.old_state
        self.directory.cleanup()

    def test_jev_red_laya_black_are_routed_by_turn_and_manual_move_is_forbidden(self):
        status, created = post(
            self.base,
            "/api/game",
            {"puzzle": PUZZLE["id"], "mode": "duel", "model": "jev", "opponentModel": "laya"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(created["mode"], "duel")
        self.assertEqual(created["model"], "jev")
        self.assertEqual(created["opponentModel"], "laya")

        forbidden = post(self.base, "/api/move", {"gameId": created["id"], "move": "a0a1", "version": 0})
        self.assertEqual(forbidden[0], 409)
        self.assertEqual(forbidden[1]["error"]["code"], "manual_move_forbidden")

        status, red = post(self.base, "/api/think", {"gameId": created["id"], "version": 0, "seconds": 0.1})
        self.assertEqual(status, 200)
        self.assertEqual(red["logs"][-1]["side"], "red")
        self.assertEqual(red["logs"][-1]["provider"], "jev")
        self.assertEqual(len(self.state.jev.calls), 1)
        self.assertEqual(len(self.state.laya.calls), 0)

        status, black = post(self.base, "/api/think", {"gameId": created["id"], "version": 1, "seconds": 0.1})
        self.assertEqual(status, 200)
        self.assertEqual(black["logs"][-1]["side"], "black")
        self.assertEqual(black["logs"][-1]["provider"], "laya")
        self.assertEqual(len(self.state.jev.calls), 1)
        self.assertEqual(len(self.state.laya.calls), 1)

    def test_provider_assignment_can_be_swapped(self):
        status, created = post(
            self.base,
            "/api/game",
            {"puzzle": PUZZLE["id"], "mode": "duel", "model": "laya", "opponentModel": "jev"},
        )
        self.assertEqual(status, 200)
        status, stepped = post(self.base, "/api/think", {"gameId": created["id"], "version": 0, "seconds": 0.1})
        self.assertEqual(status, 200)
        self.assertEqual(stepped["logs"][-1]["provider"], "laya")
        status, stepped = post(self.base, "/api/think", {"gameId": created["id"], "version": 1, "seconds": 0.1})
        self.assertEqual(status, 200)
        self.assertEqual(stepped["logs"][-1]["provider"], "jev")

    def test_jev_failure_is_recorded_for_the_current_side_only(self):
        self.state.jev.fail = True
        status, created = post(
            self.base,
            "/api/game",
            {"puzzle": PUZZLE["id"], "mode": "duel", "model": "jev", "opponentModel": "laya"},
        )
        self.assertEqual(status, 200)
        status, payload = post(self.base, "/api/think", {"gameId": created["id"], "version": 0, "seconds": 0.1})
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"]["code"], "jev_unavailable")
        failed = payload["error"]["details"]["game"]
        self.assertEqual(failed["moves"], [])
        self.assertEqual(failed["logs"][-1]["provider"], "jev")
        self.assertEqual(failed["logs"][-1]["kind"], "jev")
        self.assertEqual(len(self.state.laya.calls), 0)

    def test_duel_requires_distinct_laya_and_jev_and_rejects_extra_provider_on_human(self):
        status, payload = post(
            self.base,
            "/api/game",
            {"puzzle": PUZZLE["id"], "mode": "duel", "model": "laya", "opponentModel": "laya"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_opponent_model")
        status, payload = post(
            self.base,
            "/api/game",
            {"puzzle": PUZZLE["id"], "mode": "human", "model": "laya", "opponentModel": "jev"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_opponent_model")


class DuelPersistenceTests(unittest.TestCase):
    def test_persist_reload_and_undo_preserve_provider_assignment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "games.json"
            state = server.ServiceState(path)
            runtime = FakeRuntime()
            state.runtime = runtime
            state.puzzle_map = {PUZZLE["id"]: PUZZLE}
            state.laya = FakeProvider("laya")
            state.jev = FakeProvider("jev")
            game = state.create_game("duel-fixture", mode="duel", model="jev", opponent_model="laya")

            for provider in (state.jev, state.laya):
                decision = engine.choose_laya_move(runtime, provider, game.fen, seconds=0.1)
                engine.append_laya_move(game, decision, runtime)
            state.persist_games(game)

            restored_state = server.ServiceState(path)
            restored = restored_state.games[game.id]
            self.assertEqual(restored.mode, "duel")
            self.assertEqual(restored.model, "jev")
            self.assertEqual(restored.opponent_model, "laya")
            self.assertEqual([item["provider"] for item in restored.logs], ["jev", "laya"])
            restored.undo_round(runtime)
            self.assertEqual(restored.turn, "black")
            self.assertEqual(len(restored.moves), 1)
            self.assertEqual(restored.archive_logs[-1]["provider"], "laya")


if __name__ == "__main__":
    unittest.main()
