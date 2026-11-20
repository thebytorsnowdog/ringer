#!/usr/bin/env python3
"""Persistent HUD opener behavior."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ringer  # noqa: E402
from ringer import AppConfig, ArtifactConfig, EvalConfig, ensure_hud_running  # noqa: E402


def config(root: Path) -> AppConfig:
    return AppConfig(
        path=None,
        identity_default=None,
        state_dir=root / "state",
        dashboard_port_base=8787,
        hud_port=8700,
        hud_app_path=None,
        allow_full_access=False,
        eval=EvalConfig(backend="jsonl", jsonl_path=root / "eval.jsonl"),
        engines={},
        artifact=ArtifactConfig(
            enabled=False,
            out_template=str(root / "live.html"),
            report_template=str(root / "report.html"),
            index_out=root / "index.html",
        ),
    )


class HudSingleTabTests(unittest.TestCase):
    def test_already_alive_does_not_open_browser(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            opened: list[str] = []
            original_alive = ringer.hud_is_alive
            original_open = ringer.open_in_browser
            original_popen = ringer.subprocess.Popen
            try:
                ringer.hud_is_alive = lambda _port, **_kwargs: True
                ringer.open_in_browser = opened.append

                def fail_popen(*_args: object, **_kwargs: object) -> object:
                    raise AssertionError("should not spawn a HUD that is already alive")

                ringer.subprocess.Popen = fail_popen
                ensure_hud_running(config(Path(temp)), open_browser=True)
            finally:
                ringer.hud_is_alive = original_alive
                ringer.open_in_browser = original_open
                ringer.subprocess.Popen = original_popen
            self.assertEqual([], opened)

    def test_dead_then_alive_opens_once_after_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            hud_config = config(Path(temp))
            opened: list[str] = []
            spawned: list[object] = []
            alive_results = iter([False, True, True])
            original_alive = ringer.hud_is_alive
            original_open = ringer.open_in_browser
            original_popen = ringer.subprocess.Popen
            original_viewer = ringer.hud_has_recent_viewer
            try:
                ringer.hud_is_alive = lambda _port, **_kwargs: next(alive_results)
                # A live Ringside on this port answers /api/viewer-status. The
                # spawn decision is mocked, so the tab-reuse lookup must be too.
                ringer.hud_has_recent_viewer = lambda _port, **_kwargs: False
                ringer.open_in_browser = opened.append

                def fake_popen(*args: object, **_kwargs: object) -> object:
                    spawned.append(args)
                    ringer.write_hud_token(hud_config.state_dir, "d" * 43)
                    return object()

                ringer.subprocess.Popen = fake_popen
                ensure_hud_running(hud_config, open_browser=True)
            finally:
                ringer.hud_is_alive = original_alive
                ringer.hud_has_recent_viewer = original_viewer
                ringer.open_in_browser = original_open
                ringer.subprocess.Popen = original_popen
            self.assertEqual(1, len(spawned))
            self.assertEqual([(hud_config.state_dir / "browser-8700.html").as_uri()], opened)


if __name__ == "__main__":
    unittest.main(verbosity=2)
