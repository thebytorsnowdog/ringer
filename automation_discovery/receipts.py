"""Execute receipt contracts and merge validated receipts into the corpus.

The build boundary is split into two operations:

* receipts-check (run_build_check) is read-only with respect to corpus.db.
  It reads the chosen offer's receipt contract, executes each argv with
  shell=False from the deterministic build root, and writes complete
  receipt rows to the task directory's receipts.jsonl atomically.

* merge-build consumes a pre-staged receipts.jsonl and validates it against
  the chosen offer's contract before transactionally replacing that offer's
  receipt rows and marking it built. It never executes receipt commands.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from automation_discovery.db import Connection, DiscoveryError
from automation_discovery.utils import (
    atomic_write,
    is_iso_date_or_datetime,
    now_utc,
    open_read_only,
    resolve_path,
)

OUTPUT_LIMIT = 8 * 1024


def run_receipt(
    argv: list[str], cwd: Path, success_contains: str | None
) -> dict[str, object]:
    """Run one receipt argv with shell=False from cwd and return a result dict."""
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            shell=False,
            capture_output=True,
            text=True,
            timeout=120,
            stdin=subprocess.DEVNULL,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
    except FileNotFoundError as exc:
        return {
            "argv": argv,
            "exit_code": -1,
            "passed": False,
            "output_excerpt": f"command not found: {exc}",
        }
    except subprocess.TimeoutExpired:
        return {
            "argv": argv,
            "exit_code": -1,
            "passed": False,
            "output_excerpt": "timeout after 120s",
        }

    combined = (proc.stdout or "") + (proc.stderr or "")
    if len(combined) > OUTPUT_LIMIT:
        suffix = "\n[truncated]"
        keep = max(0, OUTPUT_LIMIT - len(suffix))
        combined = combined[:keep] + suffix
    passed = proc.returncode == 0
    if passed and success_contains and success_contains not in combined:
        passed = False
    return {
        "argv": argv,
        "exit_code": proc.returncode,
        "passed": passed,
        "output_excerpt": combined,
    }


def _read_chosen_offer_contract_ro(db_path: Path, offer_id: int) -> list[dict[str, Any]]:
    """Read the receipt contract of the chosen offer using a read-only connection."""
    with open_read_only(resolve_path(db_path)) as conn:
        row = conn.execute(
            "SELECT status, receipt_contract FROM offers WHERE id = ?", (offer_id,)
        ).fetchone()
        if row is None:
            raise DiscoveryError(f"offer {offer_id} not found")
        if row["status"] != "chosen":
            raise DiscoveryError(
                f"offer {offer_id} status is {row['status']!r}; must be 'chosen'"
            )
        contract = json.loads(row["receipt_contract"])
        return contract  # type: ignore[return-value]


def execute_receipt_contract(
    build_root: Path, contract: list[dict[str, Any]]
) -> tuple[list[dict[str, object]], bool]:
    """Execute contract (non-DB-mutating) and return rows with kind and all-passed."""
    build_root = resolve_path(build_root)
    results: list[dict[str, object]] = []
    all_passed = True
    for item in contract:
        argv = [str(a) for a in item["argv"]]
        result = run_receipt(argv, build_root, item.get("success_contains"))
        results.append(
            {
                "kind": item["kind"],
                "argv": result["argv"],
                "output_excerpt": result["output_excerpt"],
                "exit_code": result["exit_code"],
                "passed": result["passed"],
                "created_at": now_utc(),
            }
        )
        if not result["passed"]:
            all_passed = False
    return results, all_passed


def stage_receipts_for_offer(
    db_path: Path, offer_id: int, build_root: Path, receipts_jsonl: Path
) -> int:
    """Read contract (read-only), execute receipts, atomically write receipts.jsonl.

    This function never mutates corpus.db.
    """
    contract = _read_chosen_offer_contract_ro(db_path, offer_id)
    results, all_passed = execute_receipt_contract(build_root, contract)
    lines = [json.dumps(row, ensure_ascii=False) for row in results]
    atomic_write(receipts_jsonl, "\n".join(lines) + "\n" if lines else "")
    for result in results:
        status = "PASS" if result["passed"] else "FAIL"
        argv = " ".join(map(str, result["argv"]))
        print(f"{status}: {argv}")
        if not result["passed"]:
            print(result["output_excerpt"])
    return 0 if all_passed else 1


def run_build_check(
    db_path: Path, offer_id: int, build_root: Path, receipts_jsonl: Path
) -> int:
    """Execute receipts and write receipts.jsonl for the Ringer check.

    corpus.db is opened read-only; no rows are written or cleared.
    """
    return stage_receipts_for_offer(db_path, offer_id, build_root, receipts_jsonl)


def _load_receipts_jsonl(path: Path) -> list[dict[str, Any]]:
    """Load and parse receipts.jsonl, raising on malformed input."""
    if not path.is_file():
        raise DiscoveryError(f"receipts.jsonl not found: {path}")
    rows: list[dict[str, Any]] = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DiscoveryError(
                f"receipts.jsonl line {n}: malformed JSON: {exc}"
            ) from exc
        if not isinstance(row, dict):
            raise DiscoveryError(
                f"receipts.jsonl line {n}: receipt must be a JSON object"
            )
        rows.append(row)
    return rows


def _validate_staged_receipts_against_contract(
    contract: list[dict[str, Any]], staged: list[dict[str, Any]]
) -> list[str]:
    """Validate receipts.jsonl rows against the offer's contract. Return error list."""
    errors: list[str] = []

    expected_keys: dict[tuple[str | None, tuple[str, ...]], dict[str, Any]] = {}
    for item in contract:
        kind = item.get("kind")
        argv = tuple(str(a) for a in item.get("argv", []))
        if (kind, argv) in expected_keys:
            errors.append(f"contract contains duplicate kind {kind!r} with the same argv")
        expected_keys[(kind, argv)] = item

    seen_keys: set[tuple[str | None, tuple[str, ...]]] = set()
    for i, row in enumerate(staged):
        for field in ("kind", "argv", "output_excerpt", "exit_code", "passed", "created_at"):
            if field not in row:
                errors.append(f"staged receipt {i + 1}: missing field {field}")
                break

        kind = row.get("kind")
        if not isinstance(kind, str):
            errors.append(f"staged receipt {i + 1}: kind must be a string")

        argv = row.get("argv")
        if not isinstance(argv, list):
            errors.append(f"staged receipt {i + 1}: argv must be a JSON array")
        else:
            for j, arg in enumerate(argv):
                if not isinstance(arg, str):
                    errors.append(
                        f"staged receipt {i + 1}: argv[{j}] must be a string"
                    )

        output_excerpt = row.get("output_excerpt")
        if output_excerpt is not None and not isinstance(output_excerpt, str):
            errors.append(
                f"staged receipt {i + 1}: output_excerpt must be a string or null"
            )
        elif isinstance(output_excerpt, str) and len(output_excerpt) > OUTPUT_LIMIT:
            errors.append(
                f"staged receipt {i + 1}: output_excerpt exceeds {OUTPUT_LIMIT} character limit"
            )

        exit_code = row.get("exit_code")
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            errors.append(f"staged receipt {i + 1}: exit_code must be an integer")

        passed = row.get("passed")
        if not isinstance(passed, bool):
            errors.append(f"staged receipt {i + 1}: passed must be a boolean")

        created_at = row.get("created_at")
        if not isinstance(created_at, str):
            errors.append(
                f"staged receipt {i + 1}: created_at must be an ISO timestamp string"
            )
        elif not created_at or not is_iso_date_or_datetime(created_at):
            errors.append(
                f"staged receipt {i + 1}: created_at must be a valid ISO date/datetime"
            )

        key = (kind, tuple(str(a) for a in (argv or [])))
        if key in seen_keys:
            errors.append(f"staged receipt {i + 1}: duplicate receipt for kind={kind!r}")
        seen_keys.add(key)

        expected = expected_keys.get(key)
        if expected is None:
            # Missing/extra/unmatched receipts are summarized below.
            continue

        # argv must exactly match the contract's expected argv.
        expected_argv = [str(a) for a in expected.get("argv", [])]
        if list(key[1]) != expected_argv:
            errors.append(
                f"staged receipt {i + 1}: argv mismatch for kind={kind!r}"
            )

        if passed is False or exit_code != 0:
            errors.append(
                f"staged receipt {i + 1}: failed (passed={passed}, exit_code={exit_code})"
            )
            continue

        # success_contains is part of the receipt contract; if the staged
        # output doesn't contain it, the original check would have failed.
        success_contains = expected.get("success_contains")
        if success_contains and not isinstance(output_excerpt, str):
            errors.append(
                f"staged receipt {i + 1}: output_excerpt missing for success_contains check"
            )
        elif success_contains and success_contains not in output_excerpt:
            errors.append(
                f"staged receipt {i + 1}: output_excerpt missing success_contains "
                f"for kind={kind!r}"
            )

    staged_keys = {
        (row.get("kind"), tuple(str(a) for a in row.get("argv", []))) for row in staged
    }
    expected_key_set = set(expected_keys.keys())

    missing = expected_key_set - staged_keys
    if missing:
        missing_repr = ", ".join(f"kind={k!r}" for k, _ in missing)
        errors.append(f"missing staged receipts: {missing_repr}")

    extra = staged_keys - expected_key_set
    if extra:
        extra_repr = ", ".join(f"kind={k!r}" for k, _ in extra)
        errors.append(f"unexpected staged receipts: {extra_repr}")

    return errors


