"""Pikafish and Laya integration for the local Xiangqi service.

The module deliberately keeps the chess rules in the native Pikafish process.  A
move is accepted only when it is present in ``go perft 1`` for the current FEN;
the same source is used for check and terminal-state detection.  Laya only
chooses between real MultiPV branches returned by Pikafish.  It is never used
as a source of legal moves or as a silent replacement for a failed service.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import selectors
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


DEPLOY_DIR = Path(__file__).resolve().parent
ENGINE_DIR = DEPLOY_DIR / "engine"
ENGINE_BINARY_NAMES = (
    "pikafish",
    "Pikafish",
    "pikafish-arm64",
    "pikafish-macos-arm64",
    "Pikafish-MacOS-universal",
)
RELEASE_API_URL = "https://api.github.com/repos/official-pikafish/Pikafish/releases/latest"
RELEASE_TAG = "Pikafish-2026-09-06"
RELEASE_ASSET_NAME = "Pikafish.2026-09-06.7z"
DEFAULT_LAYA_URL = "http://127.0.0.1:8765"
LAYA_URL = os.environ.get("LAYA_XIANGQI_LAYA_URL", DEFAULT_LAYA_URL).rstrip("/")
REQUEST_TIMEOUT_SECONDS = 35.0
DEFAULT_THINK_SECONDS = 0.3
ENGINE_THREADS = 4
ENGINE_HASH_MB = 256
MOVE_RE = re.compile(r"^[a-i][0-9][a-i][0-9]$")
LAYA_DECISION_FACTOR_VERSION = "best-directed-v3"
SUPPORTED_MODELS = frozenset({"laya", "jev", "kev"})
PROVIDER_LABELS = {"laya": "Laya", "jev": "Jev", "kev": "Kev"}


class EngineError(RuntimeError):
    """Base class for a native-engine failure."""


class EngineUnavailable(EngineError):
    """The native binary or its NNUE data is unavailable."""


class UciError(EngineError):
    """The UCI process did not answer a command as expected."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


class LayaUnavailable(RuntimeError):
    """A decision provider could not make the requested decision."""


class GameRuleError(ValueError):
    """A FEN or move cannot be represented in Xiangqi coordinates."""


PIECE_NAMES = {
    "K": "帅",
    "A": "仕",
    "B": "相",
    "N": "马",
    "R": "车",
    "C": "炮",
    "P": "兵",
    "k": "将",
    "a": "士",
    "b": "象",
    "n": "马",
    "r": "车",
    "c": "炮",
    "p": "卒",
}


def _compact_error(error: BaseException, limit: int = 320) -> str:
    text = " ".join(str(error).split())
    if len(text) > limit:
        text = f"{text[: limit - 3]}..."
    return f"{type(error).__name__}: {text}" if text else type(error).__name__


