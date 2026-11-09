#!/usr/bin/env python3
"""One OpenRouter chat completion. No tools, no retries, no second request.

The caller reserves the worst case of --max-input-tokens and --max-tokens
before this process starts. This process refuses a prompt whose UTF-8 length
exceeds that input bound, sends max_tokens once, and exits. A provider that
ignores max_tokens is reported in the usage line; this process does not try again.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

DEFAULT_KEY_PATHS = (
    ".runtime/openrouter/openrouter.key",
    os.path.expanduser("~/.config/ringer/openrouter.key"),
)
ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"


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
        path = Path(rel)
        candidates.append(path if path.is_absolute() else Path(cwd) / path)
    for path in candidates:
        try:
            if path.is_file():
                text = path.read_text(encoding="utf-8").strip()
                if text:
                    return text
        except OSError:
            continue
    return ""


def bounded_input_tokens(spec: str) -> int:
    """UTF-8 bytes are an upper bound on BPE token count."""
    return len(spec.encode("utf-8"))


def completion_body(model: str, spec: str, max_tokens: int) -> dict[str, Any]:
    return {
        "model": strip_openrouter_prefix(model),
        "messages": [{"role": "user", "content": spec}],
        "max_tokens": max_tokens,
    }


def _one(flag: str, argv: list[str]) -> str | None:
    found = [argv[index + 1] for index, arg in enumerate(argv) if arg == flag and index + 1 < len(argv)]
    if len(found) != 1:
        return None
    return found[0]


def parse_request(argv: list[str]) -> tuple[str, str, str, int, int]:
    max_input = _one("--max-input-tokens", argv)
    max_tokens = _one("--max-tokens", argv)
    model = _one("-m", argv) or _one("--model", argv)
    spec = argv[-1] if argv else ""
    if (
        not model
        or not spec
        or spec.startswith("-")
        or max_input is None
        or max_tokens is None
        or not max_input.isdigit()
        or not max_tokens.isdigit()
        or int(max_input) <= 0
        or int(max_tokens) <= 0
    ):
        raise ValueError("openrouter_worker requires -m, --max-input-tokens, --max-tokens, and a final spec")
    cwd = _one("--cwd", argv) or _one("--dir", argv) or os.getcwd()
    return cwd, model, spec, int(max_input), int(max_tokens)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


def post_once(
    body: dict[str, Any],
    key: str,
    *,
    opener: Callable[..., Any] | None = None,
    url: str = ENDPOINT,
    timeout: float = 120,
) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    send = opener or urllib.request.build_opener(_NoRedirect).open
    with send(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("OpenRouter response is not an object")
    return payload


def execute(
    argv: list[str],
    *,
    env: dict[str, str] | None = None,
    opener: Callable[..., Any] | None = None,
) -> tuple[int, str]:
    cwd, model, spec, max_input, max_tokens = parse_request(argv)
    if bounded_input_tokens(spec) > max_input:
        return 1, json.dumps({
            "type": "error",
            "error": {"type": "input_bound", "message": "prompt exceeds the reserved input token bound"},
        })
    key = load_openrouter_key(cwd, env if env is not None else dict(os.environ))
    if not key:
        return 1, json.dumps({
            "type": "error",
            "error": {"type": "permission_denied", "message": "OpenRouter API key missing"},
        })
    body = completion_body(model, spec, max_tokens)
    try:
        payload = post_once(body, key, opener=opener)
    except Exception as exc:
        return 1, json.dumps({
            "type": "error",
            "error": {"type": "provider_error", "message": type(exc).__name__},
        })
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    choices = payload.get("choices")
    content = ""
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message")
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            content = message["content"]
    out_path = Path(cwd) / "openrouter-output.txt"
    out_path.write_text(content, encoding="utf-8")
    line = {
        "type": "turn.completed",
        "model": model,
        "usage": {
            "input_tokens": prompt_tokens,
            "output_tokens": completion_tokens,
            "cached_input_tokens": 0,
            "cache_write_tokens": 0,
        },
        "max_input_tokens": max_input,
        "max_output_tokens": max_tokens,
    }
    if not isinstance(prompt_tokens, int) or not isinstance(completion_tokens, int):
        line["type"] = "error"
        line["error"] = {"type": "usage_missing", "message": "OpenRouter response omitted integer usage"}
        return 1, json.dumps(line)
    if completion_tokens > max_tokens or prompt_tokens > max_input:
        line["type"] = "error"
        line["error"] = {"type": "bound_exceeded", "message": "reported usage exceeds the reserved token bound"}
        return 1, json.dumps(line)
    return 0, json.dumps(line)


def main(argv: list[str]) -> int:
    code, line = execute(argv[1:])
    print(line)
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
