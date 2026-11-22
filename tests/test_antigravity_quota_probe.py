"""Antigravity OAuth, quota transport, and subscription admission fixtures."""
from __future__ import annotations

import asyncio
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from email.message import Message
from http import HTTPStatus
from pathlib import Path
from unittest.mock import patch
from urllib.response import addinfourl

import ringer
import ringer_billing as billing
from ringer_costs import inspect_usage_snapshot, validate_cost_policy


def quota_payload(primary=0.75, weekly=0.92):
    now = datetime.now(timezone.utc)
    return {"quotaSummary": {"groups": [
        {"displayName": "Gemini Models", "buckets": [
            {"bucketId": "gemini-5h", "refreshWindowLabel": "5h", "remainingFraction": primary,
             "resetTime": (now + timedelta(hours=5)).isoformat().replace("+00:00", "Z")},
            {"bucketId": "gemini-weekly", "refreshWindowLabel": "weekly", "remainingFraction": weekly,
             "resetTime": (now + timedelta(days=7)).isoformat()},
        ]},
        {"displayName": "Claude and GPT models", "buckets": []},
    ]}}


def quota_buckets(payload):
    return payload["quotaSummary"]["groups"][0]["buckets"]


async def run_transport_inline(function, *args):
    # The network-disabled sandbox also blocks asyncio's thread wakeup socket.
    return function(*args)


class QuotaParserTests(unittest.TestCase):
    def test_healthy_snapshot_passes_inspection(self):
        now = datetime.now(timezone.utc)
        snapshot = billing.parse_gemini_quota_status(quota_payload(), now=now)
        self.assertEqual("antigravity", snapshot["observed_auth_mode"])
        self.assertEqual(now.isoformat(), snapshot["observed_at"])
        evidence = inspect_usage_snapshot(snapshot, now=now, limit_key="gemini")
        self.assertTrue(evidence.available, evidence.reason)
        primary, weekly = evidence.windows
        self.assertEqual(300, primary["duration_minutes"])
        self.assertEqual(10080, weekly["duration_minutes"])
        self.assertAlmostEqual(25, primary["used_percent"])
        self.assertAlmostEqual(8, weekly["used_percent"])

    def test_exact_zero_in_either_window_is_exhausted(self):
        for fractions in ((0, 0.9), (0.8, 0)):
            with self.subTest(fractions=fractions):
                snapshot = billing.parse_gemini_quota_status(quota_payload(*fractions))
                evidence = inspect_usage_snapshot(snapshot, limit_key="gemini")
                self.assertFalse(evidence.available)
                self.assertIn("quota exhausted", evidence.reason)

    def test_tiny_positive_fraction_stays_available(self):
        for fraction in (0.004, 0.000004, 1e-300):
            with self.subTest(fraction=fraction):
                snapshot = billing.parse_gemini_quota_status(quota_payload(fraction, fraction))
                evidence = inspect_usage_snapshot(snapshot, limit_key="gemini")
                self.assertTrue(evidence.available, evidence.reason)
                self.assertTrue(all(window["used_percent"] < 100 for window in evidence.windows))

    def test_both_windows_are_required(self):
        for index, window in ((0, "5-hour"), (1, "weekly")):
            with self.subTest(window=window):
                payload = quota_payload()
                quota_buckets(payload).pop(index)
                with self.assertRaisesRegex(billing.AccountEvidenceError, f"{window} quota bucket is missing"):
                    billing.parse_gemini_quota_status(payload)

    def test_missing_remaining_fraction_is_invalid(self):
        payload = quota_payload()
        del quota_buckets(payload)[0]["remainingFraction"]
        with self.assertRaisesRegex(billing.AccountEvidenceError, "remainingFraction"):
            billing.parse_gemini_quota_status(payload)

    def test_invalid_remaining_fractions(self):
        for value in (-0.1, 1.1, "0.5", float("nan"), float("inf"), -float("inf"), True, None, {}, 10**400):
            with self.subTest(value=value):
                with self.assertRaisesRegex(billing.AccountEvidenceError, "remainingFraction"):
                    billing.parse_gemini_quota_status(quota_payload(weekly=value))

    def test_invalid_reset_times(self):
        for value in (None, -1, True, {}, "tomorrow", "2026-11-22", "1969-12-31T23:59:59.9Z"):
            with self.subTest(value=value):
                payload = quota_payload()
                quota_buckets(payload)[0]["resetTime"] = value
                with self.assertRaisesRegex(billing.AccountEvidenceError, "resetTime"):
                    billing.parse_gemini_quota_status(payload)

    def test_expired_window_is_unavailable(self):
        payload = quota_payload()
        quota_buckets(payload)[1]["resetTime"] = "2000-01-01T00:00:00Z"
        snapshot = billing.parse_gemini_quota_status(payload)
        evidence = inspect_usage_snapshot(snapshot, limit_key="gemini")
        self.assertFalse(evidence.available)
        self.assertIn("expired", evidence.reason)

    def test_unexpected_shapes_and_wrong_field_types_are_rejected(self):
        for payload in (None, [], "quota", {}, {"error": {}}, {"quotaSummary": []},
                        {"quotaSummary": {"groups": {}}}, {"quotaSummary": {"groups": [True]}},
                        {"quotaSummary": {"groups": []}},
                        {"quotaSummary": {"groups": [{"displayName": "Other", "buckets": []}]}},
                        {"quotaSummary": {"groups": [{"displayName": {}, "buckets": []}]}},
                        {"quotaSummary": {"groups": [{"displayName": "Gemini Models", "buckets": [True]}]}}):
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(billing.AccountEvidenceError, "quota"):
                    billing.parse_gemini_quota_status(payload)
        for key, value in (("bucketId", 5), ("refreshWindowLabel", ["weekly"]), ("disabled", "false")):
            with self.subTest(key=key):
                payload = quota_payload()
                quota_buckets(payload)[1][key] = value
                with self.assertRaisesRegex(billing.AccountEvidenceError, "quota bucket"):
                    billing.parse_gemini_quota_status(payload)

    def test_ambiguous_or_disabled_windows_are_rejected(self):
        for replacement in ({"refreshWindowLabel": "weekly"}, {"disabled": True}):
            with self.subTest(replacement=replacement):
                payload = quota_payload()
                quota_buckets(payload)[0].update(replacement)
                with self.assertRaisesRegex(billing.AccountEvidenceError, "ambiguous|disabled"):
                    billing.parse_gemini_quota_status(payload)
        payload = quota_payload()
        buckets = quota_buckets(payload)
        buckets.append(dict(buckets[0], remainingFraction=0))
        with self.assertRaisesRegex(billing.AccountEvidenceError, "ambiguous"):
            billing.parse_gemini_quota_status(payload)


