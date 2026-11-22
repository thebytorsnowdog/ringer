"""Local attempt reporting for the Models and Outcomes commands.

Shared helpers and patched names are resolved lazily through ringer, which
re-exports this module's reporting functions.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import json
import re
import sys
from dataclasses import replace as dataclass_replace
from decimal import Decimal, InvalidOperation
from html import escape as html_escape
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ringer import AppConfig, ModelIdentityRegistry


RESERVED_FIXTURE_MODELS = frozenset(
    {"proven-model", "probation-model", "mock-model", "test-model"}
)
UNATTRIBUTED_MODEL_DISPLAY = "(unattributed legacy rows)"


# Statistical routing evidence only. Human quality and promotion stay separate.
PROVEN_MIN_TASKS = 30  # distinct jobs within one named task family
PROVEN_MIN_FIRST_TRY = .75
PROVEN_MIN_FINAL_CHECK = .85


def proven_model_group(group: dict[str, Any]) -> bool:
    return not (group.get("unattributed") or group.get("misrouted")) and any(
        family.get("task_type") not in {None, "", "(untyped)"}
        and model_scoreboard_tier(family["distinct_jobs"], family["first_try_pass_rate"],
                                  family["pass_rate"]) == "proven"
        for family in group.get("routing_evidence", []))


def routing_evidence(rows: list[dict[str, Any]], group: dict[str, Any]) -> list[dict[str, Any]]:
    """Count stable jobs, not task rows or repeated rounds. Keep history intact.

    A job's first/final result requires all its observed tasks in this family to
    pass. Unnamed jobs, benchmarks and identity conflicts cannot establish a floor.
    """
    families: dict[str, dict[str, dict[str, list[dict[str, Any]]]]] = {}
    for row in rows:
        if model_log_row_engine(row) != group["engine"] or model_log_row_model(row) != group["model"]:
            continue
        if model_log_row_reasoning_effort(row) != group.get("reasoning_effort"):
            continue
        family = model_log_row_task_type(row)
        if group.get("task_type") is not None and family != group["task_type"]:
            continue
        if family == "(untyped)" or model_log_row_is_unattributed(row):
            continue
        if row.get("evidence_kind") == "benchmark" or row.get("benchmark") is not None:
            continue
        job = model_log_text(row.get("job_id")) or model_log_text(row.get("run_name"))
        task = model_log_text(row.get("task_key"))
        if not job or not task:
            continue
        families.setdefault(family, {}).setdefault(job, {}).setdefault(task, []).append(row)
    evidence = []
    for family, jobs in sorted(families.items()):
        first_passed = final_passed = count = excluded = 0
        for tasks in jobs.values():
            attempts = [row for task_rows in tasks.values() for row in task_rows]
            if any(row.get("identity_mismatch") or row.get("expected_model") or
                   (row.get("reported_model") and row["reported_model"] != group["model"]) or
                   (row.get("effective_requested_model") and row["effective_requested_model"] != group["model"])
                   for row in attempts):
                excluded += 1
                continue
            count += 1
            firsts, finals = [], []
            for task_rows in tasks.values():
                ordered = sorted(task_rows, key=lambda row: (model_log_text(row.get("logged_at")),
                                  model_log_int(row.get("attempt_index")) or (2 if model_log_row_is_retry(row) else 1)))
                firsts.append(ordered[0]); finals.append(ordered[-1])
            def passed(row):
                return model_log_text(row.get("verdict")).upper() == "PASS" and row.get("product_state", "PASS") == "PASS"
            first_passed += all(passed(row) and not model_log_row_is_retry(row) and
                                (model_log_int(row.get("attempt_index")) or 1) == 1 for row in firsts)
            final_passed += all(passed(row) for row in finals)
        evidence.append(dict(task_type=family, distinct_jobs=count, excluded_jobs=excluded,
                             first_try_pass_rate=first_passed/count if count else 0.,
                             pass_rate=final_passed/count if count else 0.))
    return evidence


def attach_routing_evidence(groups: list[dict[str, Any]], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for group in groups:
        group["routing_evidence"] = routing_evidence(rows, group)
        group["tier"] = "unranked" if group.get("unattributed") or group.get("misrouted") else (
            "proven" if proven_model_group(group) else "probation")
    return groups


def catalog_model_is_text_candidate(model: dict[str, Any]) -> bool:
    try:
        context_length = int(model.get("context_length") or 0)
    except (TypeError, ValueError):
        context_length = 0
    return (
        not bool(model.get("variable_pricing"))
        and str(model.get("modality", "")).strip().lower() == "text->text"
        and context_length >= 32000
    )


def catalog_explore_candidates(
    catalog_models: list[dict[str, Any]],
    *,
    tested_models: set[str],
    limit: int = 10,
) -> list[dict[str, Any]]:
    candidates = [
        model
        for model in catalog_models
        if str(model.get("id", "")).strip() not in tested_models
        and str(model.get("id", "")).strip() not in RESERVED_FIXTURE_MODELS
        and catalog_model_is_text_candidate(model)
    ]
    return sorted(
        candidates,
        key=lambda model: (
            not bool(model.get("free")),
            float(model.get("prompt_per_m") or 0) + float(model.get("completion_per_m") or 0),
            str(model.get("id") or ""),
        ),
    )[:limit]


def print_model_explore(
    *,
    log_path: Path,
    rows_read: int,
    skipped: int,
    groups: list[dict[str, Any]],
    catalog_path: Path,
    catalog_models: list[dict[str, Any]],
) -> None:
    import ringer

    print(f"TIERS from {log_path} ({rows_read} rows, {skipped} skipped lines)")
    if not groups:
        print("  no local evidence")
    for group in groups:
        label = (
            "unranked"
            if group.get("unattributed") or group.get("misrouted")
            else ("proven" if proven_model_group(group) else "probation")
        )
        display = (
            f"{group.get('model_display') or UNATTRIBUTED_MODEL_DISPLAY} "
            f"[{group.get('engine') or 'unknown'}]"
            if group.get("unattributed")
            else str(group.get("model_display") or group["model"])
        )
        if group.get("misrouted"):
            display = f"{display} [misrouted]"
        print(
            f"  {label:<9} {display} "
            f"task_type={group['task_type']} tasks={group['tasks']} "
            f"first={group['first_try_pass_rate']:.2f} pass={group['pass_rate']:.2f}"
        )

    tested_models = {str(group.get("model")) for group in groups if group.get("model")}
    candidates = catalog_explore_candidates(catalog_models, tested_models=tested_models)
    print(f"CANDIDATES from {catalog_path}")
    if not candidates:
        print("  no untested text->text candidates with context >= 32000")
    for model in candidates:
        marker = " FREE" if model.get("free") else ""
        print(
            f"  untested  {model['id']} "
            f"in={ringer.format_catalog_price(model.get('prompt_per_m'))}/M "
            f"out={ringer.format_catalog_price(model.get('completion_per_m'))}/M "
            f"ctx={int(model.get('context_length') or 0)}{marker}"
        )


def parse_log_date(value: Any) -> str:
    import ringer

    if not isinstance(value, str) or len(value) < 10:
        return ""
    candidate = value[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", candidate):
        return ""
    try:
        ringer.datetime.strptime(candidate, "%Y-%m-%d")
    except ValueError:
        return ""
    return candidate


def validate_since_date(value: str | None) -> str | None:
    if value is None:
        return None
    if not parse_log_date(value):
        raise ValueError("--since must be YYYY-MM-DD")
    return value


def model_log_row_is_retry(row: dict[str, Any]) -> bool:
    retry = row.get("retry")
    if isinstance(retry, bool):
        return retry
    if isinstance(retry, str) and retry.strip().lower() in {"true", "false"}:
        return retry.strip().lower() == "true"
    notes = row.get("notes", "")
    return isinstance(notes, str) and "retry=true" in notes


def model_log_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def model_log_row_model(row: dict[str, Any]) -> str:
    model = model_log_text(row.get("model"))
    if model:
        return model
    return model_log_text(row.get("worker_engine"))


def model_log_row_is_unattributed(row: dict[str, Any]) -> bool:
    return not model_log_text(row.get("model"))


def model_log_row_reasoning_effort(row: dict[str, Any]) -> str | None:
    effort = model_log_text(row.get("reasoning_effort"))
    return effort or None


def model_log_row_is_reserved_fixture(row: dict[str, Any]) -> bool:
    return model_log_text(row.get("model")) in RESERVED_FIXTURE_MODELS


def model_reasoning_effort_keys(rows: list[dict[str, Any]]) -> set[tuple[str, str, bool]]:
    keys: set[tuple[str, str, bool]] = set()
    for row in rows:
        if model_log_row_is_reserved_fixture(row):
            continue
        if (
            not model_log_row_is_unattributed(row)
            and model_log_row_reasoning_effort(row) is not None
        ):
            keys.add(
                (
                    model_log_row_engine(row),
                    model_log_row_model(row),
                    model_log_row_is_unattributed(row),
                )
            )
    return keys


def model_log_row_task_type(row: dict[str, Any]) -> str:
    task_type = model_log_text(row.get("task_type"))
    return task_type or "(untyped)"


def model_log_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def median_int(values: list[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) // 2


def model_log_task_base_key(row: dict[str, Any]) -> tuple[str, str, str, str, str, bool] | None:
    run_id = model_log_text(row.get("run_id"))
    task_key = model_log_text(row.get("task_key"))
    if not run_id or not task_key:
        return None
    unattributed = model_log_row_is_unattributed(row)
    return (
        run_id,
        task_key,
        model_log_row_model(row),
        model_log_row_task_type(row),
        "" if unattributed else (model_log_row_reasoning_effort(row) or ""),
        unattributed,
    )


def group_model_log_tasks(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    grouped: list[list[dict[str, Any]]] = []
    active_by_key: dict[tuple[str, str, str, str, str, bool], int] = {}
    for row in rows:
        key = model_log_task_base_key(row)
        if key is not None and model_log_row_is_retry(row) and key in active_by_key:
            grouped[active_by_key[key]].append(row)
            continue
        grouped.append([row])
        if key is not None:
            active_by_key[key] = len(grouped) - 1
    return grouped


def read_model_log_rows(
    path: Path,
    *,
    since: str | None = None,
    engine: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    skipped = 0
    try:
        fh = path.open("r", encoding="utf-8")
    except FileNotFoundError:
        return rows, skipped
    with fh:
        for line in fh:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            if not isinstance(row, dict):
                skipped += 1
                continue
            if engine is not None and model_log_text(row.get("worker_engine")) != engine:
                continue
            rows.append(row)
    if since is not None:
        selected_row_ids: set[int] = set()
        for task_rows in group_model_log_tasks(rows):
            ordered = sorted(
                task_rows,
                key=lambda row: (
                    model_log_text(row.get("logged_at")),
                    1 if model_log_row_is_retry(row) else 0,
                ),
            )
            final_date = parse_log_date(ordered[-1].get("logged_at"))
            if final_date and final_date >= since:
                selected_row_ids.update(id(row) for row in task_rows)
        rows = [row for row in rows if id(row) in selected_row_ids]
    return rows, skipped


def aggregate_model_log_rows(
    rows: list[dict[str, Any]],
    *,
    task_type: str | None = None,
    model: str | None = None,
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str, str, bool], dict[str, Any]] = {}
    effort_keys = model_reasoning_effort_keys(rows)
    for task_rows in group_model_log_tasks(rows):
        ordered = sorted(
            task_rows,
            key=lambda row: (
                model_log_text(row.get("logged_at")),
                1 if model_log_row_is_retry(row) else 0,
            ),
        )
        first = ordered[0]
        final = ordered[-1]
        if model_log_row_is_reserved_fixture(final):
            continue
        group_engine = model_log_row_engine(final)
        group_model = model_log_row_model(final)
        group_task_type = model_log_row_task_type(final)
        unattributed = model_log_row_is_unattributed(final)
        reasoning_effort = None if unattributed else model_log_row_reasoning_effort(final)
        if model is not None and group_model != model:
            continue
        if task_type is not None and group_task_type != task_type:
            continue
        key = (
            group_engine,
            group_model,
            group_task_type,
            reasoning_effort or "",
            unattributed,
        )
        group = groups.setdefault(
            key,
            {
                "engine": group_engine,
                "model": group_model,
                "task_type": group_task_type,
                "reasoning_effort": reasoning_effort,
                "show_reasoning_effort": (
                    (group_engine, group_model, unattributed) in effort_keys
                ),
                "unattributed": unattributed,
                "tasks": 0,
                "attempts": 0,
                "passed": 0,
                "failed": 0,
                "pass_rate": 0.0,
                "first_try_pass_rate": 0.0,
                "median_duration_ms": None,
                "median_tokens": None,
                "last_seen": "",
                "_first_try_passed": 0,
                "_duration_ms": [],
                "_tokens": [],
            },
        )
        group["tasks"] += 1
        group["attempts"] += len(ordered)
        if model_log_text(final.get("verdict")).upper() == "PASS":
            group["passed"] += 1
        else:
            group["failed"] += 1
        if model_log_text(first.get("verdict")).upper() == "PASS":
            group["_first_try_passed"] += 1
        duration_ms = model_log_int(final.get("duration_ms"))
        if duration_ms is not None:
            group["_duration_ms"].append(duration_ms)
        for row in ordered:
            tokens = model_log_int(row.get("worker_tokens"))
            if tokens is not None:
                group["_tokens"].append(tokens)
        logged_at = model_log_text(final.get("logged_at"))
        if logged_at > group["last_seen"]:
            group["last_seen"] = logged_at

    finalized: list[dict[str, Any]] = []
    for group in groups.values():
        tasks_count = group["tasks"]
        group["pass_rate"] = group["passed"] / tasks_count if tasks_count else 0.0
        group["first_try_pass_rate"] = (
            group["_first_try_passed"] / tasks_count if tasks_count else 0.0
        )
        group["median_duration_ms"] = median_int(group["_duration_ms"])
        group["median_tokens"] = median_int(group["_tokens"])
        finalized.append(
            {
                "engine": group["engine"],
                "model": group["model"],
                "task_type": group["task_type"],
                "reasoning_effort": group["reasoning_effort"],
                "show_reasoning_effort": group["show_reasoning_effort"],
                "unattributed": group["unattributed"],
                "tasks": group["tasks"],
                "attempts": group["attempts"],
                "passed": group["passed"],
                "failed": group["failed"],
                "pass_rate": group["pass_rate"],
                "first_try_pass_rate": group["first_try_pass_rate"],
                "median_duration_ms": group["median_duration_ms"],
                "median_tokens": group["median_tokens"],
                "last_seen": group["last_seen"],
            }
        )
    return sorted(
        attach_routing_evidence(finalized, rows),
        key=lambda item: (
            1 if item["unattributed"] else 0,
            item["task_type"],
            -item["pass_rate"],
            -item["first_try_pass_rate"],
            item["engine"],
            item["model"],
            item["reasoning_effort"] or "",
        ),
    )


MODEL_SCOREBOARD_RUN_NAME = "model-scoreboard"
MODEL_SCOREBOARD_IDENTITY = "ringer-models"
MODEL_SCOREBOARD_COLUMNS = (
    "Model",
    "Lab",
    "Harness",
    "API/Plan",
    "Tier",
    "Tasks",
    "First try",
    "Pass",
    "Tokens (median)",
    "Speed (median)",
    "Last used",
    "Notes",
)


def default_model_notes_path() -> Path:
    return Path(__file__).resolve().parent / "docs" / "MODEL-NOTES.md"


def should_use_read_model_db(
    *,
    log_path: Path,
    default_log_path: Path,
    explicit_db: bool,
) -> bool:
    if explicit_db:
        return True
    return log_path.expanduser().resolve() == default_log_path.expanduser().resolve()


def model_log_row_engine(row: dict[str, Any]) -> str:
    return model_log_text(row.get("worker_engine") if "worker_engine" in row else row.get("engine"))


def row_identity_fields(row: dict[str, Any], registry: ModelIdentityRegistry) -> dict[str, Any]:
    if model_log_row_is_unattributed(row):
        engine = model_log_row_engine(row)
        meta = registry.engine_meta.get(engine)
        return {
            "model_display": UNATTRIBUTED_MODEL_DISPLAY,
            "lab": "(unknown)",
            "harness": meta.harness if meta else (engine or "unknown"),
            "access": meta.access if meta else "unknown",
            "alias": False,
            "last_verified": "",
            "unregistered": False,
            "misrouted": False,
            "identity_key": "",
            "canonical_route": "",
        }
    identity = registry.resolve(model_log_row_engine(row), model_log_text(row.get("model")))
    return {
        "model_display": identity.model_display,
        "lab": identity.lab,
        "harness": identity.harness,
        "access": identity.access,
        "alias": identity.alias,
        "last_verified": identity.last_verified,
        "unregistered": identity.unregistered,
        "misrouted": identity.misrouted,
        "identity_key": identity.canonical_model_key or model_log_text(row.get("model")),
        "canonical_route": (
            f"{identity.canonical_engine}:{identity.canonical_model_key} via "
            f"{identity.canonical_harness} on {identity.canonical_access}"
            if identity.misrouted
            else ""
        ),
    }


def task_final_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    finals: list[dict[str, Any]] = []
    for task_rows in group_model_log_tasks(rows):
        ordered = sorted(
            task_rows,
            key=lambda row: (
                model_log_text(row.get("logged_at")),
                1 if model_log_row_is_retry(row) else 0,
            ),
        )
        if ordered:
            finals.append(ordered[-1])
    return finals


def enrich_model_groups_with_identity(
    groups: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    registry: ModelIdentityRegistry,
    *,
    include_task_type: bool,
    catalog_models: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    catalog_by_id = catalog_models_by_id(catalog_models or [])
    identity_rows: dict[tuple[Any, ...], dict[str, Any]] = {}
    latest: dict[tuple[Any, ...], str] = {}
    for row in task_final_rows(rows):
        if model_log_row_is_reserved_fixture(row):
            continue
        group_engine = model_log_row_engine(row)
        group_model = model_log_row_model(row)
        group_task_type = model_log_row_task_type(row)
        unattributed = model_log_row_is_unattributed(row)
        reasoning_effort = None if unattributed else model_log_row_reasoning_effort(row)
        key: tuple[Any, ...]
        key = (
            (group_engine, group_model, group_task_type, reasoning_effort, unattributed)
            if include_task_type
            else (group_engine, group_model, reasoning_effort, unattributed)
        )
        logged_at = model_log_text(row.get("logged_at"))
        if key not in latest or logged_at >= latest[key]:
            latest[key] = logged_at
            identity = row_identity_fields(row, registry)
            if identity.get("unregistered"):
                identity.update(
                    catalog_identity_fields(model_log_text(row.get("model")), catalog_by_id)
                )
            identity_rows[key] = identity
    enriched: list[dict[str, Any]] = []
    for group in groups:
        key = (
            (
                str(group.get("engine") or ""),
                str(group.get("model") or ""),
                str(group.get("task_type") or ""),
                group.get("reasoning_effort"),
                bool(group.get("unattributed")),
            )
            if include_task_type
            else (
                str(group.get("engine") or ""),
                str(group.get("model") or ""),
                group.get("reasoning_effort"),
                bool(group.get("unattributed")),
            )
        )
        item = dict(group)
        item.update(
            identity_rows.get(
                key,
                {
                    "model_display": str(group.get("model") or ""),
                    "lab": "(unknown)",
                    "harness": "unknown",
                    "access": "unknown",
                    "alias": False,
                    "last_verified": "",
                    "unregistered": bool(group.get("model")),
                    "misrouted": False,
                    "identity_key": str(group.get("model") or ""),
                    "canonical_route": "",
                },
            )
        )
        if item.get("unregistered") and str(item.get("model") or "").startswith("openrouter/"):
            if item.get("model_display") == item.get("model"):
                item["model_display"] = short_model_name(item.get("model"))
        if item.get("show_reasoning_effort") and not item.get("unattributed"):
            effort = item.get("reasoning_effort") or "(effort unrecorded)"
            item["model_display"] = f"{item['model_display']} · {effort}"
        if item.get("unattributed"):
            # The aggregation helper retains the engine as a legacy grouping key.
            # Public payloads must not expose harness branding as a model identity.
            item["model"] = ""
        if item.get("unattributed") or item.get("misrouted"):
            item["tier"] = "unranked"
        elif "tier" not in item:
            item["tier"] = model_scoreboard_tier(
                int(item.get("tasks") or 0), float(item.get("first_try_pass_rate") or 0)
            )
        item["bucket_id"] = "|".join(
            (
                str(item.get("engine") or ""),
                str(item.get("model") or ""),
                str(item.get("reasoning_effort") or ""),
                "unattributed" if item.get("unattributed") else "model",
            )
        )
        item["display_bucket_id"] = "bucket-" + base64.urlsafe_b64encode(
            item["bucket_id"].encode("utf-8")
        ).decode("ascii").rstrip("=")
        enriched.append(item)
    return enriched


def normalize_notes_match_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().lower()


def parse_model_notes_sections(path: Path) -> dict[str, list[str]]:
    """Return dated bullet blocks keyed by the raw level-2 heading text."""
    try:
        lines = path.expanduser().read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return {}
    sections: dict[str, list[str]] = {}
    current_heading: str | None = None
    current_bullets: list[str] = []
    active_bullet: list[str] | None = None

    def flush_bullet() -> None:
        nonlocal active_bullet
        if active_bullet is not None:
            text = "\n".join(active_bullet).strip()
            if re.search(r"\b\d{4}-\d{2}-\d{2}\b", text):
                current_bullets.append(text)
            active_bullet = None

    def flush_section() -> None:
        flush_bullet()
        if current_heading is not None:
            sections[current_heading] = list(current_bullets)

    for line in lines:
        heading_match = re.match(r"^##\s+(.+?)\s*$", line)
        if heading_match:
            flush_section()
            current_heading = heading_match.group(1).strip()
            current_bullets = []
            active_bullet = None
            continue
        if current_heading is None:
            continue
        if line.startswith("## "):
            flush_section()
            current_heading = None
            current_bullets = []
            active_bullet = None
            continue
        if line.startswith("- "):
            flush_bullet()
            active_bullet = [line[2:].strip()]
            continue
        if active_bullet is not None and (line.startswith("  ") or not line.strip()):
            active_bullet.append(line.strip())
            continue
        flush_bullet()
    flush_section()
    return sections


def model_judgment_notes(model_id: str, notes_sections: dict[str, list[str]]) -> list[str]:
    needle = normalize_notes_match_text(model_id)
    if not needle:
        return []
    id_boundary = r"A-Za-z0-9._/:-"
    needle_re = re.compile(rf"(?<![{id_boundary}]){re.escape(needle)}(?![{id_boundary}])")
    matches: list[tuple[int, int, int, list[str]]] = []
    for index, (heading, bullets) in enumerate(notes_sections.items()):
        normalized_heading = normalize_notes_match_text(heading)
        if not needle_re.search(normalized_heading):
            continue
        exact_score = 1 if normalized_heading == needle else 0
        matches.append((exact_score, len(normalized_heading), -index, bullets))
    if not matches:
        return []
    return max(matches, key=lambda item: (item[0], item[1], item[2]))[3]


def model_judgment_notes_for_row(
    row: dict[str, Any], notes_sections: dict[str, list[str]]
) -> list[str]:
    display = re.sub(
        r"\s+·\s+(?:[^·]+)$", "", str(row.get("model_display") or "")
    ).strip()
    candidates = (
        str(row.get("identity_key") or "").strip(),
        display,
        str(row.get("model") or "").strip(),
    )
    for candidate in candidates:
        if not candidate:
            continue
        notes = model_judgment_notes(candidate, notes_sections)
        if notes:
            return notes
    return []


def enrich_model_groups_with_notes(
    groups: list[dict[str, Any]], notes_sections: dict[str, list[str]]
) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for group in groups:
        item = dict(group)
        notes = [
            scoreboard_safe_note(note)
            for note in sorted(
                model_judgment_notes_for_row(item, notes_sections),
                key=note_date_key,
                reverse=True,
            )
        ]
        item["notes"] = notes
        item["latest_note"] = strip_inline_markdown(notes[0]) if notes else ""
        enriched.append(item)
    return enriched


def strip_inline_markdown(value: str) -> str:
    text = re.sub(r"`([^`]*)`", r"\1", value)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"[*_]{1,3}([^*_]+)[*_]{1,3}", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def scoreboard_safe_note(value: str) -> str:
    return re.sub(
        r"openrouter/[A-Za-z0-9._/+:-]+",
        lambda match: short_model_name(match.group(0)),
        value,
    )


def normalized_judgment_note(item: str) -> tuple[str, str] | None:
    text = re.sub(r"\s+", " ", item).strip()
    match = re.match(r"^-?\s*(\d{4}-\d{2}-\d{2})\s+(?:[-\u2013\u2014]+\s*)?(.*)$", text)
    if not match:
        return None
    body = strip_inline_markdown(match.group(2))
    if not body:
        return None
    date = humanized_log_date(match.group(1))
    short_date = re.sub(r",\s*\d{4}$", "", date)
    return short_date, body


def note_date_key(item: str) -> str:
    match = re.search(r"\b\d{4}-\d{2}-\d{2}\b", item)
    return match.group(0) if match else ""


def fmt_scoreboard_duration(value_ms: Any) -> str:
    if value_ms is None:
        return ""
    try:
        total = max(0, int(round(float(value_ms) / 1000.0)))
    except (TypeError, ValueError):
        return ""
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{seconds:02d}s"
    if minutes:
        return f"{minutes}m{seconds:02d}s"
    return f"{seconds}s"


def render_notes_list(items: list[str], *, notes_path: Path | None = None, limit: int = 5) -> str:
    if not items:
        return '<p class="empty-note">no judgment notes yet</p>'
    ordered_items = sorted(items, key=note_date_key, reverse=True)
    rendered = []
    for item in ordered_items[:limit]:
        note = normalized_judgment_note(item)
        if note is None:
            body = strip_inline_markdown(item)
            if not body:
                continue
            rendered.append(f"<li><span>{html_escape(body)}</span></li>")
            continue
        date, body = note
        rendered.append(
            f'<li><time>{html_escape(date)}</time><span>{html_escape(body)}</span></li>'
        )
    if not rendered:
        return '<p class="empty-note">no judgment notes yet</p>'
    more = ""
    if len(ordered_items) > limit and notes_path is not None:
        more = (
            f'<li class="more-notes">{source_file_link(notes_path, "more in model notes")}</li>'
        )
    return f'<ul class="notes-list">{"".join(rendered)}{more}</ul>'


def normalize_catalog_for_scoreboard(model: dict[str, Any]) -> dict[str, Any]:
    import ringer

    if "prompt_per_m" in model and "completion_per_m" in model:
        return model
    if isinstance(model.get("pricing"), dict):
        return ringer.normalize_catalog_model(model, fetched_at=str(model.get("fetched_at") or ""))
    return model


def catalog_models_by_id(catalog_models: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for model in catalog_models:
        model_id = str(model.get("id") or "").strip()
        if model_id:
            by_id[model_id] = normalize_catalog_for_scoreboard(model)
    return by_id


def catalog_identity_fields(
    model_key: str,
    catalog_by_id: dict[str, dict[str, Any]],
) -> dict[str, str]:
    if not model_key.startswith("openrouter/"):
        return {}
    catalog_id = model_key.removeprefix("openrouter/")
    model = catalog_by_id.get(catalog_id) or catalog_by_id.get(model_key)
    if model is None:
        return {}
    name = model_log_text(model.get("name"))
    if not name or name in {catalog_id, model_key}:
        return {}
    display = name.removesuffix(" (free)").strip()
    org = catalog_id.split("/", 1)[0] if "/" in catalog_id else ""
    lab = f"{org}?" if org else "(unverified)"
    if ":" in display:
        prefix, candidate = (part.strip() for part in display.split(":", 1))
        if candidate:
            display = candidate
        if prefix:
            lab = f"{prefix}?"
    return {"model_display": display, "lab": lab}


def model_scoreboard_tier(tasks: int, first_try_pass_rate: float, final_check_pass_rate: float = 0) -> str:
    if (tasks >= PROVEN_MIN_TASKS and first_try_pass_rate >= PROVEN_MIN_FIRST_TRY
            and final_check_pass_rate >= PROVEN_MIN_FINAL_CHECK):
        return "proven"
    return "probation"


def model_scoreboard_tier_rank(tier: str) -> int:
    return {"proven": 0, "probation": 1}.get(tier, 3)


def aggregate_model_scoreboard_rows(
    rows: list[dict[str, Any]],
    *,
    task_type: str | None = None,
    model: str | None = None,
) -> list[dict[str, Any]]:
    models: dict[tuple[str, str, str, bool], dict[str, Any]] = {}
    effort_keys = model_reasoning_effort_keys(rows)
    for task_rows in group_model_log_tasks(rows):
        ordered = sorted(
            task_rows,
            key=lambda row: (
                model_log_text(row.get("logged_at")),
                1 if model_log_row_is_retry(row) else 0,
            ),
        )
        first = ordered[0]
        final = ordered[-1]
        if model_log_row_is_reserved_fixture(final):
            continue
        group_engine = model_log_row_engine(final)
        group_model = model_log_row_model(final)
        group_task_type = model_log_row_task_type(final)
        unattributed = model_log_row_is_unattributed(final)
        reasoning_effort = None if unattributed else model_log_row_reasoning_effort(final)
        if model is not None and group_model != model:
            continue
        if task_type is not None and group_task_type != task_type:
            continue
        model_key = (group_engine, group_model, reasoning_effort or "", unattributed)
        model_entry = models.setdefault(
            model_key,
            {
                "engine": group_engine,
                "model": group_model,
                "reasoning_effort": reasoning_effort,
                "show_reasoning_effort": (
                    (group_engine, group_model, unattributed) in effort_keys
                ),
                "unattributed": unattributed,
                "tasks": 0,
                "attempts": 0,
                "passed": 0,
                "failed": 0,
                "retries": 0,
                "first_try_passed": 0,
                "last_seen": "",
                "_duration_ms": [],
                "_tokens": [],
                "_task_types": {},
            },
        )
        breakdown = model_entry["_task_types"].setdefault(
            group_task_type,
            {
                "task_type": group_task_type,
                "tasks": 0,
                "attempts": 0,
                "passed": 0,
                "failed": 0,
                "first_try_passed": 0,
                "last_seen": "",
            },
        )
        passed = model_log_text(final.get("verdict")).upper() == "PASS"
        first_passed = model_log_text(first.get("verdict")).upper() == "PASS"
        for target in (model_entry, breakdown):
            target["tasks"] += 1
            target["attempts"] += len(ordered)
            target["passed"] += 1 if passed else 0
            target["failed"] += 0 if passed else 1
            target["first_try_passed"] += 1 if first_passed else 0
            logged_at = model_log_text(final.get("logged_at"))
            if logged_at > target["last_seen"]:
                target["last_seen"] = logged_at
        model_entry["retries"] += max(0, len(ordered) - 1)
        duration_ms = model_log_int(final.get("duration_ms"))
        if duration_ms is not None:
            model_entry["_duration_ms"].append(duration_ms)
        for row in ordered:
            tokens = model_log_int(row.get("worker_tokens"))
            if tokens is not None:
                model_entry["_tokens"].append(tokens)

    finalized: list[dict[str, Any]] = []
    for entry in models.values():
        tasks_count = int(entry["tasks"])
        breakdown_rows = []
        for breakdown in entry["_task_types"].values():
            b_tasks = int(breakdown["tasks"])
            breakdown_rows.append(
                {
                    "task_type": breakdown["task_type"],
                    "tasks": b_tasks,
                    "attempts": breakdown["attempts"],
                    "passed": breakdown["passed"],
                    "failed": breakdown["failed"],
                    "first_try_pass_rate": breakdown["first_try_passed"] / b_tasks if b_tasks else 0.0,
                    "pass_rate": breakdown["passed"] / b_tasks if b_tasks else 0.0,
                    "last_seen": breakdown["last_seen"],
                }
            )
        breakdown_rows.sort(key=lambda item: (-item["tasks"], item["task_type"]))
        first_try_rate = entry["first_try_passed"] / tasks_count if tasks_count else 0.0
        tier = (
            "unranked"
            if entry["unattributed"]
            else model_scoreboard_tier(tasks_count, first_try_rate)
        )
        finalized.append(
            {
                "engine": entry["engine"],
                "model": entry["model"],
                "reasoning_effort": entry["reasoning_effort"],
                "show_reasoning_effort": entry["show_reasoning_effort"],
                "unattributed": entry["unattributed"],
                "tier": tier,
                "tasks": tasks_count,
                "attempts": entry["attempts"],
                "retries": entry["retries"],
                "passed": entry["passed"],
                "failed": entry["failed"],
                "first_try_pass_rate": entry["first_try_passed"] / tasks_count if tasks_count else 0.0,
                "pass_rate": entry["passed"] / tasks_count if tasks_count else 0.0,
                "median_duration_ms": median_int(entry["_duration_ms"]),
                "median_tokens": median_int(entry["_tokens"]),
                "last_seen": entry["last_seen"],
                "task_types": breakdown_rows,
            }
        )
    return attach_routing_evidence(finalized, rows)


def estimated_task_cost(row: dict[str, Any], catalog_model: dict[str, Any] | None) -> float | None:
    # Total tokens cannot establish the input/cache/output mix. Only a complete
    # observed per-task actual-cost summary may be displayed as task cost.
    if row.get("cost_coverage") != "provider_actual":
        return None
    value = row.get("median_actual_cost_usd")
    try:
        cost = Decimal(str(value))
        return float(cost) if cost.is_finite() and cost >= 0 else None
    except (ValueError, InvalidOperation):
        return None


def model_sort_cost(row: dict[str, Any], catalog_model: dict[str, Any] | None) -> float:
    cost = estimated_task_cost(row, catalog_model)
    return cost if cost is not None else float("inf")


def order_model_scoreboard_rows(
    rows: list[dict[str, Any]],
    catalog_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    return sorted(
        rows,
        key=lambda row: (
            1 if row.get("unattributed") else 0,
            model_scoreboard_tier_rank(str(row.get("tier") or "")),
            -float(row.get("first_try_pass_rate") or 0),
            -float(row.get("pass_rate") or 0),
            model_sort_cost(row, catalog_by_id.get(str(row.get("model") or ""))),
            str(row.get("engine") or ""),
            str(row.get("model") or ""),
            str(row.get("reasoning_effort") or ""),
        ),
    )


def fmt_percent(value: Any) -> str:
    try:
        return f"{float(value) * 100:.0f}%"
    except (TypeError, ValueError):
        return "0%"


def fmt_int(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "0"


def fmt_task_cost(value: float | None) -> str:
    if value is None:
        return ""
    if value == 0:
        return "$0/task"
    if value < 0.01:
        return f"${value:.4f}/task"
    return f"${value:.2f}/task"


def fmt_short_task_cost(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value == 0:
        return "free"
    if value < 0.10:
        cents = value * 100
        if cents < 1:
            return "<1¢"
        rounded = round(cents)
        return f"~{rounded}¢"
    return f"${value:.2f}"


def humanized_log_date(value: Any, *, prefix: str = "") -> str:
    import ringer

    text = model_log_text(value)
    if not text:
        return f"{prefix}unknown" if prefix else "unknown"
    candidate = text[:10]
    try:
        dt = ringer.datetime.strptime(candidate, "%Y-%m-%d")
    except ValueError:
        return f"{prefix}{text}" if prefix else text
    rendered = f"{dt.strftime('%B')} {dt.day}, {dt.year}"
    return f"{prefix}{rendered}" if prefix else rendered


def humanize_dates_in_text(value: str) -> str:
    return re.sub(
        r"\b\d{4}-\d{2}-\d{2}\b",
        lambda match: humanized_log_date(match.group(0)),
        value,
    )


def source_file_link(path: Path, label: str) -> str:
    import ringer

    resolved = path.expanduser().resolve()
    return (
        f'<a class="source-link" href="{html_escape(ringer.file_href(resolved))}" '
        f'title="{html_escape(str(resolved))}">{html_escape(label)}</a>'
    )


def model_task_cost_label(row: dict[str, Any], catalog_model: dict[str, Any] | None) -> str:
    cost = estimated_task_cost(row, catalog_model)
    return fmt_short_task_cost(cost) if cost is not None else "unknown"


def rate_bar_html(value: Any) -> str:
    try:
        pct = max(0.0, min(100.0, float(value) * 100))
    except (TypeError, ValueError):
        pct = 0.0
    return (
        '<span class="rate-meter" aria-hidden="true">'
        f'<span class="rate-bar bar-fill" style="width: {pct:.0f}%"></span>'
        "</span>"
    )


def rate_cell_html(value: Any) -> str:
    return (
        f'<span class="rate-value">{html_escape(fmt_percent(value))}</span>'
        f"{rate_bar_html(value)}"
    )


def compact_context_label(value: Any) -> str:
    try:
        ctx = int(value)
    except (TypeError, ValueError):
        return "unknown ctx"
    if ctx >= 1_000_000 and ctx % 1_000_000 == 0:
        return f"{ctx // 1_000_000}M ctx"
    if ctx >= 1_000:
        if ctx % 1_000 == 0:
            return f"{ctx // 1_000}K ctx"
        return f"{ctx / 1000:.1f}K ctx"
    return f"{ctx} ctx"


def short_model_name(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "unknown"
    text = text.removeprefix("openrouter/")
    text = text.removesuffix(":free")
    if "/" in text:
        text = text.rsplit("/", 1)[-1]
    text = re.sub(r"[-_]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return "unknown"
    parts = []
    for part in text.split():
        parts.append(part.upper() if part.lower() in {"gpt", "glm", "ai", "llm"} else part.capitalize())
    return " ".join(parts)


def catalog_model_display_name(model: dict[str, Any]) -> str:
    name = str(model.get("name") or "").strip()
    model_id = str(model.get("id") or "").strip()
    if name and name != model_id:
        return name.removesuffix(" (free)").strip()
    return short_model_name(model_id)


def humanized_short_date(value: Any) -> str:
    return re.sub(r",\s*\d{4}$", "", humanized_log_date(value))


def humanized_catalog_event_line(event: dict[str, Any], catalog_by_id: dict[str, dict[str, Any]]) -> str:
    model_id = str(event.get("id") or "")
    if model_id in catalog_by_id:
        label = catalog_model_display_name(catalog_by_id[model_id])
    else:
        label = short_model_name(model_id)
    kind = str(event.get("kind") or "event")
    date = humanized_short_date(event.get("ts"))
    if kind == "went_free":
        action = "went free"
    elif kind == "went_paid":
        action = "went paid"
    elif kind == "pricing_variable":
        action = "moved to variable pricing"
    elif kind == "pricing_fixed":
        action = "returned to fixed pricing"
    elif kind == "added":
        action = "was added"
    elif kind == "removed":
        action = "was removed"
    else:
        action = kind.replace("_", " ")
    return f"{label} {action} — {date}"


def watchlist_chip_html(model: dict[str, Any]) -> str:
    label = catalog_model_display_name(model)
    context = compact_context_label(model.get("context_length"))
    return (
        '<li class="watch-chip">'
        f'<span title="{html_escape(label)}">'
        f"{html_escape(label)} · {html_escape(context)}</span></li>"
    )


def derived_quality_text(row: dict[str, Any], *, best: bool) -> str:
    candidates = [item for item in row.get("task_types", []) if int(item.get("tasks") or 0) >= 2]
    if not candidates:
        return "not enough per-task evidence yet"
    if best:
        chosen = max(candidates, key=lambda item: (float(item.get("first_try_pass_rate") or 0), int(item.get("tasks") or 0), str(item.get("task_type") or "")))
        prefix = "best derived"
    else:
        chosen = min(candidates, key=lambda item: (float(item.get("first_try_pass_rate") or 0), -int(item.get("tasks") or 0), str(item.get("task_type") or "")))
        prefix = "worst derived"
    return (
        f"{prefix}: {chosen['task_type']} "
        f"({fmt_percent(chosen.get('first_try_pass_rate'))} first-try, "
        f"{fmt_percent(chosen.get('pass_rate'))} pass, n={fmt_int(chosen.get('tasks'))})"
    )


def render_task_breakdown_table(task_rows: list[dict[str, Any]]) -> str:
    if not task_rows:
        return '<p class="empty-note">no task-type breakdown</p>'
    rows = []
    for item in task_rows:
        rows.append(
            f"""<tr>
      <td>{html_escape(str(item.get("task_type") or ""))}</td>
      <td class="num">{fmt_int(item.get("tasks"))}</td>
      <td class="num rate-cell">{rate_cell_html(item.get("first_try_pass_rate"))}</td>
      <td class="num rate-cell">{rate_cell_html(item.get("pass_rate"))}</td>
    </tr>"""
        )
    return f"""<table class="breakdown">
    <thead><tr><th>task_type</th><th>n</th><th>first-try</th><th>pass</th></tr></thead>
    <tbody>{"".join(rows)}</tbody>
  </table>"""


MODEL_SCOREBOARD_CSS = """
  .scoreboard-page { max-width: 1240px; }
  .scoreboard-header {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 18px;
    align-items: end;
    margin-bottom: clamp(14px, 2.5vw, 22px);
  }
  .scoreboard-title {
    margin: 0;
    font-size: clamp(28px, 5vw, 54px);
    line-height: .98;
    letter-spacing: 0;
    text-wrap: balance;
  }
  .scoreboard-meta {
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: 12px;
    flex-wrap: wrap;
    color: var(--muted);
    font-size: 12px;
  }
  .source-links {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }
  .source-link {
    color: var(--ink);
    text-decoration: none;
    border-bottom: 1px solid var(--hairline);
  }
  .source-link:hover { border-bottom-color: var(--ink); }
  .watchlist {
    border-top: 1px solid var(--hairline);
    border-bottom: 1px solid var(--hairline);
    padding: 12px 0;
    margin: 0 0 clamp(18px, 3vw, 28px);
    color: var(--muted);
    font-size: 13px;
  }
  .watchline {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
  }
  .watch-title { color: var(--ink); font-weight: 650; }
  .watch-chips, .event-list {
    display: contents;
    margin: 0;
    padding: 0;
    list-style: none;
  }
  .watch-chip {
    border: 1px solid var(--hairline);
    padding: 3px 7px 4px;
    font-size: 12px;
    color: var(--ink);
    background: rgba(255, 255, 255, .03);
  }
  .event-list li {
    color: var(--muted);
  }
  .event-list li::before {
    content: "/";
    color: var(--hairline);
    margin: 0 8px 0 2px;
  }
  .table-scroll {
    overflow-x: auto;
    border: 1px solid var(--hairline);
    border-left: 0;
    border-right: 0;
  }
  .ranked-table {
    width: 100%;
    min-width: 1500px;
    border-collapse: collapse;
    font-size: 13px;
  }
  .ranked-table th {
    color: var(--muted);
    font-size: 11px;
    font-weight: 650;
    letter-spacing: .08em;
    text-transform: uppercase;
    text-align: left;
    padding: 10px 12px;
    border-bottom: 1px solid var(--hairline);
    white-space: nowrap;
  }
  .ranked-table td {
    padding: 17px 12px;
    border-bottom: 1px solid var(--hairline);
    vertical-align: middle;
    color: var(--ink);
  }
  .ranked-table tr.model-row:hover td {
    background: color-mix(in srgb, var(--surface) 48%, transparent);
  }
  .rank-cell {
    width: 58px;
    font-size: 16px;
    font-weight: 760;
  }
  .model-cell { min-width: 230px; }
  .model-name {
    font-size: 16px;
    font-weight: 760;
    line-height: 1.2;
    overflow-wrap: anywhere;
  }
  .model-id {
    margin-top: 3px;
    color: var(--muted);
    font-size: 12px;
    overflow-wrap: anywhere;
  }
  .identity-flag, .verified-date {
    display: block;
    margin-top: 3px;
    color: var(--muted);
    font-size: 11px;
  }
  .tier-badge {
    display: inline-flex;
    align-items: center;
    min-width: 78px;
    justify-content: center;
    padding: 3px 8px 4px;
    border-radius: 4px;
    font-size: 12px;
    font-weight: 700;
    color: var(--ink);
    border: 1px solid transparent;
  }
  .tier-badge.proven {
    background: color-mix(in srgb, var(--pass) 30%, transparent);
    border-color: color-mix(in srgb, var(--pass) 58%, var(--hairline));
  }
  .tier-badge.probation {
    background: color-mix(in srgb, var(--accent) 24%, transparent);
    border-color: color-mix(in srgb, var(--accent) 48%, var(--hairline));
  }
  .num {
    text-align: right;
    font-variant-numeric: tabular-nums;
  }
  .rate-cell {
    min-width: 108px;
  }
  .rate-value {
    display: inline-block;
    min-width: 38px;
  }
  .rate-meter {
    display: inline-block;
    width: 48px;
    height: 6px;
    margin-left: 8px;
    vertical-align: 1px;
    border-radius: 4px;
    background: color-mix(in srgb, var(--muted) 22%, transparent);
    overflow: hidden;
  }
  .rate-bar {
    display: block;
    height: 100%;
    border-radius: 4px;
    background: var(--pass);
  }
  .detail-row td {
    padding: 0;
    background: color-mix(in srgb, var(--surface) 55%, transparent);
  }
  .model-detail {
    padding: 0;
  }
  .model-detail summary {
    cursor: pointer;
    color: var(--ink);
    padding: 11px 12px;
    font-size: 13px;
    border-bottom: 1px solid var(--hairline);
  }
  .model-detail summary:hover { background: color-mix(in srgb, var(--surface) 70%, transparent); }
  .detail-content {
    display: grid;
    grid-template-columns: minmax(0, .86fr) minmax(260px, 1.14fr);
    gap: 22px;
    padding: 16px 12px 20px;
  }
  .detail-heading {
    margin: 0 0 8px;
    color: var(--muted);
    font-size: 11px;
    font-weight: 650;
    letter-spacing: .08em;
    text-transform: uppercase;
  }
  .quality-lines {
    display: grid;
    gap: 6px;
    margin: 12px 0 0;
    color: var(--muted);
    font-size: 13px;
  }
  .quality-lines b { color: var(--ink); }
  .notes-panel {
    min-width: 0;
  }
  .breakdown {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
  }
  .breakdown th, .breakdown td {
    border-bottom: 1px solid var(--hairline);
    padding: 8px 6px;
    text-align: left;
  }
  .breakdown th {
    color: var(--muted);
    font-weight: 650;
  }
  .notes-list {
    display: grid;
    gap: 10px;
    margin: 0;
    padding: 0;
    list-style: none;
    color: var(--ink);
  }
  .notes-list li {
    display: grid;
    grid-template-columns: 72px minmax(0, 1fr);
    gap: 12px;
    align-items: baseline;
  }
  .notes-list time {
    color: var(--muted);
    font-size: 11px;
    font-variant-caps: all-small-caps;
    letter-spacing: .08em;
    white-space: nowrap;
  }
  .more-notes {
    color: var(--muted);
    font-size: 12px;
  }
  .empty-note { color: var(--muted); margin: 0; }
  .muted { color: var(--muted); }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace; }
  footer.scoreboard-footer {
    margin-top: 14px;
    color: var(--muted);
    font-size: 12px;
  }
  @media (prefers-color-scheme: dark) {
    .watch-chip { background: rgba(255, 255, 255, .035); }
  }
  @media (max-width: 820px) {
    body { padding: 18px 14px; }
    .scoreboard-header, .detail-content { grid-template-columns: 1fr; }
    .scoreboard-meta { justify-content: flex-start; }
    .table-scroll { margin-left: -14px; margin-right: -14px; padding-left: 14px; }
    .notes-list li { grid-template-columns: 1fr; gap: 2px; }
  }
