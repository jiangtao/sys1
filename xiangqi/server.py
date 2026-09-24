"""Loopback HTTP service for the Laya-versus-human Xiangqi game."""

from __future__ import annotations

import argparse
import json
import logging
import math
import mimetypes
import os
import posixpath
import tempfile
import threading
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

try:
    from .engine import (
        DEFAULT_THINK_SECONDS,
        EngineError,
        EngineUnavailable,
        Game,
        GameRuleError,
        LayaClient,
        LayaUnavailable,
        PikafishRuntime,
        PROVIDER_LABELS,
        SUPPORTED_MODELS,
        append_human_move,
        append_laya_move,
        choose_laya_move,
        load_game,
        load_puzzles,
        make_game,
    )
    from .jev_client import JevClient, JevUnavailable
    from .kev_client import KevClient, KevUnavailable
except ImportError:
    from engine import (
        DEFAULT_THINK_SECONDS,
        EngineError,
        EngineUnavailable,
        Game,
        GameRuleError,
        LayaClient,
        LayaUnavailable,
        PikafishRuntime,
        PROVIDER_LABELS,
        SUPPORTED_MODELS,
        append_human_move,
        append_laya_move,
        choose_laya_move,
        load_game,
        load_puzzles,
        make_game,
    )
    from jev_client import JevClient, JevUnavailable
    from kev_client import KevClient, KevUnavailable


LOGGER = logging.getLogger("laya-xiangqi")
MAX_ACTIVE_GAMES = 128
DEPLOY_DIR = Path(__file__).resolve().parent
WEB_DIR = DEPLOY_DIR / "web"
RUNTIME_DIR = DEPLOY_DIR / "runtime"
GAMES_STORE = RUNTIME_DIR / "games.json"
LEGACY_GAMES_STORE = RUNTIME_DIR / "pre-selfplay-games.json"
GAMES_FORMAT = 1
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8768
STANDARD_PUZZLE = {
    "id": "standard",
    "title": "标准对局",
    "subtitle": "完整棋盘，红先黑后",
    "difficulty": "自由对弈",
    "fen": "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR w - - 0 1",
}