class QuotaTransportTests(unittest.IsolatedAsyncioTestCase):
    async def probe(self, status, body=b"", location=None):
        def reply(handler, connection, request, **kwargs):
            headers = Message()
            if location:
                headers["Location"] = location
            response = addinfourl(io.BytesIO(body), headers, request.full_url, status)
            response.msg = HTTPStatus(status).phrase
            return response

        with (
            patch.object(billing, "_read_antigravity_access_token", return_value="fixture-oauth"),
            patch.object(billing.asyncio, "to_thread", new=run_transport_inline),
            patch("urllib.request.getproxies", return_value={}),
            patch("urllib.request.AbstractHTTPHandler.do_open", autospec=True, side_effect=reply) as transport,
        ):
            try:
                return await billing.observe_gemini_account(timeout_s=2)
            finally:
                transport.assert_called_once()
                request = transport.call_args.args[2]
                self.assertEqual("https://aicode.googleapis.com/v1alpha:fetchQuotaStatus", request.full_url)
                self.assertEqual("GET", request.get_method())
                self.assertEqual("Bearer fixture-oauth", request.get_header("Authorization"))

    async def test_http_200_returns_sanitised_snapshot(self):
        payload = quota_payload()
        payload["extra"] = "raw-response-field"
        snapshot = await self.probe(200, json.dumps(payload).encode())
        self.assertEqual("antigravity", snapshot["observed_auth_mode"])
        self.assertNotIn("raw-response-field", json.dumps(snapshot))
        self.assertNotIn("fixture-oauth", json.dumps(snapshot))

    async def test_refuses_redirects_without_sending_oauth_onward(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status), self.assertRaisesRegex(billing.AccountEvidenceError, f"HTTP {status}"):
                await self.probe(status, location="https://example.invalid/redirect")

    async def test_non_200_responses_are_rejected(self):
        for status in (201, 401, 429, 503):
            with self.subTest(status=status), self.assertRaisesRegex(billing.AccountEvidenceError, f"HTTP {status}"):
                await self.probe(status, json.dumps(quota_payload()).encode())

    async def test_malformed_json_is_rejected(self):
        with self.assertRaisesRegex(billing.AccountEvidenceError, "malformed JSON"):
            await self.probe(200, b"{private-response-data")

    async def test_wire_fraction_is_validated_before_float_rounding(self):
        body = json.dumps(quota_payload(weekly="fraction-placeholder")).encode()
        with self.assertRaisesRegex(billing.AccountEvidenceError, "remainingFraction"):
            await self.probe(200, body.replace(b'"fraction-placeholder"', b"1.00000000000000001"))
        snapshot = await self.probe(200, body.replace(b'"fraction-placeholder"', b"1e-400"))
        self.assertTrue(inspect_usage_snapshot(snapshot, limit_key="gemini").available)


