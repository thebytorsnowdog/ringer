"""Corpus database access layer."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from automation_discovery.schema import SCHEMA, SCHEMA_VERSION
from automation_discovery.utils import (
    EXCERPT_MAX_LEN,
    is_iso_date_or_datetime,
    now_utc,
    open_read_only,
    resolve_path,
    valid_candidate_status,
    valid_kind,
    valid_offer_status,
    valid_overlap,
    valid_severity,
    valid_source_status,
)


class DiscoveryError(Exception):
    """Raised for expected business-logic errors with a user-facing message."""


def schema_version() -> str:
    """Return schema version."""
    return SCHEMA_VERSION


def remove_database_files(db_path: Path) -> None:
    """Remove SQLite database and its WAL sidecars."""
    for path in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if path.exists() or path.is_symlink():
            path.unlink()


def initialize_database(write_root: Path, force: bool = False) -> Path:
    """Create and initialize corpus.db in write_root."""
    try:
        write_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DiscoveryError(f"cannot create write root {write_root}: {exc}") from exc

    db_path = write_root / "corpus.db"
    if db_path.exists() or db_path.is_symlink():
        if not force:
            raise DiscoveryError(
                f"{db_path} already exists. Use --force to replace it."
            )
        remove_database_files(db_path)

    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA)
        conn.executemany(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (("schema_version", schema_version()), ("created_at", now_utc())),
        )
        conn.commit()
        conn.close()
    except (OSError, sqlite3.Error) as exc:
        try:
            conn.close()  # type: ignore[has-type]
        except Exception:
            pass
        try:
            remove_database_files(db_path)
        except OSError:
            pass
        raise DiscoveryError(f"could not initialize {db_path}: {exc}") from exc

    return db_path


class Connection:
    """Thin wrapper around a sqlite3 connection with common helpers."""

    def __init__(self, db_path: Path):
        self.db_path = resolve_path(db_path)
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.row_factory = sqlite3.Row

    def close(self) -> None:
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    # metadata ---------------------------------------------------------------

    def required_meta(self, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value)
            )

    def invalidate_approval(self) -> None:
        """Clear source approval whenever the plan changes."""
        with self.conn:
            self.conn.execute("DELETE FROM meta WHERE key = 'sources_approved_at'")

    def get_approved_at(self) -> str | None:
        return self.required_meta("sources_approved_at")

    def set_approved_at(self) -> None:
        self.set_meta("sources_approved_at", now_utc())

    # sources ----------------------------------------------------------------

    def add_source(
        self,
        surface: str,
        kind: str,
        access_route: str,
        coverage_start: str | None = None,
        coverage_end: str | None = None,
        notes: str | None = None,
    ) -> int:
        if not valid_kind(kind):
            raise DiscoveryError(
                f"invalid source kind {kind!r}; choose one of: ai-history, communication, "
                "work-tracking, knowledge, execution, other"
            )
        if coverage_start and not is_iso_date_or_datetime(coverage_start):
            raise DiscoveryError("coverage_start must be an ISO date or datetime")
        if coverage_end and not is_iso_date_or_datetime(coverage_end):
            raise DiscoveryError("coverage_end must be an ISO date or datetime")
        cursor = self.conn.execute(
            """
            INSERT INTO sources (surface, kind, access_route, coverage_start, coverage_end, status, notes)
            VALUES (?, ?, ?, ?, ?, 'planned', ?)
            """,
            (surface, kind, access_route, coverage_start, coverage_end, notes),
        )
        self.invalidate_approval()
        self.conn.commit()
        return int(cursor.lastrowid)

    def exclude_source(self, source_id: int) -> bool:
        cursor = self.conn.execute(
            "UPDATE sources SET status = 'excluded' WHERE id = ?", (source_id,)
        )
        if cursor.rowcount == 0:
            raise DiscoveryError(f"source {source_id} not found")
        self.invalidate_approval()
        self.conn.commit()
        return True

    def get_source(self, source_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM sources WHERE id = ?", (source_id,)
        ).fetchone()

    def planned_sources(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM sources WHERE status = 'planned' ORDER BY id"
        ).fetchall()

    def all_sources(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM sources ORDER BY id").fetchall()

    def source_plan_summary(self) -> dict[str, Any]:
        rows = self.all_sources()
        approved_at = self.get_approved_at()
        return {
            "approved": approved_at is not None,
            "approved_at": approved_at,
            "count": len(rows),
            "planned": [dict(r) for r in rows if r["status"] == "planned"],
            "excluded": [dict(r) for r in rows if r["status"] == "excluded"],
            "ingested": [dict(r) for r in rows if r["status"] == "ingested"],
            "failed": [dict(r) for r in rows if r["status"] == "failed"],
        }

    # audit notes ------------------------------------------------------------

    def add_existing_automation(
        self, scope: str, finding: str, severity: str = "info", resolved: bool = False
    ) -> int:
        if not valid_severity(severity):
            raise DiscoveryError(
                f"invalid severity {severity!r}; choose info, warn, or block"
            )
        cursor = self.conn.execute(
            "INSERT INTO audit_notes (scope, finding, severity, resolved) VALUES (?, ?, ?, ?)",
            (scope, finding, severity, 1 if resolved else 0),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def unresolved_blocks(self, scope_prefix: str = "") -> list[sqlite3.Row]:
        sql = "SELECT * FROM audit_notes WHERE resolved = 0 AND severity = 'block'"
        params: tuple = ()
        if scope_prefix:
            sql += " AND scope LIKE ?"
            params = (f"{scope_prefix}%",)
        return self.conn.execute(sql, params).fetchall()

    def all_audit_notes(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM audit_notes ORDER BY id"
        ).fetchall()

    # events -----------------------------------------------------------------

    def insert_event(self, source_id: int, event: dict[str, Any]) -> int:
        row = self.get_source(source_id)
        if row is None:
            raise DiscoveryError(f"source {source_id} not found")
        excerpt = event.get("excerpt")
        if isinstance(excerpt, str) and len(excerpt) > EXCERPT_MAX_LEN:
            raise DiscoveryError(
                f"excerpt is {len(excerpt)} chars; max is {EXCERPT_MAX_LEN}"
            )
        cursor = self.conn.execute(
            """
            INSERT INTO events (source_id, source_ref, occurred_at, actor, action, artifact, excerpt, job_hint)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_id,
                event.get("source_ref"),
                event.get("occurred_at"),
                event.get("actor"),
                event.get("action"),
                event.get("artifact"),
                excerpt,
                event.get("job_hint"),
            ),
        )
        return int(cursor.lastrowid)

    def insert_events(self, source_id: int, events: list[dict[str, Any]]) -> int:
        count = 0
        with self.conn:
            for event in events:
                self.insert_event(source_id, event)
                count += 1
        return count

    def update_source_after_ingest(
        self,
        source_id: int,
        status: str,
        lane: str,
        ingested_at: str,
        coverage_start: str | None,
        coverage_end: str | None,
        notes: str | None,
    ) -> None:
        if not valid_source_status(status):
            raise DiscoveryError(f"invalid source status {status!r}")
        with self.conn:
            self.conn.execute(
                """
                UPDATE sources
                SET status = ?, lane = ?, ingested_at = ?,
                    coverage_start = COALESCE(?, coverage_start),
                    coverage_end = COALESCE(?, coverage_end),
                    notes = COALESCE(?, notes)
                WHERE id = ?
                """,
                (
                    status,
                    lane,
                    ingested_at,
                    coverage_start,
                    coverage_end,
                    notes,
                    source_id,
                ),
            )

    def event_exists(self, event_id: int) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM events WHERE id = ?", (event_id,)
        ).fetchone()
        return row is not None

    def events_for_source(self, source_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM events WHERE source_id = ? ORDER BY id", (source_id,)
        ).fetchall()

    # candidates -------------------------------------------------------------

    def insert_candidate(self, candidate: dict[str, Any]) -> int:
        event_ids = candidate["event_ids"]
        if isinstance(event_ids, list):
            event_ids = json.dumps(event_ids)
        parsed = json.loads(event_ids)
        if (
            not isinstance(parsed, list)
            or len(parsed) < 3
            or len(set(parsed)) != len(parsed)
        ):
            raise DiscoveryError(
                "candidate must reference at least 3 distinct event IDs"
            )
        missing = [eid for eid in parsed if not self.event_exists(eid)]
        if missing:
            raise DiscoveryError(
                f"candidate references missing event IDs: {missing}"
            )
        overlap = candidate.get("overlap", "unknown")
        if not valid_overlap(overlap):
            raise DiscoveryError(f"invalid overlap {overlap!r}")
        cursor = self.conn.execute(
            """
            INSERT INTO candidates (job, event_ids, independence_note, consequence, overlap, status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                candidate["job"],
                event_ids,
                candidate["independence_note"],
                candidate.get("consequence"),
                overlap,
                candidate.get("status", "proposed"),
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def all_candidates(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM candidates ORDER BY id").fetchall()

    def get_candidate(self, candidate_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM candidates WHERE id = ?", (candidate_id,)
        ).fetchone()

    # offers -----------------------------------------------------------------

    def insert_offer(self, offer: dict[str, Any]) -> int:
        candidate_id = offer["candidate_id"]
        candidate = self.get_candidate(candidate_id)
        if candidate is None:
            raise DiscoveryError(f"candidate {candidate_id} not found")
        contract = offer.get("receipt_contract")
        if isinstance(contract, list):
            contract = json.dumps(contract)
        self._validate_receipt_contract(contract)
        if offer.get("evidence_query"):
            self._validate_evidence_query(offer["evidence_query"])
        cursor = self.conn.execute(
            """
            INSERT INTO offers (candidate_id, title, what_it_does, evidence_query, run_cost, receipt_contract, status)
            VALUES (?, ?, ?, ?, ?, ?, 'offered')
            """,
            (
                candidate_id,
                offer["title"],
                offer["what_it_does"],
                offer.get("evidence_query"),
                offer.get("run_cost"),
                contract,
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def _validate_receipt_contract(self, contract: str) -> None:
        try:
            parsed = json.loads(contract)
        except json.JSONDecodeError as exc:
            raise DiscoveryError(
                f"receipt_contract must be valid JSON: {exc}"
            ) from exc
        if not isinstance(parsed, list):
            raise DiscoveryError("receipt_contract must be a JSON array")
        if not (3 <= len(parsed) <= 5):
            raise DiscoveryError(
                "receipt_contract must contain 3 to 5 receipt objects"
            )
        for item in parsed:
            if not isinstance(item, dict):
                raise DiscoveryError("each receipt must be a JSON object")
            if "kind" not in item or "argv" not in item:
                raise DiscoveryError("each receipt must have 'kind' and 'argv'")
            if not isinstance(item["argv"], list):
                raise DiscoveryError("receipt argv must be a JSON array")
            if any(isinstance(a, (dict, list)) for a in item["argv"]):
                raise DiscoveryError("receipt argv entries must be scalars")

    def _validate_evidence_query(self, query: str) -> None:
        """Ensure evidence_query is a read-only SELECT/WITH that executes."""
        q = query.strip().lower()
        forbidden = (
            "insert", "update", "delete", "drop", "alter", "create", "replace",
            "attach", "detach", "pragma", " Vacuum", "reindex",
        )
        if not (q.startswith("select") or q.startswith("with")):
            raise DiscoveryError("evidence_query must begin with SELECT or WITH")
        if any(tok in q for tok in forbidden):
            raise DiscoveryError(
                "evidence_query may only contain read-only SELECT/WITH clauses"
            )
        with open_read_only(self.db_path) as conn:
            try:
                conn.execute(query)
            except sqlite3.Error as exc:
                raise DiscoveryError(
                    f"evidence_query failed to execute: {exc}"
                ) from exc

    def all_offers(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM offers ORDER BY id"
        ).fetchall()

    def offered_or_chosen_offers(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM offers WHERE status IN ('offered','chosen') ORDER BY id"
        ).fetchall()

    def get_offer(self, offer_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM offers WHERE id = ?", (offer_id,)
        ).fetchone()

    def choose_offer(self, offer_id: int) -> None:
        offer = self.get_offer(offer_id)
        if offer is None:
            raise DiscoveryError(f"offer {offer_id} not found")
        if offer["status"] not in ("offered", "chosen"):
            raise DiscoveryError(
                f"offer {offer_id} status is {offer['status']}; can only choose from offered rows"
            )
        unresolved = self.unresolved_blocks("candidate:") + self.unresolved_blocks("corpus")
        if unresolved:
            raise DiscoveryError(
                f"cannot choose offer while {len(unresolved)} unresolved block(s) exist"
            )
        with self.conn:
            self.conn.execute(
                "UPDATE offers SET status = 'declined' WHERE status = 'offered' AND id != ?",
                (offer_id,),
            )
            self.conn.execute(
                "UPDATE offers SET status = 'chosen', chosen_at = ? WHERE id = ?",
                (now_utc(), offer_id),
            )

    def mark_offer_built(
        self, offer_id: int, build_path: str, passed_all: bool
    ) -> None:
        if not passed_all:
            return
        with self.conn:
            self.conn.execute(
                "UPDATE offers SET status = 'built', built_at = ?, build_path = ? WHERE id = ?",
                (now_utc(), build_path, offer_id),
            )

    # receipts ---------------------------------------------------------------

    def offer_receipt_contract(self, offer_id: int) -> list[dict[str, Any]]:
        row = self.get_offer(offer_id)
        if row is None:
            raise DiscoveryError(f"offer {offer_id} not found")
        contract = json.loads(row["receipt_contract"])
        return contract  # type: ignore[return-value]

    def insert_receipt(self, offer_id: int, receipt: dict[str, Any]) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO receipts (offer_id, kind, argv, output_excerpt, exit_code, passed, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                offer_id,
                receipt["kind"],
                json.dumps(receipt["argv"]),
                receipt.get("output_excerpt"),
                receipt.get("exit_code"),
                1 if receipt.get("passed") else 0,
                receipt.get("created_at", now_utc()),
            ),
        )
        return int(cursor.lastrowid)

    def clear_receipts_for_offer(self, offer_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM receipts WHERE offer_id = ?", (offer_id,))

    def receipts_for_offer(self, offer_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM receipts WHERE offer_id = ? ORDER BY id", (offer_id,)
        ).fetchall()
