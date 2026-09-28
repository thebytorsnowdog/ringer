"""SQLite schema for the automation-discovery corpus."""

SCHEMA = """
-- automation-discovery corpus schema v1
-- The corpus is the durable product: normalized, queryable, source-referenced.
-- Rows point to sources (source_ref); they never carry enough raw content
-- to substitute for them. excerpt is capped to keep it that way.

PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
  id             INTEGER PRIMARY KEY,
  surface        TEXT NOT NULL,
  kind           TEXT NOT NULL CHECK (kind IN ('ai-history','communication','work-tracking','knowledge','execution','other')),
  access_route   TEXT NOT NULL,
  coverage_start TEXT,
  coverage_end   TEXT,
  ingested_at    TEXT,
  lane           TEXT,
  status         TEXT NOT NULL DEFAULT 'planned' CHECK (status IN ('planned','ingested','failed','excluded')),
  notes          TEXT
);

CREATE TABLE IF NOT EXISTS events (
  id          INTEGER PRIMARY KEY,
  source_id   INTEGER NOT NULL REFERENCES sources(id),
  source_ref  TEXT NOT NULL,
  occurred_at TEXT,
  actor       TEXT,
  action      TEXT NOT NULL,
  artifact    TEXT,
  excerpt     TEXT CHECK (excerpt IS NULL OR length(excerpt) <= 240),
  job_hint    TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_source  ON events(source_id);
CREATE INDEX IF NOT EXISTS idx_events_jobhint ON events(job_hint);

CREATE TABLE IF NOT EXISTS candidates (
  id                INTEGER PRIMARY KEY,
  job               TEXT NOT NULL,
  event_ids         TEXT NOT NULL,
  independence_note TEXT NOT NULL,
  consequence       TEXT,
  overlap           TEXT NOT NULL DEFAULT 'unknown' CHECK (overlap IN ('none','adjacent','partial','exact','unknown')),
  status            TEXT NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed','audited','rejected','offered'))
);

CREATE TABLE IF NOT EXISTS audit_notes (
  id       INTEGER PRIMARY KEY,
  scope    TEXT NOT NULL,
  finding  TEXT NOT NULL,
  severity TEXT NOT NULL CHECK (severity IN ('info','warn','block')),
  resolved INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS offers (
  id               INTEGER PRIMARY KEY,
  candidate_id     INTEGER NOT NULL REFERENCES candidates(id),
  title            TEXT NOT NULL,
  what_it_does     TEXT NOT NULL,
  evidence_query   TEXT,
  run_cost         TEXT,
  receipt_contract TEXT NOT NULL,
  status           TEXT NOT NULL DEFAULT 'offered' CHECK (status IN ('offered','chosen','declined','built')),
  chosen_at        TEXT,
  built_at         TEXT,
  build_path       TEXT
);

CREATE TABLE IF NOT EXISTS receipts (
  id             INTEGER PRIMARY KEY,
  offer_id       INTEGER NOT NULL REFERENCES offers(id),
  kind           TEXT NOT NULL,
  argv           TEXT NOT NULL,
  output_excerpt TEXT,
  exit_code      INTEGER,
  passed         INTEGER,
  created_at     TEXT NOT NULL
);
"""

SCHEMA_VERSION = "1.0.0"
