"""Command-level aggregation over a shared, local attempt journal."""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ringer


class AggregationContractTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"HOME": str(root), "RINGER_HOME": str(root / "ringer-home")}))
        self.state_dir = root / "state"
        self.log = root / "attempts.jsonl"
        self.model_flags = [
            "--catalog-file", str(root / "catalog.json"),
            "--notes-file", str(root / "notes.md"),
            "--registry", str(root / "registry.toml"),
        ]
        config = ringer.AppConfig(
            path=None, identity_default=None, state_dir=self.state_dir,
            dashboard_port_base=8787, hud_port=8700, hud_app_path=None,
            allow_full_access=False, eval=ringer.EvalConfig(backend="jsonl", jsonl_path=root / "default.jsonl"),
            engines={}, artifact=ringer.ArtifactConfig(enabled=False, out_template=None, report_template=None, index_out=None),
        )
        self.enterContext(patch.object(ringer.AppConfig, "load", return_value=config))

        common = {
            "run_id": "round-one", "job_id": "example-job", "worker_engine": "codex",
            "model": "example-model", "task_type": "code-feature",
        }
        self.attempts = [
            dict(common, task_key="retry", verdict="FAIL", retry=False, duration_ms=900,
                 worker_tokens=300, logged_at="2026-07-06T12:00:00+00:00",
                 product_state="NEEDS_CHANGE", failure_class="product"),
            dict(common, task_key="retry", verdict="PASS", retry=True, duration_ms=300,
                 worker_tokens=100, logged_at="2026-07-06T12:01:00+00:00",
                 product_state="PASS", task_contract_state="PASS", export_state="PASS"),
            dict(common, task_key="first-try", verdict="PASS", retry=False, duration_ms=100,
                 worker_tokens=200, logged_at="2026-07-06T12:02:00+00:00"),
            dict(common, run_id="round-two", job_id="other-job", task_key="retry",
                 model="other-model", verdict="FAIL", retry=False, duration_ms=500,
                 worker_tokens=None, logged_at="2026-07-06T12:03:00+00:00",
                 product_state="NEEDS_CHANGE", failure_class="product"),
        ]
        self.log.write_text("".join(json.dumps(row) + "\n" for row in self.attempts), encoding="utf-8")

    def command_json(self, command: str, *flags: str):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            rc = ringer.main(["--no-self-update", command, "--log", str(self.log), "--json", *flags])
        self.assertEqual(0, rc, errors.getvalue())
        self.assertEqual("", errors.getvalue())
        return json.loads(output.getvalue())

    def test_models_json_counts_tasks_retries_final_durations_and_attempt_tokens(self) -> None:
        groups = self.command_json("models", *self.model_flags)
        fields = (
            "engine", "model", "task_type", "tasks", "attempts", "passed", "failed",
            "pass_rate", "first_try_pass_rate", "median_duration_ms", "median_tokens", "last_seen",
        )
        self.assertEqual([
            ("codex", "example-model", "code-feature", 2, 3, 2, 0, 1.0, 0.5, 200, 200, "2026-07-06T12:02:00+00:00"),
            ("codex", "other-model", "code-feature", 1, 1, 0, 1, 0.0, 0.0, 500, None, "2026-07-06T12:03:00+00:00"),
        ], [tuple(group[field] for field in fields) for group in groups])
        self.assertEqual(
            [groups[1]], self.command_json("models", *self.model_flags, "--model", "other-model"),
        )

    def test_outcomes_json_distinguishes_passed_attempts_from_verified_outcomes(self) -> None:
        report = self.command_json("outcomes", "--state-dir", str(self.state_dir))
        self.assertEqual({
            "tasks": 3, "passed": 1, "success_coverage": 1 / 3, "jobs": 2, "runs": 2,
            "journal_attempts": 4, "passed_checks": 1, "unknown_contract": 2, "unknown_export": 2,
            "failure_classes": {"unknown": 2, "product": 1}, "unscored": 1,
            "unknown_verification": 1, "state_only": 0, "missing_attempt_rows": 0,
            "partial_attempt_evidence": 0, "model_failures": 1, "interruptions": 0,
            "statuses": {"pass": 2, "fail": 1},
        }, report["totals"])
        self.assertEqual({"example-model": 2, "other-model": 1}, report["model_attribution"])
        self.assertEqual({"attempts": 0, "lifecycle": 0}, report["malformed_rows"])
        self.assertEqual([], report["read_errors"])
        self.assertEqual([
            ("round-one", "retry", "pass", "PASS", "BLOCKED", self.attempts[:2]),
            ("round-one", "first-try", "pass", "UNKNOWN", "BLOCKED", self.attempts[2:3]),
            ("round-two", "retry", "fail", "NEEDS_CHANGE", "BLOCKED", self.attempts[3:]),
        ], [
            (task["run_id"], task["task_key"], task["status"], task["product_state"],
             task["promotion_state"], task["attempt_history"])
            for task in report["tasks"]
        ])