def _read_json_bytes(body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LayaUnavailable("Laya 返回的内容不是有效 JSON") from error
    if not isinstance(payload, dict):
        raise LayaUnavailable("Laya 返回的 JSON 顶层不是对象")
    return payload


def parse_fen(fen: str) -> tuple[list[list[str | None]], str, list[str]]:
    """Return board rows (black side first), side, and FEN fields.

    Pikafish follows the usual Xiangqi FEN orientation: the first row is rank
    nine (black's back rank), while an API move such as ``a0a1`` uses rank zero
    at the red side.  Keeping this conversion in one place prevents the board
    renderer and move application from disagreeing.
    """

    fields = fen.strip().split()
    if len(fields) < 2:
        raise GameRuleError("FEN 至少需要局面和行棋方")
    rows = fields[0].split("/")
    if len(rows) != 10 or fields[1] not in {"w", "b"}:
        raise GameRuleError(f"不支持的象棋 FEN：{fen}")
    board: list[list[str | None]] = []
    for row in rows:
        expanded: list[str | None] = []
        for token in row:
            if token.isdigit():
                count = int(token)
                if count < 1 or count > 9:
                    raise GameRuleError(f"FEN 空格数量无效：{row}")
                expanded.extend([None] * count)
            elif token in PIECE_NAMES:
                expanded.append(token)
            else:
                raise GameRuleError(f"FEN 棋子无效：{token}")
        if len(expanded) != 9:
            raise GameRuleError(f"FEN 行宽不是 9：{row}")
        board.append(expanded)
    return board, fields[1], fields


def _fen_halfmove_clock(fen: str) -> int | None:
    """Return a valid FEN halfmove clock, or ``None`` when it is malformed."""

    fields = fen.strip().split()
    if len(fields) <= 4:
        return None
    try:
        return int(fields[4])
    except (TypeError, ValueError):
        return None


def _engine_query_fen(fen: str) -> str:
    """Keep geometry queries usable at the application's 120-ply draw edge.

    Pikafish rejects a ``Rule60`` counter of 120 or greater before answering
    UCI commands, even though legal moves and check status are independent of
    that counter.  Only the temporary FEN sent for those two geometry queries
    is clamped to the highest accepted value; the game's real FEN and history
    remain unchanged for adjudication and persistence.
    """

    fields = fen.strip().split()
    if len(fields) <= 4:
        return fen
    try:
        halfmove = int(fields[4])
    except (TypeError, ValueError):
        return fen
    if halfmove < 120:
        return fen
    fields[4] = "119"
    return " ".join(fields)


def board_to_fen(board: list[list[str | None]], side: str, fields: list[str] | None = None) -> str:
    """Serialize a 10x9 black-top board using Xiangqi FEN."""

    if len(board) != 10 or any(len(row) != 9 for row in board):
        raise GameRuleError("棋盘必须是 10x9")
    if side not in {"w", "b"}:
        raise GameRuleError("行棋方必须是 w 或 b")
    rows: list[str] = []
    for row in board:
        empty = 0
        encoded: list[str] = []
        for piece in row:
            if piece is None:
                empty += 1
            else:
                if empty:
                    encoded.append(str(empty))
                    empty = 0
                encoded.append(piece)
        if empty:
            encoded.append(str(empty))
        rows.append("".join(encoded))
    suffix = list(fields or ["-", "-", "0", "1"])
    if len(suffix) < 4:
        suffix.extend(["-"] * (4 - len(suffix)))
    return f"{'/'.join(rows)} {side} {' '.join(suffix[:4])}"


def apply_uci_move(fen: str, move: str) -> str:
    """Apply a previously engine-validated coordinate move to a FEN."""

    move = move.strip().lower()
    if not MOVE_RE.fullmatch(move):
        raise GameRuleError(f"走法格式无效：{move}")
    board, side, fields = parse_fen(fen)
    from_col = ord(move[0]) - ord("a")
    from_rank = int(move[1])
    to_col = ord(move[2]) - ord("a")
    to_rank = int(move[3])
    from_row = 9 - from_rank
    to_row = 9 - to_rank
    piece = board[from_row][from_col]
    if piece is None:
        raise GameRuleError(f"起点没有棋子：{move}")
    if (piece.isupper()) != (side == "w"):
        raise GameRuleError(f"不能移动对方棋子：{move}")
    captured = board[to_row][to_col]
    board[to_row][to_col] = piece
    board[from_row][from_col] = None
    halfmove = int(fields[4]) if len(fields) > 4 and fields[4].isdigit() else 0
    fullmove = int(fields[5]) if len(fields) > 5 and fields[5].isdigit() else 1
    if piece.lower() == "p" or captured is not None:
        halfmove = 0
    else:
        halfmove += 1
    if side == "b":
        fullmove += 1
    suffix = [fields[2] if len(fields) > 2 else "-", fields[3] if len(fields) > 3 else "-", str(halfmove), str(fullmove)]
    next_side = "b" if side == "w" else "w"
    return board_to_fen(board, next_side, suffix)


def _coerce_history_moves(moves: Iterable[Any] | None) -> list[str]:
    """Normalize a UCI history while preserving a strict validation boundary."""

    if moves is None:
        return []
    if isinstance(moves, (str, bytes)):
        raise GameRuleError("历史走法必须是 UCI 字符串数组")
    normalized: list[str] = []
    for item in moves:
        move = item.get("move") if isinstance(item, dict) else item
        if not isinstance(move, str):
            raise GameRuleError("历史走法必须是 UCI 字符串")
        move = move.strip().lower()
        if not MOVE_RE.fullmatch(move):
            raise GameRuleError(f"历史走法格式无效：{move}")
        normalized.append(move)
    return normalized


def replay_history(initial_fen: str, moves: Iterable[Any] | None, expected_fen: str | None = None) -> str:
    """Replay a UCI history and optionally require an exact resulting FEN.

    The search command must never silently discard a caller supplied history:
    a mismatch means the caller's state and the engine's state have diverged.
    """

    current = initial_fen
    for move in _coerce_history_moves(moves):
        current = apply_uci_move(current, move)
    if expected_fen is not None and current != expected_fen:
        raise GameRuleError("历史走法重放结果与当前 FEN 不一致")
    return current


def side_name(fen_side: str) -> str:
    return "red" if fen_side == "w" else "black"


def move_label(fen: str, move: str) -> str:
    """Give the UI a compact label without claiming Chinese notation accuracy."""

    try:
        board, _, _ = parse_fen(fen)
        col = ord(move[0]) - ord("a")
        rank = int(move[1])
        piece = board[9 - rank][col]
        target = board[9 - int(move[3])][ord(move[2]) - ord("a")]
    except (GameRuleError, IndexError, ValueError):
        piece = None
        target = None
    prefix = PIECE_NAMES.get(piece or "", "棋子")
    capture = f" ·吃{PIECE_NAMES.get(target, '子')}" if target else ""
    return f"{prefix} {move[:2]}→{move[2:]}{capture}"


def score_object(score_type: str | None, value: int | None) -> dict[str, Any] | None:
    if score_type not in {"cp", "mate"} or value is None:
        return None
    return {score_type: int(value), "type": score_type, "value": int(value)}


def scores_equivalent(left: dict[str, Any] | None, right: dict[str, Any] | None) -> bool:
    if not left or not right:
        return False
    left_type = left.get("type") or ("mate" if "mate" in left else "cp")
    right_type = right.get("type") or ("mate" if "mate" in right else "cp")
    if left_type != right_type:
        return False
    left_value = left.get(left_type, left.get("value"))
    right_value = right.get(right_type, right.get("value"))
    return left_value == right_value


def _score_rank(score: dict[str, Any] | None) -> tuple[int, int, int]:
    """Rank a score from the side-to-move perspective used by UCI.

    Pikafish's MultiPV order remains authoritative.  This rank is only a
    defensive ordering when an engine build emits lines out of order.
    """

    if not score:
        return (0, 0, 0)
    kind = score.get("type") or ("mate" if "mate" in score else "cp")
    value = int(score.get(kind, score.get("value", 0)))
    if kind == "mate":
        if value > 0:
            return (3, 100000 - value, 0)
        return (1, value, 0)
    return (2, value, 0)


class UciProcess:
    """One persistent native Pikafish process and its serialized UCI traffic."""

    MAX_DIAGNOSTIC_CHARS = 6000

    def __init__(self, binary: Path, threads: int = ENGINE_THREADS, hash_mb: int = ENGINE_HASH_MB) -> None:
        self.binary = binary.resolve()
        self.threads = threads
        self.hash_mb = hash_mb
        self.process: subprocess.Popen[str] | None = None
        self.io_lock = threading.RLock()
        self.selector = selectors.DefaultSelector()
        self._stdout_buffer = bytearray()
        self._recent_output: list[str] = []
        self._last_command: str | None = None
        self.last_diagnostics: dict[str, Any] | None = None
        self.ready = False

    def start(self) -> None:
        with self.io_lock:
            if self.ready and self.process and self.process.poll() is None:
                return
            if self.process is not None:
                self.stop()
            if not self.binary.is_file():
                raise EngineUnavailable(f"Pikafish 可执行文件不存在：{self.binary}")
            try:
                process = subprocess.Popen(
                    [str(self.binary)],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    bufsize=0,
                    cwd=str(self.binary.parent),
                )
            except OSError as error:
                raise EngineUnavailable(f"无法启动 Pikafish：{_compact_error(error)}") from error
            self.process = process
            self._stdout_buffer = bytearray()
            assert process.stdout is not None
            self.selector.register(process.stdout, selectors.EVENT_READ)
            try:
                self._send("uci")
                self._read_until(lambda line: line.strip() == "uciok", 12.0)
                self._send(f"setoption name Threads value {self.threads}")
                self._send(f"setoption name Hash value {self.hash_mb}")
                self._send("isready")
                self._read_until(lambda line: line.strip() == "readyok", 20.0)
                self.ready = True
            except Exception:
                self.stop()
                raise

    def stop(self) -> None:
        with self.io_lock:
            process = self.process
            self.ready = False
            self.process = None
            if process is None:
                return
            try:
                if process.poll() is None and process.stdin:
                    process.stdin.write(b"quit\n")
                    process.stdin.flush()
            except OSError:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
            try:
                self.selector.unregister(process.stdout)  # type: ignore[arg-type]
            except (KeyError, ValueError):
                pass
            for stream in (process.stdin, process.stdout):
                if stream is None:
                    continue
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass

    def _send(self, command: str) -> None:
        self._last_command = command.rstrip("\n")
        if not self.process:
            raise UciError("Pikafish 进程未运行")
        if self.process.poll() is not None:
            raise self._uci_failure("Pikafish 进程未运行")
        if not self.process.stdin:
            raise self._uci_failure("Pikafish 输入流不可用")
        try:
            self.process.stdin.write((command.rstrip("\n") + "\n").encode("utf-8"))
            self.process.stdin.flush()
        except OSError as error:
            raise self._uci_failure("Pikafish 命令发送失败") from error

    def _buffered_line(self) -> str | None:
        marker = self._stdout_buffer.find(b"\n")
        if marker < 0:
            return None
        line = bytes(self._stdout_buffer[:marker])
        del self._stdout_buffer[: marker + 1]
        return line.rstrip(b"\r").decode("utf-8", errors="replace")

    def _remember_output(self, line: str) -> None:
        self._recent_output.append(line)
        while self._recent_output and len("\n".join(self._recent_output)) > self.MAX_DIAGNOSTIC_CHARS:
            self._recent_output.pop(0)

    def _drain_output(self) -> None:
        """Read all currently available bytes after a child has exited."""

        if not self.process or not self.process.stdout:
            return
        while True:
            try:
                events = self.selector.select(0)
            except (OSError, ValueError):
                return
            if not events:
                return
            try:
                chunk = os.read(self.process.stdout.fileno(), 65536)
            except OSError:
                return
            if not chunk:
                return
            self._stdout_buffer.extend(chunk)

    def _uci_failure(self, message: str) -> UciError:
        self._drain_output()
        pending = bytes(self._stdout_buffer).decode("utf-8", errors="replace").strip()
        output_lines = list(self._recent_output)
        if pending:
            output_lines.append(pending)
        output = "\n".join(output_lines)
        if len(output) > self.MAX_DIAGNOSTIC_CHARS:
            output = output[-self.MAX_DIAGNOSTIC_CHARS :]
        process = self.process
        details: dict[str, Any] = {
            "command": self._last_command,
            "exitCode": process.poll() if process is not None else None,
            "output": output,
        }
        self.last_diagnostics = details
        diagnostic_line = next((line.strip() for line in reversed(output_lines) if line.strip()), "")
        if diagnostic_line and diagnostic_line not in message:
            message = f"{message}：{diagnostic_line}"
        return UciError(message, details=details)

    def _read_until(self, predicate: Any, timeout: float) -> list[str]:
        if not self.process or not self.process.stdout:
            raise UciError("Pikafish 输出流不可用")
        lines: list[str] = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            buffered = self._buffered_line()
            if buffered is not None:
                lines.append(buffered)
                self._remember_output(buffered)
                if predicate(buffered):
                    return lines
                continue
            if self.process.poll() is not None:
                self._drain_output()
                buffered = self._buffered_line()
                if buffered is not None:
                    lines.append(buffered)
                    self._remember_output(buffered)
                    if predicate(buffered):
                        return lines
                    continue
                raise self._uci_failure("Pikafish 进程提前退出")
            remaining = max(0.01, deadline - time.monotonic())
            events = self.selector.select(remaining)
            if not events:
                continue
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                buffered = self._buffered_line()
                if buffered is not None:
                    lines.append(buffered)
                    self._remember_output(buffered)
                    if predicate(buffered):
                        return lines
                    continue
                raise self._uci_failure("Pikafish 输出提前结束")
            self._stdout_buffer.extend(chunk)
        raise self._uci_failure(f"等待 Pikafish 响应超时（{timeout:.1f}s）")

    def legal_moves(self, fen: str) -> tuple[list[str], list[str]]:
        with self.io_lock:
            self.start()
            self._send(f"position fen {_engine_query_fen(fen)}")
            self._send("go perft 1")
            try:
                lines = self._read_until(lambda line: line.startswith("Nodes searched:"), 8.0)
            except UciError as error:
                self.stop()
                raise UciError(f"Pikafish perft 结果无效：{error}", details=error.details) from error
            moves: list[str] = []
            for line in lines:
                match = re.search(r"\b([a-i][0-9][a-i][0-9])\s*:", line.lower())
                if match and match.group(1) not in moves:
                    moves.append(match.group(1))
            return moves, lines

    def checkers(self, fen: str) -> tuple[bool, list[str]]:
        with self.io_lock:
            self.start()
            self._send(f"position fen {_engine_query_fen(fen)}")
            self._send("d")
            lines: list[str] = []
            try:
                lines = self._read_until(lambda line: line.startswith("Checkers:"), 4.0)
            except UciError as error:
                # Some old builds print Checkers only after a delayed board
                # dump.  A missing marker is unknown, rather than proof of no
                # check; callers report engine status as unavailable if needed.
                self.stop()
                raise UciError(f"Pikafish checkers 结果无效：{error}", details=error.details) from error
            marker = next((line.split(":", 1)[1].strip() for line in lines if line.startswith("Checkers:")), "-")
            return marker not in {"", "-", "none", "None"}, lines

    def analyze(
        self,
        fen: str,
        seconds: float = DEFAULT_THINK_SECONDS,
        multipv: int = 5,
        initial_fen: str | None = None,
        moves: Iterable[Any] | None = None,
    ) -> dict[str, Any]:
        if isinstance(seconds, bool):
            raise ValueError("seconds 必须是有限数字")
        seconds = float(seconds)
        if not math.isfinite(seconds):
            raise ValueError("seconds 必须是有限数字")
        seconds = min(30.0, max(0.1, seconds))
        multipv = max(1, min(int(multipv), 8))
        halfmove = _fen_halfmove_clock(fen)
        if halfmove is not None and halfmove >= 120:
            raise UciError(
                "Pikafish 已达到 120 个半回合的和棋边界，不继续搜索",
                details={"command": None, "exitCode": None, "output": "", "halfmove": halfmove},
            )
        history_moves = _coerce_history_moves(moves)
        if initial_fen is None:
            if history_moves:
                raise GameRuleError("提供历史走法时必须同时提供 initial_fen")
            position_command = f"position fen {fen}"
        else:
            replay_history(initial_fen, history_moves, expected_fen=fen)
            position_command = f"position fen {initial_fen}"
            if history_moves:
                position_command += " moves " + " ".join(history_moves)
        with self.io_lock:
            self.start()
            self._send(f"setoption name MultiPV value {multipv}")
            self._send("isready")
            self._read_until(lambda line: line.strip() == "readyok", 20.0)
            self._send(position_command)
            self._send(f"go movetime {int(seconds * 1000)}")
            try:
                lines = self._read_until(lambda line: line.startswith("bestmove "), seconds + 15.0)
            except UciError:
                self.stop()
                raise
        parsed: dict[int, dict[str, Any]] = {}
        for line in lines:
            if not line.startswith("info "):
                continue
            tokens = line.split()
            if "depth" not in tokens or "pv" not in tokens:
                continue
            try:
                depth = int(tokens[tokens.index("depth") + 1])
            except (ValueError, IndexError):
                continue
            multipv_index = 1
            if "multipv" in tokens:
                try:
                    multipv_index = int(tokens[tokens.index("multipv") + 1])
                except (ValueError, IndexError):
                    multipv_index = 1
            score: dict[str, Any] | None = None
            bound: str | None = None
            if "score" in tokens:
                score_index = tokens.index("score")
                if score_index + 2 < len(tokens) and tokens[score_index + 1] in {"cp", "mate"}:
                    try:
                        score = score_object(tokens[score_index + 1], int(tokens[score_index + 2]))
                    except ValueError:
                        score = None
                for possible_bound in ("lowerbound", "upperbound"):
                    if possible_bound in tokens[score_index + 3 : score_index + 6]:
                        bound = possible_bound
                        break
            pv = tokens[tokens.index("pv") + 1 :]
            if not pv:
                continue
            previous = parsed.get(multipv_index)
            if previous is None or depth >= int(previous.get("depth", 0)):
                parsed[multipv_index] = {
                    "move": pv[0],
                    "pv": pv,
                    "depth": depth,
                    "score": score,
                    "bound": bound,
                }
        bestmove = ""
        for line in reversed(lines):
            if line.startswith("bestmove "):
                bestmove = line.split()[1]
                break
        candidates = [parsed[index] for index in sorted(parsed)]
        if bestmove and all(item["move"] != bestmove for item in candidates):
            candidates.insert(0, {"move": bestmove, "pv": [bestmove], "depth": 0, "score": None})
        return {
            "bestmove": bestmove,
            "candidates": candidates,
            "raw": lines,
            "positionCommand": position_command,
            "historyMoves": history_moves,
        }


def _candidate_binary(paths: Iterable[Path]) -> Path | None:
    for path in paths:
        if not path.is_file() or not os.access(path, os.X_OK):
            continue
        name = path.name.lower()
        if "pikafish" in name or name in {item.lower() for item in ENGINE_BINARY_NAMES}:
            return path
    return None


def find_engine_binary() -> Path | None:
    """Find an extracted macOS arm64 binary without guessing another engine."""

    candidates: list[Path] = []
    for name in ENGINE_BINARY_NAMES:
        candidates.extend([ENGINE_DIR / name, ENGINE_DIR / "bin" / name])
    candidates.extend(ENGINE_DIR.glob("**/pikafish"))
    candidates.extend(ENGINE_DIR.glob("**/Pikafish"))
    candidates.extend(path for path in ENGINE_DIR.glob("**/*") if path.is_file() and path.name.lower().startswith("pikafish"))
    return _candidate_binary(candidates)


def _asset_metadata() -> tuple[str, str, int, str]:
    request = urllib.request.Request(RELEASE_API_URL, headers={"Accept": "application/vnd.github+json", "User-Agent": "laya-xiangqi"})
    with urllib.request.urlopen(request, timeout=20.0) as response:
        payload = _read_json_bytes(response.read())
    tag = str(payload.get("tag_name") or RELEASE_TAG)
    assets = payload.get("assets")
    if not isinstance(assets, list):
        raise EngineUnavailable("GitHub release 没有可识别的 assets")
    preferred: dict[str, Any] | None = None
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name") or "")
        if name.lower().endswith(".7z"):
            preferred = asset
            if "pikafish" in name.lower():
                break
    if preferred is None:
        raise EngineUnavailable(f"GitHub release {tag} 没有 Pikafish 7z asset")
    return (
        tag,
        str(preferred.get("browser_download_url") or ""),
        int(preferred.get("size") or 0),
        str(preferred.get("digest") or ""),
    )


def _extract_archive(archive: Path, target: Path) -> None:
    extractor = shutil.which("7z") or shutil.which("7zz") or shutil.which("bsdtar")
    if not extractor:
        raise EngineUnavailable("无法解压官方 .7z 包：需要 7z、7zz 或 bsdtar")
    target.mkdir(parents=True, exist_ok=True)
    if Path(extractor).name.lower() in {"7z", "7zz"}:
        command = [extractor, "x", str(archive), f"-o{target}", "-y"]
    else:
        command = [extractor, "-xf", str(archive), "-C", str(target)]
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=180)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise EngineUnavailable(f"解压 Pikafish 失败：{_compact_error(error)}") from error
    if result.returncode:
        raise EngineUnavailable(f"解压 Pikafish 失败：{_compact_error(RuntimeError(result.stderr or result.stdout))}")


