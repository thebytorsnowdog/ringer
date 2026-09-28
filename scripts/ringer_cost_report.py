#!/usr/bin/env python3
"""Read-only cost report from journals, referenced raw logs and sanitised state."""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ringer_costs import (UsageEvent, _count, _decimal, _text, estimate_event,
                         inspect_usage_snapshot, parse_usage_lines, validate_cost_policy)


def _money(value: Decimal | None) -> str:
    return format(value, "f") if value is not None else "UNKNOWN"


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _read_rows(path: Path, warnings: list[str]) -> list[dict[str, Any]]:
    if not path.exists():
        warnings.append(f"journal unavailable: {path}")
        return []
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("row must be object")
            rows.append(row)
        except ValueError:
            warnings.append(f"malformed journal row {number}; coverage incomplete")
    return rows


def _route(row: dict[str, Any]) -> str:
    engine = row.get("worker_engine", row.get("engine"))
    model = row.get("model")
    if engine in {"opencode", "openrouter"} or (isinstance(model, str) and model.startswith("openrouter/")):
        return "api"
    declared = row.get("billing_category", row.get("billing_route"))
    if declared in {"api", "credits"}:
        return declared
    auth = row.get("observed_auth_mode")
    if engine in {"codex", "chatgpt"}:
        if auth == "api_key":
            return "api"
        if auth == "chatgpt":
            return "subscription"
        return "unknown"
    return "unknown"


def _model(row: dict[str, Any]) -> str | None:
    # Older Codex journal 'model' could be a guessed default. Trust only explicit
    # reported_model / caller-attested expected_model, or raw header identity.
    explicit = _text(row.get("reported_model")) or _text(row.get("expected_model"))
    if explicit:
        return explicit
    if row.get("worker_engine", row.get("engine")) not in {"codex", "chatgpt"}:
        return _text(row.get("model"))
    return None


def _index(row: dict[str, Any], fallback: int) -> int:
    value = row.get("attempt_index", row.get("attempt"))
    return value if _count(value) is not None and value > 0 else fallback


def _pass(row: dict[str, Any]) -> bool:
    status = row.get("status")
    if status is not None and (not isinstance(status, str) or status.lower() != "pass"):
        return False
    verdict = row.get("verdict") or status or ""
    return isinstance(verdict, str) and verdict.upper() == "PASS"


def _human_review(row: dict[str, Any]) -> str:
    review = row.get("human_review")
    if not isinstance(review, dict) or review.get("reviewer_type") != "human":
        return "unknown"
    reviewer = review.get("reviewer")
    if not _text(reviewer) or reviewer.lower() in {"worker", "agent", "assistant", "codex", "opencode"}:
        return "unknown"
    if reviewer in (row.get("worker_id"), row.get("agent_id")):
        return "unknown"
    digest = row.get("artifact_sha256")
    if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or review.get("artifact_sha256") != digest or not _text(review.get("reviewer"))
            or not _text(review.get("reference"))):
        return "unknown"
    return review.get("status") if review.get("status") in {"accepted", "rejected"} else "unknown"


def _placeholder(model: str | None, issue: str = "attempt usage unavailable") -> UsageEvent:
    return UsageEvent(model, None, None, None, None, source="missing-attempt-usage", aggregate=True, issue=issue)


