#!/usr/bin/env python3
"""OpenRouter API fallback worker adapter for Ringer (via Claude Code CLI + OpenRouter Anthropic-compatible API)."""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

DEFAULT_CLAUDE_BIN = "/home/snowy/.local/share/mise/installs/claude/latest/claude"
DEFAULT_KEY_PATHS = (
    ".runtime/openrouter/openrouter.key",
    "/home/snowy/.config/ringer/openrouter.key",
)

EFFORT_MAP: dict[str, str] = {
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "max",
    "max": "max",
}


def strip_openrouter_prefix(model: str) -> str:
    clean = model.strip()
    if clean.startswith("openrouter/"):
        return clean[len("openrouter/"):]
    return clean


def load_openrouter_key(cwd: str, env: dict[str, str]) -> str:
    direct = (env.get("OPENROUTER_API_KEY") or "").strip()
    if direct:
        return direct
    custom_file = (env.get("RINGER_OPENROUTER_KEY_FILE") or "").strip()
    candidates: list[Path] = []
    if custom_file:
        candidates.append(Path(custom_file).expanduser())
    for rel in DEFAULT_KEY_PATHS:
        p = Path(rel)
        candidates.append(p if p.is_absolute() else Path(cwd) / p)
    for path in candidates:
        try:
            if path.is_file():
                text = path.read_text(encoding="utf-8").strip()
                if text:
                    return text
        except OSError:
            continue
    return ""


def parse_args(argv: list[str]) -> tuple[str, str, str, str, list[str]]:
    cwd = ""
    model = "openrouter/z-ai/glm-5.3-flash"
    effort = "medium"
    spec = ""
    extra: list[str] = []
    idx = 0
    while idx < len(argv):
        arg = argv[idx]
        if arg in {"run", "--pure", "--auto"}:
            idx += 1
        elif arg == "--format" and idx + 1 < len(argv):
            idx += 2
        elif arg.startswith("--format="):
            idx += 1
        elif arg in {"--dir", "--cwd"} and idx + 1 < len(argv):
            cwd = argv[idx + 1]
            idx += 2
        elif arg.startswith(("--dir=", "--cwd=")):
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
        raise ValueError("openrouter_worker: missing spec argument")
    target_cwd = cwd or os.getcwd()
    env = dict(os.environ)
    key = load_openrouter_key(target_cwd, env)
    if not key:
        raise PermissionError("permission denied: OpenRouter API key missing")

    base_url = env.get("RINGER_OPENROUTER_BASE_URL") or ("https://" + "openrouter.ai/api")
    claude_config_dir = Path(target_cwd) / ".runtime" / "openrouter" / "claude"
    claude_config_dir.mkdir(parents=True, exist_ok=True)

    env["ANTHROPIC_BASE_URL"] = base_url
    env["ANTHROPIC_AUTH_TOKEN"] = key
    env["ANTHROPIC_API_KEY"] = ""
    env["CLAUDE_CONFIG_DIR"] = str(claude_config_dir)
    env["DISABLE_AUTOUPDATER"] = "1"

    claude_bin = env.get("RINGER_CLAUDE_BIN") or DEFAULT_CLAUDE_BIN
    if not Path(claude_bin).is_file():
        resolved = shutil.which("claude")
        if resolved:
            claude_bin = resolved
    slug = strip_openrouter_prefix(model)
    claude_effort = EFFORT_MAP.get(effort.strip().lower(), "medium")
    cmd = [
        claude_bin,
        "-p",
        spec,
        "--model",
        slug,
        "--effort",
        claude_effort,
        "--output-format",
        "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
        "--no-session-persistence",
        *extra,
    ]
    return cmd, env, target_cwd


def main(argv: list[str]) -> int:
    try:
        cmd, env, target_cwd = build_exec_args(argv[1:])
    except PermissionError as exc:
        print(json.dumps({"type": "error", "error": {"type": "permission_denied", "message": str(exc)}}))
        return 1
    except Exception as exc:
        print(json.dumps({"type": "error", "error": {"type": "provider_error", "message": str(exc)}}))
        return 1
    os.chdir(target_cwd)
    os.execvpe(cmd[0], cmd, env)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
