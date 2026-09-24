"""Loopback API for the Laya-directed 2D snake game."""

from __future__ import annotations

import argparse
import json
import os
import re
import threading
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict

try:
    from .engine import (
        GameFinishedError,
        MAX_DECISIONS,
        LEVEL_CONFIGS,
        ModelDecisionError,
        ModelUnavailableError,
        SnakeGame,
    )
except ImportError:  # Running ``python app.py`` from this directory.
    from engine import GameFinishedError, LEVEL_CONFIGS, MAX_DECISIONS, ModelDecisionError, ModelUnavailableError, SnakeGame

try:
    from .jev_client import JevClient
except ImportError:  # Running ``python app.py`` from this directory.
    from jev_client import JevClient

try:
    from .kev_client import KevClient
except ImportError:  # Running ``python app.py`` from this directory.
    from kev_client import KevClient


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8766
DEFAULT_LAYA_URL = "http://127.0.0.1:8765"
REQUEST_TIMEOUT_SECONDS = 25.0
MAX_ACTIVE_GAMES = 32
LAYA_URL = (os.environ.get("SNAKE_LAYA_URL") or os.environ.get("LAYA_SNAKE_LAYA_URL") or DEFAULT_LAYA_URL).rstrip("/")
WEB_DIR = Path(__file__).resolve().parent / "web"
GAME_STORAGE_ROOT = Path(__file__).resolve().parent / "runtime" / "games"
GAME_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


class NewGameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: Literal["laya", "jev", "kev"] = "laya"
    seed: int | None = None
    policyMode: str = "survival"


class StepRequest(BaseModel):
    """Optional optimistic step number for reconnect-safe clients."""

    model_config = ConfigDict(extra="forbid")

    expectedStep: int | None = None


