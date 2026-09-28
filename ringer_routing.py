"""Pure model-fit assessment schema, task binding and validation helpers.

This module has no process, filesystem, network or billing side effects.  The
runner resolves commands and supplies the resulting routes at its boundary.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable


ASSESSMENT_SCHEMA = "ringer-model-assessment/v1"
ASSESSMENT_FIELDS = {
    "binding",
    "engine",
    "model",
    "effort",
    "billing_route",
    "service_tier",
    "rationale",
    "alternative_considered",
    "context_plan",
    "verification",
    "escalation",
    "evidence",
    "uncertainty",
}
ROOT_FIELDS = {"schema", "coordinator", "strategy", "tasks"}
EFFORTS = {"minimal", "low", "medium", "high", "xhigh", "max"}
RINGER_MODEL_EFFORTS = {
    "gpt-6-astra": {"low", "medium", "high", "xhigh", "max"},
}
SERVICE_TIERS = {"standard", "fast"}
BILLING_ROUTES = {"subscription", "api"}


class ModelAssessmentError(ValueError):
    """A recorded assessment is absent, malformed, stale or route-mismatched."""


@dataclass(frozen=True)
class ResolvedRoute:
    task_key: str
    binding: str
    engine: str
    model: str
    effort: str
    billing_route: str
    service_tier: str


def task_binding(task: Any) -> str:
    """Bind the assessment to the complete task record using canonical JSON."""
    if not isinstance(task, dict):
        raise ModelAssessmentError("task binding input must be a JSON object")
    encoded = json.dumps(
        task,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _text(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ModelAssessmentError(f"{path} must be non-empty text")
    return value.strip()


def validate_model_assessment(
    raw: Any,
    routes: Iterable[ResolvedRoute],
) -> dict[str, dict[str, str]]:
    """Validate one assessment against already resolved, command-backed routes."""
    route_list = list(routes)
    if not isinstance(raw, dict):
        raise ModelAssessmentError(
            "model_assessment is required before model dispatch; run "
            "'ringer.py assess <manifest.json>' to generate a bound draft"
        )
    if set(raw) != ROOT_FIELDS:
        missing = sorted(ROOT_FIELDS - set(raw))
        extra = sorted(set(raw) - ROOT_FIELDS)
        detail = []
        if missing:
            detail.append(f"missing {missing}")
        if extra:
            detail.append(f"unknown {extra}")
        raise ModelAssessmentError("model_assessment fields are invalid: " + "; ".join(detail))
    if raw.get("schema") != ASSESSMENT_SCHEMA:
        raise ModelAssessmentError(
            f"model_assessment.schema must be exactly {ASSESSMENT_SCHEMA!r}"
        )
    _text(raw.get("coordinator"), "model_assessment.coordinator")
    _text(raw.get("strategy"), "model_assessment.strategy")
    task_rows = raw.get("tasks")
    if not isinstance(task_rows, dict):
        raise ModelAssessmentError("model_assessment.tasks must be an object keyed by task key")
    expected_keys = {route.task_key for route in route_list}
    actual_keys = set(task_rows)
    if actual_keys != expected_keys:
        missing = sorted(expected_keys - actual_keys)
        extra = sorted(actual_keys - expected_keys)
        details = []
        if missing:
            details.append(f"missing task assessment(s): {', '.join(missing)}")
        if extra:
            details.append(f"unknown task assessment(s): {', '.join(extra)}")
        raise ModelAssessmentError("; ".join(details))

    validated: dict[str, dict[str, str]] = {}
    for route in route_list:
        path = f"model_assessment.tasks.{route.task_key}"
        row = task_rows.get(route.task_key)
        if not isinstance(row, dict):
            raise ModelAssessmentError(f"{path} must be an object")
        if set(row) != ASSESSMENT_FIELDS:
            missing = sorted(ASSESSMENT_FIELDS - set(row))
            extra = sorted(set(row) - ASSESSMENT_FIELDS)
            details = []
            if missing:
                details.append(f"missing {missing}")
            if extra:
                details.append(f"unknown {extra}")
            raise ModelAssessmentError(f"{path} fields are invalid: " + "; ".join(details))
        clean = {name: _text(row.get(name), f"{path}.{name}") for name in ASSESSMENT_FIELDS}
        if clean["effort"] == "ultra":
            raise ModelAssessmentError(
                f"{path}.effort ultra is blocked for routine Ringer workers until "
                "delegation ownership and accounting controls are verified"
            )
        if clean["effort"] not in EFFORTS:
            raise ModelAssessmentError(
                f"{path}.effort must be one of {', '.join(sorted(EFFORTS))}"
            )
        supported_efforts = RINGER_MODEL_EFFORTS.get(clean["model"])
        if supported_efforts is not None and clean["effort"] not in supported_efforts:
            raise ModelAssessmentError(
                f"{path}.effort {clean['effort']!r} is not supported for "
                f"{clean['model']}; use one of {', '.join(sorted(supported_efforts))}"
            )
        if clean["service_tier"] not in SERVICE_TIERS:
            raise ModelAssessmentError(f"{path}.service_tier must be standard or fast")
        if clean["billing_route"] not in BILLING_ROUTES:
            raise ModelAssessmentError(f"{path}.billing_route must be subscription or api")
        for field in ("binding", "engine", "model", "effort", "billing_route", "service_tier"):
            expected = getattr(route, field)
            if clean[field] != expected:
                if field == "binding":
                    raise ModelAssessmentError(
                        f"{path}.binding is stale; the task specification, checker or other "
                        "task content changed. Regenerate the assessment draft"
                    )
                raise ModelAssessmentError(
                    f"{path}.{field} records {clean[field]!r}, but the resolved command/route "
                    f"uses {expected!r}; remove the override or reassess the route"
                )
        validated[route.task_key] = clean
    return validated


def draft_model_assessment(
    tasks: Iterable[tuple[str, str]],
    *,
    coordinator: str = "",
) -> dict[str, Any]:
    """Return an intentionally incomplete zero-LLM draft with current bindings."""
    return {
        "schema": ASSESSMENT_SCHEMA,
        "coordinator": coordinator,
        "strategy": "",
        "tasks": {
            key: {
                "binding": binding,
                "engine": "",
                "model": "",
                "effort": "",
                "billing_route": "",
                "service_tier": "standard",
                "rationale": "",
                "alternative_considered": "",
                "context_plan": "",
                "verification": "",
                "escalation": "",
                "evidence": "",
                "uncertainty": "",
            }
            for key, binding in tasks
        },
    }
