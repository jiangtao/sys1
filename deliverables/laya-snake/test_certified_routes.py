"""Focused tests for the optional certified shortcut search."""

from __future__ import annotations

import unittest
from unittest.mock import patch

try:
    from .certified_routes import (
        CertifiedShortcut,
        find_certified_shortcut,
        validate_certificate,
    )
    from .strategy_planner import StrategyPlan
except ImportError:
    from certified_routes import CertifiedShortcut, find_certified_shortcut, validate_certificate
    from strategy_planner import StrategyPlan


Coordinate = tuple[int, int]


def hamiltonian_cycle(size: int) -> tuple[Coordinate, ...]:
    path: list[Coordinate] = [(x, 0) for x in range(size)]
    for x in range(size - 1, 0, -1):
        values = range(1, size) if x % 2 else range(size - 1, 0, -1)
        path.extend((x, y) for y in values)
    path.extend((0, y) for y in range(size - 1, 0, -1))
    return tuple(path)


# Fixed snapshots from the historical run.  The tests intentionally do not
# read runtime logs so the certificate implementation remains pure/replayable.
STEP_334_SNAKE: tuple[Coordinate, ...] = (
    (2, 2),
    (3, 2),
    (3, 1),
    (4, 1),
    (5, 1),
    (5, 0),
    (4, 0),
    (3, 0),
    (2, 0),
    (1, 0),
    (0, 0),
    (0, 1),
    (0, 2),
    (0, 3),
    (0, 4),
    (0, 5),
    (0, 6),
    (1, 6),
)
STEP_1142_SNAKE: tuple[Coordinate, ...] = (
    (5, 2),
    (6, 2),
    (7, 2),
    (7, 1),
    (7, 0),
    (6, 0),
    (5, 0),
    (4, 0),
    (3, 0),
    (2, 0),
    (1, 0),
    (0, 0),
    (0, 1),
    (0, 2),
    (0, 3),
    (0, 4),
    (0, 5),
    (0, 6),
    (1, 6),
    (1, 5),
    (1, 4),
    (1, 3),
    (1, 2),
    (1, 1),
    (2, 1),
    (2, 2),
    (2, 3),
    (2, 4),
    (2, 5),
    (2, 6),
    (2, 7),
    (3, 7),
    (3, 6),
    (3, 5),
    (3, 4),
    (3, 3),
    (3, 2),
    (3, 1),
    (4, 1),
    (4, 2),
    (4, 3),
    (4, 4),
    (4, 5),
    (4, 6),
    (4, 7),
    (5, 7),
    (5, 6),
    (5, 5),
)


class CertifiedRouteTests(unittest.TestCase):
    def test_historical_shortcuts_are_certified(self) -> None:
        cycle = hamiltonian_cycle(8)
        cases = (
            (STEP_334_SNAKE, (4, 4), 18, 4),
            (STEP_1142_SNAKE, (7, 6), 50, 6),
        )
        for snake, food, baseline, selected in cases:
            with self.subTest(baseline=baseline):
                result = find_certified_shortcut(
                    size=8,
                    cycle=cycle,
                    snake=snake,
                    food=food,
                    baseline_steps=baseline,
                )
                self.assertEqual(result.reason, "certified_shortcut")
                self.assertEqual(result.selected_steps, selected)
                self.assertEqual(len(result.moves), selected)
                self.assertIsNotNone(result.cycle)
                self.assertTrue(
                    validate_certificate(
                        size=8,
                        cycle=result.cycle or (),
                        snake=snake,
                        food=food,
                        moves=result.moves,
                    )
                )

    def test_zero_budget_and_inputs_unchanged(self) -> None:
        cycle = hamiltonian_cycle(4)
        snake = (cycle[0], cycle[-1])
        cycle_before, snake_before = cycle, snake
        result = find_certified_shortcut(
            size=4,
            cycle=cycle,
            snake=snake,
            food=cycle[3],
            baseline_steps=9,
            node_budget=0,
        )
        self.assertIsNone(result.cycle)
        self.assertEqual(result.reason, "certificate_budget_exhausted")
        self.assertEqual(result.expanded, 0)
        self.assertEqual((cycle, snake), (cycle_before, snake_before))

    def test_route_not_strictly_shorter_is_not_replaced(self) -> None:
        cycle = hamiltonian_cycle(4)
        snake = (cycle[0], cycle[-1])
        result = find_certified_shortcut(
            size=4,
            cycle=cycle,
            snake=snake,
            food=cycle[3],
            baseline_steps=3,
        )
        self.assertIsNone(result.cycle)
        self.assertEqual(result.reason, "not_strictly_shorter")
        self.assertEqual(result.selected_steps, 3)

    def test_validator_rejects_bad_shape_growth_and_repeated_heads(self) -> None:
        cycle = hamiltonian_cycle(4)
        snake = (cycle[0], cycle[-1])
        self.assertTrue(
            validate_certificate(
                size=4,
                cycle=cycle,
                snake=snake,
                food=cycle[3],
                moves=("right", "right", "right"),
            )
        )
        self.assertFalse(
            validate_certificate(
                size=4,
                cycle=(*cycle[:-1], cycle[-2]),
                snake=snake,
                food=cycle[3],
                moves=("right", "right", "right"),
            )
        )

    def test_route_reentering_initial_body_is_skipped(self) -> None:
        cycle = hamiltonian_cycle(4)
        snake = ((1, 1), (1, 2), (1, 3))
        # After the first three turns the old tail (1, 3) is free, but the
        # route then re-enters that initial body cell.  It must not become a
        # certificate merely because ordinary physical movement permits it.
        fake = StrategyPlan(("right", "down", "down", "left", "left"), "proved", 5, None)
        with patch("certified_routes.physical_plan_food", return_value=fake):
            result = find_certified_shortcut(
                size=4,
                cycle=cycle,
                snake=snake,
                food=(0, 3),
                baseline_steps=12,
            )
        self.assertIsNone(result.cycle)
        self.assertEqual(result.reason, "route_replay_invalid")
        self.assertFalse(
            validate_certificate(
                size=4,
                cycle=cycle,
                snake=snake,
                food=cycle[3],
                moves=("right", "right"),
            )
        )
        self.assertFalse(
            validate_certificate(
                size=4,
                cycle=cycle,
                snake=snake,
                food=cycle[3],
                moves=("right", "right", "right", "left"),
            )
        )

    def test_cycle_id_audit_does_not_embed_full_cycle(self) -> None:
        cycle = hamiltonian_cycle(4)
        result = CertifiedShortcut(cycle, ("right",), 2, "certified_shortcut", 4, 1)
        audit = result.as_dict()
        self.assertIn("cycleId", audit)
        self.assertNotIn("cycle", audit)
        self.assertEqual(len(audit["cycleId"]), 12)


if __name__ == "__main__":
    unittest.main()
