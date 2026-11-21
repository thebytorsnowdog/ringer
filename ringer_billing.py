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
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from ringer_capabilities import OPENROUTER_NOT_SELECTED, normalize_gemini_api_model, subscription_probe
from ringer_costs import (
    GEMINI_CREDIT_LEDGER_FILE,
    SpendLedger,
    billing_preflight,
    estimate_event,
    parse_usage_lines,
    validate_cost_policy,
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
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


def _parse_iso_epoch(value: Any) -> int | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def parse_gemini_quota_status(payload: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """Reduce aicode.googleapis.com/v1alpha:fetchQuotaStatus into a sanitised snapshot.

    Extracts only the Gemini Models group (5-hour primary and 7-day secondary
    windows). Handles Proto3 zero-omission where an exhausted bucket omits
    remainingFraction while still supplying resetTime.
    """
    if not isinstance(payload, dict) or "error" in payload:
        raise AccountEvidenceError("Gemini quota status payload is missing or errored")
    summary = payload.get("quotaSummary")
    if not isinstance(summary, dict):
        raise AccountEvidenceError("Gemini quotaSummary is missing")
    raw_groups = summary.get("groups")
    if not isinstance(raw_groups, list) or not raw_groups:
        raise AccountEvidenceError("Gemini quotaSummary groups are missing")
    groups = [g for g in raw_groups if isinstance(g, dict)]
    gemini_group = next(
        (g for g in groups if "gemini" in str(g.get("displayName") or "").lower()),
        None,
    )
    if gemini_group is None:
        for group in groups:
            buckets = group.get("buckets")
            if isinstance(buckets, list) and any(
                isinstance(b, dict) and str(b.get("bucketId") or "").lower().startswith("gemini-")
                for b in buckets
            ):
                gemini_group = group
                break
    if gemini_group is None or not isinstance(gemini_group.get("buckets"), list):
        raise AccountEvidenceError("Gemini Models quota group is missing")

    safe_limit: dict[str, Any] = {}
    for bucket in gemini_group["buckets"]:
        if not isinstance(bucket, dict) or bucket.get("disabled") is True:
            continue
        bucket_id = str(bucket.get("bucketId") or "").strip().lower()
        window_label = str(bucket.get("refreshWindowLabel") or "").strip().lower()
        if bucket_id == "gemini-5h" or window_label == "5h":
            slot, duration = "primary", 300
        elif bucket_id == "gemini-weekly" or window_label in {"weekly", "7d"}:
            slot, duration = "secondary", 10080
        else:
            continue
        reset_ts = _parse_iso_epoch(bucket.get("resetTime") or bucket.get("reset_time"))
        if reset_ts is None:
            raise AccountEvidenceError(f"Gemini {slot} quota resetTime is missing or invalid")
        remaining = bucket.get("remainingFraction", bucket.get("remaining_fraction"))
        if remaining is None:
            # Proto3 JSON omits zero-valued float fields; resetTime with no remainingFraction means 100% used.
            used_percent = 100.0
        elif isinstance(remaining, bool) or not isinstance(remaining, (int, float)) or not math.isfinite(remaining):
            raise AccountEvidenceError(f"Gemini {slot} remainingFraction is invalid")
        else:
            used_percent = round(max(0.0, min(1.0, 1.0 - float(remaining))) * 100.0, 2)
        safe_limit[slot] = {
            "usedPercent": used_percent,
            "windowDurationMins": duration,
            "resetsAt": reset_ts,
        }
    if "primary" not in safe_limit:
        raise AccountEvidenceError("Gemini 5-hour primary quota bucket is missing")
    current = now or datetime.now(timezone.utc)
    return {
        "observed_auth_mode": "antigravity",
        "plan_type": "antigravity",
        "observed_at": current.isoformat(),
        "rateLimitsByLimitId": {"gemini": safe_limit},
    }


def _read_antigravity_access_token(*, timeout_s: float = 3.0) -> str:
    """Read Antigravity OAuth access token from OS keyring/Keychain or oauth-token file.

    Never reads GEMINI_API_KEY or GOOGLE_API_KEY, and never logs or retains the token.
    """
    data = None
    lookup_cmd = (
        ["security", "find-generic-password", "-s", "gemini", "-a", "antigravity", "-w"]
        if sys.platform == "darwin"
        else ["secret-tool", "lookup", "service", "gemini", "username", "antigravity"]
    )
    try:
        raw = subprocess.check_output(
            lookup_cmd,
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=min(timeout_s, 3.0),
        )
        if raw.strip():
            data = json.loads(raw)
    except Exception:
        data = None
    if not isinstance(data, dict):
        agy_dir = Path(os.environ.get("AGY_DIR") or (Path.home() / ".gemini" / "antigravity-cli"))
        token_path = agy_dir / "antigravity-oauth-token"
        try:
            if token_path.is_file():
                data = json.loads(token_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
    if not isinstance(data, dict):
        raise AccountEvidenceError("Antigravity keyring OAuth login missing or locked")
    token_obj = data.get("token") if isinstance(data.get("token"), dict) else {}
    access = str(token_obj.get("access_token") or data.get("access_token") or "").strip()
    if not access:
        raise AccountEvidenceError("Antigravity keyring OAuth access token is missing")
    return access


def _fetch_gemini_quota_sync(timeout_s: float, url: str = GEMINI_QUOTA_URL) -> dict[str, Any]:
    token = _read_antigravity_access_token(timeout_s=timeout_s)
    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
    )
    opener = urllib.request.build_opener(_NoRedirect)
    with opener.open(request, timeout=timeout_s) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return parse_gemini_quota_status(payload)


async def observe_gemini_account(*, timeout_s: float = 10, url: str = GEMINI_QUOTA_URL) -> dict[str, Any]:
    """Read sanitised Antigravity 5-hour and 7-day quota windows via fetchQuotaStatus."""
    try:
        return await asyncio.wait_for(asyncio.to_thread(_fetch_gemini_quota_sync, timeout_s, url), timeout_s)
    except asyncio.TimeoutError:
        raise AccountEvidenceError("Gemini subscription quota probe timed out") from None
    except AccountEvidenceError:
        raise
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise AccountEvidenceError(f"Gemini subscription quota unavailable ({type(exc).__name__})") from None


def enforce_gemini_api_command(command: list[str], model: str, spec: str) -> list[str]:
    """Require a Google Gemini model and no inline API credentials on argv."""
    canonical = normalize_gemini_api_model(model)
    if not canonical.startswith("google/gemini-"):
        raise ValueError("Gemini AI Studio API requires a google/gemini-* model")
    if not command or command[-1] != spec:
        raise ValueError("billing requires the Gemini API spec as the final argument")
    if any(arg in {"--api-key", "--auth"} or arg.startswith(("--api-key=", "--auth=")) for arg in command):
        raise ValueError("billing rejects an API key on the Gemini API command")
    models = _flag_values(command, "-m") + _flag_values(command, "--model")
    if len(models) != 1:
        raise ValueError("billing requires exactly one forwarded Gemini API model")
    if normalize_gemini_api_model(models[0]) != canonical:
        raise ValueError("billing requires the forwarded Gemini API model to match the task model")
    return command


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


@dataclass
class Admission:
    allowed: bool
    evidence: dict[str, Any]
    policy: dict[str, Any] | None = None
    openrouter_bounds: tuple[int, int] | None = None
    ledger_filename: str = "cost-reservations.json"


async def admit(*, policy_path: Path, engine: str, binary: str, model: str,
                route: str | None, allowance: Any, command: list[str], spec: str,
                taskdir: Path, state_dir: Path, run_id: str, task_key: str,
                attempt: int, timeout_s: float = 10, task_class: str = "") -> tuple[Admission, list[str]]:
    evidence = dict(billing_route=route, observed_auth_mode=None, quota_observed_at=None,
                    reservation_id=None, reservation_status="not_reserved", billing_status="BLOCKED",
                    billing_attempt_index=attempt, usage_coverage="unknown", cost_coverage="unknown")
    def blocked(reason: str, kind: str = "unknown") -> tuple[Admission, list[str]]:
        evidence.update(billing_reason=reason, billing_failure_class=kind)
        return Admission(False, evidence), command
    try:
        openrouter_bounds = None
        ledger_filename = "cost-reservations.json"
        # Re-read policy for each attempt, so a reduced cap takes effect on retry.
        policy = validate_cost_policy(json.loads(policy_path.read_text(encoding="utf-8")))
        if route is None:
            return blocked("billing requires an explicit billing_route. " + OPENROUTER_NOT_SELECTED, "permission")
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
            if route != "api":
                return blocked("Gemini AI Studio API requires an explicit API billing route", "permission")
            try:
                command = enforce_gemini_api_command(command, model, spec)
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
            probe = subscription_probe(engine)
            if route == "api":
                return blocked(
                    f"{engine} has no API-key route. Use gemini-api for AI Studio credits. " + probe.reason,
                    "permission",
                )
            if "antigravity" not in policy["subscription_auth_modes"]:
                return blocked(
                    "Antigravity keyring subscription auth is not enabled in subscription_auth_modes. "
                    + probe.reason + " " + OPENROUTER_NOT_SELECTED,
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
            if "fx" not in policy:
                return blocked("Gemini AI Studio API cap requires fx usd_to_gbp in cost policy", "permission")
            monthly_usd = Decimal(policy.get("monthly_gemini_api_cap_usd", "0"))
            if monthly_usd <= 0:
                return blocked("monthly_gemini_api_cap_usd must be positive for Gemini AI Studio API dispatch", "permission")
            fx_rate = Decimal(policy["fx"]["usd_to_gbp"])
            if allowance is None:
                task_usd = Decimal(policy.get("task_gemini_api_cap_usd", str(min(Decimal("2"), monthly_usd))))
                allowance = str(task_usd * fx_rate)
            snapshot = {"observed_auth_mode": "gemini_api_key", "plan_type": "ai_studio_credits",
                        "observed_at": datetime.now(timezone.utc).isoformat(),
                        "rateLimitsByLimitId": {}}
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
            kind = ("permission" if "auth" in reason or "route" in reason and "quota" not in reason
                    else "quota" if "quota" in reason or "primary" in reason or "usedPercent" in reason
                    else "unknown")
            # Budget has no class in the reliability taxonomy. Preserve the reason.
            return blocked(reason, kind)
        if decision.route != route:
            return blocked("billing route differs from explicit request", "permission")
        evidence.update(billing_status="ADMITTED", billing_reason=decision.reason,
                        reservation_id=decision.reservation_id,
                        reservation_status="reserved" if decision.reservation_id else "not_applicable",
                        ledger_filename=ledger_filename)
        return Admission(True, evidence, policy, openrouter_bounds, ledger_filename=ledger_filename), command
    except AccountEvidenceError as exc:
        return blocked(str(exc), "permission")
    except (ValueError, TypeError, OSError) as exc:
        return blocked(f"billing evidence invalid: {exc}")


def usage_evidence(text: str, model: str, *, complete: bool, policy: dict[str, Any] | None = None,
                   service_tier: str | None = None,
                   openrouter_bounds: tuple[int, int] | None = None) -> dict[str, Any]:
    """Only usage events, never prompts or full log payloads, enter billing state."""
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
                thread_id=thread_id, **openrouter_evidence)


def settle_admission(admission: Admission, state_dir: Path, usage: dict[str, Any]) -> None:
    evidence = admission.evidence
    evidence.update(usage)
    identity = evidence.get("reservation_id")
    if identity is None:
        return
    evidence["reservation_status"] = "unresolved"
    ledger = SpendLedger(state_dir, filename=admission.ledger_filename)
    if admission.openrouter_bounds is not None and usage.get("token_bound_exceeded") is True:
        if not ledger.record_token_overrun(identity):
            raise ValueError("could not record a token overrun for the started reservation")
    fx = (admission.policy or {}).get("fx")
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
    elif admission.ledger_filename == GEMINI_CREDIT_LEDGER_FILE and fx:
        estimates = usage.get("cost_estimates") or []
        if (
            estimates
            and usage.get("usage_coverage") in {"input_cache_output", "input_output"}
            and all(
                isinstance(item, dict)
                and item.get("currency") == "USD"
                and item.get("amount") is not None
                and item.get("status") in {"actual", "estimate"}
                for item in estimates
            )
        ):
            try:
                est_usd = sum((Decimal(str(item["amount"])) for item in estimates), Decimal(0))
                actual = est_usd * Decimal(fx["usd_to_gbp"])
            except ArithmeticError:
                evidence["settlement_error"] = "Gemini API estimated charge cannot be converted to GBP"
                return
            source = f"Gemini AI Studio dated pricing; FX {fx['dated']} {fx.get('source', 'policy')}"
            if ledger.settle(identity, actual, source=source):
                evidence.update(
                    reservation_status="settled",
                    actual_cost_usd=str(est_usd),
                    actual_cost_gbp=str(actual),
                    fx=fx,
                )
