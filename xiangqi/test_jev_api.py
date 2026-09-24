"""Jev provider routing and shared configuration checks."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen

import engine
import jev_client
import server


FEN = "4k3r/9/9/9/9/9/9/9/9/R3K4 w - - 0 1"
PUZZLE = {
    "id": "jev-fixture",
    "title": "fixture",
    "subtitle": "fixture",
    "difficulty": "fixture",
    "fen": FEN,
}


class FakeHttpResponse:
    status_code = 200
    headers: dict[str, str] = {}
    content = b'{"model":"jev-latest","answers":{"move":{"choice":"a0a1"}}}'


class FakeHttpClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.closed = False

    def post(self, url: str, **kwargs: object) -> FakeHttpResponse:
        self.calls.append((url, kwargs))
        return FakeHttpResponse()

    def close(self) -> None:
        self.closed = True


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
    def __init__(self, name: str = "jev", fail: bool = False, ready: bool = True) -> None:
        self.provider_name = name
        self.provider_label = "Jev" if name == "jev" else "Laya"
        self.fail = fail
        self.ready = ready
        self.calls = []

    def health(self):
        if self.ready:
            return 200, {"status": "ready", "model": self.provider_name}
        return 503, {"status": "unconfigured", "model": self.provider_name, "error": "未配置"}

    def decide(self, fen, legal_moves, candidates):
        self.calls.append((fen, legal_moves, candidates))
        if self.fail:
            raise jev_client.JevUnavailable("模型暂时无法推理")
        return {"model": self.provider_name, "answers": {"move": {"choice": candidates[0]["move"]}}}

    def close(self):
        return None


def _post(base: str, path: str, payload: dict):
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


class JevClientTests(unittest.TestCase):
    def test_shared_payload_endpoint_auth_and_close_do_not_leak_key(self):
        fake_http = FakeHttpClient()
        key = "local-test-secret"
        candidates = [{"move": "a0a1", "label": "车 a0→a1", "score": {"cp": 10}, "depth": 3, "pv": ["a0a1"]}]
        client = jev_client.JevClient(timeout=7)
        client._client = fake_http
        with patch.dict(os.environ, {jev_client.TYPESAFE_API_KEY_ENV: key}, clear=True):
            result = client.decide(FEN, ["a0a1"], candidates)

        self.assertEqual(result["answers"]["move"]["choice"], "a0a1")
        self.assertEqual(len(fake_http.calls), 1)
        url, kwargs = fake_http.calls[0]
        self.assertEqual(url, jev_client.TYPESAFE_API_URL)
        self.assertEqual(kwargs["headers"]["Authorization"], f"Bearer {key}")
        body = json.loads(kwargs["content"].decode("utf-8"))
        self.assertEqual(body["model"], "jev-latest")
        self.assertEqual(body["state"], engine.build_laya_request(FEN, ["a0a1"], candidates)["state"])
        self.assertEqual(body["questions"], engine.build_laya_request(FEN, ["a0a1"], candidates)["questions"])
        self.assertNotIn(key, kwargs["content"].decode("utf-8"))
        client.close()
        self.assertTrue(fake_http.closed)

    def test_health_explicitly_reports_missing_dependency_and_missing_key(self):
        with patch.object(jev_client, "httpx", None):
            status, payload = jev_client.JevClient().health()
        self.assertEqual(status, 503)
        self.assertEqual(payload["status"], "dependency_missing")

        client = jev_client.JevClient()
        client._client = FakeHttpClient()
        with tempfile.TemporaryDirectory() as directory, patch.object(
            jev_client, "TYPESAFE_API_KEY_FILE", Path(directory) / "missing"
        ), patch.dict(os.environ, {}, clear=True):
            status, payload = client.health()
        self.assertEqual(status, 503)
        self.assertEqual(payload["status"], "unconfigured")


class JevApiTests(unittest.TestCase):
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

    def test_legacy_snapshot_defaults_to_laya(self):
        snapshot = {
            "id": "legacy",
            "puzzle": PUZZLE,
            "fen": FEN,
            "turn": "red",
            "legalMoves": ["a0a1"],
            "moves": [],
            "logs": [],
            "archiveLogs": [],
            "status": "playing",
            "version": 0,
        }
        restored = engine.load_game(snapshot)
        self.assertEqual(restored.model, "laya")
        self.assertEqual(restored.to_dict()["model"], "laya")

    def test_health_exposes_laya_and_jev_provider_statuses(self):
        status, payload = self.state.health()
        self.assertEqual(status, 200)
        self.assertEqual(payload["laya"]["status"], "ready")
        self.assertEqual(payload["jev"]["status"], "ready")
        self.assertEqual(payload["layaHttpStatus"], 200)
        self.assertEqual(payload["jevHttpStatus"], 200)

    def test_selfplay_jev_route_and_human_jev_route(self):
        status, created = _post(self.base, "/api/game", {"puzzle": PUZZLE["id"], "mode": "selfplay", "model": "jev"})
        self.assertEqual(status, 200)
        self.assertEqual(created["model"], "jev")
        status, stepped = _post(self.base, "/api/think", {"gameId": created["id"], "version": 0, "seconds": 0.1})
        self.assertEqual(status, 200)
        self.assertEqual(stepped["logs"][-1]["provider"], "jev")
        self.assertEqual(stepped["logs"][-1]["kind"], "jev")
        self.assertEqual(stepped["logs"][-1]["agent"], "Jev · 红方")
        self.assertEqual(stepped["logs"][-1]["modelChoice"], stepped["logs"][-1]["finalMove"])
        self.assertIn("jev", stepped["logs"][-1]["raw"])
        self.assertNotIn("laya", stepped["logs"][-1]["raw"])
        self.assertEqual(self.state.jev.calls[-1][1], ["a0a1"])

        status, human = _post(self.base, "/api/game", {"puzzle": PUZZLE["id"], "model": "jev"})
        self.assertEqual(status, 200)
        status, moved = _post(self.base, "/api/move", {"gameId": human["id"], "move": "a0a1", "version": 0})
        self.assertEqual(status, 200)
        status, replied = _post(self.base, "/api/think", {"gameId": human["id"], "version": 1, "seconds": 0.1})
        self.assertEqual(status, 200, replied)
        self.assertEqual(replied["logs"][-1]["provider"], "jev")
        self.assertEqual(replied["logs"][-1]["side"], "black")

    def test_missing_jev_rejects_create_and_provider_failure_rolls_back(self):
        self.state.jev = FakeProvider("jev", ready=False)
        status, payload = _post(self.base, "/api/game", {"puzzle": PUZZLE["id"], "model": "jev"})
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"]["code"], "jev_unavailable")
        self.assertEqual(self.state.games, {})

        self.state.jev = FakeProvider("jev", fail=True)
        status, created = _post(self.base, "/api/game", {"puzzle": PUZZLE["id"], "mode": "selfplay", "model": "jev"})
        self.assertEqual(status, 200)
        status, payload = _post(self.base, "/api/think", {"gameId": created["id"], "version": 0, "seconds": 0.1})
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"]["code"], "jev_unavailable")
        failed_game = payload["error"]["details"]["game"]
        self.assertEqual(failed_game["moves"], [])
        self.assertEqual(failed_game["logs"][-1]["provider"], "jev")
        self.assertEqual(failed_game["logs"][-1]["kind"], "jev")
        self.assertEqual(failed_game["logs"][-1]["label"], "Jev 决策失败")


if __name__ == "__main__":
    unittest.main()
