"""Shared helpers for automation_discovery."""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import tempfile
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


EXCERPT_MAX_LEN = 240


def now_utc() -> str:
    """Return the current UTC time as an ISO-8601 Z timestamp."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def resolve_path(value: str | Path) -> Path:
    """Expand ~ and resolve to an absolute path."""
    return Path(value).expanduser().resolve()


def slugify(text: str, fallback: str = "x") -> str:
    """Make a stable ASCII task-key slug."""
    normalized = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")
    return slug or fallback


def atomic_write(path: Path, content: str) -> None:
    """Write a text file atomically via a temp file + rename."""
    path = resolve_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        shutil.move(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def valid_kind(kind: str) -> bool:
    """Return True if kind is a recognized source kind."""
    return kind in {"ai-history", "communication", "work-tracking", "knowledge", "execution", "other"}


def valid_source_status(status: str) -> bool:
    return status in {"planned", "ingested", "failed", "excluded"}


def valid_candidate_status(status: str) -> bool:
    return status in {"proposed", "audited", "rejected", "offered"}


def valid_offer_status(status: str) -> bool:
    return status in {"offered", "chosen", "declined", "built"}


def valid_severity(severity: str) -> bool:
    return severity in {"info", "warn", "block"}


def valid_overlap(overlap: str) -> bool:
    return overlap in {"none", "adjacent", "partial", "exact", "unknown"}


def is_iso_date_or_datetime(value: str | None) -> bool:
    """Return True when value is empty or an ISO date/datetime."""
    if value is None or value.strip() == "":
        return True
    candidate = value.strip()
    try:
        from datetime import date, datetime
        date.fromisoformat(candidate)
        return True
    except ValueError:
        pass
    if candidate.endswith("Z"):
        candidate = f"{candidate[:-1]}+00:00"
    try:
        datetime.fromisoformat(candidate)
        return True
    except ValueError:
        return False


@contextmanager
def open_read_only(db_path: Path):
    """Open database in read-only mode as a context manager."""
    conn = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()
