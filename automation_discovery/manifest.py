"""Generate Ringer manifests for ingestion, audit, and build stages."""

from __future__ import annotations

import json
import shlex
import sqlite3
from pathlib import Path

from automation_discovery.db import Connection, DiscoveryError
from automation_discovery.utils import open_read_only, resolve_path, slugify
from automation_discovery.validate import SUMMARY_FIELDS

RUN_NAME = "ringer-automation-discovery"
def _discovery_cli() -> str:
    """Return the absolute path to the ringer-discover.py CLI."""
    return str(Path(__file__).resolve().parent.parent / "ringer-discover.py")


def _check_cmd(subcommand: str, *args: str) -> str:
    """Return a shell command invoking ringer-discover.py by absolute path."""
    parts = ["python3", shlex.quote(_discovery_cli()), subcommand]
    parts.extend(args)
    return " ".join(parts)


def _engine_block(model: str) -> dict[str, object]:
    """Return common engine-related task fields."""
    return {
        "engine": "opencode",
        "model": model,
        "timeout_s": 1800,
    }


def _render_ingest_spec(source: sqlite3.Row, db_path: Path) -> str:
    return (
        f"# Stage 1 — Ingest worker\n\n"
        f"You are a read-only ingest worker running through the Ringer automation-discovery pipeline. "
        f"Your role is to read exactly one assigned surface and normalize recurring human work into structured events, "
        f"then stage them for transactional merge.\n\n"
        f"## Ownership boundary\n\n"
        f"You own ONLY these staged files in your task directory:\n"
        f"- `./events.jsonl` — one JSON object per line.\n"
        f"- `./source_summary.json` — one JSON object.\n\n"
        f"You must NEVER open, write, or modify the shared database at {db_path}. "
        f"You must NEVER write files outside this task directory except the two staged files above.\n\n"
        f"## Source parameters\n\n"
        f"- Source ID: {source['id']}\n"
        f"- Surface: {source['surface']}\n"
        f"- Access route: {source['access_route']}\n"
        f"- Coverage window: {source['coverage_start'] or 'start'} → {source['coverage_end'] or 'end'}\n\n"
        f"## What to read\n\n"
        f"Read the assigned surface ONLY through the access route above, within the coverage window. "
        f"If the access route is unreadable, missing, or blocked, set status 'failed' in source_summary.json "
        f"with the exact error and leave events.jsonl empty. Never substitute a different route, tool, or service.\n\n"
        f"## events.jsonl schema (one JSON object per line)\n\n"
        f"- `source_ref` (required, string): stable pointer such as file path + line, session id + turn, or message timestamp.\n"
        f"- `occurred_at` (ISO date or datetime, optional): when it happened.\n"
        f"- `actor` (string, optional): who acted or asked.\n"
        f"- `action` (required, string): the human action or request in one plain sentence.\n"
        f"- `artifact` (string, optional): what resulted.\n"
        f"- `excerpt` (string, optional, max 240 chars): one verbatim line only; redact secrets/names.\n"
        f"- `job_hint` (string, optional): short recurring-job label, e.g. 'weekly status chasing'.\n\n"
        f"Rules: do not invent events; a busy thread/session/ticket is ONE event unless it contains genuinely separate asks; "
        f"skip one-off reading; exclude anything inside excluded walls; no source content longer than 240 chars.\n\n"
        f"## source_summary.json schema\n\n"
        f"Keys: `status` ('ingested' or 'failed'), `coverage_start`, `coverage_end`, `notes`. "
        f"Notes must include row count and anything skipped, or the exact failure.\n\n"
        f"## Output check\n\n"
        f"After staging, the Ringer check runs `{_check_cmd('validate-staging', './events.jsonl', '--min-rows', 'N')}`. "
        f"If the surface honestly yielded fewer rows, set status 'failed' and explain why."
    )


