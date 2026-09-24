"""Deterministic, model-directed 2D snake game engine.

The planner owns survival constraints and verified routes. Laya proposes a
candidate; the guarded controller accepts an equally short verified route or
executes the planner fallback, recording the two contributions separately.
"""

from __future__ import annotations

import copy
import json
import math
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Mapping
from uuid import uuid4

try:
    from .context_codec import compress_context, expand_context
    from .future_planner import future_food_cost
    from .certified_routes import cycle_id, find_certified_shortcut, validate_certificate
except ImportError:
    from context_codec import compress_context, expand_context
    from future_planner import future_food_cost
    from certified_routes import cycle_id, find_certified_shortcut, validate_certificate

try:
    from .planner import DEFAULT_NODE_BUDGET, RouteCache, RoutePlan, legal_transition, plan_food
except ImportError:  # Running ``python engine.py`` from this directory.
    from planner import DEFAULT_NODE_BUDGET, RouteCache, RoutePlan, legal_transition, plan_food

try:
    from .strategy_planner import (
        NODE_BUDGET as STRATEGY_NODE_BUDGET,
        StrategyCandidate,
        StrategyPlan,
        evaluate_candidates,
        legal_directions as strategy_legal_directions,
        legal_transition as strategy_legal_transition,
        plan_food as strategy_plan_food,
        position_metrics as strategy_position_metrics,
    )
except ImportError:
    from strategy_planner import (
        NODE_BUDGET as STRATEGY_NODE_BUDGET,
        StrategyCandidate,
        StrategyPlan,
        evaluate_candidates,
        legal_directions as strategy_legal_directions,
        legal_transition as strategy_legal_transition,
        plan_food as strategy_plan_food,
        position_metrics as strategy_position_metrics,
    )


Coordinate = tuple[int, int]
Decider = Callable[[dict[str, Any], dict[str, dict[str, Any]]], Mapping[str, Any]]

BOARD_LEVELS = (
    (14, 8, 7, 145, "空间压缩"),
    (14, 11, 8, 125, "贴身穿行"),
    (14, 15, 9, 105, "极限回路"),
    (12, 12, 10, 90, "迷你迷宫"),
    (12, 18, 11, 78, "蛇影交错"),
    (12, 24, 12, 66, "寸步千钧"),
    (12, 30, 14, 55, "蛇阵突围"),
    (12, 36, 16, 48, "密网突围"),
    (12, 42, 16, 42, "绝境穿行"),
    (12, 48, 15, 36, "极限满盘"),
)
MAX_LEVEL = len(BOARD_LEVELS)
MAX_DECISIONS = 10_000
MAX_HEALTH = 100
MAX_ENERGY = 100

DIRECTION_VECTORS: dict[str, Coordinate] = {
    "up": (0, -1),
    "right": (1, 0),
    "down": (0, 1),
    "left": (-1, 0),
}
DIRECTION_LABELS = {
    "up": "向上",
    "right": "向右",
    "down": "向下",
    "left": "向左",
}
DIRECTION_ORDER = {direction: index for index, direction in enumerate(DIRECTION_VECTORS)}
DIRECTION_CHARS = {"up": "U", "right": "R", "down": "D", "left": "L"}


def _body_links(snake: list[Coordinate]) -> str:
    """Lossless head-to-tail links; these describe the body, not future moves."""
    by_vector = {vector: DIRECTION_CHARS[direction] for direction, vector in DIRECTION_VECTORS.items()}
    runs: list[tuple[str, int]] = []
    for first, following in zip(snake, snake[1:]):
        letter = by_vector[(following[0] - first[0], following[1] - first[1])]
        if runs and runs[-1][0] == letter:
            runs[-1] = letter, runs[-1][1] + 1
        else:
            runs.append((letter, 1))
    return " ".join(f"{letter}{count}" for letter, count in runs) or "-"


@lru_cache(maxsize=1)
def _context_tokenizer() -> Any | None:
    try:
        from tokenizers import Tokenizer

        path = Path(__file__).resolve().parent.parent / "laya-zh" / "model" / "tokenizer" / "tokenizer.json"
        return Tokenizer.from_file(str(path)) if path.is_file() else None
    except Exception:
        return None


