"""Jev client and model-selection API checks without a real API key."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

try:
    from . import app as snake_app
    from .engine import ModelUnavailableError
    from .jev_client import JevClient, TYPESAFE_API_KEY_ENV, TYPESAFE_API_URL
    from . import jev_client as jev_client_module
except ImportError:
    import app as snake_app
    from engine import ModelUnavailableError
    from jev_client import JevClient, TYPESAFE_API_KEY_ENV, TYPESAFE_API_URL
    import jev_client as jev_client_module


class FakeResponse:
    def __init__(self, payload: bytes, status: int = 200) -> None:
        self.payload = payload
        self.status_code = status
        self.headers: dict[str, str] = {}

    @property
    def content(self) -> bytes:
        return self.payload


class JevClientTests(unittest.TestCase):
    def test_health_only_checks_local_key_and_missing_key_never_calls_network(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory, patch.object(
            jev_client_module, "TYPESAFE_API_KEY_FILE", Path(temporary_directory) / "missing"
        ), patch.dict(os.environ, {}, clear=True), patch.object(httpx.Client, "post") as post:
            status, payload = JevClient().health()
            self.assertEqual(status, 503)
            self.assertEqual(payload["status"], "unconfigured")
            self.assertIn(TYPESAFE_API_KEY_ENV, payload["error"])
            with self.assertRaises(ModelUnavailableError):
                JevClient().decide({"state": "offline"}, {})
            post.assert_not_called()

    def test_file_key_is_used_only_when_environment_key_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            key_file = Path(temporary_directory) / "typesafe_api_key"
            key_file.write_text("file-secret\n", encoding="utf-8")
            with patch.object(jev_client_module, "TYPESAFE_API_KEY_FILE", key_file), patch.dict(
                os.environ, {}, clear=True
            ):
                self.assertTrue(JevClient.is_configured())
                self.assertEqual(JevClient().health()[0], 200)
            with patch.object(jev_client_module, "TYPESAFE_API_KEY_FILE", key_file), patch.dict(
                os.environ, {TYPESAFE_API_KEY_ENV: "env-secret"}, clear=True
            ):
                captured: list[tuple[str, dict[str, object]]] = []

                def fake_post(url: str, **kwargs: object) -> FakeResponse:
                    captured.append((url, kwargs))
                    return FakeResponse(b'{"answers": {}}')

                with patch.object(httpx.Client, "post", side_effect=fake_post):
                    JevClient().decide({}, {})
                self.assertEqual(captured[0][1]["headers"]["Authorization"], "Bearer env-secret")

    def test_client_reuses_httpx_connection_and_can_close(self) -> None:
        class FakeHTTPClient:
            def __init__(self) -> None:
                self.calls = 0
                self.closed = 0

            def post(self, _url: str, **_kwargs: object) -> FakeResponse:
                self.calls += 1
                return FakeResponse(b'{"answers": {}}')

            def close(self) -> None:
                self.closed += 1

        fake_client = FakeHTTPClient()
        with patch.dict(os.environ, {TYPESAFE_API_KEY_ENV: "local-test-secret"}, clear=True), patch.object(
            jev_client_module.httpx, "Client", return_value=fake_client
        ) as client_factory:
            client = JevClient()
            client.decide({}, {})
            client.decide({}, {})
            client.close()

        client_factory.assert_called_once_with(timeout=20.0)
        self.assertEqual(fake_client.calls, 2)
        self.assertEqual(fake_client.closed, 1)

    def test_request_uses_fixed_endpoint_same_structured_state_and_process_key(self) -> None:
        response_payload = {
            "answers": {"direction": {"type": "choice", "choice": "right"}},
            "usage": {"input_tokens": 12, "output_tokens": 1},
            "model": "jev-latest",
        }
        captured: list[tuple[str, dict[str, object]]] = []

        def fake_post(url: str, **kwargs: object) -> FakeResponse:
            captured.append((url, kwargs))
            return FakeResponse(json.dumps(response_payload).encode("utf-8"))

        key = "local-test-secret"
        state = {"seed": 7, "中文": "蛇"}
        questions = {"direction": {"type": "choice", "criteria": {"right": "向右"}}}
        with patch.dict(os.environ, {TYPESAFE_API_KEY_ENV: key}, clear=True), patch.object(
            httpx.Client, "post", side_effect=fake_post
        ):
            result = JevClient(timeout=3.0).decide(state, questions)

        self.assertEqual({k: v for k, v in result.items() if k != "_transport_attempts"}, response_payload)
        self.assertEqual(result["_transport_attempts"], 1)
        self.assertEqual(len(captured), 1)
        url, kwargs = captured[0]
        self.assertEqual(url, TYPESAFE_API_URL)
        self.assertEqual(kwargs["headers"]["Authorization"], f"Bearer {key}")
        body = json.loads(kwargs["content"].decode("utf-8"))
        self.assertEqual(body["model"], "jev-latest")
        self.assertEqual(body["state"], state)
        self.assertEqual(body["questions"], questions)

    def test_invalid_json_and_http_errors_are_provider_errors_without_secret(self) -> None:
        key = "local-test-secret"
        with patch.dict(os.environ, {TYPESAFE_API_KEY_ENV: key}, clear=True), patch.object(
            httpx.Client, "post", return_value=FakeResponse(b"not-json")
        ):
            with self.assertRaisesRegex(ModelUnavailableError, "不是有效 JSON"):
                JevClient().decide({}, {})

        error_response = FakeResponse(b"secret body", status=401)
        with patch.dict(os.environ, {TYPESAFE_API_KEY_ENV: key}, clear=True), patch.object(
            httpx.Client, "post", return_value=error_response
        ):
            with self.assertRaisesRegex(ModelUnavailableError, "Jev HTTP 401") as raised:
                JevClient().decide({}, {})
        self.assertNotIn(key, str(raised.exception))

    def test_transient_connection_error_retries_and_records_attempts(self) -> None:
        response = FakeResponse(b'{"model":"jev-test","answers":{}}')
        with patch.dict(os.environ, {TYPESAFE_API_KEY_ENV: "local-test-secret"}, clear=True), patch.object(
            httpx.Client, "post", side_effect=[httpx.TransportError("temporary"), response]
        ) as post, patch("jev_client.time.sleep") as sleep:
            result = JevClient().decide({}, {})
        self.assertEqual(result["_transport_attempts"], 2)
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(0.5)

    def test_transient_http_status_retries_and_does_not_retry_client_errors(self) -> None:
        response = FakeResponse(b'{"model":"jev-test","answers":{}}')
        busy = FakeResponse(b"busy", status=529)
        with patch.dict(os.environ, {TYPESAFE_API_KEY_ENV: "local-test-secret"}, clear=True), patch.object(
            httpx.Client, "post", side_effect=[busy, response]
        ) as post, patch("jev_client.time.sleep") as sleep:
            result = JevClient().decide({}, {})
        self.assertEqual(result["_transport_attempts"], 2)
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(0.5)

        with patch.dict(os.environ, {TYPESAFE_API_KEY_ENV: "local-test-secret"}, clear=True), patch.object(
            httpx.Client, "post", return_value=FakeResponse(b"unauthorized", status=401)
        ) as post, patch("jev_client.time.sleep") as sleep:
            with self.assertRaisesRegex(ModelUnavailableError, "Jev HTTP 401"):
                JevClient().decide({}, {})
        post.assert_called_once()
        sleep.assert_not_called()


class JevApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_root = snake_app.GAME_STORAGE_ROOT
        self.old_games = snake_app.games.copy()
        snake_app.games.clear()

    def tearDown(self) -> None:
        snake_app.games.clear()
        snake_app.games.update(self.old_games)
        snake_app.GAME_STORAGE_ROOT = self.old_root

    def test_missing_key_health_and_create_are_explicitly_unconfigured(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory, patch.dict(
            os.environ, {}, clear=True
        ), patch.object(
            jev_client_module, "TYPESAFE_API_KEY_FILE", Path(temporary_directory) / "missing-key"
        ), patch.object(snake_app.jev_client._client, "post") as post:
            snake_app.GAME_STORAGE_ROOT = Path(temporary_directory) / "games"
            with TestClient(snake_app.app) as client:
                health = client.get("/api/health?model=jev")
                created = client.post("/api/game", json={"model": "jev", "seed": 7})

            self.assertEqual(health.status_code, 503)
            self.assertEqual(health.json()["status"], "unconfigured")
            self.assertEqual(created.status_code, 503)
            self.assertEqual(created.json()["detail"]["code"], "jev_unavailable")
            self.assertEqual(snake_app.games, {})
            self.assertFalse(Path(temporary_directory, "games").exists())
            post.assert_not_called()

    def test_configured_create_persists_only_model_metadata(self) -> None:
        key = "local-test-secret"
        with tempfile.TemporaryDirectory() as temporary_directory, patch.dict(
            os.environ, {TYPESAFE_API_KEY_ENV: key}, clear=True
        ):
            snake_app.GAME_STORAGE_ROOT = Path(temporary_directory) / "games"
            with TestClient(snake_app.app) as client:
                created = client.post("/api/game", json={"model": "jev", "seed": 7})

            self.assertEqual(created.status_code, 200)
            payload = created.json()
            self.assertEqual(payload["state"]["model"], "jev")
            metadata_path = snake_app.GAME_STORAGE_ROOT / payload["id"] / "metadata.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["model"], "jev")
            self.assertNotIn(key, metadata_path.read_text(encoding="utf-8"))

    def test_jev_and_laya_share_initial_game_and_jev_step_preserves_raw_usage(self) -> None:
        key = "local-test-secret"
        calls = []

        def decide(state: dict, questions: dict) -> dict:
            calls.append((state, questions))
            choice = next(iter(questions["direction"]["criteria"]))
            return {"model": "jev-test", "answers": {"direction": {"type": "choice", "choice": choice}},
                    "usage": {"input_tokens": 999, "output_tokens": 2}}

        with tempfile.TemporaryDirectory() as temporary_directory, patch.dict(
            os.environ, {TYPESAFE_API_KEY_ENV: key}, clear=True
        ), patch.object(snake_app.jev_client, "decide", side_effect=decide):
            snake_app.GAME_STORAGE_ROOT = Path(temporary_directory) / "games"
            with TestClient(snake_app.app) as client:
                laya = client.post("/api/game", json={"model": "laya", "seed": 9}).json()
                jev = client.post("/api/game", json={"model": "jev", "seed": 9}).json()
                laya_state = {k: v for k, v in laya["state"].items() if k != "model"}
                jev_state = {k: v for k, v in jev["state"].items() if k != "model"}
                self.assertEqual(laya_state, jev_state)
                stepped = client.post(f"/api/game/{jev['id']}/step", json={"expectedStep": 0})
                self.assertEqual(stepped.status_code, 200)
                decision = stepped.json()["decision"]
                logs = client.get(f"/api/game/{jev['id']}/logs").json()["logs"]

            self.assertEqual(len(calls), 1)
            self.assertEqual(decision["model"], "jev")
            self.assertEqual(decision["executionSource"], "jev")
            self.assertEqual(decision["raw"]["usage"]["input_tokens"], 999)
            self.assertIsNone(decision["contextAudit"]["tokenMatch"])
            self.assertEqual(logs[0]["id"], decision["id"])
            self.assertNotIn(key, json.dumps(stepped.json()))
            self.assertNotIn(key, json.dumps(logs))


if __name__ == "__main__":
    unittest.main()
