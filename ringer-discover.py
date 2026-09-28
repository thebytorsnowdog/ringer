#!/usr/bin/env python3
"""Ringer Discovery CLI — a Ringer-native companion for automation discovery."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sqlite3
import subprocess
import sys
from pathlib import Path

from automation_discovery.db import Connection, DiscoveryError, initialize_database
from automation_discovery.export import export_markdown
from automation_discovery.manifest import (
    RUN_NAME,
    make_audit_manifest,
    make_build_manifest,
    make_ingest_manifest,
)
from automation_discovery.receipts import merge_staged_receipts, run_build_check
from automation_discovery.utils import atomic_write, now_utc, resolve_path
from automation_discovery.validate import (
    SUMMARY_FIELDS,
    load_staged_summary,
    validate_staged_events,
    validate_staged_notes,
    validate_staging,
)


def write_error(message: str) -> None:
    print(f"Error: {message}", file=sys.stderr)


def ensure_db(path: Path) -> Path:
    db = resolve_path(path)
    if not db.is_file():
        raise DiscoveryError(f"corpus database {db} not found; run 'init' first")
    return db


def _write_root_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--write-root",
        default=".",
        type=lambda s: resolve_path(s),
        help="Discovery write root containing corpus.db (default: current directory)",
    )


def cmd_init(args: argparse.Namespace) -> int:
    try:
        db_path = initialize_database(resolve_path(args.write_root), force=args.force)
        print(f"Created {db_path}")
        return 0
    except DiscoveryError as exc:
        write_error(str(exc))
        return 2


def cmd_add_source(args: argparse.Namespace) -> int:
    try:
        db = ensure_db(args.write_root / "corpus.db")
        with Connection(db) as conn:
            sid = conn.add_source(
                surface=args.surface,
                kind=args.kind,
                access_route=args.access_route,
                coverage_start=args.coverage_start,
                coverage_end=args.coverage_end,
                notes=args.notes,
            )
        print(f"Added source {sid}: {args.surface}")
        return 0
    except DiscoveryError as exc:
        write_error(str(exc))
        return 2


def cmd_exclude_source(args: argparse.Namespace) -> int:
    try:
        db = ensure_db(args.write_root / "corpus.db")
        with Connection(db) as conn:
            conn.exclude_source(args.id)
        print(f"Excluded source {args.id}")
        return 0
    except DiscoveryError as exc:
        write_error(str(exc))
        return 2


def cmd_add_existing_automation(args: argparse.Namespace) -> int:
    try:
        db = ensure_db(args.write_root / "corpus.db")
        with Connection(db) as conn:
            nid = conn.add_existing_automation(
                scope=args.scope,
                finding=args.finding,
                severity=args.severity,
                resolved=args.resolved,
            )
        print(f"Added audit note {nid}")
        return 0
    except DiscoveryError as exc:
        write_error(str(exc))
        return 2


def cmd_plan(args: argparse.Namespace) -> int:
    try:
        db = ensure_db(args.write_root / "corpus.db")
        with Connection(db) as conn:
            summary = conn.source_plan_summary()
        print(json.dumps(summary, indent=2))
        return 0
    except DiscoveryError as exc:
        write_error(str(exc))
        return 2


def cmd_approve_sources(args: argparse.Namespace) -> int:
    try:
        db = ensure_db(args.write_root / "corpus.db")
        with Connection(db) as conn:
            planned = conn.planned_sources()
            if not planned:
                raise DiscoveryError(
                    "no planned sources to approve; add sources first"
                )
            if not args.confirm:
                raise DiscoveryError(
                    "approve-sources requires --confirm to sign off on the current plan. "
                    "Review 'plan' output first."
                )
            conn.set_approved_at()
            summary = conn.source_plan_summary()
        print(f"Sources approved at {summary['approved_at']}")
        return 0
    except DiscoveryError as exc:
        write_error(str(exc))
        return 2


def cmd_make_ingest_manifest(args: argparse.Namespace) -> int:
    try:
        db = ensure_db(args.write_root / "corpus.db")
        manifest = make_ingest_manifest(
            db_path=db,
            model=args.model,
            output_path=resolve_path(args.out),
            min_rows=args.min_rows,
        )
        print(f"Wrote {len(manifest['tasks'])} ingestion task(s) to {resolve_path(args.out)}")
        return 0
    except DiscoveryError as exc:
        write_error(str(exc))
        return 2


def _load_jsonl(path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    if not path.is_file():
        return rows
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        rows.append(json.loads(line))
    return rows


def cmd_merge_ingest(args: argparse.Namespace) -> int:
    try:
        db = ensure_db(args.write_root / "corpus.db")
        task_dir = resolve_path(args.task_dir)
        source_id = args.source
        with Connection(db) as conn:
            source = conn.get_source(source_id)
            if source is None:
                raise DiscoveryError(f"source {source_id} not found")
            events_path = task_dir / "events.jsonl"
            summary_path = task_dir / "source_summary.json"
            errors = validate_staged_events(events_path, min_rows=0)
            if errors:
                raise DiscoveryError("; ".join(errors))
            events = _load_jsonl(events_path)
            summary = load_staged_summary(summary_path.parent) if summary_path.is_file() else {}
            status = summary.get("status", "ingested")
            if status == "failed":
                conn.update_source_after_ingest(
                    source_id,
                    status="failed",
                    lane=args.lane,
                    ingested_at=now_utc(),
                    coverage_start=summary.get("coverage_start"),
                    coverage_end=summary.get("coverage_end"),
                    notes=summary.get("notes"),
                )
                print(f"Recorded failed ingest for source {source_id}")
                return 0
            count = conn.insert_events(source_id, events)
            conn.update_source_after_ingest(
                source_id,
                status="ingested",
                lane=args.lane,
                ingested_at=now_utc(),
                coverage_start=summary.get("coverage_start"),
                coverage_end=summary.get("coverage_end"),
                notes=summary.get("notes") or f"merged {count} events",
            )
        print(f"Merged {count} events into source {source_id}")
        return 0
    except DiscoveryError as exc:
        write_error(str(exc))
        return 2


def cmd_make_audit_manifest(args: argparse.Namespace) -> int:
    try:
        db = ensure_db(args.write_root / "corpus.db")
        manifest = make_audit_manifest(
            db_path=db,
            model=args.model,
            output_path=resolve_path(args.out),
        )
        print(f"Wrote {len(manifest['tasks'])} audit task(s) to {resolve_path(args.out)}")
        return 0
    except DiscoveryError as exc:
        write_error(str(exc))
        return 2


def cmd_merge_audit(args: argparse.Namespace) -> int:
    try:
        db = ensure_db(args.write_root / "corpus.db")
        task_dir = resolve_path(args.task_dir)
        notes_path = task_dir / "audit_notes.jsonl"
        errors = validate_staged_notes(notes_path, min_rows=1)
        if errors:
            raise DiscoveryError("; ".join(errors))
        notes = _load_jsonl(notes_path)
        with Connection(db) as conn:
            for note in notes:
                conn.conn.execute(
                    "INSERT INTO audit_notes (scope, finding, severity, resolved) VALUES (?, ?, ?, ?)",
                    (
                        note.get("scope"),
                        note.get("finding"),
                        note.get("severity"),
                        1 if note.get("resolved") else 0,
                    ),
                )
            if args.focus == "candidates":
                candidates_path = task_dir / "candidates.jsonl"
                if not candidates_path.is_file():
                    raise DiscoveryError("candidates.jsonl not found in task dir")
                candidates = _load_jsonl(candidates_path)
                for cand in candidates:
                    conn.insert_candidate(cand)
            conn.conn.commit()
        print(f"Merged {len(notes)} audit note(s)" + (f" and {len(candidates)} candidate(s)" if args.focus == "candidates" else ""))
        return 0
    except DiscoveryError as exc:
        write_error(str(exc))
        return 2


def cmd_list_candidates(args: argparse.Namespace) -> int:
    try:
        db = ensure_db(args.write_root / "corpus.db")
        with Connection(db) as conn:
            rows = conn.all_candidates()
        for row in rows:
            print(f"{row['id']}: {row['job']} [status={row['status']}, overlap={row['overlap']}]")
        return 0
    except DiscoveryError as exc:
        write_error(str(exc))
        return 2


def cmd_add_offer(args: argparse.Namespace) -> int:
    try:
        db = ensure_db(args.write_root / "corpus.db")
        contract = json.loads(args.receipt_contract)
        evidence_query = args.evidence_query
        if evidence_query and Path(evidence_query).is_file():
            evidence_query = Path(evidence_query).read_text(encoding="utf-8")
        with Connection(db) as conn:
            oid = conn.insert_offer(
                {
                    "candidate_id": args.candidate_id,
                    "title": args.title,
                    "what_it_does": args.what_it_does,
                    "evidence_query": evidence_query,
                    "run_cost": args.run_cost,
                    "receipt_contract": contract,
                }
            )
        print(f"Added offer {oid}: {args.title}")
        return 0
    except (DiscoveryError, json.JSONDecodeError) as exc:
        write_error(str(exc))
        return 2


def cmd_list_offers(args: argparse.Namespace) -> int:
    try:
        db = ensure_db(args.write_root / "corpus.db")
        with Connection(db) as conn:
            rows = conn.offered_or_chosen_offers()
        for row in rows:
            marker = " [CHOSEN]" if row["status"] == "chosen" else ""
            print(f"{row['id']}: {row['title']}{marker}")
        return 0
    except DiscoveryError as exc:
        write_error(str(exc))
        return 2


def cmd_choose_offer(args: argparse.Namespace) -> int:
    try:
        db = ensure_db(args.write_root / "corpus.db")
        if not args.confirm:
            raise DiscoveryError(
                "choose-offer requires --confirm. This is the human gate."
            )
        with Connection(db) as conn:
            conn.choose_offer(args.offer_id)
        print(f"Chose offer {args.offer_id}; other offered rows declined")
        return 0
    except DiscoveryError as exc:
        write_error(str(exc))
        return 2


def cmd_make_build_manifest(args: argparse.Namespace) -> int:
    try:
        db = ensure_db(args.write_root / "corpus.db")
        manifest = make_build_manifest(
            db_path=db,
            model=args.model,
            output_path=resolve_path(args.out),
        )
        print(f"Wrote 1 build task to {resolve_path(args.out)}")
        return 0
    except DiscoveryError as exc:
        write_error(str(exc))
        return 2


def cmd_merge_build(args: argparse.Namespace) -> int:
    try:
        db = ensure_db(args.write_root / "corpus.db")
        task_dir = resolve_path(args.task_dir)
        offer_id = args.offer_id
        merge_staged_receipts(db, offer_id, task_dir, args.write_root)
        print(f"Offer {offer_id} built; staged receipts merged")
        return 0
    except DiscoveryError as exc:
        write_error(str(exc))
        return 2


def cmd_validate(args: argparse.Namespace) -> int:
    try:
        db = ensure_db(args.write_root / "corpus.db")
        from automation_discovery.validate import run_corpus_validation
        return run_corpus_validation(db)
    except DiscoveryError as exc:
        write_error(str(exc))
        return 2


def cmd_export(args: argparse.Namespace) -> int:
    try:
        db = ensure_db(args.write_root / "corpus.db")
        out = resolve_path(args.out)
        export_markdown(db, out)
        print(f"Exported Markdown views to {out}")
        return 0
    except DiscoveryError as exc:
        write_error(str(exc))
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ringer-discover.py",
        description="Ringer Discovery: turn work traces into a verified automation corpus.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="Create corpus.db in the write root")
    _write_root_option(p_init)
    p_init.add_argument("--force", action="store_true", help="Replace existing corpus.db")

    p_add = sub.add_parser("add-source", help="Add a planned source")
    _write_root_option(p_add)
    p_add.add_argument("--surface", required=True)
    p_add.add_argument(
        "--kind",
        required=True,
        choices=["ai-history", "communication", "work-tracking", "knowledge", "execution", "other"],
    )
    p_add.add_argument("--access-route", required=True)
    p_add.add_argument("--coverage-start")
    p_add.add_argument("--coverage-end")
    p_add.add_argument("--notes")

    p_excl = sub.add_parser("exclude-source", help="Mark a source as excluded")
    _write_root_option(p_excl)
    p_excl.add_argument("id", type=int)

    p_auto = sub.add_parser(
        "add-existing-automation", help="Record an existing automation audit note"
    )
    _write_root_option(p_auto)
    p_auto.add_argument("--scope", required=True)
    p_auto.add_argument("--finding", required=True)
    p_auto.add_argument(
        "--severity", default="info", choices=["info", "warn", "block"]
    )
    p_auto.add_argument("--resolved", action="store_true")

    p_plan = sub.add_parser("plan", help="Show source plan and approval status")
    _write_root_option(p_plan)

    p_app = sub.add_parser("approve-sources", help="Approve the current source plan")
    _write_root_option(p_app)
    p_app.add_argument(
        "--confirm",
        action="store_true",
        required=True,
        help="Required flag confirming human review of the plan",
    )

    p_mim = sub.add_parser("make-ingest-manifest", help="Generate Stage 1 Ringer manifest")
    _write_root_option(p_mim)
    p_mim.add_argument("--model", required=True, help="OpenRouter model slug for opencode")
    p_mim.add_argument("--out", required=True)
    p_mim.add_argument("--min-rows", type=int, default=10)

    p_mgi = sub.add_parser("merge-ingest", help="Merge a Stage 1 task dir into corpus.db")
    _write_root_option(p_mgi)
    p_mgi.add_argument("task_dir")
    p_mgi.add_argument("--source", type=int, required=True)
    p_mgi.add_argument("--lane", default="ringer")

    p_mam = sub.add_parser("make-audit-manifest", help="Generate Stage 2 Ringer manifest")
    _write_root_option(p_mam)
    p_mam.add_argument("--model", required=True)
    p_mam.add_argument("--out", required=True)

    p_mga = sub.add_parser("merge-audit", help="Merge a Stage 2 task dir into corpus.db")
    _write_root_option(p_mga)
    p_mga.add_argument("task_dir")
    p_mga.add_argument(
        "--focus", required=True, choices=["coverage", "fabrication", "candidates"]
    )

    p_lc = sub.add_parser("list-candidates", help="List candidate rows")
    _write_root_option(p_lc)

    p_ao = sub.add_parser("add-offer", help="Add an offer for a candidate")
    _write_root_option(p_ao)
    p_ao.add_argument("--candidate-id", type=int, required=True)
    p_ao.add_argument("--title", required=True)
    p_ao.add_argument("--what-it-does", required=True)
    p_ao.add_argument("--evidence-query", required=True)
    p_ao.add_argument("--run-cost")
    p_ao.add_argument(
        "--receipt-contract", required=True, help="JSON array: [{kind, argv, success_contains?}]"
    )

    p_lo = sub.add_parser("list-offers", help="List offered/chosen offers")
    _write_root_option(p_lo)

    p_co = sub.add_parser("choose-offer", help="Choose one offer (human gate)")
    _write_root_option(p_co)
    p_co.add_argument("offer_id", type=int)
    p_co.add_argument(
        "--confirm",
        action="store_true",
        required=True,
        help="Required flag confirming the human choice",
    )

    p_mbm = sub.add_parser("make-build-manifest", help="Generate Stage 4 Ringer manifest")
    _write_root_option(p_mbm)
    p_mbm.add_argument("--model", required=True)
    p_mbm.add_argument("--out", required=True)

    p_mgb = sub.add_parser("merge-build", help="Merge validated staged receipts and mark the offer built")
    _write_root_option(p_mgb)
    p_mgb.add_argument("task_dir")
    p_mgb.add_argument("--offer-id", type=int, required=True)

    p_val = sub.add_parser("validate", help="Validate corpus.db")
    _write_root_option(p_val)

    p_exp = sub.add_parser("export", help="Export Markdown views")
    _write_root_option(p_exp)
    p_exp.add_argument("--out", required=True)

    # Hidden manifest-check subcommands are dispatched before this parser to keep --help clean.

    return parser


def cmd_validate_staging(args: argparse.Namespace) -> int:
    return validate_staging([args.events_file, "--min-rows", str(args.min_rows)])


def cmd_validate_audit_staging(args: argparse.Namespace) -> int:
    from automation_discovery.validate import validate_audit_staging
    return validate_audit_staging([args.notes_file, "--min-rows", str(args.min_rows)])


def cmd_receipts_check(args: argparse.Namespace) -> int:
    return run_build_check(
        resolve_path(args.db_path),
        args.offer_id,
        resolve_path(args.build_root),
        resolve_path(args.receipts_jsonl),
    )


def dispatch(args: argparse.Namespace) -> int:
    handlers = {
        "init": cmd_init,
        "add-source": cmd_add_source,
        "exclude-source": cmd_exclude_source,
        "add-existing-automation": cmd_add_existing_automation,
        "plan": cmd_plan,
        "approve-sources": cmd_approve_sources,
        "make-ingest-manifest": cmd_make_ingest_manifest,
        "merge-ingest": cmd_merge_ingest,
        "make-audit-manifest": cmd_make_audit_manifest,
        "merge-audit": cmd_merge_audit,
        "list-candidates": cmd_list_candidates,
        "add-offer": cmd_add_offer,
        "list-offers": cmd_list_offers,
        "choose-offer": cmd_choose_offer,
        "make-build-manifest": cmd_make_build_manifest,
        "merge-build": cmd_merge_build,
        "validate": cmd_validate,
        "export": cmd_export,
        "validate-staging": cmd_validate_staging,
        "validate-audit-staging": cmd_validate_audit_staging,
        "receipts-check": cmd_receipts_check,
    }
    handler = handlers.get(args.command)
    if handler is None:
        write_error(f"unknown command: {args.command}")
        return 2
    return handler(args)


def _dispatch_hidden(argv: list[str]) -> int | None:
    """Handle manifest-check subcommands without registering them in public --help."""
    if not argv:
        return None
    command = argv[0]
    if command == "validate-staging":
        p = argparse.ArgumentParser(prog="ringer-discover.py validate-staging")
        p.add_argument("events_file")
        p.add_argument("--min-rows", type=int, default=10)
        a = p.parse_args(argv[1:])
        return validate_staging([a.events_file, "--min-rows", str(a.min_rows)])
    if command == "validate-audit-staging":
        from automation_discovery.validate import validate_audit_staging
        p = argparse.ArgumentParser(prog="ringer-discover.py validate-audit-staging")
        p.add_argument("notes_file")
        p.add_argument("--min-rows", type=int, default=1)
        a = p.parse_args(argv[1:])
        return validate_audit_staging([a.notes_file, "--min-rows", str(a.min_rows)])
    if command == "receipts-check":
        p = argparse.ArgumentParser(prog="ringer-discover.py receipts-check")
        p.add_argument("db_path")
        p.add_argument("offer_id", type=int)
        p.add_argument("build_root")
        p.add_argument("receipts_jsonl")
        a = p.parse_args(argv[1:])
        return run_build_check(
            resolve_path(a.db_path),
            a.offer_id,
            resolve_path(a.build_root),
            resolve_path(a.receipts_jsonl),
        )
    return None


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    hidden = _dispatch_hidden(argv)
    if hidden is not None:
        return hidden
    parser = build_parser()
    args = parser.parse_args(argv)
    return dispatch(args)


if __name__ == "__main__":
    raise SystemExit(main())
