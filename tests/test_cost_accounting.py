"""Regression evidence for provider semantics, money boundaries and report joins.

All provider events and review receipts below are synthetic local fixtures. No
credentials, account APIs, model calls or live approval evidence are used.
"""
from __future__ import annotations

import importlib.util
import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ringer_costs import (CREDIT_RATES, GPT56_MODELS, RATES, SpendLedger, UsageEvent, billing_preflight,
                         canonical_model, estimate_event, inspect_usage_snapshot,
                         parse_usage_jsonl, parse_usage_lines, validate_cost_policy)
from ringer_billing import usage_evidence

SPEC = importlib.util.spec_from_file_location("cost_report", ROOT / "scripts/ringer_cost_report.py")
REPORT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REPORT)
NOW = datetime(2026, 9, 3, 8, 24, tzinfo=timezone.utc)
POLICY = {"monthly_api_cap_gbp": "1", "automatic_paid_fallback": False,
          "subscription_auth_modes": ["chatgpt"]}


def snapshot(used=74):
    return {"observed_at": "2026-09-03T08:23:39.686Z",
            "rateLimitsByLimitId": {"codex": {
                "primary": {"usedPercent": used, "windowDurationMins": 10080, "resetsAt": 1788748715},
                "secondary": None, "spendControlReached": False, "planType": "prolite",
                "credits": {"hasCredits": False, "unlimited": False, "balance": "0"}}}}


def opencode(part_id="p1", session="s1", cost="0.001", model="openrouter/z-ai/glm-5.2", provider_cost=None):
    row = {"type": "step_finish", "sessionID": session, "model": model,
           "part": {"id": part_id, "messageID": "m1", "type": "step-finish",
                    "tokens": {"total": 317, "input": 100, "output": 10, "reasoning": 5,
                               "cache": {"read": 200, "write": 2}}}}
    if cost is not None:
        row["part"]["cost"] = cost
    if provider_cost is not None:
        row["provider_cost"] = provider_cost
    return row


def reserve_in_child(directory, attempt, queue):
    queue.put(SpendLedger(Path(directory)).reserve(attempt, "0.6", "1", "2026-09"))


def crash_after_reserve(directory):
    ledger = SpendLedger(Path(directory))
    ledger.reserve("crashed", "0.8", "1", "2026-09")
    ledger.mark_started("crashed")
    os._exit(17)


class CostAccountingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def event(self, model="gpt-5.6-sol", inp=100000, cache=10000, output=1000, **kwargs):
        return UsageEvent(model, None, inp, cache, output, service_tier="standard", **kwargs)

    def parse(self, rows):
        path = self.root / "usage.jsonl"
        path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
        return parse_usage_jsonl(path)

    def report(self, rows, states=None, **kwargs):
        log = self.root / "runs.jsonl"
        log.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
        runs = self.root / "runs"
        runs.mkdir(exist_ok=True)
        for name, state in (states or {}).items():
            (runs / f"{name}.json").write_text(json.dumps(state), encoding="utf-8")
        return REPORT.build_report(log, self.root, kwargs.pop("policy", POLICY), now=NOW, **kwargs)

    def test_all_standard_short_prices_cache_write_and_output(self):
        expected = {"sol": "0.384", "terra": "0.194", "luna": "0.0194"}
        for name, amount in expected.items():
            with self.subTest(model=name):
                event = self.event("gpt-5.6-" + name)
                self.assertEqual(Decimal(amount), estimate_event(event).amount)
                # Cache write replaces one normal-input token, not an extra charge.
                with_write = replace(event, cache_write_tokens=1)
                delta = (RATES[name]["short"]["cache_write"] - RATES[name]["short"]["input"]) / 1_000_000
                self.assertEqual(Decimal(amount) + delta, estimate_event(with_write).amount)

    def test_astra_exact_standard_rates_boundary_aggregates_and_credits(self):
        short = self.event("gpt-6-astra")
        result = estimate_event(short)
        self.assertEqual(Decimal("0.96"), result.amount)
        self.assertIn("2026-09-05", result.source)

        at_boundary = self.event("gpt-6-astra", 272000, 0, 0)
        over_boundary = self.event("gpt-6-astra", 272001, 0, 0)
        self.assertEqual(Decimal("2.72"), estimate_event(at_boundary).amount)
        self.assertEqual(Decimal("5.44002"), estimate_event(over_boundary).amount)

        long = self.event(
            "gpt-6-astra", 276102, 100, 51, cache_write_tokens=2
        )
        self.assertEqual(Decimal("5.524075"), estimate_event(long).amount)

        aggregate = replace(
            self.event("gpt-6-astra", 400000, 0, 100), aggregate=True
        )
        bounded = estimate_event(aggregate)
        self.assertIsNone(bounded.amount)
        self.assertEqual(Decimal("4.005"), bounded.lower)
        self.assertEqual(Decimal("8.0075"), bounded.upper)
        self.assertEqual("aggregate-short-long-bounds", bounded.coverage)

        credits = estimate_event(short, category="credits")
        self.assertEqual(Decimal("24.00"), credits.amount)
        self.assertEqual("credits", credits.currency)
        self.assertIn("learn.chatgpt.com/docs/pricing#2026-09-05", credits.source)
        self.assertEqual(
            "unknown",
            estimate_event(replace(short, aggregate=True), category="credits").status,
        )

    def test_astra_fast_and_inexact_identities_remain_unknown(self):
        astra = self.event("gpt-6-astra")
        self.assertEqual(
            "unknown", estimate_event(replace(astra, service_tier="fast")).status
        )
        for identity in ("astra", "gpt-5.6-astra", "gpt-6-astra-latest", "openrouter/gpt-6-astra"):
            with self.subTest(identity=identity):
                self.assertIsNone(canonical_model(identity))
                self.assertEqual("unknown", estimate_event(self.event(identity)).status)

    def test_all_long_prices_with_each_counter(self):
        expected = {"sol": "2.20963", "terra": "1.104968", "luna": "0.1104968"}
        # 276000 ordinary input + 100 cached + 2 writes; 51 output.
        for name, amount in expected.items():
            event = self.event("gpt-5.6-" + name, 276102, 100, 51, cache_write_tokens=2)
            self.assertEqual(Decimal(amount), estimate_event(event).amount)

    def test_long_context_applies_per_request_not_job_sum(self):
        short = self.event(inp=200000, cache=0, output=0)
        self.assertEqual(Decimal("1.6"), sum(estimate_event(short).amount for _ in range(2)))
        self.assertEqual(Decimal("1.088"), estimate_event(replace(short, input_tokens=272000)).amount)
        self.assertEqual(Decimal("2.176008"), estimate_event(replace(short, input_tokens=272001)).amount)

    def test_codex_turn_totals_have_bounded_estimate(self):
        events = self.parse([{"type": "header", "model": "gpt-5.6-sol", "service_tier": "standard"},
                             {"type": "turn.completed", "usage": {"input_tokens": 400000,
                              "cached_input_tokens": 0, "output_tokens": 100, "reasoning_tokens": 90}}])
        self.assertTrue(events[0].aggregate)
        self.assertEqual(100, events[0].output_tokens)
        cost = estimate_event(events[0])
        self.assertEqual("estimate", cost.status)
        self.assertIsNone(cost.amount)
        self.assertEqual(Decimal("1.602"), cost.lower)
        self.assertEqual(Decimal("3.203"), cost.upper)
        self.assertEqual("header", events[0].model_source)

    def test_fast_is_only_known_56_short_and_unknown_tier_is_unknown(self):
        for name in GPT56_MODELS:
            standard = self.event("gpt-5.6-" + name)
            self.assertEqual(estimate_event(standard).amount * 2,
                             estimate_event(replace(standard, service_tier="fast")).amount)
        for event in (replace(self.event(), service_tier=None),
                      replace(self.event(inp=300000), service_tier="fast"),
                      replace(self.event("gpt-5.5"), service_tier="fast")):
            self.assertEqual("unknown", estimate_event(event).status)

    def test_all_credit_rates_separate_from_actual_usd(self):
        for name, expected in {"sol": "9.6", "terra": "4.85", "luna": "0.485"}.items():
            event = replace(self.event("gpt-5.6-" + name), actual_cost_usd=Decimal("1"))
            result = estimate_event(event, category="credits")
            self.assertEqual("credits", result.currency)
            self.assertEqual(Decimal(expected), result.amount)
            self.assertEqual(Decimal("1"), estimate_event(event).amount)
        self.assertEqual("unknown", estimate_event(replace(event, aggregate=True), category="credits").status)
        self.assertEqual("unknown", estimate_event(replace(event, cache_write_tokens=2), category="credits").status)

    def test_real_opencode_shape_and_session_scoped_dedup(self):
        first = opencode()
        events = self.parse([first, first, opencode(session="s2")])
        self.assertEqual(2, len(events))
        self.assertEqual(302, events[0].input_tokens)
        self.assertEqual(200, events[0].cached_input_tokens)
        self.assertEqual(2, events[0].cache_write_tokens)
        self.assertEqual(15, events[0].output_tokens)
        self.assertFalse(events[0].aggregate)
        self.assertIsNone(events[0].actual_cost_usd)
        self.assertEqual(Decimal("0.001"), events[0].harness_estimate_usd)
        estimate = estimate_event(events[0])
        self.assertEqual("estimate", estimate.status)
        self.assertEqual(Decimal("0.001"), estimate.amount)
        self.assertEqual("s2", events[1].session_id)

    def test_conflicting_event_id_fails_and_idless_equal_turns_are_distinct(self):
        with self.assertRaises(ValueError):
            self.parse([opencode(), opencode(cost="0.002")])
        row = {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 10}}
        self.assertEqual(2, len(self.parse([row, row])))

    def test_exact_models_header_and_attested_expected_model(self):
        for wrong in ("not-a-model-sol", "gpt-5.6-sol-unknown", "xgpt-5.6-sol", "openrouter/gpt-5.6-sol", True):
            self.assertIsNone(canonical_model(wrong))
        events = parse_usage_lines(["model: gpt-5.6-luna", "tokens used", "12,345"])
        self.assertEqual("gpt-5.6-luna", events[0].model)
        self.assertEqual(12345, events[0].total_tokens)
        self.assertEqual("unknown", estimate_event(events[0]).status)
        row = json.dumps({"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 1}})
        attested = parse_usage_lines([row], expected_model="gpt-5.6-sol")[0]
        self.assertEqual("caller-attested", attested.model_source)
        unknown = parse_usage_lines([row])[0]
        self.assertIsNone(unknown.model)

    def test_headers_reset_across_attempts_and_threads(self):
        lines = ["[ringer.py] attempt 1 started now", "model: gpt-5.6-sol", "tokens used", "10",
                 "[ringer.py] attempt 2 started now", "tokens used", "12"]
        events = parse_usage_lines(lines)
        self.assertEqual("gpt-5.6-sol", events[0].model)
        self.assertIsNone(events[1].model)

    def test_bad_counters_total_only_and_malformed_rows_stay_unknown(self):
        rows = [{"type": "turn.completed", "model": "gpt-5.6-sol", "usage": {"total_tokens": 88}},
                {"type": "turn.completed", "usage": {"input_tokens": True, "output_tokens": 5}},
                {"worker_tokens": 1000},
                {"type": "turn.completed", "usage": {"input_tokens": "10", "output_tokens": 5}}]
        for event in self.parse(rows):
            self.assertEqual("unknown", estimate_event(event).status)
        self.assertEqual("unknown", estimate_event(parse_usage_lines(['{"type":'])[0]).status)
        self.assertEqual("unknown", estimate_event(self.event(inp=5, cache=6)).status)
        self.assertEqual("unknown", estimate_event(self.event(cache=True)).status)

    def test_provider_zero_actual_overrides_missing_tokens_but_bad_cost_does_not(self):
        event = UsageEvent(None, None, None, None, None, actual_cost_usd=Decimal("0"))
        self.assertEqual("actual", estimate_event(event).status)
        self.assertEqual(Decimal("0"), estimate_event(event).amount)
        bad = self.parse([opencode(cost="NaN")])[0]
        self.assertEqual("unknown", estimate_event(bad).status)

    def test_dated_openrouter_prices_match_captured_models(self):
        policy = validate_cost_policy(json.loads((ROOT / "config/cost-policy.example.json").read_text()))
        expected = {"moonshotai/kimi-k2.7-code": "0.118", "z-ai/glm-5.2": "0.14628",
                    "z-ai/glm-5.3": "0.198", "z-ai/glm-5.3-flash": "0.0115"}
        for model, amount in expected.items():
            event = self.event("openrouter/" + model, 200000, 100000, 10000)
            cost = estimate_event(event, pricing=policy["pricing"])
            self.assertEqual(Decimal(amount), cost.amount)
            self.assertIn("2026-09-03", cost.source)
            self.assertEqual(Decimal("0.01"), estimate_event(replace(event, actual_cost_usd=Decimal("0.01")), pricing=policy["pricing"]).amount)
        self.assertEqual("unknown", estimate_event(replace(event, cache_write_tokens=2), pricing=policy["pricing"]).status)

    def test_policy_has_no_default_personal_fee_or_fx(self):
        policy = validate_cost_policy({})
        self.assertNotIn("monthly_subscription_gbp", policy)
        self.assertNotIn("fx", policy)
        self.assertEqual("0", policy["monthly_api_cap_gbp"])
        self.assertFalse(policy["automatic_paid_fallback"])

    def test_policy_rejects_wrong_types_nonfinite_negative_and_unauthorised_auth(self):
        invalid = [{"monthly_api_cap_gbp": value} for value in (True, [], {}, "NaN", "Infinity", -1)]
        # Keep the individual malformed structures readable.
        invalid += [{"automatic_paid_fallback": 1}, {"subscription_auth_modes": ["api_key"]},
                    {"subscription_auth_modes": "chatgpt"}, {"subscription_auth_modes": [True]},
                    {"snapshot_max_age_seconds": True}, {"snapshot_max_age_seconds": 0},
                    {"monthly_subscription_gbp": "NaN"}, {"unknown": 1}]
        invalid += [{"fx": {"usd_to_gbp": value, "dated": "2026-09-02"}} for value in (0, True, "NaN", -1)]
        invalid += [{"fx": {"usd_to_gbp": 1, "dated": "yesterday"}},
                    {"pricing": {"model": {"input": 1}}}]
        for policy in invalid:
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                validate_cost_policy(policy)

    def preflight(self, **kwargs):
        args = dict(engine="codex", model="gpt-5.6-sol", policy=POLICY,
                    observed_auth_mode="chatgpt", usage_snapshot=snapshot(), now=NOW)
        args.update(kwargs)
        return billing_preflight(**args)

    def test_real_snapshot_available_no_fixed_token_entitlement(self):
        result = inspect_usage_snapshot(snapshot(), now=NOW)
        self.assertTrue(result.available, result.reason)
        self.assertEqual("prolite", result.plan_type)
        self.assertEqual("0", result.credits["balance"])
        self.assertEqual(1, len(result.windows))
        self.assertTrue(self.preflight().allow)

    def test_empty_malformed_stale_and_wrong_auth_block(self):
        for value in (None, {}, [], {"exhausted": False}, snapshot(True), snapshot("74"), snapshot(float("nan"))):
            with self.subTest(snapshot=value):
                self.assertFalse(self.preflight(usage_snapshot=value).allow)
        self.assertFalse(self.preflight(now=NOW + timedelta(hours=1)).allow)
        self.assertFalse(self.preflight(now=NOW - timedelta(hours=1)).allow)
        for auth in (None, "api_key", "unknown", True):
            self.assertFalse(self.preflight(observed_auth_mode=auth).allow)
        self.assertFalse(self.preflight(policy={"subscription_auth_modes": ["api_key"]}, observed_auth_mode="api_key").allow)

    def test_exhausted_weekly_secondary_and_spend_control_block_no_fallback(self):
        values = [snapshot(100)]
        secondary = snapshot()
        secondary["rateLimitsByLimitId"]["codex"]["secondary"] = {"usedPercent": 100, "windowDurationMins": 300, "resetsAt": 1788748715}
        values.append(secondary)
        spend = snapshot()
        spend["rateLimitsByLimitId"]["codex"]["spendControlReached"] = True
        values.append(spend)
        for value in values:
            result = self.preflight(usage_snapshot=value, task_spend_allowance_gbp=1,
                                    policy={**POLICY, "automatic_paid_fallback": True})
            self.assertFalse(result.allow)
            self.assertEqual("subscription", result.route)

    def test_api_requires_explicit_allowance_atomic_reservation_and_no_relabelling(self):
        base = dict(engine="opencode", model="openrouter/z-ai/glm-5.2", observed_auth_mode="api_key",
                    state_dir=self.root, attempt_id="api", month="2026-09")
        self.assertFalse(self.preflight(**base).allow)
        self.assertFalse(self.preflight(**base, task_spend_allowance_gbp=0).allow)
        result = self.preflight(**base, task_spend_allowance_gbp="0.6", billing_route="subscription")
        self.assertTrue(result.allow, result.reason)
        self.assertEqual("api", result.route)
        self.assertFalse(self.preflight(**{**base, "attempt_id": "two"}, task_spend_allowance_gbp="0.6").allow)
        self.assertFalse(self.preflight(**{**base, "attempt_id": "three"}, task_spend_allowance_gbp="0.1", auto_fallback_requested=True).allow)
        self.assertFalse(self.preflight(billing_route="api", observed_auth_mode="chatgpt", task_spend_allowance_gbp=1).allow)

    def test_ledger_exact_idempotence_and_conflicting_id_reuse(self):
        ledger = SpendLedger(self.root)
        self.assertTrue(ledger.reserve("a", ".6", "1", "2026-09"))
        self.assertTrue(ledger.reserve("a", ".60", "1", "2026-09"))
        for amount, month in ((".1", "2026-09"), (".6", "2026-10")):
            with self.assertRaises(ValueError):
                ledger.reserve("a", amount, "1", month)
        self.assertFalse(ledger.settle("new", "0"))
        self.assertFalse(ledger.settle("a", None))
        self.assertTrue(ledger.settle("a", ".4"))
        self.assertTrue(ledger.settle("a", ".4"))
        self.assertFalse(ledger.settle("a", ".5"))
        self.assertFalse(ledger.reserve("a", ".6", "1", "2026-09"))
        row = json.loads(ledger.path.read_text())["reservations"]["a"]
        self.assertEqual("0.6", row["amount_gbp"])
        self.assertEqual("0.4", row["actual_gbp"])

    def test_parallel_reservations_cannot_race(self):
        context = multiprocessing.get_context("spawn")
        queue = context.Queue()
        children = [context.Process(target=reserve_in_child, args=(str(self.root), str(i), queue)) for i in range(4)]
        for child in children:
            child.start()
        for child in children:
            child.join(10)
        self.assertEqual([0] * 4, [child.exitcode for child in children])
        self.assertEqual(1, sum(queue.get(timeout=2) for _ in children))
        queue.close()

    def test_crash_reservation_stays_reserved(self):
        context = multiprocessing.get_context("spawn")
        child = context.Process(target=crash_after_reserve, args=(str(self.root),))
        child.start()
        child.join(10)
        self.assertEqual(17, child.exitcode)
        ledger = SpendLedger(self.root)
        self.assertFalse(ledger.reserve("new", ".3", "1", "2026-09"))
        with self.assertRaises(ValueError):
            ledger.release("crashed")

    def test_release_after_start_requires_authoritative_evidence(self):
        ledger = SpendLedger(self.root)
        ledger.reserve("a", ".6", "1", "2026-09")
        ledger.mark_started("a")
        for evidence in (None, {"kind": "timeout"}, {"kind": "not_dispatched"}):
            with self.assertRaises(ValueError):
                ledger.release("a", evidence=evidence)
        evidence = {"kind": "provider_no_spend", "reference": "synthetic-provider-receipt", "observed_at": NOW.isoformat()}
        self.assertTrue(ledger.release("a", evidence=evidence))
        self.assertTrue(ledger.release("a", evidence=evidence))
        self.assertFalse(ledger.settle("a", "0"))
        self.assertFalse(ledger.reserve("a", ".6", "1", "2026-09"))
        ledger.reserve("b", ".6", "1", "2026-09")
        self.assertTrue(ledger.release("b"))
        self.assertTrue(ledger.release("b"))

    def test_overspend_records_actual_and_blocks_new_allowance(self):
        ledger = SpendLedger(self.root)
        ledger.reserve("a", ".2", "2", "2026-09")
        ledger.settle("a", ".3")
        self.assertFalse(ledger.reserve("b", ".1", "2", "2026-09"))
        self.assertTrue(json.loads(ledger.path.read_text())["reservations"]["a"]["overspend"])
        self.assertTrue(ledger.reserve("next-month", ".1", "2", "2026-10"))

    def test_per_run_cap_and_conflicting_run_binding(self):
        ledger = SpendLedger(self.root)
        ledger.reserve("a", ".5", "5", "2026-09", run_id="r", run_cap_gbp=".6")
        self.assertFalse(ledger.reserve("b", ".2", "5", "2026-09", run_id="r", run_cap_gbp=".6"))
        with self.assertRaises(ValueError):
            ledger.reserve("a", ".5", "5", "2026-09", run_id="other")

    def test_invalid_ledger_records_and_months_fail_closed_without_rewrite(self):
        ledger = SpendLedger(self.root)
        for month in ("current", "2026-13", "2026-1", True):
            with self.assertRaises(ValueError):
                ledger.reserve("a", ".5", "1", month)
        for data in ({}, {"reservations": {}}, {"schema": "ringer-spend/v2", "reservations": {"a": {"amount_gbp": "NaN"}}}):
            ledger.path.write_text(json.dumps(data))
            before = ledger.path.read_bytes()
            with self.assertRaises(ValueError):
                ledger.reserve("a", ".5", "1", "2026-09")
            self.assertEqual(before, ledger.path.read_bytes())
            self.assertFalse(self.preflight(engine="opencode", state_dir=self.root, attempt_id="b", task_spend_allowance_gbp=".1").allow)

    def test_durable_commit_fsync_file_and_directory_and_failed_replace(self):
        ledger = SpendLedger(self.root)
        with patch("ringer_costs.os.fsync", wraps=os.fsync) as sync:
            ledger.reserve("a", ".5", "1", "2026-09")
            self.assertGreaterEqual(sync.call_count, 2)
        before = ledger.path.read_bytes()
        with patch("ringer_costs.os.replace", side_effect=OSError("simulated interruption")):
            with self.assertRaises(OSError):
                ledger.reserve("b", ".2", "1", "2026-09")
        self.assertEqual(before, ledger.path.read_bytes())
        self.assertEqual([], list(self.root.glob(".cost-*")))

    def test_original_total_only_journal_and_state_only_cancellation(self):
        rows = [{"run_id": "job", "task_key": "one", "worker_engine": "opencode",
                 "model": "openrouter/z-ai/glm-5.2", "verdict": "PASS", "retry": False, "worker_tokens": 10000}]
        state = {"job": {"tasks": [{"key": "one", "status": "pass", "attempts": 1},
                                      {"key": "cancelled", "status": "cancelled", "attempts": 0}]}}
        report = self.report(rows, state)
        self.assertEqual(1, report["api"]["unknown_events"])
        self.assertEqual(1, report["jobs"]["state_only_tasks"])
        self.assertEqual(2, report["jobs"]["total_tasks"])
        self.assertEqual(.5, report["jobs"]["final_check_pass_rate"])
        self.assertEqual(0, report["jobs"]["human_accepted_jobs"])
        self.assertEqual("UNKNOWN", report["jobs"]["cost_per_accepted"]["worker_and_fixed_gbp"])

    def test_raw_state_logs_include_failed_retry_and_interrupted_attempt(self):
        worker = self.root / "worker.log"
        lines = []
        for index, cost in ((1, ".01"), (2, ".02"), (3, ".03")):
            event = opencode(cost=cost)
            lines += [f"[ringer.py] attempt {index} started now", json.dumps(event), json.dumps(event)]
        worker.write_text("\n".join(lines))
        rows = [{"run_id": "r", "task_key": "a", "worker_engine": "opencode", "model": "openrouter/z-ai/glm-5.2",
                 "attempt_index": index, "verdict": verdict, "worker_tokens": 10000, "log_path": str(worker)}
                for index, verdict in ((1, "FAIL"), (2, "PASS"))]
        states = {"r": {"tasks": [{"key": "a", "engine": "opencode", "status": "interrupted", "attempts": 3, "log_path": str(worker)}]}}
        report = self.report(rows, states)
        self.assertEqual("0", report["api"]["actual_usd"])
        self.assertEqual("0.06", report["api"]["harness_estimated_usd"])
        self.assertEqual(3, report["api"]["unknown_events"])
        self.assertEqual(3, report["actual_cost_coverage"]["total_events"])
        self.assertEqual(3, report["jobs"]["all_attempts"])
        self.assertEqual(0, report["jobs"]["first_check_passes"])
        self.assertEqual(0, report["jobs"]["final_check_passes"])
        self.assertEqual(3, report["groups"]["api|openrouter/z-ai/glm-5.2"]["attempts"])

    def test_missing_raw_attempt_is_unknown_and_retry_pass_is_not_first_pass(self):
        rows = [{"run_id": "r", "task_key": "a", "worker_engine": "opencode", "verdict": verdict,
                 "retry": index == 2, "worker_tokens": 10} for index, verdict in ((1, "FAIL"), (2, "PASS"))]
        report = self.report(rows)
        self.assertEqual(2, report["api"]["unknown_events"])
        self.assertEqual(0, report["jobs"]["first_check_passes"])
        self.assertEqual(1, report["jobs"]["final_check_passes"])

    def test_codex_engine_and_old_model_are_not_auth_or_identity_evidence(self):
        row = {"run_id": "r", "task_key": "a", "worker_engine": "codex", "model": "gpt-5.6-sol",
               "worker_tokens": 10000, "verdict": "PASS"}
        report = self.report([row])
        self.assertEqual("unknown", report["events"][0]["model"])
        self.assertEqual("unknown", report["events"][0]["route"])
        self.assertEqual("UNKNOWN", report["subscription"]["marginal_cash_gbp"])

    def test_subscription_inclusion_is_attested_per_attempt_and_optional_snapshot_cannot_invent_it(self):
        row = {"run_id": "r", "task_key": "a", "worker_engine": "codex", "expected_model": "gpt-5.6-sol",
               "observed_auth_mode": "chatgpt", "worker_tokens": 1000, "included_usage_established": True}
        report = self.report([row], usage_snapshot=snapshot())
        self.assertEqual("0", report["subscription"]["marginal_cash_gbp"])
        report = self.report([row], usage_snapshot={})
        self.assertEqual("UNKNOWN", report["subscription"]["marginal_cash_gbp"])
        report = self.report([{**row, "included_usage_established": False}], usage_snapshot=snapshot())
        self.assertEqual("UNKNOWN", report["subscription"]["marginal_cash_gbp"])

    def test_credits_actual_is_separate_and_hypothetical_not_added(self):
        row = {"run_id": "r", "task_key": "a", "worker_engine": "codex", "billing_category": "credits",
               "reported_model": "gpt-5.6-sol", "service_tier": "standard", "provider_cost": "2",
               "usage": {"input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 10}}
        report = self.report([row])
        self.assertEqual("2", report["credits"]["actual_usd"])
        self.assertEqual("0", report["api"]["actual_usd"])
        self.assertEqual(1, report["credits"]["unknown_estimate_events"])
        self.assertGreater(Decimal(report["api_equivalent"]["upper_usd"]), 0)

    def test_scenario_math_fee_allocated_once_with_dated_fx_and_retry_inclusive_job_cost(self):
        policy = {"monthly_subscription_gbp": 90, "fx": {"usd_to_gbp": ".75", "dated": "2026-09-02"}}
        report = self.report([], policy=policy, monthly_jobs=100, included_jobs=80,
                             accepted_rate=Decimal(".8"), api_cost_per_job_usd=2)
        scenario = report["scenario"]
        self.assertEqual("120.00", scenario["subscription"]["total_gbp"])
        self.assertEqual("150.00", scenario["all_api"]["total_gbp"])
        self.assertEqual("1.5", scenario["subscription"]["per_accepted_gbp"])
        self.assertEqual("1.875", scenario["all_api"]["per_accepted_gbp"])
        self.assertEqual("1.125", report["subscription"]["fixed_allocation_per_accepted_gbp"])
        self.assertEqual("60", scenario["break_even"]["included_jobs_required"])

    def test_scenario_no_fx_zero_acceptance_and_invalid_inputs(self):
        policy = validate_cost_policy({"monthly_subscription_gbp": 90})
        scenario = REPORT.scenario_comparison(policy, 100, 80, ".8", 2)
        self.assertEqual("40", scenario["subscription"]["overflow_usd"])
        self.assertEqual("UNKNOWN", scenario["subscription"]["total_gbp"])
        self.assertEqual("UNKNOWN", REPORT.scenario_comparison(policy, 100, 80, 0, 2)["fixed_per_accepted_gbp"])
        for monthly, included, rate, cost in ((True, 0, 1, 2), (10, 11, 1, 2), (10, 0, "NaN", 2), (10, 0, 1, -1)):
            with self.assertRaises(ValueError):
                REPORT.scenario_comparison(policy, monthly, included, rate, cost)

    def test_actual_cash_per_human_accepted_once_and_review_cost_unknown(self):
        digest = "a" * 64
        row = {"run_id": "r", "task_key": "a", "worker_engine": "opencode", "provider_cost": 4,
               "verdict": "PASS", "logged_at": NOW.isoformat(), "artifact_sha256": digest,
               "human_review": {"status": "accepted", "artifact_sha256": digest, "reviewer": "fixture-human", "reviewer_type": "human", "reference": "fixture-review"}}
        # Actual provider cost without token details still needs an event.
        row["worker_tokens"] = 10
        policy = {"monthly_subscription_gbp": 90, "fx": {"usd_to_gbp": ".75", "dated": "2026-09-02"}}
        report = self.report([row], policy=policy)
        self.assertEqual("93.00", report["jobs"]["cost_per_accepted"]["worker_and_fixed_gbp"])
        self.assertEqual("UNKNOWN", report["jobs"]["cost_per_accepted"]["whole_job_gbp"])
        report = self.report([row], policy=policy, human_review_cost_gbp=2, coordinator_cost_gbp=3)
        self.assertEqual("98.00", report["jobs"]["cost_per_accepted"]["whole_job_gbp"])
        row["human_review"]["artifact_sha256"] = "b" * 64
        self.assertEqual(0, self.report([row], policy=policy)["jobs"]["human_accepted_jobs"])

    def test_dispatch_claim_is_atomic_and_cannot_repeat_started_attempt(self):
        ledger = SpendLedger(self.root)
        ledger.reserve("a", ".5", "1", "2026-09")
        self.assertTrue(ledger.mark_started("a"))
        self.assertFalse(ledger.mark_started("a"))
        self.assertFalse(ledger.reserve("a", ".5", "1", "2026-09"))
        self.assertFalse(self.preflight(engine="opencode", attempt_id="a", state_dir=self.root,
                                        task_spend_allowance_gbp=".5", month="2026-09").allow)

    def test_overspend_blocks_previously_reserved_but_unstarted_dispatch(self):
        ledger = SpendLedger(self.root)
        ledger.reserve("a", ".2", "2", "2026-09")
        ledger.reserve("b", ".2", "2", "2026-09")
        ledger.settle("a", ".3")
        self.assertFalse(ledger.mark_started("b"))
        self.assertFalse(ledger.reserve("b", ".2", "2", "2026-09"))

    def test_duplicate_json_ledger_keys_fail_closed(self):
        ledger = SpendLedger(self.root)
        ledger.path.write_text('{"schema":"ringer-spend/v2","reservations":{},"reservations":{}}')
        with self.assertRaises(ValueError):
            ledger.reserve("a", ".5", "1", "2026-09")

    def test_malformed_preflight_arguments_block_instead_of_raising(self):
        for change in ({"engine": []}, {"model": True}, {"billing_route": {}},
                       {"auto_fallback_requested": 1}, {"policy": []}):
            with self.subTest(change=change):
                self.assertFalse(self.preflight(**change).allow)
        quota = snapshot()
        quota["exhausted"] = "false"
        self.assertFalse(self.preflight(usage_snapshot=quota).allow)
        quota = snapshot()
        quota["rateLimitsByLimitId"]["codex"]["credits"]["hasCredits"] = 0
        self.assertFalse(self.preflight(usage_snapshot=quota).allow)

    def test_provider_cost_is_actual_but_opencode_part_cost_is_harness_estimate(self):
        report = self.report([opencode(cost=".5")])
        self.assertEqual("0", report["api"]["actual_usd"])
        self.assertEqual("0.5", report["api"]["harness_estimated_usd"])
        self.assertEqual(1, report["api"]["unknown_events"])
        event = self.parse([{"provider_cost": "0.7"}])[0]
        self.assertEqual("actual", estimate_event(event).status)
        self.assertEqual(Decimal(".7"), estimate_event(event).amount)

    def test_harness_estimate_never_settles_a_paid_reservation(self):
        raw = opencode(cost="0")
        event = self.parse([raw])[0]
        self.assertEqual("estimate", estimate_event(event).status)
        self.assertEqual(Decimal("0"), estimate_event(event).amount)
        usage = usage_evidence(json.dumps(raw), "openrouter/z-ai/glm-5.2", complete=True)
        self.assertIsNone(usage["actual_cost_usd"])
        self.assertEqual("unknown", usage["cost_coverage"])
        ledger = SpendLedger(self.root)
        self.assertTrue(ledger.reserve("part-cost", ".5", "1", "2026-09"))
        self.assertTrue(ledger.mark_started("part-cost"))
        self.assertFalse(ledger.settle("part-cost", usage["actual_cost_usd"]))
        self.assertEqual("reserved", json.loads(ledger.path.read_text())["reservations"]["part-cost"]["state"])

    def test_state_cancellation_overrides_earlier_journal_pass(self):
        rows = [{"run_id": "r", "task_key": "a", "verdict": "PASS", "worker_tokens": 10}]
        states = {"r": {"tasks": [{"key": "a", "status": "cancelled", "attempts": 1}]}}
        report = self.report(rows, states)
        self.assertEqual(0, report["jobs"]["final_check_passes"])
        self.assertEqual(1, report["jobs"]["first_check_passes"])

    def test_partial_actual_coverage_never_becomes_cash_per_accepted(self):
        rows = [{"run_id": "r", "task_key": "a", "worker_engine": "opencode", "provider_cost": 2},
                {"run_id": "r", "task_key": "b", "worker_engine": "opencode", "worker_tokens": 10}]
        report = self.report(rows)
        self.assertEqual("2", report["api"]["actual_usd"])
        self.assertEqual(1, report["api"]["unknown_events"])
        self.assertFalse(report["actual_cost_coverage"]["complete"])
        self.assertEqual("UNKNOWN", report["jobs"]["cost_per_accepted"]["actual_incremental_usd"])

    def test_missing_cache_breakdown_is_unknown_not_assumed_uncached(self):
        event = self.parse([{"type": "turn.completed", "model": "gpt-5.6-sol", "service_tier": "standard",
                             "usage": {"input_tokens": 100, "output_tokens": 10}}])[0]
        self.assertIsNone(event.cached_input_tokens)
        self.assertEqual("unknown", estimate_event(event).status)

    def test_agent_review_is_not_human_acceptance_even_with_matching_hash(self):
        digest = "a" * 64
        for reviewer_type in (None, "agent", "worker"):
            review = {"artifact_sha256": digest, "human_review": {
                "artifact_sha256": digest, "reviewer": "worker", "reviewer_type": reviewer_type,
                "status": "accepted", "reference": "synthetic worker notes"}}
            self.assertEqual("unknown", REPORT._human_review(review))
        review["human_review"]["reviewer_type"] = "human"
        self.assertEqual("unknown", REPORT._human_review(review))

    def test_codex_cache_write_input_tokens_preserved_and_charged_once(self):
        row = {"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 20,
               "cache_write_input_tokens": 30, "output_tokens": 10}}
        event = parse_usage_lines([json.dumps(row)], expected_model="gpt-5.6-sol", service_tier="standard")[0]
        self.assertEqual(30, event.cache_write_tokens)
        cost = estimate_event(event)
        self.assertEqual(Decimal(".000558"), cost.lower)
        self.assertEqual(Decimal(".001016"), cost.upper)
        self.assertIsNone(cost.amount)

    def test_cli_json_markdown_no_writes_and_nonfinite_arguments_fail(self):
        log = self.root / "runs.jsonl"
        log.write_text(json.dumps({"run_id": "r", "task_key": "a", "verdict": "PASS"}) + "\n")
        before = log.read_bytes()
        command = [sys.executable, str(ROOT / "scripts/ringer_cost_report.py"), "--state-dir", str(self.root),
                   "--policy", str(ROOT / "config/cost-policy.example.json"), "--monthly-jobs", "100",
                   "--included-jobs", "80", "--accepted-rate", ".8", "--api-cost-per-job-usd", "2"]
        result = subprocess.run(command + ["--json"], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("ringer-cost-report/v2", json.loads(result.stdout)["schema"])
        markdown = subprocess.check_output(command, text=True)
        self.assertIn("All API:", markdown)
        self.assertIn("First/final check passes", markdown)
        self.assertEqual(before, log.read_bytes())
        self.assertEqual(["runs.jsonl"], sorted(path.name for path in self.root.iterdir()))
        invalid = subprocess.run(command + ["--accepted-rate", "NaN"], capture_output=True, text=True)
        self.assertNotEqual(0, invalid.returncode)


if __name__ == "__main__":
    unittest.main()