def _collect(log: Path, state_dir: Path, warnings: list[str]) -> tuple[list[dict], int, int]:
    journal = _read_rows(log, warnings)
    jobs: dict[tuple[str, str], dict] = {}
    raw_rows = []
    journal_count = 0
    for row in journal:
        if "task_key" not in row:
            raw_rows.append(row)
            continue
        key = (str(row.get("run_id", "unknown")), str(row["task_key"]))
        job = jobs.setdefault(key, {"key": key, "state": {}, "attempts": {}, "journal": True})
        index = _index(row, len(job["attempts"]) + 1)
        # Explicit attempt identity is preferred; exact repeat of a journal row
        # is a transport duplicate. Counts alone are never a deduplication key.
        if any(attempt["row"] == row for attempt in job["attempts"].values()):
            continue
        if index in job["attempts"]:
            raise ValueError(f"conflicting journal attempt {key}/{index}")
        job["attempts"][index] = {"row": row, "events": [], "raw_seen": False}
        journal_count += 1
    if raw_rows:
        events = parse_usage_lines([json.dumps(row) for row in raw_rows], namespace=str(log))
        if events:
            jobs[("raw-log", log.name)] = {"key": ("raw-log", log.name), "state": {}, "journal": True,
                                           "attempts": {1: {"row": {}, "events": events, "raw_seen": True}}}
    state_only = 0
    for path in sorted((state_dir / "runs").glob("*.json")):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(state, dict) or not isinstance(state.get("tasks"), list):
                raise ValueError("invalid state shape")
            for task in state["tasks"]:
                if not isinstance(task, dict) or not _text(task.get("key", task.get("task_key"))):
                    raise ValueError("invalid task state")
                key = (str(state.get("run_id", path.stem)), task.get("key", task.get("task_key")))
                if key not in jobs:
                    state_only += 1
                job = jobs.setdefault(key, {"key": key, "state": {}, "attempts": {}, "journal": False})
                job["state"] = task
                job["state_path"] = path
                count = task.get("attempts", task.get("attempt_index", 0))
                if _count(count) is None:
                    raise ValueError("state attempts must be a non-negative integer")
                for index in range(1, count + 1):
                    # Final state is evidence for final attempt only, never the
                    # first attempt's verdict or historical authentication.
                    row = dict(task) if index == count else {"engine": task.get("engine")}
                    job["attempts"].setdefault(index, {"row": row, "events": [], "raw_seen": False})
        except (OSError, ValueError) as exc:
            warnings.append(f"state coverage incomplete {path}: {exc}")
    for job in jobs.values():
        state = job["state"]
        references = []
        if _text(state.get("log_path")):
            references.append((state["log_path"], None, job.get("state_path", log).parent))
        for index, attempt in list(job["attempts"].items()):
            if _text(attempt["row"].get("log_path")):
                references.append((attempt["row"]["log_path"], index, log.parent))
        seen_paths = set()
        for name, owner, base in references:
            path = Path(name).expanduser()
            path = (path if path.is_absolute() else base / path).resolve()
            if path in seen_paths:
                continue
            seen_paths.add(path)
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError as exc:
                warnings.append(f"raw log unavailable {path}: {exc}")
                continue
            sections: dict[int, list[str]] = defaultdict(list)
            current = owner or (next(iter(job["attempts"])) if len(job["attempts"]) == 1 else 0)
            for line in lines:
                marker = re.match(r"\[ringer\.py\] attempt (\d+) started\b", line)
                if marker:
                    current = int(marker[1])
                    sections.setdefault(current, [])
                sections[current].append(line)
            for index, body in sections.items():
                if index == 0 and not any(line.lstrip().startswith("{") or line.strip() == "tokens used" for line in body):
                    continue
                attempt = job["attempts"].setdefault(index, {"row": {"engine": state.get("engine")}, "events": [], "raw_seen": False})
                row = attempt["row"]
                events = parse_usage_lines(body, expected_model=_model(row), namespace=f"{job['key']}/{index}",
                                           service_tier=_text(row.get("service_tier")))
                existing_ids = {(event.session_id, event.event_id): event for event in attempt["events"] if event.event_id}
                for event in events:
                    identity = (event.session_id, event.event_id)
                    if event.event_id and identity in existing_ids:
                        if existing_ids[identity] != event:
                            raise ValueError(f"conflicting usage in raw log references: {path}")
                        continue
                    attempt["events"].append(event)
                    if event.event_id:
                        existing_ids[identity] = event
                attempt["raw_seen"] = bool(attempt["events"])
        for index, attempt in job["attempts"].items():
            row = attempt["row"]
            if not attempt["events"]:
                cleaned = dict(row)
                cleaned.pop("model", None)
                if _model(row):
                    cleaned["model"] = _model(row)
                attempt["events"] = parse_usage_lines([json.dumps(cleaned)], expected_model=_model(row),
                                                       service_tier=_text(row.get("service_tier")))
                if not attempt["events"]:
                    attempt["events"] = [_placeholder(_model(row))]
    return list(jobs.values()), journal_count, state_only


