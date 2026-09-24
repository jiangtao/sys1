"""Kev provider contract and model identity checks."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import engine
import kev_client


FEN = "4k3r/9/9/9/9/9/9/9/9/R3K4 w - - 0 1"


class FakeResponse:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self.status = status
        self.content = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.content


class KevClientTests(unittest.TestCase):
    def test_health_requires_the_expected_loaded_run(self):
        card = {"name": "kev-latest", "run": "jaredpalmer/kev-0.8b@54f4f877", "device": "mps"}
        client = kev_client.KevClient()
        with patch.object(kev_client.urllib.request, "urlopen", return_value=FakeResponse({"models": [card]})):
            status, payload = client.health()
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["run"], card["run"])

        card["run"] = "jaredpalmer/kev-4b@wrong"
        with patch.object(kev_client.urllib.request, "urlopen", return_value=FakeResponse({"models": [card]})):
            status, payload = client.health()
        self.assertEqual(status, 503)
        self.assertEqual(payload["status"], "wrong_model")

    def test_decide_uses_kev_latest_without_authorization_header(self):
        captured = []
        response = FakeResponse({"model": "kev-latest", "answers": {"move": {"choice": "a0a1"}}})

        def fake_urlopen(request, timeout):
            captured.append((request, timeout))
            return response

        candidates = [{"move": "a0a1", "label": "车 a0→a1", "score": {"cp": 10}, "depth": 3, "pv": ["a0a1"]}]
        with patch.object(kev_client.urllib.request, "urlopen", side_effect=fake_urlopen):
            result = kev_client.KevClient().decide(FEN, ["a0a1"], candidates)
        self.assertEqual(result["answers"]["move"]["choice"], "a0a1")
        request, _ = captured[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["model"], "kev-latest")
        self.assertFalse(request.has_header("Authorization"))
        self.assertEqual(request.full_url, kev_client.KEV_SYSTEMONE_URL)

    def test_engine_keeps_kev_in_model_kind_and_raw_log_fields(self):
        class Runtime:
            def legal_moves(self, fen):
                return (["a0a1"] if " w " in fen else []), []

            def in_check(self, _fen):
                return False, []

            def analyze(self, _fen, **_kwargs):
                return {"bestmove": "a0a1", "candidates": [{"move": "a0a1", "score": {"cp": 1}, "depth": 1, "pv": ["a0a1"]}], "raw": []}

        class Provider:
            provider_name = "kev"
            provider_label = "Kev"

            def decide(self, _fen, _legal, candidates):
                return {"model": "kev-latest", "answers": {"move": {"choice": candidates[0]["move"]}}}

        runtime = Runtime()
        game = engine.make_game({"id": "fixture", "fen": FEN}, runtime, mode="selfplay", model="kev")
        decision = engine.choose_laya_move(runtime, Provider(), game.fen, seconds=0.1)
        engine.append_laya_move(game, decision, runtime)
        self.assertEqual(game.moves[-1]["kind"], "kev")
        self.assertEqual(game.logs[-1]["provider"], "kev")
        self.assertIn("kev", game.logs[-1]["raw"])


if __name__ == "__main__":
    unittest.main()