class OAuthLookupTests(unittest.TestCase):
    def test_supported_keyrings_supply_nested_oauth_token(self):
        for platform, command in (("linux", "secret-tool"), ("darwin", "security")):
            with (
                self.subTest(platform=platform),
                patch.object(billing.sys, "platform", platform),
                patch.object(billing.subprocess, "check_output", return_value='{"token":{"access_token":"fixture"}}') as lookup,
                patch.object(Path, "read_text", side_effect=AssertionError("unexpected token file read")),
            ):
                self.assertEqual("fixture", billing._read_antigravity_access_token(timeout_s=2))
                self.assertEqual(command, lookup.call_args.args[0][0])

    def test_file_fallback_supplies_oauth_token(self):
        with (
            patch.object(billing.subprocess, "check_output", side_effect=FileNotFoundError),
            patch.dict(os.environ, {"AGY_DIR": "/fixture/agy"}),
            patch.object(Path, "read_text", autospec=True, return_value='{"access_token":"fixture"}') as read,
        ):
            self.assertEqual("fixture", billing._read_antigravity_access_token(timeout_s=2))
            read.assert_called_once_with(Path("/fixture/agy/antigravity-oauth-token"), encoding="utf-8")

    def test_does_not_use_api_keys_as_oauth_evidence(self):
        with (
            patch.object(billing.subprocess, "check_output", side_effect=FileNotFoundError),
            patch.object(Path, "read_text", side_effect=FileNotFoundError),
            patch.dict(os.environ, {"GEMINI_API_KEY": "fixture", "GOOGLE_API_KEY": "fixture"}),
            self.assertRaisesRegex(billing.AccountEvidenceError, "OAuth login"),
        ):
            billing._read_antigravity_access_token(timeout_s=2)


