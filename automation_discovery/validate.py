"""Validation for staged worker output and the merged corpus."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from automation_discovery.utils import (
    EXCERPT_MAX_LEN,
    is_iso_date_or_datetime,
    open_read_only,
    resolve_path,
)


EVENT_FIELDS = (
    "source_ref",
    "occurred_at",
    "actor",
    "action",
    "artifact",
    "excerpt",
    "job_hint",
)
REQUIRED_EVENT_FIELDS = ("source_ref", "action")
SUMMARY_FIELDS = {"status", "coverage_start", "coverage_end", "notes"}


def validate_staged_events(events_path: Path, min_rows: int) -> list[str]:
    """Validate a worker-staged events.jsonl and return printable FAIL lines."""
    errors: list[str] = []
    try:
        text = events_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [f"FAIL: {events_path} not found — worker staged nothing"]
    except OSError as exc:
        return [f"FAIL: cannot read {events_path}: {exc}"]

    lines = [line for line in text.splitlines() if line.strip()]
    for n, line in enumerate(lines, 1):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"line {n}: not valid JSON ({exc})")
            continue
        if not isinstance(row, dict):
            errors.append(f"line {n}: not an object")
            continue
        for field in REQUIRED_EVENT_FIELDS:
            if not row.get(field):
                errors.append(f"line {n}: missing/empty required field '{field}'")
        unknown = set(row) - set(EVENT_FIELDS)
        if unknown:
            errors.append(f"line {n}: unknown fields {sorted(unknown)}")
        excerpt = row.get("excerpt")
        if isinstance(excerpt, str) and len(excerpt) > EXCERPT_MAX_LEN:
            errors.append(
                f"line {n}: excerpt {len(excerpt)} chars > {EXCERPT_MAX_LEN}"
            )
    if len(lines) < min_rows:
        errors.append(
            f"only {len(lines)} rows staged; need >= {min_rows}. "
            "If the surface honestly yielded fewer, record a failed source."
        )
    return errors


def load_staged_summary(task_dir: Path) -> dict[str, object]:
    summary_path = task_dir / "source_summary.json"
    if not summary_path.is_file():
        return {}
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if not isinstance(summary, dict):
        raise ValueError("source_summary.json must be a JSON object")
    unknown = set(summary) - SUMMARY_FIELDS
    if unknown:
        raise ValueError(f"source_summary.json unknown fields: {sorted(unknown)}")
    if "status" in summary and summary["status"] not in ("ingested", "failed"):
        raise ValueError("source_summary.json status must be 'ingested' or 'failed'")
    return summary


AUDIT_NOTE_FIELDS = {"scope", "finding", "severity", "resolved"}
REQUIRED_AUDIT_FIELDS = ("scope", "finding", "severity")
SEVERITY_VALUES = {"info", "warn", "block"}


def validate_staged_notes(notes_path: Path, min_rows: int) -> list[str]:
    """Validate a worker-staged audit_notes.jsonl."""
    try:
        text = notes_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [f"FAIL: {notes_path} not found — worker staged nothing"]
    except OSError as exc:
        return [f"FAIL: cannot read {notes_path}: {exc}"]

    errors: list[str] = []
    lines = [line for line in text.splitlines() if line.strip()]
    for n, line in enumerate(lines, 1):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"line {n}: not valid JSON ({exc})")
            continue
        if not isinstance(row, dict):
            errors.append(f"line {n}: not an object")
            continue
        for field in REQUIRED_AUDIT_FIELDS:
            if not row.get(field):
                errors.append(f"line {n}: missing/empty required field '{field}'")
        if row.get("severity") and row["severity"] not in SEVERITY_VALUES:
            errors.append(
                f"line {n}: severity must be one of {SEVERITY_VALUES}, got {row['severity']!r}"
            )
        unknown = set(row) - AUDIT_NOTE_FIELDS
        if unknown:
            errors.append(f"line {n}: unknown fields {sorted(unknown)}")
    if len(lines) < min_rows:
        errors.append(f"only {len(lines)} audit note(s) staged; need >= {min_rows}")
    return errors


def validate_staging(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate staged events.jsonl before merging into corpus.db."
    )
    parser.add_argument("events_file", help="Path to events.jsonl")
    parser.add_argument(
        "--min-rows", type=int, default=10,
        help="Fail below this many staged events"
    )
    parser.add_argument(
        "--quiet", action="store_true", help="Only print failures"
    )
    args = parser.parse_args(argv)
    errors = validate_staged_events(resolve_path(args.events_file), args.min_rows)
    if errors:
        for error in errors[:20]:
            print(f"FAIL: {error}")
        if len(errors) > 20:
            print(f"... and {len(errors) - 20} more")
        return 1
    if not args.quiet:
        print("OK: staged events validated")
    return 0


def validate_audit_staging(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate staged audit_notes.jsonl before merging into corpus.db."
    )
    parser.add_argument("notes_file", help="Path to audit_notes.jsonl")
    parser.add_argument(
        "--min-rows", type=int, default=1,
        help="Fail below this many staged audit notes"
    )
    args = parser.parse_args(argv)
    errors = validate_staged_notes(resolve_path(args.notes_file), args.min_rows)
    if errors:
        for error in errors[:20]:
            print(f"FAIL: {error}")
        if len(errors) > 20:
            print(f"... and {len(errors) - 20} more")
        return 1
    print("OK: staged audit notes validated")
    return 0


# ---------------------------------------------------------------------------
# Corpus-level validation
# ---------------------------------------------------------------------------

REQUIRED_TABLES = {
    "meta",
    "sources",
    "events",
    "candidates",
    "audit_notes",
    "offers",
    "receipts",
}


def validate_events(conn: sqlite3.Connection) -> list[str]:
    errors: list[str] = []
    rows = conn.execute(
        """
        SELECT e.id, e.source_id, e.source_ref, e.occurred_at, e.excerpt, s.id AS existing_source_id
        FROM events AS e
        LEFT JOIN sources AS s ON s.id = e.source_id
        ORDER BY e.id
        """
    ).fetchall()
    for row in rows:
        label = f"events.id={row['id']}"
        if row["existing_source_id"] is None:
            errors.append(f"FAIL [{label}] source_id {row['source_id']} has no sources row")
        if not isinstance(row["source_ref"], str) or not row["source_ref"].strip():
            errors.append(f"FAIL [{label}] source_ref is empty")
        excerpt = row["excerpt"]
        if excerpt is not None:
            if not isinstance(excerpt, str):
                errors.append(f"FAIL [{label}] excerpt is not text")
            elif len(excerpt) > EXCERPT_MAX_LEN:
                errors.append(
                    f"FAIL [{label}] excerpt {len(excerpt)} chars exceeds {EXCERPT_MAX_LEN}"
                )
        occurred_at = row["occurred_at"]
        if occurred_at is not None and not is_iso_date_or_datetime(occurred_at):
            errors.append(f"FAIL [{label}] occurred_at={occurred_at!r} is not ISO")
    return errors


def validate_sources(conn: sqlite3.Connection) -> list[str]:
    errors: list[str] = []
    rows = conn.execute(
        """
        SELECT s.id, s.ingested_at, s.lane, s.notes, COUNT(e.id) AS event_count
        FROM sources AS s
        LEFT JOIN events AS e ON e.source_id = s.id
        WHERE s.status = 'ingested'
        GROUP BY s.id
        ORDER BY s.id
        """
    ).fetchall()
    for row in rows:
        label = f"sources.id={row['id']}"
        if not isinstance(row["ingested_at"], str) or not row["ingested_at"].strip():
            errors.append(f"FAIL [{label}] ingested_at is empty")
        if not isinstance(row["lane"], str) or not row["lane"].strip():
            errors.append(f"FAIL [{label}] lane is empty")
        if row["event_count"] == 0 and not row["notes"]:
            errors.append(f"FAIL [{label}] has 0 events and no explanatory notes")
    return errors


def validate_candidates(conn: sqlite3.Connection) -> list[str]:
    errors: list[str] = []
    existing_ids = {r["id"] for r in conn.execute("SELECT id FROM events").fetchall()}
    rows = conn.execute("SELECT id, event_ids FROM candidates ORDER BY id").fetchall()
    for row in rows:
        label = f"candidates.id={row['id']}"
        try:
            parsed = json.loads(row["event_ids"])
        except (json.JSONDecodeError, TypeError) as exc:
            errors.append(f"FAIL [{label}] event_ids is not valid JSON ({exc})")
            continue
        if not isinstance(parsed, list):
            errors.append(f"FAIL [{label}] event_ids is not an array")
            continue
        valid = [v for v in parsed if isinstance(v, int) and not isinstance(v, bool)]
        if len(valid) < 3 or len(valid) != len(parsed) or len(set(valid)) != len(valid):
            errors.append(
                f"FAIL [{label}] event_ids must be >=3 distinct integer event IDs"
            )
            continue
        missing = [v for v in valid if v not in existing_ids]
        if missing:
            errors.append(f"FAIL [{label}] references missing event IDs {missing}")
    return errors


def validate_offers(conn: sqlite3.Connection) -> list[str]:
    errors: list[str] = []
    rows = conn.execute(
        "SELECT id, evidence_query, receipt_contract FROM offers ORDER BY id"
    ).fetchall()
    for row in rows:
        label = f"offers.id={row['id']}"
        if not row["receipt_contract"] or not str(row["receipt_contract"]).strip():
            errors.append(f"FAIL [{label}] receipt_contract is empty")
            continue
        try:
            contract = json.loads(row["receipt_contract"])
        except json.JSONDecodeError as exc:
            errors.append(f"FAIL [{label}] receipt_contract is not valid JSON ({exc})")
            continue
        if not isinstance(contract, list) or not (3 <= len(contract) <= 5):
            errors.append(
                f"FAIL [{label}] receipt_contract must contain 3 to 5 objects"
            )
        else:
            for item in contract:
                if not isinstance(item, dict) or "kind" not in item or "argv" not in item:
                    errors.append(
                        f"FAIL [{label}] receipt {item!r} missing kind or argv"
                    )
        query = row["evidence_query"]
        if query:
            q = query.strip().lower()
            if not (q.startswith("select") or q.startswith("with")):
                errors.append(f"FAIL [{label}] evidence_query must be SELECT or WITH")
            elif not all(tok not in q for tok in ("insert", "update", "delete", "drop", "alter", "create", "replace", "attach", "detach", "pragma")):
                errors.append(f"FAIL [{label}] evidence_query contains write operations")
    return errors


def validate_corpus(db_path: Path, source_id: int | None = None) -> list[str]:
    errors: list[str] = []
    with open_read_only(db_path) as conn:
        present = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        missing = sorted(REQUIRED_TABLES - present)
        if missing:
            errors.append(f"FAIL [schema] missing tables: {', '.join(missing)}")
            return errors
        errors.extend(validate_events(conn))
        errors.extend(validate_sources(conn))
        errors.extend(validate_candidates(conn))
        errors.extend(validate_offers(conn))
        if source_id is not None:
            row = conn.execute(
                "SELECT status, notes FROM sources WHERE id = ?", (source_id,)
            ).fetchone()
            if row is None:
                errors.append(f"FAIL source {source_id} not found")
            elif row["status"] == "planned":
                errors.append(f"FAIL [sources.id={source_id}] still planned")
    return errors


def run_corpus_validation(db_path: Path, source_id: int | None = None) -> int:
    if not db_path.is_file():
        print(f"Error: {db_path} does not exist", file=sys.stderr)
        return 2
    errors = validate_corpus(db_path, source_id)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        print(f"Validation failed with {len(errors)} issue(s).", file=sys.stderr)
        return 1
    print("OK: corpus validation passed")
    return 0


def main_validate(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate an automation-discovery corpus.")
    parser.add_argument("db_path", help="Path to corpus.db")
    parser.add_argument("--source", type=int, help="Restrict checks to one source")
    args = parser.parse_args(argv)
    return run_corpus_validation(resolve_path(args.db_path), args.source)


if __name__ == "__main__":
    raise SystemExit(main_validate())
