"""Planned GLM/DeepSeek delegation. No network and no paid calls."""
from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import json
from ringer_costs import billing_preflight
from ringer_delegation import (
    canonical_shortlist_model,
    load_catalogue,
    load_matrix,
    prepare_openrouter_dispatch,
    select_bounded_worker,
)

NOW = datetime(2026, 11, 8, 21, 11, tzinfo=timezone.utc)


def policy(**extra):
    base = {
        "monthly_api_cap_gbp": "10",
        "run_api_cap_gbp": "1",
        "automatic_paid_fallback": True,
        "subscription_auth_modes": ["chatgpt"],
        "fx": {"usd_to_gbp": "0.8", "dated": "2026-11-08"},
    }
    base.update(extra)
    return base


class DelegationMatrixTests(unittest.TestCase):
    def test_approval_is_the_chat_record_and_not_a_quality_rank(self) -> None:
        matrix = load_matrix()
        self.assertEqual("user-chat-2026-11-08T21:11:00Z", matrix["approval"]["id"])
        self.assertEqual("2026-11-08T21:11:00Z", matrix["approval"]["recorded_at"])
        self.assertIn("£10 per month", matrix["approval"]["text"])
        self.assertFalse(matrix["approval"]["paid_retries"])
        catalogue = load_catalogue()
        flash = catalogue["models"]["z-ai/glm-5.3-flash"]
        self.assertEqual("0.0000000335", flash["prompt"])
        self.assertEqual("0.000000083", flash["completion"])
        self.assertEqual(1048576, flash["context_length"])
        self.assertTrue(flash["supports_tools"])
        self.assertTrue(flash["supports_reasoning"])
        self.assertFalse(catalogue["models"]["z-ai/glm-5.3"]["on_initial_shortlist"])
        self.assertFalse(catalogue["models"]["deepseek/deepseek-v4-pro"]["on_initial_shortlist"])

    def test_alias_matches_canonical_id_and_unknown_or_variant_does_not(self) -> None:
        self.assertEqual(
            "openrouter/z-ai/glm-5.3-flash",
            canonical_shortlist_model("z-ai/glm-5.3-flash"),
        )
        self.assertEqual(
            "openrouter/deepseek/deepseek-v4.1-flash",
            canonical_shortlist_model("openrouter/deepseek/deepseek-v4.1-flash"),
        )
        for rejected in (
            "openrouter/z-ai/glm-5.3",
            "z-ai/glm-5.3",
            "openrouter/z-ai/glm-5.3-flash:nitro",
            "openrouter/deepseek/deepseek-v4-pro",
            "openrouter/openai/gpt-5.6-terra",
            "",
        ):
            with self.subTest(model=rejected):
                self.assertIsNone(canonical_shortlist_model(rejected))

    def test_stale_fx_unknown_price_and_paid_retry_do_not_reserve(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stale = prepare_openrouter_dispatch(
                model="z-ai/glm-5.3-flash", allowance="0.25", attempt=1,
                policy=policy(fx={"usd_to_gbp": "0.8", "dated": "2026-09-02"}), now=NOW,
            )
            self.assertFalse(stale.allowed)
            self.assertIn("FX dated 2026-09-02", stale.reason)
            missing = prepare_openrouter_dispatch(
                model="openrouter/z-ai/glm-5.3", allowance="0.25", attempt=1,
                policy=policy(), now=NOW,
            )
            self.assertFalse(missing.allowed)
            self.assertIn("shortlist", missing.reason)
            retry = prepare_openrouter_dispatch(
                model="openrouter/z-ai/glm-5.3-flash", allowance="0.25", attempt=2,
                policy=policy(), now=NOW,
            )
            self.assertFalse(retry.allowed)
            self.assertEqual("no automatic paid retries", retry.reason)
            self.assertFalse((root / "cost-reservations.json").exists())

    def test_selector_picks_the_cheaper_suitable_shortlist_model_without_a_rank(self) -> None:
        decision = select_bounded_worker(policy=policy(), now=NOW)
        self.assertTrue(decision.allowed, decision.reason)
        self.assertEqual("openrouter/z-ai/glm-5.3-flash", decision.model)
        self.assertEqual("api", decision.auth_route)
        self.assertEqual("bounded", decision.complexity)
        self.assertEqual("bounded-detail", decision.task_class)
        self.assertEqual("user-chat-2026-11-08T21:11:00Z", decision.approval_id)
        self.assertIsNotNone(decision.estimated_cost_gbp)
        coordination = prepare_openrouter_dispatch(
            model="openrouter/z-ai/glm-5.3-flash", allowance="0.25", attempt=1,
            policy=policy(), now=NOW, task_class="coordination",
        )
        self.assertFalse(coordination.allowed)
        self.assertEqual("subscription", coordination.auth_route)

    def test_concurrent_reservations_cannot_pass_the_run_or_month_ceiling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            barrier = threading.Barrier(8)
            results: list[bool] = []
            lock = threading.Lock()

            def attempt(index: int) -> None:
                barrier.wait()
                allowed = billing_preflight(
                    engine="opencode",
                    model="openrouter/z-ai/glm-5.3-flash",
                    policy=policy(monthly_api_cap_gbp="10", run_api_cap_gbp="1"),
                    observed_auth_mode=None,
                    usage_snapshot={},
                    task_spend_allowance_gbp="0.20",
                    attempt_id=f"swarm/{index}",
                    state_dir=root,
                    billing_route="api",
                    run_id="swarm",
                    now=NOW,
                ).allow
                with lock:
                    results.append(allowed)

            threads = [threading.Thread(target=attempt, args=(index,)) for index in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(5, sum(results))
            rows = json.loads((root / "cost-reservations.json").read_text(encoding="utf-8"))["reservations"]
            reserved = [Decimal(row["amount_gbp"]) for row in rows.values()]
        self.assertEqual(5, len(reserved))
        self.assertLessEqual(sum(reserved, Decimal(0)), Decimal("1"))

    def test_month_ceiling_is_ten_even_when_the_file_names_a_higher_cap(self) -> None:
        decision = prepare_openrouter_dispatch(
            model="openrouter/z-ai/glm-5.3-flash", allowance="0.25", attempt=1,
            policy=policy(monthly_api_cap_gbp="100", run_api_cap_gbp="50"), now=NOW,
        )
        self.assertTrue(decision.allowed, decision.reason)
        assert decision.policy is not None
        self.assertEqual("10", decision.policy["monthly_api_cap_gbp"])
        self.assertEqual("1", decision.policy["run_api_cap_gbp"])


if __name__ == "__main__":
    unittest.main()