"""


def render_free_watchlist(
    catalog_models: list[dict[str, Any]],
    catalog_path: Path,
    *,
    events: list[dict[str, Any]] | None = None,
) -> str:
    import ringer

    normalized = [normalize_catalog_for_scoreboard(model) for model in catalog_models]
    catalog_by_id = catalog_models_by_id(normalized)
    free_models = sorted(
        (model for model in normalized if model.get("free")),
        key=lambda item: (-int(item.get("context_length") or 0), str(item.get("id") or "")),
    )
    if free_models:
        free_items = "".join(
            watchlist_chip_html(model)
            for model in free_models[:6]
        )
    else:
        free_items = '<li class="muted">no free catalog models in the current snapshot</li>'
    if events is None:
        events = ringer.read_catalog_events(ringer.catalog_changes_path(catalog_path), limit=6)
    event_items = "".join(
        f"<li>{html_escape(humanized_catalog_event_line(event, catalog_by_id))}</li>"
        for event in events[:3]
    )
    if not event_items:
        event_items = '<li class="muted">no catalog change log found</li>'
    return f"""<section class="watchlist" aria-label="free model watchlist">
    <div class="watchline">
      <span class="watch-title">{fmt_int(len(free_models))} models free on OpenRouter right now</span>
      <ul class="watch-chips">{free_items}</ul>
      <ul class="event-list">{event_items}</ul>
    </div>
  </section>"""


def render_model_table_pair(
    row: dict[str, Any],
    *,
    notes_sections: dict[str, list[str]],
    notes_path: Path,
) -> str:
    import ringer

    model_id = str(row.get("model") or "")
    model_display = str(row.get("model_display") or model_id)
    lab = str(row.get("lab") or "(unknown)")
    harness = str(row.get("harness") or "unknown")
    access = str(row.get("access") or "unknown")
    last_verified = str(row.get("last_verified") or "")
    notes = list(row.get("notes") or model_judgment_notes_for_row(row, notes_sections))
    latest_note = str(
        row.get("latest_note") or (strip_inline_markdown(notes[0]) if notes else "")
    )
    notes_title = "\n\n".join(strip_inline_markdown(note) for note in notes)
    if row.get("unattributed"):
        model_id_line = (
            f'<div class="model-id">engine: {html_escape(str(row.get("engine") or "unknown"))} '
            '· quarantined legacy data</div>'
        )
    else:
        model_id_line = ""
    tier = str(row.get("tier") or "")
    tier_display = (
        "not ranked" if row.get("unattributed") or row.get("misrouted") else tier
    )
    row_id = str(row.get("display_bucket_id") or "model")
    unregistered_flag = (
        '<span class="identity-flag">unregistered</span>' if row.get("unregistered") else ""
    )
    misrouted_flag = (
        '<span class="identity-flag misrouted">misrouted</span>'
        if row.get("misrouted")
        else ""
    )
    verified_date = (
        f'<span class="verified-date">verified {html_escape(last_verified)}</span>'
        if last_verified
        else ""
    )
    return f"""<tr class="model-row" id="model-{html_escape(ringer.sanitize_artifact_name(row_id))}">
      <td class="model-cell"><div class="model-name">{html_escape(model_display)}</div>{model_id_line}{unregistered_flag}{misrouted_flag}</td>
      <td>{html_escape(lab)}{verified_date}</td>
      <td>{html_escape(harness)}</td>
      <td>{html_escape(access)}</td>
      <td><span class="tier-badge {html_escape(tier)}">{html_escape(tier_display)}</span></td>
      <td class="num">{fmt_int(row.get("tasks"))}</td>
      <td class="num rate-cell">{rate_cell_html(row.get("first_try_pass_rate"))}</td>
      <td class="num rate-cell">{rate_cell_html(row.get("pass_rate"))}</td>
      <td class="num">{html_escape(fmt_int(row.get("median_tokens"))) if row.get("median_tokens") is not None else ""}</td>
      <td>{html_escape(fmt_scoreboard_duration(row.get("median_duration_ms")))}</td>
      <td>{html_escape(humanized_log_date(row.get("last_seen")))}</td>
      <td class="notes-cell" title="{html_escape(notes_title)}">{html_escape(latest_note)}</td>
    </tr>
    <tr class="detail-row">
      <td colspan="12">
        <details class="model-detail">
          <summary>details for {html_escape(model_display)}</summary>
          <div class="detail-content">
            <div>
              <h3 class="detail-heading">Task types</h3>
              {render_task_breakdown_table(row.get("task_types", []))}
              <div class="quality-lines">
                <div><b>Best:</b> {html_escape(derived_quality_text(row, best=True))}</div>
                <div><b>Worst:</b> {html_escape(derived_quality_text(row, best=False))}</div>
              </div>
            </div>
            <div class="notes-panel">
              <h3 class="detail-heading">Judgment notes</h3>
              {render_notes_list(notes, notes_path=notes_path)}
            </div>
          </div>
        </details>
      </td>
    </tr>"""


def render_model_scoreboard_html(
    *,
    rows: list[dict[str, Any]],
    log_path: Path,
    rows_read: int,
    skipped: int,
    catalog_path: Path,
    catalog_models: list[dict[str, Any]],
    notes_path: Path,
    notes_sections: dict[str, list[str]],
    catalog_events: list[dict[str, Any]] | None = None,
    generated_at: str | None = None,
) -> str:
    import ringer

    catalog_by_id = catalog_models_by_id(catalog_models)
    ordered = order_model_scoreboard_rows(rows, catalog_by_id)
    generated = generated_at or ringer.datetime.now().astimezone().replace(microsecond=0).isoformat()
    rendered_rows: list[str] = []
    for row in ordered:
        rendered_rows.append(
            render_model_table_pair(
                row,
                notes_sections=notes_sections,
                notes_path=notes_path,
            )
        )
    table_rows = "".join(rendered_rows)
    if not table_rows:
        table_rows = '<tr><td colspan="12" class="muted">No local model evidence matched these filters.</td></tr>'
    unregistered_slugs = sorted(
        {str(row.get("model") or "") for row in ordered if row.get("unregistered") and row.get("model")}
    )
    unregistered_pointer = ""
    if unregistered_slugs:
        pointer = (
            f"Unregistered model slug(s): {', '.join(unregistered_slugs)} "
            "— run the identity procedure in docs/TAXONOMY.md."
        )
        unregistered_pointer = f'<div class="identity-pointer">{html_escape(pointer)}</div>'
    misrouted_routes = sorted(
        {
            f"{row.get('engine')}:{row.get('model')} → {row.get('canonical_route')}"
            for row in ordered
            if row.get("misrouted") and row.get("model")
        }
    )
    misrouted_pointer = ""
    if misrouted_routes:
        misrouted_pointer = (
            '<div class="identity-pointer">Noncanonical route(s): '
            f"{html_escape(', '.join(misrouted_routes))}</div>"
        )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
{ringer.CSP_META_TAG}
<title>ringer model scoreboard</title>
<style>{ringer.ARTIFACT_BASE_CSS}
{MODEL_SCOREBOARD_CSS}</style>
</head>
<body>
<div class="page scoreboard-page">
  <header class="scoreboard-header">
    <h1 class="scoreboard-title">Model performance scoreboard</h1>
    <div class="scoreboard-meta">
      <span>Generated {html_escape(humanized_log_date(generated))}</span>
      <nav class="source-links" aria-label="scoreboard source files">
        {source_file_link(log_path, "eval log")}
        {source_file_link(catalog_path, "catalog")}
        {source_file_link(notes_path, "model notes")}
      </nav>
    </div>
  </header>
  {render_free_watchlist(catalog_models, catalog_path, events=catalog_events)}
  <main>
    <div class="table-scroll">
      <table class="ranked-table">
        <thead>
          <tr>
            <th>Model</th>
            <th>Lab</th>
            <th>Harness</th>
            <th>API/Plan</th>
            <th>Tier</th>
            <th class="num">Tasks</th>
            <th class="num">First try</th>
            <th class="num">Pass</th>
            <th class="num">Tokens (median)</th>
            <th>Speed (median)</th>
            <th>Last used</th>
            <th>Notes</th>
          </tr>
        </thead>
        <tbody>{table_rows}</tbody>
      </table>
    </div>
  </main>
  <footer class="scoreboard-footer">
    <span>{fmt_int(rows_read)} rows read, {fmt_int(skipped)} skipped lines. Ordering sorts by evidence tier first: proven requires &gt;=30 distinct jobs in one named task family, first try &gt;=75% and final check &gt;=85%; otherwise probation. Repeated rounds and microbenchmarks do not meet the floor. This is a statistical label, not human quality or promotion. Ties use first-try pass rate and pass rate. Misrouted and unattributed legacy rows are not ranked or tiered.</span>
    {unregistered_pointer}
    {misrouted_pointer}
  </footer>
</div>
</body>
</html>
"""


