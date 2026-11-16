"""Ringside resources, native CORS boundary, selected-folder routing and JS behavior."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import ringer  # noqa: E402


class RingsideFrontendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env_patch = patch.dict(os.environ, {"RINGER_HOME":str(self.root / "home")})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.server = ringer.PersistentHudServer(self.root, preferred_port=0, open_viewer=False)
        self.base = f"http://127.0.0.1:{self.server.start()}"
        self.addCleanup(self.server.stop)
        self.token = {ringer.HUD_TOKEN_HEADER: self.server.token}

    def status(self, path: str, *, method: str = "GET", headers: dict[str, str] | None = None) -> int:
        try:
            with urlopen(Request(self.base + path, headers=headers or {}, method=method), timeout=5) as response:
                return response.status
        except HTTPError as error:
            error.close()
            return error.code

    def test_token_is_minted_per_launch_and_left_owner_only(self) -> None:
        token_path = ringer.hud_token_path(self.root)
        self.assertEqual(self.server.token, token_path.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(self.server.token), 32)
        if os.name == "posix":
            self.assertEqual(0o600, token_path.stat().st_mode & 0o777)
        with urlopen(self.base + "/", timeout=5) as response:
            self.assertIn(f'<meta name="ringer-token" content="{self.server.token}"', response.read().decode("utf-8"))
        second = ringer.PersistentHudServer(self.root, preferred_port=0, open_viewer=False)
        second.start()
        self.addCleanup(second.stop)
        self.assertNotEqual(self.server.token, second.token)

    def test_api_needs_the_launch_token(self) -> None:
        for path in ["/api/runs", "/api/library", "/api/models", "/api/viewer-status", "/api/viewer-ping"]:
            with self.subTest(path=path):
                self.assertEqual(401, self.status(path))
                self.assertEqual(401, self.status(path, headers={ringer.HUD_TOKEN_HEADER: "wrong"}))
        self.assertEqual(200, self.status("/api/runs", headers=self.token))

    def test_foreign_host_headers_are_refused(self) -> None:
        port = self.base.rsplit(":", 1)[1]
        for host in ["evil.example", f"evil.example:{port}", "127.0.0.1:1", f"127.0.0.1.nip.io:{port}"]:
            with self.subTest(host=host):
                self.assertEqual(403, self.status("/", headers={"Host": host}))
                self.assertEqual(403, self.status("/api/runs", headers={"Host": host, **self.token}))
                self.assertEqual(403, self.status("/artifacts/library.json", headers={"Host": host}))
        self.assertEqual(200, self.status("/", headers={"Host": f"localhost:{port}"}))

    def test_open_folder_is_a_post_from_a_known_origin(self) -> None:
        port = self.base.rsplit(":", 1)[1]
        with patch.object(ringer.sys,"platform","darwin"), patch.object(ringer.subprocess,"Popen") as popen:
            self.assertEqual(405, self.status("/api/open-folder", headers=self.token))
            self.assertEqual(403, self.status("/api/open-folder", method="POST", headers=self.token))
            self.assertEqual(403, self.status("/api/open-folder", method="POST", headers={"Origin": "https://untrusted.example", **self.token}))
            self.assertEqual(401, self.status("/api/open-folder", method="POST", headers={"Origin": f"http://127.0.0.1:{port}"}))
            popen.assert_not_called()
            self.assertEqual(204, self.status("/api/open-folder", method="POST", headers={"Origin": f"http://127.0.0.1:{port}", **self.token}))
            popen.assert_called_once()

    def test_native_preflight_allows_the_token_header_only_for_native_origins(self) -> None:
        with urlopen(Request(self.base + "/api/runs", method="OPTIONS", headers={"Origin": "tauri://localhost"}), timeout=5) as response:
            self.assertEqual(204, response.status)
            self.assertEqual("tauri://localhost", response.headers["Access-Control-Allow-Origin"])
            self.assertIn(ringer.HUD_TOKEN_HEADER, response.headers["Access-Control-Allow-Headers"])
        self.assertEqual(403, self.status("/api/runs", method="OPTIONS", headers={"Origin": "https://untrusted.example"}))

    def test_per_run_dashboard_refuses_foreign_host_headers(self) -> None:
        state_path = self.root / "runs" / "one.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        dashboard = ringer.Dashboard(state_path, 0, open_viewer=False)
        with patch("sys.stdout"):
            port = dashboard.start()
        self.addCleanup(dashboard.stop)
        base = f"http://127.0.0.1:{port}"
        with urlopen(base + "/state.json", timeout=5) as response:
            self.assertEqual(200, response.status)
        with self.assertRaises(HTTPError) as caught:
            urlopen(Request(base + "/state.json", headers={"Host": f"evil.example:{port}"}), timeout=5)
        caught.exception.close()
        self.assertEqual(403, caught.exception.code)

    def test_shared_resources_and_native_origin_are_allowlisted(self) -> None:
        for path, content_type in [("/ringside.js","text/javascript"),("/ringside.css","text/css"),("/hud.js","text/javascript"),("/assets/ringside-mark.svg","image/svg+xml")]:
            with urlopen(self.base+path, timeout=5) as response:
                self.assertTrue(response.headers["Content-Type"].startswith(content_type))
                self.assertGreater(len(response.read()), 20)
        for origin, permitted in [("tauri://localhost",True),("https://tauri.localhost",True),("https://untrusted.example",False)]:
            with urlopen(Request(self.base+"/api/runs", headers={"Origin":origin, **self.token}), timeout=5) as response:
                self.assertEqual(origin if permitted else None, response.headers.get("Access-Control-Allow-Origin"))
                self.assertEqual([], json.loads(response.read())["runs"])
        with self.assertRaises(Exception):
            urlopen(self.base+"/assets/../../ringer.py", timeout=5)

    def test_open_folder_uses_selected_version_run(self) -> None:
        directory = self.root / "artifacts" / "deliverables" / "saved-run"
        directory.mkdir(parents=True)
        with patch.object(ringer.sys,"platform","darwin"), patch.object(ringer.subprocess,"Popen") as popen:
            with urlopen(Request(self.base+"/api/open-folder?artifact=Release&run=saved-run", headers={"Origin":"tauri://localhost", **self.token}, method="POST"), timeout=5) as response:
                self.assertEqual(204,response.status)
                self.assertEqual("tauri://localhost",response.headers.get("Access-Control-Allow-Origin"))
            self.assertEqual(["open",str(directory.resolve())],popen.call_args.args[0])

    @unittest.skipUnless(shutil.which("node"), "Node is optional; run the browser recipe in TESTING.md")
    def test_frontend_behavior_without_dependencies(self) -> None:
        result = subprocess.run(["node",str(ROOT / "tests" / "ringside_frontend_test.js")],capture_output=True,text=True,timeout=15)
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        self.assertIn("PASS:",result.stdout)


if __name__ == "__main__":
    unittest.main()
