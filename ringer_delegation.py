"""Planned GLM/DeepSeek delegation. No network, no credential reads.

A budget cap is not this approval. Quota and provider errors do not reach
this selector. The recorded approval is the 2026-11-08 chat text.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_UP
from pathlib import Path
from typing import Any

MATRIX_PATH = Path(__file__).resolve().parent / "registry" / "delegation-matrix.json"
CATALOGUE_PATH = Path(__file__).resolve().parent / "registry" / "openrouter-catalogue-2026-11-08.json"


@dataclass(frozen=True)
class DelegationDecision:
    allowed: bool
    reason: str
    failure_class: str
    complexity: str
    task_class: str
    model: str
    engine: str
    auth_route: str
    estimated_cost_gbp: str | None
    approval_id: str | None
    reserve_gbp: str | None = None
    policy: dict[str, Any] | None = None

    def evidence(self) -> dict[str, Any]:
        return {
            "complexity": self.complexity,
            "task_class": self.task_class,
            "model": self.model,
            "auth_route": self.auth_route,
            "reason": self.reason,
            "estimated_cost_gbp": self.estimated_cost_gbp,
            "approval_id": self.approval_id,
        }


def load_matrix(path: Path | None = None) -> dict[str, Any]:
    return json.loads((path or MATRIX_PATH).read_text(encoding="utf-8"))


def load_catalogue(path: Path | None = None) -> dict[str, Any]:
    return json.loads((path or CATALOGUE_PATH).read_text(encoding="utf-8"))


def canonical_shortlist_model(model: str, matrix: dict[str, Any] | None = None) -> str | None:
    """Map an alias onto a shortlist canonical id. Variants and other slugs do not match."""
    text = (model or "").strip()
    if not text or ":" in text:
        return None
    table = matrix if matrix is not None else load_matrix()
    slug = text.removeprefix("openrouter/")
    for entry in table["shortlist"]:
        canonical = entry["canonical"]
        names = {canonical, canonical.removeprefix("openrouter/"), *entry.get("aliases", [])}
        if text in names or slug in names:
            return canonical
    return None


def _decimal(value: Any) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError("amount is missing")
    amount = Decimal(str(value))
    if not amount.is_finite():
        raise ValueError("amount is not finite")
    return amount


def _fx_is_current(policy: dict[str, Any], *, now: datetime, max_age_days: int) -> tuple[bool, Decimal | None, str]:
    fx = policy.get("fx")
    if not isinstance(fx, dict) or "usd_to_gbp" not in fx or "dated" not in fx:
        return False, None, "paid admission requires a dated USD to GBP rate"
    try:
        dated = datetime.strptime(str(fx["dated"]), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        rate = _decimal(fx["usd_to_gbp"])
    except (ValueError, ArithmeticError):
        return False, None, "paid admission requires a dated USD to GBP rate"
    if rate <= 0:
        return False, None, "paid admission requires a positive USD to GBP rate"
    age = (now.astimezone(timezone.utc) - dated).days
    if age < 0 or age > max_age_days:
        return False, None, f"FX dated {fx['dated']} is outside the {max_age_days}-day window"
    return True, rate, ""


def estimate_gbp(
    canonical: str,
    *,
    input_tokens: int,
    output_tokens: int,
    fx_rate: Decimal,
    catalogue: dict[str, Any],
) -> Decimal | None:
    slug = canonical.removeprefix("openrouter/")
    row = catalogue.get("models", {}).get(slug)
    if not isinstance(row, dict):
        return None
    if not row.get("on_initial_shortlist"):
        return None
    try:
        usd = (Decimal(input_tokens) * Decimal(row["prompt"])) + (
            Decimal(output_tokens) * Decimal(row["completion"])
        )
    except (KeyError, ArithmeticError, ValueError):
        return None
    if usd < 0:
        return None
    return (usd * fx_rate).quantize(Decimal("0.0001"), rounding=ROUND_UP)


def prepare_openrouter_dispatch(
    *,
    model: str,
    allowance: Any,
    attempt: int,
    policy: dict[str, Any],
    now: datetime | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    matrix: dict[str, Any] | None = None,
    catalogue: dict[str, Any] | None = None,
    task_class: str = "bounded-detail",
) -> DelegationDecision:
    """Decide whether one planned OpenRouter attempt may reserve spend. Does not reserve."""
    table = matrix if matrix is not None else load_matrix()
    prices = catalogue if catalogue is not None else load_catalogue()
    approval = table["approval"]
    approval_id = approval["id"]
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    complexity = "bounded" if task_class == "bounded-detail" else "open"
    if task_class != "bounded-detail":
        return DelegationDecision(
            False,
            "this class stays on the native subscription route",
            "permission",
            complexity,
            task_class,
            "",
            "",
            "subscription",
            None,
            approval_id,
        )
    if attempt != 1:
        return DelegationDecision(
            False,
            "no automatic paid retries",
            "permission",
            complexity,
            task_class,
            model,
            "opencode",
            "api",
            None,
            approval_id,
        )
    canonical = canonical_shortlist_model(model, table)
    if canonical is None:
        return DelegationDecision(
            False,
            "model is not on the approved GLM/DeepSeek shortlist",
            "permission",
            complexity,
            task_class,
            model,
            "opencode",
            "api",
            None,
            approval_id,
        )
    fresh, rate, fx_reason = _fx_is_current(
        policy, now=current, max_age_days=int(table["fx_max_age_days"])
    )
    if not fresh or rate is None:
        return DelegationDecision(
            False, fx_reason, "permission", complexity, task_class, canonical, "opencode", "api", None, approval_id,
        )
    budget = table["conservative_budget"]
    in_tokens = budget["input_tokens"] if input_tokens is None else input_tokens
    out_tokens = budget["output_tokens"] if output_tokens is None else output_tokens
    if in_tokens < 0 or out_tokens < 0:
        return DelegationDecision(
            False, "token budget must be zero or positive", "permission",
            complexity, task_class, canonical, "opencode", "api", None, approval_id,
        )
    estimate = estimate_gbp(
        canonical, input_tokens=in_tokens, output_tokens=out_tokens, fx_rate=rate, catalogue=prices,
    )
    if estimate is None or estimate <= 0:
        return DelegationDecision(
            False, "paid admission requires a current catalogue price for this model", "permission",
            complexity, task_class, canonical, "opencode", "api", None, approval_id,
        )
    task_cap = _decimal(approval["task_gbp"])
    try:
        named = _decimal(allowance)
    except (ValueError, ArithmeticError):
        return DelegationDecision(
            False, "paid API requires explicit task spend allowance", "permission",
            complexity, task_class, canonical, "opencode", "api", str(estimate), approval_id,
        )
    if named <= 0 or named > task_cap or estimate > task_cap or estimate > named:
        return DelegationDecision(
            False,
            f"task spend must be within £{task_cap} and cover the conservative estimate £{estimate}",
            "permission",
            complexity,
            task_class,
            canonical,
            "opencode",
            "api",
            str(estimate),
            approval_id,
        )
    monthly_cap = min(_decimal(policy["monthly_api_cap_gbp"]), _decimal(approval["monthly_gbp"]))
    run_value = policy.get("run_api_cap_gbp")
    run_cap = _decimal(approval["run_gbp"]) if run_value is None else min(_decimal(run_value), _decimal(approval["run_gbp"]))
    narrowed = dict(policy)
    narrowed["monthly_api_cap_gbp"] = str(monthly_cap)
    narrowed["run_api_cap_gbp"] = str(run_cap)
    return DelegationDecision(
        True,
        "planned GLM/DeepSeek delegation within the 2026-11-08 approval",
        "permission",
        complexity,
        task_class,
        canonical,
        "opencode",
        "api",
        str(estimate),
        approval_id,
        reserve_gbp=str(estimate),
        policy=narrowed,
    )


def select_bounded_worker(
    *,
    policy: dict[str, Any],
    now: datetime | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> DelegationDecision:
    """Pick the cheapest shortlist model whose estimate fits the task cap."""
    table = load_matrix()
    best: DelegationDecision | None = None
    best_amount: Decimal | None = None
    for entry in table["shortlist"]:
        decision = prepare_openrouter_dispatch(
            model=entry["canonical"],
            allowance=table["approval"]["task_gbp"],
            attempt=1,
            policy=policy,
            now=now,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            task_class="bounded-detail",
        )
        if not decision.allowed or decision.estimated_cost_gbp is None:
            continue
        amount = Decimal(decision.estimated_cost_gbp)
        if best_amount is None or amount < best_amount:
            best = decision
            best_amount = amount
    if best is None:
        return prepare_openrouter_dispatch(
            model=table["shortlist"][0]["canonical"],
            allowance=table["approval"]["task_gbp"],
            attempt=1,
            policy=policy,
            now=now,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
    return best