def write_model_scoreboard_html(
    config: AppConfig,
    *,
    path: Path | None,
    rows: list[dict[str, Any]],
    log_path: Path,
    rows_read: int,
    skipped: int,
    catalog_path: Path,
    catalog_models: list[dict[str, Any]],
    notes_path: Path,
    notes_sections: dict[str, list[str]],
    catalog_events: list[dict[str, Any]] | None = None,
    generated_at: str | None = None,
) -> Path:
    import ringer

    target = path
    if target is None:
        target = ringer.artifact_live_path(config.state_dir, MODEL_SCOREBOARD_RUN_NAME)
    target = target.expanduser().resolve()
    html = render_model_scoreboard_html(
        rows=rows,
        log_path=log_path,
        rows_read=rows_read,
        skipped=skipped,
        catalog_path=catalog_path,
        catalog_models=catalog_models,
        catalog_events=catalog_events,
        notes_path=notes_path,
        notes_sections=notes_sections,
    )
    ringer.atomic_write_text(target, html)
    if path is None:
        ringer.update_artifact_library_live(
            config.state_dir,
            run_name=MODEL_SCOREBOARD_RUN_NAME,
            run_id=MODEL_SCOREBOARD_RUN_NAME,
            identity=MODEL_SCOREBOARD_IDENTITY,
            state="pass",
        )
    return target


