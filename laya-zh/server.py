"""Run the local Chinese Laya model as a loopback-only HTTP service."""

from __future__ import annotations

import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any


DEPLOY_DIR = Path(__file__).resolve().parent
MODEL_DIR = DEPLOY_DIR / "model"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
REQUESTED_DEVICE = "mps"


def configure_environment() -> None:
    """Keep the service on the local PyTorch checkpoint without network access."""

    os.environ["USE_TF"] = "0"
    os.environ["USE_TORCH"] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"


configure_environment()

from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from pydantic import BaseModel, ConfigDict, Field  # noqa: E402


LOGGER = logging.getLogger("laya-zh")


class DecideRequest(BaseModel):
    """The native state/questions shape accepted by ``Agent.predict``."""

    model_config = ConfigDict(extra="forbid")

    state: str | dict[str, Any] | list[Any]
    questions: dict[str, dict[str, Any]] = Field(min_length=1)


class ModelRuntime:
    """Own one loaded Agent and serialize inference through it."""

    def __init__(self) -> None:
        self.agent: Any | None = None
        self.actual_device: str | None = None
        self.status = "starting"
        self.fallback_reason: str | None = None
        self.error_reason: str | None = None
        self.inference_lock = threading.Lock()

    @staticmethod
    def _reason(error: BaseException) -> str:
        try:
            message = " ".join(str(error).split())
        except Exception:
            message = "异常原因无法读取"
        if len(message) > 240:
            message = f"{message[:237]}..."
        return f"{type(error).__name__}: {message}" if message else type(error).__name__

    def initialize(self) -> None:
        """Load MPS first and fall back to CPU without propagating a stack trace."""

        if not (MODEL_DIR / "model.safetensors").is_file():
            self.status = "error"
            self.error_reason = f"本地模型不存在：{MODEL_DIR}"
            return

        try:
            import laya

            self.agent = laya.load(str(MODEL_DIR), device=REQUESTED_DEVICE)
        except Exception as mps_error:
            self.fallback_reason = self._reason(mps_error)
            try:
                import laya

                self.agent = laya.load(str(MODEL_DIR), device="cpu")
            except Exception as cpu_error:
                self.status = "error"
                self.error_reason = f"MPS 和 CPU 初始化均失败：{self._reason(cpu_error)}"
                LOGGER.error("model initialization failed after MPS fallback: %s", cpu_error)
                return

        from laya_compat import register_single_option_padding

        register_single_option_padding(self.agent.model)
        self.actual_device = str(self.agent.device)
        if self.actual_device != REQUESTED_DEVICE and self.fallback_reason is None:
            self.fallback_reason = "MPS 不可用或未被模型接受，已由 Laya 回退到 CPU"
        self.status = "ready"

    def health(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "device": self.actual_device,
            "requested_device": REQUESTED_DEVICE,
            "model_path": str(MODEL_DIR),
            "fallback_reason": self.fallback_reason,
            "error": self.error_reason,
        }

    def predict(self, state: str | dict[str, Any] | list[Any], questions: dict[str, dict[str, Any]]) -> dict[str, Any]:
        if self.status != "ready" or self.agent is None:
            raise RuntimeError("模型服务尚未就绪")
        with self.inference_lock:
            return self.agent.predict(state, questions)


runtime = ModelRuntime()


INFERENCE_LOG_PATH = DEPLOY_DIR / "runtime" / "inference-errors.log"
INFERENCE_LOG_MAX_BYTES = 3 * 1024 * 1024
INFERENCE_LOG_BACKUP_COUNT = 2


def configure_inference_logging() -> None:
    """Add the bounded inference log once when the service process starts."""

    log_path = INFERENCE_LOG_PATH.resolve()
    for handler in LOGGER.handlers:
        if not isinstance(handler, RotatingFileHandler):
            continue
        try:
            existing_path = Path(handler.baseFilename).resolve()
        except Exception:
            continue
        if existing_path == log_path:
            LOGGER.setLevel(logging.INFO)
            return

    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_path,
        maxBytes=INFERENCE_LOG_MAX_BYTES,
        backupCount=INFERENCE_LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S%z"))
    LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.INFO)


def new_request_id() -> str:
    """Return a short identifier safe to include in the client error message."""

    return uuid.uuid4().hex[:12]


def _exception_type(error: BaseException) -> str:
    try:
        name = type(error).__name__
    except Exception:
        return "Exception"
    return name if isinstance(name, str) and name else "Exception"


