"""Bounded, safety-constrained food routing for the snake engine.

This module is deliberately independent from ``engine.py`` so the search can
simulate complete snake states without mutating a live game.  A proved route
is shortest in actual grid moves under the same ring-order safety invariant
used by the game.  When the bounded search cannot prove a route, the planner
returns a ring-successor fallback marked ``unproved``.
"""

from __future__ import annotations

import heapq
import itertools
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable


Coordinate = tuple[int, int]
DEFAULT_NODE_BUDGET = 12_000

DIRECTION_VECTORS: dict[str, Coordinate] = {
    "up": (0, -1),
    "right": (1, 0),
    "down": (0, 1),
    "left": (-1, 0),
}
DIRECTION_ORDER = {direction: index for index, direction in enumerate(DIRECTION_VECTORS)}


@dataclass(frozen=True)
class Transition:
    """A legal, immutable one-step simulation result."""

    direction: str
    snake: tuple[Coordinate, ...]
    ate_food: bool


@dataclass(frozen=True)
class RoutePlan:
    """A route and the strength of the evidence supporting it."""

    moves: tuple[str, ...]
    proof: str
    expanded: int
    fallback_reason: str | None = None

    @property
    def remaining(self) -> int:
        return len(self.moves)

    @property
    def next_direction(self) -> str | None:
        return self.moves[0] if self.moves else None


@dataclass(frozen=True)
class RouteCache:
    """Cache key plus the route suffix that is still executable."""

    food: Coordinate
    level: int
    snake: tuple[Coordinate, ...]
    moves: tuple[str, ...]
    proof: str
    expanded: int
    fallback_reason: str | None = None

    @classmethod
    def from_plan(
        cls,
        *,
        food: Coordinate,
        level: int,
        snake: Iterable[Coordinate],
        plan: RoutePlan,
    ) -> "RouteCache":
        return cls(
            food=food,
            level=level,
            snake=tuple(snake),
            moves=tuple(plan.moves),
            proof=plan.proof,
            expanded=plan.expanded,
            fallback_reason=plan.fallback_reason,
        )

    def matches(self, *, food: Coordinate, level: int, snake: Iterable[Coordinate]) -> bool:
        return (
            self.food == food
            and self.level == level
            and self.snake == tuple(snake)
            and self.proof in {"proved", "unproved"}
            and len(self.moves) >= 1
        )

    @property
    def next_direction(self) -> str | None:
        return self.moves[0] if self.moves else None

    def as_plan(self) -> RoutePlan:
        return RoutePlan(self.moves, self.proof, self.expanded, self.fallback_reason)


def _direction_between(current: Coordinate, following: Coordinate) -> str:
    dx = following[0] - current[0]
    dy = following[1] - current[1]
    for direction, vector in DIRECTION_VECTORS.items():
        if vector == (dx, dy):
            return direction
    raise ValueError(f"非相邻格点无法形成方向：{current!r} -> {following!r}")


def _preserves_ring_order(
    snake: tuple[Coordinate, ...], cycle_index: dict[Coordinate, int], cycle_size: int
) -> bool:
    head_index = cycle_index[snake[0]]
    distances = [(cycle_index[cell] - head_index) % cycle_size for cell in snake]
    return distances[0] == 0 and all(left > right for left, right in zip(distances[1:], distances[2:]))


def legal_transition(
    *,
    size: int,
    cycle: tuple[Coordinate, ...],
    snake: Iterable[Coordinate],
    food: Coordinate,
    direction: str,
) -> Transition | None:
    """Simulate one move under the engine's exact safety constraints."""

    if direction not in DIRECTION_VECTORS:
        return None
    current = tuple(snake)
    if not current or len(set(current)) != len(current):
        return None
    cycle_index = {cell: index for index, cell in enumerate(cycle)}
    head = current[0]
    dx, dy = DIRECTION_VECTORS[direction]
    target = (head[0] + dx, head[1] + dy)
    if not (0 <= target[0] < size and 0 <= target[1] < size) or target not in cycle_index:
        return None

    target_is_tail = target == current[-1]
    if target in current[1:] and not target_is_tail:
        return None
    cycle_size = len(cycle)
    jump = (cycle_index[target] - cycle_index[head]) % cycle_size
    tail_distance = (cycle_index[current[-1]] - cycle_index[head]) % cycle_size
    food_distance = (cycle_index[food] - cycle_index[head]) % cycle_size
    if jump == 0 or jump > tail_distance:
        return None
    if jump == tail_distance and not target_is_tail:
        return None
    if 0 < food_distance < tail_distance and jump > food_distance:
        return None

    ate_food = target == food
    following = (target, *current) if ate_food else (target, *current[:-1])
    if len(set(following)) != len(following):
        return None
    if not _preserves_ring_order(following, cycle_index, cycle_size):
        return None
    return Transition(direction=direction, snake=following, ate_food=ate_food)