def make_ingest_manifest(
    db_path: Path,
    model: str,
    output_path: Path,
    min_rows: int = 10,
) -> dict[str, object]:
    conn = Connection(db_path)
    try:
        approval = conn.get_approved_at()
        if approval is None:
            raise DiscoveryError(
                "source plan is not approved. Run 'approve-sources' first."
            )
        sources = conn.planned_sources()
        if not sources:
            raise DiscoveryError("no planned sources; add sources first")
    finally:
        conn.close()

    workdir = str(db_path.parent)
    tasks: list[dict[str, object]] = []
    used: set[str] = set()
    for source in sources:
        base = f"ingest-{source['id']}-{slugify(source['surface'], 'source')}"
        key = base
        suffix = 2
        while key in used:
            key = f"{base}-{suffix}"
            suffix += 1
        used.add(key)
        spec = _render_ingest_spec(source, db_path)
        check = _check_cmd(
            "validate-staging",
            shlex.quote(str(Path(workdir) / key / "events.jsonl")),
            "--min-rows",
            str(min_rows),
        )
        tasks.append(
            {
                "key": key,
                "task_type": "research",
                **_engine_block(model),
                "spec": spec,
                "check": check,
                "expect_files": ["events.jsonl", "source_summary.json"],
                "verified": (
                    f"Source {source['id']} staged at least {min_rows} schema-valid events "
                    "and an honest source_summary.json; an empty ingest cannot pass."
                ),
            }
        )

    manifest = {
        "run_name": RUN_NAME,
        "workdir": workdir,
        "max_parallel": min(8, len(tasks)),
        "tasks": tasks,
    }
    atomic_write_json(output_path, manifest)
    return manifest


def _render_audit_spec(focus: str, db_path: Path) -> str:
    focuses = {
        "coverage": "Compare promised sources against the ingested corpus. Flag failed sources, suspiciously small ingests, coverage gaps, and surfaces referenced by events but not ingested.",
        "fabrication": "Sample events across every source, resolve each source_ref, and confirm the referenced thing exists and supports the row. Flag wall violations.",
        "candidates": "Cluster events by job_hint and action text. Write candidate rows for jobs supported by at least 3 distinct events. Record independence reasoning and existing-automation overlap.",
    }
    candidates_file_line = (
        "- `./candidates.jsonl` — one JSON object per line (write this for candidate rows).\n"
        if focus == "candidates"
        else ""
    )
    return (
        f"# Stage 2 — Corpus audit worker\n\n"
        f"You are a fresh auditor. You did NOT ingest this corpus. "
        f"Your focus is `{focus}`.\n\n"
        f"## Ownership boundary\n\n"
        f"You own ONLY staged audit output in your task directory:\n"
        f"- `./audit_notes.jsonl` — one JSON object per line (required).\n"
        f"{candidates_file_line}"
        f"You must NEVER write the database at {db_path} or any file outside your task directory.\n\n"
        f"## Focus: {focus}\n\n"
        f"{focuses[focus]}\n\n"
        f"## audit_notes.jsonl schema\n\n"
        f"- `scope` (required): 'corpus' | 'source:<id>' | 'candidate:<id>' | 'offer:<id>'\n"
        f"- `finding` (required, string): plain description.\n"
        f"- `severity` (required): 'info', 'warn', or 'block'.\n"
        f"- `resolved` (integer, default 0): 1 if resolved, 0 otherwise.\n\n"
        f"## candidates.jsonl schema (only for candidates focus)\n\n"
        f"- `job` (required): one-sentence recurring job.\n"
        f"- `event_ids` (required): JSON array of at least 3 distinct integer IDs.\n"
        f"- `independence_note` (required): why these are independent (separate occasions/items/people).\n"
        f"- `consequence` (string, optional): only what the evidence shows.\n"
        f"- `overlap` (required): 'none', 'adjacent', 'partial', 'exact', or 'unknown'.\n"
        f"- `status` (optional): default 'audited'.\n\n"
        f"Hard rules: never edit events/sources; quote nothing longer than rows already contain; "
        f"if nothing survives the 3-event gate, write one `audit_notes` scope='corpus' severity='info' row explaining that.\n\n"
        f"## Output check\n\n"
        f"The Ringer check runs `{_check_cmd('validate-audit-staging', './audit_notes.jsonl', '--min-rows', '1')}`."
    )


