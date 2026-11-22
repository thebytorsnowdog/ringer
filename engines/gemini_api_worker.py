#!/usr/bin/env python3
"""Paid Google AI Studio Gemini API worker adapter for Ringer.

Runs OpenCode in standalone JSON mode for a Google Gemini model. Credentials
come from the environment or a local key file. Restricted Ringer tasks supply
a sandbox wrapper.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path

DEFAULT_OPENCODE_BINS = (
    "~/.local/share/mise/installs/opencode/latest/node_modules/.bin/opencode",
    "~/.local/share/mise/shims/opencode",
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
    raise ValueError("gemini_api_worker only accepts Gemini models")


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
        except (OSError, UnicodeError):
            continue
    return ""


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # Do not echo unrecognised arguments, which may contain credentials.
        raise ValueError("gemini_api_worker requires one model, a final prompt and supported worker flags")


def parse_args(argv: list[str]) -> tuple[str, str, str, str]:
    if not argv or not argv[-1]:
        raise ValueError("gemini_api_worker requires a final prompt")
    spec = argv[-1]
    options = argv[:-1]
    if options and options[-1] in ("-p", "--prompt"):
        options = options[:-1]
    if not spec:
        raise ValueError("gemini_api_worker requires a final prompt")
    parser = _ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--cwd", "--dir", default="")
    parser.add_argument("-m", "--model", action="append", required=True)
    parser.add_argument("--sandbox-wrapper", default="")
    parser.add_argument("--effort", "--variant", "--reasoning-effort")
    args = parser.parse_args(options)
    if len(args.model) != 1:
        raise ValueError("gemini_api_worker requires exactly one model")
    return args.cwd, args.model[0], spec, args.sandbox_wrapper


def resolve_opencode(environ: dict[str, str]) -> str | None:
    home = environ.get("HOME") or str(Path.home())
    candidates = (environ.get("RINGER_OPENCODE_BIN"), *DEFAULT_OPENCODE_BINS)
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate.replace("~/", home + "/", 1) if candidate.startswith("~/") else candidate)
        if path.is_file() and os.access(path, os.X_OK):
            return str(path.absolute())
    return shutil.which("opencode", path=environ.get("PATH", ""))


def build_exec_args(
    argv: list[str],
    *,
    env: dict[str, str] | None = None,
) -> tuple[list[str], dict[str, str], str]:
    cwd, model, spec, sandbox_wrapper = parse_args(argv)
    normalized_model = normalize_gemini_api_model(model)
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
    child_env["GOOGLE_API_KEY"] = key

    opencode_args = [
        "run",
        "--standalone",
        "--auto",
        "--format",
        "json",
        "-m",
        normalized_model,
        "--",
        spec,
    ]
    if sandbox_wrapper:
        # Ensure Homebrew / local bins are visible to wrapper scripts like opencode-sandboxed.sh.
        cur_path = child_env.get("PATH") or ""
        extra_dirs = [d for d in ("/opt/homebrew/bin", "/usr/local/bin") if os.path.isdir(d) and d not in cur_path.split(":")]
        if extra_dirs:
            child_env["PATH"] = ":".join([*extra_dirs, cur_path]) if cur_path else ":".join(extra_dirs)
        cmd = [sandbox_wrapper, target_cwd, "--", *opencode_args]
        return cmd, child_env, target_cwd

    opencode_bin = resolve_opencode(child_env)
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
