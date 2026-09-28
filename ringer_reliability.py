"""Local reliability evidence and policy. No model, network or approval actions."""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import shlex
import signal
import tempfile
import tomllib
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FAILURE_CLASSES = frozenset({"product", "checker_timeout", "worker_timeout", "missing_dependency",
                             "missing_checker", "export", "provider/runtime", "quota", "permission",
                             "interrupted", "unknown"})
NEVER_RETRY = frozenset({"quota", "permission", "interrupted"})
TERMINAL_STATUSES = frozenset({"pass", "fail", "blocked", "interrupted", "not_started"})


def reliability_fields(obj: dict[str, Any], key: str) -> dict[str, Any]:
    """Validate manifest additions once, at the input boundary."""
    values: dict[str, Any] = {}
    for name, default in (("check_timeout_s", 60), ("preflight_timeout_s", 15)):
        value = obj.get(name, default)
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"task {key}: {name} must be finite and positive")
        values[name] = value
    for name in ("preflight_command", "preflight_python"):
        value = obj.get(name, "")
        if not isinstance(value, str) or (name in obj and not value.strip()):
            raise ValueError(f"task {key}: {name} must be a non-empty string")
        values[name] = value
    for name in ("preflight_files", "preflight_write_paths", "preflight_python_modules", "retry_classes"):
        value = obj.get(name, [])
        if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
            raise ValueError(f"task {key}: {name} must be a list of non-empty strings")
        values[name] = tuple(value)
    if values["preflight_python_modules"] and not values["preflight_python"]:
        raise ValueError(f"task {key}: preflight_python is required with preflight_python_modules")
    if any(not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", v) for v in values["preflight_python_modules"]):
        raise ValueError(f"task {key}: invalid preflight_python_modules name")
    if set(values["retry_classes"]) - (FAILURE_CLASSES - NEVER_RETRY):
        raise ValueError(f"task {key}: retry_classes contains unknown or forbidden classes")
    policy = obj.get("retry_policy", "product")
    if policy not in ("product", "legacy"):
        raise ValueError(f"task {key}: retry_policy must be product or legacy")
    values["retry_policy"] = policy
    hashes = obj.get("source_sha256", {})
    if not isinstance(hashes, dict) or any(
        not isinstance(p, str) or not p.strip() or not isinstance(h, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", h)
        for p, h in hashes.items()
    ):
        raise ValueError(f"task {key}: source_sha256 must map paths to SHA-256 hex digests")
    values["source_sha256"] = {p: h.lower() for p, h in hashes.items()}
    return values


def sha256_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _structured_provider_failure(text: str) -> str | None:
    """Read standalone provider error envelopes, never JSON quoted in prose."""
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "error" or not isinstance(event.get("error"), dict):
            continue
        error = event["error"]
        kind = str(error.get("type", "")).lower()
        message = str(error.get("message", "")).lower()
        if kind in {"insufficient_quota", "quota_exceeded", "rate_limit_exceeded"}:
            return "quota"
        if kind in {"permission_denied", "unauthorized", "forbidden", "authentication_error"}:
            return "permission"
        if kind in {"provider_error", "api_error", "service_unavailable"}:
            return "provider/runtime"
        # A real error envelope may use a generic type, but its error message is
        # still diagnostic context, unlike arbitrary worker prose.
        if re.fullmatch(r"(?:insufficient quota|quota exceeded|usage limit|rate limit|too many requests|429|insufficient credits|credit balance)", message.strip()):
            return "quota"
        if re.fullmatch(r"(?:permission denied|operation not permitted|unauthorized|forbidden|authentication required)", message.strip()):
            return "permission"
    return None


def _plain_diagnostic_failure(text: str) -> str | None:
    """Classify complete diagnostic lines, not source snippets or conversational prose."""
    prefix = r"(?:(?:error|fatal|provider error|api error)\s*:\s*|[a-z_]*error\s*:\s*)?"
    for line in text.splitlines():
        value = line.strip().lower()
        if re.fullmatch(prefix + r"(?:insufficient_quota|quota exceeded|usage limit|rate limit|too many requests|429|insufficient credits|credit balance)", value):
            return "quota"
        if re.fullmatch(prefix + r"(?:permission denied|operation not permitted|allow_full_access is false|unauthorized|forbidden|authentication required)", value):
            return "permission"
    return None


def classify_failure(worker: Any, verify: Any, *, worker_output: str = "", export_error: bool = False) -> str | None:
    """Classify failures from process state and diagnostic events, never arbitrary output."""
    if worker.timed_out:
        return "worker_timeout"
    if verify.check_timed_out:
        return "checker_timeout"
    if export_error:
        return "export"
    worker_failed = bool(worker.error) or worker.returncode not in (0, None)
    check_failed = verify.check_returncode not in (0, None)
    worker_text = (worker.error or "") + "\n" + worker_output
    # A structured provider error is authoritative even where a CLI incorrectly
    # exits zero. Plain text only counts after a failing worker or checker.
    structured = _structured_provider_failure(worker_text)
    if structured:
        return structured
    if worker_failed or check_failed:
        diagnostic = _plain_diagnostic_failure(worker_text)
        if diagnostic:
            return diagnostic
        if check_failed:
            diagnostic = _plain_diagnostic_failure(verify.raw_output_excerpt)
            if diagnostic:
                return diagnostic
    text = (worker_text + "\n" + verify.raw_output_excerpt).lower()
    if worker_failed or check_failed:
        if re.search(r"can't open file|cannot open.*\.(py|sh)|no such file.*\.(py|sh)", text):
            return "missing_checker"
        if re.search(r"modulenotfounderror|no module named|command not found|: .*: not found", text):
            return "missing_dependency"
        if re.search(r"provider error|api error|connection refused|connection reset|service unavailable|bad gateway", text):
            return "provider/runtime"
    if worker.error:
        return "unknown"
    if verify.missing_files or (verify.check_returncode not in (0, None) and not verify.check_timed_out):
        return "product"
    return None if verify.ok and worker.returncode == 0 else "unknown"


def should_retry(failure_class: str | None, task: Any, attempt: int, verdict: str) -> bool:
    if attempt >= task.max_attempts or failure_class in NEVER_RETRY:
        return False
    if failure_class in task.retry_classes:
        return True
    if task.retry_policy == "legacy":
        return verdict in {"FAIL", "TIMEOUT"}
    return failure_class == "product"


def read_jsonl(path: Path) -> tuple[list[dict[str, Any]], int]:
    rows, skipped = [], 0
    if not path.exists():
        return rows, skipped
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("not an object")
                rows.append(row)
            except (ValueError, TypeError):
                skipped += 1
    return rows, skipped


class LifecycleJournal:
    """Append-only, fsynced events. Replaying an event never changes its first payload."""
    def __init__(self, path: Path):
        self.path = path

    def append(self, payload: dict[str, Any]) -> str:
        identity = [payload[k] for k in ("run_id", "job_id", "task_key", "attempt_index", "event")]
        event_id = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a+b") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            stream.seek(0)
            data = stream.read()
            for line in data.splitlines():
                try:
                    if json.loads(line).get("event_id") == event_id:
                        return event_id
                except (ValueError, AttributeError):
                    pass
            row = dict(payload, event_id=event_id, recorded_at=datetime.now(timezone.utc).isoformat())
            # Keep a torn historical tail, but isolate it so new rows remain parseable.
            stream.seek(0, 2)
            if data and not data.endswith(b"\n"):
                stream.write(b"\n")
            stream.write((json.dumps(row, sort_keys=True) + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())
        return event_id


async def stop_process_tree(proc: asyncio.subprocess.Process) -> None:
    # Signal the group even if the root has exited: descendants may still hold stdout.
    for sig in (signal.SIGTERM, signal.SIGKILL):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, sig)
        if sig == signal.SIGTERM:
            await asyncio.sleep(.5)
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(proc.wait(), 1)


async def run_bounded(command: str | list[str], cwd: Path, timeout: float, log_path: Path | None = None) -> tuple[int | None, bool, str]:
    kwargs = dict(cwd=str(cwd), stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                  stderr=asyncio.subprocess.STDOUT, start_new_session=True)
    if isinstance(command, str):
        proc = await asyncio.create_subprocess_shell(command, **kwargs)
    else:
        proc = await asyncio.create_subprocess_exec(*command, **kwargs)
    capture = bytearray()
    async def drain():
        while chunk := await proc.stdout.read(4096):
            capture.extend(chunk)
            if len(capture) > 1_000_000:
                del capture[:-1_000_000]
            if log_path is not None:
                with log_path.open("ab") as stream:
                    stream.write(chunk)
    async def finish():
        await drain()
        await proc.wait()
    reader = asyncio.create_task(finish())
    timed_out = False
    try:
        await asyncio.wait_for(asyncio.shield(reader), timeout)
    except asyncio.TimeoutError:
        timed_out = True
        await stop_process_tree(proc)
    except BaseException:
        await stop_process_tree(proc)
        raise
    finally:
        # Bound pipe draining even when a descendant escapes the process group.
        try:
            await asyncio.wait_for(asyncio.shield(reader), 1)
        except asyncio.TimeoutError:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader
    return proc.returncode, timed_out, capture.decode("utf-8", errors="replace")


def task_path(taskdir: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else taskdir / path


def _codex_workspace_write_roots(command: list[str]) -> tuple[str | None, list[Path]]:
    """Read only an unambiguous workspace-write sandbox from resolved Codex argv."""
    settings: list[str] = []
    sandbox_modes: list[str] = []
    index = 0
    while index < len(command):
        item = command[index]
        if item in {"--sandbox", "-s"}:
            if index + 1 >= len(command):
                return f"malformed Codex sandbox option: {item} requires a mode", []
            sandbox_modes.append(command[index + 1])
            index += 2
            continue
        if item.startswith("--sandbox="):
            sandbox_modes.append(item.partition("=")[2])
        elif item.startswith("-s="):
            sandbox_modes.append(item.partition("=")[2])
        elif item.startswith("-s") and item != "-s":
            sandbox_modes.append(item[2:])
        elif item == "--dangerously-bypass-approvals-and-sandbox":
            return "Codex sandbox bypass is incompatible with declared worker write paths", []
        if item in {"-c", "--config"}:
            if index + 1 >= len(command):
                return f"malformed Codex config override: {item} requires key=value", []
            settings.append(command[index + 1])
            index += 2
            continue
        if item.startswith("--config="):
            settings.append(item.partition("=")[2])
        elif item.startswith("-c") and item != "-c":
            settings.append(item[2:])
        index += 1

    values: list[Any] = []
    for setting in settings:
        key, separator, raw = setting.partition("=")
        if not separator:
            return f"malformed Codex config override: {setting!r}; expected key=value", []
        if key.strip() == "sandbox_mode":
            return "Codex sandbox_mode config override is ambiguous with argv sandbox validation", []
        if key.strip() != "sandbox_workspace_write.writable_roots":
            continue
        try:
            values.append(tomllib.loads(f"value = {raw}")["value"])
        except (tomllib.TOMLDecodeError, KeyError) as exc:
            return f"malformed sandbox_workspace_write.writable_roots override: {exc}", []
    if len(sandbox_modes) != 1:
        return "Codex sandbox mode is missing or ambiguous; declared worker write paths require --sandbox workspace-write", []
    if sandbox_modes[0] != "workspace-write":
        return f"Codex sandbox mode must be workspace-write for declared worker write paths, got: {sandbox_modes[0]!r}", []
    if len(values) > 1:
        return "multiple sandbox_workspace_write.writable_roots overrides are ambiguous", []
    if not values:
        return None, []
    value = values[0]
    if not isinstance(value, list) or any(not isinstance(path, str) or not path for path in value):
        return "sandbox_workspace_write.writable_roots must be a list of non-empty absolute path strings", []
    roots: list[Path] = []
    for name in value:
        path = Path(name)
        if not path.is_absolute():
            return f"sandbox workspace-write root must be absolute: {name}", []
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            return f"sandbox workspace-write root is unavailable: {path}: {exc}", []
        if not resolved.is_dir():
            return f"sandbox workspace-write root is not a directory: {resolved}", []
        roots.append(resolved)
    return None, roots


def _path_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def preflight_local(task: Any, taskdir: Path) -> tuple[str | None, str]:
    """Check only declared inputs and obvious interpreter scripts, never inferred dependencies."""
    for name in task.preflight_files:
        path = task_path(taskdir, name)
        if not path.is_file():
            return "missing_dependency", f"preflight file missing: {path}"
        with path.open("rb") as stream:
            stream.read(1)
    for name, expected in task.source_sha256.items():
        path = task_path(taskdir, name)
        if not path.is_file():
            return "missing_dependency", f"requested source missing: {path}"
        if sha256_file(path) != expected:
            return "unknown", f"requested source SHA-256 mismatch: {path}"
    write_paths: list[Path] = []
    for name in getattr(task, "preflight_write_paths", ()):
        path = task_path(taskdir, name)
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            return "missing_dependency", f"preflight write path is unavailable: {path}: {exc}"
        if not resolved.is_dir():
            return "missing_dependency", f"preflight write path is not a directory: {resolved}"
        write_paths.append(resolved)
    if getattr(task, "restricted_codex", False) and write_paths:
        error, roots = _codex_workspace_write_roots(getattr(task, "worker_command", []))
        if error:
            return "permission", error
        task_root = taskdir.resolve(strict=True)
        allowed_roots = [task_root, *roots]
        for path in write_paths:
            if not any(_path_within(path, root) for root in allowed_roots):
                return (
                    "permission",
                    f"preflight write path is outside the resolved Codex workspace-write sandbox: {path}; "
                    "forward it with engine_args as -c "
                    'sandbox_workspace_write.writable_roots=[\"/absolute/source/dir\"]',
                )
    for path in write_paths:
        fd, probe = tempfile.mkstemp(prefix=".ringer-write-probe-", dir=path)
        os.close(fd)
        os.unlink(probe)
    try:
        parts = shlex.split(task.check)
    except ValueError:
        parts = []
    if parts and re.fullmatch(r"python(?:\d+(?:\.\d+)*)?|bash|sh|node", Path(parts[0]).name):
        # The first bare argument after simple interpreter flags is the script.
        for item in parts[1:]:
            if item in ("-c", "-m", "-e", "--"):
                break
            if item.startswith("-"):
                continue
            path = task_path(taskdir, item)
            outputs = {task_path(taskdir, name).resolve() for name in task.expect_files}
            if path.suffix in {".py", ".sh", ".js"} and path.resolve() not in outputs and not path.is_file():
                return "missing_checker", f"checker script missing before dispatch: {path}"
            break
    # Probe the existing parent; checks may create nested directories and outputs later.
    for name in task.expect_files:
        parent = task_path(taskdir, name).parent
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        fd, probe = tempfile.mkstemp(prefix=".ringer-write-probe-", dir=parent)
        os.close(fd)
        os.unlink(probe)
    return None, ""


def reconcile_outcomes(states: list[dict[str, Any]], attempts: list[dict[str, Any]], events: list[dict[str, Any]]) -> dict[str, Any]:
    """Union all sources; historical gaps stay visible, never fabricated as model failures."""
    joined: dict[tuple[str, str], dict[str, Any]] = {}
    def entry(run_id, key):
        return joined.setdefault((run_id, key), dict(run_id=run_id, task_key=key, attempts=[], events=[], state=None))
    for state in states:
        for task in state.get("tasks", []):
            item = entry(state.get("run_id", "unknown"), task.get("key", task.get("task_key", "unknown")))
            item.update(state=task, job_id=state.get("job_id") or state.get("run_name") or state.get("run_id"), run_finished=state.get("finished", False))
    for row in attempts:
        if row.get("run_id") and row.get("task_key"):
            entry(row["run_id"], row["task_key"])["attempts"].append(row)
    seen = set()
    for row in events:
        if row.get("event_id"):
            if row["event_id"] in seen:
                continue
            seen.add(row["event_id"])
        if row.get("run_id") and row.get("task_key"):
            entry(row["run_id"], row["task_key"])["events"].append(row)
    tasks = []
    for item in joined.values():
        state = item["state"] or {}
        rows, life = item["attempts"], item["events"]
        terminal = next((e for e in reversed(life) if e.get("event") == "terminal"), {})
        last = rows[-1] if rows else {}
        starts = [e for e in life if e.get("event") == "attempt_started"]
        finishes = {e.get("attempt_index") for e in life if e.get("event") == "attempt_finished"}
        # A later started attempt outweighs an earlier PASS or FAIL attempt row.
        count = max([state.get("attempts", 0) or 0, len(rows)] + [e.get("attempt_index", 0) for e in life])
        partial = bool(any(e.get("attempt_index") not in finishes for e in starts) or count > len(rows))
        status = terminal.get("status") or state.get("status") or ("unknown" if partial else str(last.get("verdict", "unknown")).lower())
        evidence = {**(last if not partial else {}), **state, **terminal}
        product = evidence.get("product_state", "UNKNOWN")
        # Old exit-code state can prove a check, but an old verdict alone cannot.
        if product == "UNKNOWN" and state.get("check_returncode") is not None and not state.get("check_timed_out"):
            product = "PASS" if state["check_returncode"] == 0 else "NEEDS_CHANGE"
        interrupted = status != "not_started" and (status == "interrupted" or evidence.get("failure_class") == "interrupted")
        unknown = product == "UNKNOWN" or partial
        model = evidence.get("model") or last.get("model") or state.get("model") or "unattributed"
        task = dict(run_id=item["run_id"], job_id=terminal.get("job_id") or item.get("job_id") or last.get("job_id") or (life[-1].get("job_id") if life else None) or item["run_id"],
                    task_key=item["task_key"], status=status, model=model, attempts=count, journal_attempts=len(rows),
                    missing_attempt_rows=max(0, count-len(rows)), state_only=not rows and not life,
                    partial_attempt_evidence=partial, task_contract_state=evidence.get("task_contract_state", "UNKNOWN"),
                    product_state=product, promotion_state="BLOCKED", failure_class=evidence.get("failure_class", "unknown"),
                    verification_unknown=unknown, unscored=not rows or unknown,
                    interrupted=interrupted, model_failure=evidence.get("failure_class") == "product",
                    evidence_sources=[s for s, present in (("state", item["state"] is not None), ("attempts", bool(rows)), ("lifecycle", bool(life))) if present],
                    attempt_history=rows, lifecycle=life)
        task["export_state"] = evidence.get("export_state", "UNKNOWN")
        task["harvested_files"] = evidence.get("harvested_files", evidence.get("deliverables", []))
        tasks.append(task)
    return summarise_outcomes(tasks)


def summarise_outcomes(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(tasks)
    passed = sum(t["status"] == "pass" and not t["verification_unknown"] and t["task_contract_state"] == "PASS" and t["export_state"] == "PASS" for t in tasks)
    return dict(totals=dict(tasks=total, passed=passed, success_coverage=passed/total if total else None,
                            jobs=len({t["job_id"] for t in tasks}), runs=len({t["run_id"] for t in tasks}),
                            journal_attempts=sum(t["journal_attempts"] for t in tasks),
                            passed_checks=sum(t["product_state"] == "PASS" for t in tasks),
                            unknown_contract=sum(t["task_contract_state"] == "UNKNOWN" for t in tasks),
                            unknown_export=sum(t["export_state"] == "UNKNOWN" for t in tasks),
                            failure_classes=dict(Counter(t["failure_class"] or "none" for t in tasks)),
                            unscored=sum(t["unscored"] for t in tasks), unknown_verification=sum(t["verification_unknown"] for t in tasks),
                            state_only=sum(t["state_only"] for t in tasks),
                            missing_attempt_rows=sum(t["missing_attempt_rows"] for t in tasks),
                            partial_attempt_evidence=sum(t["partial_attempt_evidence"] for t in tasks),
                            model_failures=sum(t["model_failure"] for t in tasks),
                            interruptions=sum(t["interrupted"] for t in tasks),
                            statuses=dict(Counter(t["status"] for t in tasks))),
                model_attribution=dict(Counter(t["model"] for t in tasks)), tasks=tasks)


def read_outcomes(state_dir: Path, attempt_path: Path) -> dict[str, Any]:
    states, errors = [], []
    for path in sorted((state_dir / "runs").glob("*.json")):
        try:
            state = json.loads(path.read_text())
            if not isinstance(state, dict) or not isinstance(state.get("tasks", []), list):
                raise ValueError("invalid run state")
            states.append(state)
        except (OSError, ValueError) as exc:
            errors.append(f"{path}: {exc}")
    attempts, bad_attempts = read_jsonl(attempt_path)
    events, bad_events = read_jsonl(state_dir / "lifecycle.jsonl")
    payload = reconcile_outcomes(states, attempts, events)
    payload["read_errors"] = errors
    payload["malformed_rows"] = dict(attempts=bad_attempts, lifecycle=bad_events)
    payload["sources"] = dict(states=str(state_dir / "runs"), attempts=str(attempt_path), lifecycle=str(state_dir / "lifecycle.jsonl"))
    return payload


def read_export_evidence(deliverables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    evidence = []
    for item in deliverables:
        row = dict(item)
        try:
            path = Path(item["path"])
            row["actual_sha256"] = sha256_file(path)
            row["actual_bytes"] = path.stat().st_size
            row["matches"] = (row["actual_sha256"] == item["sha256"] and row["actual_bytes"] == item["bytes"]) if item.get("sha256") and item.get("bytes") is not None else None
        except (OSError, KeyError) as exc:
            row.update(matches=False, error=str(exc))
        evidence.append(row)
    return evidence


if __name__ == "__main__":
    # Internal subprocess boundary for bounded local file checks. No worker/model calls.
    import sys
    from types import SimpleNamespace
    if len(sys.argv) != 3 or sys.argv[1] != "--preflight":
        raise SystemExit("internal helper: expected --preflight JSON")
    try:
        result = preflight_local(SimpleNamespace(**json.loads(sys.argv[2])), Path.cwd())
    except PermissionError as exc:
        result = ("permission", str(exc))
    except OSError as exc:
        result = ("missing_dependency", str(exc))
    print(json.dumps(result))
