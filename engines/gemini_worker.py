#!/usr/bin/env python3
"""Subscription Gemini worker adapter for Ringer (uses Antigravity CLI `agy` with Google AI Ultra subscription OAuth)."""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

DEFAULT_AGY_BIN = "/home/snowy/.local/share/mise/installs/antigravity-cli/latest/agy"

EFFORT_LEVEL_MAP: dict[str, str] = {
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",
    "max": "high",
}

AGY_EFFORT_FLAG_MAP: dict[str, str] = {
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
}

EXPLICIT_AGY_MODELS = {
    "gemini-4-argon-low",
    "gemini-4-argon-medium",
    "gemini-4-argon-high",
    "gemini-3.8-flash-low",
    "gemini-3.8-flash-medium",
    "gemini-3.8-flash-high",
}


def normalize_model(model: str, effort: str) -> str:
    clean = model.strip().lower()
    tier = EFFORT_LEVEL_MAP.get(effort.strip().lower(), "medium")
    if clean in EXPLICIT_AGY_MODELS:
        return clean
    if clean in {"flash", "flash-lite", "gemini-3.8-flash", "gemini-3.8-flash-thinking", "gemini-flash", "gemini-2.5-flash"}:
        return f"gemini-3.8-flash-{tier}"
    if clean in {"", "pro", "auto", "argon", "gemini-4-argon", "gemini-4-argon-thinking", "gemini-pro", "gemini-2.5-pro", "gemini-3.1-pro-preview"}:
        return f"gemini-4-argon-{tier}"
    return model.strip()


def parse_args(argv: list[str]) -> tuple[str, str, str, str, list[str]]:
    cwd = ""
    model = "gemini-4-argon"
    effort = "medium"
    spec = ""
    extra: list[str] = []
    idx = 0
    while idx < len(argv):
        arg = argv[idx]
        if arg == "--cwd" and idx + 1 < len(argv):
            cwd = argv[idx + 1]
            idx += 2
        elif arg.startswith("--cwd="):
            cwd = arg.partition("=")[2]
            idx += 1
        elif arg in {"-m", "--model"} and idx + 1 < len(argv):
            model = argv[idx + 1]
            idx += 2
        elif arg.startswith("--model="):
            model = arg.partition("=")[2]
            idx += 1
        elif arg in {"--effort", "--variant", "--reasoning-effort"} and idx + 1 < len(argv):
            effort = argv[idx + 1]
            idx += 2
        elif arg.startswith(("--effort=", "--variant=", "--reasoning-effort=")):
            effort = arg.partition("=")[2]
            idx += 1
        elif arg in {"-p", "--prompt"} and idx + 1 < len(argv):
            spec = argv[idx + 1]
            idx += 2
        elif arg.startswith("--prompt="):
            spec = arg.partition("=")[2]
            idx += 1
        else:
            extra.append(arg)
            idx += 1
    if not spec and extra:
        spec = extra.pop()
    return cwd, model, effort, spec, extra


def build_exec_args(argv: list[str]) -> tuple[list[str], dict[str, str], str]:
    cwd, model, effort, spec, extra = parse_args(argv)
    if not spec:
        raise ValueError("gemini_worker: missing prompt (-p <spec>)")
    target_cwd = cwd or os.getcwd()
    env = dict(os.environ)
    env.pop("GEMINI_API_KEY", None)
    env.pop("GOOGLE_API_KEY", None)

    home_dir = Path(env.get("HOME") or (Path(target_cwd) / ".runtime" / "gemini"))
    (home_dir / ".gemini" / "antigravity-cli").mkdir(parents=True, exist_ok=True)
    env["HOME"] = str(home_dir)

    agy_bin = env.get("RINGER_GEMINI_BIN") or DEFAULT_AGY_BIN
    if not Path(agy_bin).is_file():
        resolved = shutil.which("agy")
        if resolved:
            agy_bin = resolved
    normalized_model = normalize_model(model, effort)
    agy_effort = AGY_EFFORT_FLAG_MAP.get(effort.strip().lower(), "medium")
    cmd = [
        agy_bin,
        "-p",
        spec,
        "--model",
        normalized_model,
        "--effort",
        agy_effort,
        "--output-format",
        "stream-json",
        "--dangerously-skip-permissions",
        "--disable-slash-commands",
        "--add-dir",
        target_cwd,
        *extra,
    ]
    return cmd, env, target_cwd


def main(argv: list[str]) -> int:
    try:
        cmd, env, target_cwd = build_exec_args(argv[1:])
    except Exception as exc:
        print(json.dumps({"type": "error", "error": {"type": "provider_error", "message": str(exc)}}))
        return 1
    os.chdir(target_cwd)
    os.execvpe(cmd[0], cmd, env)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
