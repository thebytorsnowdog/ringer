"""Executed billing boundary. Local fixtures need no account when billing is off.

Read-only account probes make no login changes, model calls or route switches.
Account replies are reduced to auth/plan/quota before crossing this boundary.
"""
from __future__ import annotations

import asyncio
import contextlib
import http.client
import json
import math
import os
import signal
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from engines.gemini_api_worker import parse_args as parse_gemini_api_args
from ringer_capabilities import OPENROUTER_NOT_SELECTED, normalize_gemini_api_model, subscription_probe
from ringer_costs import (
    DEFAULT_LEDGER_FILE, GEMINI_API_RATES, GEMINI_CREDIT_LEDGER_FILE, SpendLedger,
    billing_preflight, estimate_event, inspect_usage_snapshot, parse_usage_lines, validate_cost_policy,
)
from ringer_delegation import catalogue_usd, prepare_openrouter_dispatch

GEMINI_QUOTA_URL = "https://aicode.googleapis.com/v1alpha:fetchQuotaStatus"


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


class GeminiQuotaEvidenceError(AccountEvidenceError):
    pass


def codex_subscription_env() -> dict[str, str]:
    # Workload identity is selected before Codex applies its login-method
    # restriction. Keep native ChatGPT auth and exclude alternate API inputs.
    api_auth = {
        "OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_FEDERATION_RULE_ID",
        "OPENAI_IDENTITY_TOKEN_FILE", "OPENAI_IDENTITY_TOKEN_SOURCE",
        "OPENAI_SPIFFE_AUDIENCE", "OPENAI_SPIFFE_ID",
    }
    return {name: value for name, value in os.environ.items() if name not in api_auth}


async def _close_server(proc: asyncio.subprocess.Process) -> None:
    if proc.stdin is not None:
        proc.stdin.close()
    # Kill the whole group even when the parent has already exited.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(proc.wait(), 1)