def _reason_text(error: BaseException, max_length: int = 240) -> str:
    try:
        message = " ".join(str(error).split())
    except Exception:
        message = "异常原因无法读取"
    if not message:
        message = "未提供异常原因"
    if max_length < 1:
        return ""
    if len(message) > max_length:
        if max_length <= 3:
            return message[:max_length]
        return f"{message[: max_length - 3]}..."
    return message


def _state_char_count(state: Any) -> int | None:
    if state is None:
        return 0
    if isinstance(state, str):
        return len(state)
    try:
        encoded = json.dumps(
            state,
            ensure_ascii=False,
            separators=(",", ":"),
            default=lambda _: "<unserializable>",
        )
        return len(encoded)
    except Exception:
        try:
            return len(repr(state))
        except Exception:
            return None


def _metadata_key(value: Any, index: int) -> str:
    try:
        key = str(value)
    except Exception:
        key = f"choice_{index}"
    if not key:
        key = f"choice_{index}"
    return key[:80]


def input_metadata(state: Any, questions: Any) -> dict[str, Any]:
    """Summarize request shape without retaining request content."""

    metadata: dict[str, Any] = {
        "state_chars": _state_char_count(state),
        "question_count": 0,
        "choice_candidate_counts": {},
    }
    if not isinstance(questions, dict):
        return metadata

    try:
        metadata["question_count"] = len(questions)
    except Exception:
        metadata["question_count"] = 0

    choice_counts: dict[str, int] = {}
    try:
        items = list(questions.items())
    except Exception:
        items = []
    for index, (name, definition) in enumerate(items):
        if not isinstance(definition, dict):
            continue
        try:
            question_type = definition.get("type")
        except Exception:
            continue
        if question_type != "choice":
            continue
        try:
            candidates = definition.get("criteria")
            count = len(candidates) if isinstance(candidates, (dict, list, tuple, set)) else 0
        except Exception:
            count = 0
        choice_counts[_metadata_key(name, index)] = count
    metadata["choice_candidate_counts"] = choice_counts
    return metadata


def _input_parts(body: Any) -> tuple[Any, Any]:
    if not isinstance(body, dict):
        return None, None
    try:
        return body.get("state"), body.get("questions")
    except Exception:
        return None, None


def _elapsed_ms(started_at: float) -> int:
    try:
        return max(0, int(round((time.perf_counter() - started_at) * 1000)))
    except Exception:
        return 0