def make_audit_manifest(
    db_path: Path,
    model: str,
    output_path: Path,
) -> dict[str, object]:
    conn = Connection(db_path)
    try:
        sources = conn.all_sources()
        ingested = [s for s in sources if s["status"] == "ingested"]
        if not ingested:
            raise DiscoveryError("no ingested sources to audit")
    finally:
        conn.close()

    workdir = str(db_path.parent)
    tasks: list[dict[str, object]] = []
    for focus in ("coverage", "fabrication", "candidates"):
        key = f"audit-{focus}"
        check = _check_cmd(
            "validate-audit-staging",
            shlex.quote(str(Path(workdir) / key / "audit_notes.jsonl")),
            "--min-rows",
            "1",
        )
        if focus == "candidates":
            check += (
                f" && [ -f {shlex.quote(str(Path(workdir) / key / 'candidates.jsonl'))} ]"
                f" && echo 'candidates.jsonl present'"
                f" || (echo 'FAIL: candidates.jsonl missing' && exit 1)"
            )
        tasks.append(
            {
                "key": key,
                "task_type": "research",
                **_engine_block(model),
                "spec": _render_audit_spec(focus, db_path),
                "check": check,
                "expect_files": ["audit_notes.jsonl"] + (
                    ["candidates.jsonl"] if focus == "candidates" else []
                ),
                "verified": (
                    f"Audit focus '{focus}' produced a non-empty audit_notes.jsonl and, for candidates, a candidates.jsonl."
                ),
            }
        )

    manifest = {
        "run_name": RUN_NAME,
        "workdir": workdir,
        "max_parallel": 3,
        "tasks": tasks,
    }
    atomic_write_json(output_path, manifest)
    return manifest


def _render_build_spec(offer: sqlite3.Row, build_root: Path) -> str:
    return (
        f"# Stage 4 — Build and prove the chosen offer\n\n"
        f"You are a code-feature worker building the chosen automation. You own exactly one build root:\n"
        f"- `{build_root}`\n\n"
        f"You may write ONLY under `{build_root}` and your task directory. "
        f"You must NEVER write the corpus database or any path outside the build root.\n\n"
        f"## Offer details\n\n"
        f"- Offer ID: {offer['id']}\n"
        f"- Candidate ID: {offer['candidate_id']}\n"
        f"- Title: {offer['title']}\n"
        f"- What it does: {offer['what_it_does']}\n"
        f"- Run cost: {offer['run_cost'] or 'unknown'}\n\n"
        f"## Receipt contract (this is exactly what must work after you build)\n\n"
        f"```json\n{offer['receipt_contract']}\n```\n\n"
        f"## What to build\n\n"
        f"Implement the smallest complete version of the tool described above. "
        f"Place all code, scripts, and documentation under `{build_root}`. "
        f"Write a `BUILD.md` in `{build_root}` describing files and how to run the tool. "
        f"Do not use secrets, hard-coded tokens, or shell-execution of untrusted input.\n\n"
        f"## Output check\n\n"
        f"After you finish building, the Ringer check will execute every receipt in the contract with shell=False "
        f"from `{build_root}`, atomically write `receipts.jsonl` in your task directory, "
        f"and fail if any receipt exits nonzero or misses its `success_contains`. "
        f"The separate `merge-build` step then validates the staged receipts against this offer's contract "
        f"and transactionally replaces the offer's receipt rows in `corpus.db` before marking it built."
    )


def make_build_manifest(
    db_path: Path,
    model: str,
    output_path: Path,
) -> dict[str, object]:
    conn = Connection(db_path)
    try:
        offer = conn.conn.execute(
            "SELECT * FROM offers WHERE status = 'chosen'"
        ).fetchone()
        if offer is None:
            raise DiscoveryError(
                "no chosen offer. Run 'choose-offer --confirm' first."
            )
    finally:
        conn.close()

    workdir = str(db_path.parent)
    offer_id = offer["id"]
    build_root = resolve_path(db_path.parent / "builds" / str(offer_id))
    key = f"build-offer-{offer_id}"
    spec = _render_build_spec(offer, build_root)
    check = _check_cmd(
        "receipts-check",
        shlex.quote(str(db_path)),
        str(offer_id),
        shlex.quote(str(build_root)),
        shlex.quote(str(Path(workdir) / key / "receipts.jsonl")),
    )
    tasks = [
        {
            "key": key,
            "task_type": "code-feature",
            **_engine_block(model),
            "engine_args": ["--writable-root", str(build_root)],
            "spec": spec,
            "check": check,
            "expect_files": [str(build_root / "BUILD.md")],
            "verified": (
                f"Built offer {offer_id} under {build_root} and every receipt argv passed "
                "with shell=False, correct exit code, and required success_contains."
            ),
        }
    ]
    manifest = {
        "run_name": RUN_NAME,
        "workdir": workdir,
        "max_parallel": 1,
        "tasks": tasks,
    }
    atomic_write_json(output_path, manifest)
    return manifest


def atomic_write_json(path: Path, obj: dict[str, object]) -> None:
    from automation_discovery.utils import atomic_write

    atomic_write(path, f"{json.dumps(obj, indent=2, ensure_ascii=False)}\n")
