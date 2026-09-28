"""Export normalized Markdown views from the corpus database."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from automation_discovery.utils import open_read_only, resolve_path


def _display(value: object) -> str:
    if value is None or value == "":
        return "—"
    return str(value)


def _cell(value: object) -> str:
    return _display(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", "<br>")


def _heading(value: object) -> str:
    return _display(value).replace("\n", " ").strip()


def _fence(value: object, language: str = "") -> str:
    text = _display(value)
    longest = max((len(m.group(0)) for m in re.finditer(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{language}\n{text}\n{fence}"


def render_sources(rows: list[sqlite3.Row]) -> str:
    lines = ["# Sources", ""]
    if not rows:
        lines.append("No sources found.")
        return "\n".join(lines) + "\n"
    lines.append(
        "| ID | Surface | Kind | Status | Coverage | Access route | Lane | Ingested at | Notes |"
    )
    lines.append(
        "| ---: | --- | --- | --- | --- | --- | --- | --- | --- |"
    )
    for row in rows:
        coverage = f"{_display(row['coverage_start'])} → {_display(row['coverage_end'])}"
        values = (
            row["id"],
            row["surface"],
            row["kind"],
            row["status"],
            coverage,
            row["access_route"],
            row["lane"],
            row["ingested_at"],
            row["notes"],
        )
        lines.append(f"| {' | '.join(_cell(v) for v in values)} |")
    return "\n".join(lines) + "\n"


def render_events(rows: list[sqlite3.Row]) -> str:
    lines = ["# Events by job", ""]
    if not rows:
        lines.append("No events found.")
        return "\n".join(lines) + "\n"
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        group = (
            row["job_hint"].strip()
            if row["job_hint"] and row["job_hint"].strip()
            else "Unclassified"
        )
        groups.setdefault(group, []).append(row)
    for group in sorted(groups, key=str.casefold):
        lines.extend([f"## {_heading(group)}", ""])
        lines.append("| Date | Actor | Action | Excerpt | Source ref |")
        lines.append("| --- | --- | --- | --- | --- |")
        for row in groups[group]:
            values = (
                row["occurred_at"],
                row["actor"],
                row["action"],
                row["excerpt"],
                row["source_ref"],
            )
            lines.append(f"| {' | '.join(_cell(v) for v in values)} |")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_candidates(rows: list[sqlite3.Row]) -> str:
    lines = ["# Candidates", ""]
    if not rows:
        lines.append("No candidates found.")
        return "\n".join(lines) + "\n"
    for row in rows:
        lines.extend([
            f"## Candidate {row['id']}: {_heading(row['job'])}",
            "",
            f"- Status: {_display(row['status'])}",
            f"- Supporting event IDs: `{row['event_ids']}`",
            f"- Independence: {_display(row['independence_note'])}",
            f"- Consequence: {_display(row['consequence'])}",
            f"- Overlap: {_display(row['overlap'])}",
            "",
        ])
    return "\n".join(lines).rstrip() + "\n"


def render_offers(rows: list[sqlite3.Row]) -> str:
    lines = ["# Offers", ""]
    if not rows:
        lines.append("No offers found.")
        return "\n".join(lines) + "\n"
    for row in rows:
        lines.extend([
            f"## Offer {row['id']}: {_heading(row['title'])}",
            "",
            f"- Candidate ID: {row['candidate_id']}",
            f"- Status: {_display(row['status'])}",
            f"- What it does: {_display(row['what_it_does'])}",
            f"- Run cost: {_display(row['run_cost'])}",
            f"- Chosen at: {_display(row['chosen_at'])}",
            f"- Built at: {_display(row['built_at'])}",
            f"- Build path: {_display(row['build_path'])}",
            "",
            "### Receipt contract",
            "",
            _fence(row["receipt_contract"], "json"),
            "",
            "### Evidence query",
            "",
            _fence(row["evidence_query"], "sql"),
            "",
        ])
    return "\n".join(lines).rstrip() + "\n"


def render_receipts(rows: list[sqlite3.Row]) -> str:
    lines = ["# Receipts", ""]
    if not rows:
        lines.append("No receipts found.")
        return "\n".join(lines) + "\n"
    for row in rows:
        status = "PASS" if row["passed"] else "FAIL"
        lines.extend([
            f"## Receipt {row['id']}: {_heading(row['kind'])} ({status})",
            "",
            f"- Offer ID: {row['offer_id']}",
            f"- Exit code: {row['exit_code']}",
            f"- Created at: {_display(row['created_at'])}",
            "",
            "### argv",
            "",
            _fence(row["argv"], "json"),
            "",
            "### Output excerpt",
            "",
            _fence(row["output_excerpt"]),
            "",
        ])
    return "\n".join(lines).rstrip() + "\n"


def export_markdown(db_path: Path, output_dir: Path) -> None:
    output_dir = resolve_path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    with open_read_only(db_path) as conn:
        views = {
            "sources.md": render_sources(conn.execute("SELECT * FROM sources ORDER BY id").fetchall()),
            "events-by-job.md": render_events(conn.execute(
                "SELECT id, occurred_at, actor, action, excerpt, source_ref, job_hint "
                "FROM events ORDER BY COALESCE(job_hint, ''), COALESCE(occurred_at, ''), id"
            ).fetchall()),
            "candidates.md": render_candidates(conn.execute("SELECT * FROM candidates ORDER BY id").fetchall()),
            "offers.md": render_offers(conn.execute("SELECT * FROM offers ORDER BY id").fetchall()),
            "receipts.md": render_receipts(conn.execute("SELECT * FROM receipts ORDER BY id").fetchall()),
        }
    for filename, content in views.items():
        (output_dir / filename).write_text(content, encoding="utf-8")
