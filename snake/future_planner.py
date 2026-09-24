"""Pure one-step lookahead over the next uniformly sampled food cell.

This is an analysis metric only.  It evaluates the expected next-food cost
after a supplied, already-eaten current-food terminal snake state; it never
chooses a route or mutates a game/RNG.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

try:
    from .planner import plan_food
except ImportError:
    from planner import plan_food


Coordinate = tuple[int, int]


@dataclass(frozen=True)
class FutureFoodCost:
    proof: str
    target_count: int
    evaluated_targets: int
    total_steps: int | None
    mean_steps: float | None
    max_steps: int | None
    expanded: int
    reason: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "proof": self.proof,
            "targetCount": self.target_count,
            "evaluatedTargets": self.evaluated_targets,
            "totalSteps": self.total_steps,
            "meanSteps": self.mean_steps,
            "maxSteps": self.max_steps,
            "expanded": self.expanded,
            "reason": self.reason,
        }


@lru_cache(maxsize=512)
def future_food_cost(
    *,
    size: int,
    cycle: tuple[Coordinate, ...],
    snake: tuple[Coordinate, ...],
    node_budget: int = 1_200,
) -> FutureFoodCost:
    """Compute exact next-food costs when every target search is proved.

    The cache key includes the complete board cycle, ordered snake, size and
    budget.  Tuple arguments are intentional so callers cannot accidentally
    provide mutable state to the cached pure function.
    """

    occupied = set(snake)
    free_targets = tuple(cell for cell in cycle if cell not in occupied)
    target_count = len(free_targets)
    if target_count == 0:
        return FutureFoodCost("not_applicable", 0, 0, None, None, None, 0, "no_free_cells")

    total = 0
    maximum = 0
    expanded = 0
    for index, food in enumerate(free_targets, start=1):
        try:
            result = plan_food(
                size=size,
                cycle=cycle,
                snake=snake,
                food=food,
                node_budget=node_budget,
            )
        except RuntimeError as error:
            # The legacy planner may be unable to construct its unproved
            # ring fallback (notably with a zero budget).  That is still an
            # unknown target cost; never turn the exception into statistics.
            return FutureFoodCost(
                "unknown",
                target_count,
                index,
                None,
                None,
                None,
                expanded,
                f"target={food!r}: {error}",
            )
        expanded += result.expanded
        if result.proof != "proved":
            reason = f"target={food!r}: {result.fallback_reason or 'route_unproved'}"
            return FutureFoodCost("unknown", target_count, index, None, None, None, expanded, reason)
        total += result.remaining
        maximum = max(maximum, result.remaining)

    return FutureFoodCost(
        "proved",
        target_count,
        target_count,
        total,
        total / target_count,
        maximum,
        expanded,
        None,
    )


__all__ = ["FutureFoodCost", "future_food_cost"]
