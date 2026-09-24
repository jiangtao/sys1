"""Client for the local Kev-0.8B TypeSafe System One service."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any

try:
    from .engine import ModelUnavailableError
except ImportError:  # Running ``python kev_client.py`` from this directory.
    from engine import ModelUnavailableError


DEFAULT_KEV_URL = "http://127.0.0.1:8009"
KEV_URL_ENV = "LAYA_SNAKE_KEV_URL"
KEV_MODEL_NAME = "kev-latest"
EXPECTED_KEV_RUN_PREFIX = "jaredpalmer/kev-0.8b@"
DEFAULT_TIMEOUT_SECONDS = 25.0


class KevUnavailable(ModelUnavailableError):
    """Kev is unreachable or did not return a usable System One response."""


class KevClient:
    """Call the loopback Kev service without an API key."""

    provider_name = "kev"
    provider_label = "Kev"

    def __init__(self, base_url: str | None = None, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        configured_url = base_url or os.environ.get(KEV_URL_ENV, DEFAULT_KEV_URL)
        self.base_url = configured_url.rstrip("/")
        self.timeout = timeout

    @staticmethod
    def _decode(body: bytes) -> dict[str, Any]:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise KevUnavailable("Kev 返回的内容不是有效 JSON") from error
        if not isinstance(payload, dict):
            raise KevUnavailable("Kev 返回的 JSON 顶层不是对象")
        return payload

    @staticmethod
    def _error_text(error: BaseException) -> str:
        message = " ".join(str(error).split())
        return message[:240] if message else type(error).__name__

    def _request(self, path: str, *, data: bytes | None = None) -> tuple[int, dict[str, Any]]:
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers=headers,
            method="POST" if data is not None else "GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, self._decode(response.read())
        except urllib.error.HTTPError as error:
            try:
                payload = self._decode(error.read())
            except KevUnavailable:
                payload = {"status": "error", "error": self._error_text(error)}
            return error.code, payload
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise KevUnavailable(f"无法连接 Kev：{self._error_text(error)}") from error

    def health(self) -> tuple[int, dict[str, Any]]:
        """Probe Kev's model registry and require the loaded Kev-0.8B checkpoint."""

        try:
            status, payload = self._request("/v1/models")
        except KevUnavailable as error:
            return 503, {
                "status": "unreachable",
                "model": self.provider_name,
                "endpoint": self.base_url,
                "error": str(error),
            }
        if status < 200 or status >= 300:
            detail = payload.get("error") or payload.get("detail") or f"Kev HTTP {status}"
            return status, {
                "status": "error",
                "model": self.provider_name,
                "endpoint": self.base_url,
                "error": str(detail)[:240],
            }

        models = payload.get("models")
        if not isinstance(models, list) or not models or not isinstance(models[0], Mapping):
            return 503, {
                "status": "invalid",
                "model": self.provider_name,
                "endpoint": self.base_url,
                "error": "Kev /v1/models 缺少 models[0]",
            }
        loaded = models[0]
        run = loaded.get("run")
        if loaded.get("name") != KEV_MODEL_NAME or not isinstance(run, str) or not run.startswith(EXPECTED_KEV_RUN_PREFIX):
            return 503, {
                "status": "wrong_model",
                "model": self.provider_name,
                "endpoint": self.base_url,
                "loadedModel": loaded.get("name"),
                "run": run,
                "error": f"已加载模型不是 {EXPECTED_KEV_RUN_PREFIX}…",
            }
        return 200, {
            "status": "ready",
            "model": self.provider_name,
            "modelName": loaded.get("name"),
            "run": run,
            "endpoint": self.base_url,
            "device": loaded.get("device"),
            "backend": loaded.get("backend"),
            "dtype": loaded.get("dtype"),
        }

    def decide(self, state: str | dict[str, Any], questions: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """Submit a shared state/questions payload to local Kev."""

        body = json.dumps(
            {"model": KEV_MODEL_NAME, "state": state, "questions": questions},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        status, payload = self._request("/v1/systemone", data=body)
        if status < 200 or status >= 300:
            detail = payload.get("error") or payload.get("detail") or f"Kev HTTP {status}"
            raise KevUnavailable(f"Kev HTTP {status}：{str(detail)[:240]}")
        answers = payload.get("answers")
        if not isinstance(answers, Mapping):
            raise KevUnavailable("Kev 返回的 JSON 缺少 answers 对象")
        response_model = payload.get("model")
        if response_model is not None and response_model != KEV_MODEL_NAME:
            raise KevUnavailable(f"Kev 返回了意外模型：{response_model!r}")
        usage = payload.get("usage")
        if usage is not None and not isinstance(usage, Mapping):
            raise KevUnavailable("Kev 返回的 usage 不是对象")
        return payload


__all__ = [
    "DEFAULT_KEV_URL",
    "EXPECTED_KEV_RUN_PREFIX",
    "KEV_MODEL_NAME",
    "KEV_URL_ENV",
    "KevClient",
    "KevUnavailable",
]