async def observe_codex_account(binary: str, cwd: Path, *, timeout_s: float = 10,
                                require_quota: bool = True,
                                env: dict[str, str] | None = None) -> dict[str, Any]:
    """Read app-server account/limits only, bounded including notifications/EOF.

    The deadline covers launch and all replies; unrelated notifications and
    out-of-order replies are tolerated. Never retain raw account/error payloads.
    """
    proc = None
    async def exchange() -> dict[str, Any]:
        nonlocal proc
        proc = await asyncio.create_subprocess_exec(
            binary, "app-server", "--stdio", cwd=str(cwd), env=env,
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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _gemini_reset_epoch(value: Any, window: str) -> int:
    if isinstance(value, str):
        with contextlib.suppress(ValueError, OverflowError, OSError):
            reset = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            if reset.utcoffset() is not None and (epoch := reset.timestamp()) >= 0:
                return int(epoch)
    raise GeminiQuotaEvidenceError(f"Gemini {window} quota resetTime must be a valid ISO timestamp with timezone")


def parse_gemini_quota_status(payload: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """Reduce fetchQuotaStatus to fresh, sanitised 5-hour and weekly quota evidence."""
    if not isinstance(payload, dict) or "error" in payload:
        raise GeminiQuotaEvidenceError("Gemini quota response must be an object without an error")
    summary = payload.get("quotaSummary")
    groups = summary.get("groups") if isinstance(summary, dict) else None
    if not isinstance(groups, list) or not groups:
        raise GeminiQuotaEvidenceError("Gemini quotaSummary.groups must be a non-empty list")

    by_id = {"gemini-5h": "primary", "gemini-weekly": "secondary"}
    by_label = {"5h": "primary", "weekly": "secondary", "7d": "secondary"}
    gemini_groups = []
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("displayName", ""), str):
            raise GeminiQuotaEvidenceError("Gemini quota group or displayName is invalid")
        buckets = group.get("buckets", [])
        if not isinstance(buckets, list) or any(not isinstance(bucket, dict) for bucket in buckets):
            raise GeminiQuotaEvidenceError("Gemini quota group buckets must be a list of objects")
        if "gemini" in group.get("displayName", "").lower() or any(
            isinstance(bucket.get("bucketId"), str) and bucket["bucketId"].strip().lower() in by_id
            for bucket in buckets
        ):
            gemini_groups.append(buckets)
    if len(gemini_groups) != 1:
        raise GeminiQuotaEvidenceError("Gemini Models quota group is missing or ambiguous")

    windows = {"primary": ("5-hour", 300), "secondary": ("weekly", 10080)}
    limits = {}
    for bucket in gemini_groups[0]:
        bucket_id = bucket.get("bucketId", "")
        label = bucket.get("refreshWindowLabel", "")
        disabled = bucket.get("disabled", False)
        if not isinstance(bucket_id, str) or not isinstance(label, str) or type(disabled) is not bool:
            raise GeminiQuotaEvidenceError("Gemini quota bucket identifier, label or disabled flag is invalid")
        id_slot = by_id.get(bucket_id.strip().lower())
        label_slot = by_label.get(label.strip().lower())
        slot = id_slot or label_slot
        if slot is None:
            continue
        window, duration = windows[slot]
        if (id_slot and label_slot and id_slot != label_slot) or slot in limits:
            raise GeminiQuotaEvidenceError(f"Gemini {window} quota bucket is ambiguous")
        if disabled:
            raise GeminiQuotaEvidenceError(f"Gemini {window} quota bucket is disabled")
        reset = _gemini_reset_epoch(bucket.get("resetTime", bucket.get("reset_time")), window)
        remaining = bucket.get("remainingFraction", bucket.get("remaining_fraction"))
        if (
            type(remaining) not in (int, float, Decimal)
            or (isinstance(remaining, Decimal) and not remaining.is_finite())
            or not 0 <= remaining <= 1
        ):
            raise GeminiQuotaEvidenceError(f"Gemini {window} quota remainingFraction must be a number from 0 to 1")
        used = (1 - float(remaining)) * 100
        if remaining > 0:
            # Float subtraction can still round a positive balance to 100% used.
            used = min(used, math.nextafter(100.0, 0.0))
        limits[slot] = {"usedPercent": used, "windowDurationMins": duration, "resetsAt": reset}
    for slot, (window, _) in windows.items():
        if slot not in limits:
            raise GeminiQuotaEvidenceError(f"Gemini {window} quota bucket is missing")
    return {
        "observed_auth_mode": "antigravity",
        "plan_type": "antigravity",
        "observed_at": (now or datetime.now(timezone.utc)).isoformat(),
        "rateLimitsByLimitId": {"gemini": limits},
    }


def _read_antigravity_access_token(*, timeout_s: float) -> str:
    """Read the agy OAuth session from the OS keyring or its local token file."""
    lookup_cmd = (
        ["security", "find-generic-password", "-s", "gemini", "-a", "antigravity", "-w"]
        if sys.platform == "darwin"
        else ["secret-tool", "lookup", "service", "gemini", "username", "antigravity"]
    )
    data = None
    with contextlib.suppress(OSError, ValueError, RecursionError, subprocess.SubprocessError):
        raw = subprocess.check_output(
            lookup_cmd, text=True, stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=min(timeout_s, 3.0),
        )
        data = json.loads(raw)
    if not isinstance(data, dict):
        agy_dir = Path(os.environ.get("AGY_DIR") or (Path.home() / ".gemini" / "antigravity-cli"))
        with contextlib.suppress(OSError, ValueError, RecursionError):
            data = json.loads((agy_dir / "antigravity-oauth-token").read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise AccountEvidenceError("Antigravity keyring or local OAuth login is missing or locked")
    token = data.get("access_token")
    nested = data.get("token")
    if isinstance(nested, dict):
        token = nested.get("access_token", token)
    token = token.strip() if isinstance(token, str) else ""
    if not token or any(c.isspace() for c in token):
        raise AccountEvidenceError("Antigravity OAuth access token is missing or invalid")
    return token


def _fetch_gemini_quota_sync(timeout_s: float) -> dict[str, Any]:
    token = _read_antigravity_access_token(timeout_s=timeout_s)
    request = urllib.request.Request(
        GEMINI_QUOTA_URL,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout_s) as response:
            if response.status != 200:
                raise GeminiQuotaEvidenceError(f"Gemini quota probe returned HTTP {response.status}")
            body = response.read(1_000_001)
    except urllib.error.HTTPError as exc:
        exc.close()
        raise GeminiQuotaEvidenceError(f"Gemini quota probe returned HTTP {exc.code}") from None
    if len(body) > 1_000_000:
        raise GeminiQuotaEvidenceError("Gemini quota response exceeded read limit")
    try:
        # Keep the wire value exact until the fraction is range-checked.
        payload = json.loads(body, parse_float=Decimal)
    except (ValueError, ArithmeticError, RecursionError):
        raise GeminiQuotaEvidenceError("Gemini quota response is malformed JSON") from None
    return parse_gemini_quota_status(payload)


async def observe_gemini_account(*, timeout_s: float = 10) -> dict[str, Any]:
    """Read the Antigravity subscription quota without retaining the token or raw reply."""
    try:
        return await asyncio.wait_for(asyncio.to_thread(_fetch_gemini_quota_sync, timeout_s), timeout_s)
    except asyncio.TimeoutError:
        raise GeminiQuotaEvidenceError("Gemini subscription quota probe timed out") from None
    except AccountEvidenceError:
        raise
    except (OSError, ValueError, http.client.HTTPException) as exc:
        raise GeminiQuotaEvidenceError(f"Gemini subscription quota unavailable ({type(exc).__name__})") from None


def enforce_codex_command(command: list[str], model: str | None, route: str, spec: str) -> list[str]:
    """Reject ambiguous routing, then force the required auth on the actual argv.

    Require a separate final prompt and ordinary exec for assessed billing.
    The legacy subscription path can also use the Codex root command.
    No sandbox/permission options are added, removed or relaxed.
    """
    legacy_subscription = model is None and route == "subscription"
    if not model and not legacy_subscription:
        raise ValueError("billing requires an explicit task model")
    is_exec = len(command) >= 3 and command[1] == "exec"
    if len(command) < 2 or command[-1] != spec or not (is_exec or legacy_subscription):
        raise ValueError("billing requires Codex exec with a separate final {spec}")
    args = command[2 if is_exec else 1:-1]
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
            config = arg[2:].removeprefix("=")
        if config is not None:
            key, _, value = config.partition("=")
            key = key.strip().strip('"\'')
            if key == "forced_login_method":
                if value.strip().strip('"\'') != expected_auth:
                    raise ValueError("billing rejects conflicting forced_login_method")
            elif key == "model" or key.startswith(("model_provider", "model_providers", "profile")):
                raise ValueError("billing rejects model/provider configuration overrides")
    if model is not None and models != [model]:
        raise ValueError("billing requires exactly one forwarded -m/--model matching the task model")
    # Put enforced overrides after the configured args, before the final prompt.
    return command[:-1] + ["-c", f'forced_login_method="{expected_auth}"',
                           "-c", 'model_provider="openai"'] + (
                               ["--", spec] if not is_exec or spec.startswith("-") else [spec])


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
        if arg in {"--variant", "--dir"} or arg.startswith(("--variant=", "--dir=")):
            raise ValueError(
                "OpenCode 2.2.6 run does not accept --variant or --dir; "
                "use --standalone and the process cwd as the task directory"
            )
        if arg in {"-c", "--config", "-p", "--provider", "--base-url", "--api-key", "--auth"} or arg.startswith(
            ("--config=", "--provider=", "--model-provider=", "--base-url=", "--api-key=", "--auth=")):
            raise ValueError("billing rejects ambiguous OpenCode custom routing arguments")
    if any("#" in item for item in models):
        raise ValueError(
            "OpenCode 2.2.6 documents -m provider/model#variant, and no variant name is confirmed"
        )
    if models != [model]:
        raise ValueError("billing requires exactly one forwarded OpenRouter model matching the task model")
    if "--standalone" not in args:
        raise ValueError(
            "OpenCode 2.2.6 must include --standalone so its server stays inside the sandbox process"
        )
    return command


def _flag_values(command: list[str], flag: str) -> list[str]:
    return [command[index + 1] for index, arg in enumerate(command) if arg == flag and index + 1 < len(command)]


def _command_token_bounds(command: list[str]) -> tuple[int | None, int | None]:
    def one(flag: str) -> int | None:
        values = _flag_values(command, flag)
        if len(values) != 1 or not values[0].isdigit() or int(values[0]) <= 0:
            return None
        return int(values[0])
    return one("--max-input-tokens"), one("--max-tokens")


def enforce_openrouter_command(command: list[str], model: str, spec: str) -> list[str]:
    """Accept one bounded OpenRouter completion. The worker must not be OpenCode."""
    if not model.startswith("openrouter/") or "/" not in model.removeprefix("openrouter/"):
        raise ValueError("paid OpenRouter requires an explicit openrouter/provider/model")
    if not command or command[-1] != spec:
        raise ValueError("billing requires the OpenRouter spec as the final argument")
    if any(arg in {"--api-key", "--auth"} or arg.startswith(("--api-key=", "--auth=")) for arg in command):
        raise ValueError("billing rejects an API key on the OpenRouter command")
    models = _flag_values(command, "-m") + _flag_values(command, "--model")
    if models != [model] or "#" in model:
        raise ValueError("billing requires exactly one OpenRouter model matching the task model")
    if _command_token_bounds(command)[0] is None or _command_token_bounds(command)[1] is None:
        raise ValueError("the OpenRouter single request must name --max-input-tokens and --max-tokens")
    return command


def enforce_gemini_api_command(command: list[str], model: str, spec: str, *,
                               taskdir: Path, full_access: bool) -> None:
    if not command or command[-1] != spec:
        raise ValueError("billing requires the Gemini API spec as the final argument")
    cwd, forwarded_model, forwarded_spec, wrapper = parse_gemini_api_args(command[1:])
    canonical = normalize_gemini_api_model(model)
    if forwarded_spec != spec or normalize_gemini_api_model(forwarded_model) != canonical:
        raise ValueError("billing requires the forwarded Gemini API model and spec to match the task")
    if canonical not in GEMINI_API_RATES:
        raise ValueError("Gemini AI Studio model has no dated pricing")
    if cwd and (taskdir / cwd).resolve() != taskdir.resolve():
        raise ValueError("Gemini AI Studio worker must run in the task directory")
    if Path(command[0]).name == "gemini_api_worker.py" and not full_access and not wrapper:
        raise ValueError("restricted Gemini AI Studio worker requires --sandbox-wrapper")


def gemini_fallback_enabled(policy: dict[str, Any]) -> bool:
    return (
        policy.get("gemini_credit_fallback") is True
        and policy.get("automatic_paid_fallback") is True
        and "fx" in policy
        and Decimal(policy.get("monthly_gemini_api_cap_usd", "0")) > 0
    )


def _confirmed_gemini_exhaustion(snapshot: dict[str, Any], max_age_seconds: int) -> bool:
    # Inspection can stop at the first exhausted window. Reuse its validation
    # with exhausted windows masked to confirm that all other evidence is valid.
    limits = snapshot["rateLimitsByLimitId"]["gemini"]
    masked = {**limits}
    exhausted = False
    for name in ("primary", "secondary"):
        window = limits.get(name)
        if isinstance(window, dict) and window.get("usedPercent") == 100:
            masked[name] = {**window, "usedPercent": 99}
            exhausted = True
    return exhausted and inspect_usage_snapshot(
        {**snapshot, "rateLimitsByLimitId": {"gemini": masked}},
        max_age_seconds=max_age_seconds, limit_key="gemini",
    ).available


@dataclass
class Admission:
    allowed: bool
    evidence: dict[str, Any]
    policy: dict[str, Any] | None = None
    openrouter_bounds: tuple[int, int] | None = None
    ledger_filename: str = DEFAULT_LEDGER_FILE


async def admit(*, policy_path: Path, engine: str, binary: str, model: str,
                route: str | None, allowance: Any, command: list[str], spec: str,
                taskdir: Path, state_dir: Path, run_id: str, task_key: str,
                attempt: int, timeout_s: float = 10, task_class: str = "",
                gemini_fallback: bool = False, full_access: bool = False) -> tuple[Admission, list[str]]:
    evidence = dict(billing_route=route, observed_auth_mode=None, quota_observed_at=None,
                    reservation_id=None, reservation_status="not_reserved", billing_status="BLOCKED",
                    billing_attempt_index=attempt, usage_coverage="unknown", cost_coverage="unknown")
    def blocked(reason: str, kind: str = "unknown") -> tuple[Admission, list[str]]:
        evidence.update(billing_reason=reason, billing_failure_class=kind)
        return Admission(False, evidence), command
    try:
        openrouter_bounds = None
        ledger_filename = DEFAULT_LEDGER_FILE
        # Re-read policy for each attempt, so a reduced cap takes effect on retry.
        policy = validate_cost_policy(json.loads(policy_path.read_text(encoding="utf-8")))
        if route is None:
            reason = "billing requires an explicit billing_route"
            if engine == "gemini":
                reason += ". " + OPENROUTER_NOT_SELECTED
            return blocked(reason, "permission")
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
        elif engine == "openrouter":
            if route != "api":
                return blocked("OpenRouter requires an explicit API billing route", "permission")
            try:
                command = enforce_openrouter_command(command, model, spec)
            except ValueError as exc:
                return blocked(str(exc), "permission")
            input_bound, output_bound = _command_token_bounds(command)
            assert input_bound is not None and output_bound is not None
            openrouter_bounds = (input_bound, output_bound)
        elif engine == "gemini-api":
            if route != "api" or task_class in {"coordination", "consequential"}:
                return blocked("Gemini AI Studio requires an explicit API route on an eligible task", "permission")
            if gemini_fallback and not gemini_fallback_enabled(policy):
                return blocked("Gemini AI Studio fallback requires both paid fallback flags and a monthly credit cap", "permission")
            try:
                enforce_gemini_api_command(command, model, spec, taskdir=taskdir, full_access=full_access)
            except ValueError as exc:
                return blocked(str(exc), "permission")
            ledger_filename = GEMINI_CREDIT_LEDGER_FILE
        elif engine == "claude":
            return blocked(
                "no Claude subscription is available. " + OPENROUTER_NOT_SELECTED,
                "permission",
            )
        elif engine == "grok":
            probe = subscription_probe(engine)
            if route == "api":
                return blocked(
                    f"{engine} has no API-key route. A CLI name is not subscription evidence. " + probe.reason,
                    "permission",
                )
            return blocked(probe.reason, "permission")
        elif engine == "gemini":
            if route != "subscription":
                return blocked("gemini has no API-key route. " + OPENROUTER_NOT_SELECTED, "permission")
            if "antigravity" not in policy["subscription_auth_modes"]:
                return blocked(
                    "Antigravity keyring OAuth must be enabled in subscription_auth_modes. " + OPENROUTER_NOT_SELECTED,
                    "permission",
                )
        else:
            return blocked("billing auth evidence unavailable for this engine", "permission")
        if route == "api" and engine != "gemini-api" and "run_api_cap_gbp" not in policy:
            return blocked("paid API requires an explicit run_api_cap_gbp", "unknown")
        if engine in {"opencode", "openrouter"}:
            bounds = openrouter_bounds or (None, None)
            planned = prepare_openrouter_dispatch(
                model=model, allowance=allowance, attempt=attempt, policy=policy,
                input_tokens=bounds[0], output_tokens=bounds[1], engine=engine,
                task_class=task_class,
            )
            evidence.update(planned.evidence())
            if not planned.allowed or planned.policy is None or planned.reserve_gbp is None:
                return blocked(planned.reason, planned.failure_class)
            allowance = planned.reserve_gbp
            policy = planned.policy
            # An OpenRouter route is always chargeable.  Do not read, infer or
            # alter credentials while deciding whether its local budget permits
            # dispatch.
            snapshot = {"observed_auth_mode": None, "plan_type": None,
                        "observed_at": None, "rateLimitsByLimitId": {}}
        elif engine == "gemini-api":
            # Budget admission does not access or attest credentials.
            snapshot = {"observed_auth_mode": None, "observed_at": None,
                        "plan_type": None, "rateLimitsByLimitId": {}}
        elif engine == "gemini":
            snapshot = await observe_gemini_account(timeout_s=timeout_s)
        else:
            snapshot = await observe_codex_account(binary, taskdir, timeout_s=timeout_s,
                                                   require_quota=route == "subscription",
                                                   env=codex_subscription_env() if route == "subscription" else None)
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
            if (
                engine == "gemini"
                and reason == "subscription quota exhausted; no automatic route switch"
                and gemini_fallback_enabled(policy)
                and _confirmed_gemini_exhaustion(snapshot, policy["snapshot_max_age_seconds"])
            ):
                evidence["gemini_quota_exhausted"] = True
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
        return Admission(True, evidence, policy, openrouter_bounds, ledger_filename), command
    except GeminiQuotaEvidenceError as exc:
        return blocked(str(exc), "quota")
    except AccountEvidenceError as exc:
        return blocked(str(exc), "permission")
    except (ValueError, TypeError, OSError) as exc:
        return blocked(f"billing evidence invalid: {exc}")


def usage_evidence(text: str, model: str, *, complete: bool, policy: dict[str, Any] | None = None,
                   service_tier: str | None = None,
                   openrouter_bounds: tuple[int, int] | None = None,
                   gemini_api: bool = False) -> dict[str, Any]:
    """Only usage events, never prompts or full log payloads, enter billing state."""
    if gemini_api:
        model = normalize_gemini_api_model(model)
    try:
        events = parse_usage_lines(text.splitlines(), expected_model=model, service_tier=service_tier)
    except ValueError as exc:
        return dict(usage_coverage="unknown", cost_coverage="unknown", usage_error=str(exc), actual_cost_usd=None)
    recognised = [e for e in events if e.source in {"turn.completed", "step_finish", "step-finish", "codex-text-total"}]
    # This route admits a single request with trusted bounds from the final
    # command. The worker can report a provider response and still exit with
    # an error, for example when the response exceeded those bounds.
    openrouter_response = None
    if openrouter_bounds is not None and len(events) == 1:
        event = events[0]
        if event.source in {"turn.completed", "error"}:
            openrouter_response = event
            recognised = events
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
    provider_response_received = openrouter_response is not None and openrouter_response.provider == "openrouter"
    actual = valid and (complete or provider_response_received) and all(
        e.actual_cost_usd is not None for e in events)
    openrouter_evidence = {}
    if openrouter_response is not None:
        input_bound, output_bound = openrouter_bounds
        inp, out = openrouter_response.input_tokens, openrouter_response.output_tokens
        estimate = None
        if inp is not None and out is not None:
            # The catalogue is optional comparison data once a call returned.
            with contextlib.suppress(OSError, ValueError, TypeError, AttributeError, ArithmeticError):
                estimate = catalogue_usd(model, inp, out)
        openrouter_evidence = dict(
            token_bound_exceeded=(inp is not None and inp > input_bound)
                                 or (out is not None and out > output_bound),
            catalogue_estimate_usd=estimate,
        )
    # A wrapper can emit the legacy total after structured completions. Prefer
    # their counters rather than adding the summary to the same usage again.
    token_events = [e for e in recognised if e.source != "codex-text-total"] or recognised
    totals = [e.input_tokens + e.output_tokens if e.input_tokens is not None and e.output_tokens is not None
              else e.total_tokens for e in token_events]
    known_total = bool(totals) and all(t is not None for t in totals)
    cost_evidence = {}
    if gemini_api:
        estimates = [estimate_event(e, prefer_actual=False) for e in events]
        if complete and detailed and all(
            e.model == model
            and quote.coverage == "gemini-api-standard" and quote.amount is not None
            for e, quote in zip(events, estimates)
        ):
            cost_evidence["estimated_cost_usd"] = str(sum(quote.amount for quote in estimates))
    else:
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
                thread_id=thread_id, **openrouter_evidence, **cost_evidence)


def settle_admission(admission: Admission, state_dir: Path, usage: dict[str, Any]) -> None:
    evidence = admission.evidence
    evidence.update(usage)
    identity = evidence.get("reservation_id")
    if identity is None:
        return
    evidence["reservation_status"] = "unresolved"
    ledger = SpendLedger(
        state_dir, filename=admission.ledger_filename,
        missing="fail" if admission.ledger_filename == GEMINI_CREDIT_LEDGER_FILE else "empty",
    )
    if admission.openrouter_bounds is not None and usage.get("token_bound_exceeded") is True:
        if not ledger.record_token_overrun(identity):
            raise ValueError("could not record a token overrun for the started reservation")
    fx = (admission.policy or {}).get("fx")
    if admission.ledger_filename == GEMINI_CREDIT_LEDGER_FILE:
        estimated_usd = usage.get("estimated_cost_usd")
        if estimated_usd is not None and fx:
            estimated_gbp = Decimal(estimated_usd) * Decimal(fx["usd_to_gbp"])
            source = f"Gemini AI Studio dated pricing estimate; FX {fx['dated']} {fx.get('source', 'policy')}"
            if ledger.settle(identity, estimated_gbp, source=source):
                evidence.update(reservation_status="settled", cost_coverage="dated_estimate",
                                actual_cost_usd=None, estimated_cost_gbp=str(estimated_gbp), fx=fx)
        return
    if usage.get("cost_coverage") == "provider_actual" and usage.get("actual_cost_usd") is not None and fx:
        try:
            actual = Decimal(usage["actual_cost_usd"]) * Decimal(fx["usd_to_gbp"])
        except ArithmeticError:
            evidence["settlement_error"] = "reported provider charge cannot be converted to GBP"
            return
        source = f"provider-reported USD; FX {fx['dated']} {fx.get('source', 'policy')}"
        if admission.openrouter_bounds is not None:
            source = f"OpenRouter response usage.cost; {source}"
        if ledger.settle(identity, actual, source=source):
            evidence.update(reservation_status="settled", actual_cost_gbp=str(actual), fx=fx)
