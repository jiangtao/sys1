"""Best-effort shadow client for the local Laya next-action service.

This module deliberately has no routing side effects.  It observes a possible
next action and returns a small result to the caller; the caller remains the
authority that decides whether to dispatch an agent.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import math
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any


DEFAULT_BASE_URL = "http://127.0.0.1:8765"
DEFAULT_TIMEOUT = 2.0
MAX_RESPONSE_BYTES = 1_000_000
MAX_STATE_BYTES = 2_048
STATE_FIELDS = frozenset(
    {
        "goal",
        "last_actor",
        "last_status",
        "last_summary",
        "allowed_actions",
        "allowed_surfaces",
    }
)

ACTIONS = (
    "main_control",
    "research",
    "design",
    "implement",
    "validate",
    "review",
)
SURFACES = ("frontend", "backend", "both")

ACTION_CRITERIA = {
    "main_control": "由 main_control 直接处理、澄清、整合结果、判断权限或完成最终验收",
    "research": "需要跨文件、日志、文档或历史资料收集证据",
    "design": "事实基本充分，但需要跨系统方案或重大取舍",
    "implement": "改动范围、方案和验收条件明确，可以执行实现",
    "validate": "已有产物，需要验证前端用户链路或后端 API/数据链路",
    "review": "已有固定基线或 diff，需要独立检查变更",
}
SURFACE_CRITERIA = {
    "frontend": "涉及前端界面、交互或真实浏览器用户链路",
    "backend": "涉及后端 API、服务、数据或跨组件后端链路",
    "both": "前端和后端都需要处理或验证",
}
ACTION_ROLES = {
    "main_control": (),
    "research": ("evidence_researcher",),
    "design": ("solution_designer",),
    "implement": ("implementation_worker",),
    "review": ("incremental_reviewer",),
}


class _ClientError(Exception):
    """An expected client/protocol failure with a safe reason and detail."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


class _ProtocolError(_ClientError):
    pass


def _decision_id() -> str:
    return uuid.uuid4().hex[:12]


