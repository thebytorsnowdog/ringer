#!/usr/bin/env python3
"""Replay tests for the Mission Fit gate.

Covers the normal, stale-source, approval-boundary and impossible-evidence
cases required by the approved Mission Fit control set. Uses only the Python
standard library and temporary directories. Run directly:

    python3 tests/test_mission_fit_gate.py
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest


REPO = pathlib.Path(__file__).resolve().parent.parent
GATE = REPO / "scripts" / "check_mission_fit_gate.py"
PY = sys.executable


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [PY, str(GATE), *args],
        text=True,
        capture_output=True,
        timeout=60,
    )


def _ok(cond: bool, message: str, failures: list[str]) -> None:
    if not cond:
        failures.append(message)


def _bind(failures: list[str]):
    def ok(cond: bool, message: str) -> None:
        _ok(cond, message, failures)
    return ok


def _write(path: pathlib.Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _sha(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _receipt(
    artifact_name: str,
    artifact_sha: str,
    *,
    reviewer: str = "viewer-user",
    reviewer_type: str = "human",
    reviewed_at: str = "2026-08-07T18:00:00+00:00",
    decision: str = "APPROVED",
    evidence: str = "I opened the viewer in a browser and inspected the bridge sample.",
    schema_version: str = "1.0",
) -> dict:
    return {
        "schema_version": schema_version,
        "artifact": artifact_name,
        "artifact_sha256": artifact_sha,
        "reviewer": reviewer,
        "reviewer_type": reviewer_type,
        "reviewed_at": reviewed_at,
        "decision": decision,
        "evidence": evidence,
    }


def test_deliverables(failures: list[str]) -> None:
    ok = _bind(failures)
    with tempfile.TemporaryDirectory() as td:
        task = pathlib.Path(td)
        _write(task / "notes.md", "## Notes\nThe deliverable is non-empty.\n")
        proc = _run("deliverables", "--task-dir", str(task), "--require", "notes.md")
        ok(proc.returncode == 0, f"normal non-empty notes should PASS (got {proc.returncode}: {proc.stdout})")
        ok(proc.stdout.startswith("PASS: task contract"), f"normal case should start with PASS: task contract (got {proc.stdout!r})")

        # Missing notes.
        proc = _run("deliverables", "--task-dir", str(task), "--require", "missing.md")
        ok(proc.returncode == 1, f"missing notes should NEEDS_CHANGE (got {proc.returncode})")
        ok(proc.stdout.startswith("NEEDS_CHANGE: task contract"), f"missing case should start with NEEDS_CHANGE (got {proc.stdout!r})")

        # Empty notes.
        _write(task / "notes.md", "")
        proc = _run("deliverables", "--task-dir", str(task), "--require", "notes.md")
        ok(proc.returncode == 1, f"empty notes should NEEDS_CHANGE (got {proc.returncode})")
        ok(proc.stdout.startswith("NEEDS_CHANGE: task contract"), f"empty case should start with NEEDS_CHANGE (got {proc.stdout!r})")

        # Path traversal rejected.
        _write(task / "notes.md", "non-empty\n")
        proc = _run("deliverables", "--task-dir", str(task), "--require", "../outside.txt")
        ok(proc.returncode == 1, f"traversal should NEEDS_CHANGE (got {proc.returncode})")
        ok(proc.stdout.startswith("NEEDS_CHANGE: task contract"), f"traversal case should start with NEEDS_CHANGE (got {proc.stdout!r})")

        # Absolute path rejected.
        proc = _run("deliverables", "--task-dir", str(task), "--require", str(task / "notes.md"))
        ok(proc.returncode == 1, f"absolute path should NEEDS_CHANGE (got {proc.returncode})")
        ok(proc.stdout.startswith("NEEDS_CHANGE: task contract"), f"absolute case should start with NEEDS_CHANGE (got {proc.stdout!r})")

        # Directory is not a file.
        (task / "subdir").mkdir()
        proc = _run("deliverables", "--task-dir", str(task), "--require", "subdir")
        ok(proc.returncode == 1, f"directory should NEEDS_CHANGE (got {proc.returncode})")


def test_promotion(failures: list[str]) -> None:
    ok = _bind(failures)
    with tempfile.TemporaryDirectory() as td:
        work = pathlib.Path(td)
        artifact = work / "bridge-asset-viewer.html"
        _write(artifact, "<!doctype html><html><body>artifact bytes</body></html>\n")
        artifact_name = artifact.name
        actual_sha = _sha(artifact)
        receipt_path = work / "human-review.json"

        # Missing review receipt -> BLOCKED.
        proc = _run("promotion", "--artifact", str(artifact), "--receipt", str(receipt_path))
        ok(proc.returncode == 2, f"missing receipt should BLOCKED (got {proc.returncode})")
        ok(proc.stdout.startswith("BLOCKED: promotion"), f"missing receipt should start BLOCKED (got {proc.stdout!r})")

        # Stale / wrong artifact SHA -> BLOCKED.
        _write(receipt_path, json.dumps(_receipt(artifact_name, "0" * 64)))
        proc = _run("promotion", "--artifact", str(artifact), "--receipt", str(receipt_path))
        ok(proc.returncode == 2, f"stale SHA should BLOCKED (got {proc.returncode})")
        ok(proc.stdout.startswith("BLOCKED: promotion"), f"stale SHA should start BLOCKED (got {proc.stdout!r})")

        # PENDING receipt -> BLOCKED.
        _write(receipt_path, json.dumps(_receipt(artifact_name, actual_sha, decision="PENDING")))
        proc = _run("promotion", "--artifact", str(artifact), "--receipt", str(receipt_path))
        ok(proc.returncode == 2, f"PENDING should BLOCKED (got {proc.returncode})")
        ok(proc.stdout.startswith("BLOCKED: promotion"), f"PENDING should start BLOCKED (got {proc.stdout!r})")

        # Non-human reviewer -> BLOCKED.
        _write(receipt_path, json.dumps(_receipt(artifact_name, actual_sha, reviewer_type="ai")))
        proc = _run("promotion", "--artifact", str(artifact), "--receipt", str(receipt_path))
        ok(proc.returncode == 2, f"non-human reviewer should BLOCKED (got {proc.returncode})")
        ok(proc.stdout.startswith("BLOCKED: promotion"), f"non-human should start BLOCKED (got {proc.stdout!r})")

        # Correct human APPROVED receipt matching the current artifact -> READY.
        _write(receipt_path, json.dumps(_receipt(artifact_name, actual_sha)))
        proc = _run("promotion", "--artifact", str(artifact), "--receipt", str(receipt_path))
        ok(proc.returncode == 0, f"correct receipt should READY (got {proc.returncode}: {proc.stdout})")
        ok(proc.stdout.startswith("READY: promotion"), f"correct receipt should start READY (got {proc.stdout!r})")

        # Malformed JSON -> BLOCKED.
        _write(receipt_path, "{not json")
        proc = _run("promotion", "--artifact", str(artifact), "--receipt", str(receipt_path))
        ok(proc.returncode == 2, f"malformed JSON should BLOCKED (got {proc.returncode})")

        # Impossible approval-evidence: an APPROVED receipt whose evidence is
        # internally impossible (artifact_sha256 has the wrong shape so it can
        # never match any real file's SHA-256). The only passing result here is
        # BLOCKED; a gate that returned READY would be a false success.
        _write(
            receipt_path,
            json.dumps(
                _receipt(
                    artifact_name,
                    "impossible-not-a-sha",
                    evidence="claims approval with impossible evidence",
                )
            ),
        )
        proc = _run("promotion", "--artifact", str(artifact), "--receipt", str(receipt_path))
        ok(proc.returncode == 2, f"impossible approval-evidence should BLOCKED (got {proc.returncode})")
        ok(proc.stdout.startswith("BLOCKED: promotion"), f"impossible evidence should start BLOCKED (got {proc.stdout!r})")

        # Confirm the gate never created human-review.json inside the repo.
        repo_receipt = REPO / ".ringer-work" / "bridge-asset-viewer" / "human-review.json"
        ok(not repo_receipt.exists(), "the gate must not create human-review.json inside the repository")



class MissionFitGateTests(unittest.TestCase):
    """Run the replay under unittest discovery as well as directly."""

    def test_gate_replay(self) -> None:
        failures: list[str] = []
        test_deliverables(failures)
        test_promotion(failures)
        self.assertEqual([], failures)


def main() -> int:
    failures: list[str] = []
    test_deliverables(failures)
    test_promotion(failures)
    if failures:
        print("FAIL: Mission Fit gate replay")
        for failure in failures:
            print(" -", failure)
        return 1
    print("PASS: replay cases passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
