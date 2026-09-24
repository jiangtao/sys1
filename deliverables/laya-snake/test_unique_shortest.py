"""Regression checks for guarded planner/model ownership at route ties."""

from __future__ import annotations

import unittest

try:
    from .engine import SnakeGame
except ImportError:
    from engine import SnakeGame


def _choice_for_direction(model_state: dict, direction: str) -> str:
    return next(
        item["id"]
        for item in model_state["候选方案"]
        if item["direction"] == direction
    )


class GuardedRouteOwnershipTests(unittest.TestCase):
    def test_unique_proved_shortest_skips_laya_and_keeps_all_safe_routes(self) -> None:
        # This seeded physical layout has two safe moves.  The planner's
        # concrete routes are left=5 and down=7, so left is uniquely shortest.
        game = SnakeGame(seed=0, skip_deterministic_model=True)
        before = game.state()
        before_snake = tuple(map(tuple, before["snake"]))
        before_health = before["health"]
        calls: list[object] = []

        def fail_laya(*args: object) -> None:
            calls.append(args)
            self.fail("unique proved shortest route must not call Laya")

        result = game.step(fail_laya)
        decision = result["decision"]
        after = result["state"]
        plans = decision["candidatePlans"]

        self.assertEqual(calls, [])
        self.assertFalse(decision["modelCalled"])
        self.assertEqual(decision["executionSource"], "planner_forced")
        self.assertEqual(decision["planning"]["forcedReason"], "unique_proved_shortest")
        for field in ("raw", "request", "modelChoice", "contextAudit", "chosen"):
            self.assertIsNone(decision[field])

        self.assertEqual({item["direction"] for item in plans}, {"left", "down"})
        self.assertEqual(
            {item["direction"]: item["routeSteps"] for item in plans},
            {"left": 5, "down": 7},
        )
        self.assertTrue(all(item["proof"] == "proved" for item in plans))
        self.assertEqual(decision["planning"]["plannedDirection"], "left")
        self.assertEqual(decision["executed"], "left")
        self.assertEqual(tuple(after["snake"][0]), (6, 7))
        self.assertEqual(tuple(map(tuple, after["snake"][1:])), before_snake[:-1])
        self.assertEqual(after["health"], before_health)
        self.assertFalse(decision["penalty"]["applied"])

    def test_only_physical_safe_direction_uses_distinct_forced_reason(self) -> None:
        game = SnakeGame(seed=20260922)
        before = game.state()
        self.assertEqual(len(before["candidates"]), 1)
        calls: list[object] = []

        def fail_laya(*args: object) -> None:
            calls.append(args)
            self.fail("only physically safe direction must not call Laya")

        result = game.step(fail_laya)
        decision = result["decision"]

        self.assertEqual(calls, [])
        self.assertFalse(decision["modelCalled"])
        self.assertEqual(decision["executionSource"], "planner_forced")
        self.assertEqual(decision["planning"]["forcedReason"], "only_safe_direction")
        self.assertEqual(decision["executed"], "up")
        self.assertEqual(decision["planning"]["plannedDirection"], "up")
        self.assertFalse(decision["penalty"]["applied"])
        self.assertEqual(result["state"]["health"], before["health"])

    def test_equal_proved_shortest_routes_call_laya_and_accept_tied_proposal(self) -> None:
        # This seeded layout has left=8 and down=8.  The planner recommends
        # down, while a model proposal for the tied left route is admissible.
        game = SnakeGame(seed=9)
        before = game.state()
        before_snake = tuple(map(tuple, before["snake"]))
        calls: list[dict] = []

        def choose_tied_left(model_state: dict, questions: dict) -> dict:
            calls.append({"state": model_state, "questions": questions})
            return {
                "answers": {
                    "direction": {
                        "type": "choice",
                        "choice": _choice_for_direction(model_state, "left"),
                    }
                }
            }

        result = game.step(choose_tied_left)
        decision = result["decision"]
        after = result["state"]
        plans = decision["candidatePlans"]

        self.assertEqual(len(calls), 1)
        self.assertTrue(decision["modelCalled"])
        self.assertEqual(decision["executionSource"], "laya")
        self.assertEqual(decision["chosen"], "left")
        self.assertEqual(decision["executed"], "left")
        self.assertFalse(decision["routeOverride"])
        self.assertEqual(
            {item["direction"]: item["routeSteps"] for item in plans},
            {"left": 8, "down": 8},
        )
        self.assertTrue(all(item["proof"] == "proved" for item in plans))
        self.assertEqual(tuple(after["snake"][0]), (4, 1))
        self.assertEqual(tuple(map(tuple, after["snake"][1:])), before_snake[:-1])
        self.assertEqual(after["health"], before["health"])
        self.assertFalse(decision["penalty"]["applied"])


if __name__ == "__main__":
    unittest.main()