class LayaClient:
    """Small stdlib-only client for the existing loopback Laya service."""

    def __init__(self, base_url: str = LAYA_URL, timeout: float = REQUEST_TIMEOUT_SECONDS) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    @staticmethod
    def _decode(body: bytes) -> dict[str, Any]:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ModelUnavailableError("Laya 返回的内容不是有效 JSON") from error
        if not isinstance(payload, dict):
            raise ModelUnavailableError("Laya 返回的 JSON 顶层不是对象")
        return payload

    @staticmethod
    def _error_text(error: BaseException) -> str:
        message = " ".join(str(error).split())
        return message[:240] if message else type(error).__name__

    def health(self) -> tuple[int, dict[str, Any]]:
        request = urllib.request.Request(
            f"{self.base_url}/health",
            headers={"Accept": "application/json"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, self._decode(response.read())
        except urllib.error.HTTPError as error:
            try:
                payload = self._decode(error.read())
            except ModelUnavailableError:
                payload = {"status": "error", "error": self._error_text(error)}
            return error.code, payload
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            return 503, {
                "status": "unreachable",
                "error": f"无法连接 Laya：{self._error_text(error)}",
            }

    def decide(self, state: str | dict[str, Any], questions: dict[str, dict[str, Any]]) -> dict[str, Any]:
        body = json.dumps(
            {"state": state, "questions": questions},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/v1/decide",
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status = response.status
                payload = self._decode(response.read())
        except urllib.error.HTTPError as error:
            detail = self._error_text(error)
            try:
                error_payload = self._decode(error.read())
                detail = json.dumps(error_payload, ensure_ascii=False)[:240]
            except ModelUnavailableError:
                pass
            raise ModelUnavailableError(f"Laya HTTP {error.code}：{detail}") from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise ModelUnavailableError(f"无法连接 Laya：{self._error_text(error)}") from error

        if status < 200 or status >= 300:
            raise ModelUnavailableError(f"Laya HTTP {status}")
        return payload


@dataclass
class GameSession:
    game: SnakeGame
    step_lock: threading.Lock
    storage_dir: Path
    metadata_path: Path
    decisions_path: Path
    persistence_warnings: list[str]
    model: Literal["laya", "jev", "kev"] = "laya"


app = FastAPI(title="Laya Live 2D Snake", version="0.1.0")
laya_client = LayaClient()
jev_client = JevClient()
kev_client = KevClient()
games: dict[str, GameSession] = {}
games_lock = threading.Lock()
model_lock = threading.Lock()


def _storage_paths(game_id: str) -> tuple[Path, Path, Path]:
    """Resolve only service-generated UUID names under the fixed game root."""

    if not GAME_ID_PATTERN.fullmatch(game_id):
        raise ValueError("无效的游戏 ID")
    storage_dir = Path(GAME_STORAGE_ROOT) / game_id
    return storage_dir, storage_dir / "metadata.json", storage_dir / "decisions.jsonl"


def _exception_warning(prefix: str, error: BaseException) -> str:
    message = " ".join(str(error).split())
    if len(message) > 240:
        message = f"{message[:237]}..."
    return f"{prefix}：{type(error).__name__}{f'：{message}' if message else ''}"


def _level_metadata() -> list[dict[str, Any]]:
    return [
        {
            "level": level,
            "name": config.name,
            "size": config.size,
            "initialLength": config.initial_length,
            "target": config.target,
            "intervalMs": config.interval_ms,
        }
        for level, config in enumerate(LEVEL_CONFIGS, start=1)
    ]


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _create_storage(
    game_id: str,
    game: SnakeGame,
    created_at: str,
    model: Literal["laya", "jev", "kev"] | None = None,
) -> tuple[Path, Path, Path, list[str]]:
    storage_dir, metadata_path, decisions_path = _storage_paths(game_id)
    warnings: list[str] = []
    selected_model = model or getattr(game, "model_provider", "laya")
    try:
        storage_dir.mkdir(parents=True, exist_ok=False)
        _write_json(
            metadata_path,
            {
                "id": game_id,
                "seed": game.seed,
                "policyMode": game.policy_mode,
                "model": selected_model,
                "createdAt": created_at,
                "totalLevels": len(LEVEL_CONFIGS),
                "levels": _level_metadata(),
            },
        )
    except Exception as error:
        warnings.append(_exception_warning("游戏持久化初始化失败，已保留内存游戏", error))
    return storage_dir, metadata_path, decisions_path, warnings


def _append_decision(session: GameSession, decision: dict[str, Any]) -> None:
    """Append exactly once after a successful engine step; never rerun inference."""

    try:
        session.decisions_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(decision, ensure_ascii=False, separators=(",", ":"))
        with session.decisions_path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception as error:
        warning = _exception_warning("决策已执行但持久化日志写入失败", error)
        if warning not in session.persistence_warnings:
            session.persistence_warnings.append(warning)


def _read_decisions(decisions_path: Path) -> tuple[list[dict[str, Any]], str | None]:
    if not decisions_path.is_file():
        return [], None
    logs: list[dict[str, Any]] = []
    try:
        with decisions_path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError(f"第 {line_number} 行不是对象")
                logs.append(value)
    except Exception as error:
        return logs, _exception_warning("持久化日志读取不完整", error)
    return logs, None


def _persistence_info(session: GameSession) -> dict[str, Any]:
    warning = "；".join(session.persistence_warnings) if session.persistence_warnings else None
    return {
        "ok": warning is None,
        "warning": warning,
        "directory": str(session.storage_dir.resolve()),
        "metadataPath": str(session.metadata_path.resolve()),
        "decisionsPath": str(session.decisions_path.resolve()),
        "diskLogs": session.decisions_path.is_file(),
    }


def _warning_response(payload: dict[str, Any], session: GameSession) -> dict[str, Any]:
    info = _persistence_info(session)
    payload["persistence"] = info
    if info["warning"]:
        payload["warning"] = info["warning"]
    return payload


def _session_or_404(game_id: str) -> GameSession:
    with games_lock:
        session = games.get(game_id)
    if session is None:
        raise HTTPException(status_code=404, detail={"code": "game_not_found", "message": "游戏不存在"})
    return session


def _error(code: str, message: str, status_code: int) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


@app.get("/api/health")
def api_health(model: Literal["laya", "jev", "kev"] = "laya") -> JSONResponse:
    if model == "jev":
        status_code, payload = jev_client.health()
    elif model == "kev":
        status_code, payload = kev_client.health()
    else:
        status_code, payload = laya_client.health()
    return JSONResponse(status_code=status_code, content=payload)


@app.post("/api/game")
def create_game(payload: NewGameRequest | None = None) -> dict[str, Any]:
    with games_lock:
        if len(games) >= MAX_ACTIVE_GAMES:
            raise _error("too_many_games", "活动游戏数量已达到上限，请复用已有游戏", 429)
        game_id = uuid.uuid4().hex
        model = payload.model if payload is not None else "laya"
        if model == "jev" and not jev_client.is_configured():
            raise _error("jev_unavailable", "Jev 未配置：请设置进程环境变量 TYPESAFE_API_KEY", 503)
        if model == "kev":
            health_status, health_payload = kev_client.health()
            if health_status != 200:
                raise _error("kev_unavailable", str(health_payload.get("error", "Kev 本地服务未就绪")), 503)
        seed = payload.seed if payload is not None else None
        policy_mode = payload.policyMode if payload is not None else "survival"
        if policy_mode not in {"survival", "strict_shortest"}:
            raise _error("invalid_policy_mode", "policyMode 必须是 survival 或 strict_shortest", 422)
        game = SnakeGame(seed=seed, policy_mode=policy_mode, model_provider=model,
                         skip_deterministic_model=model != "laya")
        created_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        storage_dir, metadata_path, decisions_path, warnings = _create_storage(game_id, game, created_at, model)
        games[game_id] = GameSession(
            game=game,
            step_lock=threading.Lock(),
            storage_dir=storage_dir,
            metadata_path=metadata_path,
            decisions_path=decisions_path,
            persistence_warnings=warnings,
            model=model,
        )
        session = games[game_id]
    return _warning_response({"id": game_id, "state": session.game.state()}, session)


@app.get("/api/game/{game_id}")
def get_game(game_id: str) -> dict[str, Any]:
    session = _session_or_404(game_id)
    with session.step_lock:
        state = session.game.state()
    return {"id": game_id, "state": state}


@app.delete("/api/game/{game_id}")
def delete_game(game_id: str) -> dict[str, Any]:
    session = _session_or_404(game_id)
    if not session.step_lock.acquire(blocking=False):
        raise _error("step_in_progress", "该游戏正在等待模型决策，暂时不能删除", 409)
    try:
        with games_lock:
            if games.get(game_id) is session:
                del games[game_id]
        return {"id": game_id, "deleted": True, "persistence": _persistence_info(session)}
    finally:
        session.step_lock.release()


@app.post("/api/game/{game_id}/step")
def step_game(game_id: str, payload: StepRequest | None = None) -> dict[str, Any]:
    session = _session_or_404(game_id)
    if not session.step_lock.acquire(blocking=False):
        raise _error("step_in_progress", "该游戏正在等待上一轮模型决策", 409)
    try:
        current_step = session.game.steps
        if payload is not None and payload.expectedStep is not None and payload.expectedStep != current_step:
            raise _error(
                "step_conflict",
                f"客户端期望 step={payload.expectedStep}，当前为 {current_step}",
                409,
            )
        if session.model == "jev" and not jev_client.is_configured():
            raise _error("jev_unavailable", "Jev 未配置：请设置进程环境变量 TYPESAFE_API_KEY", 503)
        # A provider is shared by all local games. Serializing requests keeps
        # duplicate clicks and cross-session inference from creating unbounded
        # local model work while preserving one real provider call per step.
        client = (
            jev_client
            if session.model == "jev"
            else kev_client
            if session.model == "kev"
            else laya_client
        )
        with model_lock:
            result = session.game.step(client.decide)
        _append_decision(session, result["decision"])
        return _warning_response({"state": result["state"], "decision": result["decision"]}, session)
    except HTTPException:
        raise
    except GameFinishedError as error:
        raise _error("game_finished", str(error), 409) from error
    except ModelUnavailableError as error:
        # No mutation or log append occurs in SnakeGame.step before a model
        # response has been parsed, so the frontend can safely pause and retry.
        raise _error(f"{session.model}_unavailable", str(error), 503) from error
    except ModelDecisionError as error:
        raise _error("invalid_model_decision", str(error), 502) from error
    finally:
        session.step_lock.release()


@app.get("/api/game/{game_id}/logs")
def game_logs(game_id: str) -> dict[str, Any]:
    with games_lock:
        session = games.get(game_id)
    if session is not None:
        with session.step_lock:
            if session.decisions_path.is_file():
                logs, read_warning = _read_decisions(session.decisions_path)
                metadata = {"count": len(logs), "truncated": False, "truncatedCount": 0, "max": MAX_DECISIONS}
            else:
                logs = session.game.logs()
                read_warning = None
                metadata = session.game.logs_metadata()
            info = _persistence_info(session)
            warnings = [item for item in (info["warning"], read_warning) if item]
            info["ok"] = not warnings
            info["warning"] = "；".join(warnings) if warnings else None
        payload = {"id": game_id, "logs": logs, **metadata, "persistence": info}
        if info["warning"]:
            payload["warning"] = info["warning"]
        return payload

    try:
        storage_dir, metadata_path, decisions_path = _storage_paths(game_id)
    except ValueError as error:
        raise _error("game_not_found", "游戏不存在", 404) from error
    if not storage_dir.is_dir():
        raise _error("game_not_found", "游戏不存在", 404)
    logs, read_warning = _read_decisions(decisions_path)
    warnings = []
    if not metadata_path.is_file():
        warnings.append("历史游戏缺少 metadata.json")
    if read_warning:
        warnings.append(read_warning)
    info = {
        "ok": not warnings,
        "warning": "；".join(warnings) if warnings else None,
        "directory": str(storage_dir.resolve()),
        "metadataPath": str(metadata_path.resolve()),
        "decisionsPath": str(decisions_path.resolve()),
        "diskLogs": decisions_path.is_file(),
    }
    payload = {
        "id": game_id,
        "logs": logs,
        "count": len(logs),
        "truncated": False,
        "truncatedCount": 0,
        "max": MAX_DECISIONS,
        "persistence": info,
    }
    if info["warning"]:
        payload["warning"] = info["warning"]
    return payload


if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")


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
