"""Deterministic engine checks that do not require a running Laya service."""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

try:
    from .engine import (
        GameFinishedError,
        LEVEL_CONFIGS,
        MAX_LEVEL,
        ModelDecisionError,
        SnakeGame,
        build_hamiltonian_cycle,
    )
except ImportError:
    from engine import GameFinishedError, MAX_LEVEL, LEVEL_CONFIGS, ModelDecisionError, SnakeGame, build_hamiltonian_cycle

try:
    from . import app as snake_app
except ImportError:
    import app as snake_app

try:
    from . import engine as engine_module
except ImportError:
    import engine as engine_module

try:
    from .planner import legal_transition
except ImportError:
    from planner import legal_transition

try:
    from .strategy_planner import legal_transition as strategy_legal_transition
except ImportError:
    from strategy_planner import legal_transition as strategy_legal_transition

try:
    from .strategy_planner import evaluate_candidates
except ImportError:
    from strategy_planner import evaluate_candidates


def plan_label(model_state: dict, direction: str) -> str:
    return next(item["id"] for item in model_state["候选方案"] if item["direction"] == direction)


class SnakeEngineTests(unittest.TestCase):
    @staticmethod
    def _certified_fixture(level, snake, food):
        from dataclasses import replace
        game = SnakeGame(seed=14)
        # These are recorded 8×8 regressions, independent of live board sizes.
        game._config = lambda selected=None: replace(LEVEL_CONFIGS[(game.level if selected is None else selected) - 1], size=8)
        game._initialize_level(level)
        game._snake = list(snake)
        game._food = food
        game._direction = 'left'
        game._level_eaten = len(snake) - game._config().initial_length
        game._strategy_seen = {(level, food, tuple(snake)): 1}
        return game

    def test_certified_shortcuts_execute_the_promised_food_distance(self) -> None:
        from test_certified_routes import STEP_334_SNAKE, STEP_1142_SNAKE
        from certified_routes import cycle_id, validate_certificate
        for level, snake, food, old, new in ((4, STEP_334_SNAKE, (4,4), 18,4), (8, STEP_1142_SNAKE, (7,6),50,6)):
            with self.subTest(level=level):
                game = self._certified_fixture(level, snake, food)
                self.assertEqual(game._compute_food_plan().remaining, old)
                rng = game._random.getstate()
                records = []
                for _ in range(new):
                    result = game.step(lambda *_: {'answers': {'direction': {'type': 'choice', 'choice': 'bad'}}})
                    records.append(result['decision'])
                    self.assertEqual(game._health, 100)
                    if not records[-1]['foodEaten']:
                        self.assertEqual(game._random.getstate(), rng)
                evidence = records[0]['planning']['cycleOptimization']
                self.assertTrue(evidence['switched'])
                self.assertEqual(evidence['baselineSteps'], old)
                self.assertEqual(evidence['selectedSteps'], new)
                self.assertEqual(evidence['savedSteps'], old-new)
                cycle = tuple(map(tuple, evidence['certificateCycle']))
                self.assertTrue(validate_certificate(size=8, cycle=cycle, snake=snake, food=food, moves=tuple(evidence['referenceRoute'])))
                self.assertTrue(all(r['planning']['cycleId'] == cycle_id(cycle) for r in records))
                self.assertTrue(records[-1]['foodEaten'])
                self.assertFalse(any(r['foodEaten'] for r in records[:-1]))
                self.assertEqual(len(game._snake), len(snake)+1)

    def test_food_sampling_is_independent_of_active_cycle_order(self) -> None:
        a, b = SnakeGame(seed=45), SnakeGame(seed=45)
        b._cycle = (*b._cycle[17:], *b._cycle[:17])
        b._cycle_index = {p:i for i,p in enumerate(b._cycle)}
        self.assertEqual([a._spawn_food() for _ in range(10)], [b._spawn_food() for _ in range(10)])

    def test_certified_cycle_and_partial_execution_roll_back_on_exception(self) -> None:
        from unittest.mock import patch
        from test_certified_routes import STEP_334_SNAKE
        game = self._certified_fixture(4, STEP_334_SNAKE, (4,4))
        before, cycle, rng = game.state(), game._cycle, game._random.getstate()
        def fail_staged(*_):
            self.assertNotEqual(game._cycle, cycle)
            raise RuntimeError('staged failure')
        with patch.object(game, '_legacy_step', side_effect=fail_staged):
            with self.assertRaises(RuntimeError):
                game.step(lambda *_: {})
        self.assertEqual(game.state(), before)
        self.assertEqual(game._random.getstate(), rng)
        self.assertEqual(game.logs(), [])
        game = SnakeGame(seed=20260922, certified_shortcuts=False)
        game.step(lambda *_: {})
        before, rng, logs = game.state(), game._random.getstate(), game.logs()
        execute = game._execute
        def fail_after_move(**kwargs):
            execute(**kwargs)
            raise RuntimeError('after food mutation')
        with patch.object(game, '_execute', side_effect=fail_after_move):
            with self.assertRaises(RuntimeError):
                game.step(lambda *_: {})
        self.assertEqual(game.state(), before)
        self.assertEqual(game._random.getstate(), rng)
        self.assertEqual(game.logs(), logs)

    def test_lookahead_keeps_current_distance_and_preserves_selected_suffix(self) -> None:
        from dataclasses import replace
        from unittest.mock import patch
        from future_planner import FutureFoodCost
        game = SnakeGame(seed=2041590040, lookahead_enabled=True, certified_shortcuts=False)
        # This seed supplies two equal-cost choices on the historical 10×10 board.
        game._config = lambda selected=None: replace(LEVEL_CONFIGS[(game.level if selected is None else selected) - 1], size=10)
        game._random.seed(game.seed)
        game._initialize_level(1)
        planned = game._compute_food_plan()
        options, _ = game._candidate_plans(game._compute_move_options()[0], planned)
        tied = [p for p in options if p['routeSteps'] == planned.remaining]
        self.assertGreaterEqual(len(tied), 2)
        old_direction = planned.next_direction
        better = next(p for p in tied if p['direction'] != old_direction)
        better_end = tuple(map(tuple, better['afterFoodSnake']))
        rng = game._random.getstate()
        def metric(**kwargs):
            total = 100 if kwargs['snake'] == better_end else 200
            return FutureFoodCost('proved', 91, 91, total, total/91, 10, 0, None)
        with patch.object(engine_module, 'future_food_cost', side_effect=metric):
            result = game.step(lambda s,q: {'answers': {'direction': {'type': 'choice', 'choice': next(p['id'] for p in s['候选方案'] if p['direction'] == old_direction)}}})
        d = result['decision']
        self.assertEqual(d['chosen'], old_direction)
        self.assertEqual(d['executed'], better['direction'])
        self.assertEqual(d['overrideKind'], 'lookahead_preference')
        self.assertEqual(d['planning']['plannedRemaining'], d['planning']['proposedRemaining'])
        self.assertEqual(d['planning']['lookahead']['executed']['totalSteps'], 100)
        self.assertFalse(d['penalty']['applied'])
        self.assertEqual(game._random.getstate(), rng)
        cached = game._planner_cache.as_plan()
        self.assertEqual(list(cached.moves), better['fullRoute'][1:])
        following, _ = game._candidate_plans(game._compute_move_options()[0], cached)
        retained = next(p for p in following if p['direction'] == cached.next_direction)
        self.assertEqual(retained['fullRoute'], list(cached.moves))
        self.assertEqual(tuple(map(tuple, retained['afterFoodSnake'])), better_end)

    def test_lookahead_unknown_never_claims_an_improvement(self) -> None:
        from unittest.mock import patch
        from future_planner import FutureFoodCost
        game = SnakeGame(seed=2041590040, lookahead_enabled=True, certified_shortcuts=False)
        planned = game._compute_food_plan()
        options, suffixes = game._candidate_plans(game._compute_move_options()[0], planned)
        unknown = FutureFoodCost('unknown', 91, 1, None, None, None, 1200, 'budget')
        with patch.object(engine_module, 'future_food_cost', return_value=unknown):
            selected, info = game._future_route_reference(options, suffixes, planned)
        self.assertFalse(info['active'])
        self.assertEqual(selected, planned)

    def test_guarded_failed_seeds_clear_first_level_with_unhelpful_model(self) -> None:
        overrides = 0
        for seed in (1745012862, 1825549762, 354053230):
            game = SnakeGame(seed=seed, skip_deterministic_model=False)
            seen = set()
            while game.level == 1 and game.status == "playing" and game.steps < 500:
                signature = (game._food, tuple(game._snake))
                self.assertNotIn(signature, seen)
                seen.add(signature)
                result = game.step(lambda _s, q: {"answers": {"direction": {"type": "choice", "choice": next(iter(q['direction']['criteria']))}}})
                d = result["decision"]
                self.assertEqual(d["controller"], "guarded")
                self.assertIsNone(d["modelError"])
                self.assertFalse(d["penalty"]["applied"])
                self.assertEqual(d["planning"]["plannedRemaining"] - 1, d["planning"]["executedRemaining"])
                self.assertEqual(result["state"]["health"], 100)
                self.assertEqual(result["state"]["energy"], 100)
                overrides += d["routeOverride"]
                if d["routeOverride"]:
                    self.assertNotEqual(d["chosen"], d["executed"])
                    self.assertEqual(d["executed"], d["planning"]["plannedDirection"])
                    self.assertEqual(d["choiceMap"][d["raw"]["answers"]["direction"]["choice"]], d["chosen"])
            self.assertEqual(game.level, 2)
            self.assertEqual(game.total_eaten, 7)
        self.assertGreater(overrides, 0)

    def test_guarded_model_failures_preserve_raw_and_use_planner(self) -> None:
        for response in ("invalid response", {"answers": {"direction": {"type": "choice", "choice": "invalid"}}}, {"usage": {"input_tokens": 1}}):
            game = SnakeGame(seed=2041590040, skip_deterministic_model=False)
            d = game.step(lambda *_: response)["decision"]
            self.assertEqual(d["raw"], response)
            self.assertIsNone(d["chosen"])
            self.assertTrue(d["modelError"])
            self.assertEqual(d["overrideKind"], "model_error")
            self.assertEqual(d["executed"], d["planning"]["plannedDirection"])
            self.assertFalse(d["penalty"]["applied"])
            self.assertEqual(game.steps, 1)
        game = SnakeGame(seed=2041590040, skip_deterministic_model=False)
        d = game.step(lambda *_: (_ for _ in ()).throw(RuntimeError("offline")))["decision"]
        self.assertIsNone(d["raw"])
        self.assertIn("offline", d["modelError"])
        self.assertTrue(d["routeOverride"])

    def test_guarded_overbudget_uses_planner_without_sending_truncated_input(self) -> None:
        from unittest.mock import patch
        game = SnakeGame(seed=2041590040, skip_deterministic_model=False)
        with patch.object(engine_module, "_estimate_context_tokens", return_value=1025):
            d = game.step(lambda *_: self.fail("Oversized request was sent"))["decision"]
        self.assertFalse(d["contextAudit"]["requestSent"])
        self.assertIsNone(d["raw"])
        self.assertTrue(d["modelError"])
        self.assertEqual(game.steps, 1)
        self.assertEqual(game.state()["health"], 100)

    def test_unique_safe_move_is_not_counted_as_a_laya_decision(self) -> None:
        game = SnakeGame(seed=20260922)
        self.assertEqual(len(game._compute_move_options()[0]), 1)
        d = game.step(lambda *_: self.fail("No real choice should not call Laya"))["decision"]
        self.assertFalse(d["modelCalled"])
        self.assertEqual(d["executionSource"], "planner_forced")
        self.assertIsNone(d["raw"])
        self.assertIsNone(d["request"])
        self.assertIsNone(d["modelChoice"])
        self.assertIsNone(d["contextAudit"])
        self.assertFalse(d["routeOverride"])
        self.assertEqual(game.steps, 1)

    def test_guarded_actual_repeat_still_has_penalty(self) -> None:
        game = SnakeGame(seed=20260922)
        direction = game._compute_food_plan().next_direction
        transition = strategy_legal_transition(size=game._config().size, snake=game._snake, food=game._food, direction=direction)
        self.assertFalse(transition.ate_food)
        game._strategy_seen[(game.level, game._food, transition.snake)] = 1
        d = game.step(lambda *_: (_ for _ in ()).throw(RuntimeError("offline")))["decision"]
        self.assertTrue(d["penalty"]["applied"])
        self.assertEqual(d["penalty"]["healthDelta"], -5)
        self.assertIn("实际重复", d["penalty"]["reason"])

    def test_even_square_cycles_are_valid(self) -> None:
        for size in (8, 10, 12, 14):
            cycle = build_hamiltonian_cycle(size)
            self.assertEqual(len(cycle), size * size)
            self.assertEqual(len(set(cycle)), size * size)
            for current, following in zip(cycle, (*cycle[1:], cycle[0])):
                self.assertEqual(abs(current[0] - following[0]) + abs(current[1] - following[1]), 1)

    def test_legacy_ring_helper_completes_all_levels(self) -> None:
        game = SnakeGame(seed=20260922)
        seen_levels: set[int] = set()
        max_steps = 20_000
        level_ten_initial_length: int | None = None

        while game.status != "won" and game.steps < max_steps:
            state_before = game.state()
            seen_levels.add(state_before["level"])
            if state_before["level"] == MAX_LEVEL and state_before["eaten"] == 0:
                level_ten_initial_length = len(state_before["snake"])
            candidates = game.legacy_safe_candidates()
            self.assertTrue(candidates)
            result = game.advance_with_direction(candidates[0]["direction"], confidence=0.8)
            state = result["state"]
            self.assertEqual(state["status"], "playing" if state["level"] < MAX_LEVEL else state["status"])
            self.assertEqual(len(state["snake"]), len({tuple(point) for point in state["snake"]}))
            size = state["boardSize"]
            self.assertTrue(all(0 <= x < size and 0 <= y < size for x, y in state["snake"]))
            if state["status"] == "playing":
                self.assertNotIn(tuple(state["food"]), {tuple(point) for point in state["snake"]})

        self.assertEqual(game.status, "won")
        self.assertEqual(seen_levels, set(range(1, MAX_LEVEL + 1)))
        self.assertEqual(game.steps, len(game.logs()))
        self.assertEqual(sum(config.target for config in LEVEL_CONFIGS), 118)
        self.assertEqual(game.total_eaten, 118)
        self.assertEqual(level_ten_initial_length, 48)
        self.assertEqual(len(game.state()["snake"]), 63)
        self.assertEqual(game.state()["status"], "won")

    def test_state_exposes_progressive_levels_and_blocked_moves(self) -> None:
        game = SnakeGame(seed=12)
        state = game.state()
        levels = state["levels"]
        self.assertEqual(state["totalLevels"], 10)
        self.assertEqual(len(levels), 10)
        self.assertEqual([item["size"] for item in levels], [14] * 3 + [12] * 7)
        self.assertEqual([item["target"] for item in levels], [7, 8, 9, 10, 11, 12, 14, 16, 16, 15])
        self.assertEqual([item["initialLength"] for item in levels], [8, 11, 15, 12, 18, 24, 30, 36, 42, 48])
        self.assertEqual([item["intervalMs"] for item in levels], sorted((item["intervalMs"] for item in levels), reverse=True))
        self.assertEqual(sum(item["target"] for item in levels), 118)

        safe = {candidate["direction"] for candidate in state["candidates"]}
        blocked = {item["direction"] for item in state["blocked"]}
        self.assertEqual(safe & blocked, set())
        self.assertEqual(safe | blocked, {"up", "right", "down", "left"})
        self.assertTrue(all(item["kind"] in {"wall", "body", "order", "food"} for item in state["blocked"]))

    def test_runtime_dynamic_cost_is_not_hamiltonian_ring_cost(self) -> None:
        options = [item.as_dict() for item in evaluate_candidates(
            size=12,
            snake=[(4, 8), (4, 9), (4, 10)],
            food=(10, 8),
            current_direction="up",
        )]
        by_direction = {item["direction"]: item for item in options}
        self.assertEqual(by_direction["right"]["foodSteps"], 6)
        self.assertEqual(by_direction["up"]["foodSteps"], 8)
        self.assertEqual(by_direction["left"]["foodSteps"], 10)
        self.assertTrue(by_direction["right"]["tailReachableAfter"])
        self.assertGreaterEqual(by_direction["right"]["exitsAfter"], 1)

        game = SnakeGame(seed=0)
        game._snake = [(1, 1), (1, 2), (1, 3)]
        game._food = (6, 1)
        runtime_options, _ = game._strategy_option_dicts()
        self.assertEqual(next(item for item in runtime_options if item["direction"] == "right")["foodSteps"], 5)

    def test_real_corner_food_trap_records_loss_without_safe_override(self) -> None:
        game = SnakeGame(seed=0)
        game._initialize_level(7)
        game._snake = [(1, 0), (1, 1), (0, 1), (0, 2), (1, 2), (2, 2), (2, 1), (2, 0)]
        game._food = (0, 0)
        game._direction = "up"
        game._strategy_seen = {(7, game._food, tuple(game._snake)): 1}
        self.assertIn("right", {item["direction"] for item in game.safe_candidates()})
        self.assertIn("left", {item["direction"] for item in game.safe_candidates()})
        result = game._direct_strategy_step(
            lambda model_state, _questions: {
                "answers": {"direction": {"type": "choice", "choice": plan_label(model_state, "left")}}
            }
        )
        self.assertTrue(result["decision"]["foodEaten"])
        self.assertEqual(result["state"]["status"], "lost")
        self.assertEqual(result["state"]["lossReason"], "trapped")
        self.assertEqual(result["state"]["health"], 0)
        self.assertEqual(result["decision"]["vitals"]["foodRecovery"], {"health": 0, "energy": 0})
        self.assertEqual(result["decision"]["vitals"]["terminalLoss"]["reason"], "trapped")
        self.assertEqual(result["decision"]["optimality"]["status"], "failed")

    def test_survival_repeated_state_penalty_and_tradeoff_without_penalty(self) -> None:
        game = SnakeGame(seed=0)
        options, _ = game._strategy_option_dicts()
        direction = options[0]["direction"]
        transition = strategy_legal_transition(
            size=game._config().size,
            snake=game._snake,
            food=game._food,
            direction=direction,
            current_direction=game._direction,
        )
        self.assertIsNotNone(transition)
        assert transition is not None
        game._strategy_seen[(game._level, game._food, transition.snake)] = 1
        repeated = game._direct_strategy_step(
            lambda model_state, _questions: {
                "answers": {"direction": {"type": "choice", "choice": plan_label(model_state, direction)}}
            }
        )
        self.assertEqual(repeated["decision"]["judgement"]["kind"], "repeat")
        self.assertTrue(repeated["decision"]["penalty"]["applied"])

        game = SnakeGame(seed=0)
        game._snake = [(5, 5), (5, 4), (5, 3), (5, 2), (5, 1), (6, 1), (6, 2), (6, 3)]
        game._food = (4, 3)
        game._direction = "down"
        game._strategy_seen = {(game._level, game._food, tuple(game._snake)): 1}
        options, _ = game._strategy_option_dicts()
        self.assertEqual(next(item for item in options if item["direction"] == "right")["foodSteps"], 7)
        self.assertEqual(next(item for item in options if item["direction"] == "down")["foodSteps"], 5)
        tradeoff = game._direct_strategy_step(
            lambda model_state, _questions: {
                "answers": {"direction": {"type": "choice", "choice": plan_label(model_state, "right")}}
            }
        )
        self.assertEqual(tradeoff["decision"]["judgement"]["kind"], "tradeoff")
        self.assertFalse(tradeoff["decision"]["penalty"]["applied"])

    def test_decision_analysis_is_derived_from_candidates(self) -> None:
        game = SnakeGame(seed=2026)
        before = game.state()
        direction = before["candidates"][0]["direction"]
        submitted: dict[str, object] = {}

        def decider(model_state, questions):
            submitted["state"] = model_state
            submitted["questions"] = questions
            return {
                "answers": {
                    "direction": {"type": "choice", "choice": plan_label(model_state, direction), "confidence": 0.7}
                }
            }

        result = game._direct_strategy_step(
            decider
        )
        decision = result["decision"]
        analysis = decision["analysis"]
        candidate = next(item for item in before["candidates"] if item["direction"] == direction)
        self.assertEqual(analysis["headBefore"], before["snake"][0])
        self.assertEqual(analysis["foodBefore"], before["food"])
        self.assertEqual(decision["request"], submitted)
        self.assertEqual(analysis["boardSize"], before["boardSize"])
        self.assertEqual(analysis["level"], before["level"])
        self.assertIn(analysis["outcome"], {"food", "efficient", "tradeoff", "dominated", "repeat", "unknown", "forced"})
        self.assertTrue(analysis["headline"])
        self.assertTrue(analysis["explanation"])

    def test_candidate_plan_chains_match_real_legal_prefixes(self) -> None:
        game = SnakeGame(seed=0)
        candidates, _ = game._compute_move_options()
        planned = game._compute_food_plan()
        public, plans = game._candidate_plans(candidates, planned)
        by_direction = {item["direction"]: item for item in public}

        for direction, branch in plans.items():
            item = by_direction[direction]
            if direction == planned.next_direction:
                self.assertEqual(item["chain"], list(planned.moves[:6]))
            else:
                self.assertEqual(item["chain"], [direction, *branch.moves[:5]])
            current = tuple(game._snake)
            for move in item["chain"]:
                transition = legal_transition(
                    size=game._config().size,
                    cycle=game._cycle,
                    snake=current,
                    food=game._food,
                    direction=move,
                )
                self.assertIsNotNone(transition)
                current = transition.snake

    def test_compact_max_body_request_stays_under_token_budget(self) -> None:
        from laya.common import build_sequence
        from transformers import AutoTokenizer

        tokenizer_path = Path(__file__).resolve().parents[1] / "laya-zh" / "model" / "tokenizer"
        tokenizer = AutoTokenizer.from_pretrained(tokenizer_path, local_files_only=True)
        for level, length in ((1, 8), (7, 30), (MAX_LEVEL, 63)):
            with self.subTest(level=level, length=length):
                game = SnakeGame(seed=0)
                game._initialize_level(level)
                cycle = game._cycle
                game._snake = [cycle[(50 - offset) % len(cycle)] for offset in range(length)]
                game._food = cycle[51] if length == 63 else cycle[60]
                game._recent_feedback = [{"executedDirection": "up", "foodEaten": False, "penaltyApplied": True}] * 8
                options, _ = game._strategy_option_dicts()
                choice_map = game._assign_plan_ids(options)
                state = game._model_state([], candidate_plans=options, choice_map=choice_map, strategy_mode=True)
                questions = game._questions([], candidate_plans=options, choice_map=choice_map, strategy_mode=True)
                audit = game._context_audit(state, questions, options)
                q = questions["direction"]
                ids, _ = build_sequence(tokenizer, state, {"t": "choice", "ins": q["instructions"], "crit": q["criteria"]}, max_len=1024, head_max_len=256)
                self.assertLessEqual(audit["inputTokens"], 1024)
                self.assertEqual(audit["inputTokens"], len(ids))
                self.assertIn("CTX_END", tokenizer.decode(ids))
                self.assertTrue(all(audit[k] for k in ("boardComplete", "bodyComplete", "knownRoutesComplete")))
                self.assertEqual(audit["historyCount"], 8)
                body_line = next(line for line in state["context"].splitlines() if line.startswith("SNAKE H>T "))
                start, links = body_line.removeprefix("SNAKE H>T ").split(" links=")
                decoded = [tuple(map(int, start.split(",")))]
                vectors = {"U": (0, -1), "R": (1, 0), "D": (0, 1), "L": (-1, 0)}
                for letter, count in re.findall(r"([URDL])(\d+)", links):
                    dx, dy = vectors[letter]
                    for _ in range(int(count)):
                        decoded.append((decoded[-1][0] + dx, decoded[-1][1] + dy))
                self.assertEqual(decoded, game._snake)
                for option in options:
                    if option["proof"] != "proved":
                        self.assertIsNone(option["fullRoute"])
                        continue
                    self.assertEqual(len(option["fullRoute"]), option["foodSteps"])
                    snake = game._snake
                    for direction in option["fullRoute"]:
                        transition = strategy_legal_transition(size=game._config().size, snake=snake, food=game._food, direction=direction)
                        self.assertIsNotNone(transition)
                        snake = transition.snake
                    self.assertEqual(snake[0], game._food)

    def test_context_budget_and_mismatch_do_not_execute(self) -> None:
        from unittest.mock import patch
        game = SnakeGame(seed=4)
        before = game.state()
        with patch.object(engine_module, "_estimate_context_tokens", return_value=1025):
            with self.assertRaises(ModelDecisionError):
                game._direct_strategy_step(lambda *_: self.fail("Oversized input reached model"))
        self.assertEqual(game.state(), before)
        with self.assertRaises(ModelDecisionError):
            game._direct_strategy_step(lambda _s, q: {"usage": {"input_tokens": 1}, "answers": {"direction": {"choice": next(iter(q['direction']['criteria']))}}})
        self.assertEqual(game.state(), before)

    def test_invalid_model_choice_does_not_advance_or_log(self) -> None:
        game = SnakeGame(seed=4)
        before = game.state()

        with self.assertRaises(ModelDecisionError):
            game._direct_strategy_step(
                lambda _state, _questions: {
                    "status": "ok",
                    "answers": {
                        "direction": {
                            "type": "choice",
                            "choice": "unsafe-direction",
                            "confidence": 0.99,
                        }
                    },
                }
            )

        self.assertEqual(game.state(), before)
        self.assertEqual(game.logs(), [])

    def test_strict_longer_proved_branch_is_corrected_and_penalized(self) -> None:
        game = SnakeGame(seed=0, policy_mode="strict_shortest")
        game._snake = [(4, 4), (4, 5), (4, 6)]
        game._food = (8, 4)
        game._direction = "up"
        game._strategy_seen = {(1, game._food, tuple(game._snake)): 1}
        result = game._direct_strategy_step(
            lambda model_state, _questions: {
                "answers": {"direction": {"type": "choice", "choice": plan_label(model_state, "up"), "confidence": 0.6}}
            }
        )
        decision = result["decision"]
        self.assertEqual(decision["chosen"], "up")
        self.assertEqual(decision["executed"], "up")
        self.assertFalse(decision["routeOverride"])
        self.assertTrue(decision["penalty"]["applied"])
        self.assertEqual(decision["judgement"]["kind"], "dominated")
        self.assertEqual(decision["optimality"]["status"], "not_best")
        self.assertNotIn("optimality", decision["request"])
        self.assertEqual(result["state"]["health"], 95)
        self.assertEqual(result["state"]["energy"], 90)
        self.assertEqual(result["state"]["status"], "playing")

    def test_equal_proved_branch_is_accepted_without_penalty(self) -> None:
        game = SnakeGame(seed=15)
        result = game._direct_strategy_step(
            lambda model_state, _questions: {
                "answers": {"direction": {"type": "choice", "choice": plan_label(model_state, "left"), "confidence": 0.6}}
            }
        )
        decision = result["decision"]
        self.assertEqual(decision["chosen"], "left")
        self.assertEqual(decision["executed"], "left")
        self.assertFalse(decision["routeOverride"])
        self.assertIsNone(decision["overrideKind"])
        self.assertEqual(decision["planning"]["plannedDirection"], "left")
        self.assertFalse(decision["routeOverride"])
        self.assertEqual(decision["analysis"]["outcome"], "efficient")
        self.assertNotIn("多绕", decision["analysis"]["headline"])
        self.assertNotIn("捷径", decision["analysis"]["headline"])
        self.assertIn(decision["optimality"]["status"], {"metrics_best", "tradeoff", "unproven"})
        self.assertEqual(result["state"]["health"], 100)

    def test_unproved_fallback_never_penalizes(self) -> None:
        previous_budget = engine_module.STRATEGY_NODE_BUDGET
        engine_module.STRATEGY_NODE_BUDGET = 0
        try:
            game = SnakeGame(seed=0)
            result = game._direct_strategy_step(
                lambda model_state, _questions: {
                    "answers": {"direction": {"type": "choice", "choice": plan_label(model_state, "left"), "confidence": 0.6}}
                }
            )
        finally:
            engine_module.STRATEGY_NODE_BUDGET = previous_budget
        decision = result["decision"]
        self.assertFalse(decision["routeOverride"])
        self.assertEqual(decision["strategyOptions"][0]["proof"], "unknown")
        self.assertFalse(decision["penalty"]["applied"])
        self.assertEqual(decision["optimality"]["status"], "unproven")
        self.assertEqual(result["state"]["health"], 100)

    def test_food_and_level_recovery_apply_after_damage(self) -> None:
        game = SnakeGame(seed=0, policy_mode="strict_shortest")
        game._snake = [(4, 4), (4, 5), (4, 6)]
        game._food = (5, 4)
        game._direction = "up"
        game._strategy_seen = {(1, game._food, tuple(game._snake)): 1}
        game._health = 50
        game._energy = 40
        result = game._direct_strategy_step(
            lambda model_state, _questions: {
                "answers": {"direction": {"type": "choice", "choice": plan_label(model_state, "right")}}
            }
        )
        self.assertEqual(result["decision"]["vitals"]["foodRecovery"], {"health": 3, "energy": 12})
        self.assertEqual(result["state"]["health"], 53)
        self.assertEqual(result["state"]["energy"], 52)
        follow_up = game._direct_strategy_step(
            lambda model_state, _questions: {
                "answers": {
                    "direction": {"type": "choice", "choice": next(iter(model_state["候选方案"]))["id"]}
                }
            }
        )
        self.assertEqual(follow_up["state"]["status"], "playing")

        game = SnakeGame(seed=0)
        game._snake = [(4, 4), (4, 5), (4, 6)]
        game._food = (5, 4)
        game._direction = "up"
        game._strategy_seen = {(1, game._food, tuple(game._snake)): 1}
        game._level_eaten = LEVEL_CONFIGS[0].target - 1
        game._health = 50
        game._energy = 40
        result = game._direct_strategy_step(
            lambda model_state, _questions: {
                "answers": {"direction": {"type": "choice", "choice": plan_label(model_state, "right")}}
            }
        )
        self.assertEqual(result["decision"]["vitals"]["levelRecovery"], {"health": 15, "energy": 25})
        self.assertEqual(result["state"]["level"], 2)
        self.assertEqual(result["state"]["eaten"], 0)
        self.assertEqual(result["state"]["health"], 68)
        self.assertEqual(result["state"]["energy"], 77)

    def test_health_zero_loses_without_recovery_and_cannot_step(self) -> None:
        game = SnakeGame(seed=0, policy_mode="strict_shortest")
        game._snake = [(4, 4), (4, 5), (4, 6)]
        game._food = (5, 4)
        game._direction = "up"
        game._strategy_seen = {(1, game._food, tuple(game._snake)): 1}
        game._health = 5
        game._energy = 5
        result = game._direct_strategy_step(
            lambda model_state, _questions: {
                "answers": {"direction": {"type": "choice", "choice": plan_label(model_state, "up")}}
            }
        )
        self.assertEqual(result["state"]["status"], "lost")
        self.assertEqual(result["state"]["planner"]["phase"], "LOST")
        self.assertEqual(result["decision"]["vitals"]["foodRecovery"], {"health": 0, "energy": 0})
        self.assertTrue(result["decision"]["gameOver"])
        with self.assertRaises(GameFinishedError):
            game._direct_strategy_step(lambda _state, _questions: {})

        game = SnakeGame(seed=0, policy_mode="strict_shortest")
        game._snake = [(4, 4), (4, 5), (4, 6)]
        game._food = (5, 4)
        game._direction = "up"
        game._strategy_seen = {(1, game._food, tuple(game._snake)): 1}
        game._health = 50
        game._energy = 0
        result = game._direct_strategy_step(
            lambda model_state, _questions: {
                "answers": {"direction": {"type": "choice", "choice": plan_label(model_state, "up")}}
            }
        )
        self.assertEqual(result["state"]["status"], "playing")
        self.assertEqual(result["state"]["health"], 45)
        self.assertEqual(result["state"]["energy"], 0)

    def test_model_exception_does_not_advance_or_log(self) -> None:
        game = SnakeGame(seed=5)
        before = game.state()

        with self.assertRaises(RuntimeError):
            game._direct_strategy_step(lambda _state, _questions: (_ for _ in ()).throw(RuntimeError("model down")))

        self.assertEqual(game.state(), before)
        self.assertEqual(game.logs(), [])

    def test_model_failure_keeps_cached_route_and_vitals(self) -> None:
        game = SnakeGame(seed=0)
        game._direct_strategy_step(
            lambda _model_state, questions: {
                "answers": {
                    "direction": {
                        "type": "choice",
                        "choice": next(iter(questions["direction"]["criteria"])),
                    }
                }
            }
        )
        before = game.state()
        with self.assertRaises(RuntimeError):
            game._direct_strategy_step(lambda _state, _questions: (_ for _ in ()).throw(RuntimeError("model down")))
        self.assertEqual(game.state(), before)

    def test_tail_cell_can_be_entered_after_growth(self) -> None:
        """Eating immediately before the tail must leave one safe follow-up."""

        game = SnakeGame(seed=1)
        cycle = game._cycle  # The setup targets the ring-boundary edge case.
        head_index = 2 * game._config().size
        game._snake = [cycle[head_index - offset] for offset in range(3)]
        game._food = cycle[(head_index - 3) % len(cycle)]
        self.assertIn("right", {candidate["direction"] for candidate in game.legacy_safe_candidates()})

        result = game._direct_strategy_step(
            lambda model_state, _questions: {
                "answers": {"direction": {"type": "choice", "choice": plan_label(model_state, "right")}}
            }
        )

        old_tail = cycle[head_index - 2]
        follow_up = result["state"]["candidates"]
        self.assertTrue(follow_up)
        follow_up_directions = {candidate["direction"] for candidate in follow_up}
        new_head = tuple(result["state"]["snake"][0])
        self.assertTrue(
            any(
                (new_head[0] + delta[0], new_head[1] + delta[1]) == old_tail
                for direction, delta in {
                    "up": (0, -1),
                    "right": (1, 0),
                    "down": (0, 1),
                    "left": (-1, 0),
                }.items()
                if direction in follow_up_directions
            )
        )

    def test_single_safe_direction_has_model_compatibility_alias(self) -> None:
        game = SnakeGame(seed=19)
        game._snake = [(0, 0), (1, 0), (1, 1)]
        game._food = (5, 5)
        game._direction = "up"
        game._strategy_seen = {(1, game._food, tuple(game._snake)): 1}
        candidates = game.safe_candidates()
        self.assertEqual(len(candidates), 1)
        direction = candidates[0]["direction"]

        result = game._direct_strategy_step(
            lambda _state, questions: {
                "answers": {
                    "direction": {
                        "type": "choice",
                        "choice": next(
                            key
                            for key in questions["direction"]["criteria"]
                            if key.endswith("_safe")
                        ),
                    }
                }
            }
        )

        model_choice = result["decision"]["modelChoice"]
        self.assertTrue(model_choice.endswith("_safe"))
        self.assertEqual(result["decision"]["choiceMap"][model_choice], direction)
        self.assertEqual(result["decision"]["chosen"], direction)
        self.assertEqual(result["decision"]["executed"], direction)

    def test_persistent_logs_survive_delete(self) -> None:
        class FakeLaya:
            @staticmethod
            def decide(_state, questions):
                choice = next(
                    key for key in questions["direction"]["criteria"] if not key.endswith("_safe")
                )
                return {
                    "status": "ok",
                    "answers": {
                        "direction": {"type": "choice", "choice": choice, "confidence": 0.75}
                    },
                }

        old_root = snake_app.GAME_STORAGE_ROOT
        old_client = snake_app.laya_client
        snake_app.games.clear()
        with tempfile.TemporaryDirectory() as temporary_directory:
            snake_app.GAME_STORAGE_ROOT = Path(temporary_directory) / "games"
            snake_app.laya_client = FakeLaya()
            try:
                with TestClient(snake_app.app) as client:
                    created = client.post("/api/game", json={"seed": 23})
                    self.assertEqual(created.status_code, 200)
                    created_payload = created.json()
                    game_id = created_payload["id"]
                    game_dir = snake_app.GAME_STORAGE_ROOT / game_id
                    self.assertTrue((game_dir / "metadata.json").is_file())
                    metadata = json.loads((game_dir / "metadata.json").read_text(encoding="utf-8"))
                    self.assertEqual(metadata["seed"], 23)
                    self.assertEqual(len(metadata["levels"]), 10)

                    stepped = client.post(f"/api/game/{game_id}/step", json={"expectedStep": 0})
                    self.assertEqual(stepped.status_code, 200)
                    decision = stepped.json()["decision"]
                    self.assertIn("request", decision)
                    self.assertIn("raw", decision)
                    self.assertIn("analysis", decision)

                    decisions_path = game_dir / "decisions.jsonl"
                    lines = decisions_path.read_text(encoding="utf-8").splitlines()
                    self.assertEqual(len(lines), 1)
                    persisted = json.loads(lines[0])
                    self.assertEqual(persisted["id"], decision["id"])
                    self.assertIn("request", persisted)

                    deleted = client.delete(f"/api/game/{game_id}")
                    self.assertEqual(deleted.status_code, 200)
                    self.assertTrue(game_dir.is_dir())
                    historical = client.get(f"/api/game/{game_id}/logs")
                    self.assertEqual(historical.status_code, 200)
                    self.assertEqual(historical.json()["count"], 1)
                    self.assertEqual(historical.json()["logs"][0]["id"], decision["id"])
            finally:
                snake_app.games.clear()
                snake_app.GAME_STORAGE_ROOT = old_root
                snake_app.laya_client = old_client


if __name__ == "__main__":
    unittest.main()
