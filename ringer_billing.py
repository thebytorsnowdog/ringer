"""Executed billing boundary. Local fixtures need no account when billing is off.

No credential files, login changes, model calls or implicit route fallback here.
Account replies are reduced to auth/plan/quota before crossing this boundary.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import signal
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from ringer_costs import SpendLedger, billing_preflight, estimate_event, parse_usage_lines, validate_cost_policy


def task_billing_fields(obj: dict[str, Any], key: str) -> dict[str, Any]:
    route = obj.get("billing_route")
    if route is not None and (not isinstance(route, str) or route not in {"subscription", "api"}):
        raise ValueError(f"task {key}: billing_route must be subscription or api")
    allowance = obj.get("task_spend_allowance_gbp")
    if allowance is not None and (type(allowance) not in (int, float) or
                                  (type(allowance) is float and not math.isfinite(allowance)) or allowance <= 0):
        raise ValueError(f"task {key}: task_spend_allowance_gbp must be a finite positive number")
    return dict(billing_route=route, task_spend_allowance_gbp=allowance)


def billing_policy_path(raw: Any, config_path: Path) -> Path | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) - {"policy_path"}:
        raise ValueError("billing must be a table containing only policy_path")
    value = raw.get("policy_path")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("billing.policy_path must be a non-empty path")
    path = Path(value).expanduser()
    return (config_path.parent / path).resolve() if not path.is_absolute() else path


class AccountEvidenceError(ValueError):
    pass


async def _close_server(proc: asyncio.subprocess.Process) -> None:
    if proc.stdin is not None:
        proc.stdin.close()
    # Kill the whole group even when the parent has already exited.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(proc.wait(), 1)


async def observe_codex_account(binary: str, cwd: Path, *, timeout_s: float = 10,
                                require_quota: bool = True) -> dict[str, Any]:
    """Read app-server account/limits only, bounded including notifications/EOF.

    The deadline covers launch and all replies; unrelated notifications and
    out-of-order replies are tolerated. Never retain raw account/error payloads.
    """
    proc = None
    async def exchange() -> dict[str, Any]:
        nonlocal proc
        proc = await asyncio.create_subprocess_exec(
            binary, "app-server", "--stdio", cwd=str(cwd),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=True, limit=65536)
        pending: dict[int, dict[str, Any]] = {}
        consumed = 0
        async def send(value: dict[str, Any]) -> None:
            proc.stdin.write((json.dumps(value) + "\n").encode())
            await proc.stdin.drain()
        async def reply(identity: int) -> dict[str, Any]:
            nonlocal consumed
            while identity not in pending:
                line = await proc.stdout.readline()
                consumed += len(line)
                if not line:
                    raise AccountEvidenceError("account evidence EOF before reply")
                if consumed > 1_000_000:
                    raise AccountEvidenceError("account evidence exceeded read limit")
                try:
                    row = json.loads(line)
                except (ValueError, UnicodeError):
                    raise AccountEvidenceError("account evidence malformed JSON") from None
                if not isinstance(row, dict):
                    raise AccountEvidenceError("account evidence reply must be an object")
                rid = row.get("id")
                if type(rid) is int and rid in {1, 2, 3}:
                    if rid in pending:
                        raise AccountEvidenceError("account evidence duplicate reply")
                    pending[rid] = row
            row = pending.pop(identity)
            if "error" in row:
                error = row["error"]
                code = error.get("code") if isinstance(error, dict) else None
                suffix = f" (code {code})" if type(code) is int else ""
                raise AccountEvidenceError(f"account evidence RPC {identity} failed{suffix}")
            result = row.get("result")
            if not isinstance(result, dict):
                raise AccountEvidenceError("account evidence result must be an object")
            return result
        await send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "ringer", "version": "1.0"}}})
        await reply(1)
        await send({"method": "initialized"})
        await send({"id": 2, "method": "account/read", "params": {"refreshToken": False}})
        await send({"id": 3, "method": "account/rateLimits/read", "params": None})
        account_result = await reply(2)
        try:
            rate_result = await reply(3)
        except AccountEvidenceError:
            if require_quota:
                raise
            rate_result = {}
        account = account_result.get("account")
        if not isinstance(account, dict):
            raise AccountEvidenceError("account evidence account is missing")
        raw_auth = account.get("type")
        auth = {"chatgpt": "chatgpt", "apiKey": "api_key"}.get(raw_auth) if isinstance(raw_auth, str) else None
        if auth is None:
            raise AccountEvidenceError("account evidence auth type is unknown")
        limits = rate_result.get("rateLimitsByLimitId")
        codex = limits.get("codex") if isinstance(limits, dict) else None
        safe_limit = {}
        if isinstance(codex, dict):
            for name in ("primary", "secondary"):
                value = codex.get(name)
                safe_limit[name] = ({k: value.get(k) for k in ("usedPercent", "windowDurationMins", "resetsAt")}
                                    if isinstance(value, dict) else value)
            for name in ("spendControlReached", "rateLimitReachedType", "planType"):
                safe_limit[name] = codex.get(name)
            # These fields are admission evidence, not account identity.  Keep
            # their safe, small shape so ringer_costs can apply its conservative
            # guards.  In particular, an individual limit of any shape must not
            # silently become absence after sanitisation.
            if codex.get("individualLimit") is not None:
                safe_limit["individualLimit"] = {"present": True}
            if codex.get("credits") is not None:
                credits = codex.get("credits")
                if not isinstance(credits, dict):
                    safe_limit["credits"] = {}
                else:
                    balance = credits.get("balance")
                    try:
                        if isinstance(balance, bool):
                            raise ValueError
                        safe_balance = Decimal(str(balance))
                        if not safe_balance.is_finite() or safe_balance < 0:
                            raise ValueError
                    except (ValueError, ArithmeticError):
                        safe_balance = None
                    safe_limit["credits"] = {
                        "hasCredits": credits.get("hasCredits") if type(credits.get("hasCredits")) is bool else None,
                        "unlimited": credits.get("unlimited") if type(credits.get("unlimited")) is bool else None,
                        "balance": str(safe_balance) if safe_balance is not None else None,
                    }
        plan = account.get("planType")
        if plan is not None and not isinstance(plan, str):
            raise AccountEvidenceError("account evidence plan type is invalid")
        return {"observed_auth_mode": auth, "plan_type": plan,
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "rateLimitsByLimitId": {"codex": safe_limit}}
    try:
        return await asyncio.wait_for(exchange(), timeout_s)
    except asyncio.TimeoutError:
        raise AccountEvidenceError("account evidence timed out") from None
    except (OSError, ValueError) as exc:
        if isinstance(exc, AccountEvidenceError):
            raise
        # Exception messages can contain transport payloads. Keep only the class.
        raise AccountEvidenceError(f"account evidence unavailable ({type(exc).__name__})") from None
    finally:
        if proc is not None:
            await _close_server(proc)


def enforce_codex_command(command: list[str], model: str, route: str, spec: str) -> list[str]:
    """Reject ambiguous routing, then force observed auth on the actual argv.

    Require ordinary exec with a separate final prompt. No sandbox/permission
    options are added, removed or relaxed by this function.
    """
    if not model:
        raise ValueError("billing requires an explicit task model")
    if len(command) < 3 or command[1] != "exec" or command[-1] != spec:
        raise ValueError("billing requires Codex exec with a separate final {spec}")
    args = command[2:-1]
    if "--" in args or any(a in {"resume", "review", "--oss", "--local-provider", "--profile", "-p"} or a.startswith(("--profile=", "--local-provider=")) for a in args):
        raise ValueError("billing rejects alternative Codex routing/profile arguments")
    models = []
    # Installed app-server schema calls this enum "api"; observed account.type
    # is "apiKey" and the cost boundary normalises that to "api_key".
    expected_auth = "chatgpt" if route == "subscription" else "api"
    for index, arg in enumerate(args):
        if arg in {"-m", "--model"}:
            models.append(args[index + 1] if index + 1 < len(args) else "")
        elif arg.startswith("--model="):
            models.append(arg.partition("=")[2])
        elif arg.startswith("-m") and arg != "-m":
            models.append(arg[2:])
        config = None
        if arg in {"-c", "--config"}:
            config = args[index + 1] if index + 1 < len(args) else ""
        elif arg.startswith("--config="):
            config = arg.partition("=")[2]
        elif arg.startswith("-c") and arg != "-c":
            config = arg[2:]
        if config is not None:
            key, _, value = config.partition("=")
            key = key.strip().strip('"\'')
            if key == "forced_login_method":
                if value.strip().strip('"\'') != expected_auth:
                    raise ValueError("billing rejects conflicting forced_login_method")
            elif key == "model" or key.startswith(("model_provider", "model_providers", "profile")):
                raise ValueError("billing rejects model/provider configuration overrides")
    if models != [model]:
        raise ValueError("billing requires exactly one forwarded -m/--model matching the task model")
    # Put enforced overrides after the configured args, before the final prompt.
    return command[:-1] + ["-c", f'forced_login_method="{expected_auth}"',
                           "-c", 'model_provider="openai"'] + command[-1:]


def enforce_opencode_command(command: list[str], model: str, spec: str) -> list[str]:
    """Accept only an explicit OpenCode/OpenRouter model route.

    OpenCode API billing is deliberately credential-agnostic: its admission is
    an allowance reservation, not an account probe.  The actual argv must still
    prove one unambiguous OpenRouter model selection before it can spend.
    """
    if not model.startswith("openrouter/") or "/" not in model.removeprefix("openrouter/"):
        raise ValueError("paid OpenCode API requires an explicit openrouter/provider/model")
    if len(command) < 4 or command[-1] != spec or command[:-1].count("run") != 1:
        raise ValueError("billing requires OpenCode run with a separate final {spec}")
    args = command[command.index("run") + 1:-1]
    if "--" in args:
        raise ValueError("billing rejects ambiguous OpenCode custom arguments")
    models: list[str] = []
    for index, arg in enumerate(args):
        if arg in {"-m", "--model"}:
            models.append(args[index + 1] if index + 1 < len(args) else "")
        elif arg.startswith("--model="):
            models.append(arg.partition("=")[2])
        elif arg.startswith("-m") and arg != "-m":
            models.append(arg[2:])
        if arg in {"-c", "--config", "-p", "--provider", "--base-url", "--api-key", "--auth"} or arg.startswith(
            ("--config=", "--provider=", "--model-provider=", "--base-url=", "--api-key=", "--auth=")):
            raise ValueError("billing rejects ambiguous OpenCode custom routing arguments")
    if models != [model]:
        raise ValueError("billing requires exactly one forwarded OpenRouter model matching the task model")
    return command


@dataclass
class Admission:
    allowed: bool
    evidence: dict[str, Any]
    policy: dict[str, Any] | None = None


async def admit(*, policy_path: Path, engine: str, binary: str, model: str,
                route: str | None, allowance: Any, command: list[str], spec: str,
                taskdir: Path, state_dir: Path, run_id: str, task_key: str,
                attempt: int, timeout_s: float = 10) -> tuple[Admission, list[str]]:
    evidence = dict(billing_route=route, observed_auth_mode=None, quota_observed_at=None,
                    reservation_id=None, reservation_status="not_reserved", billing_status="BLOCKED",
                    billing_attempt_index=attempt, usage_coverage="unknown", cost_coverage="unknown")
    def blocked(reason: str, kind: str = "unknown") -> tuple[Admission, list[str]]:
        evidence.update(billing_reason=reason, billing_failure_class=kind)
        return Admission(False, evidence), command
    try:
        # Re-read policy for each attempt, so a reduced cap takes effect on retry.
        policy = validate_cost_policy(json.loads(policy_path.read_text(encoding="utf-8")))
        if route is None:
            return blocked("billing requires an explicit billing_route", "permission")
        if not model:
            return blocked("billing requires an explicit task model", "permission")
        if engine == "codex":
            try:
                command = enforce_codex_command(command, model, route, spec)
            except ValueError as exc:
                return blocked(str(exc), "permission")
        elif engine == "opencode":
            if route != "api":
                return blocked("OpenCode requires an explicit API billing route", "permission")
            try:
                command = enforce_opencode_command(command, model, spec)
            except ValueError as exc:
                return blocked(str(exc), "permission")
        else:
            return blocked("billing auth evidence unavailable for this engine", "permission")
        if route == "api" and "run_api_cap_gbp" not in policy:
            return blocked("paid API requires an explicit run_api_cap_gbp", "unknown")
        if engine == "opencode":
            # An OpenRouter route is always chargeable.  Do not read, infer or
            # alter credentials while deciding whether its local budget permits
            # dispatch.
            snapshot = {"observed_auth_mode": None, "plan_type": None,
                        "observed_at": None, "rateLimitsByLimitId": {}}
        else:
            snapshot = await observe_codex_account(binary, taskdir, timeout_s=timeout_s,
                                                   require_quota=route == "subscription")
        evidence.update(observed_auth_mode=snapshot["observed_auth_mode"],
                        quota_observed_at=snapshot["observed_at"], plan_type=snapshot["plan_type"],
                        quota=snapshot["rateLimitsByLimitId"])
        attempt_id = f"{run_id}/{task_key}/{attempt}"
        decision = billing_preflight(engine=engine, model=model, policy=policy,
            observed_auth_mode=snapshot["observed_auth_mode"], usage_snapshot=snapshot,
            task_spend_allowance_gbp=allowance, attempt_id=attempt_id, state_dir=state_dir,
            billing_route=route, run_id=run_id)
        if not decision.allow:
            reason = decision.reason
            kind = ("permission" if "auth" in reason or "route" in reason and "quota" not in reason
                    else "quota" if "quota" in reason or "primary" in reason or "usedPercent" in reason
                    else "unknown")
            # Budget has no class in the reliability taxonomy. Preserve the reason.
            return blocked(reason, kind)
        if decision.route != route:
            return blocked("billing route differs from explicit request", "permission")
        evidence.update(billing_status="ADMITTED", billing_reason=decision.reason,
                        reservation_id=decision.reservation_id,
                        reservation_status="reserved" if decision.reservation_id else "not_applicable")
        return Admission(True, evidence, policy), command
    except AccountEvidenceError as exc:
        return blocked(str(exc), "permission")
    except (ValueError, TypeError, OSError) as exc:
        return blocked(f"billing evidence invalid: {exc}")


def usage_evidence(text: str, model: str, *, complete: bool, policy: dict[str, Any] | None = None,
                   service_tier: str | None = None) -> dict[str, Any]:
    """Only usage events, never prompts or full log payloads, enter billing state."""
    try:
        events = parse_usage_lines(text.splitlines(), expected_model=model, service_tier=service_tier)
    except ValueError as exc:
        return dict(usage_coverage="unknown", cost_coverage="unknown", usage_error=str(exc), actual_cost_usd=None)
    recognised = [e for e in events if e.source in {"turn.completed", "step_finish", "step-finish", "codex-text-total"}]
    # Missing or malformed usage cannot become a settled zero.
    valid = bool(recognised) and len(recognised) == len(events) and all(not e.issue for e in events)
    detailed = valid and all(None not in (e.input_tokens, e.cached_input_tokens,
                                          e.cache_write_tokens, e.output_tokens) for e in events)
    input_output = valid and all(e.input_tokens is not None and e.output_tokens is not None for e in events)
    # A provider may exit zero after reporting a failed turn. Prior completed
    # turns do not prove the failed request's cost, so the total stays unknown.
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and (row.get("type") in {"error", "turn.failed"} or "error" in row):
            complete = False
    actual = valid and complete and all(e.actual_cost_usd is not None for e in events)
    # A wrapper can emit the legacy total after structured completions. Prefer
    # their counters rather than adding the summary to the same usage again.
    token_events = [e for e in recognised if e.source != "codex-text-total"] or recognised
    totals = [e.input_tokens + e.output_tokens if e.input_tokens is not None and e.output_tokens is not None
              else e.total_tokens for e in token_events]
    known_total = bool(totals) and all(t is not None for t in totals)
    estimates = [estimate_event(e, pricing=(policy or {}).get("pricing")) for e in events]
    thread_id = next((e.session_id for e in events if e.session_id), None)
    if thread_id is None:
        for line in text.splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("type") == "thread.started" and isinstance(row.get("thread_id"), str):
                thread_id = row["thread_id"]
                break
    serialise = lambda value: json.loads(json.dumps(value, default=str))
    return dict(usage_coverage="input_cache_output" if detailed else "input_output" if input_output else "total_only" if valid and known_total else "unknown",
                cost_coverage="provider_actual" if actual else "unknown",
                actual_cost_usd=str(sum((e.actual_cost_usd for e in events), Decimal(0))) if actual else None,
                service_tier=service_tier,
                usage_events=serialise([asdict(e) for e in events]),
                cost_estimates=serialise([asdict(e) for e in estimates]),
                worker_tokens=sum(totals) if known_total else None,
                thread_id=thread_id)


def settle_admission(admission: Admission, state_dir: Path, usage: dict[str, Any]) -> None:
    evidence = admission.evidence
    evidence.update(usage)
    identity = evidence.get("reservation_id")
    if identity is None:
        return
    evidence["reservation_status"] = "unresolved"
    fx = (admission.policy or {}).get("fx")
    if usage.get("actual_cost_usd") is not None and fx:
        actual = Decimal(usage["actual_cost_usd"]) * Decimal(fx["usd_to_gbp"])
        source = f"provider-reported USD; FX {fx['dated']} {fx.get('source', 'policy')}"
        if SpendLedger(state_dir).settle(identity, actual, source=source):
            evidence.update(reservation_status="settled", actual_cost_gbp=str(actual), fx=fx)