def print_model_log_table(path: Path, rows_read: int, skipped: int, groups: list[dict[str, Any]]) -> None:
    import ringer

    print(f"Model log: {path} ({rows_read} rows, {skipped} skipped lines)")
    widths = (32, 20, 18, 18, 10, 7, 10, 7, 15, 14, 14, 60)
    header = " | ".join(
        f"{name:<{width}}" for name, width in zip(MODEL_SCOREBOARD_COLUMNS, widths)
    )
    current_task_type: str | None = None
    if not groups:
        print(header)
        print("-" * len(header))
    for group in groups:
        task_type = str(group.get("task_type") or "(untyped)")
        if task_type != current_task_type:
            if current_task_type is not None:
                print()
            current_task_type = task_type
            print(f"Task type: {task_type}")
            print(header)
            print("-" * len(header))
        display = str(group.get("model_display") or group["model"])
        if group.get("unattributed"):
            display = UNATTRIBUTED_MODEL_DISPLAY
        if group.get("misrouted"):
            display = f"{display} [misrouted]"
        if group.get("unregistered"):
            display = f"{display} [unregistered]"
        values = (
            display,
            str(group.get("lab") or "(unknown)"),
            str(group.get("harness") or "unknown"),
            str(group.get("access") or "unknown"),
            "not ranked" if group.get("tier") == "unranked" else str(group.get("tier") or ""),
            fmt_int(group.get("tasks")),
            fmt_percent(group.get("first_try_pass_rate")),
            fmt_percent(group.get("pass_rate")),
            "" if group.get("median_tokens") is None else fmt_int(group.get("median_tokens")),
            fmt_scoreboard_duration(group.get("median_duration_ms")),
            humanized_log_date(group.get("last_seen")),
            ringer.shorten(str(group.get("latest_note") or ""), 60),
        )
        print(" | ".join(f"{ringer.shorten(value, width):<{width}}" for value, width in zip(values, widths)))
    print("Judgment layer: docs/MODEL-NOTES.md")
    unregistered_slugs = sorted(
        {str(group.get("model") or "") for group in groups if group.get("unregistered") and group.get("model")}
    )
    if unregistered_slugs:
        print(
            f"Unregistered model slug(s): {', '.join(unregistered_slugs)} "
            "— run the identity procedure in docs/TAXONOMY.md."
        )
