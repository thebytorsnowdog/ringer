"""Capability checks for harness, provider, and billing route.

This module does not spawn processes, read credentials, reserve spend, or
rewrite a requested route into a different one. Callers use it at the
preflight boundary and when they still have to choose a route.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

OPENROUTER_ENGINES = frozenset({"opencode", "openrouter"})
NATIVE_ENGINES = ("codex", "grok", "gemini", "claude")

# These defaults are the ones the worker adapters already resolve. Tests keep
# them aligned with engines/gemini_worker.py and engines/openrouter_worker.py.
DEFAULT_AGY_BIN = "~/.local/share/mise/installs/antigravity-cli/latest/agy"
DEFAULT_CLAUDE_BIN = "~/.local/share/mise/installs/claude/latest/claude"

OPENROUTER_NOT_SELECTED = (
    "OpenRouter was not selected and no other harness was substituted."
)


@dataclass(frozen=True)
class HarnessCapability:
    engine: str
    providers: frozenset[str]
    subscription_auth: str | None
    api_auth: str | None
    note: str


@dataclass(frozen=True)
class SubscriptionProbe:
    """Whether admission can observe this harness's native subscription.

    ``supported`` is true only for a probe this repository actually runs.
    An engine name, a binary on PATH, or an API-key environment variable is
    not that probe.
    """

    engine: str
    supported: bool
    reason: str


def subscription_probe(engine: str) -> SubscriptionProbe:
    """Return the auth observation this process is allowed to perform.

    The function reads no environment variables and no credential files.
    """
    if engine == "codex":
        return SubscriptionProbe(
            "codex",
            True,
            "Codex subscription evidence is the app-server account/read reply "
            "with refreshToken false, reduced to auth type chatgpt and quota "
            "windows. An api_key reply is a different login and is not "
            "ChatGPT subscription credit.",
        )
    if engine == "grok":
        return SubscriptionProbe(
            "grok",
            False,
            "Grok subscription admission is not implemented. The published CLI "
            "reference lists grok login, grok logout, and grok models, and does "
            "not define a headless status that separates SuperGrok OAuth from an "
            "XAI_API_KEY session. A successful grok models exit and the "
            "XAI_API_KEY variable are not subscription evidence. "
            "~/.grok/auth.json is not read.",
        )
    if engine == "gemini":
        return SubscriptionProbe(
            "gemini",
            False,
            "Antigravity agy subscription admission is not implemented. "
            "Documented sign-in uses the OS keyring or an interactive browser "
            "flow, and the command list has no headless auth-status command. "
            "GEMINI_API_KEY and GOOGLE_API_KEY are not this subscription. "
            "The keyring is not read.",
        )
    if engine == "claude":
        return SubscriptionProbe(
            "claude",
            False,
            "Claude subscription admission is not implemented. The claude "
            "engine name is not observed subscription evidence.",
        )
    return SubscriptionProbe(
        engine,
        False,
        f"No subscription probe is implemented for {engine}.",
    )


# Auth names below are the ones this repository actually implements or documents.
# Codex subscription credits and OpenAI API credit balance are different accounts.
# Grok OAuth and Antigravity OAuth are not probes this billing layer can read.
HARNESSES: dict[str, HarnessCapability] = {
    "codex": HarnessCapability(
        engine="codex",
        providers=frozenset({"openai"}),
        subscription_auth="chatgpt",
        api_auth="api_key",
        note=(
            "Codex subscription is ChatGPT login (account type chatgpt, "
            "forced_login_method chatgpt). Codex API is a separate observed "
            "api_key login. Those credentials are not interchangeable, and "
            "neither one is an OpenRouter key."
        ),
    ),
    "grok": HarnessCapability(
        engine="grok",
        providers=frozenset({"xai"}),
        subscription_auth="grok_cli_oauth",
        api_auth=None,
        note=(
            "Grok's documented login is grok login on a SuperGrok or X Premium "
            "Plus plan. That OAuth session is not an OpenAI, Gemini, or "
            "OpenRouter credential. config.sample.toml records that Grok's JSON "
            "usage fields do not establish subscription coverage."
        ),
    ),
    "gemini": HarnessCapability(
        engine="gemini",
        providers=frozenset({"google"}),
        subscription_auth="agy_google_oauth",
        api_auth=None,
        note=(
            "Gemini subscription is the Antigravity CLI agy. "
            "engines/gemini_worker.py removes GEMINI_API_KEY and GOOGLE_API_KEY "
            "before exec. The gemini CLI is a different program."
        ),
    ),
    "claude": HarnessCapability(
        engine="claude",
        providers=frozenset({"anthropic"}),
        subscription_auth="claude_cli",
        api_auth=None,
        note=(
            "Claude is a native engine in this tree. This repository does not "
            "define an API-key route for it."
        ),
    ),
}


@dataclass(frozen=True)
class RouteDecision:
    status: str
    engine: str
    model: str
    billing_route: str | None
    provider: str
    reason: str

    @property
    def spends_openrouter(self) -> bool:
        return self.status == "allowed" and self.engine in OPENROUTER_ENGINES


def provider_for_model(model: str) -> str:
    text = (model or "").strip().lower()
    if not text:
        return "unspecified"
    if text.startswith("openrouter/"):
        return "openrouter"
    if text.startswith("grok"):
        return "xai"
    if text.startswith("gemini"):
        return "google"
    if text.startswith(("gpt-", "chatgpt-")) or text in {"gpt", "chatgpt"}:
        return "openai"
    if len(text) >= 2 and text[0] == "o" and text[1].isdigit():
        return "openai"
    if text.startswith("claude") or text in {"opus", "sonnet", "haiku"}:
        return "anthropic"
    return "unspecified"


def quota_stop_message(reason: str) -> str:
    return f"{reason}. {OPENROUTER_NOT_SELECTED}"


def _allowed(engine: str, model: str, billing_route: str | None, provider: str, reason: str) -> RouteDecision:
    return RouteDecision("allowed", engine, model, billing_route, provider, reason)


def _blocked(engine: str, model: str, billing_route: str | None, provider: str, reason: str) -> RouteDecision:
    return RouteDecision("blocked", engine, model, billing_route, provider, reason)


def assess_explicit_route(
    *,
    engine: str,
    model: str = "",
    billing_route: str | None = None,
    billing_policy_loaded: bool = False,
) -> RouteDecision:
    """Accept the named engine, or block it. Never returns a different engine."""
    provider = provider_for_model(model)
    if engine in OPENROUTER_ENGINES:
        if billing_route != "api":
            return _blocked(
                engine, model, billing_route, provider,
                "Explicit OpenRouter route requires billing_route api. "
                "A subscription label is refused, and the task stays on this engine. "
                + OPENROUTER_NOT_SELECTED,
            )
        if provider != "openrouter":
            return _blocked(
                engine, model, billing_route, provider,
                "Explicit OpenRouter route requires a model that starts with openrouter/.",
            )
        return _allowed(
            engine, model, "api", provider,
            "Explicit OpenRouter task. This spends the OpenRouter API, which is "
            "separate from Codex, Grok, and Gemini subscription auth. A cost-policy "
            "cap is not consent to choose this route automatically.",
        )

    harness = HARNESSES.get(engine)
    if harness is None:
        if provider == "openrouter" and billing_route == "subscription":
            return _blocked(
                engine, model, billing_route, provider,
                "A subscription label on an openrouter/ model is refused. "
                "Name engine opencode or openrouter with billing_route api to spend. "
                + OPENROUTER_NOT_SELECTED,
            )
        return _allowed(
            engine, model, billing_route, provider,
            "Custom engine left unchanged.",
        )

    if provider == "openrouter":
        return _blocked(
            engine, model, billing_route, provider,
            f"{engine} cannot run an openrouter/ model. "
            + OPENROUTER_NOT_SELECTED,
        )
    if provider not in harness.providers and provider != "unspecified":
        return _blocked(
            engine, model, billing_route, provider,
            f"{engine} authenticates {', '.join(sorted(harness.providers))} only. "
            f"Model provider {provider} stays on its own harness. "
            + harness.note + " " + OPENROUTER_NOT_SELECTED,
        )
    if billing_route == "api":
        if harness.api_auth is None:
            return _blocked(
                engine, model, billing_route, provider,
                f"{engine} has no API-key route in this repository. {harness.note} "
                + OPENROUTER_NOT_SELECTED,
            )
        return _allowed(
            engine, model, "api", provider,
            f"Explicit {engine} API route. It requires observed {harness.api_auth} "
            f"auth. {harness.note}",
        )
    if billing_policy_loaded and billing_route in {None, "subscription"}:
        probe = subscription_probe(engine)
        if not probe.supported:
            return _blocked(
                engine, model, billing_route, provider,
                probe.reason + " " + OPENROUTER_NOT_SELECTED,
            )
    route = "subscription" if billing_route in {None, "subscription"} else billing_route
    if route != "subscription":
        return _blocked(
            engine, model, billing_route, provider,
            f"Unsupported billing_route {billing_route!r} for {engine}.",
        )
    return _allowed(
        engine, model, "subscription", provider,
        f"{engine} subscription route. {harness.note}",
    )


def choose_unspecified_route(
    *,
    originating: str,
    provider: str,
    available: set[str] | frozenset[str],
    openrouter_consent: bool = False,
    billing_route: str | None = None,
    model: str = "",
) -> RouteDecision:
    """Pick a route only when the caller has not named an engine.

    Originating native subscription wins when that harness supports the
    provider and is available. Another configured native harness may run a
    provider the origin cannot. OpenRouter is chosen only with consent and
    only for an OpenRouter provider.
    """
    present = set(available)
    requested = billing_route or "subscription"
    if provider == "openrouter":
        if not openrouter_consent:
            return _blocked(
                "", model, requested, provider,
                "OpenRouter consent is missing. " + OPENROUTER_NOT_SELECTED,
            )
        for engine in ("opencode", "openrouter"):
            if engine in present:
                return _allowed(
                    engine, model, "api", "openrouter",
                    "Caller consented to an explicit OpenRouter route.",
                )
        return _blocked(
            "", model, "api", provider,
            "OpenRouter was requested, and neither the opencode nor the openrouter engine is available.",
        )

    order: list[str] = []
    if originating in HARNESSES:
        order.append(originating)
    for name in NATIVE_ENGINES:
        if name not in order:
            order.append(name)
    for engine in order:
        if engine not in present:
            continue
        harness = HARNESSES[engine]
        if provider not in harness.providers:
            continue
        if requested == "api":
            if harness.api_auth is None:
                return _blocked(
                    engine, model, "api", provider,
                    f"{engine} is the native harness for {provider} and has no API-key route. "
                    + OPENROUTER_NOT_SELECTED,
                )
            return _allowed(
                engine, model, "api", provider,
                f"Explicit API route on {engine}. Requires observed {harness.api_auth}. {harness.note}",
            )
        if requested != "subscription":
            return _blocked(engine, model, requested, provider, f"Unsupported billing_route {requested!r}.")
        return _allowed(
            engine, model, "subscription", provider,
            f"Native subscription on {engine}. {harness.note}",
        )
    return _blocked(
        "", model, requested, provider,
        f"No available native harness supports {provider} via {requested}. "
        + OPENROUTER_NOT_SELECTED,
    )


def _file_exists(value: str, environ: Mapping[str, str]) -> bool:
    text = (value or "").strip()
    if not text:
        return False
    if text.startswith("~/"):
        home = environ.get("HOME", "")
        path = Path(home) / text[2:] if home else Path(text).expanduser()
    else:
        path = Path(text)
    return path.is_file()


def command_available(name: str, environ: Mapping[str, str]) -> bool:
    if name == "agy":
        if _file_exists(environ.get("RINGER_GEMINI_BIN", ""), environ) or _file_exists(DEFAULT_AGY_BIN, environ):
            return True
    elif name == "claude":
        if _file_exists(environ.get("RINGER_CLAUDE_BIN", ""), environ) or _file_exists(DEFAULT_CLAUDE_BIN, environ):
            return True
    path = environ.get("PATH")
    if path is None:
        path = ""
    return shutil.which(name, path=path) is not None


def _wrapper_needs_opencode(path: Path) -> bool:
    if path.name == "opencode-sandboxed.sh":
        return True
    if path.suffix != ".sh" and "opencode" not in path.name:
        return False
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[:4000]
    except OSError:
        return False
    return "command -v opencode" in text or "OPENCODE_BIN" in text


def declared_runtime_dependencies(bin_path: str) -> tuple[str, ...]:
    path = Path(bin_path)
    base = path.name
    if base == "gemini_worker.py":
        return ("agy",)
    if base == "openrouter_worker.py":
        return ("claude",)
    if path.exists() and _wrapper_needs_opencode(path):
        return ("opencode",)
    if base == "opencode-sandboxed.sh":
        return ("opencode",)
    return ()


def missing_runtime_dependencies(bin_path: str, environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    env = environ if environ is not None else {}
    return tuple(
        name for name in declared_runtime_dependencies(bin_path) if not command_available(name, env)
    )


def missing_runtime_message(engine: str, bin_path: str, missing: tuple[str, ...]) -> str:
    detail = {
        "opencode": "the OpenCode CLI; the configured bin is a wrapper",
        "agy": "the Antigravity CLI agy; the gemini harness does not use the gemini CLI or an API key",
        "claude": "the claude CLI used by the OpenRouter adapter",
    }
    described = ", ".join(f"{name} ({detail.get(name, name)})" for name in missing)
    return (
        f"engine {engine!r} cannot run before any worker or spend reservation: {described} not found. "
        f"Configured bin: {bin_path}. This explicit route was kept. {OPENROUTER_NOT_SELECTED}"
    )


def inventory_engines(
    engines: Mapping[str, str],
    environ: Mapping[str, str],
) -> tuple[str, ...]:
    """Return engine names whose own bin and any wrapper CLI are both present."""
    found: list[str] = []
    for name, bin_path in engines.items():
        path = Path(bin_path)
        if path.is_absolute() or "/" in bin_path:
            if not path.is_file():
                continue
        elif not command_available(bin_path, environ):
            continue
        if missing_runtime_dependencies(bin_path, environ):
            continue
        found.append(name)
    return tuple(found)


def parallelism_reason(tasks: Any) -> str:
    """Say whether these tasks can share a swarm. Does not raise max_parallel."""
    items = list(tasks)
    if len(items) <= 1:
        return "One task, and ask, stay a single worker."
    shared: dict[str, list[str]] = {}
    for task in items:
        key = str(getattr(task, "key", ""))
        paths = list(getattr(task, "preflight_write_paths", ()) or ())
        paths.extend(
            path for path in getattr(task, "expect_files", ()) or ()
            if str(path).startswith("/")
        )
        for path in paths:
            shared.setdefault(str(path), []).append(key)
    collisions = [keys for keys in shared.values() if len(keys) >= 2]
    if collisions:
        return "Shared write paths stay serial. A swarm must not share a write root."
    return (
        "Independent tasks are a swarm: set max_parallel above 1. "
        "Parallelism does not authorise a paid route or a missing harness."
    )
