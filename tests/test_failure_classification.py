"""Focused failure-classification fixtures using only local RingerRunner workers."""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import ringer
from ringer_reliability import classify_failure


class FailureClassificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # These tests isolate runner failure handling with local shell fixtures;
        # model-gate behaviour is covered by test_model_assessment.
        gate = patch.object(ringer, "validate_manifest_model_assessment", return_value={})
        gate.start()
        self.addCleanup(gate.stop)

    def runner(self, command: str, *, check: str = "test -s out.txt", max_attempts: int = 2):
        task = {"key": "one", "spec": "Use only the local fixture.", "engine": "local", "check": check,
                "expect_files": ["out.txt"] if check == "test -s out.txt" else [], "max_attempts": max_attempts}
        manifest = ringer.Manifest.from_obj({"run_name": "classification", "workdir": str(self.root / "work"), "tasks": [task]})
        engine = ringer.EngineConfig(name="local", bin="/bin/sh", args_template=("-c", command),
                                     sandbox_args=(), full_access_args=(), token_regex=None)
        config = ringer.AppConfig(path=None, identity_default=None, state_dir=self.root / "state",
                                 dashboard_port_base=18787, hud_port=18788, hud_app_path=None, allow_full_access=False,
                                 eval=ringer.EvalConfig(backend="jsonl", jsonl_path=self.root / "attempts.jsonl"),
                                 engines={"local": engine}, artifact=ringer.ArtifactConfig(enabled=False, out_template="", report_template="", index_out=self.root / "index"))
        return ringer.RingerRunner(manifest, config, "test", dashboard_enabled=False)

    def rows(self):
        return [json.loads(line) for line in (self.root / "attempts.jsonl").read_text().splitlines()]

    def test_passing_worker_source_and_prose_are_not_provider_stops(self):
        runner = self.runner("printf '%s\\n' 'const code = 429;' 'I added tests for quota exceeded and permission denied.'; printf yes > out.txt")
        self.assertEqual(0, asyncio.run(runner.run()))
        row = self.rows()[0]
        self.assertEqual("PASS", row["verdict"])
        self.assertIsNone(row["failure_class"])

    def test_quoted_quota_fixture_does_not_hide_a_product_failure(self):
        runner = self.runner("printf '%s\\n' 'Added fixture: {\"error\":\"quota exceeded\"}'", check="echo 'AssertionError: output differs'; exit 1", max_attempts=1)
        self.assertEqual(1, asyncio.run(runner.run()))
        row = self.rows()[0]
        self.assertEqual("FAIL", row["verdict"])
        self.assertEqual("product", row["failure_class"])

    def test_failed_quota_diagnostic_blocks_without_retry(self):
        runner = self.runner("echo 'ERROR: quota exceeded'; exit 1")
        self.assertEqual(1, asyncio.run(runner.run()))
        rows = self.rows()
        self.assertEqual(1, len(rows))
        self.assertEqual("quota", rows[0]["failure_class"])

    def test_structured_provider_error_is_authoritative_even_with_zero_exit(self):
        raw = json.dumps({"type": "error", "error": {"type": "insufficient_quota", "message": "quota exceeded"}})
        worker = ringer.WorkerResult(0, False, None, raw_output=raw)
        verify = ringer.VerifyResult(True, 0, False, "check passed")
        self.assertEqual("quota", classify_failure(worker, verify, worker_output=raw))


if __name__ == "__main__":
    unittest.main()