def build_models_api_payload(
    *,
    log_path: Path,
    default_log_path: Path | None = None,
    db_path: Path | None = None,
    catalog_path: Path | None = None,
    registry_path: Path | None = None,
    notes_path: Path | None = None,
) -> dict[str, Any]:
    import ringer

    log_path = log_path.expanduser().resolve()
    default_log_path = (default_log_path or log_path).expanduser().resolve()
    explicit_db = db_path is not None
    resolved_db_path = (db_path or ringer.default_read_model_db_path()).expanduser().resolve()
    catalog_path = (catalog_path or ringer.default_catalog_path()).expanduser().resolve()
    registry_path = (registry_path or ringer.default_model_registry_path()).expanduser().resolve()
    notes_path = (notes_path or default_model_notes_path()).expanduser().resolve()
    using_db = should_use_read_model_db(
        log_path=log_path,
        default_log_path=default_log_path,
        explicit_db=explicit_db,
    )
    catalog_models: list[dict[str, Any]] = []
    if using_db:
        try:
            ringer.sync_read_model_db(
                resolved_db_path,
                log_path,
                catalog_path=catalog_path,
                registry_path=registry_path,
            )
            rows, identity_registry = ringer.db_attempt_rows(resolved_db_path)
            disk_registry = ringer.load_model_identity_registry(registry_path)
            identity_registry = dataclass_replace(
                identity_registry,
                noncanonical_routes=disk_registry.noncanonical_routes,
            )
            catalog_models = ringer.db_catalog_models(resolved_db_path)
        except Exception:
            using_db = False
            rows, _skipped = read_model_log_rows(log_path)
            identity_registry = ringer.load_model_identity_registry(registry_path)
    else:
        rows, _skipped = read_model_log_rows(log_path)
        identity_registry = ringer.load_model_identity_registry(registry_path)
    if not using_db:
        with contextlib.suppress(Exception):
            catalog_models = ringer.load_catalog_snapshot(catalog_path)
    notes_sections = parse_model_notes_sections(notes_path)
    groups = enrich_model_groups_with_notes(
        enrich_model_groups_with_identity(
            aggregate_model_log_rows(rows),
            rows,
            identity_registry,
            include_task_type=True,
            catalog_models=catalog_models,
        ),
        notes_sections,
    )
    rollup = enrich_model_groups_with_notes(
        enrich_model_groups_with_identity(
            aggregate_model_scoreboard_rows(rows),
            rows,
            identity_registry,
            include_task_type=False,
            catalog_models=catalog_models,
        ),
        notes_sections,
    )
    catalog_by_id = catalog_models_by_id(catalog_models)
    ordered_rollup: list[dict[str, Any]] = []
    for row in order_model_scoreboard_rows(rollup, catalog_by_id):
        item = dict(row)
        item.setdefault("task_type", "(all)")
        ordered_rollup.append(item)
    return {
        "generated_at": ringer.utc_now_iso(),
        "columns": list(MODEL_SCOREBOARD_COLUMNS),
        "groups": groups,
        "rollup": ordered_rollup,
    }


