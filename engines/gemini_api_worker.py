#!/usr/bin/env python3
"""Paid Google AI Studio Gemini API worker adapter for Ringer.

Runs OpenCode (`opencode run --standalone --auto --format json`) inside Ringer's
sandbox with `GEMINI_API_KEY` / `GOOGLE_GENERATIVE_AI_API_KEY` loaded from a
read-only mounted key file rather than argv. Never accepts API keys on argv and
never routes non-Gemini models.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path

DEFAULT_OPENCODE_BINS = (
    os.path.expanduser("~/.local/share/mise/installs/opencode/latest/node_modules/.bin/opencode"),
    os.path.expanduser("~/.local/share/mise/shims/opencode"),
    "/opt/homebrew/bin/opencode",
    "/usr/local/bin/opencode",
)
DEFAULT_KEY_PATHS = (
    ".runtime/gemini-api/gemini.key",
    os.path.expanduser("~/.config/ringer/gemini.key"),
)

_MODEL_ALIASES: dict[str, str] = {
    "": "google/gemini-4-argon",
    "auto": "google/gemini-4-argon",
    "pro": "google/gemini-4-argon",
    "argon": "google/gemini-4-argon",
    "gemini-pro": "google/gemini-4-argon",
    "gemini-4-argon": "google/gemini-4-argon",
    "gemini-4-argon-thinking": "google/gemini-4-argon",
    "neon": "google/gemini-4-neon",
    "gemini-4-neon": "google/gemini-4-neon",
    "carbon": "google/gemini-4-carbon",
    "gemini-4-carbon": "google/gemini-4-carbon",
    "hydrogen": "google/gemini-4-hydrogen",
    "gemini-4-hydrogen": "google/gemini-4-hydrogen",
    "flash": "google/gemini-3.8-flash",
    "flash-lite": "google/gemini-3.8-flash",
    "gemini-flash": "google/gemini-3.8-flash",
    "gemini-3.8-flash": "google/gemini-3.8-flash",
    "gemini-3.8-flash-thinking": "google/gemini-3.8-flash",
}

_EFFORT_SUFFIX_RE = re.compile(r"-(minimal|low|medium|high|xhigh|max)$")
_SAFE_GEMINI_MODEL_RE = re.compile(r"^gemini-[a-z0-9][a-z0-9._-]*$")


def normalize_gemini_api_model(model: str) -> str:
    """Map a Gemini subscription or API model slug to an OpenCode google/<model> ID."""
    clean = (model or "").strip().lower()
    if clean.startswith("openrouter/"):
        raise ValueError("gemini_api_worker refuses openrouter/ models")
    if "#" in clean or ":" in clean:
        raise ValueError("gemini_api_worker refuses model variant/tag suffixes")
    if clean.startswith("google/"):
        base = clean.removeprefix("google/")
    else:
        base = clean
    if base in _MODEL_ALIASES:
        return _MODEL_ALIASES[base]
    stripped = _EFFORT_SUFFIX_RE.sub("", base)
    if stripped in _MODEL_ALIASES:
        return _MODEL_ALIASES[stripped]
    if _SAFE_GEMINI_MODEL_RE.fullmatch(stripped):
        return f"google/{stripped}"
    raise ValueError(f"gemini_api_worker only accepts Gemini models (got {model!r})")


def load_gemini_api_key(cwd: str, env: dict[str, str]) -> str:
    direct = (env.get("GEMINI_API_KEY") or env.get("GOOGLE_GENERATIVE_AI_API_KEY") or "").strip()
    if direct:
        return direct
    custom_file = (env.get("RINGER_GEMINI_KEY_FILE") or "").strip()
    candidates: list[Path] = []
    if custom_file:
        candidates.append(Path(custom_file).expanduser())
    for rel in DEFAULT_KEY_PATHS:
        path = Path(rel)
        candidates.append(path if path.is_absolute() else Path(cwd) / path)
    for path in candidates:
        try:
            if path.is_file():
                text = path.read_text(encoding="utf-8-sig").lstrip("\ufeff").strip()
                if text:
                    return text
        except OSError:
            continue
    return ""


def parse_args(argv: list[str]) -> tuple[str, str, str, str, list[str]]:
    cwd = ""
    model = "google/gemini-4-argon"
    spec = ""
    sandbox_wrapper = ""
    extra: list[str] = []
    idx = 0
    while idx < len(argv):
        arg = argv[idx]
        if arg in {"--api-key", "--auth"} or arg.startswith(("--api-key=", "--auth=")):
            raise ValueError("gemini_api_worker refuses API keys on argv")
        if arg in {"--cwd", "--dir"} and idx + 1 < len(argv):
            cwd = argv[idx + 1]
            idx += 2
        elif arg.startswith(("--cwd=", "--dir=")):
            cwd = arg.partition("=")[2]
            idx += 1
        elif arg == "--sandbox-wrapper" and idx + 1 < len(argv):
            sandbox_wrapper = argv[idx + 1]
            idx += 2
        elif arg.startswith("--sandbox-wrapper="):
            sandbox_wrapper = arg.partition("=")[2]
            idx += 1
        elif arg in {"-m", "--model"} and idx + 1 < len(argv):
            model = argv[idx + 1]
            idx += 2
        elif arg.startswith("--model="):
            model = arg.partition("=")[2]
            idx += 1
        elif arg in {"--effort", "--variant", "--reasoning-effort"} and idx + 1 < len(argv):
            # Effort suffixes are stripped onto base google/<model>; ignore flag.
            idx += 2
        elif arg.startswith(("--effort=", "--variant=", "--reasoning-effort=")):
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
    return cwd, model, spec, sandbox_wrapper, extra


def build_exec_args(
    argv: list[str],
    *,
    env: dict[str, str] | None = None,
) -> tuple[list[str], dict[str, str], str]:
    cwd, model, spec, sandbox_wrapper, extra = parse_args(argv)
    if not spec:
        raise ValueError("gemini_api_worker: missing prompt (-p <spec>)")
    target_cwd = cwd or os.getcwd()
    child_env = dict(os.environ if env is None else env)
    key = load_gemini_api_key(target_cwd, child_env)
    if not key:
        raise PermissionError(
            "Gemini AI Studio API key missing (create ~/.config/ringer/gemini.key with mode 0600)"
        )
    if not key.isascii() or any(ch.isspace() or ord(ch) < 32 for ch in key):
        raise PermissionError(
            "Gemini AI Studio API key in ~/.config/ringer/gemini.key contains whitespace or non-ASCII characters "
            "(check that you copied the full raw API key, not a masked preview)"
        )
    child_env.pop("OPENROUTER_API_KEY", None)
    child_env["GEMINI_API_KEY"] = key
    child_env["GOOGLE_GENERATIVE_AI_API_KEY"] = key
    normalized_model = normalize_gemini_api_model(model)

    opencode_args = [
        "run",
        "--standalone",
        "--auto",
        "--format",
        "json",
        "-m",
        normalized_model,
        *extra,
        spec,
    ]
    if sandbox_wrapper:
        # Ensure Homebrew / local bins are visible to wrapper scripts like opencode-sandboxed.sh.
        cur_path = child_env.get("PATH") or ""
        extra_dirs = [d for d in ("/opt/homebrew/bin", "/usr/local/bin") if os.path.isdir(d) and d not in cur_path.split(":")]
        if extra_dirs:
            child_env["PATH"] = ":".join([*extra_dirs, cur_path]) if cur_path else ":".join(extra_dirs)
        cmd = [sandbox_wrapper, target_cwd, *opencode_args]
        return cmd, child_env, target_cwd

    opencode_bin = child_env.get("RINGER_OPENCODE_BIN") or ""
    if not opencode_bin or not Path(opencode_bin).is_file():
        opencode_bin = next((p for p in DEFAULT_OPENCODE_BINS if Path(p).is_file()), "")
    if not opencode_bin:
        resolved = shutil.which("opencode")
        if resolved:
            opencode_bin = resolved
    if not opencode_bin:
        raise FileNotFoundError("opencode binary not found")
    cmd = [opencode_bin, *opencode_args]
    return cmd, child_env, target_cwd


def main(argv: list[str]) -> int:
    try:
        cmd, child_env, target_cwd = build_exec_args(argv[1:])
    except PermissionError as exc:
        print(json.dumps({"type": "error", "error": {"type": "permission_denied", "message": str(exc)}}))
        return 1
    except Exception as exc:
        print(json.dumps({"type": "error", "error": {"type": "provider_error", "message": str(exc)}}))
        return 1
    os.chdir(target_cwd)
    os.execvpe(cmd[0], cmd, child_env)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
