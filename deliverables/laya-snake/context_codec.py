"""Lossless compression for the text context sent to Laya.

The engine currently emits CTX2.  This module deliberately operates on the
serialized text only; it does not know anything about the game state or the
decision policy.  A compressed CTX3 context always expands back to the exact
CTX2 string it came from.
"""

from __future__ import annotations

from typing import Iterable


CTX2_HEADER = (
    "CTX2; x right,y down,origin 0,0; H=head,T=tail,S=body,F=food,.=free. "
    "U/R/D/L=up/right/down/left. Body links describe H to T with counts; "
    "routes are future moves."
)

# Keep the replacement shorter than CTX2 while spelling out both conventions
# needed to read a compressed context.  D3=DDD is an example of the exact
# count syntax used by body links and route RLE; @id names a plan's fullPath.
CTX3_HEADER = (
    "CTX3; x right,y down,origin 0,0; H=head,T=tail,S=body,F=food,.=free. "
    "U/R/D/L=up/right/down/left. body links H>T; RLE D3=DDD; @id=plan "
    "fullPath; routes=future."
)

MAX_PATH_STEPS = 10_000

_DIRECTIONS = frozenset("UDLR")
_HISTORY_PREFIX = "HISTORY(last8 oldest>newest move:food:penalty) "
_ROUTE_MARKERS = ("PREVIOUS_EXECUTED_ROUTE", "PREVIOUS_OWN_ROUTE")


def _line_body(line: str) -> tuple[str, str]:
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith("\n") or line.endswith("\r"):
        return line[:-1], line[-1:]
    return line, ""


def _rle_encode(path: str) -> str:
    """Return canonical direction+count RLE, without deciding whether to use it."""

    if not path or any(char not in _DIRECTIONS for char in path):
        raise ValueError("path contains a character outside UDLR")
    if len(path) > MAX_PATH_STEPS:
        raise ValueError("path exceeds MAX_PATH_STEPS")
    runs: list[tuple[str, int]] = []
    for char in path:
        if runs and runs[-1][0] == char:
            runs[-1] = char, runs[-1][1] + 1
        else:
            runs.append((char, 1))
    return "".join(f"{char}{count}" for char, count in runs)


def _rle_decode(value: str) -> str:
    """Decode canonical RLE and reject malformed or unreasonably large input."""

    if not value:
        raise ValueError("empty RLE path")
    result: list[str] = []
    total = 0
    position = 0
    while position < len(value):
        direction = value[position]
        if direction not in _DIRECTIONS:
            raise ValueError(f"invalid RLE direction: {direction!r}")
        position += 1
        start = position
        while position < len(value) and value[position] in "0123456789":
            position += 1
        if start == position:
            raise ValueError("RLE direction must be followed by a count")
        count_text = value[start:position]
        if count_text[0] == "0":
            raise ValueError("RLE counts must be positive decimal integers")
        # Avoid converting an attacker-controlled, very long integer before
        # the path-size guard can reject it.
        if len(count_text) > len(str(MAX_PATH_STEPS)) + 1:
            raise ValueError("RLE count is too large")
        count = int(count_text)
        if count <= 0:
            raise ValueError("RLE counts must be positive")
        total += count
        if total > MAX_PATH_STEPS:
            raise ValueError("decoded path exceeds MAX_PATH_STEPS")
        result.append(direction * count)
    return "".join(result)


def _decode_path(value: str) -> str:
    """Decode a plan/path token, retaining the CTX2 unknown markers."""

    if value in ("?", "-"):
        return value
    if not value:
        raise ValueError("empty path token")
    if any(char.isdigit() for char in value):
        return _rle_decode(value)
    if any(char not in _DIRECTIONS for char in value):
        raise ValueError(f"invalid path character in {value!r}")
    if len(value) > MAX_PATH_STEPS:
        raise ValueError("path exceeds MAX_PATH_STEPS")
    return value


def _compress_path(value: str) -> str:
    if value in ("?", "-"):
        return value
    # Validate before considering the size of an alternative representation.
    _decode_path(value)
    encoded = _rle_encode(value)
    return encoded if len(encoded) < len(value) else value