def ensure_official_engine(download: bool = True) -> Path:
    """Ensure the official release is available and return its native binary.

    The API lookup is intentionally done at download time.  Existing extracted
    files are reused, so normal service starts do not need GitHub access.
    """

    existing = find_engine_binary()
    if existing:
        return existing
    if not download:
        raise EngineUnavailable(f"未找到 Pikafish native binary：{ENGINE_DIR}")
    ENGINE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        tag, url, expected_size, digest = _asset_metadata()
    except (OSError, urllib.error.URLError, ValueError) as error:
        raise EngineUnavailable(f"无法读取官方 Pikafish release：{_compact_error(error)}") from error
    if not url:
        raise EngineUnavailable(f"官方 release {tag} 缺少下载地址")
    archive_name = Path(urllib.parse.urlparse(url).path).name if "." in url else RELEASE_ASSET_NAME
    if not archive_name.lower().endswith(".7z"):
        archive_name = RELEASE_ASSET_NAME
    archive = ENGINE_DIR / archive_name
    partial = ENGINE_DIR / f".{archive_name}.part"
    request = urllib.request.Request(url, headers={"User-Agent": "laya-xiangqi", "Accept": "application/octet-stream"})
    try:
        with urllib.request.urlopen(request, timeout=60.0) as response, partial.open("wb") as output:
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                output.write(block)
        if expected_size and partial.stat().st_size != expected_size:
            raise EngineUnavailable(f"官方包大小校验失败：期望 {expected_size}，收到 {partial.stat().st_size}")
        if digest.startswith("sha256:"):
            expected_digest = digest.split(":", 1)[1].lower()
            actual_digest = hashlib.sha256(partial.read_bytes()).hexdigest()
            if actual_digest != expected_digest:
                raise EngineUnavailable(f"官方包 SHA-256 校验失败：期望 {expected_digest}，收到 {actual_digest}")
        partial.replace(archive)
    except (OSError, urllib.error.URLError, TimeoutError, EngineUnavailable) as error:
        raise EngineUnavailable(f"下载官方 Pikafish 失败：{_compact_error(error)}") from error
    extract_dir = ENGINE_DIR / f"release-{tag}"
    _extract_archive(archive, extract_dir)
    binary = find_engine_binary()
    if binary is None:
        raise EngineUnavailable(f"官方包已下载但未找到 mac arm64 Pikafish binary：{extract_dir}")
    manifest = ENGINE_DIR / "release.json"
    manifest.write_text(json.dumps({"tag": tag, "asset": archive.name, "digest": digest, "binary": str(binary.relative_to(ENGINE_DIR))}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return binary


class PikafishRuntime:
    """Own the process and expose legal-move/check/search operations."""

    def __init__(self, binary: Path | None = None) -> None:
        self.binary = binary
        self.process: UciProcess | None = None
        self.lock = threading.RLock()
        self.status = "starting"
        self.error: str | None = None

    def initialize(self, download: bool = True) -> None:
        with self.lock:
            try:
                self.binary = self.binary or ensure_official_engine(download=download)
                self.process = UciProcess(self.binary)
                self.process.start()
                self.status = "ready"
                self.error = None
            except EngineError as error:
                self.status = "error"
                self.error = str(error)
                raise

    def ensure_ready(self) -> UciProcess:
        with self.lock:
            if self.process is None or self.status != "ready":
                self.initialize(download=True)
            assert self.process is not None
            return self.process

    def health(self) -> dict[str, Any]:
        process_alive = bool(self.process and self.process.process and self.process.process.poll() is None and self.process.ready)
        if self.status == "ready" and not process_alive:
            self.status = "error"
            self.error = "Pikafish 进程已退出"
        return {
            "status": self.status,
            "binary": str(self.binary) if self.binary else None,
            "release": RELEASE_TAG,
            "threads": ENGINE_THREADS,
            "hashMb": ENGINE_HASH_MB,
            "error": self.error,
        }

    def legal_moves(self, fen: str) -> tuple[list[str], list[str]]:
        process = self.ensure_ready()
        return process.legal_moves(fen)

    def in_check(self, fen: str) -> tuple[bool, list[str]]:
        process = self.ensure_ready()
        return process.checkers(fen)

    def analyze(
        self,
        fen: str,
        seconds: float = DEFAULT_THINK_SECONDS,
        multipv: int = 5,
        initial_fen: str | None = None,
        moves: Iterable[Any] | None = None,
    ) -> dict[str, Any]:
        process = self.ensure_ready()
        if initial_fen is None and moves is None:
            return process.analyze(fen, seconds=seconds, multipv=multipv)
        return process.analyze(fen, seconds=seconds, multipv=multipv, initial_fen=initial_fen, moves=moves)

    def stop(self) -> None:
        with self.lock:
            if self.process:
                self.process.stop()
            self.status = "stopped"


def _compact_laya_score(score: Any) -> dict[str, Any] | None:
    """Keep the score shape small while preserving its engine meaning."""

    if not isinstance(score, dict):
        return None
    score_type = score.get("type")
    if score_type not in {"cp", "mate"}:
        if "cp" in score:
            score_type = "cp"
        elif "mate" in score:
            score_type = "mate"
    score_value = score.get(score_type, score.get("value")) if score_type in {"cp", "mate"} else score.get("value")
    try:
        if score_type in {"cp", "mate"} and score_value is not None:
            return {"type": score_type, "value": int(score_value)}
    except (TypeError, ValueError):
        pass
    return None


def _laya_score_factor(score: dict[str, Any] | None, side_label: str) -> str:
    """Describe the reported score without turning bounds into certainty."""

    perspective = f"{side_label}方视角"
    if not score:
        return f"分值 未返回（{perspective}）"
    score_type = score.get("type")
    value = score.get("value")
    if score_type == "cp":
        try:
            return f"分值 {int(value) / 100:+.2f}（{perspective}）"
        except (TypeError, ValueError):
            return f"分值 未返回（{perspective}）"
    if score_type == "mate":
        try:
            mate = int(value)
        except (TypeError, ValueError):
            return f"将杀步数 未返回（{perspective}）"
        if mate > 0:
            risk = f"{side_label}方将杀优势"
        elif mate < 0:
            risk = f"{side_label}方被将杀风险"
        else:
            risk = "将杀结果未定"
        return f"将杀 {mate:+d}步（{risk}）"
    return f"分值 未返回（{perspective}）"


def _laya_candidate_factor(index: int, candidate: dict[str, Any], side_label: str) -> str:
    """Build one concise, auditable choice description for the model."""

    rank = "引擎首选" if index == 0 else f"候选{index + 1}"
    label = str(candidate.get("label") or candidate.get("move") or "未知着法")
    try:
        depth = int(candidate.get("depth", 0) or 0)
    except (TypeError, ValueError):
        depth = 0
    try:
        repetition_count = int(candidate.get("repetitionCount", 0) or 0)
    except (TypeError, ValueError):
        repetition_count = 0
    bound = candidate.get("bound") or "无"
    return "；".join(
        (
            f"{rank}: {label}",
            _laya_score_factor(_compact_laya_score(candidate.get("score")), side_label),
            f"深度 {depth}",
            f"边界 {bound}",
            f"重复 {repetition_count}",
        )
    )


def build_laya_request(fen: str, legal_moves: list[str], candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the audited request shared by human and self-play Laya."""

    _, fen_side, _ = parse_fen(fen)
    side = side_name(fen_side)
    side_label = "红" if side == "red" else "黑"
    compact_candidates: list[dict[str, Any]] = []
    criteria: dict[str, str] = {}
    for index, candidate in enumerate(candidates):
        move = str(candidate["move"])
        compact_candidates.append(
            {
                "move": move,
                "score": _compact_laya_score(candidate.get("score")),
                "pv": list(candidate.get("pv", [])[:3]) if isinstance(candidate.get("pv"), (list, tuple)) else [],
                "bound": candidate.get("bound"),
                "repetitionCount": int(candidate.get("repetitionCount", 0) or 0),
            }
        )
        label = str(candidate.get("label") or move)
        prefix = "正确的首选着法：" if index == 0 else "非首选着法："
        criteria[move] = prefix + label + "；" + _laya_candidate_factor(index, candidate, side_label)
    state = {
        "game": "xiangqi",
        "instruction": f"你执{side_label}，以最强形态进攻；先选引擎首选。只从候选中选择最强分支。",
        "fenBefore": fen,
        "side": side,
        "scorePerspective": side,
        "legalMoveCount": len(legal_moves),
        "objective": "按 Pikafish 排名选择第一名，最佳着法已在选项中明确标注。",
        "candidates": compact_candidates,
    }
    questions = {
        "move": {
            "type": "choice",
            "instructions": "请选择正确的首选着法。",
            "criteria": criteria,
        }
    }
    return {"state": state, "questions": questions}


class LayaClient:
    """Call the existing local Laya decision endpoint with actual branches."""

    provider_name = "laya"
    provider_label = "Laya"

    def __init__(self, base_url: str = LAYA_URL, timeout: float = REQUEST_TIMEOUT_SECONDS) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def health(self) -> tuple[int, dict[str, Any]]:
        request = urllib.request.Request(f"{self.base_url}/health", headers={"Accept": "application/json"}, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=min(self.timeout, 3.0)) as response:
                return response.status, _read_json_bytes(response.read())
        except urllib.error.HTTPError as error:
            try:
                return error.code, _read_json_bytes(error.read())
            except LayaUnavailable:
                return error.code, {"status": "error", "error": _compact_error(error)}
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            return 503, {"status": "unreachable", "error": f"无法连接 Laya：{_compact_error(error)}"}

    def decide(self, fen: str, legal_moves: list[str], candidates: list[dict[str, Any]]) -> dict[str, Any]:
        payload = build_laya_request(fen, legal_moves, candidates)
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/v1/decide",
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status = response.status
                payload = _read_json_bytes(response.read())
        except urllib.error.HTTPError as error:
            detail = _compact_error(error)
            try:
                detail = json.dumps(_read_json_bytes(error.read()), ensure_ascii=False)[:360]
            except LayaUnavailable:
                pass
            raise LayaUnavailable(f"Laya HTTP {error.code}：{detail}") from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise LayaUnavailable(f"无法连接 Laya：{_compact_error(error)}") from error
        if status < 200 or status >= 300:
            raise LayaUnavailable(f"Laya HTTP {status}")
        return payload


def _extract_laya_choice(payload: Any, candidates: list[dict[str, Any]]) -> str | None:
    """Read a choice key from known Laya response shapes without inventing one."""

    valid = {item["move"] for item in candidates}
    valid_labels = {item.get("label"): item["move"] for item in candidates if item.get("label")}

    def normalize(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        clean = value.strip()
        if clean in valid:
            return clean
        if clean in valid_labels:
            return valid_labels[clean]
        match = re.search(r"\b([a-i][0-9][a-i][0-9])\b", clean.lower())
        return match.group(1) if match and match.group(1) in valid else None

    if isinstance(payload, dict):
        answers = payload.get("answers")
        if isinstance(answers, dict):
            move_answer = answers.get("move")
            if isinstance(move_answer, dict):
                for key in ("choice", "value", "answer", "selected", "move"):
                    result = normalize(move_answer.get(key))
                    if result:
                        return result
            for value in answers.values():
                result = normalize(value)
                if result:
                    return result
        for key in ("choice", "value", "answer", "selected", "move", "layaChoice"):
            result = normalize(payload.get(key))
            if result:
                return result
        for value in payload.values():
            result = _extract_laya_choice(value, candidates)
            if result:
                return result
    elif isinstance(payload, list):
        for value in payload:
            result = _extract_laya_choice(value, candidates)
            if result:
                return result
    else:
        return normalize(payload)
    return None


def build_candidates(
    fen: str,
    analysis: dict[str, Any],
    legal_moves: list[str],
    repetition_counts: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Normalize actual UCI MultiPV data into the public decision-tree shape."""

    _, fen_side, _ = parse_fen(fen)
    score_perspective = side_name(fen_side)
    by_move: dict[str, dict[str, Any]] = {}
    for source in analysis.get("candidates", []):
        move = str(source.get("move", "")).lower()
        if not MOVE_RE.fullmatch(move) or move not in legal_moves:
            continue
        candidate = {
            "move": move,
            "label": move_label(fen, move),
            "score": source.get("score"),
            "scorePerspective": score_perspective,
            "depth": int(source.get("depth", 0) or 0),
            "pv": [item for item in source.get("pv", []) if MOVE_RE.fullmatch(str(item))],
            "selected": False,
            "bound": source.get("bound"),
            "repetitionCount": int((repetition_counts or {}).get(move, 0)),
        }
        by_move[move] = candidate
    bestmove = str(analysis.get("bestmove") or "").lower()
    if MOVE_RE.fullmatch(bestmove) and bestmove in legal_moves and bestmove not in by_move:
        by_move[bestmove] = {
            "move": bestmove,
            "label": move_label(fen, bestmove),
            "score": None,
            "scorePerspective": score_perspective,
            "depth": 0,
            "pv": [bestmove],
            "selected": False,
            "bound": None,
            "repetitionCount": int((repetition_counts or {}).get(bestmove, 0)),
        }
    candidates = list(by_move.values())
    # MultiPV's first line and the engine's bestmove are the authority.  Never
    # reorder by scores from different depths: a later provisional line must
    # not become a stronger move merely because its cp value is larger.
    if bestmove and bestmove in by_move:
        candidates = [by_move[bestmove]] + [item for item in candidates if item["move"] != bestmove]
    if not candidates:
        raise EngineError("Pikafish 没有返回合法 MultiPV 候选")
    return candidates


@dataclass
class Decision:
    final_move: str
    laya_choice: str | None
    guarded: bool
    guard_reason: str | None
    candidates: list[dict[str, Any]]
    raw_laya: dict[str, Any]
    raw_uci: list[str]
    depth: int
    score: dict[str, Any] | None
    elapsed_ms: int
    timings: dict[str, int]
    legal_move_count: int
    fen_before: str
    score_perspective: str = "black"
    engine_best_move: str = ""
    laya_rank: int | None = None
    choice_score: dict[str, Any] | None = None
    score_loss_cp: int | None = None
    repetition_avoided: bool = False
    history_ply_count: int = 0
    factor_version: str = LAYA_DECISION_FACTOR_VERSION
    factor_criteria: dict[str, str] = field(default_factory=dict)
    provider: str = "laya"
    provider_label: str = "Laya"


def choose_laya_move(
    runtime: PikafishRuntime,
    laya: LayaClient,
    fen: str,
    seconds: float = DEFAULT_THINK_SECONDS,
    initial_fen: str | None = None,
    moves: Iterable[Any] | None = None,
) -> Decision:
    """Search, ask Laya, and guard the choice against the engine optimum."""

    legal_moves, perft_raw = runtime.legal_moves(fen)
    if not legal_moves:
        raise EngineError("当前局面没有合法走法")
    history_moves = _coerce_history_moves(moves)
    position_counts, history_ply_count = _history_position_counts(fen, initial_fen, history_moves)
    repetition_counts: dict[str, int] = {move: 0 for move in legal_moves}
    if history_moves:
        for move in legal_moves:
            result_fen = apply_uci_move(fen, move)
            repetition_counts[move] = position_counts.get(_position_key(result_fen), 0)
    started = time.monotonic()
    timing_started = time.perf_counter()
    search_started = timing_started
    if initial_fen is None and not history_moves:
        analysis = runtime.analyze(fen, seconds=seconds, multipv=min(5, max(1, len(legal_moves))))
    else:
        analysis = runtime.analyze(
            fen,
            seconds=seconds,
            multipv=min(5, max(1, len(legal_moves))),
            initial_fen=initial_fen,
            moves=history_moves,
        )
    search_ms = int((time.perf_counter() - search_started) * 1000)
    candidates = build_candidates(fen, analysis, legal_moves, repetition_counts=repetition_counts)
    decision_payload = build_laya_request(fen, legal_moves, candidates)
    laya_started = time.perf_counter()
    raw_laya = laya.decide(fen, legal_moves, candidates)
    laya_ms = int((time.perf_counter() - laya_started) * 1000)
    choice = _extract_laya_choice(raw_laya, candidates)
    engine_bestmove = str(analysis.get("bestmove") or "").lower()
    engine_best = next((item for item in candidates if item["move"] == engine_bestmove), candidates[0])
    best = engine_best
    for candidate in candidates:
        same_search_line = (
            int(candidate.get("depth", 0) or 0) == int(engine_best.get("depth", 0) or 0)
            and not candidate.get("bound")
            and not engine_best.get("bound")
        )
        if same_search_line and scores_equivalent(candidate.get("score"), engine_best.get("score")):
            if int(candidate.get("repetitionCount", 0)) < int(best.get("repetitionCount", 0)):
                best = candidate
    repetition_avoided = False
    _, fen_side, _ = parse_fen(fen)
    score_perspective = side_name(fen_side)
    choice_candidate = next((item for item in candidates if item["move"] == choice), None) if choice else None
    final = best["move"]
    guarded = False
    guard_reason: str | None = None
    if choice:
        selected = next((item for item in candidates if item["move"] == choice), None)
        same_search_line = selected and int(selected.get("depth", 0)) == int(best.get("depth", 0)) and not selected.get("bound") and not best.get("bound")
        same_score = bool(selected and same_search_line and scores_equivalent(selected.get("score"), best.get("score")))
        if selected and same_score and int(selected.get("repetitionCount", 0)) > int(best.get("repetitionCount", 0)):
            guarded = True
            repetition_avoided = True
            final = best["move"]
            guard_reason = "同深度、无边界且同分候选中，已选择历史重复次数更少的着法，保留 Laya 原始选择供审计。"
        elif selected and selected["move"] == best["move"]:
            # A direct Laya choice of the engine's own bestmove is already the
            # guarded result, even when the engine did not attach a score.
            final = selected["move"]
        elif selected and same_score:
            final = selected["move"]
        elif selected:
            guarded = True
            if not same_search_line:
                guard_reason = "Laya 选择的候选与 Pikafish 最优分支搜索深度不同或带有边界评分，不能视为等优，已执行最优着法。"
            else:
                guard_reason = "Laya 选择的候选未达到 Pikafish 最优分支的等优评分，已拦截并执行最优着法。"
        else:
            guarded = True
            guard_reason = "Laya 返回的候选不在本次合法 MultiPV 中，已执行 Pikafish 最优着法。"
    elif len(candidates) == 1:
        guard_reason = "本次搜索仅有一个候选；仍已完成真实 Laya 决策请求。"
    else:
        guarded = True
        guard_reason = "Laya 原始输出没有可解析的候选键，已执行 Pikafish 最优着法。"
    for candidate in candidates:
        candidate["selected"] = candidate["move"] == final
        if candidate["move"] != final:
            if choice == candidate["move"] and guarded:
                candidate["reason"] = "Laya 选择但被最优评分守门拦截"
            elif not candidate.get("score"):
                candidate["reason"] = "引擎未在该分支输出评分"
            else:
                candidate["reason"] = "未执行"
        elif guarded:
            candidate["reason"] = "最优评分守门采用"
    elapsed_ms = int((time.monotonic() - started) * 1000)
    depth = max(int(item.get("depth", 0) or 0) for item in candidates)
    final_candidate = next(item for item in candidates if item["move"] == final)
    choice_score = choice_candidate.get("score") if choice_candidate else None
    best_score = best.get("score")
    score_loss_cp: int | None = None
    choice_type = choice_score.get("type") if choice_score else None
    best_type = best_score.get("type") if best_score else None
    same_score_line = bool(
        choice_candidate
        and int(choice_candidate.get("depth", 0) or 0) == int(best.get("depth", 0) or 0)
        and not choice_candidate.get("bound")
        and not best.get("bound")
    )
    if choice_score and best_score:
        choice_type = choice_type or ("mate" if "mate" in choice_score else "cp" if "cp" in choice_score else None)
        best_type = best_type or ("mate" if "mate" in best_score else "cp" if "cp" in best_score else None)
    if same_score_line and choice_score and best_score and choice_type == "cp" and best_type == "cp":
        try:
            score_loss_cp = int(best_score.get("value", best_score.get("cp"))) - int(choice_score.get("value", choice_score.get("cp")))
        except (TypeError, ValueError):
            score_loss_cp = None
    timings = {
        "searchMs": search_ms,
        "layaMs": laya_ms,
        "modelMs": laya_ms,
        "totalMs": int((time.perf_counter() - timing_started) * 1000),
    }
    raw_uci = perft_raw + list(analysis.get("raw", []))
    position_command = analysis.get("positionCommand")
    if history_moves:
        if not isinstance(position_command, str):
            if initial_fen is None:
                raise GameRuleError("提供历史走法时必须同时提供 initial_fen")
            position_command = f"position fen {initial_fen} moves {' '.join(history_moves)}"
        raw_uci = perft_raw + [position_command] + list(analysis.get("raw", []))
    return Decision(
        final_move=final,
        laya_choice=choice,
        guarded=guarded,
        guard_reason=guard_reason,
        candidates=candidates,
        raw_laya=raw_laya,
        raw_uci=raw_uci,
        depth=depth,
        score=final_candidate.get("score"),
        elapsed_ms=elapsed_ms,
        timings=timings,
        legal_move_count=len(legal_moves),
        fen_before=fen,
        score_perspective=score_perspective,
        engine_best_move=engine_best["move"],
        laya_rank=(candidates.index(choice_candidate) + 1) if choice_candidate else None,
        choice_score=choice_score,
        score_loss_cp=score_loss_cp,
        repetition_avoided=repetition_avoided,
        history_ply_count=history_ply_count,
        factor_version=LAYA_DECISION_FACTOR_VERSION,
        factor_criteria=dict(decision_payload["questions"]["move"]["criteria"]),
        provider=str(getattr(laya, "provider_name", "laya")),
        provider_label=str(getattr(laya, "provider_label", "Laya")),
    )


@dataclass
class Game:
    id: str
    puzzle: dict[str, Any]
    fen: str
    turn: str
    legal_moves: list[str]
    in_check: bool
    status: str = "playing"
    reason: str | None = None
    moves: list[dict[str, Any]] = field(default_factory=list)
    logs: list[dict[str, Any]] = field(default_factory=list)
    archive_logs: list[dict[str, Any]] = field(default_factory=list)
    version: int = 0
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    rounds: list[dict[str, Any]] = field(default_factory=list, repr=False)
    mode: str = "human"
    model: str = "laya"
    opponent_model: str | None = None
    adjudication: dict[str, Any] | None = None

    def snapshot_round(self) -> None:
        self.rounds.append(
            {
                "fen": self.fen,
                "turn": self.turn,
                "legal_moves": list(self.legal_moves),
                "in_check": self.in_check,
                "status": self.status,
                "reason": self.reason,
                "adjudication": json.loads(json.dumps(self.adjudication, ensure_ascii=False)) if self.adjudication else None,
                "moves_count": len(self.moves),
                "logs_count": len(self.logs),
            }
        )

    def undo_round(self, runtime: PikafishRuntime | None = None) -> None:
        if not self.rounds:
            raise GameRuleError("没有可撤销的回合")
        mutation_state = _capture_game_mutation_state(self)
        try:
            snapshot = self.rounds.pop()
            moves_count = int(snapshot.get("moves_count", len(snapshot.get("moves", []))))
            logs_count = int(snapshot.get("logs_count", len(snapshot.get("logs", []))))
        except (TypeError, ValueError) as error:
            _restore_game_mutation_state(self, mutation_state)
            raise GameRuleError("悔棋快照无效") from error
        if moves_count < 0 or logs_count < 0 or moves_count > len(self.moves) or logs_count > len(self.logs):
            _restore_game_mutation_state(self, mutation_state)
            raise GameRuleError("悔棋快照超出当前棋局范围")
        removed = self.logs[logs_count:]
        for item in removed:
            item = json.loads(json.dumps(item, ensure_ascii=False))
            item["undone"] = True
            self.archive_logs.append(item)
        self.fen = snapshot["fen"]
        self.turn = snapshot["turn"]
        self.legal_moves[:] = snapshot["legal_moves"]
        self.in_check = snapshot["in_check"]
        self.status = snapshot["status"]
        self.reason = snapshot["reason"]
        adjudication = snapshot.get("adjudication")
        self.adjudication = json.loads(json.dumps(adjudication, ensure_ascii=False)) if isinstance(adjudication, dict) else None
        self.moves[:] = self.moves[:moves_count]
        self.logs[:] = self.logs[:logs_count]
        self.logs.append({
            "id": uuid.uuid4().hex,
            "side": "system",
            "move": None,
            "label": "悔棋",
            "kind": "system",
            "summary": "上一回合已撤回；被撤回的决策日志保存在 archiveLogs。",
            "undoneCount": len(removed),
        })
        self.version += 1
        if runtime is not None:
            try:
                refresh_game(self, runtime)
            except Exception:
                _restore_game_mutation_state(self, mutation_state)
                raise

    def to_dict(self) -> dict[str, Any]:
        board, _, _ = parse_fen(self.fen)
        return {
            "id": self.id,
            "fen": self.fen,
            "board": board,
            "turn": self.turn,
            "mode": self.mode,
            "model": self.model,
            "opponentModel": self.opponent_model,
            "legalMoves": list(self.legal_moves),
            "moves": json.loads(json.dumps(self.moves, ensure_ascii=False)),
            "logs": json.loads(json.dumps(self.logs, ensure_ascii=False)),
            "archiveLogs": json.loads(json.dumps(self.archive_logs, ensure_ascii=False)),
            "status": self.status,
            "reason": self.reason,
            "adjudication": json.loads(json.dumps(self.adjudication, ensure_ascii=False)) if self.adjudication else None,
            "inCheck": self.in_check,
            "version": self.version,
            "puzzle": self.puzzle,
        }

    def to_persist_dict(self) -> dict[str, Any]:
        payload = self.to_dict()
        payload["rounds"] = json.loads(json.dumps(self.rounds, ensure_ascii=False))
        return payload


def _position_key(fen: str) -> str:
    fields = fen.split()
    return " ".join(fields[:2])


def _history_position_counts(
    fen: str,
    initial_fen: str | None,
    moves: Iterable[Any] | None,
) -> tuple[dict[str, int], int]:
    """Count historical board-plus-side positions for repetition tie-breaking."""

    history_moves = _coerce_history_moves(moves)
    if initial_fen is None:
        if history_moves:
            raise GameRuleError("提供历史走法时必须同时提供 initial_fen")
        return {_position_key(fen): 1}, 0
    current = initial_fen
    counts: dict[str, int] = {_position_key(current): 1}
    for move in history_moves:
        current = apply_uci_move(current, move)
        key = _position_key(current)
        counts[key] = counts.get(key, 0) + 1
    if current != fen:
        raise GameRuleError("历史走法重放结果与当前 FEN 不一致")
    return counts, len(history_moves)


def _replayed_game_fens(game: Game) -> list[str] | None:
    """Return every historical FEN, or ``None`` for an inconsistent archive."""

    current = str(game.puzzle.get("fen", game.fen))
    states = [current]
    for item in game.moves:
        move = item.get("move") if isinstance(item, dict) else None
        if not isinstance(move, str):
            return None
        try:
            current = apply_uci_move(current, move)
        except GameRuleError:
            return None
        states.append(current)
    return states if current == game.fen else None


def _repetition_adjudication(game: Game, runtime: PikafishRuntime | None = None) -> dict[str, Any] | None:
    """Classify the current third repetition, including perpetual check."""

    states = _replayed_game_fens(game)
    if not states:
        return None
    current_key = _position_key(states[-1])
    occurrences = [index for index, state in enumerate(states) if _position_key(state) == current_key]
    if len(occurrences) < 3:
        return None
    occurrences = occurrences[-3:]
    cycle_moves: list[dict[str, Any]] = []
    for state_index in range(occurrences[0], occurrences[-1]):
        item = game.moves[state_index]
        move = item.get("move")
        if not isinstance(move, str):
            return None
        side = item.get("side")
        if side not in {"red", "black"}:
            _, state_side, _ = parse_fen(states[state_index])
            side = side_name(state_side)
        gave_check = item.get("gaveCheck")
        if not isinstance(gave_check, bool):
            # Older archives did not store this bit.  Only backfill the
            # positions that are actually needed for a repetition decision.
            if runtime is None:
                gave_check = False
            else:
                next_check, _ = runtime.in_check(states[state_index + 1])
                gave_check = bool(next_check)
                item["gaveCheck"] = gave_check
        cycle_moves.append(
            {
                "ply": int(item.get("ply", state_index + 1)),
                "side": side,
                "move": move,
                "gaveCheck": gave_check,
            }
        )
    by_side = {
        side: [item for item in cycle_moves if item["side"] == side]
        for side in ("red", "black")
    }
    all_check = {
        side: bool(items) and all(item["gaveCheck"] for item in items)
        for side, items in by_side.items()
    }
    red_all = all_check["red"]
    black_all = all_check["black"]
    if red_all and not black_all:
        return {
            "kind": "perpetual_check",
            "loser": "red",
            "occurrences": occurrences,
            "cycleMoves": cycle_moves,
            "reason": "红方在重复循环中连续长将，按规则判负。",
        }
    if black_all and not red_all:
        return {
            "kind": "perpetual_check",
            "loser": "black",
            "occurrences": occurrences,
            "cycleMoves": cycle_moves,
            "reason": "黑方在重复循环中连续长将，按规则判负。",
        }
    if red_all and black_all:
        return {
            "kind": "mutual_check",
            "loser": None,
            "occurrences": occurrences,
            "cycleMoves": cycle_moves,
            "reason": "双方在重复循环中均连续将军，判和。",
        }
    return {
        "kind": "repetition",
        "loser": None,
        "occurrences": occurrences,
        "cycleMoves": cycle_moves,
        "reason": "同一局面（含行棋方）出现三次，未检测到单方长将，按本应用练习规则判和。",
    }


def _has_threefold_repetition(game: Game) -> bool:
    """Backward-compatible boolean wrapper for callers outside the service."""

    return _repetition_adjudication(game) is not None


def refresh_game(game: Game, runtime: PikafishRuntime) -> None:
    board, fen_side, _ = parse_fen(game.fen)
    del board
    game.turn = side_name(fen_side)
    legal, _ = runtime.legal_moves(game.fen)
    game.legal_moves = legal
    game.in_check, _ = runtime.in_check(game.fen)
    if game.moves and isinstance(game.moves[-1], dict) and not isinstance(game.moves[-1].get("gaveCheck"), bool):
        game.moves[-1]["gaveCheck"] = game.in_check
    if legal:
        game.status = "playing"
        game.reason = None
        game.adjudication = None
    else:
        # In Xiangqi a side that has no legal move loses even when its king is
        # not attacked (困毙).  This is distinct from the explicit 120-ply
        # draw guard below.
        game.status = "black_win" if game.turn == "red" else "red_win"
        side_label = "红方" if game.turn == "red" else "黑方"
        game.reason = f"{side_label}无合法走法，判定{'将死' if game.in_check else '困毙'}。"
        game.adjudication = None
    if game.status == "playing":
        adjudication = _repetition_adjudication(game, runtime)
        fields = game.fen.split()
        if adjudication is not None:
            game.adjudication = adjudication
            loser = adjudication.get("loser")
            if loser == "red":
                game.status = "black_win"
            elif loser == "black":
                game.status = "red_win"
            else:
                game.status = "draw"
            game.reason = adjudication["reason"]
        elif len(fields) > 4 and fields[4].isdigit() and int(fields[4]) >= 120:
            halfmove_clock = int(fields[4])
            game.adjudication = {
                "kind": "no_progress",
                "loser": None,
                "occurrences": [],
                "cycleMoves": [],
                "halfmoveClock": halfmove_clock,
                "limit": 120,
                "reason": "连续 120 个半回合未吃子或走兵，判定和棋。",
            }
            game.status = "draw"
            game.reason = game.adjudication["reason"]
    if game.adjudication:
        existing = any(
            item.get("kind") == "system"
            and isinstance(item.get("raw"), dict)
            and item["raw"].get("adjudication") == game.adjudication
            for item in game.logs
            if isinstance(item, dict)
        )
        if not existing:
            label = {
                "perpetual_check": "长将判负",
                "mutual_check": "双方互将和棋",
                "repetition": "三次重复和棋",
                "no_progress": "无进展和棋",
            }.get(str(game.adjudication.get("kind")), "重复裁决")
            game.logs.append(
                {
                    "id": uuid.uuid4().hex,
                    "side": "system",
                    "move": None,
                    "label": f"裁决 · {label}",
                    "kind": "system",
                    "summary": game.adjudication.get("reason"),
                    "raw": {"adjudication": json.loads(json.dumps(game.adjudication, ensure_ascii=False))},
                }
            )


def make_game(
    puzzle: dict[str, Any],
    runtime: PikafishRuntime,
    mode: str = "human",
    model: str = "laya",
    opponent_model: str | None = None,
) -> Game:
    if mode not in {"human", "selfplay", "duel"}:
        raise GameRuleError("mode 必须是 human、selfplay 或 duel")
    if model not in SUPPORTED_MODELS:
        raise GameRuleError("model 必须是 laya、jev 或 kev")
    if mode == "duel":
        if opponent_model not in SUPPORTED_MODELS or opponent_model == model:
            raise GameRuleError("duel 模式必须使用两个不同的模型分别执红、执黑")
    else:
        opponent_model = None
    fen = str(puzzle["fen"])
    _, fen_side, _ = parse_fen(fen)
    if fen_side != "w":
        raise GameRuleError("象棋残局必须由红方先行")
    game = Game(
        id=uuid.uuid4().hex,
        puzzle=puzzle,
        fen=fen,
        turn="red",
        legal_moves=[],
        in_check=False,
        mode=mode,
        model=model,
        opponent_model=opponent_model,
    )
    refresh_game(game, runtime)
    return game


def _round_from_state(
    fen: str,
    turn: str,
    status: str,
    reason: str | None,
    adjudication: dict[str, Any] | None,
    moves_count: int,
    logs_count: int,
) -> dict[str, Any]:
    return {
        "fen": fen,
        "turn": turn,
        "legal_moves": [],
        "in_check": False,
        "status": status,
        "reason": reason,
        "adjudication": json.loads(json.dumps(adjudication, ensure_ascii=False)) if adjudication else None,
        "moves_count": moves_count,
        "logs_count": logs_count,
    }


def _compact_round(snapshot: Any) -> dict[str, Any]:
    """Normalize compact rounds and older full-list round snapshots."""

    if not isinstance(snapshot, dict):
        raise GameRuleError("悔棋快照必须是对象")
    try:
        moves_count = int(snapshot.get("moves_count", len(snapshot.get("moves", []))))
        logs_count = int(snapshot.get("logs_count", len(snapshot.get("logs", []))))
    except (TypeError, ValueError) as error:
        raise GameRuleError("悔棋快照计数无效") from error
    if moves_count < 0 or logs_count < 0:
        raise GameRuleError("悔棋快照计数无效")
    return {
        "fen": snapshot.get("fen"),
        "turn": snapshot.get("turn"),
        "legal_moves": list(snapshot.get("legal_moves", [])),
        "in_check": bool(snapshot.get("in_check", False)),
        "status": snapshot.get("status", "playing"),
        "reason": snapshot.get("reason"),
        "adjudication": json.loads(json.dumps(snapshot.get("adjudication"), ensure_ascii=False)) if isinstance(snapshot.get("adjudication"), dict) else None,
        "moves_count": moves_count,
        "logs_count": logs_count,
    }


def load_game(snapshot: dict[str, Any], runtime: PikafishRuntime | None = None) -> Game:
    """Restore a persisted game without exposing an import endpoint.

    Older snapshots contain only the public ``Game.to_dict`` fields.  Their
    undo stack is rebuilt from the initial puzzle FEN and move history; the
    next undo refreshes legal moves through the live engine.
    """

    if not isinstance(snapshot, dict):
        raise GameRuleError("棋局快照必须是对象")
    game_id = snapshot.get("id")
    puzzle = snapshot.get("puzzle")
    fen = snapshot.get("fen")
    if not isinstance(game_id, str) or not game_id:
        raise GameRuleError("棋局快照缺少 id")
    if not isinstance(puzzle, dict) or not isinstance(puzzle.get("fen"), str):
        raise GameRuleError("棋局快照缺少 puzzle.fen")
    if not isinstance(fen, str):
        raise GameRuleError("棋局快照缺少 fen")
    parse_fen(fen)
    _, puzzle_side, _ = parse_fen(str(puzzle["fen"]))
    if puzzle_side != "w":
        raise GameRuleError("棋局快照 puzzle 必须由红方先行")
    mode = snapshot.get("mode", "human")
    if mode not in {"human", "selfplay", "duel"}:
        raise GameRuleError("棋局快照 mode 无效")
    model = snapshot.get("model", "laya")
    if model not in SUPPORTED_MODELS:
        raise GameRuleError("棋局快照 model 无效")
    opponent_model = snapshot.get("opponentModel")
    if mode == "duel":
        if opponent_model not in SUPPORTED_MODELS or opponent_model == model:
            raise GameRuleError("棋局快照 duel 模型归属无效")
    else:
        opponent_model = None
    _, fen_side, _ = parse_fen(fen)
    expected_turn = side_name(fen_side)
    turn = snapshot.get("turn", expected_turn)
    if turn not in {"red", "black"} or turn != expected_turn:
        raise GameRuleError("棋局快照 turn 与 FEN 不一致")
    legal_moves = snapshot.get("legalMoves", [])
    if not isinstance(legal_moves, list) or not all(isinstance(move, str) for move in legal_moves):
        legal_moves = []
    moves = snapshot.get("moves", [])
    logs = snapshot.get("logs", [])
    archive_logs = snapshot.get("archiveLogs", [])
    if not isinstance(moves, list) or not all(isinstance(item, dict) for item in moves):
        raise GameRuleError("棋局快照 moves 无效")
    if not isinstance(logs, list) or not all(isinstance(item, dict) for item in logs):
        raise GameRuleError("棋局快照 logs 无效")
    if not isinstance(archive_logs, list) or not all(isinstance(item, dict) for item in archive_logs):
        archive_logs = []
    try:
        version = int(snapshot.get("version", 0))
    except (TypeError, ValueError) as error:
        raise GameRuleError("棋局快照 version 无效") from error
    status = snapshot.get("status", "playing")
    if status not in {"playing", "draw", "red_win", "black_win"}:
        raise GameRuleError("棋局快照 status 无效")
    adjudication = snapshot.get("adjudication")
    if adjudication is not None and not isinstance(adjudication, dict):
        adjudication = None
    game = Game(
        id=game_id,
        puzzle=json.loads(json.dumps(puzzle, ensure_ascii=False)),
        fen=fen,
        turn=turn,
        legal_moves=list(legal_moves),
        in_check=bool(snapshot.get("inCheck", False)),
        mode=mode,
        model=model,
        opponent_model=opponent_model,
        status=status,
        reason=snapshot.get("reason") if isinstance(snapshot.get("reason"), str) else None,
        adjudication=json.loads(json.dumps(adjudication, ensure_ascii=False)) if adjudication else None,
        moves=json.loads(json.dumps(moves, ensure_ascii=False)),
        logs=json.loads(json.dumps(logs, ensure_ascii=False)),
        archive_logs=json.loads(json.dumps(archive_logs, ensure_ascii=False)),
        version=max(0, version),
    )
    persisted_rounds = snapshot.get("rounds")
    if isinstance(persisted_rounds, list) and persisted_rounds:
        game.rounds = [_compact_round(item) for item in persisted_rounds]
        return game

    # Rebuild a useful undo boundary for snapshots produced before rounds were
    # persisted.  The expensive legal/check refresh is deferred until undo.
    initial_fen = str(puzzle["fen"])
    current_fen = initial_fen
    current_turn = "red"
    current_moves: list[dict[str, Any]] = []
    log_cursor = 0
    for move_item in game.moves:
        move = move_item.get("move")
        if not isinstance(move, str):
            continue
        event_index = next(
            (position for position in range(log_cursor, len(game.logs)) if game.logs[position].get("move") == move),
            None,
        )
        if mode in {"selfplay", "duel"} or move_item.get("side") == "red":
            logs_count = event_index if event_index is not None else log_cursor
            game.rounds.append(_round_from_state(current_fen, current_turn, "playing", None, None, len(current_moves), logs_count))
        try:
            current_fen = apply_uci_move(current_fen, move)
        except GameRuleError:
            break
        current_turn = "black" if current_turn == "red" else "red"
        current_moves.append(json.loads(json.dumps(move_item, ensure_ascii=False)))
        if event_index is not None:
            log_cursor = event_index + 1
    return game


def _capture_game_mutation_state(game: Game) -> dict[str, Any]:
    """Capture the small mutable surface changed by an append operation.

    Move dictionaries are copied at the top level because refresh_game may
    backfill ``gaveCheck``.  Raw UCI/log payloads are intentionally retained
    by reference; refresh only appends log entries, so copying those large
    nested structures would add needless per-move cost.
    """

    return {
        "fen": game.fen,
        "turn": game.turn,
        "legal_moves": list(game.legal_moves),
        "in_check": game.in_check,
        "status": game.status,
        "reason": game.reason,
        "adjudication": game.adjudication,
        "moves": [dict(item) if isinstance(item, dict) else item for item in game.moves],
        "logs": list(game.logs),
        "archive_logs": list(game.archive_logs),
        "rounds": list(game.rounds),
        "version": game.version,
    }


def _restore_game_mutation_state(game: Game, state: dict[str, Any]) -> None:
    game.fen = state["fen"]
    game.turn = state["turn"]
    game.legal_moves[:] = state["legal_moves"]
    game.in_check = state["in_check"]
    game.status = state["status"]
    game.reason = state["reason"]
    game.adjudication = state["adjudication"]
    game.moves[:] = state["moves"]
    game.logs[:] = state["logs"]
    game.archive_logs[:] = state["archive_logs"]
    game.rounds[:] = state["rounds"]
    game.version = state["version"]


def append_human_move(game: Game, move: str, runtime: PikafishRuntime) -> None:
    if game.status != "playing":
        raise GameRuleError("对局已经结束")
    if game.turn != "red":
        raise GameRuleError("当前不是红方回合")
    move = move.strip().lower()
    if move not in game.legal_moves:
        raise GameRuleError("这步不在引擎返回的合法走法中")
    mutation_state = _capture_game_mutation_state(game)
    try:
        game.snapshot_round()
        fen_before = game.fen
        game.fen = apply_uci_move(game.fen, move)
        game.moves.append({"ply": len(game.moves) + 1, "side": "red", "move": move, "label": move_label(fen_before, move), "kind": "human"})
        human_log = {
            "id": uuid.uuid4().hex,
            "side": "red",
            "move": move,
            "label": move_label(fen_before, move),
            "kind": "human",
            "summary": "红方落子已通过 Pikafish 合法性验证。",
            "fenBefore": fen_before,
            "legalMoveCount": len(game.legal_moves),
        }
        game.logs.append(human_log)
        game.version += 1
        refresh_game(game, runtime)
        human_log["gaveCheck"] = bool(game.moves[-1].get("gaveCheck", False))
    except Exception:
        _restore_game_mutation_state(game, mutation_state)
        raise


def append_laya_move(game: Game, decision: Decision, runtime: PikafishRuntime) -> None:
    if game.status != "playing":
        return
    if game.turn not in {"red", "black"}:
        raise GameRuleError("当前行棋方无效")
    if decision.final_move not in game.legal_moves:
        raise EngineError("模型最终着法未通过当前局面合法走法校验")
    mutation_state = _capture_game_mutation_state(game)
    try:
        if game.mode in {"selfplay", "duel"}:
            game.snapshot_round()
        fen_before = game.fen
        game.fen = apply_uci_move(game.fen, decision.final_move)
        side = game.turn
        side_label = "红方" if side == "red" else "黑方"
        provider = decision.provider if decision.provider in SUPPORTED_MODELS else "laya"
        provider_label = decision.provider_label if provider == decision.provider else PROVIDER_LABELS[provider]
        game.moves.append({"ply": len(game.moves) + 1, "side": side, "move": decision.final_move, "label": move_label(fen_before, decision.final_move), "kind": provider, "score": decision.score})
        log = {
            "id": uuid.uuid4().hex,
            "side": side,
            "move": decision.final_move,
            "label": move_label(fen_before, decision.final_move),
            "kind": provider,
            "summary": f"{provider_label} · {side_label} 从 Pikafish 实际 MultiPV 候选中选择进攻线路，并经过最优评分守门。",
            "agent": f"{provider_label} · {side_label}",
            "provider": provider,
            "model": provider,
            "depth": decision.depth,
            "score": decision.score,
            "scorePerspective": side,
            "elapsedMs": decision.elapsed_ms,
            "timings": decision.timings,
            "candidates": decision.candidates,
            "raw": {provider: decision.raw_laya, "uci": decision.raw_uci},
            "guarded": decision.guarded,
            "guardReason": decision.guard_reason,
            "positionBefore": decision.fen_before,
            "fenBefore": decision.fen_before,
            "legalMoveCount": decision.legal_move_count,
            "modelChoice": decision.laya_choice,
            **({"layaChoice": decision.laya_choice} if provider == "laya" else {}),
            "finalMove": decision.final_move,
            "modelRank": decision.laya_rank,
            **({"layaRank": decision.laya_rank} if provider == "laya" else {}),
            "engineBestMove": decision.engine_best_move,
            "choiceScore": decision.choice_score,
            "scoreLossCp": decision.score_loss_cp,
            "repetitionAvoided": decision.repetition_avoided,
            "historyPlyCount": decision.history_ply_count,
            "decisionFactorVersion": decision.factor_version,
            "decisionCriteria": dict(decision.factor_criteria),
        }
        game.logs.append(log)
        game.version += 1
        refresh_game(game, runtime)
        log["gaveCheck"] = bool(game.moves[-1].get("gaveCheck", False))
    except Exception:
        _restore_game_mutation_state(game, mutation_state)
        raise


def load_puzzles(path: Path | None = None) -> list[dict[str, Any]]:
    target = path or (DEPLOY_DIR / "puzzles.json")
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise GameRuleError(f"残局配置读取失败：{_compact_error(error)}") from error
    if not isinstance(payload, list) or len(payload) != 10:
        raise GameRuleError("残局配置必须恰好包含 10 篇")
    required = {"id", "title", "subtitle", "difficulty", "fen"}
    result: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict) or not required.issubset(item):
            raise GameRuleError("残局条目字段不完整")
        _, side, _ = parse_fen(str(item["fen"]))
        if side != "w":
            raise GameRuleError(f"残局 {item.get('id')} 必须红先")
        result.append({key: item[key] for key in ("id", "title", "subtitle", "difficulty", "fen", "theme", "pieceCount", "analysis") if key in item})
    return result
