"""Search for independently certified physical shortcuts.

The live controller keeps its existing Hamiltonian route as the baseline.  A
candidate from the ordinary dynamic-snake planner is eligible here only when
it is strictly shorter than that baseline and a new Hamiltonian cycle can be
found whose order supports the complete candidate route.  This module only
returns a certificate candidate; it does not mutate a game or select a move.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Callable, Iterable

try:
    from .planner import legal_transition as ring_legal_transition
    from .strategy_planner import legal_transition as physical_legal_transition
    from .strategy_planner import plan_food as physical_plan_food
except ImportError:  # Running the module from its directory.
    from planner import legal_transition as ring_legal_transition
    from strategy_planner import legal_transition as physical_legal_transition
    from strategy_planner import plan_food as physical_plan_food


Coordinate = tuple[int, int]
Direction = str


@dataclass(frozen=True)
class CertifiedShortcut:
    """A route whose shorter physical path has an independently checked ring."""

    cycle: tuple[Coordinate, ...] | None
    moves: tuple[Direction, ...]
    expanded: int
    reason: str
    baseline_steps: int
    selected_steps: int | None

    @property
    def cycle_id(self) -> str | None:
        return cycle_id(self.cycle) if self.cycle is not None else None

    def as_dict(self) -> dict[str, Any]:
        """Return compact audit data; the full cycle stays on ``cycle``."""

        return {
            "cycleId": self.cycle_id,
            "moves": list(self.moves),
            "expanded": self.expanded,
            "reason": self.reason,
            "baselineSteps": self.baseline_steps,
            "selectedSteps": self.selected_steps,
        }


def cycle_id(cycle: Iterable[Coordinate] | None) -> str | None:
    """Return a stable twelve-character identifier for a certified cycle."""

    if cycle is None:
        return None
    encoded = ";".join(f"{x},{y}" for x, y in cycle).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()[:12]


def _cells(size: int) -> tuple[Coordinate, ...]:
    return tuple((x, y) for y in range(size) for x in range(size))


def _adjacency(size: int, cells: tuple[Coordinate, ...]) -> tuple[int, ...]:
    index = {cell: position for position, cell in enumerate(cells)}
    # Keep this order fixed for deterministic certificates and audit replay.
    vectors = ((1, 0), (-1, 0), (0, 1), (0, -1))
    result: list[int] = []
    for x, y in cells:
        mask = 0
        for dx, dy in vectors:
            neighbor = (x + dx, y + dy)
            if neighbor in index:
                mask |= 1 << index[neighbor]
        result.append(mask)
    return tuple(result)


def _preserves_ring_order(snake: tuple[Coordinate, ...], index: dict[Coordinate, int]) -> bool:
    if not snake or any(cell not in index for cell in snake):
        return False
    head_index = index[snake[0]]
    size = len(index)
    distances = [(index[cell] - head_index) % size for cell in snake]
    return distances[0] == 0 and all(left > right for left, right in zip(distances[1:], distances[2:]))


def _valid_cycle_shape(size: int, cycle: tuple[Coordinate, ...]) -> bool:
    cells = _cells(size)
    if len(cycle) != size * size or set(cycle) != set(cells):
        return False
    return all(
        abs(current[0] - following[0]) + abs(current[1] - following[1]) == 1
        for current, following in zip(cycle, (*cycle[1:], cycle[0]))
    )


def _replay_physical_route(
    *,
    size: int,
    snake: tuple[Coordinate, ...],
    food: Coordinate,
    moves: tuple[Direction, ...],
) -> tuple[tuple[Coordinate, ...], tuple[Coordinate, ...]] | None:
    """Replay the ordinary route and return its final state and all heads."""

    if not moves:
        return None
    current = snake
    heads: list[Coordinate] = []
    previous: Direction | None = None
    for position, direction in enumerate(moves):
        transition = physical_legal_transition(
            size=size,
            snake=current,
            food=food,
            direction=direction,
            current_direction=previous,
        )
        if transition is None:
            return None
        current = transition.snake
        heads.append(current[0])
        previous = direction
        if transition.ate_food and position != len(moves) - 1:
            return None
    if current[0] != food or len(set(heads)) != len(heads) or set(heads) & set(snake):
        return None
    return current, tuple(heads)


def _validate_certificate(
    *,
    size: int,
    cycle: tuple[Coordinate, ...],
    snake: tuple[Coordinate, ...],
    food: Coordinate,
    moves: tuple[Direction, ...],
) -> bool:
    """Independently validate shape, ring order, and every growth transition."""

    if not _valid_cycle_shape(size, cycle) or food not in cycle:
        return False
    index = {cell: position for position, cell in enumerate(cycle)}
    if not _preserves_ring_order(snake, index):
        return False

    current = snake
    heads: list[Coordinate] = []
    ate_food = False
    for position, direction in enumerate(moves):
        transition = ring_legal_transition(
            size=size,
            cycle=cycle,
            snake=current,
            food=food,
            direction=direction,
        )
        if transition is None:
            return False
        current = transition.snake
        heads.append(current[0])
        ate_food = transition.ate_food
        if ate_food and position != len(moves) - 1:
            return False
    return (
        bool(moves)
        and len(set(heads)) == len(heads)
        and not set(heads) & set(snake)
        and ate_food
        and current[0] == food
        and _preserves_ring_order(current, index)
    )


def validate_certificate(
    *,
    size: int,
    cycle: tuple[Coordinate, ...],
    snake: tuple[Coordinate, ...],
    food: Coordinate,
    moves: tuple[Direction, ...],
) -> bool:
    """Public replay helper used by callers and focused certificate tests."""

    return _validate_certificate(
        size=size,
        cycle=cycle,
        snake=snake,
        food=food,
        moves=moves,
    )


def _search_ordered_cycle(
    *,
    size: int,
    snake: tuple[Coordinate, ...],
    heads: tuple[Coordinate, ...],
    food: Coordinate,
    moves: tuple[Direction, ...],
    budget: int,
) -> tuple[tuple[Coordinate, ...] | None, int, bool]:
    """Find a full cycle with the fixed head/route/body relative order."""

    cells = _cells(size)
    cell_index = {cell: position for position, cell in enumerate(cells)}
    if any(cell not in cell_index for cell in (*snake, *heads)):
        return None, 0, False
    # The cycle must visit current head, route heads in time order, then the
    # original body from tail back to neck.  Free cells may be interleaved.
    forced = tuple(heads) + tuple(reversed(snake[1:]))
    forced_ids = tuple(cell_index[cell] for cell in forced)
    if len(set((cell_index[snake[0]], *forced_ids))) != 1 + len(forced_ids):
        return None, 0, False

    adjacency = _adjacency(size, cells)
    all_mask = (1 << len(cells)) - 1
    head_id = cell_index[snake[0]]
    head_bit = 1 << head_id
    forced_set = set(forced_ids)
    failed: set[tuple[int, int, int]] = set()
    path = [head_id]
    expanded = 0
    exhausted = False
    validator: Callable[[tuple[Coordinate, ...]], bool] = lambda candidate: _validate_certificate(
        size=size,
        cycle=candidate,
        snake=snake,
        food=food,
        moves=moves,
    )

    def visit(current: int, forced_position: int, remaining: int) -> bool:
        nonlocal expanded, exhausted
        if not remaining:
            if forced_position != len(forced_ids) or not (adjacency[current] & head_bit):
                return False
            candidate = tuple(cells[position] for position in path)
            # Do not trust the search constraints alone.  If a candidate is
            # rejected, leave this terminal state un-memoized so another
            # ordering of free cells can still be checked.
            return validator(candidate)

        key = current, forced_position, remaining
        if key in failed:
            return False
        if expanded >= budget:
            exhausted = True
            return False
        expanded += 1

        available = remaining | (1 << current) | head_bit
        bits = remaining
        while bits:
            bit = bits & -bits
            bits &= bits - 1
            vertex = bit.bit_length() - 1
            if (adjacency[vertex] & available).bit_count() < 2:
                failed.add(key)
                return False

        next_forced = forced_ids[forced_position] if forced_position < len(forced_ids) else None
        choices = []
        neighbor_bits = adjacency[current] & remaining
        while neighbor_bits:
            bit = neighbor_bits & -neighbor_bits
            neighbor_bits &= neighbor_bits - 1
            vertex = bit.bit_length() - 1
            if vertex in forced_set and vertex != next_forced:
                continue
            choices.append(vertex)
        choices.sort(
            key=lambda vertex: (
                0 if vertex == next_forced else 1,
                (adjacency[vertex] & available).bit_count(),
                vertex,
            )
        )
        for vertex in choices:
            path.append(vertex)
            following_position = forced_position + (vertex == next_forced)
            if visit(vertex, following_position, remaining & ~(1 << vertex)):
                return True
            path.pop()
            if exhausted:
                return False
        failed.add(key)
        return False

    found = visit(head_id, 0, all_mask ^ head_bit)
    return (tuple(cells[position] for position in path) if found else None), expanded, exhausted


def find_certified_shortcut(
    *,
    size: int,
    cycle: tuple[Coordinate, ...],
    snake: tuple[Coordinate, ...],
    food: Coordinate,
    baseline_steps: int,
    node_budget: int = 50_000,
    route_budget: int = 12_000,
) -> CertifiedShortcut:
    """Find a shorter ordinary route with a newly certified safety cycle."""

    baseline = int(baseline_steps)
    if size < 2 or baseline < 0:
        return CertifiedShortcut(None, (), 0, "invalid_input", baseline, None)
    if node_budget < 1:
        return CertifiedShortcut(None, (), 0, "certificate_budget_exhausted", baseline, None)
    if route_budget < 1:
        return CertifiedShortcut(None, (), 0, "ordinary_route_unknown", baseline, None)

    start = tuple(snake)
    try:
        ordinary = physical_plan_food(
            size=size,
            snake=start,
            food=food,
            node_budget=route_budget,
        )
    except (TypeError, ValueError):
        return CertifiedShortcut(None, (), 0, "ordinary_route_invalid", baseline, None)
    if ordinary.proof != "proved":
        return CertifiedShortcut(None, (), 0, "ordinary_route_unproved", baseline, None)

    moves = tuple(ordinary.moves)
    selected_steps = len(moves)
    if selected_steps >= baseline:
        return CertifiedShortcut(None, moves, 0, "not_strictly_shorter", baseline, selected_steps)
    replayed = _replay_physical_route(size=size, snake=start, food=food, moves=moves)
    if replayed is None:
        return CertifiedShortcut(None, moves, 0, "route_replay_invalid", baseline, selected_steps)
    _, heads = replayed
    if len(set((*start, *heads))) != len(start) + len(heads):
        return CertifiedShortcut(None, moves, 0, "route_heads_repeat", baseline, selected_steps)

    candidate, expanded, exhausted = _search_ordered_cycle(
        size=size,
        snake=start,
        heads=heads,
        food=food,
        moves=moves,
        budget=node_budget,
    )
    if candidate is not None:
        return CertifiedShortcut(candidate, moves, expanded, "certified_shortcut", baseline, selected_steps)
    reason = "certificate_budget_exhausted" if exhausted else "certificate_not_found"
    return CertifiedShortcut(None, moves, expanded, reason, baseline, selected_steps)


__all__ = [
    "CertifiedShortcut",
    "Coordinate",
    "cycle_id",
    "find_certified_shortcut",
    "validate_certificate",
]
