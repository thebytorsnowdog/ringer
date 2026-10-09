#!/usr/bin/env python3
"""Antigravity PreInvocation hook: nudge the coordinator once per conversation to route work through Ringer.

The hook only injects an ephemeral reminder. It never blocks, never returns a
permission decision and always exits 0.
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from ringer_nudge import HARNESS_RE, PROVIDER_RE
except Exception:
    PROVIDER_RE = re.compile(
        r"(api\.anthropic\.com|api\.openai\.com|openrouter\.ai|"
        r"generativelanguage\.googleapis|/v1/chat/completions|/v1/messages)",
        re.IGNORECASE,
    )
    HARNESS_RE = re.compile(
        r"\b(?:node|python3?|bun|deno)\s+\S*"
        r"(?:simulat|probe|smoke|harness|persona|grader|eval)\S*"
        r"\.(?:mjs|js|ts|py)\b",
        re.IGNORECASE,
    )


TAIL_LINES = 400
EDIT_LOOP_THRESHOLD = 3
EDIT_TOOLS = {"replace_file_content", "multi_replace_file_content", "write_to_file"}
COMMAND_TOOL = "run_command"
LIVE_RUN_RE = re.compile(r"ringer\.py\s+(?:run|ask)\b")
NON_LIVE_FLAGS = ("--dry-run", "--baseline")
WORKER_CLI_RE = re.compile(
    r"(?:^|[\s;&|(/])"
    r"(?:claude\s+(?:-p|--print)|grok\s+(?:-p|--single)|opencode\s+run|codex\s+exec)"
    r"(?=\s|$|=|[;&|)])",
)

NUDGE_LEADS = {
    "edit-loop": "This looks like an inline edit loop (three or more edits outside a live Ringer run).",
    "model-call": "This looks like a direct worker or model call outside a live Ringer run.",
}
NUDGE_BODY = (
    "Ringer routing check: load the ringer skill, record the model-fit assessment "
    "before work, and route the work through Ringer as a manifest with an executable "
    "check — a single task is a one-task manifest. If the user explicitly asked for "
    "inline work, that request wins; proceed inline. This reminder never authorises "
    "a model, paid spend or broader access."
)


def state_path() -> Path:
    value = os.environ.get("RINGER_NUDGE_STATE")
    if value and value.strip():
        return Path(value).expanduser()
    return Path.home() / ".ringer" / "antigravity-nudge-state.json"


def load_state(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return {"nudged": {}}
    if not isinstance(raw, dict) or not isinstance(raw.get("nudged"), dict):
        return {"nudged": {}}
    return raw


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, sort_keys=True)
        fh.write("\n")
    os.replace(tmp_path, path)


def decode_arg(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def string_arg(args: dict[str, Any], key: str) -> str:
    raw = args.get(key)
    decoded = decode_arg(raw)
    if isinstance(decoded, str):
        return decoded
    return raw if isinstance(raw, str) else ""


def normalise_path(value: str) -> str:
    text = value.strip()
    if text.startswith("file://"):
        text = text[len("file://"):]
    return os.path.normpath(os.path.expanduser(text)) if text else ""


def is_under(path: str, directory: str) -> bool:
    if not path or not directory:
        return False
    try:
        return os.path.commonpath([path, directory]) == directory
    except ValueError:
        return False


def tail_lines(path: Path, limit: int) -> deque[str]:
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        return deque(fh, maxlen=limit)


def tool_calls(lines: deque[str]) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("type") != "PLANNER_RESPONSE":
            continue
        items = entry.get("tool_calls")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            args = decode_arg(item.get("args"))
            if isinstance(name, str):
                calls.append((name, args if isinstance(args, dict) else {}))
    return calls


def is_live_run(command: str) -> bool:
    return bool(LIVE_RUN_RE.search(command)) and not any(flag in command for flag in NON_LIVE_FLAGS)


def is_model_call(command: str) -> bool:
    if "ringer.py" in command:
        return False
    return bool(WORKER_CLI_RE.search(command) or PROVIDER_RE.search(command) or HARNESS_RE.search(command))


def detect(calls: list[tuple[str, dict[str, Any]]], artifact_dir: str) -> str | None:
    edits = 0
    model_calls = 0
    for name, args in calls:
        if name == COMMAND_TOOL:
            command = string_arg(args, "CommandLine")
            if is_live_run(command):
                edits = 0
                model_calls = 0
            elif is_model_call(command):
                model_calls += 1
        elif name in EDIT_TOOLS:
            target = normalise_path(string_arg(args, "TargetFile"))
            if not is_under(target, artifact_dir):
                edits += 1
    if model_calls:
        return "model-call"
    if edits >= EDIT_LOOP_THRESHOLD:
        return "edit-loop"
    return None


def evaluate(payload: dict[str, Any]) -> dict[str, Any]:
    conversation_id = payload.get("conversationId")
    transcript = payload.get("transcriptPath")
    if not isinstance(conversation_id, str) or not conversation_id.strip():
        return {}
    if not isinstance(transcript, str) or not transcript.strip():
        return {}

    path = state_path()
    state = load_state(path)
    if conversation_id in state["nudged"]:
        return {}

    transcript_path = Path(transcript).expanduser()
    if not transcript_path.is_file():
        return {}
    artifact_raw = payload.get("artifactDirectoryPath")
    artifact_dir = normalise_path(artifact_raw) if isinstance(artifact_raw, str) else ""

    kind = detect(tool_calls(tail_lines(transcript_path, TAIL_LINES)), artifact_dir)
    if kind is None:
        return {}

    state["nudged"][conversation_id] = {
        "kind": kind,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    save_state(path, state)
    return {"injectSteps": [{"ephemeralMessage": f"{NUDGE_LEADS[kind]} {NUDGE_BODY}"}]}


def main() -> int:
    result: dict[str, Any] = {}
    try:
        payload = json.loads(sys.stdin.read())
        if isinstance(payload, dict):
            result = evaluate(payload)
    except Exception:
        result = {}
    try:
        sys.stdout.write(json.dumps(result) + "\n")
        sys.stdout.flush()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
