"""Kev client and model-selection API checks without a running app server."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

try:
    from . import app as snake_app
    from . import engine as snake_engine
    from . import kev_client as kev_client_module
    from .kev_client import KevClient
except ImportError:
    import app as snake_app
    import engine as snake_engine
    import kev_client as kev_client_module
    from kev_client import KevClient


class FakeResponse:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self.status = status
        self.payload = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self.payload

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class KevClientTests(unittest.TestCase):
    def test_health_requires_the_loaded_kev_checkpoint(self) -> None:
        models = {
            "models": [
                {
                    "name": "kev-latest",
                    "run": "jaredpalmer/kev-0.8b@54f4f8777356cd5bbbb6c6919c657f26e6f2f6d8",
                    "device": "mps",
                    "backend": "mlx",
                }
            ]
        }
        with patch.object(
            kev_client_module.urllib.request,
            "urlopen",
            return_value=FakeResponse(models),
        ) as urlopen:
            status, payload = KevClient("http://127.0.0.1:8009").health()

        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["modelName"], "kev-latest")
        self.assertTrue(payload["run"].startswith("jaredpalmer/kev-0.8b@"))
        urlopen.assert_called_once()
        self.assertEqual(urlopen.call_args.args[0].full_url, "http://127.0.0.1:8009/v1/models")

        wrong = {"models": [{"name": "jev-latest", "run": "hosted/jev@latest"}]}
        with patch.object(
            kev_client_module.urllib.request,
            "urlopen",
            return_value=FakeResponse(wrong),
        ):
            status, payload = KevClient().health()
        self.assertEqual(status, 503)
        self.assertEqual(payload["status"], "wrong_model")

    def test_decide_uses_local_kev_protocol_without_a_key(self) -> None:
        response = {
            "model": "kev-latest",
            "answers": {"direction": {"type": "choice", "choice": "A", "confidence": 0.8}},
            "usage": {"input_tokens": 777, "output_tokens": 5},
        }
        captured: list[object] = []

        def fake_urlopen(request: object, **_kwargs: object) -> FakeResponse:
            captured.append(request)
            return FakeResponse(response)

        state = {"context": "board"}
        questions = {"direction": {"type": "choice", "criteria": {"A": "right"}}}
        with patch.object(kev_client_module.urllib.request, "urlopen", side_effect=fake_urlopen):
            result = KevClient("http://127.0.0.1:8009").decide(state, questions)

        self.assertEqual(result, response)
        request = captured[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:8009/v1/systemone")
        self.assertEqual(request.method, "POST")
        self.assertIsNone(request.get_header("Authorization"))
        self.assertEqual(
            json.loads(request.data.decode("utf-8")),
            {"model": "kev-latest", "state": state, "questions": questions},
        )


class KevApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_root = snake_app.GAME_STORAGE_ROOT
        self.old_games = snake_app.games.copy()
        snake_app.games.clear()

    def tearDown(self) -> None:
        snake_app.games.clear()
        snake_app.games.update(self.old_games)
        snake_app.GAME_STORAGE_ROOT = self.old_root

    def test_kev_usage_does_not_require_laya_tokenizer_count(self) -> None:
        def decide(_state: dict, questions: dict) -> dict:
            choice = next(iter(questions["direction"]["criteria"]))
            return {
                "model": "kev-latest",
                "answers": {"direction": {"type": "choice", "choice": choice}},
                "usage": {"input_tokens": 777},
            }

        with patch.object(snake_engine, "_estimate_context_tokens", return_value=None):
            result = snake_engine.SnakeGame(
                seed=9,
                model_provider="kev",
                skip_deterministic_model=False,
            ).step(decide)

        decision = result["decision"]
        self.assertEqual(decision["modelCallStatus"], "succeeded")
        self.assertIsNone(decision["contextAudit"]["inputTokens"])
        self.assertIsNone(decision["contextAudit"]["tokenDelta"])
        self.assertIsNone(decision["contextAudit"]["tokenMatch"])

    def test_create_and_step_keep_real_kev_provenance(self) -> None:
        health = {
            "status": "ready",
            "model": "kev",
            "modelName": "kev-latest",
            "run": "jaredpalmer/kev-0.8b@test",
        }

        def decide(_state: dict, questions: dict) -> dict:
            choice = next(iter(questions["direction"]["criteria"]))
            return {
                "model": "kev-latest",
                "answers": {"direction": {"type": "choice", "choice": choice, "confidence": 0.6}},
                "usage": {"input_tokens": 777, "output_tokens": 4},
            }

        with tempfile.TemporaryDirectory() as temporary_directory, patch.object(
            snake_app.kev_client, "health", return_value=(200, health)
        ), patch.object(snake_app.kev_client, "decide", side_effect=decide):
            snake_app.GAME_STORAGE_ROOT = Path(temporary_directory) / "games"
            with TestClient(snake_app.app) as client:
                created = client.post("/api/game", json={"model": "kev", "seed": 9})
                self.assertEqual(created.status_code, 200)
                game = created.json()
                self.assertEqual(game["state"]["model"], "kev")
                stepped = client.post(f"/api/game/{game['id']}/step", json={"expectedStep": 0})
                self.assertEqual(stepped.status_code, 200)
                decision = stepped.json()["decision"]
                logs = client.get(f"/api/game/{game['id']}/logs").json()["logs"]

        self.assertEqual(decision["model"], "kev")
        self.assertEqual(decision["modelCallStatus"], "succeeded")
        self.assertEqual(decision["executionSource"], "kev")
        self.assertEqual(decision["raw"]["model"], "kev-latest")
        self.assertIsNone(decision["contextAudit"]["tokenMatch"])
        self.assertEqual(decision["contextAudit"]["rawInputTokens"], 777)
        self.assertEqual(logs[0]["model"], "kev")


if __name__ == "__main__":
    unittest.main()