def _manhattan(cell: Coordinate, food: Coordinate) -> int:
    return abs(cell[0] - food[0]) + abs(cell[1] - food[1])


@lru_cache(maxsize=128)
def _cycle_lower_bounds(cycle: tuple[Coordinate, ...], food: Coordinate) -> tuple[int, ...]:
    """Exact distances in the relaxed, body-free forward-cycle grid graph.

    Every legal move advances toward food without passing it in cycle order.
    Removing body/tail constraints gives a DAG containing all legal edges, so
    its distances are admissible and consistent even while the tail moves.
    The cache includes the entire cycle: a certified cycle switch cannot reuse
    distances from the old topology.
    """
    index = {point: i for i, point in enumerate(cycle)}
    food_index = index[food]
    count = len(cycle)
    bounds = [0] * count
    for distance in range(1, count):
        current_index = (food_index - distance) % count
        x, y = cycle[current_index]
        successors = [
            index[point]
            for dx, dy in DIRECTION_VECTORS.values()
            if (point := (x + dx, y + dy)) in index
            and (food_index - index[point]) % count < distance
        ]
        if not successors:
            raise ValueError("安全环无法构造通往食物的距离下界")
        bounds[current_index] = 1 + min(bounds[i] for i in successors)
    return tuple(bounds)


def _fallback_ring_route(
    *,
    size: int,
    cycle: tuple[Coordinate, ...],
    snake: tuple[Coordinate, ...],
    food: Coordinate,
    expanded: int,
    reason: str,
) -> RoutePlan:
    cycle_index = {cell: index for index, cell in enumerate(cycle)}
    current = snake
    moves: list[str] = []
    max_steps = len(cycle) + len(snake) + 1
    for _ in range(max_steps):
        if current[0] == food:
            return RoutePlan(tuple(moves), "unproved", expanded, reason)
        next_cell = cycle[(cycle_index[current[0]] + 1) % len(cycle)]
        direction = _direction_between(current[0], next_cell)
        transition = legal_transition(
            size=size,
            cycle=cycle,
            snake=current,
            food=food,
            direction=direction,
        )
        if transition is None:
            break
        moves.append(direction)
        current = transition.snake
        if transition.ate_food:
            return RoutePlan(tuple(moves), "unproved", expanded, reason)
    raise RuntimeError("Hamiltonian 环后继无法构造到当前食物的安全回退路径")


def _compact_plan(
    *, size: int, cycle: tuple[Coordinate, ...], start: tuple[Coordinate, ...],
    food: Coordinate, node_budget: int, cycle_index: dict[Coordinate, int],
    lower_bounds: tuple[int, ...],
) -> RoutePlan | None:
    """Search exact (head, elapsed) states under the forward-ring invariant.

    Before food, the head never passes food or revisits a newly created body
    cell. Only the initial body's tail-release schedule can block its advance.
    Thus paths reaching the same head at the same time have identical legal
    futures. Unsupported/invalid input retains the original full-state search.
    """
    if (len(start) < 2 or len(set(start)) != len(start) or food in start
            or any(point not in cycle_index for point in start)
            or not _preserves_ring_order(start, cycle_index, len(cycle))):
        return None
    count, start_index = len(cycle), cycle_index[start[0]]
    food_distance = (cycle_index[food] - start_index) % count
    body_positions = tuple((cycle_index[point] - start_index) % count for point in start)
    first = (start_index, 0)
    h = lower_bounds[start_index]
    sequence = itertools.count()
    queue = [(h, h, next(sequence), first)]
    parents: dict[tuple[int, int], tuple[tuple[int, int], str] | None] = {first: None}
    expanded = 0
    while queue and expanded < node_budget:
        _, _, _, key = heapq.heappop(queue)
        head_index, elapsed = key
        if cycle[head_index] == food:
            moves = []
            cursor = key
            while parents[cursor] is not None:
                cursor, direction = parents[cursor]
                moves.append(direction)
            moves.reverse()
            current = start
            for direction in moves:
                transition = legal_transition(size=size, cycle=cycle, snake=current, food=food, direction=direction)
                if transition is None:
                    raise RuntimeError("压缩规划路线未通过完整蛇体复验")
                current = transition.snake
            if current[0] != food or len(current) != len(start) + 1:
                raise RuntimeError("压缩规划进食末态校验失败")
            return RoutePlan(tuple(moves), "proved", expanded, None)
        expanded += 1
        head_position = (head_index - start_index) % count
        tail_limit = body_positions[len(start) - 1 - elapsed] if elapsed < len(start) - 1 else count
        x, y = cycle[head_index]
        for direction, (dx, dy) in DIRECTION_VECTORS.items():
            point = (x + dx, y + dy)
            if not (0 <= point[0] < size and 0 <= point[1] < size) or point not in cycle_index:
                continue
            following_index = cycle_index[point]
            position = (following_index - start_index) % count
            if not head_position < position <= min(tail_limit, food_distance):
                continue
            following = (following_index, elapsed + 1)
            if following in parents:
                continue
            parents[following] = key, direction
            heuristic = lower_bounds[following_index]
            heapq.heappush(queue, (elapsed + 1 + heuristic, heuristic, next(sequence), following))
    return _fallback_ring_route(size=size, cycle=cycle, snake=start, food=food, expanded=expanded,
                                reason="node_budget_exhausted" if queue else "unreachable")


