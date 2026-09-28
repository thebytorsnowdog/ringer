#!/usr/bin/env python3
"""Mission Fit gate.

Two read-only subcommands implemented with the Python standard library only.

``deliverables --task-dir DIR --require RELATIVE_PATH``
    Verify the task-contract deliverables named on the command line. Each
    ``--require`` path must be a non-empty file inside ``--task-dir``. Absolute
    paths, traversal, missing paths, non-files and empty files are
    ``NEEDS_CHANGE: task contract`` and exit 1. A clean check is
    ``PASS: task contract`` and exits 0.

``promotion --artifact FILE --receipt FILE``
    Validate a JSON human-review receipt bound to the current artifact. The
    receipt must be a JSON object with ``schema_version == "1.0"``,
    ``artifact`` equal to the artifact file name, ``artifact_sha256`` equal to
    the current file SHA-256, a non-empty ``reviewer``, ``reviewer_type``
    exactly ``human``, a valid RFC 3339 ``reviewed_at`` with a timezone,
    ``decision`` exactly ``APPROVED`` and non-empty ``evidence``. Missing,
    unreadable, malformed, incomplete, PENDING or rejected receipts are
    ``BLOCKED: promotion`` and exit 2. A valid receipt is ``READY: promotion``
    and exits 0. This script never creates ``human-review.json``.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import pathlib
import re
import sys
from typing import Iterable


def _fail_task(message: str) -> int:
    print(f"NEEDS_CHANGE: task contract - {message}")
    return 1


def _ok_task(message: str) -> int:
    print(f"PASS: task contract - {message}")
    return 0


def _blocked(message: str) -> int:
    print(f"BLOCKED: promotion - {message}")
    return 2


def _ready(message: str) -> int:
    print(f"READY: promotion - {message}")
    return 0


def _check_deliverable(task_dir: pathlib.Path, rel: str) -> str | None:
    """Return None if ``rel`` is an acceptable deliverable, else a reason."""
    if os.path.isabs(rel):
        return f"absolute path is not a task-relative deliverable: {rel}"
    parts = pathlib.PurePath(rel).parts
    if ".." in parts:
        return f"deliverable path contains a traversal component: {rel}"
    if not rel or rel in (".",):
        return "deliverable path is empty"
    target = (task_dir / rel)
    try:
        resolved = target.resolve(strict=False)
    except OSError:
        return f"deliverable path could not be resolved: {rel}"
    try:
        resolved.relative_to(task_dir.resolve(strict=False))
    except ValueError:
        return f"deliverable escapes the task directory: {rel}"
    if not resolved.exists():
        return f"required deliverable is missing: {rel}"
    if not resolved.is_file():
        return f"required deliverable is not a file: {rel}"
    try:
        size = resolved.stat().st_size
    except OSError:
        return f"required deliverable could not be read: {rel}"
    if size == 0:
        return f"required deliverable is empty: {rel}"
    return None


def cmd_deliverables(args: argparse.Namespace) -> int:
    task_dir = pathlib.Path(args.task_dir)
    if not task_dir.is_dir():
        return _fail_task(f"task directory does not exist: {args.task_dir}")
    required: list[str] = list(args.require or [])
    if not required:
        return _fail_task("no required deliverable paths were supplied")
    seen: set[str] = set()
    for rel in required:
        if rel in seen:
            continue
        seen.add(rel)
        problem = _check_deliverable(task_dir, rel)
        if problem is not None:
            return _fail_task(problem)
    return _ok_task(f"{len(required)} required deliverable(s) verified in {task_dir}")


_RFC3339 = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?"
    r"(?:[Zz]|[+-]\d{2}:\d{2})$"
)


def _is_rfc3339_with_tz(value: object) -> bool:
    if not isinstance(value, str) or not _RFC3339.match(value):
        return False
    # An explicit timezone offset is required. Accept a trailing Z/z or a
    # +/-HH:MM offset. A bare local timestamp (no offset) is rejected.
    if value.endswith(("Z", "z")):
        return True
    return bool(re.search(r"[+-]\d{2}:\d{2}$", value))


def _parse_rfc3339(value: str) -> bool:
    """Best-effort cross-check that the timestamp is calendar-valid."""
    try:
        datetime.datetime.fromisoformat(value.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        return False
    return True


def cmd_promotion(args: argparse.Namespace) -> int:
    artifact_path = pathlib.Path(args.artifact)
    receipt_path = pathlib.Path(args.receipt)

    if not receipt_path.exists():
        return _blocked("review receipt is missing")
    if not receipt_path.is_file():
        return _blocked("review receipt is not a file")
    try:
        raw = receipt_path.read_bytes()
    except OSError:
        return _blocked("review receipt is unreadable")
    try:
        receipt = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        return _blocked("review receipt is not valid UTF-8")
    except json.JSONDecodeError:
        return _blocked("review receipt is malformed JSON")
    if not isinstance(receipt, dict):
        return _blocked("review receipt is not a JSON object")

    if receipt.get("schema_version") != "1.0":
        return _blocked("schema_version must be exactly '1.0'")

    if not artifact_path.is_file():
        return _blocked("artifact file is missing")
    artifact_name = artifact_path.name
    if receipt.get("artifact") != artifact_name:
        return _blocked(f"artifact must equal the artifact file name ('{artifact_name}')")

    try:
        actual_sha = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    except OSError:
        return _blocked("artifact file could not be read for SHA-256")
    if receipt.get("artifact_sha256") != actual_sha:
        return _blocked("artifact_sha256 does not match the current artifact SHA-256")

    reviewer = receipt.get("reviewer")
    if not isinstance(reviewer, str) or not reviewer.strip():
        return _blocked("reviewer is missing or empty")

    if receipt.get("reviewer_type") != "human":
        return _blocked("reviewer_type must be exactly 'human'")

    reviewed_at = receipt.get("reviewed_at")
    if not _is_rfc3339_with_tz(reviewed_at) or not _parse_rfc3339(reviewed_at):
        return _blocked("reviewed_at must be a valid RFC 3339 timestamp with a timezone")

    decision = receipt.get("decision")
    if decision == "PENDING":
        return _blocked("review decision is still PENDING")
    if decision != "APPROVED":
        return _blocked("review decision must be exactly 'APPROVED'")

    evidence = receipt.get("evidence")
    if not isinstance(evidence, str) or not evidence.strip():
        return _blocked("evidence is missing or empty")

    return _ready(
        f"human review APPROVED for {artifact_name} (sha256 {actual_sha[:12]})"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="check_mission_fit_gate",
        description="Mission Fit task-contract and promotion gate (standard library only).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    deliverables = sub.add_parser(
        "deliverables",
        help="verify task-contract deliverables are present and non-empty",
    )
    deliverables.add_argument("--task-dir", required=True, help="task scratch directory")
    deliverables.add_argument(
        "--require",
        action="append",
        default=[],
        metavar="RELATIVE_PATH",
        help="task-relative deliverable path (may be repeated)",
    )
    deliverables.set_defaults(func=cmd_deliverables)

    promotion = sub.add_parser(
        "promotion",
        help="validate a human-review receipt bound to an artifact SHA-256",
    )
    promotion.add_argument("--artifact", required=True, help="artifact file to bind the receipt to")
    promotion.add_argument("--receipt", required=True, help="human-review JSON receipt file")
    promotion.set_defaults(func=cmd_promotion)

    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
