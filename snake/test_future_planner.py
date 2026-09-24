"""Small pure tests for the optional next-food cost metric."""

from __future__ import annotations

import unittest
from collections import deque

try:
    from .engine import build_hamiltonian_cycle
    from .future_planner import FutureFoodCost, future_food_cost
    from .planner import legal_transition, DIRECTION_VECTORS
except ImportError:
    from engine import build_hamiltonian_cycle
    from future_planner import FutureFoodCost, future_food_cost
    from planner import legal_transition, DIRECTION_VECTORS


class FuturePlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        future_food_cost.cache_clear()

    def test_four_by_four_counts_and_matches_independent_costs(self) -> None:
        cycle = build_hamiltonian_cycle(4)
        snake = (cycle[0], cycle[-1])
        result = future_food_cost(size=4, cycle=cycle, snake=snake, node_budget=1200)
        independent = []
        for food in cycle:
            if food in snake:
                continue
            queue, visited = deque([(snake, 0)]), {snake}
            while queue:
                body, distance = queue.popleft()
                if body[0] == food:
                    independent.append(distance)
                    break
                for direction in DIRECTION_VECTORS:
                    move = legal_transition(size=4, cycle=cycle, snake=body, food=food, direction=direction)
                    if move is not None and move.snake not in visited:
                        visited.add(move.snake)
                        queue.append((move.snake, distance + 1))
        self.assertEqual(result.proof, "proved")
        self.assertEqual(result.target_count, 14)
        self.assertEqual(result.evaluated_targets, 14)
        self.assertEqual(result.total_steps, sum(independent))
        self.assertEqual(result.max_steps, max(independent))
        self.assertAlmostEqual(result.mean_steps or 0, sum(independent) / len(independent))

    def test_budget_zero_is_unknown_without_fake_statistics(self) -> None:
        cycle = build_hamiltonian_cycle(4)
        result = future_food_cost(size=4, cycle=cycle, snake=(cycle[0],), node_budget=0)
        self.assertEqual(result.proof, "unknown")
        self.assertIsNone(result.total_steps)
        self.assertIsNone(result.mean_steps)
        self.assertIsNone(result.max_steps)
        self.assertEqual(result.evaluated_targets, 1)

    def test_full_board_is_not_applicable(self) -> None:
        cycle = build_hamiltonian_cycle(4)
        result = future_food_cost(size=4, cycle=cycle, snake=cycle, node_budget=1200)
        self.assertEqual(result, FutureFoodCost("not_applicable", 0, 0, None, None, None, 0, "no_free_cells"))

    def test_inputs_are_unchanged_and_cache_key_is_complete(self) -> None:
        cycle = build_hamiltonian_cycle(4)
        snake = (cycle[0], cycle[-1])
        cycle_before, snake_before = cycle, snake
        first = future_food_cost(size=4, cycle=cycle, snake=snake, node_budget=1200)
        second = future_food_cost(size=4, cycle=cycle, snake=snake, node_budget=1200)
        self.assertIs(first, second)
        self.assertEqual((cycle, snake), (cycle_before, snake_before))
        changed_snake = (cycle[0], cycle[-2])
        self.assertIsNot(first, future_food_cost(size=4, cycle=cycle, snake=changed_snake, node_budget=1200))
        changed_cycle = tuple(reversed(cycle))
        self.assertIsNot(first, future_food_cost(size=4, cycle=changed_cycle, snake=snake, node_budget=1200))


if __name__ == "__main__":
    unittest.main()