def _state_hash(state: Any) -> str | None:
    try:
        encoded = json.dumps(
            state,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        return None
    return hashlib.sha256(encoded).hexdigest()


def _safe_float(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("not a number")
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise ValueError("number out of range")
    return number


def _validate_state(state: Any) -> tuple[dict[str, Any], list[str], list[str]]:
    if not isinstance(state, dict):
        raise _ClientError("invalid_input", "state must be a JSON object")
    unknown_fields = set(state) - STATE_FIELDS
    if unknown_fields:
        raise _ClientError("invalid_input", "state contains an unsupported field")

    for field in ("goal", "last_actor", "last_status", "last_summary"):
        if not isinstance(state.get(field), str):
            raise _ClientError("invalid_input", f"{field} must be a string")

    allowed_actions = state.get("allowed_actions")
    if not isinstance(allowed_actions, list) or not allowed_actions:
        raise _ClientError("invalid_input", "allowed_actions must be a non-empty list")
    if any(not isinstance(item, str) for item in allowed_actions):
        raise _ClientError("invalid_input", "allowed_actions contains a non-string")
    if len(set(allowed_actions)) != len(allowed_actions):
        raise _ClientError("invalid_input", "allowed_actions contains duplicates")
    if any(item not in ACTIONS for item in allowed_actions):
        raise _ClientError("invalid_input", "allowed_actions contains an unknown action")
    if "main_control" not in allowed_actions:
        raise _ClientError("invalid_input", "allowed_actions must include main_control")

    allowed_surfaces_value = state.get("allowed_surfaces", list(SURFACES))
    if not isinstance(allowed_surfaces_value, list) or not allowed_surfaces_value:
        raise _ClientError("invalid_input", "allowed_surfaces must be a non-empty list")
    if any(not isinstance(item, str) for item in allowed_surfaces_value):
        raise _ClientError("invalid_input", "allowed_surfaces contains a non-string")
    if len(set(allowed_surfaces_value)) != len(allowed_surfaces_value):
        raise _ClientError("invalid_input", "allowed_surfaces contains duplicates")
    if any(item not in SURFACES for item in allowed_surfaces_value):
        raise _ClientError("invalid_input", "allowed_surfaces contains an unknown surface")

    # Keep the caller's state intact.  The copy is also the object sent to the
    # service, so a non-JSON value is rejected before any network call.
    try:
        encoded = json.dumps(
            state,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as error:
        raise _ClientError("invalid_input", "state is not JSON serializable") from error
    if len(encoded) > MAX_STATE_BYTES:
        raise _ClientError("invalid_input", "state is too large")
    return state, list(allowed_actions), list(allowed_surfaces_value)


def _validate_timeout(timeout: Any) -> float:
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise _ClientError("invalid_input", "timeout must be a positive number")
    value = float(timeout)
    if not math.isfinite(value) or value <= 0:
        raise _ClientError("invalid_input", "timeout must be a positive number")
    return value


def _loopback_url(base_url: Any) -> str:
    if not isinstance(base_url, str) or not base_url.strip():
        raise _ClientError("invalid_base_url", "base_url must be a loopback HTTP URL")
    try:
        parsed = urllib.parse.urlsplit(base_url)
        host = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise _ClientError("invalid_base_url", "base_url must be a loopback HTTP URL") from error
    if parsed.scheme.lower() != "http" or host is None or parsed.username or parsed.password:
        raise _ClientError("invalid_base_url", "base_url must be a loopback HTTP URL")
    if parsed.query or parsed.fragment:
        raise _ClientError("invalid_base_url", "base_url must not contain query or fragment")
    if port is not None and not 1 <= port <= 65535:
        raise _ClientError("invalid_base_url", "base_url has an invalid port")
    host_lower = host.lower().rstrip(".")
    is_loopback = host_lower == "localhost"
    if not is_loopback:
        try:
            is_loopback = ipaddress.ip_address(host_lower).is_loopback
        except ValueError:
            is_loopback = False
    if not is_loopback:
        raise _ClientError("invalid_base_url", "base_url must use a loopback host")
    return base_url.rstrip("/") + "/v1/decide"


def _question(name: str, options: list[str], criteria: dict[str, str], instructions: str) -> dict[str, Any]:
    return {
        name: {
            "type": "choice",
            "instructions": instructions,
            "criteria": {option: criteria[option] for option in options},
        }
    }


_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _urlopen_no_proxy(request: urllib.request.Request, timeout: float) -> Any:
    """Open only through a direct connection; never consult HTTP proxy env."""

    return _NO_PROXY_OPENER.open(request, timeout=timeout)


def _post_decision(
    base_url: str,
    state: dict[str, Any],
    questions: dict[str, Any],
    timeout: float,
) -> dict[str, Any]:
    url = _loopback_url(base_url)
    try:
        body = json.dumps(
            {"state": state, "questions": questions},
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as error:
        raise _ClientError("invalid_input", "request state is not JSON serializable") from error

    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with _urlopen_no_proxy(request, timeout=timeout) as response:
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            if not isinstance(status, int) or not 200 <= status < 300:
                raise _ClientError("service_http_error", f"service returned HTTP {status}")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except _ClientError:
        raise
    except urllib.error.HTTPError as error:
        raise _ClientError("service_http_error", f"service returned HTTP {error.code}") from error
    except (TimeoutError, urllib.error.URLError, OSError) as error:
        if isinstance(error, TimeoutError) or "timed out" in str(getattr(error, "reason", error)).lower():
            raise _ClientError("service_timeout", "service request timed out") from error
        raise _ClientError("service_unavailable", "service connection failed") from error
    except Exception as error:
        # A mocked/custom transport should not be able to break the caller's
        # task either.  Do not expose exception text, which may contain input.
        raise _ClientError("service_unavailable", "service request failed") from error

    if not isinstance(raw, (bytes, bytearray)) or len(raw) > MAX_RESPONSE_BYTES:
        raise _ClientError("malformed_response", "service response is too large")
    try:
        payload = json.loads(bytes(raw).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise _ClientError("malformed_response", "service response is not valid JSON") from error
    if not isinstance(payload, dict):
        raise _ClientError("malformed_response", "service response must be a JSON object")
    return payload


def _parse_choice_response(payload: dict[str, Any], question_name: str, options: list[str]) -> dict[str, Any]:
    answers = payload.get("answers")
    if not isinstance(answers, dict):
        raise _ProtocolError("malformed_response", "service response has no answers object")
    answer = answers.get(question_name)
    if not isinstance(answer, dict) or answer.get("type") != "choice":
        raise _ProtocolError("malformed_response", "service response has an invalid choice answer")
    choice = answer.get("choice")
    if not isinstance(choice, str):
        raise _ProtocolError("malformed_response", "service choice is not a string")
    if choice not in options:
        raise _ProtocolError("invalid_candidate", "service selected an option outside the allowed candidates")

    probabilities = answer.get("probabilities")
    if not isinstance(probabilities, dict):
        raise _ProtocolError("malformed_response", "service response has no probabilities object")
    if set(probabilities) != set(options):
        raise _ProtocolError("malformed_response", "service probabilities do not match allowed candidates")
    parsed_probabilities: dict[str, float] = {}
    for option in options:
        try:
            parsed_probabilities[option] = _safe_float(probabilities[option])
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            raise _ProtocolError("malformed_response", "service returned an invalid probability") from error

    request_id = payload.get("requestId")
    model = payload.get("model")
    if not isinstance(request_id, str) or not request_id:
        raise _ProtocolError("malformed_response", "service response has no requestId")
    if not isinstance(model, str) or not model:
        raise _ProtocolError("malformed_response", "service response has no model")
    return {
        "choice": choice,
        "probabilities": parsed_probabilities,
        "requestId": request_id,
        "model": model,
    }


def _skip_result(
    decision_id: str,
    state_hash: str | None,
    reason: str,
    detail: str,
    started_at: float,
) -> dict[str, Any]:
    return {
        "decision_id": decision_id,
        "status": "skipped",
        "mode": "shadow",
        "skip_reason": reason,
        "detail": detail[:160],
        "state_hash": state_hash,
        "elapsed_ms": round(max(0.0, (time.perf_counter() - started_at) * 1000), 1),
    }


def _observed_result(
    decision_id: str,
    state_hash: str | None,
    action: str,
    surface: str | None,
    action_probabilities: dict[str, float],
    surface_probabilities: dict[str, float] | None,
    action_response: dict[str, Any],
    surface_response: dict[str, Any] | None,
    started_at: float,
) -> dict[str, Any]:
    roles: tuple[str, ...]
    if action in ("implement", "validate") and surface is not None:
        roles = {
            "frontend": ("e2e_tester",) if action == "validate" else ("implementation_worker",),
            "backend": ("backend_tester",) if action == "validate" else ("implementation_worker",),
            "both": ("e2e_tester", "backend_tester") if action == "validate" else ("implementation_worker",),
        }[surface]
    else:
        roles = ACTION_ROLES[action]
    service: dict[str, Any] = {
        "requestId": action_response["requestId"],
        "model": action_response["model"],
    }
    if surface_response is not None:
        service["surfaceRequestId"] = surface_response["requestId"]
        service["surfaceModel"] = surface_response["model"]
    probabilities: dict[str, Any] = {"action": action_probabilities}
    if surface_probabilities is not None:
        probabilities["surface"] = surface_probabilities
    result: dict[str, Any] = {
        "decision_id": decision_id,
        "status": "observed",
        "mode": "shadow",
        "suggested_action": action,
        "surface": surface,
        "suggested_surface": surface,
        "candidate_roles": list(roles),
        "probabilities": probabilities,
        "action_probabilities": action_probabilities,
        "service": service,
        "requestId": action_response["requestId"],
        "model": action_response["model"],
        "state_hash": state_hash,
        "elapsed_ms": round(max(0.0, (time.perf_counter() - started_at) * 1000), 1),
    }
    if surface_probabilities is not None:
        result["surface_probabilities"] = surface_probabilities
    return result


def suggest_next_action(
    state: dict[str, Any],
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Observe one allowed next action without dispatching any agent.

    Every expected service or protocol failure is represented as
    ``status="skipped"``.  This function is intentionally best effort so a
    local Laya outage cannot block the caller's normal Codex execution.
    """

    started_at = time.perf_counter()
    decision_id = _decision_id()
    state_hash = _state_hash(state)
    try:
        validated_state, allowed_actions, allowed_surfaces = _validate_state(state)
        timeout_value = _validate_timeout(timeout)
        # Validate the URL before the cheap no-routing shortcut as well: a
        # caller can therefore observe malformed configuration consistently.
        _loopback_url(base_url)
        if allowed_actions == ["main_control"]:
            return _skip_result(decision_id, state_hash, "no_routing_needed", "only main_control is allowed", started_at)

        action_payload = _post_decision(
            base_url,
            validated_state,
            _question(
                "next_action",
                allowed_actions,
                ACTION_CRITERIA,
                "只选择当前允许的唯一下一步；不要预测完整调用链。",
            ),
            timeout_value,
        )
        try:
            action_result = _parse_choice_response(action_payload, "next_action", allowed_actions)
        except _ProtocolError as error:
            return _skip_result(decision_id, state_hash, error.reason, error.detail, started_at)

        action = action_result["choice"]
        if action not in ("implement", "validate"):
            return _observed_result(
                decision_id,
                state_hash,
                action,
                None,
                action_result["probabilities"],
                None,
                action_result,
                None,
                started_at,
            )

        # The second decision keeps all caller-provided constraints and adds
        # only the first decision.  It cannot broaden the allowed surfaces.
        surface_state = dict(validated_state)
        surface_state["next_action"] = action
        try:
            surface_payload = _post_decision(
                base_url,
                surface_state,
                _question(
                    "surface",
                    allowed_surfaces,
                    SURFACE_CRITERIA,
                    "只选择当前允许的前端、后端或前后端范围。",
                ),
                timeout_value,
            )
        except _ClientError as error:
            return _skip_result(decision_id, state_hash, "second_step_failed", error.detail, started_at)
        try:
            surface_result = _parse_choice_response(surface_payload, "surface", allowed_surfaces)
        except _ProtocolError as error:
            return _skip_result(decision_id, state_hash, "second_step_failed", error.detail, started_at)
        return _observed_result(
            decision_id,
            state_hash,
            action,
            surface_result["choice"],
            action_result["probabilities"],
            surface_result["probabilities"],
            action_result,
            surface_result,
            started_at,
        )
    except _ClientError as error:
        return _skip_result(decision_id, state_hash, error.reason, error.detail, started_at)
    except Exception:
        # The shadow path must remain non-blocking even when a transport or
        # unexpected input implementation raises an unfamiliar exception.
        return _skip_result(decision_id, state_hash, "client_error", "shadow client failed safely", started_at)


def _log_record(result: dict[str, Any], state: Any, actual_action: str | None) -> dict[str, Any]:
    suggestion = {
        "action": result.get("suggested_action"),
        "surface": result.get("surface"),
        "candidate_roles": result.get("candidate_roles", []),
    }
    return {
        "decision_id": result.get("decision_id"),
        "state_hash": _state_hash(state),
        "suggestion": suggestion,
        "actual_action": actual_action,
        "probabilities": result.get("probabilities"),
        "skip_reason": result.get("skip_reason"),
        "status": result.get("status"),
        "elapsed_ms": result.get("elapsed_ms"),
        "service": result.get("service"),
    }


def _append_log(path: str, result: dict[str, Any], state: Any, actual_action: str | None) -> bool:
    try:
        record = _log_record(result, state, actual_action)
        with Path(path).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception:
        return False
    return True


def _input_error_result(detail: str) -> dict[str, Any]:
    started_at = time.perf_counter()
    return _skip_result(_decision_id(), None, "invalid_input", detail, started_at)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    # Parse this manually so a malformed CLI value is still represented as a
    # JSON skipped result with exit code 0, just like malformed stdin.
    parser.add_argument("--timeout", default=str(DEFAULT_TIMEOUT))
    parser.add_argument("--log")
    parser.add_argument("--actual-action")
    args = parser.parse_args(argv)

    try:
        timeout = float(args.timeout)
    except (TypeError, ValueError):
        timeout = math.nan

    try:
        raw_input = sys.stdin.read()
        state = json.loads(raw_input)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        state = None
        result = _input_error_result("stdin is not valid JSON")
    else:
        result = suggest_next_action(state, base_url=args.base_url, timeout=timeout)
    if args.actual_action is not None:
        result["actual_action"] = args.actual_action
    if args.log and not _append_log(args.log, result, state, args.actual_action):
        result["log_issue"] = "write_failed"
    try:
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    except (OSError, UnicodeEncodeError):
        # stdout errors cannot be repaired here, but the CLI still must not
        # turn a Laya failure into a non-zero task failure.
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