class ApiError(RuntimeError):
    def __init__(self, status: int, code: str, message: str, details: Any | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


class ServiceState:
    def __init__(self, store_path: Path | None = None) -> None:
        self.runtime = PikafishRuntime()
        self.laya = LayaClient()
        self.jev = JevClient()
        self.kev = KevClient()
        self.puzzles = load_puzzles()
        self.puzzle_map = {item["id"]: item for item in self.puzzles}
        self.games: dict[str, Game] = {}
        self.games_lock = threading.RLock()
        self.initialized = False
        self.port = DEFAULT_PORT
        self.store_path = store_path or GAMES_STORE
        self.legacy_store_path = LEGACY_GAMES_STORE if store_path is None else None
        self.store_lock = threading.RLock()
        self.game_snapshots: dict[str, dict[str, Any]] = {}
        self._load_persisted_games()

    def initialize(self, download: bool = True) -> None:
        try:
            self.runtime.initialize(download=download)
        except EngineError as error:
            LOGGER.error("Pikafish initialization failed: %s", error)
        self.initialized = True

    def health(self) -> tuple[int, dict[str, Any]]:
        engine_payload = self.runtime.health()
        laya_status, laya_payload = self.laya.health()
        jev_status, jev_payload = self.jev.health()
        kev_status, kev_payload = self.kev.health()
        engine_ready = engine_payload.get("status") == "ready"
        laya_ready = laya_status == 200 and laya_payload.get("status") == "ready"
        jev_ready = jev_status == 200 and jev_payload.get("status") == "ready"
        kev_ready = kev_status == 200 and kev_payload.get("status") == "ready"
        if not engine_ready:
            status = "error"
            http_status = 503
        # Laya remains the default provider; an unavailable optional Jev
        # dependency must not make the existing game appear offline.
        elif laya_ready:
            status = "ready"
            http_status = 200
        elif jev_ready or kev_ready:
            status = "degraded"
            http_status = 200
        else:
            status = "degraded"
            http_status = 200
        return http_status, {
            "status": status,
            "engine": engine_payload,
            "laya": laya_payload,
            "layaHttpStatus": laya_status,
            "jev": jev_payload,
            "jevHttpStatus": jev_status,
            "kev": kev_payload,
            "kevHttpStatus": kev_status,
            "host": DEFAULT_HOST,
            "port": self.port,
        }

    def provider(self, model: str) -> LayaClient | JevClient | KevClient:
        providers = {"laya": self.laya, "jev": self.jev, "kev": self.kev}
        provider = providers.get(model)
        if provider is None:
            raise ApiError(400, "invalid_model", "model 必须是 laya、jev 或 kev")
        return provider

    def game_provider_model(self, game: Game) -> str:
        """Return the configured provider for the side whose turn it is."""

        if game.mode == "duel" and game.turn == "black":
            return game.opponent_model or ""
        return game.model

    def close(self) -> None:
        self.jev.close()
        self.kev.close()
        self.runtime.stop()

    def get_game(self, game_id: str) -> Game:
        with self.games_lock:
            game = self.games.get(game_id)
            if game is not None:
                # dict insertion order is the small in-process LRU.  A client
                # reconnecting to a game should keep it ahead of stale games.
                self.games.pop(game_id)
                self.games[game_id] = game
        if game is None:
            raise ApiError(404, "game_not_found", "棋局不存在或已过期")
        return game

    def _load_persisted_games(self) -> None:
        source_path = self.store_path
        if not source_path.is_file() and self.legacy_store_path is not None and self.legacy_store_path.is_file():
            source_path = self.legacy_store_path
        try:
            payload = json.loads(source_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as error:
            LOGGER.warning("棋局存档读取失败，忽略存档：%s", error)
            return
        if isinstance(payload, dict) and payload.get("format") == GAMES_FORMAT:
            snapshots = payload.get("games", [])
        elif isinstance(payload, list):
            # Accept the migration file emitted by the previous in-memory
            # service and rewrite it in the canonical archive format.
            snapshots = payload
        else:
            LOGGER.warning("棋局存档格式无效，忽略存档")
            return
        if not isinstance(snapshots, list):
            LOGGER.warning("棋局存档 games 必须是数组，忽略存档")
            return
        needs_rewrite = source_path != self.store_path
        for snapshot in snapshots[-MAX_ACTIVE_GAMES:]:
            try:
                game = load_game(snapshot)
            except (GameRuleError, TypeError, ValueError) as error:
                LOGGER.warning("跳过无效棋局存档：%s", error)
                continue
            if game.id not in self.games:
                self.games[game.id] = game
                self.game_snapshots[game.id] = game.to_persist_dict()
                rounds = snapshot.get("rounds") if isinstance(snapshot, dict) else None
                if isinstance(rounds, list) and any(isinstance(item, dict) and ("moves" in item or "logs" in item) for item in rounds):
                    needs_rewrite = True
        if needs_rewrite and self.games:
            self.persist_games()

    def persist_games(self, current_game: Game | None = None) -> None:
        """Atomically persist recent games without holding a game lock during IO."""

        current_payload: dict[str, Any] | None = None
        if current_game is not None:
            with current_game.lock:
                current_payload = current_game.to_persist_dict()
        with self.store_lock:
            if current_game is not None and current_payload is not None:
                self.game_snapshots[current_game.id] = current_payload
            with self.games_lock:
                game_ids = list(self.games)[-MAX_ACTIVE_GAMES:]
            snapshots = [self.game_snapshots[game_id] for game_id in game_ids if game_id in self.game_snapshots]
            self._write_game_snapshots(snapshots)

    def _write_game_snapshots(self, snapshots: list[dict[str, Any]]) -> None:
        payload = {"format": GAMES_FORMAT, "games": snapshots}
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=".games-", suffix=".tmp", dir=str(self.store_path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            Path(temporary_name).replace(self.store_path)
        except Exception:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def _evict_if_needed(self) -> None:
        while True:
            with self.games_lock:
                if len(self.games) < MAX_ACTIVE_GAMES:
                    return
                candidates = list(self.games.items())
            for old_id, old_game in candidates:
                if not old_game.lock.acquire(blocking=False):
                    continue
                removed = False
                try:
                    with self.games_lock:
                        if self.games.get(old_id) is old_game:
                            self.games.pop(old_id, None)
                            removed = True
                finally:
                    old_game.lock.release()
                if removed:
                    with self.store_lock:
                        self.game_snapshots.pop(old_id, None)
                    return
            raise ApiError(429, "too_many_games", "活动棋局正在处理，暂时无法创建更多棋局")

    def create_game(
        self,
        puzzle_id: str,
        mode: str = "human",
        model: str = "laya",
        opponent_model: str | None = None,
    ) -> Game:
        if mode not in {"human", "selfplay", "duel"}:
            raise ApiError(400, "invalid_mode", "mode 必须是 human、selfplay 或 duel")
        if model not in SUPPORTED_MODELS:
            raise ApiError(400, "invalid_model", "model 必须是 laya、jev 或 kev")
        if mode == "duel":
            if opponent_model not in SUPPORTED_MODELS or opponent_model == model:
                raise ApiError(400, "invalid_opponent_model", "duel 模式必须使用两个不同的模型分别执红、执黑")
        elif opponent_model is not None:
            raise ApiError(400, "invalid_opponent_model", "opponentModel 仅可用于 duel 模式")
        if puzzle_id == "standard":
            puzzle = STANDARD_PUZZLE
        else:
            puzzle = self.puzzle_map.get(puzzle_id)
            if puzzle is None:
                raise ApiError(404, "puzzle_not_found", f"残局不存在：{puzzle_id}")
        required_models = [model]
        if mode == "duel" and opponent_model is not None:
            required_models.append(opponent_model)
        for required_model in required_models:
            if required_model not in {"jev", "kev"}:
                continue
            provider_status, provider_payload = self.provider(required_model).health()
            if provider_status != 200 or provider_payload.get("status") != "ready":
                label = PROVIDER_LABELS[required_model]
                detail = provider_payload.get("error") or provider_payload.get("status") or f"{label} 不可用"
                raise ApiError(503, f"{required_model}_unavailable", f"{label} 当前不可用：{detail}", {"provider": required_model})
        try:
            game = make_game(puzzle, self.runtime, mode=mode, model=model, opponent_model=opponent_model)
        except EngineUnavailable as error:
            raise ApiError(503, "engine_unavailable", f"Pikafish 当前不可用：{error}") from error
        except (EngineError, GameRuleError) as error:
            raise ApiError(422, "invalid_position", str(error)) from error
        self._evict_if_needed()
        with self.games_lock:
            self.games[game.id] = game
        self.persist_games(game)
        return game


STATE = ServiceState()


def error_body(error: ApiError) -> dict[str, Any]:
    payload: dict[str, Any] = {"error": {"code": error.code, "message": error.message}}
    if error.details is not None:
        payload["error"]["details"] = error.details
    return payload


def expect_object(payload: Any) -> dict[str, Any]:
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise ApiError(400, "invalid_request", "JSON 请求体必须是对象")
    return payload


def expected_version(payload: dict[str, Any], game: Game) -> None:
    if "version" not in payload and "expectedVersion" not in payload:
        return
    raw = payload.get("version", payload.get("expectedVersion"))
    try:
        expected = int(raw)
    except (TypeError, ValueError) as error:
        raise ApiError(400, "invalid_version", "version 必须是整数") from error
    if expected != game.version:
        raise ApiError(409, "stale_game", "棋局版本已变化，请刷新后重试", {"version": game.version, "game": game.to_dict()})


def parse_seconds(payload: dict[str, Any]) -> float:
    value = payload.get("seconds", DEFAULT_THINK_SECONDS)
    if isinstance(value, bool):
        raise ApiError(400, "invalid_seconds", "seconds 必须是有限数字")
    try:
        seconds = float(value)
    except (TypeError, ValueError) as error:
        raise ApiError(400, "invalid_seconds", "seconds 必须是数字") from error
    if not math.isfinite(seconds):
        raise ApiError(400, "invalid_seconds", "seconds 必须是有限数字")
    if seconds < 0.1 or seconds > 30:
        raise ApiError(400, "invalid_seconds", "seconds 必须在 0.1 到 30 之间")
    return seconds


def record_model_failure(game: Game, error: BaseException, provider: str) -> None:
    provider = provider if provider in SUPPORTED_MODELS else "laya"
    provider_label = PROVIDER_LABELS[provider]
    side_label = "红方" if game.turn == "red" else "黑方"
    game.logs.append(
        {
            "id": uuid.uuid4().hex,
            "side": game.turn,
            "move": None,
            "label": f"{provider_label} 决策失败",
            "kind": provider,
            "agent": f"{provider_label} · {side_label}",
            "provider": provider,
            "model": provider,
            "scorePerspective": game.turn,
            "summary": f"真实 {provider_label} 服务未返回{side_label}决策；没有用引擎最佳着法静默替代，可重试当前回合。",
            "raw": {"provider": provider, provider: {"error": str(error)}, "error": str(error)},
        }
    )
    game.version += 1


def record_laya_failure(game: Game, error: BaseException) -> None:
    """Backward-compatible wrapper for existing callers and snapshots."""

    record_model_failure(game, error, "laya")


def _engine_diagnostics(error: BaseException) -> dict[str, Any]:
    details = getattr(error, "details", None)
    if isinstance(details, dict):
        return details
    cause = getattr(error, "__cause__", None)
    cause_details = getattr(cause, "details", None)
    return cause_details if isinstance(cause_details, dict) else {}


def record_engine_failure(game: Game, error: EngineError) -> None:
    """Keep native-engine diagnostics with the game and server log."""

    log_id = uuid.uuid4().hex
    diagnostics = _engine_diagnostics(error)
    LOGGER.error(
        "Pikafish engine failure game=%s logID=%s error=%s diagnostics=%s",
        game.id,
        log_id,
        error,
        diagnostics,
        exc_info=(type(error), error, error.__traceback__),
    )
    side_label = "红方" if game.turn == "red" else "黑方"
    game.logs.append(
        {
            "id": log_id,
            "side": game.turn,
            "move": None,
            "label": "Pikafish 引擎失败",
            "kind": "system",
            "agent": "Pikafish",
            "scorePerspective": game.turn,
            "summary": f"Pikafish 未完成{side_label}本次操作；棋局已回滚，可重试当前回合。",
            "raw": {"error": str(error), "diagnostics": diagnostics},
        }
    )
    game.version += 1


class XiangqiHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _headers(self, content_type: str, length: int) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, If-Match")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Connection", "close")

    def send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self._headers("application/json; charset=utf-8", len(body))
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def send_error_payload(self, error: ApiError) -> None:
        payload = error_body(error)
        self.send_json(error.status, payload)

    def read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ApiError(400, "invalid_request", "Content-Length 无效") from error
        if length > 2 * 1024 * 1024:
            raise ApiError(413, "request_too_large", "请求体超过 2 MB")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ApiError(400, "invalid_json", "请求体不是有效 JSON") from error
        return expect_object(payload)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._headers("text/plain; charset=utf-8", 0)
        self.end_headers()

    def do_GET(self) -> None:
        try:
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path == "/api/health":
                status, payload = STATE.health()
                self.send_json(status, payload)
                return
            if parsed.path == "/api/puzzles":
                self.send_json(200, {"puzzles": STATE.puzzles})
                return
            if parsed.path == "/api/game":
                query = urllib.parse.parse_qs(parsed.query)
                game_id = (query.get("id") or [""])[0]
                if not game_id:
                    raise ApiError(400, "missing_game_id", "需要 query 参数 id")
                game = STATE.get_game(game_id)
                with game.lock:
                    self.send_json(200, game.to_dict())
                return
            self.serve_static(parsed.path)
        except ApiError as error:
            self.send_error_payload(error)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as error:
            LOGGER.exception("GET request failed")
            self.send_error_payload(ApiError(500, "internal_error", f"服务内部错误：{type(error).__name__}"))

    def do_POST(self) -> None:
        try:
            parsed = urllib.parse.urlsplit(self.path)
            payload = self.read_json()
            if parsed.path == "/api/game":
                puzzle_id = payload.get("puzzle", "standard")
                if not isinstance(puzzle_id, str):
                    raise ApiError(400, "invalid_puzzle", "puzzle 必须是字符串")
                mode = payload.get("mode", "human")
                if mode not in {"human", "selfplay", "duel"}:
                    raise ApiError(400, "invalid_mode", "mode 必须是 human、selfplay 或 duel")
                model = payload.get("model", "laya")
                if model not in SUPPORTED_MODELS:
                    raise ApiError(400, "invalid_model", "model 必须是 laya、jev 或 kev")
                opponent_model = payload.get("opponentModel")
                if opponent_model is not None and opponent_model not in SUPPORTED_MODELS:
                    raise ApiError(400, "invalid_opponent_model", "opponentModel 必须是 laya、jev 或 kev")
                game = STATE.create_game(puzzle_id, mode=mode, model=model, opponent_model=opponent_model)
                with game.lock:
                    self.send_json(200, game.to_dict())
                return
            if parsed.path in {"/api/move", "/api/think", "/api/undo"}:
                self.handle_game_action(parsed.path, payload)
                return
            raise ApiError(404, "not_found", "接口不存在")
        except ApiError as error:
            self.send_error_payload(error)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as error:
            LOGGER.exception("POST request failed")
            self.send_error_payload(ApiError(500, "internal_error", f"服务内部错误：{type(error).__name__}"))

    def handle_game_action(self, path: str, payload: dict[str, Any]) -> None:
        game_id = payload.get("gameId")
        if not isinstance(game_id, str) or not game_id:
            raise ApiError(400, "missing_game_id", "需要 gameId")
        game = STATE.get_game(game_id)
        with game.lock:
            expected_version(payload, game)
            if path == "/api/move":
                if game.mode in {"selfplay", "duel"}:
                    raise ApiError(409, "manual_move_forbidden", "模型自对弈模式由双方模型轮流决策，不能手动落子", {"game": game.to_dict()})
                move = payload.get("move")
                if not isinstance(move, str):
                    raise ApiError(400, "missing_move", "需要 move，例如 a0a1")
                try:
                    append_human_move(game, move, STATE.runtime)
                except GameRuleError as error:
                    raise ApiError(409, "illegal_move", str(error), {"legalMoves": game.legal_moves}) from error
                except EngineError as error:
                    record_engine_failure(game, error)
                    STATE.persist_games(game)
                    raise ApiError(503, "engine_unavailable", f"Pikafish 无法验证走法：{error}", {"game": game.to_dict(), "retryable": True}) from error
                STATE.persist_games(game)
                self.send_json(200, game.to_dict())
                return
            if path == "/api/undo":
                try:
                    game.undo_round(STATE.runtime)
                except GameRuleError as error:
                    raise ApiError(409, "cannot_undo", str(error)) from error
                except EngineError as error:
                    record_engine_failure(game, error)
                    STATE.persist_games(game)
                    raise ApiError(503, "engine_failed", f"Pikafish 无法刷新棋局：{error}", {"game": game.to_dict(), "retryable": True}) from error
                STATE.persist_games(game)
                self.send_json(200, game.to_dict())
                return
            if game.status != "playing":
                raise ApiError(409, "game_finished", game.reason or "对局已经结束", {"game": game.to_dict()})
            if game.mode == "human" and game.turn != "black":
                raise ApiError(409, "wrong_turn", "当前等待红方落子", {"game": game.to_dict()})
            seconds = parse_seconds(payload)
            try:
                provider_model = STATE.game_provider_model(game)
                decision = choose_laya_move(
                    STATE.runtime,
                    STATE.provider(provider_model),
                    game.fen,
                    seconds=seconds,
                    initial_fen=str(game.puzzle["fen"]),
                    moves=[item.get("move") for item in game.moves],
                )
                append_laya_move(game, decision, STATE.runtime)
                STATE.persist_games(game)
            except (LayaUnavailable, JevUnavailable, KevUnavailable) as error:
                provider = STATE.game_provider_model(game)
                record_model_failure(game, error, provider)
                STATE.persist_games(game)
                provider_label = PROVIDER_LABELS.get(provider, "模型")
                raise ApiError(
                    503,
                    f"{provider}_unavailable",
                    f"{provider_label} 决策失败：{error}",
                    {"game": game.to_dict(), "retryable": True, "provider": provider},
                ) from error
            except GameRuleError as error:
                STATE.persist_games(game)
                raise ApiError(409, "history_mismatch", str(error), {"game": game.to_dict()}) from error
            except EngineUnavailable as error:
                record_engine_failure(game, error)
                STATE.persist_games(game)
                raise ApiError(503, "engine_unavailable", f"Pikafish 当前不可用：{error}", {"game": game.to_dict(), "retryable": True}) from error
            except EngineError as error:
                record_engine_failure(game, error)
                STATE.persist_games(game)
                raise ApiError(503, "engine_failed", f"Pikafish 搜索失败：{error}", {"game": game.to_dict(), "retryable": True}) from error
            self.send_json(200, game.to_dict())

    def serve_static(self, requested_path: str) -> None:
        clean = posixpath.normpath(urllib.parse.unquote(requested_path or "/"))
        relative = "index.html" if clean in {"", "/", "."} else clean.lstrip("/")
        if relative.startswith("../") or relative == ".." or "\\" in relative:
            raise ApiError(404, "not_found", "资源不存在")
        target = (WEB_DIR / relative).resolve()
        try:
            target.relative_to(WEB_DIR.resolve())
        except ValueError as error:
            raise ApiError(404, "not_found", "资源不存在") from error
        if not target.is_file():
            raise ApiError(404, "not_found", "资源不存在")
        body = target.read_bytes()
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self.send_response(200)
        self._headers(f"{content_type}; charset=utf-8" if content_type.startswith("text/") or content_type in {"application/javascript", "text/javascript"} else content_type, len(body))
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def log_message(self, format_string: str, *args: Any) -> None:
        LOGGER.info("%s - %s", self.address_string(), format_string % args)


class XiangqiHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-download", action="store_true", help="缺少引擎时不访问 GitHub")
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("服务只允许绑定本机回环地址")
    if not 1 <= args.port <= 65535:
        parser.error("端口必须在 1 到 65535 之间")
    return args


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    STATE.port = args.port
    STATE.initialize(download=not args.no_download)
    server = XiangqiHTTPServer((args.host, args.port), XiangqiHandler)
    LOGGER.info("Xiangqi service listening on http://%s:%d", args.host, args.port)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        LOGGER.info("stopping Xiangqi service")
    finally:
        server.server_close()
        STATE.close()


if __name__ == "__main__":
    main()