def _estimate_context_tokens(state: Any, questions: dict[str, Any]) -> int | None:
    tokenizer = _context_tokenizer()
    if tokenizer is None:
        return None
    question = questions.get("direction", {})
    instruction = str(question.get("instructions", "")).replace("<mask>", " ")
    criteria = question.get("criteria", {})
    mask_id = tokenizer.token_to_id("<mask>") or 4
    options = [
        [mask_id]
        + tokenizer.encode(
            " " + f"{key}: {value}".replace("<mask>", " "),
            add_special_tokens=False,
        ).ids[:48]
        for key, value in criteria.items()
    ]
    head_max_len = 256
    option_budget = head_max_len - sum(len(option) for option in options)
    if option_budget < 16:
        per = max(4, (head_max_len - 16) // max(1, len(options)))
        options = [option[:per] for option in options]
        option_budget = head_max_len - sum(len(option) for option in options)
    head = tokenizer.encode(
        f"choice question: {instruction}", add_special_tokens=False
    ).ids[: max(8, option_budget)]
    prefix = 1 + len(head) + 1 + sum(len(option) for option in options) + 1
    state_text = json.dumps(state, ensure_ascii=False) if isinstance(state, (dict, list)) else str(state)
    state_tokens = len(
        tokenizer.encode(state_text.replace("<mask>", " "), add_special_tokens=False).ids
    )
    return prefix + state_tokens + 1


@dataclass(frozen=True)
class LevelConfig:
    """Static difficulty parameters for one level."""

    size: int
    initial_length: int
    target: int
    interval_ms: int
    name: str


LEVEL_CONFIGS = tuple(LevelConfig(*values) for values in BOARD_LEVELS)


class ModelDecisionError(RuntimeError):
    """Raised when Laya returns a response that cannot safely be executed."""


class ModelUnavailableError(RuntimeError):
    """Raised when the Laya service cannot produce a decision."""


class GameFinishedError(RuntimeError):
    """Raised when a step is requested after the final level is won."""


def build_hamiltonian_cycle(size: int) -> tuple[Coordinate, ...]:
    """Return a Hamiltonian cycle for an even square board.

    The first row is traversed left-to-right, the remaining columns are
    traversed in alternating vertical directions, and the left column closes
    the cycle back to the first cell.  Every consecutive pair, including the
    final-to-first pair, is orthogonally adjacent.
    """

    if size < 2 or size % 2:
        raise ValueError("Hamiltonian 方形回路要求尺寸为不小于 2 的偶数")

    path: list[Coordinate] = [(x, 0) for x in range(size)]
    for x in range(size - 1, 0, -1):
        y_values = range(1, size) if x % 2 else range(size - 1, 0, -1)
        path.extend((x, y) for y in y_values)
    path.extend((0, y) for y in range(size - 1, 0, -1))

    if len(path) != size * size or len(set(path)) != len(path):
        raise RuntimeError("Hamiltonian 回路生成失败：格点重复或缺失")
    for current, following in zip(path, (*path[1:], path[0])):
        if abs(current[0] - following[0]) + abs(current[1] - following[1]) != 1:
            raise RuntimeError("Hamiltonian 回路生成失败：存在非相邻格点")
    return tuple(path)


def _direction_between(current: Coordinate, following: Coordinate) -> str:
    dx = following[0] - current[0]
    dy = following[1] - current[1]
    for direction, vector in DIRECTION_VECTORS.items():
        if vector == (dx, dy):
            return direction
    raise RuntimeError(f"无法推导相邻格点方向：{current!r} -> {following!r}")


def _as_point(point: Coordinate) -> list[int]:
    return [point[0], point[1]]


def _manhattan(first: Coordinate, second: Coordinate) -> int:
    return abs(first[0] - second[0]) + abs(first[1] - second[1])


def _safe_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or not 0 <= number <= 1:
        return None
    return number


class SnakeGame:
    """A single ten-level game session.

    Real choices are proposed by Laya and guarded by the verified planner.
    Unique safe moves bypass the model. A staged safety-cycle change and its
    first executed move commit together; engine exceptions restore the state.
    Model failures are recorded and use the already-verified planner route.
    """

    def __init__(self, seed: int | None = None, policy_mode: str = "survival", *, lookahead_enabled: bool = False, certified_shortcuts: bool = True, compact_context: bool = True, midroute_shortcuts: bool = True, skip_deterministic_model: bool = True, model_provider: str = "laya", neutral_execution_source: bool | None = None) -> None:
        if policy_mode not in {"survival", "strict_shortest"}:
            raise ValueError("policy_mode 必须是 survival 或 strict_shortest")
        if model_provider not in {"laya", "jev", "kev"}:
            raise ValueError("model_provider 必须是 laya、jev 或 kev")
        self.seed = seed if seed is not None else random.SystemRandom().randrange(2**32)
        self.policy_mode = policy_mode
        self.model_provider = model_provider
        self.neutral_execution_source = False if neutral_execution_source is None else neutral_execution_source
        self.lookahead_enabled = lookahead_enabled
        self.certified_shortcuts = certified_shortcuts
        self.compact_context = compact_context
        self.midroute_shortcuts = midroute_shortcuts
        self.skip_deterministic_model = skip_deterministic_model
        self._straight_probe_goal: tuple[int, int] | None = None
        self._straight_probe_attempts = 0
        self._random = random.Random(self.seed)
        self._level = 1
        self._steps = 0
        self._total_eaten = 0
        self._level_eaten = 0
        self._health = MAX_HEALTH
        self._energy = MAX_ENERGY
        self._loss_reason: str | None = None
        self._status = "playing"
        self._cycle: tuple[Coordinate, ...] = ()
        self._cycle_index: dict[Coordinate, int] = {}
        self._snake: list[Coordinate] = []
        self._food: Coordinate = (0, 0)
        self._direction = "right"
        self._logs: list[dict[str, Any]] = []
        self._logs_truncated = 0
        self._planner_cache: RouteCache | None = None
        self._planner_phase = "PLAN_FOOD"
        self._recent_feedback: list[dict[str, Any]] = []
        self._strategy_phase = "CHOOSE_TACTIC"
        self._strategy_no_food_steps = 0
        self._strategy_seen: dict[tuple[int, Coordinate, tuple[Coordinate, ...]], int] = {}
        self._initialize_level(1)

    @property
    def status(self) -> str:
        return self._status

    @property
    def level(self) -> int:
        return self._level

    @property
    def steps(self) -> int:
        return self._steps

    @property
    def total_eaten(self) -> int:
        return self._total_eaten

    def _config(self, level: int | None = None) -> LevelConfig:
        selected = self._level if level is None else level
        return LEVEL_CONFIGS[selected - 1]

    def _initialize_level(self, level: int) -> None:
        config = self._config(level)
        self._level = level
        self._level_eaten = 0
        self._status = "playing"
        self._planner_cache = None
        self._planner_phase = "PLAN_FOOD"
        self._strategy_phase = "CHOOSE_TACTIC"
        self._strategy_no_food_steps = 0
        self._cycle = build_hamiltonian_cycle(config.size)
        self._cycle_index = {coordinate: index for index, coordinate in enumerate(self._cycle)}
        start = self._random.randrange(len(self._cycle))
        self._snake = [self._cycle[(start - offset) % len(self._cycle)] for offset in range(config.initial_length)]
        self._food = self._spawn_food()
        next_cell = self._cycle[(start + 1) % len(self._cycle)]
        self._direction = _direction_between(self._snake[0], next_cell)
        self._strategy_seen = {(self._level, self._food, tuple(self._snake)): 1}

    def _spawn_food(self) -> Coordinate:
        occupied = set(self._snake)
        # Safety cycles may change; food RNG must keep its canonical ordering.
        available = [coordinate for coordinate in build_hamiltonian_cycle(self._config().size) if coordinate not in occupied]
        if not available:
            raise RuntimeError("棋盘没有可放置食物的空格")
        return self._random.choice(available)

    def _cycle_distance(self, start: Coordinate, end: Coordinate) -> int:
        return (self._cycle_index[end] - self._cycle_index[start]) % len(self._cycle)

    def _preserves_ring_order(self, next_snake: list[Coordinate]) -> bool:
        """Check that the body remains ordered behind the new head on the ring."""

        if not next_snake:
            return False
        head_index = self._cycle_index[next_snake[0]]
        distances = [
            (self._cycle_index[part] - head_index) % len(self._cycle)
            for part in next_snake
        ]
        if distances[0] != 0:
            return False
        return all(left > right for left, right in zip(distances[1:], distances[2:]))

    def _compute_strategy_options(self) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        """Expose all immediately legal ordinary-snake moves for live mode."""

        if self._status in {"won", "lost"}:
            return [], []
        candidates: list[dict[str, Any]] = []
        blocked: list[dict[str, str]] = []
        head = self._snake[0]
        for direction, (dx, dy) in DIRECTION_VECTORS.items():
            target = head[0] + dx, head[1] + dy
            transition = strategy_legal_transition(
                size=self._config().size,
                snake=self._snake,
                food=self._food,
                direction=direction,
                current_direction=self._direction,
            )
            if transition is None:
                if not (0 <= target[0] < self._config().size and 0 <= target[1] < self._config().size):
                    blocked.append({"direction": direction, "reason": "下一格越过棋盘边界", "kind": "wall"})
                else:
                    blocked.append({"direction": direction, "reason": "下一格被蛇身占据或立即回头", "kind": "body"})
                continue
            cycle_distance = self._cycle_distance(head, target) if target in self._cycle_index else 0
            candidates.append(
                {
                    "direction": direction,
                    "safe": True,
                    "distance": cycle_distance,
                    "space": None,
                    "reason": "普通蛇身物理合法方向；真实 food 路线由动态规划特征另行评估。",
                }
            )
        return candidates, blocked

    def _compute_move_options(self) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        """Compute safe moves and a factual explanation for every blocked move."""

        if self._status in {"won", "lost"}:
            return [], []

        head = self._snake[0]
        tail_distance = self._cycle_distance(head, self._snake[-1])
        food_distance = self._cycle_distance(head, self._food)
        food_is_ahead = 0 < food_distance < tail_distance
        candidates: list[dict[str, Any]] = []
        blocked: list[dict[str, str]] = []
        board_size = self._config().size

        def add_blocked(direction: str, reason: str, kind: str) -> None:
            blocked.append({"direction": direction, "reason": reason, "kind": kind})

        for direction, (dx, dy) in DIRECTION_VECTORS.items():
            target = (head[0] + dx, head[1] + dy)
            if not (0 <= target[0] < board_size and 0 <= target[1] < board_size):
                add_blocked(direction, "下一格越过棋盘边界", "wall")
                continue

            target_is_tail = target == self._snake[-1]
            if target in self._snake[1:] and not target_is_tail:
                add_blocked(direction, "下一格被蛇身占据", "body")
                continue

            cycle_distance = self._cycle_distance(head, target)
            if food_is_ahead and cycle_distance > food_distance:
                add_blocked(direction, f"会越过前方食物（环距 {food_distance} 格）", "food")
                continue
            if cycle_distance == 0 or cycle_distance > tail_distance:
                add_blocked(direction, "会越过尾部安全边界", "order")
                continue
            if cycle_distance == tail_distance and not target_is_tail:
                add_blocked(direction, "不在蛇体环序允许的前进区间", "order")
                continue

            eating = target == self._food
            next_snake = [target, *self._snake] if eating else [target, *self._snake[:-1]]
            if len(set(next_snake)) != len(next_snake):
                add_blocked(direction, "执行后会与蛇身重叠", "body")
                continue
            if not self._preserves_ring_order(next_snake):
                add_blocked(direction, "会打乱蛇体沿环顺序", "order")
                continue

            space = max(0, tail_distance - cycle_distance - 1)
            distance_to_food = 0 if eating else self._cycle_distance(target, self._food)
            candidates.append(
                {
                    "direction": direction,
                    "safe": True,
                    "distance": distance_to_food,
                    "space": space,
                    "_cycleDistance": cycle_distance,
                    "_isBaseline": cycle_distance == 1,
                }
            )

        candidates.sort(key=lambda item: (item["distance"], -item["space"], DIRECTION_ORDER[item["direction"]]))
        best_distance = min((candidate["distance"] for candidate in candidates), default=0)
        baseline = next((candidate for candidate in candidates if candidate["_isBaseline"]), None)
        baseline_distance = baseline["distance"] if baseline is not None else best_distance
        for candidate in candidates:
            extra_distance = candidate["distance"] - best_distance
            shortcut_saved = max(0, baseline_distance - candidate["distance"])
            candidate["reason"] = (
                f"引擎特征：环序参考距 {candidate['distance']} 格，"
                f"相对环序参考差值 {extra_distance} 格，可用空间 {candidate['space']} 格，"
                f"相对环序后继参考差值 {shortcut_saved} 格；"
                "已通过边界、蛇身、尾部和食物顺序检查（不是实际移动步数，非模型生成推理）。"
            )
            candidate.pop("_cycleDistance", None)
            candidate.pop("_isBaseline", None)
        return candidates, blocked

    def _compute_safe_candidates(self) -> list[dict[str, Any]]:
        return self._compute_move_options()[0]

    def _compute_blocked(self) -> list[dict[str, str]]:
        return self._compute_move_options()[1]

    def safe_candidates(self) -> list[dict[str, Any]]:
        """Return immediately legal ordinary-snake directions for live mode."""

        return copy.deepcopy(self._compute_strategy_options()[0])

    def legacy_safe_candidates(self) -> list[dict[str, Any]]:
        """Return legacy Hamiltonian candidates for historical replay only."""

        return copy.deepcopy(self._compute_safe_candidates())

    def _current_cache_matches(self) -> bool:
        return self._planner_cache is not None and self._planner_cache.matches(
            food=self._food,
            level=self._level,
            snake=self._snake,
        )

    def _compute_food_plan(self, snake: tuple[Coordinate, ...] | None = None) -> RoutePlan:
        return plan_food(
            size=self._config().size,
            cycle=self._cycle,
            snake=self._snake if snake is None else snake,
            food=self._food,
            node_budget=DEFAULT_NODE_BUDGET,
        )

    def _planner_state(self) -> dict[str, Any]:
        if self._status == "won":
            return {"phase": "DONE", "proof": None, "remainingSteps": None, "nextDirection": None}
        if self._status == "lost":
            return {"phase": "LOST", "proof": None, "remainingSteps": None, "nextDirection": None}
        cache = self._planner_cache if self._current_cache_matches() else None
        plan = cache.as_plan() if cache else None
        return {"phase": self._planner_phase, "proof": plan.proof if plan else None,
                "remainingSteps": plan.remaining if plan else None,
                "nextDirection": plan.next_direction if plan else None}

    @staticmethod
    def _planner_request_view(plan: RoutePlan, phase: str) -> dict[str, Any]:
        return {
            "phase": phase,
            "proof": plan.proof,
            "remainingSteps": plan.remaining,
            "nextDirection": plan.next_direction,
            "suggestedDirection": plan.next_direction,
            "suggestedRemainingSteps": plan.remaining,
            "routeChain": list(plan.moves[:6]),
            "routeChainProof": plan.proof,
            "suggestion": "引擎规划建议，仅用于审计和参考，不是模型生成的推理。",
        }

    def _candidate_plans(
        self,
        candidates: list[dict[str, Any]],
        planned: RoutePlan,
    ) -> tuple[list[dict[str, Any]], dict[str, RoutePlan]]:
        """Summarize real current-food routes for every safe model option."""

        public: list[dict[str, Any]] = []
        plans: dict[str, RoutePlan] = {}
        for candidate in candidates:
            direction = candidate["direction"]
            follows_planned_route = direction == planned.next_direction
            if follows_planned_route:
                branch = planned
            else:
                transition = legal_transition(
                    size=self._config().size,
                    cycle=self._cycle,
                    snake=self._snake,
                    food=self._food,
                    direction=direction,
                )
                if transition is None:
                    # This cannot happen for an engine candidate; retaining a
                    # null plan makes the protocol honest if state is stale.
                    branch = RoutePlan((), "unproved", 0, "candidate_state_changed")
                elif transition.ate_food:
                    branch = RoutePlan((), "proved", 0, None)
                else:
                    branch = self._compute_food_plan(transition.snake)
            # Every entry is a suffix after executing its first direction.
            plans[direction] = RoutePlan(branch.moves[1:], branch.proof, branch.expanded, branch.fallback_reason) if follows_planned_route else branch
            route_steps = (
                branch.remaining if follows_planned_route else 1 + branch.remaining
            ) if branch.proof == "proved" else None
            public.append(
                {
                    "direction": direction,
                    "routeSteps": route_steps,
                    "proof": branch.proof,
                    "chain": list(planned.moves[:6]) if follows_planned_route else [direction, *branch.moves[:5]],
                    "fullRoute": list(planned.moves) if follows_planned_route else [direction, *branch.moves],
                    "engineSuggested": direction == planned.next_direction,
                    "fallbackReason": branch.fallback_reason,
                }
            )
            option = public[-1]
            simulated = tuple(self._snake)
            first_metrics = final_metrics = None
            for index, move in enumerate(option["fullRoute"]):
                transition = strategy_legal_transition(size=self._config().size, snake=simulated, food=self._food, direction=move)
                if transition is None:
                    raise ModelDecisionError("规划器完整路线未通过物理复演")
                simulated = transition.snake
                if index == 0:
                    first_metrics = strategy_position_metrics(size=self._config().size, snake=simulated, food=self._food, current_direction=move)
            if simulated[0] != self._food:
                raise ModelDecisionError("规划器路线没有到达食物")
            final_metrics = strategy_position_metrics(size=self._config().size, snake=simulated, food=self._food, current_direction=option["fullRoute"][-1])
            option.update({"foodSteps": route_steps, "spaceAfter": first_metrics.space,
                           "exitsAfter": first_metrics.exits, "tailReachableAfter": first_metrics.tail_reachable,
                           "spaceAfterFood": final_metrics.space, "exitsAfterFood": final_metrics.exits,
                           "tailReachableAfterFood": final_metrics.tail_reachable,
                           "afterFoodSnake": [_as_point(p) for p in simulated]})
        return public, plans

    def _future_route_reference(self, options: list[dict[str, Any]], suffixes: dict[str, RoutePlan], planned: RoutePlan) -> tuple[RoutePlan, dict[str, Any]]:
        """Compare concrete equal-length routes, never every route of an action."""
        info: dict[str, Any] = {"active": False, "scope": "沿给定完整路线进食后，下一颗均匀随机能量的条件期望；不是全局最优"}
        if not self.lookahead_enabled or planned.proof != "proved" or self._level_eaten + 1 >= self._config().target:
            return planned, info
        tied = [p for p in options if p["proof"] == "proved" and p["routeSteps"] == planned.remaining]
        if len(tied) < 2:
            return planned, info
        started = time.perf_counter()
        for option in tied:
            endpoint = tuple(map(tuple, option["afterFoodSnake"]))
            option["nextFood"] = future_food_cost(size=self._config().size, cycle=self._cycle, snake=endpoint).as_dict()
        info["evaluationMs"] = round((time.perf_counter() - started) * 1000, 2)
        reference = next(p for p in tied if p["direction"] == planned.next_direction)
        info["cachedReference"] = copy.deepcopy(reference["nextFood"])
        if reference["nextFood"]["proof"] != "proved":
            info["reason"] = "缓存路线终态的下一颗期望未证，沿用当前最短规则"
            return planned, info
        known = [p for p in tied if p["nextFood"]["proof"] == "proved"]
        count = reference["nextFood"]["targetCount"]
        if any(p["nextFood"]["targetCount"] != count for p in known):
            raise ModelDecisionError("等长候选的进食后长度不一致")
        minimum = min(p["nextFood"]["totalSteps"] for p in known)
        preferred = [p for p in known if p["nextFood"]["totalSteps"] == minimum]
        if reference not in preferred:
            reference = preferred[0]
        direction = reference["direction"]
        suffix = suffixes[direction]
        selected_plan = RoutePlan((direction, *suffix.moves), suffix.proof, suffix.expanded, suffix.fallback_reason)
        for p in options:
            p["engineSuggested"] = p["direction"] == direction
        info.update(active=True, preferredDirections=[p["direction"] for p in preferred],
                    reference=copy.deepcopy(reference["nextFood"]),
                    referenceChanged=direction != planned.next_direction)
        return selected_plan, info

    def _assign_plan_ids(self, candidate_plans: list[dict[str, Any]]) -> dict[str, str]:
        """Rotate neutral labels by step so model choice is not direction-order biased."""

        directions = [item["direction"] for item in candidate_plans]
        if not directions:
            return {}
        offset = self._steps % len(directions)
        rotated = directions[offset:] + directions[:offset]
        choice_map: dict[str, str] = {}
        by_direction = {item["direction"]: item for item in candidate_plans}
        for index, direction in enumerate(rotated, start=1):
            label = chr(ord('A') + index - 1)
            by_direction[direction]["id"] = label
            choice_map[label] = direction
        return choice_map

    def _penalty_rules(self) -> dict[str, Any]:
        return {
            "policyMode": self.policy_mode,
            "objective": "存活 > 最短路径 > 能量收益；先满足存活约束再比较步数",
            "evaluatedAction": "executed",
            "correctedProposal": {"health": 0, "energy": 0},
            "strict_shortest": {"health": -5, "energy": -10, "onlyWhen": "已证明 foodSteps 更长"},
            "survival": {"health": -5, "energy": -10, "onlyWhen": "更快方案在全部可比较生存指标上不差"},
            "unknown": {"health": 0, "energy": 0, "note": "未知指标不支配、不扣罚"},
            "repeat": {"health": -5, "energy": -10, "onlyWhen": "同一完整局面再次出现且无进食"},
            "foodRecovery": {"health": 3, "energy": 12, "cap": 100},
            "levelRecovery": {"health": 15, "energy": 25, "cap": 100, "finalLevel": 0},
            "death": "health <= 0 时判负；energy 可归零但不单独判负",
        }

    def _questions(
        self,
        candidates: list[dict[str, Any]],
        planner_view: Mapping[str, Any] | None = None,
        candidate_plans: list[dict[str, Any]] | None = None,
        choice_map: dict[str, str] | None = None,
        strategy_mode: bool = False,
        guarded_mode: bool = False,
    ) -> dict[str, dict[str, Any]]:
        if choice_map is None:
            choice_map = {
                item.get("id", item["direction"]): item["direction"]
                for item in (candidate_plans or candidates)
            }
        if len(choice_map) == 1:
            only_label, only_direction = next(iter(choice_map.items()))
            choice_map[f"{only_label}_safe"] = only_direction
        plan_by_id = {item.get("id"): item for item in (candidate_plans or []) if isinstance(item, dict)}

        def short_metric(value: Any, true_text: str = "通") -> str:
            if value is None:
                return "?"
            if isinstance(value, bool):
                return true_text if value else "断"
            return str(value)

        criteria: dict[str, str] = {}
        future_active = bool(guarded_mode and planner_view and planner_view.get("lookahead", {}).get("active"))
        explicit_cost = bool(strategy_mode and guarded_mode and not future_active and candidate_plans
                             and all(p.get("proof") == "proved" and isinstance(p.get("routeSteps"), int) for p in candidate_plans))
        for label in choice_map:
            plan = plan_by_id.get(label.removesuffix("_safe"), {})
            steps = "?" if plan.get("routeSteps") is None else f"{plan['routeSteps']}步"
            if future_active:
                future = plan.get("nextFood", {})
                value = f"{future['meanSteps']:.2f}步" if future.get("proof") == "proved" else "未证"
                criteria[label] = f"退路已校验；本颗{steps}；沿此路线吃后下一颗平均{value}"
                continue
            if explicit_cost:
                criteria[label] = f"Food in {plan['routeSteps']} moves; afterwards {plan['exitsAfterFood']} exits and {plan['spaceAfterFood']} reachable cells"
                continue
            criteria[label] = (
                f"出口{short_metric(plan.get('exitsAfter'))}/"
                f"食后出口{short_metric(plan.get('exitsAfterFood'))}/"
                f"尾{short_metric(plan.get('tailReachableAfter'))}/区{short_metric(plan.get('spaceAfter'))}/"
                f"食后区{short_metric(plan.get('spaceAfterFood'))}"
                f"/食物{steps}"
            )
        # The installed Laya checkpoint computes a top-2 confidence feature
        # even for a choice question, so its internal marker count must be at
        # least two.  When the safety filter leaves one real direction, add a
        # second label that maps to that same safe action.  It is not a second
        # executable move and is deliberately not exposed in state.candidates.
        if strategy_mode and guarded_mode:
            instruction = "优先级：存活 > 最短路径 > 能量收益。先保留安全退路，再在所列安全路线中选已证明步数最少的方案；前两项相当时考虑后续收集。未知时沿规划参考。只选方案编号。"
            if explicit_cost:
                instruction = "All candidate routes are certified safe. Choose the route with the fewest moves to reach food. Do not trade extra moves for more exits. Only compare future space when the move counts are equal."
            if future_active:
                instruction = "存活优先，再最短。本颗完整路线同样短时，比较沿此路线吃完后到下一颗随机能量的已证平均步数，越少越好；未知沿规划参考。选择编号，不能牺牲本颗最短。"
        elif strategy_mode:
            instruction = (
                "先比较当前 food 的已证明步数；只选方案编号。"
                if self.policy_mode == "strict_shortest"
                else "先保命再进食。先避免本步执行后立即无路；非过关食物若按所列续路吃后无出口会困死，但只执行一格后可重规划。比较完整路线，风险相近时优先少走；同一食物没有更短或更安全的方案时，继续上一轮路线，避免折返兜圈。只选方案编号。"
            )
        elif planner_view is not None and planner_view.get("proof") == "proved":
            instruction = "请选择当前推荐方案中的一个编号。规划器已经验证最短步数，不要重新猜方向。"
        else:
            instruction = "请选择一个安全方案编号，沿已验证安全回退参考；当前未证明最优，不要把任意方向称作最短。"
        return {
            "direction": {
                "type": "choice",
                "instructions": instruction,
                "criteria": criteria,
            }
        }

    def _model_state(
        self,
        candidates: list[dict[str, Any]],
        planner_view: Mapping[str, Any] | None = None,
        candidate_plans: list[dict[str, Any]] | None = None,
        choice_map: dict[str, str] | None = None,
        strategy_mode: bool = False,
        guarded_mode: bool = False,
    ) -> dict[str, Any]:
        """Keep model input compact while the browser receives the rich state."""

        planner_proved = planner_view is not None and planner_view.get("proof") == "proved"
        planner_steps = planner_view.get("remainingSteps") if planner_view is not None else None
        choice_map = choice_map or {
            item.get("id", item["direction"]): item["direction"]
            for item in (candidate_plans or candidates)
        }
        labels_by_direction = {
            direction: label
            for label, direction in choice_map.items()
            if not label.endswith("_safe")
        }
        compact_plans = [
            {
                "id": item.get("id", labels_by_direction.get(item.get("direction"))),
                "direction": item.get("direction"),
                "routeSteps": item.get("routeSteps"),
                "chain": list(item.get("chain") or [])[:6],
                "fullRoute": list(item.get("fullRoute")) if item.get("fullRoute") is not None else None,
                "nextFood": copy.deepcopy(item.get("nextFood")),
                "proof": item.get("proof"),
                **{
                    key: item[key]
                    for key in (
                        "spaceAfter",
                        "exitsAfter",
                        "tailReachableAfter",
                        "spaceAfterFood",
                        "exitsAfterFood",
                        "tailReachableAfterFood",
                        "revisitCountAfter",
                        "seenStateAfter",
                    )
                    if key in item
                },
            }
            for item in (candidate_plans or [])
            if isinstance(item, dict)
        ]
        if planner_proved and isinstance(planner_steps, int):
            recommended = [item["id"] for item in compact_plans if item["routeSteps"] == planner_steps]
            reason = "确定性规划器已验证这些方案达到当前食物所需的真实移动步数最少。"
        else:
            suggested_direction = planner_view.get("nextDirection") if planner_view is not None else None
            recommended = [labels_by_direction[suggested_direction]] if suggested_direction in labels_by_direction else []
            reason = "确定性规划器给出已验证安全回退参考；当前食物最短步数未证明。"
        lookahead = planner_view.get("lookahead", {}) if planner_view else {}
        if guarded_mode and lookahead.get("active"):
            recommended = [p["id"] for p in compact_plans if p["direction"] in lookahead["preferredDirections"]]
        compact_feedback = [
            {
                "model": item.get("modelDirection"),
                "executed": item.get("executedDirection"),
                "food": item.get("foodEaten"),
                "penalty": item.get("penaltyApplied"),
                "judgement": item.get("judgement"),
            }
            for item in self._recent_feedback[-3:]
        ]
        if strategy_mode:
            foods_remaining = max(0, self._config().target - self._level_eaten)
            food_completes_level = self._level_eaten + 1 >= self._config().target
            damage_rule = (
                "survival：仅生存指标支配或重复局面扣罚"
                if self.policy_mode == "survival"
                else "strict_shortest：已证明 foodSteps 更长才扣罚"
            )
            if guarded_mode:
                damage_rule = "实际无效绕路或重复扣罚；提议被纠正不扣罚。安全环序约束内最短，不是整局全局最短"
            rows = []
            for item in compact_plans:
                path = item.get("fullRoute")
                rows.append(
                    "|".join(
                        [
                            str(item.get("id")),
                            str(item.get("direction")),
                            str(item.get("routeSteps") if item.get("routeSteps") is not None else "?"),
                            str(item.get("proof")),
                            "?" if path is None else "".join(DIRECTION_CHARS.get(move, "?") for move in path),
                            f"E{item.get('exitsAfter','?')}/{item.get('exitsAfterFood','?')}",
                            f"S{item.get('spaceAfter','?')}/{item.get('spaceAfterFood','?')}",
                            f"T{item.get('tailReachableAfter','?')}/{item.get('tailReachableAfterFood','?')}",
                            f"R{item.get('revisitCountAfter', 0)}",
                        ]
                    )
                )
            size = self._config().size
            occupied = {point: "S" for point in self._snake}
            occupied[self._snake[-1]] = "T"
            occupied[self._snake[0]] = "H"
            occupied[self._food] = "F" if self._food not in occupied else occupied[self._food]
            board_rows = [
                "".join(occupied.get((x, y), ".") for x in range(size))
                for y in range(size)
            ]
            body = f"{self._snake[0][0]},{self._snake[0][1]} links={_body_links(self._snake)}"
            history = ";".join(
                f"{DIRECTION_CHARS.get(item.get('executedDirection'), '?')}:{int(bool(item.get('foodEaten')))}:{int(bool(item.get('penaltyApplied')))}"
                for item in self._recent_feedback[-8:]
            ) or "-"
            previous = self._recent_feedback[-1] if self._recent_feedback else None
            previous_route = "-"
            if previous and previous.get("foodBefore") == self._food and not previous.get("foodEaten"):
                path = previous.get("fullRoute") or []
                previous_route = "".join(DIRECTION_CHARS.get(move, "?") for move in path[1:]) or "-"
            route_memory = f"PREVIOUS_OWN_ROUTE(sameFood,remainingOnly) {previous_route}"
            if guarded_mode:
                source = "planner" if previous and (previous.get("routeOverride") or previous.get("modelCalled") is False) else self.model_provider
                route_memory = (
                    f"PREVIOUS_EXECUTED_ROUTE(sameFood,remainingOnly) {previous_route}"
                    if self.neutral_execution_source
                    else f"PREVIOUS_EXECUTED_ROUTE(source={source},sameFood,remainingOnly) {previous_route}"
                )
            reference = []
            if guarded_mode:
                reference = [f"PLANNER_REFERENCE {'/'.join(recommended)}; minimumSteps={planner_steps if planner_proved else '?'}; survivalCycle=required"]
                if lookahead.get("active"):
                    values = []
                    for p in compact_plans:
                        future = p.get("nextFood") or {}
                        value = f"{future['meanSteps']:.2f}" if future.get("proof") == "proved" else "?"
                        values.append(f"{p['id']}:{value}")
                    reference.append("NEXT_FOOD_MEAN(along given full route; uniform free cells; ?=unproved): " + ";".join(values))
            context = "\n".join(
                [
                    "CTX2; x right,y down,origin 0,0; H=head,T=tail,S=body,F=food,.=free. U/R/D/L=up/right/down/left. Body links describe H to T with counts; routes are future moves.",
                    f"BOARD {size}x{size} rows=" + "/".join(board_rows),
                    f"SNAKE H>T {body}",
                    f"FOOD {self._food[0]},{self._food[1]} LEVEL {self._level} HP {self._health} E {self._energy} NOFOOD {self._strategy_no_food_steps} REMAIN {foods_remaining} COMPLETES {food_completes_level}",
                    f"PLANS id|dir|steps|proof|fullPath(?=unknown)|E=exitsAfter/food|S=reachableAfter/food|T=tailAfter/food|R=stateVisitsAfter: {';'.join(rows)}",
                    f"HISTORY(last8 oldest>newest move:food:penalty) {history}",
                    route_memory,
                    *reference,
                    f"RULE {damage_rule}; -5HP/-10energy; tail/space are risk signals, not guarantees; CTX_END",
                ]
            )
            request_state = {
                "context": context,
                "候选方案": [{"id": item.get("id"), "direction": item.get("direction")} for item in compact_plans],
            }
            if self.compact_context:
                compressed = {**request_state, "context": compress_context(context)}
                before = _estimate_context_tokens(request_state, {})
                after = _estimate_context_tokens(compressed, {})
                if before is not None and after is not None and after < before:
                    request_state = compressed
            return request_state
        return {
            # Put deterministic route evidence first: the local Laya encoder
            # truncates the tail when a request reaches max_len.
            "当前推荐方案": recommended,
            "原因": reason,
            "最短步数": planner_steps if planner_proved and isinstance(planner_steps, int) else None,
            "候选方案": compact_plans,
            "boardSize": self._config().size,
            "head": _as_point(self._snake[0]),
            "food": _as_point(self._food),
            "snake": [_as_point(point) for point in self._snake],
            "health": self._health,
            "energy": self._energy,
            "recentFeedback": compact_feedback,
            "penalty": {"longerProved": {"health": -5, "energy": -10}, "unknownFallback": "no penalty"},
            "goal": "当前 food 的贪心目标是完整动态蛇身下真实移动步数最少",
            "selectionRule": "只比较 candidatePlans.routeSteps；未知不称最短，所有方向仍可选择",
        }

    def state(self) -> dict[str, Any]:
        config = self._config()
        head = self._snake[0]
        candidates, blocked = self._compute_move_options()
        return {
            # All boards are square; the UI protocol represents their
            # side length as one number (for example, 12 means 12×12).
            "boardSize": config.size,
            "seed": self.seed,
            "model": self.model_provider,
            "snake": [_as_point(point) for point in self._snake],
            "food": _as_point(self._food),
            "obstacles": [],
            "level": self._level,
            "levelName": config.name,
            "totalLevels": MAX_LEVEL,
            "levels": [
                {
                    "level": level,
                    "name": level_config.name,
                    "size": level_config.size,
                    "initialLength": level_config.initial_length,
                    "target": level_config.target,
                    "intervalMs": level_config.interval_ms,
                }
                for level, level_config in enumerate(LEVEL_CONFIGS, start=1)
            ],
            "eaten": self._level_eaten,
            "target": config.target,
            "totalEaten": self._total_eaten,
            "steps": self._steps,
            "status": self._status,
            "health": self._health,
            "energy": self._energy,
            "maxHealth": MAX_HEALTH,
            "maxEnergy": MAX_ENERGY,
            "lossReason": self._loss_reason,
            "penaltyRules": self._penalty_rules(),
            "intervalMs": config.interval_ms,
            "direction": self._direction,
            "candidates": copy.deepcopy(candidates),
            "blocked": copy.deepcopy(blocked),
            "planner": self._planner_state(),
            "controller": "guarded",
            "lookaheadEnabled": self.lookahead_enabled,
            "certifiedShortcuts": self.certified_shortcuts,
            "objectivePriority": ["survival", "shortest_path", "energy"],
            "strategy": {
                "phase": self._planner_phase,
                "policyMode": self.policy_mode,
                "noFoodSteps": self._strategy_no_food_steps,
            },
            "cycle": {
                "path": [_as_point(point) for point in self._cycle],
                "headIndex": self._cycle_index[head],
                "foodIndex": self._cycle_index[self._food],
            },
        }

    snapshot = state

    def logs(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._logs)

    def logs_metadata(self) -> dict[str, Any]:
        return {
            "count": len(self._logs),
            "truncated": self._logs_truncated > 0,
            "truncatedCount": self._logs_truncated,
            "max": MAX_DECISIONS,
        }

    @staticmethod
    def _parse_choice(
        raw: Mapping[str, Any], allowed: set[str], provider_label: str = "Laya"
    ) -> tuple[str, float | None]:
        answers = raw.get("answers")
        if not isinstance(answers, Mapping):
            raise ModelDecisionError(f"{provider_label} 响应缺少 answers 对象")
        answer = answers.get("direction")
        if not isinstance(answer, Mapping) or answer.get("type") != "choice":
            raise ModelDecisionError(f"{provider_label} 响应缺少 direction.choice 结果")
        choice = answer.get("choice")
        if not isinstance(choice, str) or choice not in allowed:
            allowed_text = "、".join(sorted(allowed))
            raise ModelDecisionError(
                f"{provider_label} 返回了不安全或无效方向：{choice!r}；安全候选为 {allowed_text}"
            )
        return choice, _safe_number(answer.get("confidence"))

    def _strategy_option_dicts(self) -> tuple[list[dict[str, Any]], dict[str, StrategyCandidate]]:
        evaluated = evaluate_candidates(
            size=self._config().size,
            snake=self._snake,
            food=self._food,
            current_direction=self._direction,
            node_budget=STRATEGY_NODE_BUDGET,
        )
        options = [candidate.as_dict() for candidate in evaluated]
        for option in options:
            option["routeSteps"] = option["foodSteps"]
            if option["proof"] == "proved":
                option["fullRoute"] = list(option["chain"][:1]) + list(
                    next(candidate.plan.moves for candidate in evaluated if candidate.direction == option["direction"])
                )
            else:
                option["fullRoute"] = None
        for option in options:
            transition = strategy_legal_transition(
                size=self._config().size,
                snake=self._snake,
                food=self._food,
                direction=option["direction"],
                current_direction=self._direction,
            )
            if transition is None or transition.ate_food:
                option["revisitCountAfter"] = 0
                option["seenStateAfter"] = False
            else:
                signature = (self._level, self._food, transition.snake)
                count = self._strategy_seen.get(signature, 0)
                option["revisitCountAfter"] = count
                option["seenStateAfter"] = count > 0
        by_direction = {candidate.direction: candidate for candidate in evaluated}
        return options, by_direction

    @staticmethod
    def _strategy_metrics(candidate: Mapping[str, Any]) -> tuple[Any, ...]:
        return tuple(candidate.get(key) for key in (
            "spaceAfter",
            "exitsAfter",
            "tailReachableAfter",
            "spaceAfterFood",
            "exitsAfterFood",
            "tailReachableAfterFood",
        ))

    def _strategy_judgement(
        self,
        options: list[dict[str, Any]],
        chosen: dict[str, Any],
        *,
        repeat: bool,
    ) -> tuple[dict[str, Any], bool]:
        known = [item for item in options if item.get("proof") == "proved" and isinstance(item.get("foodSteps"), int)]
        chosen_steps = chosen.get("foodSteps")
        witness: dict[str, Any] | None = None
        penalty = False
        reason = "当前方案保持生存信号，未证明全局最优。"
        kind = "unknown" if chosen_steps is None or chosen.get("proof") != "proved" else "efficient"
        if isinstance(chosen_steps, int) and chosen.get("proof") == "proved":
            faster = [item for item in known if item.get("foodSteps", 0) < chosen_steps]
            if self.policy_mode == "strict_shortest":
                witness = min(faster, key=lambda item: item["foodSteps"]) if faster else None
                penalty = witness is not None
                kind = "dominated" if penalty else "efficient"
                reason = "严格模式下存在已证明更短方案。" if penalty else "已证明当前方案不长于其他已证明方案。"
            else:
                for item in faster:
                    chosen_metrics = self._strategy_metrics(chosen)
                    witness_metrics = self._strategy_metrics(item)
                    comparable = all(value is not None for value in chosen_metrics + witness_metrics)
                    no_worse = comparable and all(
                        (isinstance(left, bool) and isinstance(right, bool) and (left or not right))
                        or (isinstance(left, (int, float)) and isinstance(right, (int, float)) and left >= right)
                        for left, right in zip(witness_metrics, chosen_metrics)
                    )
                    if no_worse:
                        witness = item
                        penalty = True
                        break
                kind = "dominated" if penalty else ("tradeoff" if faster else "efficient")
                reason = "更快方案在所有可比较生存指标上不差。" if penalty else "速度与生存信号存在权衡，未知指标不作支配判断。"
        if repeat:
            penalty = True
            kind = "repeat"
            reason = "重复完整局面且没有收集进展，按规则扣罚一次。"
        return {
            "kind": kind,
            "reason": reason,
            "witnessPlanId": witness.get("id") if witness is not None else None,
        }, penalty

    def _context_audit(self, state: dict[str, Any], questions: dict[str, Any], options: list[dict[str, Any]]) -> dict[str, Any]:
        encoded = str(state.get("context", ""))
        try:
            context = expand_context(encoded)
            encoding_valid = True
        except ValueError:
            context = ""
            encoding_valid = False
        input_tokens = _estimate_context_tokens(state, questions)
        original_tokens = _estimate_context_tokens({**state, "context": context}, questions) if encoding_valid else None
        unknown = [item.get("id") for item in options if item.get("proof") == "unknown" or item.get("fullRoute") is None]
        known_incomplete = [
            item.get("id") for item in options if item.get("proof") == "proved" and (
                item.get("fullRoute") is None
                or len(item["fullRoute"]) != item.get("routeSteps")
                or f"{item['id']}|{item['direction']}|{item['routeSteps']}|proved|{''.join(DIRECTION_CHARS[move] for move in item['fullRoute'])}|" not in context
            )
        ]
        occupied = {point: "S" for point in self._snake}
        occupied[self._snake[-1]] = "T"
        occupied[self._snake[0]] = "H"
        occupied[self._food] = "F"
        size = self._config().size
        rows = "/".join("".join(occupied.get((x, y), ".") for x in range(size)) for y in range(size))
        body = f"SNAKE H>T {self._snake[0][0]},{self._snake[0][1]} links={_body_links(self._snake)}\n"
        token_budget = 16_384 if self.model_provider == "kev" else 1024
        return {
            "format": "CTX3" if encoded.startswith("CTX3;") else "CTX2",
            "encodingValid": encoding_valid,
            "uncompressedInputTokens": original_tokens,
            "savedInputTokens": original_tokens - input_tokens if original_tokens is not None and input_tokens is not None else None,
            "boardComplete": f"BOARD {size}x{size} rows={rows}\n" in context,
            "bodyComplete": body in context,
            "knownRoutesComplete": len(known_incomplete) == 0,
            "unknownPlanIds": unknown,
            "unknownRouteCount": len(unknown),
            "pathSteps": {item.get("id"): item.get("routeSteps") for item in options},
            "inputTokens": input_tokens,
            "tokenBudget": token_budget,
            "tokenizer": "laya-local-estimate" if self.model_provider == "kev" else "laya-local",
            # Kev uses a different tokenizer, so raw usage is recorded for
            # reference but is never compared with the local Laya estimate.
            "tokenMatch": None,
            "historyCount": min(8, len(self._recent_feedback)),
            "endMarkerPresent": context.endswith("CTX_END"),
        }

    def _strategy_optimality(
        self,
        options: list[dict[str, Any]],
        selected: dict[str, Any],
        judgement: dict[str, Any],
        *,
        final_status: str,
    ) -> dict[str, Any]:
        scope = "当前候选和已测指标，不是整局全局最优"
        known_costs = [item["foodSteps"] for item in options if item.get("proof") == "proved" and isinstance(item.get("foodSteps"), int)]
        selected_steps = selected.get("foodSteps")
        shortest = min(known_costs) if known_costs else None
        base = {
            "scope": scope,
            "selectedFoodSteps": selected_steps,
            "shortestKnownFoodSteps": shortest,
        }
        if final_status == "lost":
            return {"status": "failed", "label": "本步导致失败，未证明最优", "reason": "本步之后生命耗尽或无路可走。", **base}
        if judgement.get("kind") == "repeat":
            return {"status": "unproven", "label": "重复轨迹，未证明最优", "reason": "完整局面重复，不能把本步称作最优。", **base}
        if len(options) == 1:
            return {"status": "forced", "label": "唯一可执行方向", "reason": "当前只有一个普通物理合法方向。", **base}
        all_known = (
            len(known_costs) == len(options)
            and all(
                all(item.get(key) is not None for key in (
                    "spaceAfter", "exitsAfter", "tailReachableAfter",
                    "spaceAfterFood", "exitsAfterFood", "tailReachableAfterFood",
                ))
                for item in options
            )
        )
        if not all_known or not isinstance(selected_steps, int) or shortest is None:
            return {"status": "unproven", "label": "证据不足，未证明最优", "reason": "存在未知 foodSteps 或生存指标。", **base}
        if self.policy_mode == "strict_shortest":
            if selected_steps == shortest:
                return {"status": "food_shortest", "label": "当前食物步数最优", "reason": "所有候选路线已证明，选中当前 food 最少步数；不代表生存最优。", **base}
        else:
            selected_metrics = self._strategy_metrics(selected)
            metrics_best = all(
                all(
                    (isinstance(selected_value, bool) and isinstance(other_value, bool) and selected_value >= other_value)
                    or (isinstance(selected_value, (int, float)) and isinstance(other_value, (int, float)) and selected_value >= other_value)
                    for other_value in (item.get(key) for item in options)
                )
                for key, selected_value in zip(
                    ("spaceAfter", "exitsAfter", "tailReachableAfter", "spaceAfterFood", "exitsAfterFood", "tailReachableAfterFood"),
                    selected_metrics,
                )
            )
            if selected_steps <= min(item["foodSteps"] for item in options) and metrics_best:
                return {"status": "metrics_best", "label": "当前候选指标最优", "reason": "所有候选指标已知，选中方案步数不长且六项生存指标均不差；不代表整局全局最优。", **base}
        if judgement.get("kind") == "dominated":
            result = {"status": "not_best", "label": "存在严格支配方案", "reason": "已测存在更快且生存指标不差的候选。", **base}
            if judgement.get("witnessPlanId") is not None:
                result["witnessPlanId"] = judgement["witnessPlanId"]
            return result
        return {"status": "tradeoff", "label": "存在策略取舍，未证明最优", "reason": "候选在效率和生存信号之间存在取舍。", **base}

    def _legacy_step(self, decider: Decider) -> dict[str, Any]:
        """Guarded controller using the original verified route planner."""

        if self._status == "won":
            raise GameFinishedError("游戏已经通关")
        if self._status == "lost":
            raise GameFinishedError("游戏已因生命耗尽结束")

        candidates, blocked = self._compute_move_options()
        if not candidates:
            raise ModelDecisionError("引擎没有可执行的安全候选")
        safe_directions = {candidate["direction"] for candidate in candidates}
        cache_valid = self._current_cache_matches()
        phase_before = "FOLLOW_ROUTE" if cache_valid else "PLAN_FOOD"
        planned = self._planner_cache.as_plan() if cache_valid else self._compute_food_plan()
        if not planned.moves or planned.next_direction not in safe_directions:
            if cache_valid:
                phase_before = "PLAN_FOOD"
                planned = self._compute_food_plan()
            if not planned.moves or planned.next_direction not in safe_directions:
                raise ModelDecisionError("规划器没有产生当前可执行的安全路线")

        candidate_plans, route_plans = self._candidate_plans(candidates, planned)
        planned, lookahead = self._future_route_reference(candidate_plans, route_plans, planned)
        planner_view = self._planner_request_view(planned, phase_before)
        planner_view["lookahead"] = lookahead
        choice_map = self._assign_plan_ids(candidate_plans)
        proved_options = [p for p in candidate_plans if p.get("proof") == "proved" and isinstance(p.get("routeSteps"), int)]
        shortest_options = (
            [p for p in proved_options if p["routeSteps"] == planned.remaining]
            if planned.proof == "proved" else []
        )
        unique_best = (
            self.skip_deterministic_model and len(candidates) > 1
            and len(proved_options) == len(candidate_plans)
            and len(shortest_options) == 1
            and shortest_options[0]["direction"] == planned.next_direction
        )
        if len(candidates) == 1 or unique_best:
            direction = planned.next_direction
            suffix = route_plans[direction]
            planning = {
                "phaseBefore": phase_before, "phaseAfter": None, "proof": planned.proof,
                "optimalRemaining": planned.remaining if planned.proof == "proved" else None,
                "plannedRemaining": planned.remaining, "proposedRemaining": None,
                "executedRemaining": suffix.remaining, "plannedDirection": direction,
                "modelDirection": None, "executedDirection": direction,
                "expanded": planned.expanded, "fallbackReason": planned.fallback_reason,
                "lookahead": lookahead,
                "forcedReason": "unique_proved_shortest" if unique_best else "only_safe_direction",
            }
            return self._execute(
                choice=direction, model_direction=None, model_choice=None, confidence=None,
                latency_ms=0.0, raw=None, request=None, model_candidate=None,
                candidates=candidates, candidate_plans=candidate_plans, choice_map=choice_map,
                selected=candidates[0], blocked=blocked, route_after=suffix,
                route_override=False, override_kind=None, planning=planning,
                controller="guarded", model_called=False,
            )
        request_state = self._model_state(candidates, planner_view, candidate_plans, choice_map, strategy_mode=True, guarded_mode=True)
        questions = self._questions(
            candidates, planner_view, candidate_plans, choice_map, strategy_mode=True, guarded_mode=True
        )
        request = {"state": copy.deepcopy(request_state), "questions": copy.deepcopy(questions)}
        context_audit = self._context_audit(request_state, questions, candidate_plans)
        started = time.perf_counter()
        model_error: str | None = None
        model_choice: str | None = None
        confidence: float | None = None
        raw: Any = None
        context_audit["requestSent"] = False
        try:
            if not all(context_audit[k] for k in ("boardComplete", "bodyComplete", "knownRoutesComplete", "endMarkerPresent")):
                raise ModelDecisionError("模型输入编码不完整")
            if (
                self.model_provider != "kev"
                and (context_audit["inputTokens"] is None or context_audit["inputTokens"] > context_audit["tokenBudget"])
            ) or (
                self.model_provider == "kev"
                and context_audit["inputTokens"] is not None
                and context_audit["inputTokens"] > context_audit["tokenBudget"]
            ):
                raise ModelDecisionError("模型输入无法通过完整性与 token 预算校验")
            # Keep an immutable snapshot of exactly what the model received;
            # a provider implementation must not be able to mutate the log.
            context_audit["requestSent"] = True
            raw = decider(copy.deepcopy(request["state"]), copy.deepcopy(request["questions"]))
            if not isinstance(raw, Mapping):
                raise ModelDecisionError(f"{self.model_provider.capitalize()} 响应顶层不是对象")
            usage = raw.get("usage")
            if isinstance(usage, Mapping) and isinstance(usage.get("input_tokens"), int):
                estimated_tokens = context_audit["inputTokens"]
                token_delta = (
                    usage["input_tokens"] - estimated_tokens
                    if isinstance(estimated_tokens, int)
                    else None
                )
                context_audit.update(rawInputTokens=usage["input_tokens"], tokenDelta=token_delta)
                context_audit["tokenMatch"] = (
                    token_delta == 0 if self.model_provider == "laya" else None
                )
                if self.model_provider == "laya" and not context_audit["tokenMatch"]:
                    raise ModelDecisionError("Laya 实际输入 token 数与完整输入预检不一致")
            model_choice, confidence = self._parse_choice(
                raw,
                set(questions["direction"]["criteria"]),
                self.model_provider.capitalize(),
            )
        except Exception as error:
            message = " ".join(str(error).split())[:240]
            model_error = f"{type(error).__name__}: {message}" if message else type(error).__name__
        latency_ms = round((time.perf_counter() - started) * 1000, 1)
        model_direction = choice_map.get(model_choice) if model_choice is not None else None
        if model_direction is not None:
            model_candidate = next(candidate for candidate in candidates if candidate["direction"] == model_direction)
        else:
            model_candidate = None
        planned_direction = planned.next_direction
        assert planned_direction is not None
        executed_direction = model_direction or planned_direction
        executed_plan_after = RoutePlan(
            planned.moves[1:], planned.proof, planned.expanded, planned.fallback_reason
        )
        route_override = False
        override_kind: str | None = None
        proposed_remaining: int | None = planned.remaining
        planning_proof = planned.proof
        planning_expanded = planned.expanded
        planning_fallback = planned.fallback_reason

        if model_error is not None:
            route_override = True
            override_kind = "model_error"
            planning_fallback = model_error
            proposed_remaining = None
        elif model_direction is not None and model_direction != planned_direction:
            model_transition = legal_transition(
                size=self._config().size,
                cycle=self._cycle,
                snake=self._snake,
                food=self._food,
                direction=model_direction,
            )
            if model_transition is None:
                raise ModelDecisionError("Laya 选择无法通过当前安全转移校验")
            branch_plan = route_plans.get(model_direction)
            if branch_plan is None:
                raise ModelDecisionError("缺少 Laya 分支的真实规划结果")
            if branch_plan.proof == "proved":
                proposed_remaining = 1 + branch_plan.remaining
            else:
                proposed_remaining = None

            if planned.proof == "proved" and branch_plan.proof == "proved":
                assert proposed_remaining is not None
                if proposed_remaining == planned.remaining:
                    proposed = next(p for p in candidate_plans if p["direction"] == model_direction)
                    future = proposed.get("nextFood", {})
                    admissible = not lookahead["active"] or (
                        future.get("proof") == "proved"
                        and future["totalSteps"] <= lookahead["reference"]["totalSteps"]
                    )
                    if admissible:
                        executed_plan_after = branch_plan
                        planning_proof = branch_plan.proof
                        planning_expanded = branch_plan.expanded
                        planning_fallback = branch_plan.fallback_reason
                    else:
                        executed_direction = planned_direction
                        route_override = True
                        override_kind = "lookahead_preference" if future.get("proof") == "proved" else "lookahead_unproved"
                elif proposed_remaining > planned.remaining:
                    executed_direction = planned_direction
                    route_override = True
                    override_kind = "longer_proved"
                else:
                    raise ModelDecisionError("规划器证明结果不一致，拒绝执行本步")
            else:
                # Unknown optimality evidence never silently becomes a claim
                # about the model branch; follow the verified safety route.
                executed_direction = planned_direction
                route_override = True
                override_kind = "unproven_fallback"
                planning_fallback = planning_fallback or branch_plan.fallback_reason or "model_branch_unproved"

        selected = next(candidate for candidate in candidates if candidate["direction"] == executed_direction)
        planning = {
            "phaseBefore": phase_before,
            "phaseAfter": None,
            "proof": planning_proof,
            "optimalRemaining": planned.remaining if planned.proof == "proved" else None,
            "plannedRemaining": planned.remaining,
            "proposedRemaining": proposed_remaining,
            "executedRemaining": len(executed_plan_after.moves),
            "plannedDirection": planned_direction,
            "modelDirection": model_direction,
            "executedDirection": executed_direction,
            "expanded": planning_expanded,
            "fallbackReason": planning_fallback,
            "lookahead": copy.deepcopy(lookahead),
        }
        if lookahead["active"]:
            actual = next(p for p in candidate_plans if p["direction"] == executed_direction)
            planning["lookahead"]["executed"] = copy.deepcopy(actual["nextFood"])
            proposed = next((p for p in candidate_plans if p["direction"] == model_direction), {})
            planning["lookahead"]["proposed"] = copy.deepcopy(proposed.get("nextFood"))
        return self._execute(
            choice=executed_direction,
            model_direction=None if model_error is not None else model_direction,
            model_choice=model_choice,
            confidence=confidence,
            latency_ms=latency_ms,
            raw=copy.deepcopy(raw),
            request=request,
            model_candidate=model_candidate,
            candidates=candidates,
            candidate_plans=candidate_plans,
            choice_map=choice_map,
            selected=selected,
            blocked=blocked,
            route_after=executed_plan_after,
            route_override=route_override,
            override_kind=override_kind,
            planning=planning,
            context_audit=context_audit,
            controller="guarded",
            model_error=model_error,
            model_called=context_audit["requestSent"],
        )

    def _execute_strategy(
        self,
        *,
        choice: str,
        model_choice: str,
        confidence: float | None,
        latency_ms: float,
        raw: dict[str, Any],
        request: dict[str, Any],
        choice_map: dict[str, str],
        candidates: list[dict[str, Any]],
        blocked: list[dict[str, str]],
        strategy_options: list[dict[str, Any]],
        selected: dict[str, Any],
        transition: Any,
        judgement: dict[str, Any],
        penalty_requested: bool,
        context_audit: dict[str, Any],
    ) -> dict[str, Any]:
        phase_before = self._strategy_phase
        head_before = self._snake[0]
        food_before = self._food
        snake_before = list(self._snake)
        level_before = self._level
        self._snake = list(transition.snake)
        self._direction = choice
        self._steps += 1
        food_eaten = transition.ate_food
        if food_eaten:
            self._level_eaten += 1
            self._total_eaten += 1

        vitals_before = {"health": self._health, "energy": self._energy}
        health_delta = -min(5, self._health) if penalty_requested else 0
        energy_delta = -min(10, self._energy) if penalty_requested else 0
        self._health += health_delta
        self._energy += energy_delta
        after_damage = {"health": self._health, "energy": self._energy}
        food_recovery = {"health": 0, "energy": 0}
        level_recovery = {"health": 0, "energy": 0}
        terminal_loss: dict[str, Any] | None = None
        level_completed: dict[str, Any] | None = None

        if self._health <= 0:
            self._status = "lost"
            self._loss_reason = "health_depleted"
            self._strategy_phase = "LOST"
            self._planner_phase = "LOST"
            if food_eaten:
                self._food = self._snake[0]
        elif food_eaten:
            before_health, before_energy = self._health, self._energy
            self._health = min(MAX_HEALTH, self._health + 3)
            self._energy = min(MAX_ENERGY, self._energy + 12)
            food_recovery = {"health": self._health - before_health, "energy": self._energy - before_energy}
            if self._level_eaten >= self._config().target:
                config_before = self._config()
                next_level = self._level + 1 if self._level < MAX_LEVEL else None
                level_completed = {
                    "level": level_before,
                    "eaten": self._level_eaten,
                    "target": config_before.target,
                    "nextLevel": next_level,
                    "won": next_level is None,
                }
                if next_level is None:
                    self._status = "won"
                    self._strategy_phase = "DONE"
                    self._planner_phase = "DONE"
                    self._food = self._snake[0]
                else:
                    before_health, before_energy = self._health, self._energy
                    self._health = min(MAX_HEALTH, self._health + 15)
                    self._energy = min(MAX_ENERGY, self._energy + 25)
                    level_recovery = {"health": self._health - before_health, "energy": self._energy - before_energy}
                    self._initialize_level(next_level)
                    self._strategy_phase = "REPLAN"
            else:
                self._food = self._spawn_food()
                self._strategy_no_food_steps = 0
                self._strategy_seen = {(self._level, self._food, tuple(self._snake)): 1}
                self._strategy_phase = "REPLAN"
        else:
            self._strategy_no_food_steps += 1
            signature = (self._level, self._food, tuple(self._snake))
            self._strategy_seen[signature] = self._strategy_seen.get(signature, 0) + 1
            self._strategy_phase = "REPLAN"

        if self._status == "playing" and not strategy_legal_directions(
            size=self._config().size,
            snake=self._snake,
            food=self._food,
            current_direction=self._direction,
        ):
            if food_eaten:
                self._health = after_damage["health"]
                self._energy = after_damage["energy"]
                food_recovery = {"health": 0, "energy": 0}
            previous_health = self._health
            self._health = 0
            terminal_loss = {"health": -previous_health, "energy": 0, "reason": "trapped"}
            self._status = "lost"
            self._loss_reason = "trapped"
            self._strategy_phase = "LOST"
            self._planner_phase = "LOST"
        phase_after = self._strategy_phase
        optimality = self._strategy_optimality(
            strategy_options,
            selected,
            judgement,
            final_status=self._status,
        )
        executed_remaining = None if selected.get("foodSteps") is None else (0 if food_eaten else max(0, selected["foodSteps"] - 1))
        planning = {
            "phaseBefore": phase_before,
            "phaseAfter": phase_after,
            "proof": selected.get("proof"),
            "optimalRemaining": None,
            "plannedRemaining": selected.get("foodSteps"),
            "proposedRemaining": selected.get("foodSteps"),
            "executedRemaining": executed_remaining,
            "plannedDirection": choice,
            "modelDirection": choice,
            "executedDirection": choice,
            "expanded": None,
            "fallbackReason": None if selected.get("proof") == "proved" else "route_steps_unknown",
        }
        penalty_reason = judgement["reason"] if penalty_requested else None
        penalty = {
            "applied": penalty_requested,
            "healthDelta": health_delta,
            "energyDelta": energy_delta,
            "extraSteps": None,
            "reason": penalty_reason,
        }
        vitals = {
            "before": vitals_before,
            "afterDamage": after_damage,
            "after": {"health": self._health, "energy": self._energy},
            "foodRecovery": food_recovery,
            "levelRecovery": level_recovery,
            "terminalLoss": terminal_loss,
        }
        distance_before = self._cycle_distance(head_before, food_before) if food_before in self._cycle_index else None
        distance_after = self._cycle_distance(self._snake[0], food_before) if food_before in self._cycle_index else None
        analysis = {
            "headBefore": _as_point(head_before),
            "foodBefore": _as_point(food_before),
            "boardSize": self._config(level_before).size,
            "level": level_before,
            "snakeBefore": [_as_point(point) for point in snake_before],
            "distanceBefore": distance_before,
            "distanceAfter": distance_after,
            "distanceMetric": "cycleDistance（环序参考格数，不是实际移动步数）",
            "safeOptions": [item["direction"] for item in candidates],
            "blocked": copy.deepcopy(blocked),
            "outcome": "food" if food_eaten else judgement["kind"],
            "headline": judgement["reason"],
            "explanation": f"生存优先策略直接执行 {self.model_provider.capitalize()} 选择；当前 food routeSteps={selected.get('foodSteps')}，风险信号仅供审计。",
        }
        if self._status == "lost":
            analysis["headline"] = f"{analysis['headline']} 游戏结束：{self._loss_reason}。"
        decision = {
            "id": uuid4().hex,
            "level": level_before,
            "step": self._steps,
            "modelChoice": model_choice,
            "choiceMap": copy.deepcopy(choice_map),
            "chosen": choice,
            "executed": choice,
            "confidence": confidence,
            "latencyMs": latency_ms,
            "reason": "模型选择按普通蛇身物理合法性直接执行；引擎不替换方向。",
            "candidates": copy.deepcopy(candidates),
            "candidatePlans": copy.deepcopy(strategy_options),
            "strategyOptions": copy.deepcopy(strategy_options),
            "strategy": {
                "policyMode": self.policy_mode,
                "chosenPlanId": next((item.get("id") for item in strategy_options if item.get("direction") == choice), None),
                "chosenTactic": selected.get("tactic"),
                "tradeoff": judgement["kind"],
                "phaseBefore": phase_before,
                "phaseAfter": phase_after,
                "noFoodSteps": self._strategy_no_food_steps,
            },
            "judgement": judgement,
            "optimality": optimality,
            "contextAudit": copy.deepcopy(context_audit),
            "request": copy.deepcopy(request),
            "raw": copy.deepcopy(raw),
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "safetyOverride": False,
            "routeOverride": False,
            "overrideKind": None,
            "foodEaten": food_eaten,
            "penalty": penalty,
            "vitals": vitals,
            "gameOver": self._status in {"won", "lost"},
            "analysis": analysis,
            "planning": planning,
        }
        if level_completed is not None:
            decision["levelCompleted"] = level_completed["level"]
            decision["levelCompletedInfo"] = level_completed
        self._recent_feedback.append({
            "modelDirection": choice,
            "executedDirection": choice,
            "foodBefore": food_before,
            "foodEaten": food_eaten,
            "penaltyApplied": penalty_requested,
            "judgement": judgement["kind"],
            "fullRoute": selected.get("fullRoute"),
        })
        self._recent_feedback = self._recent_feedback[-8:]
        self._logs.append(decision)
        if len(self._logs) > MAX_DECISIONS:
            self._logs.pop(0)
            self._logs_truncated += 1
        return {"state": self.state(), "decision": copy.deepcopy(decision)}

    def _direct_strategy_step(self, decider: Decider) -> dict[str, Any]:
        if self._status in {"won", "lost"}:
            raise GameFinishedError("游戏已经结束")
        candidates, blocked = self._compute_strategy_options()
        if not candidates:
            self._health = 0
            self._status = "lost"
            self._loss_reason = "trapped"
            self._strategy_phase = "LOST"
            raise GameFinishedError("蛇已无合法下一步")
        strategy_options, _ = self._strategy_option_dicts()
        choice_map = self._assign_plan_ids(strategy_options)
        request_state = self._model_state(
            candidates, candidate_plans=strategy_options, choice_map=choice_map, strategy_mode=True
        )
        questions = self._questions(
            candidates, candidate_plans=strategy_options, choice_map=choice_map, strategy_mode=True
        )
        request = {"state": copy.deepcopy(request_state), "questions": copy.deepcopy(questions)}
        context_audit = self._context_audit(request_state, questions, strategy_options)
        if not all(context_audit[key] for key in ("boardComplete", "bodyComplete", "knownRoutesComplete", "endMarkerPresent")):
            raise ModelDecisionError("局面或已知路线编码不完整，本步暂停")
        if self.model_provider != "kev" and context_audit["inputTokens"] is None:
            raise ModelDecisionError("本地 Laya tokenizer 不可用，无法确认 request 未被截断")
        if context_audit["inputTokens"] is not None and context_audit["inputTokens"] > context_audit["tokenBudget"]:
            raise ModelDecisionError(f"request 超过 Laya 输入预算：{context_audit['inputTokens']}")
        started = time.perf_counter()
        try:
            raw = decider(copy.deepcopy(request["state"]), copy.deepcopy(request["questions"]))
        except (ModelDecisionError, ModelUnavailableError):
            raise
        except Exception as error:
            message = " ".join(str(error).split())[:240]
            raise ModelUnavailableError(
                f"{self.model_provider.capitalize()} 推理失败：{type(error).__name__}: {message}"
            ) from error
        latency_ms = round((time.perf_counter() - started) * 1000, 1)
        if not isinstance(raw, Mapping):
            raise ModelDecisionError(f"{self.model_provider.capitalize()} 响应顶层不是对象")
        raw_usage = raw.get("usage") if isinstance(raw, Mapping) else None
        if isinstance(raw_usage, Mapping) and isinstance(raw_usage.get("input_tokens"), int):
            context_audit["rawInputTokens"] = raw_usage["input_tokens"]
            estimated_tokens = context_audit["inputTokens"]
            context_audit["tokenDelta"] = (
                raw_usage["input_tokens"] - estimated_tokens
                if isinstance(estimated_tokens, int)
                else None
            )
            context_audit["tokenMatch"] = (
                context_audit["tokenDelta"] == 0 if self.model_provider == "laya" else None
            )
            if self.model_provider == "laya" and not context_audit["tokenMatch"]:
                raise ModelDecisionError("Laya input token audit mismatch; step paused without execution")
        model_choice, confidence = self._parse_choice(
            raw,
            set(questions["direction"]["criteria"]),
            self.model_provider.capitalize(),
        )
        model_direction = choice_map.get(model_choice)
        if model_direction is None:
            raise ModelDecisionError(
                f"{self.model_provider.capitalize()} 返回了本次未列出的方案标签：{model_choice!r}"
            )
        selected = next(item for item in strategy_options if item["direction"] == model_direction)
        transition = strategy_legal_transition(
            size=self._config().size,
            snake=self._snake,
            food=self._food,
            direction=model_direction,
            current_direction=self._direction,
        )
        if transition is None:
            raise ModelDecisionError("Laya 选择无法通过普通蛇身物理校验")
        repeat = not transition.ate_food and (self._level, self._food, transition.snake) in self._strategy_seen
        judgement, penalty_requested = self._strategy_judgement(strategy_options, selected, repeat=repeat)
        return self._execute_strategy(
            choice=model_direction,
            model_choice=model_choice,
            confidence=confidence,
            latency_ms=latency_ms,
            raw=dict(raw),
            request=request,
            choice_map=choice_map,
            candidates=candidates,
            blocked=blocked,
            strategy_options=strategy_options,
            selected=selected,
            transition=transition,
            judgement=judgement,
            penalty_requested=penalty_requested,
            context_audit=context_audit,
        )

    def _straight_food_route_is_clear(self) -> bool:
        head = self._snake[0]
        if head[0] != self._food[0] and head[1] != self._food[1]:
            return False
        direction = ("down" if self._food[1] > head[1] else "up") if head[0] == self._food[0] else ("right" if self._food[0] > head[0] else "left")
        current = tuple(self._snake)
        for _ in range(_manhattan(head, self._food)):
            transition = strategy_legal_transition(size=self._config().size, snake=current, food=self._food, direction=direction)
            if transition is None:
                return False
            current = transition.snake
        return current[0] == self._food

    def step(self, decider: Decider) -> dict[str, Any]:
        """Stage a strictly shorter certified route, then atomically execute."""
        if self._status in {"won", "lost"}:
            raise GameFinishedError("游戏已经结束")
        checkpoint = self.__dict__.copy()
        for key in ("_snake", "_logs", "_recent_feedback"):
            checkpoint[key] = list(checkpoint[key])
        checkpoint["_strategy_seen"] = dict(self._strategy_seen)
        checkpoint["_random"] = copy.copy(self._random)
        optimization = None
        try:
            cache_matches = self._current_cache_matches()
            goal = (self._level, self._total_eaten)
            if self._straight_probe_goal != goal:
                self._straight_probe_goal, self._straight_probe_attempts = goal, 0
            midroute = bool(self.certified_shortcuts and self.midroute_shortcuts and cache_matches
                           and self._straight_probe_attempts < 4
                           and self._planner_cache.as_plan().remaining > _manhattan(self._snake[0], self._food)
                           and self._straight_food_route_is_clear())
            if midroute:
                self._straight_probe_attempts += 1
            if self.certified_shortcuts and (not cache_matches or midroute):
                baseline = self._planner_cache.as_plan() if midroute else self._compute_food_plan()
                selected = baseline
                optimization = {
                    "owner": "planner", "switched": False,
                    "trigger": "mid_route_straight" if midroute else "initial_food",
                    "probeAttempt": self._straight_probe_attempts if midroute else 0,
                    "certificateNodeBudget": 2000 if midroute else 50000,
                    "baselineSteps": baseline.remaining, "selectedSteps": baseline.remaining,
                    "savedSteps": 0, "reason": "already_at_geometric_lower_bound",
                    "priorCycleId": cycle_id(self._cycle),
                }
                if baseline.remaining > _manhattan(self._snake[0], self._food):
                    proposal = find_certified_shortcut(
                        size=self._config().size, cycle=self._cycle, snake=tuple(self._snake),
                        food=self._food, baseline_steps=baseline.remaining,
                        node_budget=2000 if midroute else 50000,
                    )
                    optimization.update(searchExpanded=proposal.expanded, reason=proposal.reason)
                    if proposal.cycle is not None:
                        if not (0 < len(proposal.moves) < baseline.remaining) or not validate_certificate(
                            size=self._config().size, cycle=proposal.cycle, snake=tuple(self._snake),
                            food=self._food, moves=proposal.moves,
                        ):
                            raise ModelDecisionError("捷径安全证书未通过独立复验")
                        self._cycle = proposal.cycle
                        self._cycle_index = {point: index for index, point in enumerate(self._cycle)}
                        # Physical A* proved length k; this checked safe cycle
                        # contains that same k-step route, so its optimum is k.
                        selected = RoutePlan(proposal.moves, "proved", proposal.expanded)
                        optimization.update(
                            switched=True, selectedSteps=selected.remaining,
                            savedSteps=baseline.remaining-selected.remaining,
                            cycleId=cycle_id(self._cycle), certificateValidated=True,
                            certificateCycle=[_as_point(p) for p in self._cycle],
                            referenceRoute=list(selected.moves), physicalShortestProved=True,
                        )
                # Use the verified route itself, not a fresh bounded search.
                self._planner_cache = RouteCache.from_plan(food=self._food, level=self._level, snake=self._snake, plan=selected)
            used_cycle = cycle_id(self._cycle)
            result = self._legacy_step(decider)
            decision = result["decision"]
            decision["planning"]["cycleId"] = used_cycle
            if optimization is not None:
                decision["planning"]["cycleOptimization"] = optimization
                decision["planning"]["phaseBefore"] = "FOLLOW_ROUTE" if midroute else "PLAN_FOOD"
            self._logs[-1] = copy.deepcopy(decision)
            return result
        except Exception:
            self.__dict__.clear()
            self.__dict__.update(checkpoint)
            raise

    def advance_with_direction(
        self,
        direction: str,
        *,
        raw: Mapping[str, Any] | None = None,
        confidence: float | None = None,
        latency_ms: float = 0.0,
    ) -> dict[str, Any]:
        """Advance using a previously validated safe direction for engine tests.

        Production callers should use :meth:`step`, which attempts a model
        decision and explicitly records any planner fallback. This helper is strict and
        still rejects any direction outside the same safe candidate set.
        """

        if self._status == "won":
            raise GameFinishedError("游戏已经通关")
        if self._status == "lost":
            raise GameFinishedError("游戏已因生命耗尽结束")
        candidates, blocked = self._compute_move_options()
        selected = next((candidate for candidate in candidates if candidate["direction"] == direction), None)
        if selected is None:
            raise ModelDecisionError(f"方向不在当前安全候选中：{direction!r}")
        request_state = self._model_state(candidates)
        questions = self._questions(candidates)
        transition = legal_transition(
            size=self._config().size,
            cycle=self._cycle,
            snake=self._snake,
            food=self._food,
            direction=direction,
        )
        if transition is None:
            raise ModelDecisionError(f"方向不在当前安全转移中：{direction!r}")
        route_after = (
            RoutePlan((), "proved", 0, None)
            if transition.ate_food
            else self._compute_food_plan(transition.snake)
        )
        planning = {
            "phaseBefore": "FOLLOW_ROUTE" if self._current_cache_matches() else "PLAN_FOOD",
            "phaseAfter": None,
            "proof": route_after.proof,
            "optimalRemaining": route_after.remaining if route_after.proof == "proved" else None,
            "plannedRemaining": route_after.remaining + 1,
            "proposedRemaining": route_after.remaining + 1,
            "executedRemaining": route_after.remaining,
            "plannedDirection": direction,
            "modelDirection": direction,
            "executedDirection": direction,
            "expanded": route_after.expanded,
            "fallbackReason": route_after.fallback_reason,
        }
        return self._execute(
            choice=direction,
            model_direction=direction,
            confidence=_safe_number(confidence),
            latency_ms=max(0.0, float(latency_ms)),
            raw=dict(raw or {"answers": {"direction": {"type": "choice", "choice": direction}}}),
            request={"state": request_state, "questions": questions},
            model_candidate=selected,
            candidates=candidates,
            candidate_plans=[],
            choice_map={direction: direction},
            selected=selected,
            model_choice=direction,
            blocked=blocked,
            route_after=route_after,
            route_override=False,
            override_kind=None,
            planning=planning,
        )

    def _execute(
        self,
        *,
        choice: str,
        model_direction: str | None,
        model_choice: str | None,
        confidence: float | None,
        latency_ms: float,
        raw: Any,
        request: dict[str, Any],
        model_candidate: dict[str, Any] | None,
        candidates: list[dict[str, Any]],
        candidate_plans: list[dict[str, Any]],
        choice_map: dict[str, str],
        selected: dict[str, Any],
        blocked: list[dict[str, str]],
        route_after: RoutePlan,
        route_override: bool,
        override_kind: str | None,
        planning: dict[str, Any],
        context_audit: dict[str, Any] | None = None,
        controller: str = "legacy",
        model_error: str | None = None,
        model_called: bool = True,
    ) -> dict[str, Any]:
        head = self._snake[0]
        config_before = self._config()
        head_before = head
        food_before = self._food
        snake_before = list(self._snake)
        distance_before = self._cycle_distance(head_before, food_before)
        best_distance = min(candidate["distance"] for candidate in candidates)
        selected_distance = selected["distance"]
        baseline_direction = _direction_between(
            head_before,
            self._cycle[(self._cycle_index[head_before] + 1) % len(self._cycle)],
        )
        baseline_candidate = next(
            (candidate for candidate in candidates if candidate["direction"] == baseline_direction),
            None,
        )
        baseline_distance = baseline_candidate["distance"] if baseline_candidate is not None else selected_distance
        shortcut_saved = max(0, baseline_distance - selected_distance)
        extra_distance = selected_distance - best_distance
        safe_options = [candidate["direction"] for candidate in candidates]
        dx, dy = DIRECTION_VECTORS[choice]
        new_head = (head[0] + dx, head[1] + dy)
        if new_head not in {(point[0], point[1]) for point in self._cycle}:
            raise ModelDecisionError("安全候选在执行前失效：目标越界")

        food_eaten = new_head == self._food
        self._snake = [new_head, *self._snake] if food_eaten else [new_head, *self._snake[:-1]]
        if len(set(self._snake)) != len(self._snake):
            raise ModelDecisionError("安全候选在执行前失效：发生蛇身碰撞")
        self._direction = choice
        self._steps += 1
        if food_eaten:
            self._level_eaten += 1
            self._total_eaten += 1

        vitals_before = {"health": self._health, "energy": self._energy}
        # Guarded execution already performs the verified planner move; a
        # corrected model proposal is audited but does not incur a penalty.
        actual_repeat = (
            controller == "guarded"
            and not food_eaten
            and (self._level, self._food, tuple(self._snake)) in self._strategy_seen
        )
        actual_longer = (
            controller == "guarded" and planning["proof"] == "proved" and route_after.proof == "proved"
            and 1 + route_after.remaining > planning["plannedRemaining"]
        )
        penalty_applied = (controller != "guarded" and override_kind == "longer_proved") or actual_repeat or actual_longer
        health_delta = -min(5, self._health) if penalty_applied else 0
        energy_delta = -min(10, self._energy) if penalty_applied else 0
        self._health += health_delta
        self._energy += energy_delta
        after_damage = {"health": self._health, "energy": self._energy}
        food_recovery = {"health": 0, "energy": 0}
        level_recovery = {"health": 0, "energy": 0}
        completed_level = self._level
        level_completed: dict[str, Any] | None = None
        if self._health <= 0:
            self._status = "lost"
            self._loss_reason = "health_depleted"
            self._planner_cache = None
            self._planner_phase = "LOST"
            if food_eaten:
                # A fatal bite is recorded but cannot trigger recovery or a
                # level transition after the damage check.
                self._food = new_head
        elif food_eaten:
            before_health, before_energy = self._health, self._energy
            self._health = min(MAX_HEALTH, self._health + 3)
            self._energy = min(MAX_ENERGY, self._energy + 12)
            food_recovery = {
                "health": self._health - before_health,
                "energy": self._energy - before_energy,
            }
            if self._level_eaten >= self._config().target:
                completed_config = self._config()
                next_level = self._level + 1 if self._level < MAX_LEVEL else None
                level_completed = {
                    "level": completed_level,
                    "eaten": self._level_eaten,
                    "target": completed_config.target,
                    "nextLevel": next_level,
                    "won": next_level is None,
                }
                if next_level is None:
                    self._status = "won"
                    self._planner_cache = None
                    self._planner_phase = "DONE"
                    # Keep a coordinate-shaped food value for the stable
                    # schema after the final bite.
                    self._food = new_head
                else:
                    before_health, before_energy = self._health, self._energy
                    self._health = min(MAX_HEALTH, self._health + 15)
                    self._energy = min(MAX_ENERGY, self._energy + 25)
                    level_recovery = {
                        "health": self._health - before_health,
                        "energy": self._energy - before_energy,
                    }
                    self._initialize_level(next_level)
            else:
                self._food = self._spawn_food()
                self._planner_cache = None
                self._planner_phase = "PLAN_FOOD"
        elif route_after.moves:
            self._planner_cache = RouteCache.from_plan(
                food=self._food,
                level=self._level,
                snake=self._snake,
                plan=route_after,
            )
            self._planner_phase = "FOLLOW_ROUTE"
        else:
            self._planner_cache = None
            self._planner_phase = "PLAN_FOOD"
        planning["phaseAfter"] = self._planner_phase
        planning["executedRemaining"] = 0 if food_eaten else len(route_after.moves)
        if controller == "guarded":
            signature = (self._level, self._food, tuple(self._snake))
            if food_eaten:
                self._strategy_seen = {signature: 1}
                self._strategy_no_food_steps = 0
            else:
                self._strategy_seen[signature] = self._strategy_seen.get(signature, 0) + 1
                self._strategy_no_food_steps += 1

        planned_steps = planning["plannedRemaining"]
        executed_steps = planning["executedRemaining"]
        proposed_steps = planning["proposedRemaining"]
        if proposed_steps is None:
            model_extra_steps = None
        else:
            model_extra_steps = proposed_steps - planned_steps

        if food_eaten:
            outcome = "food"
            headline = "吃到能量，蛇身增长！"
            if level_completed is not None:
                headline = "吃到能量，完成本关并进入下一关。" if not level_completed["won"] else "吃到最后能量，挑战完成！"
            if penalty_applied:
                headline += " 本步实际执行触发扣罚。" if controller == "guarded" else " 本步模型路线虽被证明更长，仍按规则扣罚。"
        elif override_kind == "longer_proved":
            outcome = "detour"
            headline = (
                f"{self.model_provider.capitalize()} 提议 {proposed_steps} 步；安全约束内已证明最短路线为 {planned_steps} 步，"
                f"引擎执行{DIRECTION_LABELS[choice]}，{'实际违规扣罚' if penalty_applied else '纠正后不扣血'}。"
            )
        elif override_kind == "model_error":
            outcome = "forced"
            headline = f"{self.model_provider.capitalize()} 请求或解析失败；引擎沿已验证安全路线执行。"
        elif override_kind == "unproven_fallback":
            outcome = "forced"
            headline = f"{self.model_provider.capitalize()} 选择{DIRECTION_LABELS[model_direction] if model_direction else '未知'}；引擎沿安全回退路线执行，最优性未知。"
        elif override_kind in {"lookahead_preference", "lookahead_unproved"}:
            outcome = "lookahead"
            headline = "本颗路线同长，采用食后条件期望不变差的完整路线。"
        elif model_direction is not None and model_direction != choice:
            outcome = "efficient"
            headline = f"{self.model_provider.capitalize()} 提议等长 {proposed_steps} 步，接受已证明分支执行{DIRECTION_LABELS[choice]}。"
        elif planning["proof"] == "proved":
            outcome = "efficient"
            headline = f"当前 food 在安全约束内已证明最短 {planned_steps} 步，执行{DIRECTION_LABELS[choice]}。"
        else:
            outcome = "forced"
            headline = f"沿已验证安全路线执行{DIRECTION_LABELS[choice]}，当前最优性未知。"
        if self._status == "lost":
            headline = f"{headline} 生命值归零，游戏结束。"
        extra_steps = None
        if planning["proposedRemaining"] is not None:
            extra_steps = max(0, planning["proposedRemaining"] - planning["plannedRemaining"])
        penalty_reason = None
        if penalty_applied:
            if controller == "guarded":
                extra_steps = max(0, 1 + route_after.remaining - planning["plannedRemaining"])
                penalty_reason = "实际重复完整局面且未进食，按规则扣罚。" if actual_repeat else f"实际执行的已证路线多 {extra_steps} 步，按规则扣罚。"
            else:
                penalty_reason = f"已证明模型路线多 {extra_steps} 步，执行后按规则扣除生命和能量。"
        penalty = {
            "applied": penalty_applied,
            "healthDelta": health_delta,
            "energyDelta": energy_delta,
            "extraSteps": extra_steps if penalty_applied else None,
            "reason": penalty_reason,
        }
        vitals = {
            "before": vitals_before,
            "afterDamage": after_damage,
            "after": {"health": self._health, "energy": self._energy},
            "foodRecovery": food_recovery,
            "levelRecovery": level_recovery,
        }
        if override_kind == "longer_proved":
            route_note = f" {self.model_provider.capitalize()} 提议 {proposed_steps} 步，较已证明路线多 {model_extra_steps} 步。"
        elif override_kind == "unproven_fallback":
            route_note = f" {self.model_provider.capitalize()} 分支未完成最优性证明，沿已验证安全回退路线执行。"
        elif override_kind in {"lookahead_preference", "lookahead_unproved"}:
            route_note = " 只比较本次提供的完整路线的食后条件期望，不代表该首方向所有后续路线都更差。"
        elif model_direction is not None and model_direction != choice:
            route_note = f" {self.model_provider.capitalize()} 与引擎接受等长已证明分支。"
        else:
            route_note = ""
        analysis = {
            "headBefore": _as_point(head_before),
            "foodBefore": _as_point(food_before),
            "boardSize": config_before.size,
            "snakeBefore": [_as_point(point) for point in snake_before],
            "distanceBefore": distance_before,
            "distanceAfter": selected_distance,
            "baselineDistance": baseline_distance,
            "shortcutSaved": shortcut_saved,
            "bestDistance": best_distance,
            "extraDistance": extra_distance,
            "modelExtraDistance": model_extra_steps,
            "modelExtraSteps": model_extra_steps,
            "distanceMetric": "cycleDistance（环序参考格数，不是实际移动步数）",
            "safeOptions": safe_options,
            "blocked": copy.deepcopy(blocked),
            "outcome": outcome,
            "headline": headline,
            "explanation": (
                (
                    f"当前 food 的真实规划本步前剩 {planned_steps} 步，执行后剩 {executed_steps} 步；"
                    "该路线已由完整动态蛇身搜索在安全环序约束内证明。"
                    if planning["proof"] == "proved"
                    else f"当前 food 的真实最优步数未证明；执行已验证安全路线后剩 {executed_steps} 步。"
                )
                + route_note
                + f" 环序参考为 {distance_before}→{selected_distance} 格（不是实际步数）。"
            ),
        }

        if not model_called and model_error is None:
            reason = (
                f"多个方向安全，但只有当前方向达到已证明的最短步数；规划器直接执行，本步未调用 {self.model_provider.capitalize()}。"
                if planning.get("forcedReason") == "unique_proved_shortest"
                else f"唯一安全方向，规划器按已验证路线执行；本步未调用 {self.model_provider.capitalize()}。"
            )
        elif override_kind == "model_error":
            reason = f"{self.model_provider.capitalize()} 请求/解析失败：{model_error}；planner 已接管执行。"
        elif route_override:
            reason = (
                f"引擎根据已验证路线执行 {choice}；{self.model_provider.capitalize()} 原始选择 {model_direction}，"
                f"纠正类型 {override_kind}。（非模型生成推理）"
            )
        elif model_direction is not None and model_direction != choice:
            reason = f"{self.model_provider.capitalize()} 选择 {model_direction}，引擎接受等长已证明路线执行 {choice}。（非模型生成推理）"
        else:
            reason = (
                f"引擎规划：当前 food 真实路线本步前剩 {planned_steps} 步，"
                f"执行后剩 {executed_steps} 步；模型选择未被替换。（非模型生成推理）"
                if planning["proof"] == "proved"
                else "引擎沿已验证安全回退路线执行，当前最优性未知。（非模型生成推理）"
            )
        decision: dict[str, Any] = {
            "id": uuid4().hex,
            "level": completed_level,
            "step": self._steps,
            "chosen": model_direction,
            "executed": choice,
            "modelChoice": model_choice,
            "confidence": confidence,
            "latencyMs": latency_ms,
            "reason": reason,
            "candidates": copy.deepcopy(candidates),
            "candidatePlans": copy.deepcopy(candidate_plans),
            "choiceMap": copy.deepcopy(choice_map),
            "request": copy.deepcopy(request),
            "raw": copy.deepcopy(raw),
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "safetyOverride": False,
            "foodEaten": food_eaten,
            "analysis": analysis,
            "routeOverride": route_override,
            "overrideKind": override_kind,
            "planning": copy.deepcopy(planning),
            "controller": controller,
            "modelError": model_error,
            "modelCalled": model_called,
            "modelCallStatus": (
                "failed" if model_error is not None else "succeeded" if model_called else "skipped"
            ),
            "model": self.model_provider,
            "executionSource": "planner_forced" if not model_called and model_error is None else "planner_fallback" if route_override else self.model_provider,
            "contextAudit": copy.deepcopy(context_audit) if context_audit is not None else None,
            "penalty": penalty,
            "vitals": vitals,
            "gameOver": self._status in {"won", "lost"},
        }
        if level_completed is not None:
            # The compact numeric value is convenient for the sidebar; the
            # full transition details remain available in levelCompletedInfo.
            decision["levelCompleted"] = completed_level
            decision["levelCompletedInfo"] = level_completed

        self._recent_feedback.append(
            {
                "plannedDirection": planning["plannedDirection"],
                "modelDirection": model_direction,
                "executedDirection": choice,
                "proof": planning["proof"],
                "routeOverride": route_override,
                "overrideKind": override_kind,
                "penaltyApplied": penalty_applied,
                "extraSteps": extra_steps if penalty_applied else None,
                "foodBefore": food_before,
                "foodEaten": food_eaten,
                "fullRoute": [choice, *route_after.moves],
                "modelCalled": model_called,
            }
        )
        self._recent_feedback = self._recent_feedback[-8:]
        self._logs.append(decision)
        if len(self._logs) > MAX_DECISIONS:
            self._logs.pop(0)
            self._logs_truncated += 1
        return {"state": self.state(), "decision": copy.deepcopy(decision)}


__all__ = [
    "BOARD_LEVELS",
    "DIRECTION_VECTORS",
    "GameFinishedError",
    "LEVEL_CONFIGS",
    "MAX_DECISIONS",
    "MAX_ENERGY",
    "MAX_HEALTH",
    "MAX_LEVEL",
    "ModelDecisionError",
    "ModelUnavailableError",
    "SnakeGame",
    "build_hamiltonian_cycle",
]
