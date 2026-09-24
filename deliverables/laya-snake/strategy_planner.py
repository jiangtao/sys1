"""Ordinary dynamic-snake planning used by the live strategy mode.

This module intentionally does not import the legacy Hamiltonian planner.  It
models only physical wall/body movement, a moving tail, and growth on food.
The returned route and risk indicators are evidence for the model; the live
engine never replaces a legal model choice with a planner recommendation.
"""

from __future__ import annotations

import heapq
import itertools
from dataclasses import dataclass
from typing import Any, Iterable


Coordinate = tuple[int, int]
NODE_BUDGET = 12_000
DIRECTIONS = ("up", "right", "down", "left")
VECTORS: dict[str, Coordinate] = {
    "up": (0, -1),
    "right": (1, 0),
    "down": (0, 1),
    "left": (-1, 0),
}
OPPOSITE = {"up": "down", "down": "up", "left": "right", "right": "left"}


@dataclass(frozen=True)
class StrategyTransition:
    direction: str
    snake: tuple[Coordinate, ...]
    ate_food: bool


@dataclass(frozen=True)
class StrategyPlan:
    moves: tuple[str, ...]
    proof: str
    expanded: int
    fallback_reason: str | None = None

    @property
    def remaining(self) -> int:
        return len(self.moves)


@dataclass(frozen=True)
class PositionMetrics:
    space: int | None
    exits: int | None
    tail_reachable: bool | None


@dataclass(frozen=True)
class StrategyCandidate:
    direction: str
    food_steps: int | None
    proof: str
    chain: tuple[str, ...]
    space_after: int | None
    exits_after: int | None
    tail_reachable_after: bool | None
    space_after_food: int | None
    exits_after_food: int | None
    tail_reachable_after_food: bool | None
    risk_notes: tuple[str, ...]
    tactic: str
    plan: StrategyPlan

    def as_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "foodSteps": self.food_steps,
            "proof": self.proof,
            "chain": list(self.chain),
            "spaceAfter": self.space_after,
            "spaceMetric": "reachableCells（含头部，尾格视为可腾空）",
            "exitsAfter": self.exits_after,
            "tailReachableAfter": self.tail_reachable_after,
            "spaceAfterFood": self.space_after_food,
            "exitsAfterFood": self.exits_after_food,
            "tailReachableAfterFood": self.tail_reachable_after_food,
            "riskNotes": list(self.risk_notes),
            "tactic": self.tactic,
        }


def _manhattan(cell: Coordinate, food: Coordinate) -> int:
    return abs(cell[0] - food[0]) + abs(cell[1] - food[1])


def legal_transition(
    *,
    size: int,
    snake: Iterable[Coordinate],
    food: Coordinate,
    direction: str,
    current_direction: str | None = None,
) -> StrategyTransition | None:
    """Return a normal physical snake transition, or ``None`` if illegal."""

    if direction not in VECTORS:
        return None
    current = tuple(snake)
    if not current or len(set(current)) != len(current):
        return None
    if len(current) == 1 and current_direction in OPPOSITE and OPPOSITE[current_direction] == direction:
        return None
    dx, dy = VECTORS[direction]
    head = current[0]
    target = head[0] + dx, head[1] + dy
    if not (0 <= target[0] < size and 0 <= target[1] < size):
        return None
    if target in current[1:] and target != current[-1]:
        return None
    ate_food = target == food
    following = (target, *current) if ate_food else (target, *current[:-1])
    if len(set(following)) != len(following):
        return None
    return StrategyTransition(direction, following, ate_food)


def legal_directions(
    *,
    size: int,
    snake: Iterable[Coordinate],
    food: Coordinate,
    current_direction: str | None = None,
) -> list[str]:
    current = tuple(snake)
    return [
        direction
        for direction in DIRECTIONS
        if legal_transition(
            size=size,
            snake=current,
            food=food,
            direction=direction,
            current_direction=current_direction,
        )
        is not None
    ]


def plan_food(
    *,
    size: int,
    snake: Iterable[Coordinate],
    food: Coordinate,
    current_direction: str | None = None,
    node_budget: int = NODE_BUDGET,
) -> StrategyPlan:
    """Bounded A* over complete dynamic snake states."""

    start_snake = tuple(snake)
    if not start_snake:
        raise ValueError("strategy planner requires a non-empty snake")
    if start_snake[0] == food:
        return StrategyPlan((), "proved", 0, None)
    if node_budget < 1:
        return StrategyPlan((), "unknown", 0, "node_budget_exhausted")

    sequence = itertools.count()
    start_key = start_snake, current_direction
    start_h = _manhattan(start_snake[0], food)
    queue: list[tuple[int, int, int, int, tuple[Coordinate, ...], str | None]] = [
        (start_h, start_h, next(sequence), 0, start_snake, current_direction)
    ]
    costs: dict[tuple[tuple[Coordinate, ...], str | None], int] = {start_key: 0}
    parents: dict[
        tuple[tuple[Coordinate, ...], str | None],
        tuple[tuple[tuple[Coordinate, ...], str | None], str] | None,
    ] = {start_key: None}
    expanded = 0
    while queue and expanded < node_budget:
        _, _, _, cost, current, last_direction = heapq.heappop(queue)
        key = current, last_direction
        if cost != costs.get(key):
            continue
        if current[0] == food:
            moves: list[str] = []
            cursor = key
            while parents[cursor] is not None:
                previous, direction = parents[cursor]
                moves.append(direction)
                cursor = previous
            return StrategyPlan(tuple(reversed(moves)), "proved", expanded, None)
        expanded += 1
        for direction in DIRECTIONS:
            transition = legal_transition(
                size=size,
                snake=current,
                food=food,
                direction=direction,
                current_direction=last_direction,
            )
            if transition is None:
                continue
            following_key = transition.snake, direction
            next_cost = cost + 1
            if next_cost >= costs.get(following_key, float("inf")):
                continue
            costs[following_key] = next_cost
            parents[following_key] = key, direction
            heuristic = _manhattan(transition.snake[0], food)
            heapq.heappush(
                queue,
                (next_cost + heuristic, heuristic, next(sequence), next_cost, transition.snake, direction),
            )
    reason = "node_budget_exhausted" if queue else "unreachable"
    return StrategyPlan((), "unknown", expanded, reason)


