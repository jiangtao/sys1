"""Optional TypeSafe Jev client for the local Xiangqi service.

The key and endpoint deliberately match ``snake``.  The
dependency is optional so the normal Laya/Pikafish service can still start on
the system Python used by the local app.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Mapping

try:  # httpx is intentionally optional for the Laya-only runtime.
    import httpx  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - exercised through the health path.
    httpx = None  # type: ignore[assignment]

try:
    from .engine import build_laya_request, LayaUnavailable
except ImportError:  # Running ``python jev_client.py`` from this directory.
    from engine import build_laya_request, LayaUnavailable


TYPESAFE_API_URL = "https://api.typesafe.ai/v1/systemone"
TYPESAFE_API_KEY_ENV = "TYPESAFE_API_KEY"
TYPESAFE_API_KEY_FILE = Path.home() / ".config" / "jev-snake" / "typesafe_api_key"
DEFAULT_TIMEOUT_SECONDS = 20.0
MAX_ATTEMPTS = 5
MAX_RETRY_DELAY_SECONDS = 4.0


class JevUnavailable(LayaUnavailable):
    """Jev is not configured or could not complete a decision."""


# Keep the familiar provider error name available to callers that share the
# model-provider code with the snake service.
ModelUnavailableError = JevUnavailable


def _transport_errors() -> tuple[type[BaseException], ...]:
    errors: tuple[type[BaseException], ...] = (OSError, TimeoutError)
    if httpx is not None:
        transport_error = getattr(httpx, "TransportError", None)
        if isinstance(transport_error, type):
            errors += (transport_error,)
    return errors


class JevClient:
    """Call Jev with one reusable httpx connection pool when available."""

    endpoint = TYPESAFE_API_URL
    provider_name = "jev"
    provider_label = "Jev"

    def __init__(self, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self.timeout = timeout
        self._client = httpx.Client(timeout=timeout) if httpx is not None else None

    @property
    def dependency_available(self) -> bool:
        return self._client is not None

    def close(self) -> None:
        """Close the reusable connection pool, if the optional dependency exists."""

        if self._client is not None:
            self._client.close()
            self._client = None

    @staticmethod
    def _api_key() -> str | None:
        value = os.environ.get(TYPESAFE_API_KEY_ENV)
        if value is not None:
            value = value.strip()
            if value:
                return value
        try:
            value = TYPESAFE_API_KEY_FILE.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            return None
        return value or None

    @classmethod
    def is_configured(cls) -> bool:
        """Return whether a non-empty key is available in this process."""

        return cls._api_key() is not None

    def health(self) -> tuple[int, dict[str, Any]]:
        """Check local dependency and configuration without making a request."""

        if not self.dependency_available:
            return 503, {
                "status": "dependency_missing",
                "model": "jev",
                "error": "Jev 依赖 httpx 未安装",
            }
        if not self.is_configured():
            return 503, {
                "status": "unconfigured",
                "model": "jev",
                "error": f"{TYPESAFE_API_KEY_ENV} 未配置",
            }
        return 200, {"status": "ready", "model": "jev"}

    @staticmethod
    def _decode(body: bytes) -> dict[str, Any]:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise JevUnavailable("Jev 返回的内容不是有效 JSON") from error
        if not isinstance(payload, dict):
            raise JevUnavailable("Jev 返回的 JSON 顶层不是对象")
        if not isinstance(payload.get("answers"), Mapping):
            raise JevUnavailable("Jev 返回的 JSON 缺少 answers 对象")
        usage = payload.get("usage")
        if usage is not None and not isinstance(usage, Mapping):
            raise JevUnavailable("Jev 返回的 usage 不是对象")
        model = payload.get("model")
        if model is not None and not isinstance(model, str):
            raise JevUnavailable("Jev 返回的 model 不是字符串")
        return payload

    @staticmethod
    def _error_text(error: BaseException, secret: str | None = None) -> str:
        message = " ".join(str(error).split())
        if secret:
            message = message.replace(secret, "***")
        return message[:240] if message else type(error).__name__

    def decide(
        self,
        fen_or_state: str | dict[str, Any],
        legal_moves_or_questions: list[str] | dict[str, dict[str, Any]],
        candidates: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Submit the shared Xiangqi state/questions and return Jev's response.

        The three-argument form is the provider interface used by
        ``choose_laya_move``.  The two-argument form is kept for focused client
        tests and callers that already built the shared payload.
        """

        if candidates is None:
            if not isinstance(fen_or_state, dict) or not isinstance(legal_moves_or_questions, dict):
                raise TypeError("Jev.decide 需要 (fen, legal_moves, candidates) 或 (state, questions)")
            state = fen_or_state
            questions = legal_moves_or_questions
        else:
            if not isinstance(fen_or_state, str) or not isinstance(legal_moves_or_questions, list):
                raise TypeError("Jev.decide 需要 (fen, legal_moves, candidates)")
            built = build_laya_request(fen_or_state, legal_moves_or_questions, candidates)
            state = built["state"]
            questions = built["questions"]

        secret = self._api_key()
        if secret is None:
            raise JevUnavailable(f"{TYPESAFE_API_KEY_ENV} 未配置")
        if self._client is None:
            raise JevUnavailable("Jev 依赖 httpx 未安装")

        body = json.dumps(
            {"state": state, "questions": questions, "model": "jev-latest"},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        transport_errors = _transport_errors()
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self._client.post(self.endpoint, content=body, headers=headers)
            except transport_errors as error:
                if attempt < MAX_ATTEMPTS:
                    time.sleep(0.5 * 2 ** (attempt - 1))
                    continue
                detail = self._error_text(error, secret)
                raise JevUnavailable(f"无法连接 Jev：{detail}") from error

            status = response.status_code
            if status == 429 or status == 529 or 500 <= status < 600:
                if attempt < MAX_ATTEMPTS:
                    retry_after = response.headers.get("Retry-After")
                    delay = (
                        float(retry_after)
                        if retry_after and retry_after.isdigit()
                        else 0.5 * 2 ** (attempt - 1)
                    )
                    time.sleep(min(delay, MAX_RETRY_DELAY_SECONDS))
                    continue
                raise JevUnavailable(f"Jev HTTP {status}")
            if status < 200 or status >= 300:
                raise JevUnavailable(f"Jev HTTP {status}")
            payload = self._decode(response.content)
            payload["_transport_attempts"] = attempt
            return payload
        raise AssertionError("Jev 请求重试次数耗尽")


__all__ = [
    "JevClient",
    "JevUnavailable",
    "ModelUnavailableError",
    "TYPESAFE_API_KEY_ENV",
    "TYPESAFE_API_KEY_FILE",
    "TYPESAFE_API_URL",
]
