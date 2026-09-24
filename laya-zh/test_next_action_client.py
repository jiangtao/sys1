import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import next_action_client as client


class FakeResponse:
    def __init__(self, payload, status=200):
        self.status = status
        self._payload = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, _limit=-1):
        return self._payload

    def getcode(self):
        return self.status


def choice_response(question, choice, probabilities, request_id="req-1", model="test-model"):
    return {
        "status": "ok",
        "requestId": request_id,
        "model": model,
        "answers": {
            question: {
                "type": "choice",
                "choice": choice,
                "probabilities": probabilities,
            }
        },
    }


class NextActionClientTest(unittest.TestCase):
    def state(self, actions=None, surfaces=None):
        return {
            "goal": "private goal marker",
            "last_actor": "main_control",
            "last_status": "none",
            "last_summary": "private summary marker",
            "allowed_actions": actions or list(client.ACTIONS),
            "allowed_surfaces": surfaces or list(client.SURFACES),
        }

    def responses(self, *payloads):
        return [FakeResponse(payload) for payload in payloads]

    def test_action_only(self):
        payload = choice_response(
            "next_action",
            "research",
            {"main_control": 0.1, "research": 0.9},
        )
        with patch.object(client, "_urlopen_no_proxy", side_effect=self.responses(payload)) as request:
            result = client.suggest_next_action(self.state(["main_control", "research"]))
        self.assertEqual(result["status"], "observed")
        self.assertEqual(result["suggested_action"], "research")
        self.assertIsNone(result["surface"])
        self.assertEqual(result["candidate_roles"], ["evidence_researcher"])
        self.assertEqual(request.call_count, 1)

    def test_implement_frontend(self):
        action = choice_response(
            "next_action",
            "implement",
            {option: 0.0 for option in client.ACTIONS} | {"implement": 1.0},
            request_id="action-1",
        )
        surface = choice_response(
            "surface",
            "frontend",
            {"frontend": 0.8, "backend": 0.1, "both": 0.1},
            request_id="surface-1",
        )
        with patch.object(client, "_urlopen_no_proxy", side_effect=self.responses(action, surface)) as request:
            result = client.suggest_next_action(self.state())
        self.assertEqual(result["status"], "observed")
        self.assertEqual(result["suggested_action"], "implement")
        self.assertEqual(result["surface"], "frontend")
        self.assertEqual(result["candidate_roles"], ["implementation_worker"])
        self.assertEqual(result["service"]["surfaceRequestId"], "surface-1")
        self.assertEqual(request.call_count, 2)
        first_body = json.loads(request.call_args_list[0].args[0].data)
        second_body = json.loads(request.call_args_list[1].args[0].data)
        self.assertNotIn("next_action", first_body["state"])
        self.assertEqual(second_body["state"]["next_action"], "implement")

    def test_validate_both_maps_to_two_validation_candidates(self):
        action = choice_response(
            "next_action",
            "validate",
            {option: (1.0 if option == "validate" else 0.0) for option in client.ACTIONS},
        )
        surface = choice_response(
            "surface",
            "both",
            {"frontend": 0.1, "backend": 0.1, "both": 0.8},
        )
        with patch.object(client, "_urlopen_no_proxy", side_effect=self.responses(action, surface)):
            result = client.suggest_next_action(self.state())
        self.assertEqual(result["candidate_roles"], ["e2e_tester", "backend_tester"])

    def test_only_main_control_skips_without_calling_service(self):
        with patch.object(client, "_urlopen_no_proxy") as request:
            result = client.suggest_next_action(self.state(["main_control"]))
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["skip_reason"], "no_routing_needed")
        request.assert_not_called()

    def test_connection_refusal_is_skipped(self):
        with patch.object(client, "_urlopen_no_proxy", side_effect=ConnectionRefusedError()):
            result = client.suggest_next_action(self.state(["main_control", "research"]))
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["skip_reason"], "service_unavailable")

    def test_timeout_is_skipped(self):
        with patch.object(client, "_urlopen_no_proxy", side_effect=TimeoutError()):
            result = client.suggest_next_action(self.state(["main_control", "research"]))
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["skip_reason"], "service_timeout")

    def test_http_503_is_skipped(self):
        error = HTTPError("http://127.0.0.1:8765/v1/decide", 503, "unavailable", {}, None)
        with patch.object(client, "_urlopen_no_proxy", side_effect=error):
            result = client.suggest_next_action(self.state(["main_control", "research"]))
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["skip_reason"], "service_http_error")

    def test_malformed_response_is_skipped(self):
        with patch.object(client, "_urlopen_no_proxy", side_effect=[FakeResponse(b"not-json")]):
            result = client.suggest_next_action(self.state(["main_control", "research"]))
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["skip_reason"], "malformed_response")

    def test_illegal_candidate_is_skipped(self):
        payload = choice_response("next_action", "implement", {"main_control": 0.5, "research": 0.5})
        with patch.object(client, "_urlopen_no_proxy", side_effect=[FakeResponse(payload)]):
            result = client.suggest_next_action(self.state(["main_control", "research"]))
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["skip_reason"], "invalid_candidate")

    def test_second_layer_failure_is_skipped(self):
        action = choice_response(
            "next_action",
            "implement",
            {option: (1.0 if option == "implement" else 0.0) for option in client.ACTIONS},
        )
        with patch.object(client, "_urlopen_no_proxy", side_effect=[FakeResponse(action), FakeResponse(b"bad")]):
            result = client.suggest_next_action(self.state())
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["skip_reason"], "second_step_failed")

    def test_remote_url_is_rejected_without_request(self):
        with patch.object(client, "_urlopen_no_proxy") as request:
            result = client.suggest_next_action(self.state(["main_control", "research"]), base_url="http://example.com")
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["skip_reason"], "invalid_base_url")
        request.assert_not_called()

    def test_unknown_state_field_is_rejected_before_request(self):
        state = self.state(["main_control", "research"])
        state["private_context"] = "must not be forwarded"
        with patch.object(client, "_urlopen_no_proxy") as request:
            result = client.suggest_next_action(state)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["skip_reason"], "invalid_input")
        request.assert_not_called()

    def test_oversized_state_is_rejected_before_request(self):
        state = self.state(["main_control", "research"])
        state["last_summary"] = "x" * client.MAX_STATE_BYTES
        with patch.object(client, "_urlopen_no_proxy") as request:
            result = client.suggest_next_action(state)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["skip_reason"], "invalid_input")
        request.assert_not_called()

    def test_cli_service_skip_exits_zero(self):
        stdin = io.StringIO(json.dumps(self.state(["main_control", "research"])))
        stdout = io.StringIO()
        with (
            patch.object(client, "_urlopen_no_proxy", side_effect=URLError("offline")),
            patch.object(client.sys, "stdin", stdin),
            patch.object(client.sys, "stdout", stdout),
        ):
            code = client.main([])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["status"], "skipped")

    def test_cli_invalid_input_exits_zero(self):
        stdin = io.StringIO("not-json")
        stdout = io.StringIO()
        with patch.object(client.sys, "stdin", stdin), patch.object(client.sys, "stdout", stdout):
            code = client.main([])
        result = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["skip_reason"], "invalid_input")

    def test_cli_invalid_timeout_exits_zero(self):
        stdin = io.StringIO(json.dumps(self.state(["main_control", "research"])))
        stdout = io.StringIO()
        with patch.object(client.sys, "stdin", stdin), patch.object(client.sys, "stdout", stdout):
            code = client.main(["--timeout", "not-a-number"])
        result = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["skip_reason"], "invalid_input")

    def test_log_does_not_contain_state_text(self):
        state = self.state(["main_control", "research"])
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "shadow.jsonl"
            stdin = io.StringIO(json.dumps(state))
            stdout = io.StringIO()
            with (
                patch.object(client, "_urlopen_no_proxy", side_effect=URLError("offline")),
                patch.object(client.sys, "stdin", stdin),
                patch.object(client.sys, "stdout", stdout),
            ):
                self.assertEqual(client.main(["--log", str(log_path), "--actual-action", "main_control"]), 0)
            logged = log_path.read_text(encoding="utf-8")
        self.assertNotIn("private goal marker", logged)
        self.assertNotIn("private summary marker", logged)
        record = json.loads(logged)
        self.assertEqual(record["actual_action"], "main_control")
        self.assertIn("state_hash", record)


if __name__ == "__main__":
    unittest.main()
