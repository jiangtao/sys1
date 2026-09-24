"""Independent shortest-path and moving-tail checks for the relaxed heuristic."""

from collections import deque
import unittest

from engine import build_hamiltonian_cycle
from planner import _cycle_lower_bounds, legal_transition, plan_food


class PlannerBoundsTests(unittest.TestCase):
    def test_bounds_cover_all_legal_edges_and_cycle_changes(self):
        for size in (4, 8, 12, 14):
            canonical = build_hamiltonian_cycle(size)
            for cycle in (canonical, tuple(reversed(canonical))):
                index = {cell: i for i, cell in enumerate(cycle)}
                food = cycle[len(cycle) // 3]
                bounds = _cycle_lower_bounds(cycle, food)
                self.assertEqual(bounds[index[food]], 0)
                for head_index, head in enumerate(cycle):
                    self.assertGreaterEqual(bounds[head_index], abs(head[0] - food[0]) + abs(head[1] - food[1]))
                    self.assertLessEqual(bounds[head_index], (index[food] - head_index) % len(cycle))
                    snake = tuple(cycle[(head_index - k) % len(cycle)] for k in range(min(7, len(cycle) - 1)))
                    if food in snake:
                        continue
                    for direction in ('up', 'right', 'down', 'left'):
                        transition = legal_transition(size=size, cycle=cycle, snake=snake, food=food, direction=direction)
                        if transition is not None:
                            following = index[transition.snake[0]]
                            self.assertLess((index[food] - following) % len(cycle), (index[food] - head_index) % len(cycle))
                            self.assertLessEqual(bounds[head_index], 1 + bounds[following])

    def test_astar_matches_independent_breadth_first_search(self):
        size = 4
        cycle = build_hamiltonian_cycle(size)
        for length, head_index, food_index in ((2, 0, 9), (5, 8, 14), (10, 12, 2), (3, 5, 11), (7, 10, 13)):
            snake = tuple(cycle[(head_index - k) % len(cycle)] for k in range(length))
            food = cycle[food_index]
            self.assertNotIn(food, snake)
            queue, seen = deque([(snake, 0)]), {snake}
            optimum = None
            while queue:
                current, cost = queue.popleft()
                if current[0] == food:
                    optimum = cost
                    break
                for direction in ('up', 'right', 'down', 'left'):
                    step = legal_transition(size=size, cycle=cycle, snake=current, food=food, direction=direction)
                    if step is not None and step.snake not in seen:
                        seen.add(step.snake)
                        queue.append((step.snake, cost + 1))
            plan = plan_food(size=size, cycle=cycle, snake=snake, food=food)
            self.assertEqual(plan.proof, 'proved')
            self.assertEqual(plan.remaining, optimum)
            current = snake
            for direction in plan.moves:
                transition = legal_transition(size=size, cycle=cycle, snake=current, food=food, direction=direction)
                self.assertIsNotNone(transition)
                current = transition.snake
            self.assertEqual(current[0], food)
            self.assertEqual(len(current), len(snake) + 1)


if __name__ == '__main__':
    unittest.main()
