"""Small client for the TypeSafe Jev System One API."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Mapping

import httpx

try:
    from .engine import ModelUnavailableError
except ImportError:  # Running ``python jev_client.py`` from this directory.
    from engine import ModelUnavailableError


TYPESAFE_API_URL = "https://api.typesafe.ai/v1/systemone"
TYPESAFE_API_KEY_ENV = "TYPESAFE_API_KEY"
TYPESAFE_API_KEY_FILE = Path.home() / ".config" / "jev-snake" / "typesafe_api_key"
DEFAULT_TIMEOUT_SECONDS = 20.0
MAX_ATTEMPTS = 5
MAX_RETRY_DELAY_SECONDS = 4.0


class JevClient:
    """Call the fixed TypeSafe endpoint without persisting the API key."""

    endpoint = TYPESAFE_API_URL

    def __init__(self, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self.timeout = timeout
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        """Close the reusable HTTP connection pool."""

        self._client.close()

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
        """Check local configuration without making a network request."""

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
            raise ModelUnavailableError("Jev 返回的内容不是有效 JSON") from error
        if not isinstance(payload, dict):
            raise ModelUnavailableError("Jev 返回的 JSON 顶层不是对象")
        answers = payload.get("answers")
        if not isinstance(answers, Mapping):
            raise ModelUnavailableError("Jev 返回的 JSON 缺少 answers 对象")
        usage = payload.get("usage")
        if usage is not None and not isinstance(usage, Mapping):
            raise ModelUnavailableError("Jev 返回的 usage 不是对象")
        model = payload.get("model")
        if model is not None and not isinstance(model, str):
            raise ModelUnavailableError("Jev 返回的 model 不是字符串")
        return payload

    @staticmethod
    def _error_text(error: BaseException, secret: str | None = None) -> str:
        message = " ".join(str(error).split())
        if secret:
            message = message.replace(secret, "***")
        return message[:240] if message else type(error).__name__

    def decide(self, state: Any, questions: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """Submit the same structured state as Laya and return the response."""

        secret = self._api_key()
        if secret is None:
            raise ModelUnavailableError(f"{TYPESAFE_API_KEY_ENV} 未配置")

        body = json.dumps(
            {
                "state": state,
                "questions": questions,
                "model": "jev-latest",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self._client.post(self.endpoint, content=body, headers=headers)
            except (httpx.TransportError, TimeoutError, OSError) as error:
                if attempt < MAX_ATTEMPTS:
                    time.sleep(0.5 * 2 ** (attempt - 1))
                    continue
                detail = self._error_text(error, secret)
                raise ModelUnavailableError(f"无法连接 Jev：{detail}") from error

            status = response.status_code
            if status in {429, 529} or 500 <= status < 600:
                if attempt < MAX_ATTEMPTS:
                    retry_after = response.headers.get("Retry-After")
                    delay = (
                        float(retry_after)
                        if retry_after and retry_after.isdigit()
                        else 0.5 * 2 ** (attempt - 1)
                    )
                    time.sleep(min(delay, MAX_RETRY_DELAY_SECONDS))
                    continue
                raise ModelUnavailableError(f"Jev HTTP {status}")
            if status < 200 or status >= 300:
                raise ModelUnavailableError(f"Jev HTTP {status}")
            payload = self._decode(response.content)
            payload["_transport_attempts"] = attempt
            return payload
        raise AssertionError("Jev 请求重试次数耗尽")


__all__ = ["JevClient", "TYPESAFE_API_KEY_ENV", "TYPESAFE_API_KEY_FILE", "TYPESAFE_API_URL"]
