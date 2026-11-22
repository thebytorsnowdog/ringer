"""Command-line argument definitions for Ringer."""

from __future__ import annotations

import argparse
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    import ringer

    parser = argparse.ArgumentParser(
        prog="ringer.py",
        description=(
            "Ringer: deterministic parallel AI-agent orchestrator. Runs manifest tasks in parallel, "
            "verifies artifacts with executed checks, retries failures once, logs eval rows, "
            "and serves a live dashboard."
        ),
    )
    parser.add_argument("--config", type=Path, help="path to config.toml (default: XDG config path)")
    parser.add_argument(
        "--no-self-update",
        action="store_true",
        help="skip the startup self-update check for this invocation",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    update_parser = subparsers.add_parser(
        "self-update", help="check and apply an ff-only update from origin/main"
    )
    update_parser.add_argument(
        "--config", type=Path, default=argparse.SUPPRESS, help=argparse.SUPPRESS
    )

    run_parser = subparsers.add_parser("run", help="run a ringer manifest")
    run_parser.add_argument("manifest", type=Path, help="path to ringer.json")
    run_parser.add_argument("--config", type=Path, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    run_parser.add_argument("--max-parallel", type=int, help="override manifest max_parallel")
    run_parser.add_argument("--identity", help="orchestrator identity for HUD state and eval rows")
    run_parser.add_argument("--no-dashboard", action="store_true", help="disable live dashboard")
    run_parser.add_argument("--browser", action="store_true", help="open the dashboard in the browser instead of Ringside")
    run_parser.epilog = "Set RINGER_NO_CATALOG_REFRESH=1 to skip the non-blocking OpenRouter catalog auto-refresh."
    run_parser.add_argument(
        "--no-artifact",
        action="store_true",
        help="disable zero-LLM HTML status/report artifacts (see [artifact] in config.toml)",
    )
    run_parser.add_argument("--dry-run", action="store_true", help="print the plan without spawning codex")
    run_parser.add_argument(
        "--baseline",
        action="store_true",
        help=(
            "execute every task's CHECK against the unmodified tree and report, "
            "spawning no workers — assertions about unchanged behavior that fail "
            "baseline are bugs in the check, not work for a model"
        ),
    )
    run_parser.add_argument(
        "--allow-noncanonical-route",
        action="store_true",
        help="allow a registry-marked noncanonical model route for a deliberate bakeoff",
    )

    ask_parser = subparsers.add_parser(
        "ask",
        help="answer one normal request with a small, clean worker",
    )
    ask_parser.add_argument("request", nargs="?", help="the normal-language request")
    ask_parser.add_argument(
        "--request-file",
        type=Path,
        help="read the request from a text file",
    )
    ask_parser.add_argument(
        "--source",
        type=Path,
        action="append",
        default=[],
        help=(
            "file or directory to search for relevant passages; "
            "repeat as needed"
        ),
    )
    ask_parser.add_argument(
        "--state",
        type=Path,
        action="append",
        default=[],
        help=(
            "small file with settled decisions that must take priority; "
            "repeat as needed"
        ),
    )
    ask_parser.add_argument(
        "--config",
        type=Path,
        default=argparse.SUPPRESS,
        help=argparse.SUPPRESS,
    )
    ask_parser.add_argument(
        "--engine",
        default=ringer.DEFAULT_ENGINE_NAME,
        help=f"worker engine (default: {ringer.DEFAULT_ENGINE_NAME})",
    )
    ask_parser.add_argument("--model", help="model override (other engines also require --billing-route)")
    ask_parser.add_argument(
        "--billing-route", choices=("subscription", "api"),
        help="explicit billing route; billing-enabled Codex defaults to subscription",
    )
    ask_parser.add_argument(
        "--task-class",
        choices=("bounded-detail", "coordination", "consequential"),
        default="",
        help="delegation-table class; required before a paid shortlist route",
    )
    ask_parser.add_argument(
        "--task-spend-allowance-gbp", type=float,
        help="explicit finite positive GBP allowance for a paid API task; policy caps still apply",
    )
    ask_parser.add_argument(
        "--reasoning-effort",
        choices=("minimal", "low", "medium", "high", "xhigh", "max", "ultra"),
        default="low",
        help="Codex reasoning effort (default: low)",
    )
    ask_parser.add_argument(
        "--timeout-s",
        type=int,
        default=300,
        help="worker timeout (default: 300)",
    )
    ask_parser.add_argument(
        "--max-packet-bytes",
        type=int,
        default=16_000,
        help=(
            "hard limit for request plus selected source text "
            "(default: 16000)"
        ),
    )
    ask_parser.add_argument(
        "--max-file-bytes",
        type=int,
        default=4_000_000,
        help="skip any one source larger than this (default: 4000000)",
    )
    ask_parser.add_argument(
        "--max-files",
        type=int,
        default=200,
        help="source file limit (default: 200)",
    )
    ask_parser.add_argument(
        "--workdir",
        type=Path,
        help="where to save the packet, answer, and log",
    )
    ask_parser.add_argument(
        "--keep-packet",
        action="store_true",
        help="save the full request packet for debugging (off by default)",
    )
    ask_parser.add_argument(
        "--redact",
        action="store_true",
        help="hide the request packet from state, command, and eval records",
    )
    ask_parser.add_argument(
        "--identity",
        help="orchestrator identity for the local run record",
    )
    ask_parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "select passages and show the packet size without making "
            "a model call"
        ),
    )
    ask_parser.add_argument(
        "--assessment",
        type=Path,
        help="JSON model_assessment generated from this exact ask dry-run",
    )

    assess_parser = subparsers.add_parser(
        "assess",
        help="generate a zero-LLM, source-bound model assessment draft",
    )
    assess_parser.add_argument("manifest", type=Path, help="path to ringer manifest")
    assess_parser.add_argument("--config", type=Path, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    assess_parser.add_argument("--coordinator", help="coordinator identity to prefill")
    assess_parser.add_argument("--output", type=Path, help="write the full draft manifest here")

    lint_parser = subparsers.add_parser("lint", help="lint a ringer manifest")
    lint_parser.add_argument("manifest", type=Path, help="path to ringer.json")
    lint_parser.add_argument(
        "--allow-noncanonical-route",
        action="store_true",
        help="allow a registry-marked noncanonical model route for a deliberate bakeoff",
    )

    outcomes_parser = subparsers.add_parser("outcomes", help="reconcile task states, attempt journal and lifecycle evidence")
    outcomes_parser.add_argument("--config", type=Path, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    outcomes_parser.add_argument("--state-dir", type=Path)
    outcomes_parser.add_argument("--log", type=Path)
    outcomes_parser.add_argument("--job-id", help="filter a job across run rounds")
    outcomes_parser.add_argument("--json", action="store_true")
    outcomes_parser.add_argument("--read-back", action="store_true", help="rehash exported files against their recorded evidence")

    hud_parser = subparsers.add_parser("hud", help="start the persistent Ringside page in your browser")
    hud_parser.add_argument("--config", type=Path, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    hud_parser.add_argument("--port", type=int, help=f"port to bind on 127.0.0.1 (default: {ringer.DEFAULT_HUD_PORT})")
    hud_parser.add_argument("--no-open", action="store_true", help="start the server without opening a browser")
    hud_parser.add_argument("--force-open", action="store_true", help="open a new browser tab even if Ringside is already open")

    db_parser = subparsers.add_parser("db", help="manage the derived SQLite read model")
    db_parser.add_argument("--config", type=Path, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    db_subparsers = db_parser.add_subparsers(dest="db_command", required=True)
    for name in ("rebuild", "sync"):
        sub = db_subparsers.add_parser(name, help=f"{name} the derived SQLite read model")
        sub.add_argument("--db", type=Path, help="path to SQLite read model (default: ~/.ringer/ringer.db)")
        sub.add_argument("--log", type=Path, help="path to local eval JSONL log")
        sub.add_argument("--catalog-file", type=Path, help="path to local OpenRouter catalog snapshot")
        sub.add_argument("--registry", type=Path, help="path to model identity registry")

    models_parser = subparsers.add_parser("models", help="show the local per-model performance scoreboard")
    models_parser.add_argument("--config", type=Path, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    models_parser.add_argument("--log", type=Path, help="path to local eval JSONL log")
    models_parser.add_argument("--db", type=Path, help="path to SQLite read model (default: ~/.ringer/ringer.db)")
    models_parser.add_argument("--task-type", help="only include one task_type bucket")
    models_parser.add_argument("--model", help="only include one resolved model bucket")
    models_parser.add_argument("--engine", help="only include rows from one worker engine")
    models_parser.add_argument("--since", help="only include rows logged on or after YYYY-MM-DD")
    models_parser.add_argument("--explore", action="store_true", help="show proven/probation tiers plus cheap untested catalog candidates")
    models_parser.add_argument("--catalog-file", type=Path, help="path to local OpenRouter catalog snapshot")
    models_parser.add_argument("--notes-file", type=Path, default=ringer.default_model_notes_path(), help="path to MODEL-NOTES.md judgment layer")
    models_parser.add_argument("--registry", type=Path, default=ringer.default_model_registry_path(), help=argparse.SUPPRESS)
    models_parser.add_argument("--html", nargs="?", const="", help="render a self-contained HTML scoreboard; optional output path")
    models_parser.add_argument("--open", action="store_true", help="render the HTML scoreboard to the artifact library and open it")
    models_parser.add_argument("--json", action="store_true", help="print the scoreboard as JSON")

    catalog_parser = subparsers.add_parser("catalog", help="show or refresh the local OpenRouter model catalog")
    catalog_parser.add_argument("--refresh", action="store_true", help="fetch source and rewrite the local snapshot")
    catalog_parser.add_argument("--source", help=f"OpenRouter models URL or fixture file (default: {ringer.DEFAULT_CATALOG_SOURCE})")
    catalog_parser.add_argument("--file", type=Path, help="catalog snapshot path (default: ~/.ringer/openrouter-catalog.json)")
    catalog_parser.add_argument("--free", action="store_true", help="show free models only")
    catalog_parser.add_argument("--changes", action="store_true", help="show recent catalog changes newest first")
    catalog_parser.add_argument("--json", action="store_true", help="print the model list as JSON and nothing else")

    demo_parser = subparsers.add_parser("demo", help="generate and run a 3-task toy manifest in /tmp")
    demo_parser.add_argument("--config", type=Path, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    demo_parser.add_argument("--max-parallel", type=int, help="override demo max_parallel")
    demo_parser.add_argument("--identity", help="orchestrator identity for HUD state and eval rows")
    demo_parser.add_argument("--no-dashboard", action="store_true", help="disable live dashboard")
    demo_parser.add_argument("--browser", action="store_true", help="open the dashboard in the browser instead of Ringside")
    demo_parser.add_argument(
        "--no-artifact",
        action="store_true",
        help="disable zero-LLM HTML status/report artifacts (see [artifact] in config.toml)",
    )
    demo_parser.add_argument("--dry-run", action="store_true", help="print the demo plan without spawning codex")
    demo_parser.add_argument(
        "--assessment", type=Path,
        help="JSON model_assessment bound to the generated demo tasks",
    )

    install_parser = subparsers.add_parser("install-agent", help="install the ringer skill and hooks for Codex")
    install_parser.add_argument("--project", action="store_true", help="install under the current project instead of the user config directory")
    install_parser.add_argument("--codex", action="store_true", help="install for Codex under .agents and .codex")
    install_parser.add_argument("--claude", action="store_true", help=argparse.SUPPRESS)

    uninstall_parser = subparsers.add_parser("uninstall-agent", help="remove the ringer skill and hooks")
    uninstall_parser.add_argument("--project", action="store_true", help="remove from the current project instead of the user config directory")
    uninstall_parser.add_argument("--codex", action="store_true", help="remove the Codex installation")
    uninstall_parser.add_argument("--claude", action="store_true", help="remove a leftover Claude Code installation")
    return parser