def plan_food(
    *,
    size: int,
    cycle: tuple[Coordinate, ...],
    snake: Iterable[Coordinate],
    food: Coordinate,
    node_budget: int = DEFAULT_NODE_BUDGET,
) -> RoutePlan:
    """Find a shortest safe route, or return a verified-but-unproved fallback."""

    start = tuple(snake)
    if not start:
        raise ValueError("规划需要非空蛇体")
    if start[0] == food:
        return RoutePlan((), "proved", 0, None)
    if node_budget < 1:
        return _fallback_ring_route(
            size=size,
            cycle=cycle,
            snake=start,
            food=food,
            expanded=0,
            reason="node_budget_exhausted",
        )

    cycle_index = {cell: index for index, cell in enumerate(cycle)}
    lower_bounds = _cycle_lower_bounds(cycle, food)
    compact = _compact_plan(size=size, cycle=cycle, start=start, food=food, node_budget=node_budget,
                            cycle_index=cycle_index, lower_bounds=lower_bounds)
    if compact is not None:
        return compact
    sequence = itertools.count()
    queue: list[tuple[int, int, int, int, tuple[Coordinate, ...]]] = []
    start_heuristic = lower_bounds[cycle_index[start[0]]]
    heapq.heappush(queue, (start_heuristic, start_heuristic, next(sequence), 0, start))
    costs: dict[tuple[Coordinate, ...], int] = {start: 0}
    parents: dict[tuple[Coordinate, ...], tuple[tuple[Coordinate, ...], str] | None] = {start: None}
    expanded = 0

    while queue and expanded < node_budget:
        _, _, _, cost, current = heapq.heappop(queue)
        if cost != costs.get(current):
            continue
        if current[0] == food:
            moves: list[str] = []
            cursor = current
            while parents[cursor] is not None:
                previous, direction = parents[cursor]
                moves.append(direction)
                cursor = previous
            return RoutePlan(tuple(reversed(moves)), "proved", expanded, None)

        expanded += 1
        for direction in DIRECTION_VECTORS:
            transition = legal_transition(
                size=size,
                cycle=cycle,
                snake=current,
                food=food,
                direction=direction,
            )
            if transition is None:
                continue
            following = transition.snake
            next_cost = cost + 1
            if next_cost >= costs.get(following, float("inf")):
                continue
            costs[following] = next_cost
            parents[following] = current, direction
            heuristic = lower_bounds[cycle_index[following[0]]]
            heapq.heappush(queue, (next_cost + heuristic, heuristic, next(sequence), next_cost, following))

    reason = "node_budget_exhausted" if queue else "unreachable"
    return _fallback_ring_route(
        size=size,
        cycle=cycle,
        snake=start,
        food=food,
        expanded=expanded,
        reason=reason,
    )


__all__ = [
    "DEFAULT_NODE_BUDGET",
    "DIRECTION_VECTORS",
    "RouteCache",
    "RoutePlan",
    "Transition",
    "legal_transition",
    "plan_food",
]