def run_models_command(config: AppConfig, args: argparse.Namespace) -> int:
    import ringer

    default_log_path = config.eval.jsonl_path.expanduser().resolve()
    log_path = (args.log or default_log_path).expanduser().resolve()
    since = validate_since_date(args.since)
    explicit_db = getattr(args, "db", None) is not None
    db_path = (getattr(args, "db", None) or ringer.default_read_model_db_path()).expanduser().resolve()
    catalog_path = (getattr(args, "catalog_file", None) or ringer.default_catalog_path()).expanduser().resolve()
    registry_path = (getattr(args, "registry", None) or ringer.default_model_registry_path()).expanduser().resolve()
    notes_path = (getattr(args, "notes_file", None) or default_model_notes_path()).expanduser().resolve()
    using_db = should_use_read_model_db(
        log_path=log_path,
        default_log_path=default_log_path,
        explicit_db=explicit_db,
    )
    catalog_events: list[dict[str, Any]] | None = None
    if using_db:
        try:
            sync_result = ringer.sync_read_model_db(
                db_path,
                log_path,
                catalog_path=catalog_path,
                registry_path=registry_path,
            )
            rows, identity_registry = ringer.db_attempt_rows(db_path, since=since, engine=args.engine)
            disk_registry = ringer.load_model_identity_registry(registry_path)
            identity_registry = dataclass_replace(
                identity_registry,
                noncanonical_routes=disk_registry.noncanonical_routes,
            )
            skipped = sync_result.skipped
            catalog_models_from_db = ringer.db_catalog_models(db_path)
            catalog_events = ringer.db_catalog_events(db_path, limit=6)
        except Exception as exc:
            using_db = False
            print(f"models: SQLite read model unavailable; using JSONL fallback ({exc})", file=sys.stderr)
            rows, skipped = read_model_log_rows(log_path, since=since, engine=args.engine)
            identity_registry = ringer.load_model_identity_registry(registry_path)
            catalog_models_from_db = []
    else:
        rows, skipped = read_model_log_rows(log_path, since=since, engine=args.engine)
        identity_registry = ringer.load_model_identity_registry(registry_path)
        catalog_models_from_db = []
    catalog_models = catalog_models_from_db if using_db else ringer.load_catalog_snapshot(catalog_path)
    notes_sections = parse_model_notes_sections(notes_path)
    groups = enrich_model_groups_with_notes(
        enrich_model_groups_with_identity(
            aggregate_model_log_rows(rows, task_type=args.task_type, model=args.model),
            rows,
            identity_registry,
            include_task_type=True,
            catalog_models=catalog_models,
        ),
        notes_sections,
    )
    if args.explore:
        print_model_explore(
            log_path=log_path,
            rows_read=len(rows),
            skipped=skipped,
            groups=groups,
            catalog_path=catalog_path,
            catalog_models=catalog_models,
        )
        return 0
    html_arg = getattr(args, "html", None)
    open_requested = bool(getattr(args, "open", False))
    if html_arg is not None or open_requested:
        scoreboard_rows = enrich_model_groups_with_notes(
            enrich_model_groups_with_identity(
                aggregate_model_scoreboard_rows(rows, task_type=args.task_type, model=args.model),
                rows,
                identity_registry,
                include_task_type=False,
                catalog_models=catalog_models,
            ),
            notes_sections,
        )
        explicit_path = None
        if html_arg not in {None, ""}:
            explicit_path = Path(str(html_arg))
        page_path = write_model_scoreboard_html(
            config,
            path=explicit_path,
            rows=scoreboard_rows,
            log_path=log_path,
            rows_read=len(rows),
            skipped=skipped,
            catalog_path=catalog_path,
            catalog_models=catalog_models,
            notes_path=notes_path,
            notes_sections=notes_sections,
            catalog_events=catalog_events,
        )
        print(page_path)
        if open_requested:
            ringer.open_in_browser(ringer.file_href(page_path))
        return 0
    if args.json:
        print(json.dumps(groups))
    else:
        print_model_log_table(log_path, len(rows), skipped, groups)
    return 0


