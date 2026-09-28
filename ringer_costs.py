"""Local, stdlib-only accounting and dispatch boundaries. See SUBSCRIPTION-COSTS.md.

No credentials or network access. Money is Decimal; None means unknown, never zero.
The runner must attest authentication, mark reservations started before dispatch,
and supply authoritative actual costs for settlement.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator

LONG_INPUT_THRESHOLD = 272000
PRICING_SOURCE = "https://developers.openai.com/api/docs/pricing"
PRICING_DATE = "2026-09-03"
ASTRA_PRICING_DATE = "2026-09-05"
ASTRA_CREDIT_SOURCE = "https://learn.chatgpt.com/docs/pricing"
MILLION = Decimal(1_000_000)
OPENCODE_PART_COST_SOURCE = "https://github.com/anomalyco/opencode/blob/v1.18.20/packages/opencode/src/session/session.ts#L321"


def _rates(input_: str, cached: str, write: str, output: str) -> dict[str, Decimal]:
    return dict(zip(("input", "cached", "cache_write", "output"),
                    map(Decimal, (input_, cached, write, output))))


RATES = {
    "sol": {"short": _rates("4", "0.4", "5", "20"),
            "long": _rates("8", "0.8", "10", "30")},
    "terra": {"short": _rates("2", "0.2", "2.5", "12"),
              "long": _rates("4", "0.4", "5", "18")},
    "luna": {"short": _rates("0.2", "0.02", "0.25", "1.2"),
              "long": _rates("0.4", "0.04", "0.5", "1.8")},
    "astra": {"short": _rates("10", "1", "12.5", "50"),
              "long": _rates("20", "2", "25", "75")},
}
CREDIT_RATES = {
    "sol": _rates("100", "10", "0", "500"),
    "terra": _rates("50", "5", "0", "300"),
    "luna": _rates("5", "0.5", "0", "30"),
    "astra": _rates("250", "25", "0", "1250"),
}
# Explicit aliases only. In particular, provider slugs are not suffix matched.
MODEL_ALIASES = {
    alias: name
    for name in ("sol", "terra", "luna")
    for alias in (name, f"gpt-5.6-{name}")
}
MODEL_ALIASES["gpt-6-astra"] = "astra"
GPT56_MODELS = {"sol", "terra", "luna"}


@dataclass(frozen=True)
class UsageEvent:
    model: str | None
    provider: str | None
    input_tokens: int | None
    cached_input_tokens: int | None
    output_tokens: int | None
    cache_write_tokens: int | None = 0
    actual_cost_usd: Decimal | None = None
    source: str = "unknown"
    aggregate: bool = False
    event_id: str | None = None
    session_id: str | None = None
    attempt_id: str | None = None
    model_source: str = "unknown"
    service_tier: str | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    billing_category: str | None = None
    issue: str = ""
    # OpenCode 1.18.20 computes part.cost from its local model catalogue. It
    # is useful evidence of a harness estimate, never provider cash evidence.
    harness_estimate_usd: Decimal | None = None


@dataclass(frozen=True)
class CostResult:
    status: str  # actual, estimate, unknown
    amount: Decimal | None
    currency: str
    source: str
    coverage: str
    reason: str = ""
    lower: Decimal | None = None
    upper: Decimal | None = None


@dataclass(frozen=True)
class BillingDecision:
    allow: bool
    reason: str
    route: str
    reservation_id: str | None = None


@dataclass(frozen=True)
class QuotaEvidence:
    available: bool
    reason: str
    observed_at: str | None = None
    windows: tuple[dict[str, Any], ...] = ()
    plan_type: str | None = None
    credits: dict[str, Any] | None = None


def _decimal(value: Any, name: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f"{name} must be a finite number") from None
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise ValueError(f"{name} must be finite and {'positive' if positive else 'non-negative'}")
    return result


def _count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _dated(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("date must be YYYY-MM-DD")
    date.fromisoformat(value)
    return value


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("observed_at must be an ISO timestamp with timezone")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("observed_at must include timezone")
    return result


def canonical_model(model: str | None) -> str | None:
    return MODEL_ALIASES.get(model) if isinstance(model, str) else None


def validate_cost_policy(raw: Any) -> dict[str, Any]:
    """Validate JSON policy. No account fee or exchange rate is a library default."""
    if not isinstance(raw, dict):
        raise ValueError("cost policy must be an object")
    allowed = {"monthly_api_cap_gbp", "run_api_cap_gbp", "automatic_paid_fallback",
               "subscription_auth_modes", "fx", "monthly_subscription_gbp",
               "subscription_fee_evidence", "snapshot_max_age_seconds", "pricing"}
    if any(not isinstance(key, str) for key in raw):
        raise ValueError("cost policy keys must be strings")
    if set(raw) - allowed:
        raise ValueError(f"unknown cost policy keys: {sorted(set(raw) - allowed)}")
    fallback = raw.get("automatic_paid_fallback", False)
    if type(fallback) is not bool:
        raise ValueError("automatic_paid_fallback must be boolean")
    modes = raw.get("subscription_auth_modes", ["chatgpt"])
    if not isinstance(modes, list) or any(mode != "chatgpt" for mode in modes):
        raise ValueError("subscription_auth_modes may only contain chatgpt")
    age = raw.get("snapshot_max_age_seconds", 900)
    if _count(age) is None or age == 0:
        raise ValueError("snapshot_max_age_seconds must be a positive integer")
    result = {"monthly_api_cap_gbp": str(_decimal(raw.get("monthly_api_cap_gbp", 0), "cap")),
              "automatic_paid_fallback": fallback, "subscription_auth_modes": sorted(set(modes)),
              "snapshot_max_age_seconds": age}
    for key in ("run_api_cap_gbp", "monthly_subscription_gbp"):
        if key in raw:
            result[key] = str(_decimal(raw[key], key))
    if "subscription_fee_evidence" in raw:
        if not _text(raw["subscription_fee_evidence"]):
            raise ValueError("subscription_fee_evidence must be text")
        result["subscription_fee_evidence"] = raw["subscription_fee_evidence"]
    if "fx" in raw:
        fx = raw["fx"]
        if not isinstance(fx, dict) or not {"usd_to_gbp", "dated"} <= set(fx) or set(fx) - {"usd_to_gbp", "dated", "source"}:
            raise ValueError("fx requires usd_to_gbp and dated, with optional source")
        result["fx"] = {"usd_to_gbp": str(_decimal(fx["usd_to_gbp"], "FX", positive=True)),
                        "dated": _dated(fx["dated"])}
        if "source" in fx:
            if not _text(fx["source"]):
                raise ValueError("FX source must be text")
            result["fx"]["source"] = fx["source"]
    pricing = raw.get("pricing", {})
    if not isinstance(pricing, dict):
        raise ValueError("pricing must map exact model identities to dated rate records")
    result["pricing"] = {}
    for model, entry in pricing.items():
        if not _text(model) or not isinstance(entry, dict):
            raise ValueError("invalid pricing entry")
        required = {"dated", "source", "input", "cached", "output"}
        if not required <= set(entry) or set(entry) - required - {"cache_write"}:
            raise ValueError("pricing requires dated/source/input/cached/output; optional cache_write")
        if not _text(entry["source"]):
            raise ValueError("pricing source must be text")
        rates = {key: str(_decimal(entry[key], f"pricing.{key}"))
                 for key in ("input", "cached", "output", "cache_write") if key in entry}
        result["pricing"][model] = {**rates, "dated": _dated(entry["dated"]), "source": entry["source"]}
    return result


def inspect_usage_snapshot(snapshot: Any, *, now: datetime | None = None,
                           max_age_seconds: int = 900) -> QuotaEvidence:
    """Require fresh sanitised codex limit evidence; never infer remaining tokens."""
    try:
        if _count(max_age_seconds) is None or max_age_seconds == 0:
            raise ValueError("snapshot age limit must be a positive integer")
        if not isinstance(snapshot, dict) or not snapshot:
            raise ValueError("quota snapshot is missing or empty")
        observed = _timestamp(snapshot.get("observed_at"))
        current = now or datetime.now(timezone.utc)
        age = (current - observed).total_seconds()
        if age < 0 or age > max_age_seconds:
            raise ValueError("quota snapshot is stale or future-dated")
        if "exhausted" in snapshot and type(snapshot["exhausted"]) is not bool:
            raise ValueError("exhausted must be boolean")
        limits = snapshot.get("rateLimitsByLimitId")
        if not isinstance(limits, dict) or not isinstance(limits.get("codex"), dict):
            raise ValueError("codex quota limits are missing")
        limit = limits["codex"]
        spend = limit.get("spendControlReached")
        if spend is not None and type(spend) is not bool:
            raise ValueError("spendControlReached must be boolean or null")
        if limit.get("individualLimit") is not None:
            raise ValueError("individual quota limit requires supported evidence before dispatch")
        if spend or snapshot.get("exhausted") is True or limit.get("rateLimitReachedType") is not None:
            raise ValueError("subscription quota or spend control reached; no automatic route switch")
        windows = []
        for key in ("primary", "secondary"):
            window = limit.get(key)
            if window is None:
                continue
            if not isinstance(window, dict):
                raise ValueError(f"{key} quota window must be an object")
            used = window.get("usedPercent")
            if type(used) not in (int, float) or not 0 <= _decimal(used, "usedPercent") <= 100:
                raise ValueError("usedPercent must be a number from 0 to 100")
            duration = _count(window.get("windowDurationMins"))
            reset = _count(window.get("resetsAt"))
            if not duration or reset is None or reset <= current.timestamp():
                raise ValueError("quota duration/reset missing, invalid or expired")
            if used >= 100:
                raise ValueError("subscription quota exhausted; no automatic route switch")
            windows.append({"window": key, "used_percent": used, "duration_minutes": duration,
                            "resets_at": reset})
        if not windows or limit.get("primary") is None:
            raise ValueError("primary quota window is missing")
        credits = limit.get("credits")
        if credits is not None:
            if not isinstance(credits, dict) or any(type(credits.get(k)) is not bool for k in ("hasCredits", "unlimited")):
                raise ValueError("invalid credit status")
            balance = _decimal(credits.get("balance"), "credit balance")
            credits = {"hasCredits": credits["hasCredits"], "unlimited": credits["unlimited"],
                       "balance": str(balance)}
        plan = limit.get("planType")
        if plan is not None and not _text(plan):
            raise ValueError("invalid planType")
        return QuotaEvidence(True, "fresh observed quota available; exact entitlement unknown",
                             observed.isoformat(), tuple(windows), plan, credits)
    except (ValueError, TypeError, OverflowError) as exc:
        return QuotaEvidence(False, str(exc))


def _unknown(reason: str, currency: str = "USD") -> CostResult:
    return CostResult("unknown", None, currency, "none", "insufficient", reason)


def estimate_event(event: UsageEvent, *, category: str = "api",
                   pricing: dict[str, Any] | None = None,
                   service_tier: str | None = None,
                   prefer_actual: bool = True) -> CostResult:
    """Actual provider USD overrides API estimates. Bounds are not invoices.

    Input includes cache read/write subsets. OpenCode adapter normalises its
    exclusive counters. A caller constructing events must use that convention.
    """
    if category not in {"api", "credits"}:
        raise ValueError("category must be api or credits")
    currency = "credits" if category == "credits" else "USD"
    if category == "api" and prefer_actual and event.actual_cost_usd is not None:
        try:
            amount = _decimal(event.actual_cost_usd, "provider cost")
        except ValueError as exc:
            return _unknown(str(exc))
        return CostResult("actual", amount, "USD", "provider-reported", "event", lower=amount, upper=amount)
    if category == "api" and prefer_actual and event.harness_estimate_usd is not None:
        try:
            amount = _decimal(event.harness_estimate_usd, "harness estimate")
        except ValueError as exc:
            return _unknown(str(exc))
        return CostResult("estimate", amount, "USD", OPENCODE_PART_COST_SOURCE,
                          "harness-reported-catalogue-estimate", lower=amount, upper=amount)
    counters = (event.input_tokens, event.cached_input_tokens, event.cache_write_tokens, event.output_tokens)
    if event.issue or any(_count(value) is None for value in counters):
        return _unknown(event.issue or "complete input/cache/output breakdown missing", currency)
    inp, cached, write, out = counters
    if cached + write > inp:
        return _unknown("cache subsets exceed total input", currency)
    tier = service_tier or event.service_tier
    model = canonical_model(event.model)
    override = (pricing or {}).get(event.model)
    if category == "credits":
        if not model or tier != "standard" or write or event.aggregate or inp > LONG_INPUT_THRESHOLD:
            return _unknown("credit rates cover only standard short per-request input/cache-read/output", currency)
        rates = CREDIT_RATES[model]
        amount = _charge(inp, cached, write, out, rates)
        credit_source = (
            f"{ASTRA_CREDIT_SOURCE}#{ASTRA_PRICING_DATE}"
            if model == "astra"
            else "https://developers.openai.com/codex/pricing#2026-09-03"
        )
        return CostResult("estimate", amount, currency, credit_source,
                          "standard-short-request", lower=amount, upper=amount)
    if override is not None:
        # Public callers supply a validated policy pricing map.
        if write and "cache_write" not in override:
            return _unknown("dated model pricing lacks cache-write rate")
        if tier not in (None, "standard"):
            return _unknown("configured comparison rates cover standard service only")
        rates = {key: Decimal(override.get(key, "0"))
                 for key in ("input", "cached", "cache_write", "output")}
        amount = _charge(inp, cached, write, out, rates)
        return CostResult("estimate", amount, "USD", f"{override['source']}#{override['dated']}",
                          "configured-standard-comparison", lower=amount, upper=amount)
    if not model:
        return _unknown("exact model identity has no dated pricing")
    if tier not in {"standard", "fast"}:
        return _unknown("service tier unknown; specify standard or known GPT-5.6 fast")
    if tier == "fast" and (
        model not in GPT56_MODELS or event.aggregate or inp > LONG_INPUT_THRESHOLD
    ):
        return _unknown("verified fast rates only cover GPT-5.6 short requests")
    short = _charge(inp, cached, write, out, RATES[model]["short"])
    long = _charge(inp, cached, write, out, RATES[model]["long"])
    if event.aggregate:
        pricing_date = ASTRA_PRICING_DATE if model == "astra" else PRICING_DATE
        return CostResult("estimate", None, "USD", f"{PRICING_SOURCE}#{pricing_date}",
                          "aggregate-short-long-bounds", "request context sizes unavailable", short, long)
    context = "long" if inp > LONG_INPUT_THRESHOLD else "short"
    amount = (long if context == "long" else short) * (2 if tier == "fast" else 1)
    pricing_date = ASTRA_PRICING_DATE if model == "astra" else PRICING_DATE
    return CostResult("estimate", amount, "USD", f"{PRICING_SOURCE}#{pricing_date}",
                      "per-request", f"{model} {tier} {context}", amount, amount)


def _charge(inp: int, cached: int, write: int, out: int, rates: dict[str, Decimal]) -> Decimal:
    return ((inp - cached - write) * rates["input"] + cached * rates["cached"]
            + write * rates["cache_write"] + out * rates["output"]) / MILLION


def parse_usage_lines(lines: list[str], *, expected_model: str | None = None,
                      namespace: str = "", service_tier: str | None = None) -> list[UsageEvent]:
    """Parse raw JSONL/text without changing it. Expected model is caller attested.

    Repeated stable event IDs are deduplicated within attempt/session namespace.
    ID-less completions remain separate: identical counts can be distinct calls.
    """
    events: list[UsageEvent] = []
    seen: dict[tuple[str, str, str, str], UsageEvent] = {}
    header_model = None
    header_tier = None
    session = ""
    attempt = ""
    for number, line in enumerate(lines, 1):
        start = re.match(r"\[ringer\.py\] attempt (\d+) started\b", line)
        if start:
            attempt = start[1]
            header_model = header_tier = None
            session = ""
            continue
        match = re.fullmatch(r"\s*model:\s*(\S+)\s*", line)
        if match:
            header_model = match[1]
            continue
        if line.strip() == "tokens used":
            # Codex text output is a total, never an input/output breakdown.
            continue
        if number > 1 and lines[number - 2].strip() == "tokens used":
            total = line.strip().replace(",", "")
            if total.isdigit():
                events.append(UsageEvent(header_model or expected_model, None, None, None, None,
                                         source="codex-text-total", aggregate=True, total_tokens=int(total),
                                         attempt_id=attempt or None, model_source="header" if header_model else "caller-attested" if expected_model else "unknown"))
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            if line.lstrip().startswith("{"):
                events.append(UsageEvent(None, None, None, None, None, source="malformed-json",
                                         attempt_id=attempt or None, issue="malformed JSON event"))
            continue
        if not isinstance(row, dict):
            continue
        kind = row.get("type", row.get("event", ""))
        if kind in {"thread.started", "session.started"}:
            session = _text(row.get("thread_id")) or _text(row.get("sessionID")) or ""
            header_model = None
            header_tier = None
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        if kind in {"session_meta", "turn_context", "header", "session.started", "thread.started"}:
            header_model = _text(row.get("model")) or _text(payload.get("model")) or header_model
            header_tier = _text(row.get("service_tier")) or _text(payload.get("service_tier")) or header_tier
        part = row.get("part") if isinstance(row.get("part"), dict) else row
        usage = row.get("usage") if isinstance(row.get("usage"), dict) else row
        is_step = kind == "step_finish" or part.get("type") == "step-finish"
        if is_step:
            tokens = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
        elif kind == "turn.completed":
            tokens = usage
        else:
            # Journal counters are aggregate. Raw token_count cumulative snapshots
            # are retained as unknown, rather than summed as request increments.
            if not any(key in row for key in ("worker_tokens", "tokens", "input_tokens", "usage", "provider_cost", "cost")):
                continue
            tokens = row.get("tokens") if isinstance(row.get("tokens"), dict) else usage
        cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
        inp = _count(tokens.get("input", tokens.get("input_tokens")))
        cached = _count(cache.get("read", tokens.get("cached_input_tokens")))
        write = _count(cache.get("write", tokens.get("cache_write_input_tokens",
                                                        tokens.get("cache_write_tokens", None if is_step else 0))))
        out = _count(tokens.get("output", tokens.get("output_tokens")))
        reasoning = _count(tokens.get("reasoning", 0))
        if is_step:
            # OpenCode's observed counters are exclusive, including reasoning.
            inp = inp + cached + write if None not in (inp, cached, write) else None
            out = out + reasoning if None not in (out, reasoning) else None
        actual = None
        harness_estimate = None
        issue = ""
        # `provider_cost` is explicit metadata supplied by the provider path.
        # OpenCode step-finish `part.cost` has a distinct, client-calculated
        # provenance and must never settle a reservation as cash.
        cost = row.get("provider_cost")
        if isinstance(cost, dict):
            cost = cost.get("usd", cost.get("amount")) if cost.get("currency", "USD") == "USD" else None
        if cost is not None:
            try:
                actual = _decimal(cost, "provider cost")
            except ValueError:
                issue = "invalid provider cost"
        if is_step and "cost" in part:
            try:
                harness_estimate = _decimal(part["cost"], "OpenCode part.cost")
            except ValueError:
                issue = issue or "invalid OpenCode part.cost"
        explicit_model = (_text(row.get("reported_model")) or _text(row.get("model"))
                          or _text(part.get("model")) or _text(usage.get("model")))
        model = explicit_model or header_model or expected_model
        source = "event" if explicit_model else "header" if header_model else "caller-attested" if expected_model else "unknown"
        session_id = _text(row.get("sessionID")) or _text(part.get("sessionID")) or _text(row.get("thread_id")) or session
        attempt_id = _text(row.get("attempt_id")) or attempt
        identity = _text(part.get("id")) or _text(row.get("turn_id")) or _text(row.get("step_id")) or _text(row.get("id"))
        event = UsageEvent(model, _text(row.get("provider")) or _text(part.get("provider")),
                           inp, cached, out, write, actual, str(kind), not is_step,
                           identity, session_id or None, attempt_id or None, source,
                           _text(row.get("service_tier")) or header_tier or service_tier,
                           reasoning, _count(tokens.get("total_tokens", tokens.get("total", row.get("worker_tokens")))),
                           _text(row.get("billing_category")), issue, harness_estimate)
        key = (namespace, attempt_id, session_id, identity or f"line:{number}")
        if key in seen:
            if seen[key] != event:
                raise ValueError(f"conflicting usage for event ID {identity!r}")
            continue
        seen[key] = event
        events.append(event)
    return events


def parse_usage_jsonl(path: Path, *, expected_model: str | None = None,
                      service_tier: str | None = None) -> list[UsageEvent]:
    return parse_usage_lines(Path(path).read_text(encoding="utf-8").splitlines(),
                             expected_model=expected_model, namespace=str(Path(path).resolve()),
                             service_tier=service_tier)


def _month(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value) or value.startswith("0000"):
        raise ValueError("month must be YYYY-MM")
    return value


class SpendLedger:
    """Atomic monthly/per-run allowance reservations, not provider hard caps.

    A reservation's amount/month/run never changes. Actual overspend is retained
    and blocks further reservations for that month. Unknown settlement stays held.
    """
    def __init__(self, state_dir: Path):
        self.path = Path(state_dir) / "cost-reservations.json"
        self.lock_path = Path(state_dir) / "cost-reservations.lock"
        missing = []
        parent = self.path.parent
        while not parent.exists():
            missing.append(parent)
            parent = parent.parent
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Persist newly created directory entries before any reservation can be
        # acknowledged. The final commit also fsyncs the ledger directory.
        for directory in reversed(missing):
            self._sync_directory(directory.parent)

    @staticmethod
    def _sync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _validate(data: Any) -> None:
        if not isinstance(data, dict) or set(data) != {"schema", "reservations"} or data["schema"] != "ringer-spend/v2" or not isinstance(data["reservations"], dict):
            raise ValueError("invalid reservation ledger schema; preserve and reconcile manually")
        fields = {"amount_gbp", "month", "state", "run_id", "started", "actual_gbp", "overspend", "settlement_source", "release_evidence"}
        for identity, row in data["reservations"].items():
            if not _text(identity) or not isinstance(row, dict) or set(row) != fields:
                raise ValueError("invalid reservation record")
            amount = _decimal(row["amount_gbp"], "reservation", positive=True)
            _month(row["month"])
            if row["state"] not in {"reserved", "settled", "released"} or type(row["started"]) is not bool or type(row["overspend"]) is not bool:
                raise ValueError("invalid reservation state")
            if row["run_id"] is not None and not _text(row["run_id"]):
                raise ValueError("invalid run_id")
            if row["state"] == "settled":
                actual = _decimal(row["actual_gbp"], "actual")
                if row["overspend"] != (actual > amount) or not _text(row["settlement_source"]) or row["release_evidence"] is not None:
                    raise ValueError("invalid settlement evidence")
            elif row["actual_gbp"] is not None or row["overspend"] or row["settlement_source"] is not None:
                raise ValueError("unsettled record contains actual cost")
            if row["state"] == "released":
                SpendLedger._release_evidence(row["release_evidence"], row["started"])
            elif row["release_evidence"] is not None:
                raise ValueError("unexpected release evidence")

    @staticmethod
    def _release_evidence(evidence: Any, started: bool) -> dict[str, Any]:
        if not started and evidence == {"kind": "not_dispatched"}:
            return evidence
        if not isinstance(evidence, dict) or set(evidence) != {"kind", "reference", "observed_at"} or evidence["kind"] != "provider_no_spend" or not _text(evidence["reference"]):
            raise ValueError("started attempts require authoritative provider_no_spend evidence")
        _timestamp(evidence["observed_at"])
        return evidence

    @staticmethod
    def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key in reservation ledger")
            result[key] = value
        return result

    @contextlib.contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        with self.lock_path.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            temporary = None
            try:
                try:
                    data = json.loads(self.path.read_text(encoding="utf-8"), object_pairs_hook=self._unique_object)
                except FileNotFoundError:
                    data = {"schema": "ringer-spend/v2", "reservations": {}}
                self._validate(data)
                yield data
                self._validate(data)
                fd, temporary = tempfile.mkstemp(prefix=".cost-", dir=self.path.parent)
                with os.fdopen(fd, "w", encoding="utf-8") as output:
                    json.dump(data, output, sort_keys=True, allow_nan=False)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, self.path)
                temporary = None
                self._sync_directory(self.path.parent)
            finally:
                if temporary is not None:
                    os.unlink(temporary)
                fcntl.flock(lock, fcntl.LOCK_UN)

    def reserve(self, attempt_id: str, amount_gbp: Any, cap_gbp: Any, month: str,
                *, run_id: str | None = None, run_cap_gbp: Any = None) -> bool:
        if not _text(attempt_id) or (run_id is not None and not _text(run_id)):
            raise ValueError("attempt_id/run_id must be non-empty strings")
        month = _month(month)
        amount = _decimal(amount_gbp, "reservation", positive=True)
        cap = _decimal(cap_gbp, "cap")
        run_cap = _decimal(run_cap_gbp, "run cap") if run_cap_gbp is not None else None
        if run_cap is not None and run_id is None:
            raise ValueError("run cap requires run_id")
        with self._locked() as data:
            rows = data["reservations"]
            existing = rows.get(attempt_id)
            overspent = any(row["month"] == month and row["overspend"] for row in rows.values())
            if existing is not None:
                if Decimal(existing["amount_gbp"]) != amount or existing["month"] != month or existing["run_id"] != run_id:
                    raise ValueError("attempt ID conflicts with original amount/month/run")
                return existing["state"] == "reserved" and not existing["started"] and not overspent
            active = [row for row in rows.values() if row["month"] == month and row["state"] != "released"]
            if overspent:
                return False
            def cost(row: dict[str, Any]) -> Decimal:
                return Decimal(row["actual_gbp"] if row["state"] == "settled" else row["amount_gbp"])
            if sum(map(cost, active), Decimal(0)) + amount > cap:
                return False
            if run_cap is not None and sum((cost(row) for row in rows.values() if row["run_id"] == run_id and row["state"] != "released"), Decimal(0)) + amount > run_cap:
                return False
            rows[attempt_id] = {"amount_gbp": str(amount), "month": month, "state": "reserved",
                                "run_id": run_id, "started": False, "actual_gbp": None,
                                "overspend": False, "settlement_source": None, "release_evidence": None}
            return True

    def mark_started(self, attempt_id: str) -> bool:
        with self._locked() as data:
            row = data["reservations"].get(attempt_id)
            if row is None or row["state"] != "reserved" or row["started"]:
                return False
            if any(item["month"] == row["month"] and item["overspend"]
                                          for item in data["reservations"].values()):
                return False
            row["started"] = True
            return True

    def settle(self, attempt_id: str, actual_gbp: Any, *, source: str = "caller-attested-provider-actual") -> bool:
        if actual_gbp is None:
            return False
        actual = _decimal(actual_gbp, "actual")
        if not _text(source):
            raise ValueError("settlement source must identify authoritative actual cost")
        with self._locked() as data:
            row = data["reservations"].get(attempt_id)
            if row is None or row["state"] == "released":
                return False
            if row["state"] == "settled":
                return Decimal(row["actual_gbp"]) == actual and row["settlement_source"] == source
            row.update(state="settled", actual_gbp=str(actual), overspend=actual > Decimal(row["amount_gbp"]), settlement_source=source)
            return True

    def release(self, attempt_id: str, *, evidence: dict[str, Any] | None = None) -> bool:
        with self._locked() as data:
            row = data["reservations"].get(attempt_id)
            if row is None or row["state"] == "settled":
                return False
            proof = self._release_evidence(evidence or ({"kind": "not_dispatched"} if not row["started"] else None), row["started"])
            if row["state"] == "released":
                return row["release_evidence"] == proof
            row.update(state="released", release_evidence=proof)
            return True


def billing_preflight(*, engine: str, model: str | None, policy: Any,
                      observed_auth_mode: str | None, usage_snapshot: Any,
                      task_spend_allowance_gbp: Any = None, attempt_id: str | None = None,
                      state_dir: Path | None = None, month: str | None = None,
                      billing_route: str | None = None, auto_fallback_requested: bool = False,
                      run_id: str | None = None, now: datetime | None = None) -> BillingDecision:
    """Allow/block only. Does not dispatch, change auth, or switch billing routes."""
    route = "unknown"
    try:
        p = validate_cost_policy(policy)
        if not _text(engine) or (model is not None and not _text(model)):
            raise ValueError("engine/model must be strings")
        if billing_route is not None and not _text(billing_route):
            raise ValueError("billing_route must be a string")
        route = billing_route or ("subscription" if engine in {"codex", "chatgpt"} else "api")
        if type(auto_fallback_requested) is not bool:
            raise ValueError("auto_fallback_requested must be boolean")
        # An explicit provider route is API even if the caller labels it included.
        if engine in {"opencode", "openrouter"} or (isinstance(model, str) and model.startswith("openrouter/")):
            route = "api"
        if route == "subscription":
            if engine not in {"codex", "chatgpt"} or observed_auth_mode != "chatgpt" or "chatgpt" not in p["subscription_auth_modes"]:
                return BillingDecision(False, "subscription requires observed ChatGPT authentication", route)
            evidence = inspect_usage_snapshot(usage_snapshot, now=now, max_age_seconds=p["snapshot_max_age_seconds"])
            return BillingDecision(evidence.available, evidence.reason, route)
        if route != "api":
            return BillingDecision(False, "unsupported billing route; explicit credit spending is not authorised here", route)
        if engine in {"codex", "chatgpt"} and observed_auth_mode != "api_key":
            return BillingDecision(False, "explicit Codex API route requires observed api_key auth", route)
        if auto_fallback_requested and not p["automatic_paid_fallback"]:
            return BillingDecision(False, "automatic paid fallback is disabled", route)
        if task_spend_allowance_gbp is None:
            return BillingDecision(False, "paid API requires explicit task spend allowance", route)
        allowance = _decimal(task_spend_allowance_gbp, "allowance", positive=True)
        if not attempt_id or state_dir is None:
            return BillingDecision(False, "paid API requires attempt ID and reservation ledger", route)
        if not SpendLedger(state_dir).reserve(attempt_id, allowance, p["monthly_api_cap_gbp"],
                                              month or (now or datetime.now(timezone.utc)).strftime("%Y-%m"),
                                              run_id=run_id, run_cap_gbp=p.get("run_api_cap_gbp")):
            return BillingDecision(False, "allowance unavailable, terminal reservation or recorded overspend", route)
        return BillingDecision(True, "explicit API allowance reserved; not a provider hard cap", route, attempt_id)
    except (ValueError, TypeError, OSError) as exc:
        return BillingDecision(False, f"billing evidence invalid: {exc}", route)