def _split_plans_line(line: str) -> tuple[str, list[list[str]]] | None:
    if not line.startswith("PLANS "):
        return None
    before, separator, after = line.partition(":")
    if not separator:
        raise ValueError("PLANS line has no row separator")
    spacing = " " if after.startswith(" ") else ""
    rows_text = after[1:] if spacing else after
    if not rows_text:
        raise ValueError("PLANS line has no plan rows")
    rows: list[list[str]] = []
    seen_ids: set[str] = set()
    for row in rows_text.split(";"):
        fields = row.split("|")
        if len(fields) < 5:
            raise ValueError("PLANS row has no fullPath field")
        plan_id = fields[0]
        if not plan_id or plan_id in seen_ids:
            raise ValueError("PLANS contains an empty or duplicate id")
        seen_ids.add(plan_id)
        rows.append(fields)
    return f"{before}:{spacing}", rows


def _plan_paths(line: str, *, compressed: bool) -> dict[str, str]:
    parsed = _split_plans_line(line)
    if parsed is None:
        return {}
    _, rows = parsed
    paths: dict[str, str] = {}
    for fields in rows:
        value = _decode_path(fields[4]) if compressed else fields[4]
        if not compressed:
            _decode_path(value)
        paths[fields[0]] = value
    return paths


def _compress_plans_line(line: str) -> str:
    parsed = _split_plans_line(line)
    if parsed is None:
        return line
    prefix, rows = parsed
    compressed_rows: list[str] = []
    for fields in rows:
        fields = list(fields)
        fields[4] = _compress_path(fields[4])
        compressed_rows.append("|".join(fields))
    return prefix + ";".join(compressed_rows)


def _expand_plans_line(line: str) -> str:
    parsed = _split_plans_line(line)
    if parsed is None:
        return line
    prefix, rows = parsed
    expanded_rows: list[str] = []
    for fields in rows:
        fields = list(fields)
        fields[4] = _decode_path(fields[4])
        expanded_rows.append("|".join(fields))
    return prefix + ";".join(expanded_rows)


def _route_parts(line: str) -> tuple[str, str] | None:
    if not any(line.startswith(marker) for marker in _ROUTE_MARKERS):
        return None
    split_at = line.rfind(" ")
    if split_at <= 0 or split_at == len(line) - 1:
        raise ValueError("previous-route line has no path token")
    return line[: split_at + 1], line[split_at + 1 :]


def _compress_route_line(line: str, paths: dict[str, str]) -> str:
    parts = _route_parts(line)
    if parts is None:
        return line
    prefix, value = parts
    if value in ("?", "-"):
        return line
    shortest = _compress_path(value)
    for plan_id, full_path in paths.items():
        reference = "@" + plan_id
        if full_path not in ("?", "-") and value == full_path and len(reference) < len(shortest):
            shortest = reference
    return prefix + shortest


def _expand_route_line(line: str, paths: dict[str, str]) -> str:
    parts = _route_parts(line)
    if parts is None:
        return line
    prefix, value = parts
    if value.startswith("@"):
        plan_id = value[1:]
        if not plan_id or plan_id not in paths:
            raise ValueError(f"unknown plan reference: {value!r}")
        resolved = paths[plan_id]
        if resolved in ("?", "-"):
            raise ValueError("plan reference does not identify a concrete path")
        return prefix + resolved
    return prefix + _decode_path(value)


def _history_entries(value: str) -> tuple[str, str, str]:
    entries = value.split(";")
    if not entries or any(not entry for entry in entries):
        raise ValueError("invalid empty HISTORY entry")
    moves: list[str] = []
    foods: list[str] = []
    penalties: list[str] = []
    for entry in entries:
        fields = entry.split(":")
        if len(fields) != 3 or len(fields[0]) != 1 or fields[0] not in _DIRECTIONS | {"?"}:
            raise ValueError("invalid HISTORY move:food:penalty entry")
        if fields[1] not in {"0", "1"} or fields[2] not in {"0", "1"}:
            raise ValueError("HISTORY food and penalty must be 0 or 1")
        moves.append(fields[0])
        foods.append(fields[1])
        penalties.append(fields[2])
    return "".join(moves), "".join(foods), "".join(penalties)


