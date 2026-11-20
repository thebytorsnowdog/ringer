"""Defensive dashboard checks using temporary state and in-process HTTP handlers."""
from __future__ import annotations

import http.client
import io
import json
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ringer


class MemorySocket:
    def __init__(self, data: bytes) -> None:
        self.input = io.BytesIO(data)
        self.output = io.BytesIO()

    def makefile(self, _mode: str, *_args: object) -> io.BytesIO:
        return self.input

    def sendall(self, data: bytes) -> None:
        self.output.write(data)


class MemoryServer:
    """Keep the bind and HTTP parsing contracts without opening a sandbox socket."""

    def __init__(self, address, handler) -> None:
        self.server_address = address
        self.handler = handler

    def serve_forever(self) -> None:
        pass

    def shutdown(self) -> None:
        pass

    def server_close(self) -> None:
        pass

    def request(self, path, *, method="GET", headers=None):
        host, port = self.server_address
        request_headers = {"Host": f"{host}:{port}", **(headers or {})}
        lines = [f"{method} {path} HTTP/1.1"]
        lines += [f"{key}: {value}" for key, value in request_headers.items()]
        client = MemorySocket(("\r\n".join(lines) + "\r\n\r\n").encode())
        self.handler(client, self.server_address, self)
        with http.client.HTTPResponse(MemorySocket(client.output.getvalue())) as response:
            response.begin()
            return response.status, response.headers, response.read()


class BrowserDashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.state = self.root / "runs" / "saved-run.json"
        self.state.parent.mkdir()
        self.log = self.root / "worker.log"
        self.log.write_text("fixture log\n")
        self.state.write_text(json.dumps({
            "run_id": "saved-run", "run_name": "Fixture Run", "state": "finished",
            "tasks": [{"key": "sample", "log_path": str(self.log)}],
        }))
        self.artifacts = self.root / "artifacts"
        self.deliverables = self.artifacts / "deliverables" / "saved-run" / "sample"
        self.deliverables.mkdir(parents=True)
        self.report = self.artifacts / "versions" / "saved report.html"
        self.report.parent.mkdir()
        self.report.write_text('<h1>Old report</h1><a href="../deliverables/saved-run/sample/sample.html">Sample</a>')
        (self.artifacts / "library.json").write_text(json.dumps({"artifacts": {
            "Fixture Run": {"state": "pass", "live_path": str(self.report),
                            "versions": [{"run_id": "saved-run", "path": str(self.report)}]},
        }}))
        self.enterContext(patch.object(ringer, "ReusableThreadingHTTPServer", MemoryServer))
        self.enterContext(patch.object(ringer, "ThreadingHTTPServer", MemoryServer))
        self.enterContext(patch.object(ringer, "read_active_runs_file", return_value={}))
        self.opener = self.enterContext(patch.object(ringer, "open_in_browser"))

    def start(self, persistent: bool):
        server = (ringer.PersistentHudServer(self.root, preferred_port=8700) if persistent
                  else ringer.Dashboard(self.state, 8787, force_browser=True))
        server.start()
        self.addCleanup(server.stop)
        return server, server.httpd

    def test_owner_launcher_authenticates_both_viewers_without_public_bootstrap(self) -> None:
        for persistent in (True, False):
            with self.subTest(persistent=persistent):
                server, http = self.start(persistent)
                self.assertEqual(("127.0.0.1", server.port), http.server_address)
                launcher = self.root / f"browser-{server.port}.html"
                self.assertEqual(0o600, stat.S_IMODE(launcher.stat().st_mode))
                self.opener.assert_called_with(launcher.as_uri())
                launch = re.search(r"#ringer-session=([a-f0-9]{64})", launcher.read_text())
                self.assertIsNotNone(launch)
                browser_token = launch[1]
                self.assertNotIn(server.token, launcher.read_text())
                for public in ("/", "/view", "/artifact-preview.js"):
                    status, headers, body = http.request(public)
                    self.assertEqual(200, status)
                    self.assertNotIn(server.token.encode(), body)
                    self.assertNotIn(browser_token.encode(), body)
                    self.assertEqual("no-referrer", headers["Referrer-Policy"])
                status, headers, js = http.request("/artifact-preview.js")
                self.assertTrue(headers["Content-Type"].startswith("text/javascript"))
                self.assertIn(b"browserFetch", js)

                run = "/api/runs" if persistent else "/state.json"
                log = ("/logs/saved-run/sample" if persistent else "/logs/sample")
                for path in (run, log, "/artifacts/library.json", "/artifacts/versions/saved%20report.html"):
                    with self.subTest(path=path):
                        self.assertEqual(401, http.request(path)[0])
                        self.assertEqual(401, http.request(path, headers={ringer.HUD_TOKEN_HEADER: "invalid"})[0])
                        for token in (browser_token, server.token):
                            status, headers, body = http.request(path, headers={ringer.HUD_TOKEN_HEADER: token})
                            self.assertEqual(200, status)
                            self.assertEqual("no-store", headers["Cache-Control"])
                        self.assertEqual(403, http.request(path, headers={
                            ringer.HUD_TOKEN_HEADER: browser_token, "Host": "fixture.invalid",
                        })[0])
                self.assertIn(b"Fixture Run", http.request(run, headers={ringer.HUD_TOKEN_HEADER: browser_token})[2])
                self.assertEqual(b"fixture log\n", http.request(log, headers={ringer.HUD_TOKEN_HEADER: browser_token})[2])

    def test_direct_artifacts_are_downloads_or_sandboxed_previews(self) -> None:
        samples = {
            "sample.html": b"<p>HTML fixture</p>",
            "sample.SVG": b'<svg xmlns="http://www.w3.org/2000/svg"/>',
            "sample.xhtml": b"<p>XML fixture</p>",
            "sample.unknown": b"<p>Unrecognized fixture</p>",
            "sample.js": b"/* JavaScript fixture */",
        }
        for name, body in samples.items():
            (self.deliverables / name).write_bytes(body)
        for persistent in (True, False):
            server, http = self.start(persistent)
            session = {ringer.HUD_TOKEN_HEADER: ringer.dashboard_browser_token(server.token)}
            for name, expected in samples.items():
                with self.subTest(persistent=persistent, name=name):
                    path = f"/artifacts/deliverables/saved-run/sample/{name}"
                    self.assertEqual(401, http.request(path)[0])
                    status, headers, body = http.request(path, headers=session)
                    self.assertEqual(200, status)
                    self.assertEqual(expected, body)
                    self.assertEqual("application/octet-stream", headers["Content-Type"])
                    self.assertTrue(headers["Content-Disposition"].startswith("attachment;"))
                    self.assertIn("sandbox", headers["Content-Security-Policy"])
                    self.assertEqual("nosniff", headers["X-Content-Type-Options"])
            status, headers, body = http.request("/artifacts/versions/saved%20report.html", headers=session)
            self.assertEqual(200, status)
            self.assertIn(b"Old report", body)
            self.assertIsNone(headers.get("Content-Disposition"))
            self.assertTrue(headers["Content-Type"].startswith("text/html"))
            policy = headers["Content-Security-Policy"]
            self.assertIn("sandbox", policy)
            self.assertIn("default-src 'none'", policy)
            self.assertNotIn("allow-same-origin", policy)
            self.assertNotIn("allow-scripts", policy)

    def test_startup_protects_existing_default_run_history(self):
        private = self.root / "old-state"
        private.mkdir()
        private.chmod(0o755)
        with patch.object(ringer, "default_state_dir", return_value=private):
            ringer.write_hud_token(private, "d" * 43)
        self.assertEqual(0o700, private.stat().st_mode & 0o777)

    def test_linux_open_folder_requires_session_and_known_origin(self) -> None:
        server, http = self.start(True)
        origin = "http://127.0.0.1:8700"
        session = {ringer.HUD_TOKEN_HEADER: ringer.dashboard_browser_token(server.token)}
        native = {ringer.HUD_TOKEN_HEADER: server.token, "Origin": "tauri://localhost"}
        url = "/api/open-folder?run=saved-run"
        with (patch.object(ringer.sys, "platform", "linux"),
              patch.object(ringer.shutil, "which", return_value="/fixture/xdg-open") as which,
              patch.object(ringer.subprocess, "Popen") as open_folder):
            self.assertTrue(json.loads(http.request("/api/runs", headers=session)[2])["open_folder"])
            self.assertEqual(405, http.request(url, headers=session)[0])
            self.assertEqual(403, http.request(url, method="POST", headers=session)[0])
            self.assertEqual(401, http.request(url, method="POST", headers={"Origin": origin})[0])
            self.assertEqual(403, http.request(url, method="POST", headers={**session, "Origin": "https://fixture.invalid"})[0])
            status, headers, _ = http.request(url, method="OPTIONS", headers={"Origin": native["Origin"]})
            self.assertEqual(204, status)
            self.assertEqual(ringer.HUD_TOKEN_HEADER, headers["Access-Control-Allow-Headers"])
            open_folder.assert_not_called()
            self.assertEqual(204, http.request(url, method="POST", headers={**session, "Origin": origin})[0])
            self.assertEqual(["/fixture/xdg-open", str(self.deliverables.parent)], open_folder.call_args.args[0])
            status, headers, _ = http.request(url, method="POST", headers=native)
            self.assertEqual(204, status)
            self.assertEqual("tauri://localhost", headers["Access-Control-Allow-Origin"])
            which.return_value = None
            self.assertFalse(json.loads(http.request("/api/runs", headers=session)[2])["open_folder"])
            self.assertEqual(501, http.request(url, method="POST", headers=native)[0])

    def test_native_token_is_reused_until_a_new_launch(self) -> None:
        server, http = self.start(True)
        self.assertEqual(server.token, ringer.read_hud_token(self.root))
        first_browser_token = ringer.dashboard_browser_token(server.token)
        second, second_http = self.start(True)
        self.assertNotEqual(server.token, second.token)
        self.assertEqual(second.token, ringer.read_hud_token(self.root))
        self.assertEqual(401, second_http.request("/api/runs", headers={ringer.HUD_TOKEN_HEADER: first_browser_token})[0])
        self.assertEqual(200, second_http.request("/api/runs", headers={ringer.HUD_TOKEN_HEADER: second.token})[0])
        token_path = ringer.hud_token_path(self.root)
        token_path.chmod(0o644)
        self.assertEqual("", ringer.read_hud_token(self.root))

    @unittest.skipUnless(shutil.which("node"), "Node is needed for the frontend unit checks")
    def test_shared_preview_and_session_logic(self) -> None:
        script = Path(__file__).with_name("ringer_browser_frontend_test.js")
        result = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
