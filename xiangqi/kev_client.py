"""Client for the local Kev-0.8B TypeSafe System One service."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Mapping

try:
    from .engine import LayaUnavailable, _compact_error, _read_json_bytes, build_laya_request
except ImportError:  # Running the service directly from this directory.
    from engine import LayaUnavailable, _compact_error, _read_json_bytes, build_laya_request


KEV_BASE_URL = "http://127.0.0.1:8009"
KEV_SYSTEMONE_URL = f"{KEV_BASE_URL}/v1/systemone"
KEV_MODELS_URL = f"{KEV_BASE_URL}/v1/models"
KEV_MODEL_NAME = "kev-latest"
KEV_RUN_PREFIX = "jaredpalmer/kev-0.8b@"
DEFAULT_TIMEOUT_SECONDS = 35.0


class KevUnavailable(LayaUnavailable):
    """Kev is not loaded or could not complete a decision."""


def _status(response: Any) -> int:
    value = getattr(response, "status", None)
    if value is None:
        value = response.getcode()
    return int(value)


class KevClient:
    """Call the open local Kev endpoint without sending an API key."""

    endpoint = KEV_SYSTEMONE_URL
    models_endpoint = KEV_MODELS_URL
    provider_name = "kev"
    provider_label = "Kev"

    def __init__(self, base_url: str | None = None, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        base_url = (base_url or os.environ.get("XIANGQI_KEV_URL") or KEV_BASE_URL).rstrip("/")
        self.base_url = base_url
        self.endpoint = f"{base_url}/v1/systemone"
        self.models_endpoint = f"{base_url}/v1/models"
        self.timeout = timeout

    def close(self) -> None:
        """Keep the provider interface symmetric with the reusable HTTP clients."""

    @staticmethod
    def _decode(body: bytes) -> dict[str, Any]:
        try:
            payload = _read_json_bytes(body)
        except LayaUnavailable as error:
            raise KevUnavailable(str(error).replace("Laya", "Kev")) from error
        if not isinstance(payload.get("answers"), Mapping):
            raise KevUnavailable("Kev 返回的 JSON 缺少 answers 对象")
        return payload

    def health(self) -> tuple[int, dict[str, Any]]:
        """Probe the local model card and verify the expected Kev checkpoint is loaded."""

        request = urllib.request.Request(self.models_endpoint, headers={"Accept": "application/json"}, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=min(self.timeout, 3.0)) as response:
                status = _status(response)
                body = response.read()
        except urllib.error.HTTPError as error:
            try:
                payload = _read_json_bytes(error.read())
                detail = payload.get("detail") or payload.get("error") or f"HTTP {error.code}"
            except LayaUnavailable:
                detail = _compact_error(error)
            return error.code, {"status": "error", "model": KEV_MODEL_NAME, "error": str(detail)}
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            return 503, {"status": "unreachable", "model": KEV_MODEL_NAME, "error": f"无法连接 Kev：{_compact_error(error)}"}
        if status < 200 or status >= 300:
            return status, {"status": "error", "model": KEV_MODEL_NAME, "error": f"Kev HTTP {status}"}
        try:
            payload = _read_json_bytes(body)
        except LayaUnavailable as error:
            return 503, {"status": "invalid_response", "model": KEV_MODEL_NAME, "error": str(error)}
        cards = payload.get("models")
        if not isinstance(cards, list):
            return 503, {"status": "invalid_response", "model": KEV_MODEL_NAME, "error": "Kev /v1/models 缺少 models 数组"}
        card = next(
            (item for item in cards if isinstance(item, Mapping) and item.get("name") == KEV_MODEL_NAME),
            None,
        )
        if card is None:
            return 503, {"status": "wrong_model", "model": KEV_MODEL_NAME, "error": "Kev /v1/models 未返回 kev-latest"}
        run = card.get("run")
        if not isinstance(run, str) or not run.startswith(KEV_RUN_PREFIX):
            return 503, {
                "status": "wrong_model",
                "model": KEV_MODEL_NAME,
                "run": run,
                "error": f"Kev 已加载的模型不是 {KEV_RUN_PREFIX}…",
            }
        return 200, {
            "status": "ready",
            "model": KEV_MODEL_NAME,
            "run": run,
            **{key: card[key] for key in ("device", "backend", "dtype") if key in card},
        }

    def decide(
        self,
        fen_or_state: str | dict[str, Any],
        legal_moves_or_questions: list[str] | dict[str, dict[str, Any]],
        candidates: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Submit the shared Xiangqi state and return Kev's response."""

        if candidates is None:
            if not isinstance(fen_or_state, dict) or not isinstance(legal_moves_or_questions, dict):
                raise TypeError("Kev.decide 需要 (fen, legal_moves, candidates) 或 (state, questions)")
            state = fen_or_state
            questions = legal_moves_or_questions
        else:
            if not isinstance(fen_or_state, str) or not isinstance(legal_moves_or_questions, list):
                raise TypeError("Kev.decide 需要 (fen, legal_moves, candidates)")
            built = build_laya_request(fen_or_state, legal_moves_or_questions, candidates)
            state = built["state"]
            questions = built["questions"]

        body = json.dumps(
            {"state": state, "questions": questions, "model": KEV_MODEL_NAME},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status = _status(response)
                payload = self._decode(response.read())
        except urllib.error.HTTPError as error:
            try:
                detail = json.dumps(_read_json_bytes(error.read()), ensure_ascii=False)[:360]
            except LayaUnavailable:
                detail = _compact_error(error)
            raise KevUnavailable(f"Kev HTTP {error.code}：{detail}") from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise KevUnavailable(f"无法连接 Kev：{_compact_error(error)}") from error
        if status < 200 or status >= 300:
            raise KevUnavailable(f"Kev HTTP {status}")
        return payload


__all__ = [
    "KEV_BASE_URL",
    "KEV_MODEL_NAME",
    "KEV_MODELS_URL",
    "KEV_RUN_PREFIX",
    "KEV_SYSTEMONE_URL",
    "KevClient",
    "KevUnavailable",
]