class SubscriptionAdmissionTests(unittest.TestCase):
    def run_with_policy(self, payload, *, engine="gemini", route="subscription", modes=("chatgpt", "antigravity")):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        policy = root / "cost-policy.json"
        policy.write_text(json.dumps({"subscription_auth_modes": list(modes)}), encoding="utf-8")
        worker = root / "fixture-worker"
        worker.write_text("#!/bin/sh\nprintf 'fixture ok\\n' > out.txt\n", encoding="utf-8")
        worker.chmod(0o755)
        model = {"gemini": "gemini-4-argon-medium", "grok": "grok-4.7",
                 "openrouter": "openrouter/z-ai/glm-5.3-flash"}[engine]
        manifest = ringer.Manifest.from_obj({
            "run_name": "antigravity-quota-test", "workdir": str(root / "work"),
            "tasks": [{"key": "one", "spec": "Write out.txt", "engine": engine, "model": model,
                       "billing_route": route, "max_attempts": 1, "check": "test -s out.txt",
                       "expect_files": ["out.txt"]}],
        })
        config = ringer.AppConfig(
            path=None, identity_default=None, state_dir=root / "state",
            dashboard_port_base=18787, hud_port=18788, hud_app_path=None, allow_full_access=False,
            eval=ringer.EvalConfig(backend="jsonl", jsonl_path=root / "attempts.jsonl"),
            engines={engine: ringer.EngineConfig(
                name=engine, bin=str(worker), args_template=("{spec}",), sandbox_args=(),
                full_access_args=(), token_regex=None, model_default=model,
            )},
            artifact=ringer.ArtifactConfig(enabled=False, out_template="", report_template="", index_out=root / "index"),
            billing_policy_path=policy,
        )
        runner = ringer.RingerRunner(manifest, config, "test", dashboard_enabled=False)
        stderr = io.StringIO()
        with (
            patch.object(ringer, "validate_manifest_model_assessment", return_value={}),
            patch.object(billing, "_read_antigravity_access_token", return_value="fixture-oauth") as read_token,
            patch.object(billing.asyncio, "to_thread", new=run_transport_inline),
            patch("urllib.request.build_opener") as opener,
            patch.dict(os.environ, {"RINGER_USAGE_DIR": str(root / "usage")}),
            redirect_stdout(io.StringIO()),
            redirect_stderr(stderr),
        ):
            response = opener.return_value.open.return_value.__enter__.return_value
            response.status = 200
            response.read.return_value = json.dumps(payload).encode()
            code = asyncio.run(runner.run())
        self.assertFalse((root / "state" / "cost-reservations.json").exists())
        runtime = runner.runtimes[0]
        self.assertEqual(engine, runtime.task.engine)
        return code, runtime, stderr.getvalue(), read_token.call_count

    def test_healthy_subscription_admits_gemini_under_loaded_policy(self):
        code, runtime, _, probes = self.run_with_policy(quota_payload())
        self.assertEqual(0, code)
        self.assertEqual("ADMITTED", runtime.billing["billing_status"])
        self.assertEqual("antigravity", runtime.billing["observed_auth_mode"])
        self.assertEqual("fixture ok\n", (runtime.taskdir / "out.txt").read_text(encoding="utf-8"))
        self.assertEqual(1, probes)

    def test_exhausted_and_malformed_quota_emit_notice_and_block_worker(self):
        missing_weekly = quota_payload()
        quota_buckets(missing_weekly).pop()
        cases = ((quota_payload(0, 0.9), "quota exhausted"),
                 (quota_payload(0.9, 0), "quota exhausted"),
                 (missing_weekly, "weekly quota bucket is missing"),
                 (quota_payload(weekly=1.1), "remainingFraction"))
        for payload, reason in cases:
            with self.subTest(reason=reason):
                code, runtime, stderr, probes = self.run_with_policy(payload)
                self.assertEqual(1, code)
                self.assertEqual("blocked", runtime.status)
                self.assertEqual("quota", runtime.failure_class)
                self.assertIn(reason, runtime.billing["billing_reason"])
                self.assertIn("Quota blocked billing admission", stderr)
                self.assertIn("OpenRouter was not selected", stderr)
                self.assertFalse((runtime.taskdir / "out.txt").exists())
                self.assertEqual(1, probes)

    def test_unapproved_routes_block_before_oauth_probe(self):
        for engine, route, modes, reason in (
            ("gemini", "subscription", ("chatgpt",), "subscription_auth_modes"),
            ("gemini", "api", ("antigravity",), "no API-key route"),
            ("grok", "subscription", ("antigravity",), "not subscription evidence"),
            ("openrouter", "subscription", ("antigravity",), "Explicit OpenRouter route requires billing_route api"),
        ):
            with self.subTest(engine=engine, route=route):
                code, runtime, _, probes = self.run_with_policy(quota_payload(), engine=engine, route=route, modes=modes)
                self.assertEqual(1, code)
                self.assertEqual("blocked", runtime.status)
                self.assertIn(reason, runtime.setup_error)
                self.assertFalse((runtime.taskdir / "out.txt").exists())
                self.assertEqual(0, probes)

    def test_policy_still_rejects_gemini_credit_fallback(self):
        with self.assertRaisesRegex(ValueError, r"unknown cost policy keys: \['gemini_credit_fallback'\]"):
            validate_cost_policy({"subscription_auth_modes": ["antigravity"], "gemini_credit_fallback": False})