def scenario_comparison(policy: dict, monthly_jobs: int | None, included_jobs: int | None,
                        accepted_rate: Any, api_cost_per_job_usd: Any) -> dict:
    """Counterfactual monthly costs, assuming per-job spend includes all retries."""
    if monthly_jobs is not None and (_count(monthly_jobs) is None or monthly_jobs == 0):
        raise ValueError("monthly_jobs must be a positive integer")
    if included_jobs is not None and _count(included_jobs) is None:
        raise ValueError("included_jobs must be a non-negative integer")
    if monthly_jobs is not None and included_jobs is not None and included_jobs > monthly_jobs:
        raise ValueError("included_jobs must not exceed monthly_jobs")
    rate = _decimal(accepted_rate, "accepted_rate") if accepted_rate is not None else None
    if rate is not None and rate > 1:
        raise ValueError("accepted_rate must be from 0 to 1")
    price = _decimal(api_cost_per_job_usd, "api_cost_per_job_usd") if api_cost_per_job_usd is not None else None
    result = {"monthly_jobs": monthly_jobs, "included_jobs": included_jobs,
              "accepted_rate": _money(rate), "api_cost_per_job_usd": _money(price),
              "assumptions": "User scenario only. Per-job API spend includes failed attempts and retries. Same assumed acceptance rate for both routes; no measured human quality or fixed token entitlement.",
              "status": "UNKNOWN", "reason": "requires monthly_jobs, included_jobs, accepted_rate, per-job USD cost and configured fee"}
    fee = Decimal(policy["monthly_subscription_gbp"]) if "monthly_subscription_gbp" in policy else None
    if None in (monthly_jobs, included_jobs, rate, price, fee):
        return result
    accepted = Decimal(monthly_jobs) * rate
    overflow = monthly_jobs - included_jobs
    all_api = monthly_jobs * price
    overflow_usd = overflow * price
    result.update(status="estimated", reason="scenario assumptions, not observed results", accepted_monthly_jobs=str(accepted),
                  overflow_jobs=overflow, subscription={"fixed_gbp": str(fee), "overflow_usd": str(overflow_usd)},
                  all_api={"usd": str(all_api)}, fixed_per_accepted_gbp=_money(fee / accepted if accepted else None),
                  denominator_note="accepted jobs is zero" if not accepted else "monthly_jobs × accepted_rate")
    fx = Decimal(policy["fx"]["usd_to_gbp"]) if "fx" in policy else None
    result["subscription"]["total_gbp"] = _money(fee + overflow_usd * fx if fx is not None else None)
    result["all_api"]["total_gbp"] = _money(all_api * fx if fx is not None else None)
    result["subscription"]["per_accepted_gbp"] = _money((fee + overflow_usd * fx) / accepted if fx is not None and accepted else None)
    result["all_api"]["per_accepted_gbp"] = _money(all_api * fx / accepted if fx is not None and accepted else None)
    result["break_even"] = {"included_jobs_required": "UNKNOWN", "api_cost_per_job_usd_threshold": "UNKNOWN",
                             "reason": "dated FX required to compare GBP fee and USD consumption"}
    if fx is not None:
        result["fx"] = policy["fx"]
        result["savings_gbp"] = str(included_jobs * price * fx - fee)
        result["break_even"] = {"included_jobs_required": _money(fee / (price * fx) if price else None),
                                 "api_cost_per_job_usd_threshold": _money(fee / (included_jobs * fx) if included_jobs else None),
                                 "reason": "included_jobs × retry-inclusive USD/job × dated FX >= fixed fee; zero denominator is UNKNOWN"}
    return result