def _is_inside(path: Path, root: Path) -> bool:
    """Return True when resolved path is inside or equal to resolved root."""
    resolved = resolve_path(path)
    resolved_root = resolve_path(root)
    try:
        resolved.relative_to(resolved_root)
        return True
    except ValueError:
        return resolved == resolved_root


def merge_staged_receipts(
    db_path: Path, offer_id: int, task_dir: Path, write_root: Path
) -> int:
    """Validate receipts.jsonl and transactionally merge it into corpus.db.

    The offer must be in 'chosen' state. The task directory and deterministic
    build root must both reside inside write_root. On any rejection, corpus.db
    is left unchanged and the offer is not marked built.
    """
    db_path = resolve_path(db_path)
    task_dir = resolve_path(task_dir)
    write_root = resolve_path(write_root)
    build_root = resolve_path(write_root / "builds" / str(offer_id))

    if not _is_inside(task_dir, write_root):
        raise DiscoveryError(
            f"task_dir {task_dir} must be inside write_root {write_root}"
        )
    if not _is_inside(build_root, write_root):
        raise DiscoveryError(
            f"build_root {build_root} must be inside write_root {write_root}"
        )
    if not build_root.is_dir():
        raise DiscoveryError(f"build_root not found: {build_root}")

    receipts_jsonl = task_dir / "receipts.jsonl"
    staged = _load_receipts_jsonl(receipts_jsonl)

    # Acquire a single write connection and keep a transaction open for the
    # entire read-check-write sequence. BEGIN IMMEDIATE blocks concurrent
    # writers and closes the time-of-check/time-of-use gap: we re-read the
    # offer status and contract inside the transaction and only commit after
    # validating the staged rows against that contract. merge-build never
    # executes receipt commands, so no command runs while holding the lock.
    with Connection(db_path) as conn:
        conn.conn.execute("BEGIN IMMEDIATE")
        with conn.conn:
            offer = conn.conn.execute(
                "SELECT status, receipt_contract FROM offers WHERE id = ?",
                (offer_id,),
            ).fetchone()
            if offer is None:
                raise DiscoveryError(f"offer {offer_id} not found")
            if offer["status"] != "chosen":
                raise DiscoveryError(
                    f"offer {offer_id} status is {offer['status']!r}; must be 'chosen'"
                )
            contract = json.loads(offer["receipt_contract"])

            errors = _validate_staged_receipts_against_contract(contract, staged)
            if errors:
                raise DiscoveryError("invalid staged receipts: " + "; ".join(errors))

            conn.conn.execute(
                "DELETE FROM receipts WHERE offer_id = ?", (offer_id,)
            )
            for row in staged:
                conn.conn.execute(
                    """
                    INSERT INTO receipts
                        (offer_id, kind, argv, output_excerpt, exit_code, passed, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        offer_id,
                        row["kind"],
                        json.dumps(row["argv"]),
                        row.get("output_excerpt"),
                        row["exit_code"],
                        1 if row["passed"] else 0,
                        row["created_at"],
                    ),
                )
            created_at = now_utc()
            cursor = conn.conn.execute(
                """
                UPDATE offers
                SET status = 'built', built_at = ?, build_path = ?
                WHERE id = ? AND status = 'chosen'
                """,
                (created_at, str(build_root), offer_id),
            )
            if cursor.rowcount == 0:
                raise DiscoveryError(
                    f"offer {offer_id} concurrently changed from 'chosen'; build not applied"
                )

    return 0
