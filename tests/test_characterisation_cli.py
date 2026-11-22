"""CLI and import compatibility contracts for the ringer.py split."""
from __future__ import annotations

import argparse
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ringer


# Snapshot of public names explicitly imported by the existing tests and preview helper.
# Check availability so the implementation can move and ringer can expose new names.
TEST_IMPORTS = """
ARTIFACT_BASE_CSS
ARTIFACT_WRAPPER_TAIL_BYTES
AppConfig
ArtifactConfig
ArtifactRenderer
CSP_META_TAG
CatalogRefreshResult
DELIVERABLE_MAX_BYTES
Dashboard
EngineConfig
EvalConfig
EvalLogger
HUD_TOKEN_HEADER
MODEL_SCOREBOARD_COLUMNS
Manifest
PersistentHudServer
RESERVED_FIXTURE_MODELS
RingerRunner
StateWriter
SteeringConfig
SteeringProfile
SteeringRule
TaskRuntime
TaskSpec
UpdateConfig
Verifier
VerifyResult
WORKER_LOG_TAIL_BYTES
WorkerResult
active_runs_path
aggregate_model_log_rows
aggregate_model_scoreboard_rows
artifact_library_path
artifact_live_path
artifact_version_path
build_context_packet
build_models_api_payload
build_worker_command
catalog_changes_path
connect_read_model_db
create_read_model_schema
effective_model_from_command
enrich_model_groups_with_identity
ensure_hud_running
fetch_catalog_payload
file_href
hud_should_restart
humanized_log_date
image_data_uri
inject_models_tab_into_ringside_html
inject_steering_spec
lint_manifest
load_engines
load_model_identity_registry
load_steering_config
maybe_self_update
model_judgment_notes
model_log_row_is_retry
normalize_catalog_payload
parse_model_notes_sections
parse_reported_model
parse_steering_profile
perform_self_update
preflight_engine_bins
print_model_log_table
print_steering_notes
read_active_runs
read_artifact_library
read_model_log_rows
rebuild_read_model_db
reconcile_artifact_library_dead_runs
refresh_openrouter_catalog
register_active_run
render_artifact_index_html
render_final_report_html
render_status_html
resolve_steering_profile
run_catalog_command
run_models_command
self_update_state_path
start_catalog_auto_refresh
steering_profile_candidates
sync_read_model_db
unregister_active_run
update_artifact_library_live
validate_manifest_engines
write_dashboard_launcher
""".split()


class CliContractTests(unittest.TestCase):
    def test_public_test_imports_remain_available(self) -> None:
        self.assertEqual([], [name for name in TEST_IMPORTS if not hasattr(ringer, name)])

    def test_subcommands_and_nested_db_commands(self) -> None:
        parser = ringer.build_parser()
        subcommands = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
        self.assertEqual(
            ["ask", "assess", "catalog", "db", "demo", "hud", "install-agent", "lint",
             "models", "outcomes", "run", "self-update", "uninstall-agent"],
            sorted(subcommands.choices),
        )
        db_subcommands = next(
            action for action in subcommands.choices["db"]._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        self.assertEqual(["rebuild", "sync"], sorted(db_subcommands.choices))

    def test_key_flag_defaults(self) -> None:
        parser = ringer.build_parser()
        cases = [
            (["run", "manifest.json"], dict(
                manifest=Path("manifest.json"), config=None, no_self_update=False,
                max_parallel=None, identity=None, no_dashboard=False, browser=False,
                no_artifact=False, dry_run=False, baseline=False, allow_noncanonical_route=False,
            )),
            (["ask"], dict(
                request=None, request_file=None, source=[], state=[], engine="codex",
                model=None, billing_route=None, task_class="", task_spend_allowance_gbp=None,
                reasoning_effort="low", timeout_s=300, max_packet_bytes=16_000,
                max_file_bytes=4_000_000, max_files=200, workdir=None, keep_packet=False,
                redact=False, identity=None, dry_run=False, assessment=None,
            )),
            (["hud"], dict(port=None, no_open=False, force_open=False)),
            (["models"], dict(
                log=None, db=None, task_type=None, model=None, engine=None, since=None,
                explore=False, catalog_file=None, html=None, open=False, json=False,
            )),
            (["outcomes"], dict(state_dir=None, log=None, job_id=None, json=False, read_back=False)),
        ]
        for argv, defaults in cases:
            with self.subTest(argv=argv):
                args = parser.parse_args(argv)
                self.assertEqual(defaults, {name: getattr(args, name) for name in defaults})

    def test_flag_spellings_types_and_optional_html_path(self) -> None:
        parser = ringer.build_parser()
        run = parser.parse_args(
            ["--config", "config.toml", "--no-self-update", "run", "manifest.json", "--max-parallel", "3",
             "--no-dashboard", "--no-artifact", "--baseline", "--dry-run", "--allow-noncanonical-route"],
        )
        self.assertEqual((Path("config.toml"), 3), (run.config, run.max_parallel))
        self.assertTrue(run.no_self_update)
        self.assertTrue(run.no_dashboard)
        self.assertTrue(run.no_artifact)
        self.assertTrue(run.baseline)
        self.assertTrue(run.dry_run)
        self.assertTrue(run.allow_noncanonical_route)

        hud = parser.parse_args(
            ["--config", "global.toml", "hud", "--config", "local.toml", "--port", "8788", "--no-open", "--force-open"],
        )
        self.assertEqual((Path("local.toml"), 8788, True, True), (hud.config, hud.port, hud.no_open, hud.force_open))
        self.assertEqual("", parser.parse_args(["models", "--html"]).html)
        self.assertEqual("scoreboard.html", parser.parse_args(["models", "--html", "scoreboard.html"]).html)
        outcomes = parser.parse_args(["outcomes", "--state-dir", "state", "--job-id", "job", "--json", "--read-back"])
        self.assertEqual((Path("state"), "job", True, True),
                         (outcomes.state_dir, outcomes.job_id, outcomes.json, outcomes.read_back))

    def test_main_argparse_usage_errors_exit_two(self) -> None:
        cases = [
            ([], "the following arguments are required: command"),
            (["missing-command"], "invalid choice: 'missing-command'"),
            (["run"], "the following arguments are required: manifest"),
            (["hud", "--port", "text"], "invalid int value: 'text'"),
        ]
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"HOME": home}):
            for argv, message in cases:
                with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()) as errors:
                    with self.assertRaises(SystemExit) as exit_error:
                        ringer.main(["--no-self-update", *argv])
                    self.assertEqual(2, exit_error.exception.code)
                    self.assertIn(message, errors.getvalue())

    def test_main_rejects_invalid_install_and_uninstall_flags_with_code_two(self) -> None:
        cases = [
            (["install-agent"], "install-agent requires --codex"),
            (["uninstall-agent"], "uninstall-agent requires --codex or --claude"),
            (["uninstall-agent", "--codex", "--claude"], "uninstall-agent accepts one"),
        ]
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"HOME": home}):
            for argv, message in cases:
                with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()) as errors:
                    self.assertEqual(2, ringer.main(["--no-self-update", *argv]))
                    self.assertIn(message, errors.getvalue())