def build_report(log: Path, state_dir: Path, policy: dict, usage_snapshot: Any = None,
                 monthly_jobs: int | None = None, included_jobs: int | None = None,
                 accepted_rate: Decimal | None = None, api_cost_per_job_usd: Any = None,
                 human_review_cost_gbp: Any = None, coordinator_cost_gbp: Any = None,
                 *, now: datetime | None = None) -> dict:
    policy = validate_cost_policy(policy)
    scenario = scenario_comparison(policy, monthly_jobs, included_jobs, accepted_rate, api_cost_per_job_usd)
    human_cost = _decimal(human_review_cost_gbp, "human review cost") if human_review_cost_gbp is not None else None
    coordinator_cost = _decimal(coordinator_cost_gbp, "coordinator cost") if coordinator_cost_gbp is not None else None
    warnings: list[str] = []
    jobs, journal_count, state_only = _collect(Path(log), Path(state_dir), warnings)
    quota = inspect_usage_snapshot(usage_snapshot, now=now, max_age_seconds=policy["snapshot_max_age_seconds"])
    groups: dict[str, dict] = {}
    event_rows = []
    actuals = {"api": Decimal(0), "credits": Decimal(0), "subscription": Decimal(0), "unknown": Decimal(0)}
    unknown_cash = Counter()
    actual_count = Counter()
    included_count = 0
    first_pass = final_pass = human_accepted = human_rejected = attempts_count = 0
    unattributed_batches = 0
    quality = {name: Counter() for name in ("task_contract_state", "product_state", "promotion_state")}
    months = set()
    for job in jobs:
        ordered = sorted(job["attempts"].items())
        first = ordered[0][1]["row"] if ordered and ordered[0][0] == 1 else {}
        last = ordered[-1][1]["row"] if ordered else {}
        final = {**last, **job["state"]}
        first_pass += _pass(first)
        final_pass += _pass(final)
        review = _human_review(final)
        human_accepted += review == "accepted"
        human_rejected += review == "rejected"
        for name in quality:
            quality[name][str(final.get(name, "UNKNOWN"))] += 1
        stamp = final.get("logged_at", final.get("finished_at", ""))
        try:
            months.add(datetime.fromisoformat(stamp.replace("Z", "+00:00")).strftime("%Y-%m"))
        except (ValueError, AttributeError):
            months.add("unknown")
        task_groups = set()
        for index, attempt in ordered:
            attempts_count += index > 0
            unattributed_batches += index == 0
            row = attempt["row"]
            route = _route(row)
            attempt_groups = set()
            for event in attempt["events"]:
                category = event.billing_category if event.billing_category in {"api", "credits"} else route
                if (event.provider or "").lower() in {"credit", "credits"}:
                    category = "credits"
                if (row.get("worker_engine", row.get("engine")) in {"opencode", "openrouter"}
                        or event.source == "step_finish" or (event.model or "").startswith("openrouter/")):
                    category = "api"
                identity = event.model or _model(row) or "unknown"
                key = f"{category}|{identity}"
                group = groups.setdefault(key, {"route": category, "model": identity, "events": 0, "attempts": 0,
                                                "tasks": 0, "first_check_passes": 0, "final_check_passes": 0,
                                                "human_accepted": 0, "human_review_unknown": 0})
                group["events"] += 1
                attempt_groups.add(key)
                equivalent = estimate_event(event, pricing=policy["pricing"], prefer_actual=False)
                group.setdefault("actual_usd", Decimal(0))
                group.setdefault("unknown_cash_events", 0)
                group.setdefault("harness_estimate_usd", Decimal(0))
                group.setdefault("harness_estimate_events", 0)
                group.setdefault("api_equivalent_lower_usd", Decimal(0))
                group.setdefault("api_equivalent_upper_usd", Decimal(0))
                group.setdefault("api_equivalent_unknown_events", 0)
                if equivalent.lower is not None and equivalent.upper is not None:
                    group["api_equivalent_lower_usd"] += equivalent.lower
                    group["api_equivalent_upper_usd"] += equivalent.upper
                else:
                    group["api_equivalent_unknown_events"] += 1
                provider_actual = estimate_event(event) if event.actual_cost_usd is not None else None
                included = (category == "subscription" and row.get("observed_auth_mode") == "chatgpt"
                            and row.get("included_usage_established") is True
                            and (usage_snapshot is None or quota.available))
                actual_usd = provider_actual.amount if provider_actual and provider_actual.status == "actual" else None
                if event.harness_estimate_usd is not None:
                    group["harness_estimate_usd"] += event.harness_estimate_usd
                    group["harness_estimate_events"] += 1
                if actual_usd is not None:
                    actuals[category] += actual_usd
                    group["actual_usd"] += actual_usd
                    actual_count[category] += 1
                elif included:
                    actual_count[category] += 1
                    included_count += 1
                else:
                    unknown_cash[category] += 1
                    group["unknown_cash_events"] += 1
                credit_estimate = estimate_event(event, category="credits") if category == "credits" else None
                event_rows.append({"run_id": job["key"][0], "task_key": job["key"][1], "attempt": index,
                                   "route": category, "model": identity, "model_source": event.model_source,
                                   "source": event.source, "event_id": event.event_id, "session_id": event.session_id,
                                   "aggregate": event.aggregate, "input_tokens": event.input_tokens,
                                   "cached_input_tokens": event.cached_input_tokens, "cache_write_tokens": event.cache_write_tokens,
                                   "output_tokens": event.output_tokens,
                                   "actual_usd": _money(actual_usd), "included_marginal_gbp": "0" if included and actual_usd is None else "UNKNOWN",
                                   "harness_estimate_usd": _money(event.harness_estimate_usd),
                                   "api_equivalent": asdict(equivalent),
                                   "credit_estimate": asdict(credit_estimate) if credit_estimate else None})
            for key in attempt_groups:
                groups[key]["attempts"] += index > 0
                if index == 1 and _pass(first):
                    groups[key]["first_check_passes"] += 1
                if ordered and index == ordered[-1][0] and _pass(final):
                    groups[key]["final_check_passes"] += 1
            task_groups.update(attempt_groups)
        if not ordered:
            key = f"{_route(final)}|{_model(final) or 'unknown'}"
            groups.setdefault(key, {"route": _route(final), "model": _model(final) or "unknown", "events": 0,
                                    "attempts": 0, "tasks": 0, "first_check_passes": 0, "final_check_passes": 0,
                                    "human_accepted": 0, "human_review_unknown": 0})
            task_groups.add(key)
        for key in task_groups:
            groups[key]["tasks"] += 1
            groups[key]["human_accepted"] += review == "accepted"
            groups[key]["human_review_unknown"] += review == "unknown"
    for group in groups.values():
        group["first_check_pass_rate"] = group["first_check_passes"] / group["tasks"]
        group["final_check_pass_rate"] = group["final_check_passes"] / group["tasks"]
    equivalent_rows = [row["api_equivalent"] for row in event_rows]
    known_bounds = [row for row in equivalent_rows if row["lower"] is not None and row["upper"] is not None]
    bounds = {"lower_usd": str(sum((row["lower"] for row in known_bounds), Decimal(0))),
              "upper_usd": str(sum((row["upper"] for row in known_bounds), Decimal(0))),
              "unknown_events": len(equivalent_rows) - len(known_bounds), "covered_events": len(known_bounds),
              "label": "hypothetical API-equivalent consumption, not invoice; bounds cover known events only"}
    fee = Decimal(policy["monthly_subscription_gbp"]) if "monthly_subscription_gbp" in policy else None
    assumed_accepted = Decimal(monthly_jobs) * Decimal(accepted_rate) if monthly_jobs is not None and accepted_rate is not None else None
    fixed_per_accepted = fee / assumed_accepted if fee is not None and assumed_accepted else None
    total_actual = sum(actuals.values(), Decimal(0))
    complete_cash = bool(event_rows) and not unknown_cash.total() and not warnings
    fx = Decimal(policy["fx"]["usd_to_gbp"]) if "fx" in policy else None
    extra_gbp = total_actual * fx if fx is not None else Decimal(0) if complete_cash and total_actual == 0 else None
    observed_month = next(iter(months)) if len(months) == 1 and "unknown" not in months else None
    machine_per_accepted = ((fee + extra_gbp) / human_accepted
                            if fee is not None and extra_gbp is not None and human_accepted and complete_cash and observed_month else None)
    whole_per_accepted = (machine_per_accepted + (human_cost + coordinator_cost) / human_accepted
                          if machine_per_accepted is not None and human_cost is not None and coordinator_cost is not None else None)
    harness_rows = [row for row in event_rows if row["harness_estimate_usd"] != "UNKNOWN"]
    coverage = {"known_actual_events": sum(actual_count.values()), "unknown_cash_events": unknown_cash.total(),
                "reported_estimate_events": len(harness_rows),
                "reported_estimate_usd": str(sum((Decimal(row["harness_estimate_usd"]) for row in harness_rows), Decimal(0))),
                "total_events": len(event_rows), "complete": complete_cash}
    credit_rows = [row["credit_estimate"] for row in event_rows if row["credit_estimate"] is not None]
    return _jsonable({"schema": "ringer-cost-report/v2", "currency_note": "USD and GBP are not summed. Conversion requires explicit dated FX.",
                     "api": {"actual_usd": str(actuals["api"]), "actual_events": actual_count["api"],
                             "unknown_events": unknown_cash["api"],
                             "harness_estimated_usd": coverage["reported_estimate_usd"],
                             "harness_estimate_events": len(harness_rows),
                             "coverage": "known actual subtotal only; harness estimates are not actual cash"},
                     "credits": {"actual_usd": str(actuals["credits"]), "actual_events": actual_count["credits"],
                                 "unknown_cash_events": unknown_cash["credits"],
                                 "estimated_credits": str(sum((row["amount"] for row in credit_rows if row["amount"] is not None), Decimal(0))),
                                 "unknown_estimate_events": sum(row["amount"] is None for row in credit_rows),
                                 "note": "credits are a separate paid category; units are not USD or API billing"},
                     "subscription": {"monthly_fee_gbp": _money(fee), "fee_evidence": policy.get("subscription_fee_evidence", "not supplied"),
                                      "marginal_cash_gbp": "0" if included_count and not unknown_cash["subscription"] and not actuals["subscription"] else "UNKNOWN",
                                      "included_events": included_count, "actual_extra_usd": str(actuals["subscription"]),
                                      "unknown_cash_events": unknown_cash["subscription"],
                                      "fixed_allocation_per_accepted_gbp": _money(fixed_per_accepted),
                                      "allocation_basis": "configured monthly fee / (assumed monthly_jobs × accepted_rate), once",
                                      "quota_measurement": asdict(quota)},
                     "unclassified": {"actual_usd": str(actuals["unknown"]), "unknown_cash_events": unknown_cash["unknown"]},
                     "api_equivalent": bounds, "actual_cost_coverage": coverage,
                     "jobs": {"total_tasks": len(jobs), "journal_attempts": journal_count, "all_attempts": attempts_count,
                              "state_only_tasks": state_only, "unattributed_usage_batches": unattributed_batches,
                              "coverage": "partial source coverage" if warnings else "supplied journal and state",
                              "first_check_passes": first_pass, "final_check_passes": final_pass,
                              "first_check_pass_rate": first_pass / len(jobs) if jobs else None,
                              "final_check_pass_rate": final_pass / len(jobs) if jobs else None,
                              "human_accepted_jobs": human_accepted, "human_rejected_jobs": human_rejected,
                              "human_review_unknown_jobs": len(jobs) - human_accepted - human_rejected,
                              "quality": "Check passes support only the executed check; human acceptance requires supplied hash-bound review evidence.",
                              "evidence_states": {name: dict(counts) for name, counts in quality.items()},
                              "cost_per_accepted": {"worker_and_fixed_gbp": _money(machine_per_accepted), "whole_job_gbp": _money(whole_per_accepted),
                                                    "actual_incremental_usd": _money(total_actual / human_accepted if complete_cash and human_accepted else None),
                                                    "month": observed_month, "denominator": human_accepted,
                                                    "basis": "one observed calendar month: (configured fixed fee + actual extra cash) / human-accepted jobs; missing month, costs or zero acceptance stays UNKNOWN"},
                              "human_review_cost_gbp": _money(human_cost), "coordinator_cost_gbp": _money(coordinator_cost)},
                     "groups": groups, "events": event_rows, "scenario": scenario, "policy": policy, "warnings": warnings})