def diagnostic_details(
    request_id: str,
    started_at: float,
    state: Any,
    questions: Any,
    error: BaseException | None = None,
    *,
    exception_type: str | None = None,
    reason: str | None = None,
    validation_errors: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    details: dict[str, Any] = {
        "request_id": request_id,
        "device": runtime.actual_device,
        "elapsed_ms": _elapsed_ms(started_at),
        "input_metadata": input_metadata(state, questions),
    }
    if error is not None:
        if exception_type is None:
            exception_type = _exception_type(error)
        if reason is None:
            reason = _reason_text(error)
    if exception_type is not None:
        details["exception_type"] = exception_type
    if reason is not None:
        details["reason"] = reason[:240]
    if validation_errors is not None:
        details["errors"] = validation_errors
    return details


def diagnostic_message(
    summary: str,
    request_id: str,
    error: BaseException | None = None,
    *,
    exception_type: str | None = None,
    reason: str | None = None,
) -> str:
    if error is not None:
        if exception_type is None:
            exception_type = _exception_type(error)
        if reason is None:
            reason = _reason_text(error, max_length=120)
    message = f"{summary}（request_id={request_id}"
    if exception_type is not None:
        message += f"，{exception_type}"
        if reason:
            message += f": {reason}"
    return f"{message}）" if len(message) < 200 else f"{message[:199]}）"


def log_exception(
    event: str,
    request_id: str,
    started_at: float,
    state: Any,
    questions: Any,
    error: BaseException,
) -> None:
    """Log traceback plus bounded request metadata, never the request body."""

    try:
        metadata = input_metadata(state, questions)
    except Exception:
        metadata = {"state_chars": None, "question_count": None, "choice_candidate_counts": {}}
    try:
        LOGGER.exception(
            "%s request_id=%s elapsed_ms=%s device=%s input_metadata=%s",
            event,
            request_id,
            _elapsed_ms(started_at),
            runtime.actual_device,
            metadata,
            exc_info=(type(error), error, error.__traceback__),
        )
    except Exception:
        # Logging must never replace the original inference exception.
        return


def _validation_items(exc: RequestValidationError) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    try:
        errors = exc.errors()
    except Exception:
        errors = []
    for item in errors:
        try:
            location = ".".join(str(part) for part in item.get("loc", ()))
            message = str(item.get("msg", "参数无效"))
        except Exception:
            location = ""
            message = "参数无效"
        items.append({"loc": location, "message": message})
    return items


@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_inference_logging()
    runtime.initialize()
    yield


app = FastAPI(title="Laya 中文本地服务", version="0.1.0", lifespan=lifespan)


def error_payload(code: str, message: str, details: Any | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"error": {"code": code, "message": message}}
    if details is not None:
        payload["error"]["details"] = details
    return payload


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    started_at = time.perf_counter()
    request_id = new_request_id()
    state, questions = _input_parts(getattr(exc, "body", None))
    validation_errors = _validation_items(exc)
    details = diagnostic_details(
        request_id,
        started_at,
        state,
        questions,
        exc,
        reason="请求参数校验失败",
        validation_errors=validation_errors,
    )
    log_exception("request validation failed", request_id, started_at, state, questions, exc)
    return JSONResponse(
        status_code=422,
        content=error_payload(
            "invalid_request",
            diagnostic_message("请求参数无效", request_id, exc, reason="请求参数校验失败"),
            details,
        ),
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    started_at = time.perf_counter()
    request_id = new_request_id()
    details = diagnostic_details(
        request_id,
        started_at,
        None,
        None,
        exc,
    )
    log_exception("unhandled request error", request_id, started_at, None, None, exc)
    return JSONResponse(
        status_code=500,
        content=error_payload(
            "internal_error",
            diagnostic_message("服务内部错误", request_id, exc),
            details,
        ),
    )


@app.get("/health")
def health() -> JSONResponse:
    payload = runtime.health()
    status_code = 200 if payload["status"] == "ready" else 503
    return JSONResponse(status_code=status_code, content=payload)


@app.post("/v1/decide")
def decide(payload: DecideRequest) -> dict[str, Any]:
    started_at = time.perf_counter()
    request_id = new_request_id()
    if runtime.status != "ready":
        details = diagnostic_details(
            request_id,
            started_at,
            payload.state,
            payload.questions,
            exception_type="ServiceUnavailable",
            reason="模型服务尚未就绪",
        )
        raise HTTPException(
            status_code=503,
            detail=error_payload(
                "service_unavailable",
                diagnostic_message(
                    "模型服务尚未就绪",
                    request_id,
                    exception_type="ServiceUnavailable",
                    reason="模型服务尚未就绪",
                ),
                details,
            ),
        )

    try:
        result = runtime.predict(payload.state, payload.questions)
    except (KeyError, TypeError, ValueError) as error:
        details = diagnostic_details(request_id, started_at, payload.state, payload.questions, error)
        log_exception("invalid Laya input", request_id, started_at, payload.state, payload.questions, error)
        raise HTTPException(
            status_code=422,
            detail=error_payload(
                "invalid_questions",
                diagnostic_message("问题定义无效", request_id, error),
                details,
            ),
        ) from error
    except RuntimeError as error:
        details = diagnostic_details(request_id, started_at, payload.state, payload.questions, error)
        log_exception("model inference failed", request_id, started_at, payload.state, payload.questions, error)
        raise HTTPException(
            status_code=503,
            detail=error_payload(
                "inference_unavailable",
                diagnostic_message("模型暂时无法推理", request_id, error),
                details,
            ),
        ) from error
    except Exception as error:
        details = diagnostic_details(request_id, started_at, payload.state, payload.questions, error)
        log_exception("unexpected model inference error", request_id, started_at, payload.state, payload.questions, error)
        raise HTTPException(
            status_code=500,
            detail=error_payload(
                "inference_failed",
                diagnostic_message("模型推理失败", request_id, error),
                details,
            ),
        ) from error

    response = dict(result)
    response["status"] = "ok"
    response["device"] = runtime.actual_device
    response["requestId"] = request_id
    return response


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("服务只允许绑定本机回环地址")
    if not 1 <= args.port <= 65535:
        parser.error("端口必须在 1 到 65535 之间")
    return args


def main() -> None:
    import uvicorn

    args = parse_args()
    uvicorn.run(app, host=args.host, port=args.port, workers=1, log_level="info")


if __name__ == "__main__":
    main()