def _compress_history_line(line: str) -> str:
    if not line.startswith(_HISTORY_PREFIX):
        return line
    value = line[len(_HISTORY_PREFIX) :]
    if value == "-":
        return "HISTORY -"
    moves, foods, penalties = _history_entries(value)
    return f"HISTORY moves={moves} food={foods} penalty={penalties}"


def _expand_history_line(line: str) -> str:
    if line == "HISTORY -":
        return _HISTORY_PREFIX + "-"
    if not line.startswith("HISTORY moves="):
        return line
    fields = line[len("HISTORY moves=") :].split(" food=", 1)
    if len(fields) != 2:
        raise ValueError("compressed HISTORY is missing food")
    moves, remainder = fields
    foods_parts = remainder.split(" penalty=", 1)
    if len(foods_parts) != 2:
        raise ValueError("compressed HISTORY is missing penalty")
    foods, penalties = foods_parts
    if not moves or any(char not in _DIRECTIONS | {"?"} for char in moves):
        raise ValueError("invalid compressed HISTORY moves")
    if any(char not in "01" for char in foods + penalties):
        raise ValueError("invalid compressed HISTORY bits")
    if len(moves) != len(foods) or len(moves) != len(penalties):
        raise ValueError("compressed HISTORY fields have different lengths")
    value = ";".join(
        f"{move}:{food}:{penalty}"
        for move, food, penalty in zip(moves, foods, penalties)
    )
    return _HISTORY_PREFIX + value


def _lines(text: str) -> Iterable[tuple[str, str]]:
    for line in text.splitlines(keepends=True):
        yield _line_body(line)


def compress_context(context: str) -> str:
    """Compress a CTX2 context, returning other input unchanged.

    Compression is intentionally all-or-nothing: a malformed CTX2 context is
    returned untouched rather than being turned into a CTX3 value that cannot
    be expanded losslessly.
    """

    if not context.startswith(CTX2_HEADER):
        return context
    try:
        body = context[len(CTX2_HEADER) :]
        raw_lines = list(_lines(body))
        paths: dict[str, str] = {}
        for line, _ in raw_lines:
            if line.startswith("PLANS "):
                discovered = _plan_paths(line, compressed=False)
                duplicate = paths.keys() & discovered.keys()
                if duplicate:
                    raise ValueError("duplicate plan id across PLANS lines")
                paths.update(discovered)

        output: list[str] = []
        for line, ending in raw_lines:
            if line.startswith("PLANS "):
                transformed = _compress_plans_line(line)
            elif line.startswith(_HISTORY_PREFIX):
                transformed = _compress_history_line(line)
            elif any(line.startswith(marker) for marker in _ROUTE_MARKERS):
                transformed = _compress_route_line(line, paths)
            else:
                transformed = line
            output.append(transformed + ending)
        return CTX3_HEADER + "".join(output)
    except ValueError:
        return context


def expand_context(context: str) -> str:
    """Strictly expand CTX3; non-CTX3 input is returned unchanged."""

    if not context.startswith(CTX3_HEADER):
        return context
    body = context[len(CTX3_HEADER) :]
    raw_lines = list(_lines(body))
    paths: dict[str, str] = {}
    for line, _ in raw_lines:
        if line.startswith("PLANS "):
            discovered = _plan_paths(line, compressed=True)
            duplicate = paths.keys() & discovered.keys()
            if duplicate:
                raise ValueError("duplicate plan id across PLANS lines")
            paths.update(discovered)

    output: list[str] = []
    for line, ending in raw_lines:
        if line.startswith("PLANS "):
            transformed = _expand_plans_line(line)
        elif line.startswith("HISTORY "):
            transformed = _expand_history_line(line)
        elif any(line.startswith(marker) for marker in _ROUTE_MARKERS):
            transformed = _expand_route_line(line, paths)
        else:
            transformed = line
        output.append(transformed + ending)
    return CTX2_HEADER + "".join(output)


__all__ = [
    "CTX2_HEADER",
    "CTX3_HEADER",
    "MAX_PATH_STEPS",
    "compress_context",
    "expand_context",
]