def position_metrics(
    *,
    size: int,
    snake: Iterable[Coordinate],
    food: Coordinate,
    current_direction: str | None = None,
) -> PositionMetrics:
    """Estimate immediate breathing room and static tail connectivity."""

    body = tuple(snake)
    if not body or len(set(body)) != len(body):
        return PositionMetrics(None, None, None)
    exits = len(legal_directions(size=size, snake=body, food=food, current_direction=current_direction))
    blocked = set(body[1:-1])
    start = body[0]
    queue = [start]
    visited = {start}
    while queue:
        x, y = queue.pop(0)
        for dx, dy in VECTORS.values():
            target = x + dx, y + dy
            if not (0 <= target[0] < size and 0 <= target[1] < size):
                continue
            if target in blocked or target in visited:
                continue
            visited.add(target)
            queue.append(target)
    tail_reachable = body[-1] in visited
    return PositionMetrics(len(visited), exits, tail_reachable)


def _simulate_route(
    *, size: int, snake: tuple[Coordinate, ...], food: Coordinate, first_direction: str, plan: StrategyPlan
) -> tuple[Coordinate, ...] | None:
    current = snake
    transition = legal_transition(size=size, snake=current, food=food, direction=first_direction)
    if transition is None:
        return None
    current = transition.snake
    if transition.ate_food:
        return current
    last = first_direction
    for direction in plan.moves:
        transition = legal_transition(
            size=size,
            snake=current,
            food=food,
            direction=direction,
            current_direction=last,
        )
        if transition is None:
            return None
        current = transition.snake
        last = direction
        if transition.ate_food:
            return current
    return current if current[0] == food else None


def evaluate_candidates(
    *,
    size: int,
    snake: Iterable[Coordinate],
    food: Coordinate,
    current_direction: str | None = None,
    node_budget: int = NODE_BUDGET,
) -> list[StrategyCandidate]:
    """Evaluate every immediately legal first move without ranking them."""

    current = tuple(snake)
    results: list[StrategyCandidate] = []
    for direction in legal_directions(
        size=size,
        snake=current,
        food=food,
        current_direction=current_direction,
    ):
        transition = legal_transition(
            size=size,
            snake=current,
            food=food,
            direction=direction,
            current_direction=current_direction,
        )
        assert transition is not None
        after = position_metrics(size=size, snake=transition.snake, food=food, current_direction=direction)
        if transition.ate_food:
            plan = StrategyPlan((), "proved", 0, None)
            after_food = after
        else:
            plan = plan_food(
                size=size,
                snake=transition.snake,
                food=food,
                current_direction=direction,
                node_budget=node_budget,
            )
            final_snake = (
                _simulate_route(size=size, snake=current, food=food, first_direction=direction, plan=plan)
                if plan.proof == "proved"
                else None
            )
            after_food = (
                position_metrics(size=size, snake=final_snake, food=food, current_direction=plan.moves[-1] if plan.moves else direction)
                if final_snake is not None
                else PositionMetrics(None, None, None)
            )
        food_steps = 1 if transition.ate_food else (1 + plan.remaining if plan.proof == "proved" else None)
        notes: list[str] = []
        if after.space is not None and after.space <= len(transition.snake) + 2:
            notes.append("执行后可达空间偏紧")
        if after.exits is not None and after.exits <= 1:
            notes.append("执行后出口偏少")
        if after.tail_reachable is False:
            notes.append("执行后静态尾连通性为否")
        if after_food.exits == 0:
            notes.append("进食后没有立即出口")
        if after_food.tail_reachable is False:
            notes.append("进食后静态尾连通性为否")
        if plan.proof == "unknown":
            notes.append("到 food 的真实步数未证明")
        if not notes:
            notes.append("保留普通蛇身可行走空间")
        target = transition.snake[0]
        old_tail = current[-1]
        before_metrics = position_metrics(size=size, snake=current, food=food, current_direction=current_direction)
        if transition.ate_food:
            tactic = "进食"
        elif target == old_tail:
            tactic = "跟随尾部"
        elif (
            before_metrics.space is not None
            and after.space is not None
            and after.space > before_metrics.space
        ) or (
            before_metrics.exits is not None
            and after.exits is not None
            and after.exits > before_metrics.exits
        ):
            tactic = "扩大空间"
        elif _manhattan(target, food) < _manhattan(current[0], food):
            tactic = "接近食物"
        else:
            tactic = "调整位置"
        results.append(
            StrategyCandidate(
                direction=direction,
                food_steps=food_steps,
                proof=plan.proof,
                chain=(direction, *plan.moves[:5]),
                space_after=after.space,
                exits_after=after.exits,
                tail_reachable_after=after.tail_reachable,
                space_after_food=after_food.space,
                exits_after_food=after_food.exits,
                tail_reachable_after_food=after_food.tail_reachable,
                risk_notes=tuple(notes),
                tactic=tactic,
                plan=plan,
            )
        )
    return results


__all__ = [
    "NODE_BUDGET",
    "PositionMetrics",
    "StrategyCandidate",
    "StrategyPlan",
    "StrategyTransition",
    "evaluate_candidates",
    "legal_directions",
    "legal_transition",
    "plan_food",
    "position_metrics",
]