def run_outcomes_command(config: AppConfig, args: argparse.Namespace) -> int:
    import ringer

    payload = ringer.read_outcomes(args.state_dir or config.state_dir, args.log or config.eval.jsonl_path)
    if args.job_id:
        payload.update(ringer.summarise_outcomes([t for t in payload["tasks"] if t["job_id"] == args.job_id]))
    if args.read_back:
        for task in payload["tasks"]:
            files = task["harvested_files"]
            task["export_read_back"] = ringer.read_export_evidence(files)
            task["export_evidence_available"] = any(f.get("sha256") for f in files)
        readbacks = [f for task in payload["tasks"] for f in task["export_read_back"]]
        payload["export_read_back_totals"] = dict(
            matched=sum(f["matches"] is True for f in readbacks),
            mismatched_or_missing=sum(f["matches"] is False for f in readbacks),
            unknown=sum(f["matches"] is None for f in readbacks),
            tasks_without_recorded_hashes=sum(not t["export_evidence_available"] for t in payload["tasks"]))
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(json.dumps(payload["totals"], indent=2, sort_keys=True))
        print("Model attribution:", json.dumps(payload["model_attribution"], sort_keys=True))
        if args.read_back:
            print("Export read-back:", json.dumps(payload["export_read_back_totals"], sort_keys=True))
        for task in payload["tasks"]:
            print(f"{task['job_id']} / {task['run_id']} / {task['task_key']}: {task['status']}; product={task['product_state']}; promotion=BLOCKED; failure={task['failure_class']}")
        if payload["read_errors"] or any(payload["malformed_rows"].values()):
            print("Read errors:", payload["read_errors"], payload["malformed_rows"])
    return 0