def render_markdown(report: dict) -> str:
    jobs = report["jobs"]
    lines = ["# Ringer cost report", "", "API-equivalent estimates are hypothetical consumption, not invoices.", "",
             f"- API actual subtotal: USD {report['api']['actual_usd']}; unknown API cost events: {report['api']['unknown_events']}.",
             f"- Credits actual subtotal: USD {report['credits']['actual_usd']}; estimated units: {report['credits']['estimated_credits']} (separate paid category).",
             f"- Subscription monthly fee: GBP {report['subscription']['monthly_fee_gbp']}; fixed fee per assumed accepted monthly job: GBP {report['subscription']['fixed_allocation_per_accepted_gbp']}.",
             f"- Tasks: {jobs['total_tasks']}; attempts including retries: {jobs['all_attempts']}; state-only tasks: {jobs['state_only_tasks']}.",
             f"- First/final check passes: {jobs['first_check_passes']}/{jobs['final_check_passes']}; rates: {jobs['first_check_pass_rate']}/{jobs['final_check_pass_rate']}.",
             f"- Human-accepted jobs: {jobs['human_accepted_jobs']}; human review unknown: {jobs['human_review_unknown_jobs']}.",
             f"- Actual cost coverage: {report['actual_cost_coverage']['known_actual_events']}/{report['actual_cost_coverage']['total_events']} events; unknowns remain unpriced.",
             f"- API-equivalent known-event bounds: USD {report['api_equivalent']['lower_usd']} to {report['api_equivalent']['upper_usd']}; unknown events: {report['api_equivalent']['unknown_events']}.",
             f"- Observed worker/fixed cash per human-accepted job: GBP {jobs['cost_per_accepted']['worker_and_fixed_gbp']}; whole job including review/coordinator: GBP {jobs['cost_per_accepted']['whole_job_gbp']}.",
             "", "| Route | Exact model | Tasks | Attempts | First/final check passes |", "|---|---|---:|---:|---:|"]
    for group in report["groups"].values():
        model = group['model'].replace('|', '\\|')
        lines.append(f"| {group['route']} | {model} | {group['tasks']} | {group['attempts']} | {group['first_check_passes']}/{group['final_check_passes']} |")
    scenario = report["scenario"]
    lines += ["", "## Monthly scenario", "", scenario["assumptions"]]
    if scenario["status"] == "estimated":
        lines += ["", f"Subscription: GBP {scenario['subscription']['fixed_gbp']} fixed + USD {scenario['subscription']['overflow_usd']} overflow; total GBP {scenario['subscription']['total_gbp']}.",
                  f"All API: USD {scenario['all_api']['usd']}; total GBP {scenario['all_api']['total_gbp']}.",
                  f"Per accepted job: subscription GBP {scenario['subscription']['per_accepted_gbp']}; all API GBP {scenario['all_api']['per_accepted_gbp']}.",
                  f"Break-even included jobs: {scenario['break_even']['included_jobs_required']}. {scenario['break_even']['reason']}"]
    else:
        lines += ["", f"UNKNOWN: {scenario['reason']}."]
    lines += ["", f"Human review cost: GBP {jobs['human_review_cost_gbp']}; coordinator cost: GBP {jobs['coordinator_cost_gbp']}.",
              "Check evidence, product state and promotion state remain distinct. This report creates no approval receipt."]
    if report["warnings"]:
        lines += ["", "Coverage warnings:", ""] + [f"- {warning}" for warning in report["warnings"]]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", type=Path, help="default: STATE_DIR/runs.jsonl")
    parser.add_argument("--state-dir", type=Path, default=Path.home() / ".ringer")
    parser.add_argument("--policy", type=Path, help="default: STATE_DIR/cost-policy.json, or conservative empty policy")
    parser.add_argument("--usage-snapshot", type=Path)
    parser.add_argument("--monthly-jobs", type=int)
    parser.add_argument("--included-jobs", type=int)
    parser.add_argument("--accepted-rate", type=Decimal)
    parser.add_argument("--api-cost-per-job-usd", type=Decimal)
    parser.add_argument("--human-review-cost-gbp", type=Decimal)
    parser.add_argument("--coordinator-cost-gbp", type=Decimal)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        policy_path = args.policy or args.state_dir / "cost-policy.json"
        policy = json.loads(policy_path.read_text(encoding="utf-8")) if args.policy or policy_path.exists() else {}
        snapshot = json.loads(args.usage_snapshot.read_text(encoding="utf-8")) if args.usage_snapshot else None
        report = build_report(args.log or args.state_dir / "runs.jsonl", args.state_dir, policy, snapshot,
                              args.monthly_jobs, args.included_jobs, args.accepted_rate, args.api_cost_per_job_usd,
                              args.human_review_cost_gbp, args.coordinator_cost_gbp)
    except (OSError, ValueError, TypeError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) if args.json else render_markdown(report), end="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
