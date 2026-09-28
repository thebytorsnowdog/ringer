#!/usr/bin/env python3
"""Tests for Ringer Discovery CLI and library."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable
RINGER = REPO_ROOT / "ringer.py"
CLI = REPO_ROOT / "ringer-discover.py"

from automation_discovery.receipts import OUTPUT_LIMIT


def run(*args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    cwd = cwd or REPO_ROOT
    result = subprocess.run(
        [PYTHON, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"Command failed: {args}\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result


def run_cli(*args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(str(CLI), *args, cwd=cwd, check=check)


class DatabaseTests(unittest.TestCase):
    def test_init_creates_database(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            result = run_cli("init", "--write-root", td)
            self.assertIn("Created", result.stdout)
            self.assertTrue((Path(td) / "corpus.db").is_file())

    def test_init_refuses_existing_db_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            run_cli("init", "--write-root", td)
            result = run_cli("init", "--write-root", td, check=False)
            self.assertEqual(result.returncode, 2)
            self.assertIn("already exists", result.stderr)

    def test_approval_invalidated_when_plan_changes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            run_cli("init", "--write-root", td)
            run_cli(
                "add-source",
                "--write-root", td,
                "--surface", "s1",
                "--kind", "ai-history",
                "--access-route", "/tmp/history",
            )
            run_cli("approve-sources", "--write-root", td, "--confirm")

            result = run_cli("plan", "--write-root", td)
            data = json.loads(result.stdout)
            self.assertTrue(data["approved"])

            run_cli(
                "add-source",
                "--write-root", td,
                "--surface", "s2",
                "--kind", "work-tracking",
                "--access-route", "/tmp/tickets",
            )
            result = run_cli("plan", "--write-root", td)
            data = json.loads(result.stdout)
            self.assertFalse(data["approved"])


class ManifestLintTests(unittest.TestCase):
    # Generated manifests carry no model assessment, so with the gate on,
    # lint must reject them. A private config keeps this independent of
    # the machine's own Ringer config.
    def _lint(self, td: str, manifest: str) -> subprocess.CompletedProcess[str]:
        config = Path(td) / "ringer-config.toml"
        config.write_text(
            "require_model_assessment = true\n\n"
            "[engines.opencode]\n"
            'bin = "opencode"\n'
            'args_template = ["{taskdir}", "{access_args}", "run", "-m", "{model}", '
            '"--dangerously-skip-permissions", "--format", "json", "{engine_args}", '
            '"--dir", "{taskdir}", "{spec}"]\n'
            "sandbox_args = []\n"
            'full_access_args = ["--no-sandbox"]\n',
            encoding="utf-8",
        )
        return run(str(RINGER), "--config", str(config), "lint", manifest, check=False)

    def _build_corpus_with_chosen_offer(self, td: str) -> None:
        run_cli("init", "--write-root", td)
        run_cli(
            "add-source", "--write-root", td,
            "--surface", "history", "--kind", "ai-history",
            "--access-route", "/tmp/history",
        )
        run_cli("approve-sources", "--write-root", td, "--confirm")
        run_cli(
            "make-ingest-manifest", "--write-root", td,
            "--model", "openrouter/moonshotai/glm-5.2",
            "--out", os.path.join(td, "ingest.json"),
        )
        task_dir = Path(td) / "ingest-1-history"
        task_dir.mkdir()
        events = [
            json.dumps({
                "source_ref": f"session-{i}",
                "occurred_at": "2026-01-01T00:00:00Z",
                "actor": "user",
                "action": f"request {i}",
                "job_hint": "report requests",
            })
            for i in range(10)
        ]
        (task_dir / "events.jsonl").write_text("\n".join(events) + "\n")
        (task_dir / "source_summary.json").write_text(
            json.dumps({"status": "ingested", "notes": "10 events"})
        )
        run_cli("merge-ingest", "--write-root", td, "--source", "1", "--lane", "ringer", str(task_dir))

        # add candidate directly with 3 valid event ids
        db_path = Path(td) / "corpus.db"
        from automation_discovery.db import Connection
        with Connection(db_path) as conn:
            conn.conn.execute(
                "INSERT INTO candidates (job, event_ids, independence_note, consequence, overlap, status) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("report summarizer", "[1,2,3]", "three separate sessions", "saves time", "unknown", "audited"),
            )
            conn.conn.commit()
            contract = [
                {"kind": "t1", "argv": ["python3", "-c", "print('OK')"], "success_contains": "OK"},
                {"kind": "t2", "argv": ["python3", "-c", "print('OK2')"]},
                {"kind": "t3", "argv": ["python3", "-c", "print('OK3')"]},
            ]
            conn.conn.execute(
                "INSERT INTO offers (candidate_id, title, what_it_does, evidence_query, run_cost, receipt_contract, status) "
                "VALUES (?, ?, ?, ?, ?, ?, 'offered')",
                (1, "Report summarizer", "does the thing", "SELECT id FROM events", "cheap", json.dumps(contract)),
            )
            conn.conn.commit()

    def test_ingest_manifest_lint_requires_assessment(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            run_cli("init", "--write-root", td)
            run_cli(
                "add-source", "--write-root", td,
                "--surface", "history", "--kind", "ai-history",
                "--access-route", "/tmp/history",
            )
            run_cli("approve-sources", "--write-root", td, "--confirm")
            run_cli(
                "make-ingest-manifest", "--write-root", td,
                "--model", "openrouter/moonshotai/glm-5.2",
                "--out", os.path.join(td, "ingest.json"),
            )
            result = self._lint(td, os.path.join(td, "ingest.json"))
            self.assertEqual(1, result.returncode)
            self.assertIn("model_assessment is required", result.stdout)

    def test_audit_manifest_lint_requires_assessment(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self._build_corpus_with_chosen_offer(td)
            run_cli(
                "make-audit-manifest", "--write-root", td,
                "--model", "openrouter/moonshotai/glm-5.2",
                "--out", os.path.join(td, "audit.json"),
            )
            result = self._lint(td, os.path.join(td, "audit.json"))
            self.assertEqual(1, result.returncode)
            self.assertIn("model_assessment is required", result.stdout)

    def test_build_manifest_lint_requires_assessment(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self._build_corpus_with_chosen_offer(td)
            run_cli("choose-offer", "--write-root", td, "--confirm", "1")
            run_cli(
                "make-build-manifest", "--write-root", td,
                "--model", "openrouter/moonshotai/glm-5.2",
                "--out", os.path.join(td, "build.json"),
            )
            result = self._lint(td, os.path.join(td, "build.json"))
            self.assertEqual(1, result.returncode)
            self.assertIn("model_assessment is required", result.stdout)


class IngestionTests(unittest.TestCase):
    def test_staging_validation_fails_on_too_few_rows(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            events = Path(td) / "events.jsonl"
            events.write_text(json.dumps({"source_ref": "a", "action": "x"}) + "\n")
            result = run_cli(
                "validate-staging",
                str(events),
                "--min-rows", "5",
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("only 1 rows staged", result.stdout)

    def test_merge_ingest_records_failed_source(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            run_cli("init", "--write-root", td)
            run_cli(
                "add-source", "--write-root", td,
                "--surface", "broken", "--kind", "other",
                "--access-route", "/this/does/not/exist",
            )
            run_cli("approve-sources", "--write-root", td, "--confirm")
            task_dir = td_path / "ingest-1-broken"
            task_dir.mkdir()
            (task_dir / "events.jsonl").write_text("")
            (task_dir / "source_summary.json").write_text(
                json.dumps({"status": "failed", "notes": "path missing"})
            )
            result = run_cli(
                "merge-ingest", "--write-root", td,
                "--source", "1", "--lane", "ringer", str(task_dir),
            )
            self.assertIn("Recorded failed ingest", result.stdout)

    def test_excerpt_cap_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            run_cli("init", "--write-root", td)
            run_cli(
                "add-source", "--write-root", td,
                "--surface", "x", "--kind", "other", "--access-route", "/x",
            )
            run_cli("approve-sources", "--write-root", td, "--confirm")
            task_dir = td_path / "ingest-1-x"
            task_dir.mkdir()
            big = "x" * 241
            (task_dir / "events.jsonl").write_text(
                json.dumps({"source_ref": "a", "action": "b", "excerpt": big}) + "\n"
            )
            (task_dir / "source_summary.json").write_text(json.dumps({"status": "ingested"}))
            result = run_cli(
                "merge-ingest", "--write-root", td,
                "--source", "1", "--lane", "ringer", str(task_dir),
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("240", result.stderr)


class CandidateTests(unittest.TestCase):
    def test_candidate_requires_three_events(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            run_cli("init", "--write-root", td)
            run_cli("add-source", "--write-root", td, "--surface", "x", "--kind", "other", "--access-route", "/x")
            run_cli("approve-sources", "--write-root", td, "--confirm")
            task_dir = td_path / "ingest-1-x"
            task_dir.mkdir()
            events = [
                json.dumps({"source_ref": f"a{i}", "action": f"action {i}", "job_hint": "job"})
                for i in range(2)
            ]
            (task_dir / "events.jsonl").write_text("\n".join(events) + "\n")
            (task_dir / "source_summary.json").write_text(json.dumps({"status": "ingested", "notes": "2 events"}))
            run_cli("merge-ingest", "--write-root", td, "--source", "1", "--lane", "ringer", str(task_dir))

            audit_dir = td_path / "audit-candidates"
            audit_dir.mkdir()
            (audit_dir / "audit_notes.jsonl").write_text(
                json.dumps({"scope": "corpus", "finding": "n", "severity": "info"}) + "\n"
            )
            (audit_dir / "candidates.jsonl").write_text(
                json.dumps({
                    "job": "j", "event_ids": [1, 2],
                    "independence_note": "two events", "overlap": "unknown",
                }) + "\n"
            )
            result = run_cli(
                "merge-audit", "--write-root", td, "--focus", "candidates", str(audit_dir),
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("at least 3 distinct event IDs", result.stderr)

    def test_candidate_rejects_missing_event_references(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            run_cli("init", "--write-root", td)
            run_cli("add-source", "--write-root", td, "--surface", "x", "--kind", "other", "--access-route", "/x")
            run_cli("approve-sources", "--write-root", td, "--confirm")
            task_dir = td_path / "ingest-1-x"
            task_dir.mkdir()
            events = [
                json.dumps({"source_ref": f"a{i}", "action": f"action {i}", "job_hint": "job"})
                for i in range(3)
            ]
            (task_dir / "events.jsonl").write_text("\n".join(events) + "\n")
            (task_dir / "source_summary.json").write_text(json.dumps({"status": "ingested", "notes": "3 events"}))
            run_cli("merge-ingest", "--write-root", td, "--source", "1", "--lane", "ringer", str(task_dir))

            audit_dir = td_path / "audit-candidates"
            audit_dir.mkdir()
            (audit_dir / "audit_notes.jsonl").write_text(
                json.dumps({"scope": "corpus", "finding": "n", "severity": "info"}) + "\n"
            )
            (audit_dir / "candidates.jsonl").write_text(
                json.dumps({
                    "job": "j", "event_ids": [1, 2, 999],
                    "independence_note": "three refs one missing", "overlap": "unknown",
                }) + "\n"
            )
            result = run_cli(
                "merge-audit", "--write-root", td, "--focus", "candidates", str(audit_dir),
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("missing event IDs", result.stderr)


class OfferTests(unittest.TestCase):
    def test_evidence_query_must_be_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            run_cli("init", "--write-root", td)
            # direct DB insert candidate
            from automation_discovery.db import Connection
            with Connection(td_path / "corpus.db") as conn:
                conn.conn.execute(
                    "INSERT INTO candidates (job, event_ids, independence_note, consequence, overlap, status) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    ("j", "[1,2,3]", "i", "c", "unknown", "audited"),
                )
                conn.conn.commit()
            result = run_cli(
                "add-offer", "--write-root", td,
                "--candidate-id", "1",
                "--title", "t",
                "--what-it-does", "d",
                "--evidence-query", "DELETE FROM events",
                "--receipt-contract", json.dumps([
                    {"kind": "k1", "argv": ["echo", "x"]},
                    {"kind": "k2", "argv": ["echo", "x"]},
                    {"kind": "k3", "argv": ["echo", "x"]},
                ]),
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("select or with", result.stderr.lower())

    def test_choose_offer_requires_confirm(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            run_cli("init", "--write-root", td)
            from automation_discovery.db import Connection
            with Connection(Path(td) / "corpus.db") as conn:
                conn.conn.execute(
                    "INSERT INTO candidates (job, event_ids, independence_note, consequence, overlap, status) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    ("j", "[1,2,3]", "i", "c", "unknown", "audited"),
                )
                contract = [{"kind": "k", "argv": ["echo", "x"]} for _ in range(3)]
                conn.conn.execute(
                    "INSERT INTO offers (candidate_id, title, what_it_does, receipt_contract, status) "
                    "VALUES (?, ?, ?, ?, 'offered')",
                    (1, "t", "d", json.dumps(contract)),
                )
                conn.conn.commit()
            result = run_cli("choose-offer", "--write-root", td, "1", check=False)
            self.assertEqual(result.returncode, 2)
            self.assertIn("--confirm", result.stderr)


class BuildTests(unittest.TestCase):
    def _seed_offer(self, td: str) -> None:
        from automation_discovery.db import Connection
        td_path = Path(td)
        with Connection(td_path / "corpus.db") as conn:
            conn.conn.execute(
                "INSERT INTO candidates (job, event_ids, independence_note, consequence, overlap, status) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("j", "[1,2,3]", "i", "c", "none", "audited"),
            )
            conn.conn.commit()
            contract = [
                {"kind": "ok", "argv": ["python3", "-c", "print('OK')"], "success_contains": "OK"},
                {"kind": "usage", "argv": ["python3", "-c", "print('usage')"]},
                {"kind": "run", "argv": ["python3", "-c", "print('result')"], "success_contains": "result"},
            ]
            conn.conn.execute(
                "INSERT INTO offers (candidate_id, title, what_it_does, evidence_query, receipt_contract, status) "
                "VALUES (?, ?, ?, ?, ?, 'offered')",
                (1, "t", "d", "SELECT 1", json.dumps(contract)),
            )
            conn.conn.commit()

    def _chosen_offer_with_staged_receipts(
        self, td: str
    ) -> tuple[Path, Path, Path, Path, Path]:
        """Create and choose an offer, stage valid receipts, and return useful paths."""
        td_path = Path(td)
        run_cli("init", "--write-root", td)
        self._seed_offer(td)
        run_cli("choose-offer", "--write-root", td, "--confirm", "1")
        build_root = td_path / "builds" / "1"
        build_root.mkdir(parents=True)
        (build_root / "BUILD.md").write_text("# built")
        task_dir = td_path / "build-offer-1"
        task_dir.mkdir()
        receipts_jsonl = task_dir / "receipts.jsonl"
        db_path = td_path / "corpus.db"
        run_cli(
            "receipts-check",
            str(db_path), "1", str(build_root), str(receipts_jsonl),
        )
        return td_path, db_path, build_root, task_dir, receipts_jsonl

    def test_build_root_is_inside_write_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            run_cli("init", "--write-root", td)
            self._seed_offer(td)
            run_cli("choose-offer", "--write-root", td, "--confirm", "1")
            run_cli(
                "make-build-manifest", "--write-root", td,
                "--model", "openrouter/moonshotai/glm-5.2",
                "--out", os.path.join(td, "build.json"),
            )
            manifest = json.loads((Path(td) / "build.json").read_text())
            spec = manifest["tasks"][0]["spec"]
            build_root_lines = [
                line for line in spec.splitlines()
                if str((Path(td) / "builds" / "1").resolve()) in line
            ]
            self.assertTrue(build_root_lines, "build root path not embedded in spec")
            self.assertEqual(
                manifest["tasks"][0]["expect_files"],
                [str((Path(td) / "builds" / "1" / "BUILD.md").resolve())],
            )
            self.assertEqual(
                manifest["tasks"][0]["engine_args"],
                ["--writable-root", str((Path(td) / "builds" / "1").resolve())],
            )

    def test_successful_receipt_merge_marks_built(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            run_cli("init", "--write-root", td)
            self._seed_offer(td)
            run_cli("choose-offer", "--write-root", td, "--confirm", "1")
            build_root = td_path / "builds" / "1"
            build_root.mkdir(parents=True)
            (build_root / "BUILD.md").write_text("# built")
            task_dir = td_path / "build-offer-1"
            task_dir.mkdir()
            receipts_jsonl = task_dir / "receipts.jsonl"

            # Stage receipts via the read-only check that executes the contract.
            run_cli(
                "receipts-check",
                str(td_path / "corpus.db"), "1", str(build_root), str(receipts_jsonl),
            )
            result = run_cli("merge-build", "--write-root", td, "--offer-id", "1", str(task_dir))
            self.assertIn("built", result.stdout)
            self.assertTrue(receipts_jsonl.is_file())

            # verify database status and receipt rows
            from automation_discovery.db import Connection
            with Connection(td_path / "corpus.db") as conn:
                offer = conn.get_offer(1)
                receipts = conn.receipts_for_offer(1)
            self.assertEqual(offer["status"], "built")
    def test_merge_build_consumes_pre_staged_receipts_without_rerunning(self) -> None:
        """merge-build must consume receipts.jsonl without re-running commands."""
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            run_cli("init", "--write-root", td)
            from automation_discovery.db import Connection
            with Connection(td_path / "corpus.db") as conn:
                conn.conn.execute(
                    "INSERT INTO candidates (job, event_ids, independence_note, consequence, overlap, status) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    ("j", "[1,2,3]", "i", "c", "none", "audited"),
                )
                conn.conn.commit()
                marker = td_path / "exec-marker.txt"
                contract = [
                    {
                        "kind": "marker",
                        "argv": [
                            "python3", "-c",
                            "from pathlib import Path; p=Path('" + str(marker).replace("'", "'\\''") + "'); "
                            "assert not p.exists(), 'marker already exists'; "
                            "p.write_text('ran')",
                        ],
                    },
                    {"kind": "ok", "argv": ["python3", "-c", "print('ok')"]},
                    {"kind": "ok2", "argv": ["python3", "-c", "print('ok2')"]},
                ]
                conn.conn.execute(
                    "INSERT INTO offers (candidate_id, title, what_it_does, receipt_contract, status) "
                    "VALUES (?, ?, ?, ?, 'offered')",
                    (1, "t", "d", json.dumps(contract)),
                )
                conn.conn.commit()
            run_cli("choose-offer", "--write-root", td, "--confirm", "1")
            build_root = td_path / "builds" / "1"
            build_root.mkdir(parents=True)
            (build_root / "BUILD.md").write_text("# built")
            task_dir = td_path / "build-offer-1"
            task_dir.mkdir()
            receipts_jsonl = task_dir / "receipts.jsonl"

            # Simulate a worker staging receipts by running the command once.
            run_cli(
                "receipts-check",
                str(td_path / "corpus.db"), "1", str(build_root),
                str(receipts_jsonl),
            )
            self.assertTrue(marker.exists())
            self.assertEqual(marker.read_text(), "ran")

            # merge-build consumes the staged receipts without re-executing the
            # contract; leaving the marker in place would make a rerun fail.
            result = run_cli(
                "merge-build", "--write-root", td, "--offer-id", "1", str(task_dir),
            )
            self.assertIn("built", result.stdout)
            self.assertTrue(marker.exists())
            self.assertEqual(marker.read_text(), "ran")

            with Connection(td_path / "corpus.db") as conn:
                offer = conn.get_offer(1)
            self.assertEqual(offer["status"], "built")

    def test_receipts_check_leaves_corpus_unchanged(self) -> None:
        """The read-only receipts-check must not modify corpus.db."""
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            run_cli("init", "--write-root", td)
            from automation_discovery.db import Connection
            with Connection(td_path / "corpus.db") as conn:
                conn.conn.execute(
                    "INSERT INTO candidates (job, event_ids, independence_note, consequence, overlap, status) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    ("j", "[1,2,3]", "three independent events", "c", "none", "audited"),
                )
                conn.conn.commit()
                contract = [
                    {"kind": "ok", "argv": ["python3", "-c", "print('ok')"]},
                    {"kind": "ok2", "argv": ["python3", "-c", "print('ok2')"]},
                    {"kind": "ok3", "argv": ["python3", "-c", "print('ok3')"]},
                ]
                conn.conn.execute(
                    "INSERT INTO offers (candidate_id, title, what_it_does, receipt_contract, status) "
                    "VALUES (?, ?, ?, ?, 'offered')",
                    (1, "t", "d", json.dumps(contract)),
                )
                conn.conn.commit()
            run_cli("choose-offer", "--write-root", td, "--confirm", "1")
            build_root = td_path / "builds" / "1"
            build_root.mkdir(parents=True)
            (build_root / "BUILD.md").write_text("# built")
            task_dir = td_path / "build-offer-1"
            task_dir.mkdir()
            db_path = td_path / "corpus.db"

            before = db_path.read_bytes()
            before_hash = hashlib.sha256(before).hexdigest()
            with Connection(db_path) as conn:
                before_receipts = len(conn.receipts_for_offer(1))
                before_status = conn.get_offer(1)["status"]
            self.assertEqual(before_status, "chosen")

            run_cli(
                "receipts-check",
                str(db_path), "1", str(build_root), str(task_dir / "receipts.jsonl"),
            )

            after = db_path.read_bytes()
            after_hash = hashlib.sha256(after).hexdigest()
            self.assertEqual(before_hash, after_hash)
            with Connection(db_path) as conn:
                self.assertEqual(conn.get_offer(1)["status"], "chosen")
                self.assertEqual(len(conn.receipts_for_offer(1)), before_receipts)

    def test_merge_build_rejects_tampered_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            run_cli("init", "--write-root", td)
            from automation_discovery.db import Connection
            with Connection(td_path / "corpus.db") as conn:
                conn.conn.execute(
                    "INSERT INTO candidates (job, event_ids, independence_note, consequence, overlap, status) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    ("j", "[1,2,3]", "three independent events", "c", "none", "audited"),
                )
                conn.conn.commit()
                contract = [
                    {"kind": "ok", "argv": ["python3", "-c", "print('ok')"], "success_contains": "ok"},
                    {"kind": "ok2", "argv": ["python3", "-c", "print('ok2')"]},
                    {"kind": "ok3", "argv": ["python3", "-c", "print('ok3')"]},
                ]
                conn.conn.execute(
                    "INSERT INTO offers (candidate_id, title, what_it_does, receipt_contract, status) "
                    "VALUES (?, ?, ?, ?, 'offered')",
                    (1, "t", "d", json.dumps(contract)),
                )
                conn.conn.commit()
            run_cli("choose-offer", "--write-root", td, "--confirm", "1")
            build_root = td_path / "builds" / "1"
            build_root.mkdir(parents=True)
            (build_root / "BUILD.md").write_text("# built")
            task_dir = td_path / "build-offer-1"
            task_dir.mkdir()
            receipts_jsonl = task_dir / "receipts.jsonl"
            db_path = td_path / "corpus.db"

            # Pre-stage valid receipts and tamper output_excerpt so it no longer
            # contains the required success_contains string.
            run_cli(
                "receipts-check",
                str(db_path), "1", str(build_root), str(receipts_jsonl),
            )
            before_hash = hashlib.sha256(db_path.read_bytes()).hexdigest()
            with Connection(db_path) as conn:
                before_status = conn.get_offer(1)["status"]
                before_receipts_count = len(conn.receipts_for_offer(1))
            lines = receipts_jsonl.read_text(encoding="utf-8").splitlines()
            row = json.loads(lines[0])
            row["output_excerpt"] = "tampered"
            lines[0] = json.dumps(row)
            receipts_jsonl.write_text("\n".join(lines) + "\n")

            result = run_cli(
                "merge-build", "--write-root", td, "--offer-id", "1", str(task_dir),
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("invalid staged receipts", result.stderr)
            after_hash = hashlib.sha256(db_path.read_bytes()).hexdigest()
            self.assertEqual(before_hash, after_hash)
            with Connection(db_path) as conn:
                self.assertEqual(conn.get_offer(1)["status"], before_status)
                self.assertEqual(len(conn.receipts_for_offer(1)), before_receipts_count)

    def test_merge_build_rejects_missing_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            run_cli("init", "--write-root", td)
            from automation_discovery.db import Connection
            with Connection(td_path / "corpus.db") as conn:
                conn.conn.execute(
                    "INSERT INTO candidates (job, event_ids, independence_note, consequence, overlap, status) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    ("j", "[1,2,3]", "i", "c", "none", "audited"),
                )
                conn.conn.commit()
                contract = [
                    {"kind": "ok", "argv": ["python3", "-c", "print('ok')"]},
                    {"kind": "ok2", "argv": ["python3", "-c", "print('ok2')"]},
                    {"kind": "ok3", "argv": ["python3", "-c", "print('ok3')"]},
                ]
                conn.conn.execute(
                    "INSERT INTO offers (candidate_id, title, what_it_does, receipt_contract, status) "
                    "VALUES (?, ?, ?, ?, 'offered')",
                    (1, "t", "d", json.dumps(contract)),
                )
                conn.conn.commit()
            run_cli("choose-offer", "--write-root", td, "--confirm", "1")
            build_root = td_path / "builds" / "1"
            build_root.mkdir(parents=True)
            (build_root / "BUILD.md").write_text("# built")
            task_dir = td_path / "build-offer-1"
            task_dir.mkdir()
            receipts_jsonl = task_dir / "receipts.jsonl"
            db_path = td_path / "corpus.db"

            run_cli(
                "receipts-check",
                str(db_path), "1", str(build_root), str(receipts_jsonl),
            )
            before_hash = hashlib.sha256(db_path.read_bytes()).hexdigest()
            with Connection(db_path) as conn:
                before_status = conn.get_offer(1)["status"]
                before_receipts_count = len(conn.receipts_for_offer(1))
            lines = [json.loads(line) for line in receipts_jsonl.read_text(encoding="utf-8").splitlines() if line.strip()]
            lines.pop(0)
            receipts_jsonl.write_text("\n".join(json.dumps(row) for row in lines) + "\n")

            result = run_cli(
                "merge-build", "--write-root", td, "--offer-id", "1", str(task_dir),
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("missing staged receipts", result.stderr)
            after_hash = hashlib.sha256(db_path.read_bytes()).hexdigest()
            self.assertEqual(before_hash, after_hash)
            with Connection(db_path) as conn:
                self.assertEqual(conn.get_offer(1)["status"], before_status)
                self.assertEqual(len(conn.receipts_for_offer(1)), before_receipts_count)

    def test_merge_build_rejects_offer_not_chosen(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            run_cli("init", "--write-root", td)
            from automation_discovery.db import Connection
            with Connection(td_path / "corpus.db") as conn:
                conn.conn.execute(
                    "INSERT INTO candidates (job, event_ids, independence_note, consequence, overlap, status) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    ("j", "[1,2,3]", "i", "c", "none", "audited"),
                )
                conn.conn.commit()
                contract = [
                    {"kind": "ok", "argv": ["python3", "-c", "print('ok')"]},
                    {"kind": "ok2", "argv": ["python3", "-c", "print('ok2')"]},
                    {"kind": "ok3", "argv": ["python3", "-c", "print('ok3')"]},
                ]
                conn.conn.execute(
                    "INSERT INTO offers (candidate_id, title, what_it_does, receipt_contract, status) "
                    "VALUES (?, ?, ?, ?, 'offered')",
                    (1, "t", "d", json.dumps(contract)),
                )
                conn.conn.commit()
            # Do NOT choose the offer.
            build_root = td_path / "builds" / "1"
            build_root.mkdir(parents=True)
            (build_root / "BUILD.md").write_text("# built")
            task_dir = td_path / "build-offer-1"
            task_dir.mkdir()
            db_path = td_path / "corpus.db"
            before_hash = hashlib.sha256(db_path.read_bytes()).hexdigest()
            with Connection(db_path) as conn:
                before_status = conn.get_offer(1)["status"]
                before_receipts_count = len(conn.receipts_for_offer(1))

            # Stage valid receipts so the transaction reaches the status guard.
            receipts_jsonl = task_dir / "receipts.jsonl"
            rows = [
                json.dumps(
                    {
                        "kind": kind,
                        "argv": ["python3", "-c", f"print('{kind}')"],
                        "output_excerpt": kind,
                        "exit_code": 0,
                        "passed": True,
                        "created_at": "2026-01-01T00:00:00Z",
                    }
                )
                for kind in ("ok", "ok2", "ok3")
            ]
            receipts_jsonl.write_text("\n".join(rows) + "\n")

            result = run_cli(
                "merge-build", "--write-root", td, "--offer-id", "1", str(task_dir),
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("must be 'chosen'", result.stderr)
            after_hash = hashlib.sha256(db_path.read_bytes()).hexdigest()
            self.assertEqual(before_hash, after_hash)
            with Connection(db_path) as conn:
                self.assertEqual(conn.get_offer(1)["status"], before_status)
                self.assertEqual(len(conn.receipts_for_offer(1)), before_receipts_count)

    def test_failed_staged_receipt_rejects_merge(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            run_cli("init", "--write-root", td)
            from automation_discovery.db import Connection
            with Connection(td_path / "corpus.db") as conn:
                conn.conn.execute(
                    "INSERT INTO candidates (job, event_ids, independence_note, consequence, overlap, status) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    ("j", "[1,2,3]", "i", "c", "none", "audited"),
                )
                conn.conn.commit()
                contract = [
                    {"kind": "ok", "argv": ["python3", "-c", "print('OK')"], "success_contains": "OK"},
                    {"kind": "bad", "argv": ["python3", "-c", "raise SystemExit(1)"]},
                    {"kind": "run", "argv": ["python3", "-c", "print('result')"], "success_contains": "result"},
                ]
                conn.conn.execute(
                    "INSERT INTO offers (candidate_id, title, what_it_does, receipt_contract, status) "
                    "VALUES (?, ?, ?, ?, 'offered')",
                    (1, "t", "d", json.dumps(contract)),
                )
                conn.conn.commit()
            run_cli("choose-offer", "--write-root", td, "--confirm", "1")
            build_root = td_path / "builds" / "1"
            build_root.mkdir(parents=True)
            (build_root / "BUILD.md").write_text("# built")
            task_dir = td_path / "build-offer-1"
            task_dir.mkdir()
            receipts_jsonl = task_dir / "receipts.jsonl"

            # The read-only check writes a receipts.jsonl that contains a failure.
            db_path = td_path / "corpus.db"
            before_hash = hashlib.sha256(db_path.read_bytes()).hexdigest()
            with Connection(db_path) as conn:
                before_status = conn.get_offer(1)["status"]
                before_receipts_count = len(conn.receipts_for_offer(1))
            run_cli(
                "receipts-check",
                str(db_path), "1", str(build_root), str(receipts_jsonl),
                check=False,
            )
            result = run_cli(
                "merge-build", "--write-root", td, "--offer-id", "1", str(task_dir),
                check=False,
            )
            self.assertEqual(result.returncode, 2)

            after_hash = hashlib.sha256(db_path.read_bytes()).hexdigest()
            self.assertEqual(before_hash, after_hash)
            with Connection(db_path) as conn:
                offer = conn.get_offer(1)
                receipts = conn.receipts_for_offer(1)
            self.assertEqual(offer["status"], before_status)
            self.assertEqual(len(receipts), before_receipts_count)


    def test_merge_build_rejects_boolean_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path, db_path, build_root, task_dir, receipts_jsonl = self._chosen_offer_with_staged_receipts(td)
            before_hash = hashlib.sha256(db_path.read_bytes()).hexdigest()
            from automation_discovery.db import Connection
            with Connection(db_path) as conn:
                before_status = conn.get_offer(1)["status"]
                before_receipts_count = len(conn.receipts_for_offer(1))

            lines = receipts_jsonl.read_text(encoding="utf-8").splitlines()
            row = json.loads(lines[0])
            row["exit_code"] = True
            lines[0] = json.dumps(row)
            receipts_jsonl.write_text("\n".join(lines) + "\n")

            result = run_cli(
                "merge-build", "--write-root", td, "--offer-id", "1", str(task_dir),
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("exit_code must be an integer", result.stderr)
            after_hash = hashlib.sha256(db_path.read_bytes()).hexdigest()
            self.assertEqual(before_hash, after_hash)
            with Connection(db_path) as conn:
                self.assertEqual(conn.get_offer(1)["status"], before_status)
                self.assertEqual(len(conn.receipts_for_offer(1)), before_receipts_count)

    def test_merge_build_rejects_invalid_created_at(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path, db_path, build_root, task_dir, receipts_jsonl = self._chosen_offer_with_staged_receipts(td)
            before_hash = hashlib.sha256(db_path.read_bytes()).hexdigest()
            from automation_discovery.db import Connection
            with Connection(db_path) as conn:
                before_status = conn.get_offer(1)["status"]
                before_receipts_count = len(conn.receipts_for_offer(1))

            lines = receipts_jsonl.read_text(encoding="utf-8").splitlines()
            row = json.loads(lines[0])
            row["created_at"] = "not-a-timestamp"
            lines[0] = json.dumps(row)
            receipts_jsonl.write_text("\n".join(lines) + "\n")

            result = run_cli(
                "merge-build", "--write-root", td, "--offer-id", "1", str(task_dir),
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("created_at must be a valid ISO date/datetime", result.stderr)
            after_hash = hashlib.sha256(db_path.read_bytes()).hexdigest()
            self.assertEqual(before_hash, after_hash)
            with Connection(db_path) as conn:
                self.assertEqual(conn.get_offer(1)["status"], before_status)
                self.assertEqual(len(conn.receipts_for_offer(1)), before_receipts_count)

    def test_merge_build_rejects_oversized_output_excerpt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path, db_path, build_root, task_dir, receipts_jsonl = self._chosen_offer_with_staged_receipts(td)
            before_hash = hashlib.sha256(db_path.read_bytes()).hexdigest()
            from automation_discovery.db import Connection
            with Connection(db_path) as conn:
                before_status = conn.get_offer(1)["status"]
                before_receipts_count = len(conn.receipts_for_offer(1))

            lines = receipts_jsonl.read_text(encoding="utf-8").splitlines()
            row = json.loads(lines[1])
            row["output_excerpt"] = "x" * (OUTPUT_LIMIT + 1)
            lines[1] = json.dumps(row)
            receipts_jsonl.write_text("\n".join(lines) + "\n")

            result = run_cli(
                "merge-build", "--write-root", td, "--offer-id", "1", str(task_dir),
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("output_excerpt exceeds", result.stderr)
            after_hash = hashlib.sha256(db_path.read_bytes()).hexdigest()
            self.assertEqual(before_hash, after_hash)
            with Connection(db_path) as conn:
                self.assertEqual(conn.get_offer(1)["status"], before_status)
                self.assertEqual(len(conn.receipts_for_offer(1)), before_receipts_count)


class ValidateExportTests(unittest.TestCase):
    def test_validate_and_export(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            run_cli("init", "--write-root", td)
            run_cli("add-source", "--write-root", td, "--surface", "x", "--kind", "other", "--access-route", "/x")
            run_cli("approve-sources", "--write-root", td, "--confirm")
            task_dir = td_path / "ingest-1-x"
            task_dir.mkdir()
            events = [
                json.dumps({"source_ref": f"a{i}", "action": f"action {i}", "job_hint": "job"})
                for i in range(10)
            ]
            (task_dir / "events.jsonl").write_text("\n".join(events) + "\n")
            (task_dir / "source_summary.json").write_text(json.dumps({"status": "ingested", "notes": "10"}))
            run_cli("merge-ingest", "--write-root", td, "--source", "1", "--lane", "ringer", str(task_dir))

            result = run_cli("validate", "--write-root", td)
            self.assertIn("OK: corpus validation passed", result.stdout)

            out = td_path / "export"
            run_cli("export", "--write-root", td, "--out", str(out))
            self.assertTrue((out / "sources.md").is_file())
            self.assertTrue((out / "events-by-job.md").is_file())


if __name__ == "__main__":
    unittest.main()
