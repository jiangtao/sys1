import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server


class DiagnosticsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = server.DecideRequest(
            state="private-state-marker",
            questions={
                "route": {
                    "type": "choice",
                    "criteria": {"a": "A", "b": "B"},
                },
                "urgency": {"type": "score", "criteria": ["low", "high"]},
            },
        )

    def test_runtime_error_keeps_cause_and_diagnostic_metadata(self) -> None:
        cause = RuntimeError("selected index k out of range")
        with (
            patch.object(server.runtime, "status", "ready"),
            patch.object(server.runtime, "actual_device", "mps"),
            patch.object(server.runtime, "predict", side_effect=cause),
            patch.object(server.LOGGER, "exception") as log_exception,
        ):
            with self.assertRaises(server.HTTPException) as raised:
                server.decide(self.payload)

        error = raised.exception
        self.assertEqual(error.status_code, 503)
        self.assertIs(error.__cause__, cause)
        body = error.detail["error"]
        self.assertEqual(body["code"], "inference_unavailable")
        details = body["details"]
        request_id = details["request_id"]
        self.assertRegex(request_id, r"^[0-9a-f]{12}$")
        self.assertIn(request_id, body["message"][:200])
        self.assertIn("RuntimeError", body["message"])
        self.assertIn("selected index k out of range", details["reason"])
        self.assertEqual(details["exception_type"], "RuntimeError")
        self.assertEqual(details["input_metadata"]["state_chars"], len("private-state-marker"))
        self.assertEqual(details["input_metadata"]["question_count"], 2)
        self.assertEqual(details["input_metadata"]["choice_candidate_counts"], {"route": 2})
        self.assertNotIn("private-state-marker", json.dumps(details, ensure_ascii=False))
        log_exception.assert_called_once()
        self.assertIn(request_id, log_exception.call_args.args)
        self.assertIs(log_exception.call_args.kwargs["exc_info"][1], cause)

    def test_success_preserves_result_and_adds_request_id(self) -> None:
        result = {"model": "laya", "answers": {"route": "a"}, "usage": {"tokens": 4}}
        with (
            patch.object(server.runtime, "status", "ready"),
            patch.object(server.runtime, "actual_device", "mps"),
            patch.object(server.runtime, "predict", return_value=result),
        ):
            response = server.decide(self.payload)

        self.assertEqual(response["model"], "laya")
        self.assertEqual(response["answers"], {"route": "a"})
        self.assertEqual(response["usage"], {"tokens": 4})
        self.assertEqual(response["status"], "ok")
        self.assertEqual(response["device"], "mps")
        self.assertRegex(response["requestId"], r"^[0-9a-f]{12}$")

    def test_non_runtime_error_is_logged_and_returns_500_diagnostics(self) -> None:
        cause = LookupError("model output shape is invalid")
        with (
            patch.object(server.runtime, "status", "ready"),
            patch.object(server.runtime, "actual_device", "cpu"),
            patch.object(server.runtime, "predict", side_effect=cause),
            patch.object(server.LOGGER, "exception") as log_exception,
        ):
            with self.assertRaises(server.HTTPException) as raised:
                server.decide(self.payload)

        error = raised.exception
        self.assertEqual(error.status_code, 500)
        self.assertEqual(error.detail["error"]["code"], "inference_failed")
        self.assertEqual(error.detail["error"]["details"]["exception_type"], "LookupError")
        log_exception.assert_called_once()

    def test_invalid_questions_422_has_same_diagnostics_shape(self) -> None:
        cause = ValueError("criteria must not be empty")
        with (
            patch.object(server.runtime, "status", "ready"),
            patch.object(server.runtime, "predict", side_effect=cause),
            patch.object(server.LOGGER, "exception"),
        ):
            with self.assertRaises(server.HTTPException) as raised:
                server.decide(self.payload)

        error = raised.exception
        self.assertEqual(error.status_code, 422)
        self.assertEqual(error.detail["error"]["code"], "invalid_questions")
        details = error.detail["error"]["details"]
        self.assertIn("request_id", details)
        self.assertIn("input_metadata", details)
        self.assertEqual(details["exception_type"], "ValueError")

    def test_validation_handler_does_not_echo_state(self) -> None:
        exc = server.RequestValidationError(
            [
                {
                    "type": "missing",
                    "loc": ("body", "questions"),
                    "msg": "Field required",
                    "input": None,
                }
            ]
        )
        exc.body = {"state": "validation-private-state", "questions": "wrong"}
        with patch.object(server.LOGGER, "exception"):
            response = asyncio.run(server.validation_error_handler(None, exc))

        body = json.loads(response.body)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(body["error"]["code"], "invalid_request")
        details = body["error"]["details"]
        self.assertIn("request_id", details)
        self.assertIn("input_metadata", details)
        self.assertIn("errors", details)
        self.assertNotIn("validation-private-state", json.dumps(body, ensure_ascii=False))

    def test_inference_log_handler_is_bounded_and_not_duplicated(self) -> None:
        original_path = server.INFERENCE_LOG_PATH
        original_handlers = list(server.LOGGER.handlers)
        original_level = server.LOGGER.level
        with tempfile.TemporaryDirectory() as directory:
            added_path = Path(directory) / "runtime" / "inference-errors.log"
            server.INFERENCE_LOG_PATH = added_path
            try:
                server.configure_inference_logging()
                server.configure_inference_logging()
                matching = [
                    handler
                    for handler in server.LOGGER.handlers
                    if getattr(handler, "baseFilename", None) == str(added_path.resolve())
                ]
                self.assertEqual(len(matching), 1)
                self.assertEqual(matching[0].maxBytes, 3 * 1024 * 1024)
                self.assertEqual(matching[0].backupCount, 2)
                self.assertIn("%(asctime)s", matching[0].formatter._fmt)
            finally:
                for handler in list(server.LOGGER.handlers):
                    if handler not in original_handlers:
                        server.LOGGER.removeHandler(handler)
                        handler.close()
                server.INFERENCE_LOG_PATH = original_path
                server.LOGGER.setLevel(original_level)


if __name__ == "__main__":
    unittest.main()
