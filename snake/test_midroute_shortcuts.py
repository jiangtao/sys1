"""Regression checks for bounded mid-route certified shortcut probes."""

from __future__ import annotations

import copy
import json
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

try:
    from . import engine as engine_module
    from .certified_routes import validate_certificate
    from .engine import LEVEL_CONFIGS, SnakeGame, build_hamiltonian_cycle
    from .planner import RouteCache, plan_food
except ImportError:
    import engine as engine_module
    from certified_routes import validate_certificate
    from engine import LEVEL_CONFIGS, SnakeGame, build_hamiltonian_cycle
    from planner import RouteCache, plan_food


FIXTURES = json.loads(
    (Path(__file__).with_name("test_midroute_shortcuts_fixtures.json")).read_text(encoding="utf-8")
)


class MidrouteShortcutTests(unittest.TestCase):
    def _game(self, fixture: dict, *, active_cycle=None) -> tuple[SnakeGame, RouteCache]:
        game = SnakeGame(seed=2_040_915_040, midroute_shortcuts=True)
        game._config = lambda selected=None: replace(
            LEVEL_CONFIGS[(game.level if selected is None else selected) - 1],
            size=fixture["board_size"],
        )
        game._initialize_level(fixture["level"])
        if active_cycle is not None:
            game._cycle = tuple(tuple(point) for point in active_cycle)
            game._cycle_index = {point: index for index, point in enumerate(game._cycle)}
        snake = tuple(tuple(point) for point in fixture["snake"])
        food = tuple(fixture["food"])
        game._snake = list(snake)
        game._food = food
        game._level_eaten = fixture["level_eaten"]
        game._total_eaten = fixture["total_eaten"]
        game._steps = fixture["source_step"] - 1
        game._direction = "right"
        game._strategy_seen = {(game.level, food, snake): 1}
        plan = plan_food(
            size=fixture["board_size"],
            cycle=game._cycle,
            snake=snake,
            food=food,
        )
        self.assertEqual(plan.proof, "proved")
        self.assertEqual(plan.remaining, fixture["planned_remaining"])
        cache = RouteCache.from_plan(food=food, level=game.level, snake=snake, plan=plan)
        game._planner_cache = cache
        return game, cache

    @staticmethod
    def _checkpoint(game: SnakeGame) -> dict:
        checkpoint = game.__dict__.copy()
        for key in ("_snake", "_logs", "_recent_feedback"):
            checkpoint[key] = list(checkpoint[key])
        checkpoint["_strategy_seen"] = dict(game._strategy_seen)
        checkpoint["_random"] = copy.copy(game._random)
        return checkpoint

    @staticmethod
    def _restore(game: SnakeGame, checkpoint: dict, *, attempts: int, goal) -> None:
        game.__dict__.clear()
        game.__dict__.update(checkpoint)
        game._straight_probe_attempts = attempts
        game._straight_probe_goal = goal

    def test_frozen_midroute_states_switch_and_replay_independent_certificates(self) -> None:
        for name in ("step391", "step905", "step1830"):
            fixture = FIXTURES[name]
            with self.subTest(name=name):
                game, old_cache = self._game(fixture)
                old_cycle = game._cycle
                rng_before = game._random.getstate()
                result = game.step(lambda *_: {})
                decision = result["decision"]
                optimization = decision["planning"]["cycleOptimization"]

                self.assertTrue(optimization["switched"])
                self.assertEqual(optimization["trigger"], "mid_route_straight")
                self.assertEqual(decision["planning"]["phaseBefore"], "FOLLOW_ROUTE")
                self.assertGreater(optimization["savedSteps"], 0)
                self.assertEqual(optimization["baselineSteps"], fixture["planned_remaining"])
                self.assertEqual(optimization["selectedSteps"], fixture["selected_remaining"])
                self.assertEqual(optimization["certificateNodeBudget"], 2000)
                self.assertNotEqual(game._cycle, old_cycle)
                self.assertEqual(game._random.getstate(), rng_before)
                self.assertFalse(decision["foodEaten"])
                self.assertEqual(decision["planning"]["plannedRemaining"], fixture["selected_remaining"])
                self.assertEqual(game._planner_cache.as_plan().remaining, fixture["selected_remaining"] - 1)

                certificate_cycle = tuple(tuple(point) for point in optimization["certificateCycle"])
                reference_route = tuple(optimization["referenceRoute"])
                self.assertTrue(
                    validate_certificate(
                        size=fixture["board_size"],
                        cycle=certificate_cycle,
                        snake=tuple(tuple(point) for point in fixture["snake"]),
                        food=tuple(fixture["food"]),
                        moves=reference_route,
                    )
                )

    def test_budget_exhaustion_keeps_old_cache_cycle_and_route(self) -> None:
        fixture = FIXTURES["step13"]
        game, old_cache = self._game(fixture)
        old_cycle = game._cycle
        rng_before = game._random.getstate()
        result = game.step(lambda *_: {})
        decision = result["decision"]
        optimization = decision["planning"]["cycleOptimization"]

        self.assertFalse(optimization["switched"])
        self.assertEqual(optimization["trigger"], "mid_route_straight")
        self.assertEqual(optimization["certificateNodeBudget"], 2000)
        self.assertEqual(optimization["searchExpanded"], 2000)
        self.assertEqual(optimization["reason"], "certificate_budget_exhausted")
        self.assertEqual(optimization["baselineSteps"], fixture["planned_remaining"])
        self.assertEqual(optimization["selectedSteps"], fixture["planned_remaining"])
        self.assertEqual(decision["executed"], old_cache.next_direction)
        self.assertEqual(decision["planning"]["phaseBefore"], "FOLLOW_ROUTE")
        self.assertEqual(decision["penalty"]["healthDelta"], 0)
        self.assertEqual(game._health, 100)
        self.assertEqual(game._cycle, old_cycle)
        self.assertEqual(game._planner_cache.as_plan().remaining, fixture["planned_remaining"] - 1)
        self.assertEqual(game._random.getstate(), rng_before)

    def test_probe_limit_is_four_per_food_and_goal_change_resets_it(self) -> None:
        fixture = FIXTURES["step13"]
        game, _ = self._game(fixture)
        baseline = self._checkpoint(game)
        calls: list[dict] = []
        real_find = engine_module.find_certified_shortcut

        def counted(*args, **kwargs):
            calls.append(dict(kwargs))
            return real_find(*args, **kwargs)

        with patch.object(engine_module, "find_certified_shortcut", side_effect=counted):
            goal = None
            for attempt in range(1, 5):
                decision = game.step(lambda *_: {})["decision"]
                optimization = decision["planning"]["cycleOptimization"]
                self.assertEqual(optimization["trigger"], "mid_route_straight")
                self.assertEqual(optimization["probeAttempt"], attempt)
                self.assertFalse(optimization["switched"])
                self.assertEqual(optimization["certificateNodeBudget"], 2000)
                goal = game._straight_probe_goal
                self._restore(game, baseline, attempts=attempt, goal=goal)

            self.assertEqual(len(calls), 4)
            self.assertTrue(all(call["node_budget"] == 2000 for call in calls))
            decision = game.step(lambda *_: {})["decision"]
            self.assertNotIn("cycleOptimization", decision["planning"])
            self.assertEqual(len(calls), 4)

        game, _ = self._game(fixture)
        game._straight_probe_goal = (game.level, game.total_eaten)
        game._straight_probe_attempts = 4
        eaten = False
        for _ in range(fixture["planned_remaining"] + 1):
            decision = game.step(lambda *_: {})["decision"]
            eaten = decision["foodEaten"]
            if eaten:
                break
        self.assertTrue(eaten)
        self.assertEqual(game._straight_probe_attempts, 4)
        decision = game.step(lambda *_: {})["decision"]
        self.assertEqual(decision["planning"]["cycleOptimization"]["trigger"], "initial_food")
        self.assertEqual(decision["planning"]["cycleOptimization"]["certificateNodeBudget"], 50000)
        self.assertEqual(game._straight_probe_attempts, 0)
        self.assertEqual(game._straight_probe_goal, (game.level, game.total_eaten))

    def test_failed_execution_rolls_back_certificate_cycle_cache_rng_and_probe_count(self) -> None:
        fixture = FIXTURES["step391"]
        game, old_cache = self._game(fixture)
        before = self._checkpoint(game)
        old_cycle = game._cycle
        rng_before = game._random.getstate()

        def fail_after_certificate(_decider):
            self.assertNotEqual(game._cycle, old_cycle)
            raise RuntimeError("injected execution failure")

        with patch.object(game, "_legacy_step", side_effect=fail_after_certificate):
            with self.assertRaisesRegex(RuntimeError, "injected execution failure"):
                game.step(lambda *_: {})

        self.assertEqual(game._snake, before["_snake"])
        self.assertEqual(game._cycle, old_cycle)
        self.assertEqual(game._planner_cache, old_cache)
        self.assertEqual(game._random.getstate(), rng_before)
        self.assertEqual(game._straight_probe_attempts, 0)
        self.assertIsNone(game._straight_probe_goal)
        self.assertEqual(game._steps, before["_steps"])
        self.assertEqual(game.logs(), [])


if __name__ == "__main__":
    unittest.main()
