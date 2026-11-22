"""HTTP and filesystem contracts for Ringside and the per-run dashboard."""
from __future__ import annotations

import contextlib
import io
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ringer
from test_ringer_browser import MemoryServer


class HudContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {
            "HOME": str(self.root / "home"),
            "RINGER_HOME": str(self.root / "ringer-home"),
        }))
        self.enterContext(patch.object(ringer, "ReusableThreadingHTTPServer", MemoryServer))
        self.enterContext(patch.object(ringer, "ThreadingHTTPServer", MemoryServer))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def start_hud(self) -> ringer.PersistentHudServer:
        hud = ringer.PersistentHudServer(self.root / "custom-hud", 8700, open_viewer=False)
        hud.start()
        self.addCleanup(hud.stop)
        return hud

    def test_private_routes_require_current_launch_header_and_loopback_host(self) -> None:
        first = self.start_hud()
        hud = self.start_hud()
        self.assertEqual(("127.0.0.1", 8700), hud.httpd.server_address)
        self.assertNotEqual(first.token, hud.token)
        self.assertEqual(hud.token, ringer.read_hud_token(hud.state_dir))
        self.assertEqual(0o600, stat.S_IMODE((hud.state_dir / "hud.token").stat().st_mode))
        session = {"X-Ringer-Token": hud.token}

        for host in ("localhost:8700", "127.0.0.1:8700"):
            with self.subTest(host=host):
                self.assertEqual(200, hud.httpd.request("/api/viewer-status", headers={**session, "Host": host})[0])
        for host in ("", "localhost", "127.0.0.1:8701", "foreign.invalid:8700"):
            with self.subTest(host=host):
                for path in ("/", "/api/viewer-status"):
                    self.assertEqual(403, hud.httpd.request(path, headers={**session, "Host": host})[0])

        for path in ("/api/viewer-status", "/artifacts/report.html", "/logs/run/task"):
            for headers in ({}, {"X-Ringer-Token": "wrong"}, {"X-Ringer-Token": first.token}):
                with self.subTest(path=path, headers=headers):
                    self.assertEqual(401, hud.httpd.request(path, headers=headers)[0])
        self.assertEqual(200, hud.httpd.request("/")[0])

    def test_open_folder_checks_host_origin_and_token_before_opening(self) -> None:
        hud = self.start_hud()
        target = hud.state_dir / "artifacts" / "deliverables" / "saved-run"
        target.mkdir(parents=True)
        path = "/api/open-folder?run=saved-run"
        session = {"X-Ringer-Token": hud.token}
        loopback_origin = {"Origin": "http://localhost:8700"}
        with (
            patch.object(ringer, "folder_opener", return_value=["/fixture/open"]),
            patch.object(ringer.subprocess, "Popen") as opener,
        ):
            self.assertEqual(405, hud.httpd.request(path, headers=session)[0])
            for headers, status in (
                (session, 403),
                ({**session, "Origin": "http://localhost:8701"}, 403),
                ({**session, "Origin": "https://foreign.invalid"}, 403),
                ({**session, **loopback_origin, "Host": "foreign.invalid:8700"}, 403),
                (loopback_origin, 401),
            ):
                with self.subTest(headers=headers):
                    self.assertEqual(status, hud.httpd.request(path, method="POST", headers=headers)[0])
            opener.assert_not_called()
            self.assertEqual(204, hud.httpd.request(path, method="POST", headers={**session, **loopback_origin})[0])
            opener.assert_called_once_with(
                ["/fixture/open", str(target.resolve())], stdin=ringer.subprocess.DEVNULL,
                stdout=ringer.subprocess.DEVNULL, stderr=ringer.subprocess.DEVNULL,
            )

    def test_native_preflight_is_tokenless_and_returns_exact_cors_headers(self) -> None:
        hud = self.start_hud()
        path = "/api/open-folder"
        preflight = {"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "x-ringer-token"}
        for origin in ("tauri://localhost", "http://tauri.localhost", "https://tauri.localhost"):
            with self.subTest(origin=origin):
                status, headers, body = hud.httpd.request(
                    path, method="OPTIONS", headers={**preflight, "Origin": origin},
                )
                self.assertEqual((204, b""), (status, body))
                self.assertEqual({
                    "Access-Control-Allow-Origin": origin,
                    "Access-Control-Allow-Methods": "GET, POST",
                    "Access-Control-Allow-Headers": "X-Ringer-Token",
                    "Access-Control-Max-Age": "600",
                    "Vary": "Origin",
                }, {key: value for key, value in headers.items() if key.startswith("Access-Control-") or key == "Vary"})
        for headers in (
            preflight,
            {**preflight, "Origin": "http://localhost:8700"},
            {**preflight, "Origin": "https://foreign.invalid"},
            {**preflight, "Origin": "tauri://localhost", "Host": "foreign.invalid:8700"},
        ):
            with self.subTest(headers=headers):
                status, response_headers, _ = hud.httpd.request(path, method="OPTIONS", headers=headers)
                self.assertEqual(403, status)
                self.assertIsNone(response_headers.get("Access-Control-Allow-Origin"))

    def test_dashboard_accepts_launch_and_browser_tokens_only_in_header(self) -> None:
        state_path = self.root / "custom-dashboard" / "runs" / "run.json"
        state_path.parent.mkdir(parents=True)
        state = b'{"run_id":"fixture"}'
        state_path.write_bytes(state)
        dashboard = ringer.Dashboard(state_path, 8787, open_viewer=False)
        dashboard.start()
        self.addCleanup(dashboard.stop)
        browser_token = ringer.dashboard_browser_token(dashboard.token)
        self.assertNotEqual(dashboard.token, browser_token)

        for token in (dashboard.token, browser_token):
            with self.subTest(token=token):
                status, headers, body = dashboard.httpd.request("/state.json", headers={"X-Ringer-Token": token})
                self.assertEqual((200, "no-store", state), (status, headers["Cache-Control"], body))
                self.assertEqual(401, dashboard.httpd.request(f"/state.json?token={token}")[0])
        self.assertEqual(401, dashboard.httpd.request("/state.json", headers={"Authorization": f"Bearer {dashboard.token}"})[0])

    def test_custom_launcher_directory_is_private_when_created(self) -> None:
        custom = self.root / "custom-dashboard"
        previous = os.umask(0o002)
        try:
            launcher = ringer.write_dashboard_launcher(custom, 8787, "a" * 43)
        finally:
            os.umask(previous)
        self.assertEqual(custom.resolve() / "browser-8787.html", launcher)
        self.assertEqual(0o700, stat.S_IMODE(custom.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(launcher.stat().st_mode))

    def test_existing_custom_launcher_directory_must_be_writable_only_by_owner(self) -> None:
        custom = self.root / "custom-dashboard"
        custom.mkdir()
        for mode in (0o770, 0o702):
            with self.subTest(mode=oct(mode)):
                custom.chmod(mode)
                with self.assertRaises(PermissionError):
                    ringer.write_dashboard_launcher(custom, 8787, "a" * 43)
                self.assertEqual([], list(custom.iterdir()))
                self.assertEqual(mode, stat.S_IMODE(custom.stat().st_mode))
        custom.chmod(0o755)
        launcher = ringer.write_dashboard_launcher(custom, 8787, "a" * 43)
        self.assertEqual(0o755, stat.S_IMODE(custom.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(launcher.stat().st_mode))
