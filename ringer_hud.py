"""HTTP serving for Ringside and the per-run dashboard.

Lazy imports of ringer give the handlers access to shared run and model
helpers and keep existing patches to the ringer namespace visible.
"""

from __future__ import annotations

import contextlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


DEFAULT_HUD_PORT = 8700
WORKER_LOG_TAIL_BYTES = 64 * 1024
DASHBOARD_HTML_PATH = Path(__file__).resolve().parent / "dashboard" / "dashboard.html"
RINGSIDE_HTML_PATH = Path(__file__).resolve().parent / "dashboard" / "ringside.html"
ARTIFACT_VIEWER_HTML = b"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Ringer output</title>
<style>body { font: 16px system-ui; } iframe { border: 0; position: fixed; inset: 0;
width: 100%; height: 100%; }</style></head>
<body><main id="artifact-viewer">Loading output...</main>
<script src="/artifact-preview.js"></script></body></html>
"""
MINIMAL_DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>ringer dashboard</title></head>
<body style="font-family: system-ui, sans-serif; background:#080a0f; color:#eef4ff;">
<main id="app">dashboard/dashboard.html is missing</main>
<script>
function update(states) {
  document.getElementById("app").textContent = JSON.stringify(states, null, 2);
}
</script>
</body>
</html>
"""


def read_dashboard_html() -> str:
    import ringer

    try:
        return ringer.DASHBOARD_HTML_PATH.read_text(encoding="utf-8")
    except OSError:
        return MINIMAL_DASHBOARD_HTML


def tail_file_text(path: Path, max_bytes: int) -> str:
    if max_bytes <= 0 or not path.exists():
        return ""
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            data = fh.read()
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")


def artifact_content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".html", ".htm"}:
        return "text/html; charset=utf-8"
    if suffix == ".json":
        return "application/json; charset=utf-8"
    guessed, _encoding = mimetypes.guess_type(str(path))
    if guessed:
        if guessed.startswith("text/"):
            return f"{guessed}; charset=utf-8"
        return guessed
    return "application/octet-stream"


def inject_models_tab_into_ringside_html(html: str) -> str:
    if 'id="models-panel"' in html or 'id="artifacts-panel"' not in html:
        return html
    tabs = """
    <nav class="tabs" id="ringside-tabs" aria-label="Ringside views">
      <button type="button" class="tab" id="runs-tab" aria-selected="true">Runs</button>
      <button type="button" class="tab" id="models-tab" aria-selected="false">Models</button>
    </nav>
"""
    panel = """
      <section id="models-panel" class="panel models-panel" hidden>
        <div id="models-status" class="models-status mono">models not loaded</div>
        <div id="models-table-wrap" class="models-table-wrap">
          <div class="empty">No model results yet. Run './ringer.py models' for the local scoreboard docs.</div>
        </div>
      </section>
"""
    style = """
    .models-panel {
      min-height: calc(100vh - 83px);
      padding: 0 clamp(12px, 2vw, 22px) clamp(20px, 3vw, 30px);
    }
    .models-status {
      padding: 10px 0;
      color: var(--muted);
      font-size: 12px;
      border-bottom: 1px solid var(--hairline);
    }
    .models-status.error { color: var(--fail); }
    .models-table-wrap { overflow: auto; }
    .models-table {
      width: 100%;
      min-width: 1500px;
      border-collapse: collapse;
      font-size: 13px;
    }
    .models-table th,
    .models-table td {
      padding: 11px 10px;
      border-bottom: 1px solid var(--hairline);
      vertical-align: middle;
      text-align: left;
    }
    .models-table th {
      color: var(--muted);
      font-size: 11px;
      font-weight: 700;
      letter-spacing: .06em;
      text-transform: uppercase;
    }
    .models-table .numeric { text-align: right; }
    .model-row { cursor: pointer; }
    .model-row:hover,
    .model-row.expanded { background: var(--surface); }
    .model-name-cell { display: grid; gap: 1px; min-width: 220px; }
    .model-display { color: var(--ink); font-weight: 700; }
    .models-meta {
      color: var(--muted);
      font-size: 12px;
    }
    .model-flag {
      color: var(--muted);
      font-size: 11px;
      text-transform: uppercase;
    }
    .model-notes { min-width: 280px; }
    .tier-badge {
      display: inline-flex;
      align-items: center;
      min-height: 22px;
      padding: 2px 7px;
      border: 1px solid var(--hairline);
      border-radius: 5px;
      color: var(--ink);
      font-size: 11px;
      font-weight: 700;
      text-transform: uppercase;
    }
    .tier-badge.proven {
      border-color: color-mix(in srgb, var(--pass) 48%, var(--hairline));
      color: var(--pass);
    }
    .tier-badge.probation {
      border-color: color-mix(in srgb, var(--accent) 48%, var(--hairline));
      color: var(--accent);
    }
    .model-breakdown td {
      padding: 0;
      background: color-mix(in srgb, var(--surface) 72%, transparent);
    }
    .breakdown-grid {
      display: grid;
      grid-template-columns: minmax(120px, 1fr) repeat(5, minmax(70px, auto));
      gap: 0;
      padding: 8px 10px 10px 46px;
      color: var(--muted);
      font-size: 12px;
    }
    .breakdown-grid > div {
      padding: 5px 8px;
      border-bottom: 1px solid var(--hairline);
      min-width: 0;
    }
    .breakdown-head {
      font-size: 10px;
      font-weight: 700;
      letter-spacing: .06em;
      text-transform: uppercase;
    }
    @media (max-width: 760px) {
      .breakdown-grid {
        grid-template-columns: minmax(110px, 1fr) repeat(2, minmax(64px, auto));
        padding-left: 10px;
      }
      .breakdown-grid .optional { display: none; }
    }
"""
    script = r"""
    function installModelsView() {
      const MODELS_REFRESH_MS = 30000;
      const VIEW_KEY = "ringside-view";
      const runsPanel = document.getElementById("artifacts-panel");
      const modelsPanel = document.getElementById("models-panel");
      const runsTab = document.getElementById("runs-tab");
      const modelsTab = document.getElementById("models-tab");
      const status = document.getElementById("models-status");
      const wrap = document.getElementById("models-table-wrap");
      if (!runsPanel || !modelsPanel || !runsTab || !modelsTab || !status || !wrap) return;
      let payload = null;
      let expandedModel = null;
      let lastFetch = 0;
      let inFlight = false;
      let activeView = "runs";

      function html(value) {
        return String(value ?? "")
          .replace(/&/g, "&amp;")
          .replace(/</g, "&lt;")
          .replace(/>/g, "&gt;")
          .replace(/"/g, "&quot;")
          .replace(/'/g, "&#39;");
      }

      function numberOrZeroLocal(value) {
        const number = Number(value);
        return Number.isFinite(number) ? number : 0;
      }

      function percent(value) {
        const number = Number(value);
        return Number.isFinite(number) ? `${Math.round(number * 100)}%` : "0%";
      }

      function modelDuration(value) {
        if (value === null || value === undefined || value === "") return "";
        const total = Math.max(0, Math.round(numberOrZeroLocal(value) / 1000));
        const hours = Math.floor(total / 3600);
        const minutes = Math.floor((total % 3600) / 60);
        const seconds = total % 60;
        if (hours) return `${hours}h${String(minutes).padStart(2, "0")}m${String(seconds).padStart(2, "0")}s`;
        if (minutes) return `${minutes}m${String(seconds).padStart(2, "0")}s`;
        return `${seconds}s`;
      }

      function modelDate(value) {
        const text = String(value || "").trim();
        if (!text) return "unknown";
        const match = text.match(/^(\d{4})-(\d{2})-(\d{2})/);
        if (match) {
          const date = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
          return date.toLocaleDateString("en-US", {month: "long", day: "numeric", year: "numeric"});
        }
        const stamp = Date.parse(text);
        return Number.isFinite(stamp)
          ? new Date(stamp).toLocaleDateString("en-US", {month: "long", day: "numeric", year: "numeric"})
          : text;
      }

      function safeClass(value) {
        return String(value || "unknown").toLowerCase().replace(/[^a-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "") || "unknown";
      }

      function groupsFor(bucketId) {
        const groups = Array.isArray(payload?.groups) ? payload.groups : [];
        return groups.filter(group => String(group?.display_bucket_id || "") === bucketId);
      }

      function breakdown(bucketId) {
        const groups = groupsFor(bucketId);
        if (!groups.length) return '<div class="empty">No per-task breakdown recorded for this model.</div>';
        const cells = [
          '<div class="breakdown-head">Task type</div>',
          '<div class="breakdown-head">Tasks</div>',
          '<div class="breakdown-head">First</div>',
          '<div class="breakdown-head optional">Pass</div>',
          '<div class="breakdown-head optional">Attempts</div>',
          '<div class="breakdown-head optional">Last used</div>',
        ];
        groups.forEach(group => {
          cells.push(
            `<div>${html(group.task_type || "(untyped)")}</div>`,
            `<div>${numberOrZeroLocal(group.tasks).toLocaleString()}</div>`,
            `<div>${html(percent(group.first_try_pass_rate))}</div>`,
            `<div class="optional">${html(percent(group.pass_rate))}</div>`,
            `<div class="optional">${numberOrZeroLocal(group.attempts).toLocaleString()}</div>`,
            `<div class="optional">${html(modelDate(group.last_seen))}</div>`,
          );
        });
        return `<div class="breakdown-grid mono">${cells.join("")}</div>`;
      }

      function renderModels() {
        const rows = Array.isArray(payload?.rollup) ? payload.rollup : [];
        const error = String(payload?.error || "").trim();
        status.classList.toggle("error", Boolean(error));
        status.textContent = error ? `models unavailable: ${error}` : `updated ${modelDate(payload?.generated_at)}`;
        if (!rows.length) {
          wrap.innerHTML = '<div class="empty">No model results yet. Run \'./ringer.py models\' for the local scoreboard docs.</div>';
          return;
        }
        const body = [];
        rows.forEach((row, index) => {
          const bucketId = String(row.display_bucket_id || `bucket-${index}`);
          const expanded = expandedModel === bucketId;
          const tierClass = safeClass(row.tier);
          const marker = row.misrouted ? "misrouted" : (row.unregistered ? "unregistered" : "");
          const tier = row.unattributed || row.misrouted ? "not ranked" : (row.tier || "unknown");
          const notes = Array.isArray(row.notes) ? row.notes.join("\n\n") : "";
          body.push(
            `<tr class="model-row${expanded ? " expanded" : ""}" data-model="${html(bucketId)}" tabindex="0">`,
            '<td><span class="model-name-cell">',
            `<span class="model-display">${html(row.model_display || row.model || "unknown")}</span>`,
            marker ? `<span class="model-flag">${html(marker)}</span>` : "",
            '</span></td>',
            `<td>${html(row.lab || "(unknown)")}</td>`,
            `<td>${html(row.harness || "unknown")}</td>`,
            `<td>${html(row.access || "unknown")}</td>`,
            `<td><span class="tier-badge ${html(tierClass)}">${html(tier)}</span></td>`,
            `<td class="numeric">${numberOrZeroLocal(row.tasks).toLocaleString()}</td>`,
            `<td class="numeric">${html(percent(row.first_try_pass_rate))}</td>`,
            `<td class="numeric">${html(percent(row.pass_rate))}</td>`,
            `<td class="numeric">${row.median_tokens === null || row.median_tokens === undefined ? "" : numberOrZeroLocal(row.median_tokens).toLocaleString()}</td>`,
            `<td>${html(modelDuration(row.median_duration_ms))}</td>`,
            `<td>${html(modelDate(row.last_seen))}</td>`,
            `<td class="model-notes" title="${html(notes)}">${html(row.latest_note || "")}</td>`,
            '</tr>',
          );
          if (expanded) body.push(`<tr class="model-breakdown"><td colspan="12">${breakdown(bucketId)}</td></tr>`);
        });
        wrap.innerHTML = [
          '<table class="models-table">',
          '<thead><tr>',
          '<th>Model</th><th>Lab</th><th>Harness</th><th>API/Plan</th><th>Tier</th>',
          '<th class="numeric">Tasks</th><th class="numeric">First try</th><th class="numeric">Pass</th>',
          '<th class="numeric">Tokens (median)</th><th>Speed (median)</th><th>Last used</th><th>Notes</th>',
          '</tr></thead>',
          `<tbody>${body.join("")}</tbody>`,
          '</table>',
        ].join("");
      }

      async function fetchModels(force) {
        const now = Date.now();
        if (inFlight || (!force && lastFetch && now - lastFetch < MODELS_REFRESH_MS)) return;
        inFlight = true;
        status.textContent = payload ? "refreshing models..." : "loading models...";
        try {
          const response = await fetch(`/api/models?t=${Date.now()}`, {cache: "no-store"});
          payload = await response.json();
          lastFetch = Date.now();
        } catch (error) {
          payload = {generated_at: new Date().toISOString(), groups: [], rollup: [], error: error?.message || "models unavailable"};
        } finally {
          inFlight = false;
          renderModels();
        }
      }

      function selectView(view, persist = true) {
        activeView = view === "models" ? "models" : "runs";
        runsPanel.hidden = activeView === "models";
        modelsPanel.hidden = activeView !== "models";
        runsTab.setAttribute("aria-selected", String(activeView === "runs"));
        modelsTab.setAttribute("aria-selected", String(activeView === "models"));
        if (persist) localStorage.setItem(VIEW_KEY, activeView);
        if (activeView === "models") fetchModels(true);
      }

      runsTab.addEventListener("click", () => selectView("runs"));
      modelsTab.addEventListener("click", () => selectView("models"));
      wrap.addEventListener("click", event => {
        const row = event.target.closest(".model-row");
        if (!row) return;
        const model = row.getAttribute("data-model") || "";
        expandedModel = expandedModel === model ? null : model;
        renderModels();
      });
      wrap.addEventListener("keydown", event => {
        if (event.key !== "Enter" && event.key !== " ") return;
        const row = event.target.closest(".model-row");
        if (!row) return;
        event.preventDefault();
        const model = row.getAttribute("data-model") || "";
        expandedModel = expandedModel === model ? null : model;
        renderModels();
      });
      setInterval(() => {
        if (activeView === "models") fetchModels(false);
      }, MODELS_REFRESH_MS);
      selectView(localStorage.getItem(VIEW_KEY) === "models" ? "models" : "runs", false);
    }

"""
    html = html.replace("    main {\n", style + "    main {\n", 1)
    html = html.replace("    <main>\n", tabs + "\n    <main>\n", 1)
    html = html.replace("    </main>\n", panel + "    </main>\n", 1)
    html = html.replace("    tickClock();\n", script + "    installModelsView();\n    tickClock();\n", 1)
    return html


def read_ringside_html() -> str:
    import ringer

    try:
        return inject_models_tab_into_ringside_html(ringer.RINGSIDE_HTML_PATH.read_text(encoding="utf-8"))
    except OSError:
        return """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Ringside</title></head>
<body><main id="app">dashboard/ringside.html is missing</main></body>
</html>
"""


def send_response_body(
    handler: BaseHTTPRequestHandler,
    status: HTTPStatus,
    body: bytes,
    *,
    content_type: str,
    no_store: bool = False,
    headers: dict[str, str] | None = None,
) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.send_header("X-Content-Type-Options", "nosniff")
    # The native webview reads only this local HUD. Never expose it to arbitrary sites.
    origin = native_hud_origin(handler)
    if origin is not None:
        handler.send_header("Access-Control-Allow-Origin", origin)
        handler.send_header("Vary", "Origin")
    if no_store:
        handler.send_header("Cache-Control", "no-store")
    for name, value in (headers or {}).items():
        handler.send_header(name, value)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def send_json_response(handler: BaseHTTPRequestHandler, data: dict[str, Any]) -> None:
    body = json.dumps(data, sort_keys=True).encode("utf-8")
    send_response_body(
        handler,
        HTTPStatus.OK,
        body,
        content_type="application/json; charset=utf-8",
        no_store=True,
    )


# Loopback and Host checks constrain the listener. A launch credential also
# authenticates private routes, including logs and direct artifact navigation.
HUD_TOKEN_HEADER = "X-Ringer-Token"
HUD_TOKEN_FILE_NAME = "hud.token"
NATIVE_HUD_ORIGINS = frozenset({"tauri://localhost", "http://tauri.localhost", "https://tauri.localhost"})


def hud_token_path(state_dir: Path) -> Path:
    return state_dir / HUD_TOKEN_FILE_NAME


def write_hud_token(state_dir: Path, token: str) -> None:
    """Leave this launch's token where the CLI and the native shell can read it (owner-only)."""
    write_owner_file(hud_token_path(state_dir), token)


def prepare_hud_directory(directory: Path) -> None:
    import ringer

    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    ringer.protect_default_state(directory)
    parent = directory.stat()
    if parent.st_uid != os.geteuid() or parent.st_mode & 0o022:
        raise PermissionError(f"dashboard state directory must be writable only by its owner: {directory}")


def write_owner_file(path: Path, content: str) -> None:
    prepare_hud_directory(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_hud_token(state_dir: Path | None) -> str:
    if state_dir is None:
        return ""
    try:
        fd = os.open(hud_token_path(state_dir), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, encoding="utf-8") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                return ""
            token = handle.read(128).strip()
            return token if re.fullmatch(r"[A-Za-z0-9_-]{43}", token) else ""
    except (OSError, UnicodeError):
        return ""


def loopback_host_allowed(handler: BaseHTTPRequestHandler, port: int | None) -> bool:
    host = handler.headers.get("Host", "")
    return port is not None and host in {f"127.0.0.1:{port}", f"localhost:{port}"}


def native_hud_origin(handler: BaseHTTPRequestHandler) -> str | None:
    requested = handler.headers.get("Origin", "")
    return next((allowed for allowed in NATIVE_HUD_ORIGINS if allowed == requested), None)


def hud_origin_allowed(handler: BaseHTTPRequestHandler, port: int | None) -> bool:
    origin = handler.headers.get("Origin", "")
    if origin in NATIVE_HUD_ORIGINS:
        return True
    return port is not None and origin in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}


def dashboard_browser_token(token: str) -> str:
    return hmac.digest(token.encode("ascii"), b"ringer-browser", "sha256").hex()


def write_dashboard_launcher(state_dir: Path, port: int, token: str) -> Path:
    url = f"http://127.0.0.1:{port}/#ringer-session={dashboard_browser_token(token)}"
    # The browser command line contains only the launcher path. Its contents
    # are readable by the owner even when the process list is shared.
    launcher = state_dir / f"browser-{port}.html"
    content = (
        '<!doctype html><meta charset="utf-8"><meta name="referrer" content="no-referrer">'
        f'<meta http-equiv="refresh" content="0;url={url}"><title>Ringer</title>'
        f'<a href="{url}">Open the Ringer dashboard</a>'
    )
    write_owner_file(launcher, content)
    return launcher.resolve()


def open_dashboard_browser(state_dir: Path, port: int, token: str) -> None:
    import ringer

    if token:
        ringer.open_in_browser(write_dashboard_launcher(state_dir, port, token).as_uri())


def dashboard_private_request(handler: BaseHTTPRequestHandler, token: str) -> bool:
    supplied = handler.headers.get(HUD_TOKEN_HEADER, "").encode("utf-8")
    if token and (
        hmac.compare_digest(supplied, token.encode("ascii"))
        or hmac.compare_digest(supplied, dashboard_browser_token(token).encode("ascii"))
    ):
        return True
    handler.send_error(HTTPStatus.UNAUTHORIZED, "launch Ringer to open a private dashboard session")
    return False


def serve_artifact_frontend(handler: BaseHTTPRequestHandler, path: str) -> bool:
    import ringer

    if path == "/view":
        send_dashboard_page(handler, ARTIFACT_VIEWER_HTML)
        return True
    if path == "/artifact-preview.js":
        try:
            body = ringer.RINGSIDE_HTML_PATH.with_name("artifact-preview.js").read_bytes()
        except OSError:
            handler.send_error(HTTPStatus.NOT_FOUND)
            return True
        send_response_body(
            handler, HTTPStatus.OK, body, content_type="text/javascript; charset=utf-8", no_store=True,
        )
        return True
    return False


def send_dashboard_page(handler: BaseHTTPRequestHandler, body: bytes) -> None:
    send_response_body(
        handler, HTTPStatus.OK, body, content_type="text/html; charset=utf-8",
        no_store=True, headers={"X-Frame-Options": "DENY"},
    )


def read_json_object(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError, UnicodeDecodeError):
        return default
    return data if isinstance(data, dict) else default


def scan_hud_run_states(state_dir: Path, *, limit: int = 12) -> list[dict[str, Any]]:
    runs_dir = state_dir / "runs"
    try:
        paths = [path for path in runs_dir.glob("*.json") if path.is_file()]
    except OSError:
        return []

    def path_mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    paths.sort(key=path_mtime, reverse=True)
    runs: list[dict[str, Any]] = []
    for path in paths[:limit]:
        data = read_json_object(path, {})
        if data:
            runs.append(data)
    return runs


def read_active_runs_file() -> dict[str, Any]:
    import ringer

    return read_json_object(ringer.active_runs_path(), {})


def resolve_artifact_http_path(artifact_root: Path, request_path: str) -> Path | None:
    if request_path == "/artifacts/library.json":
        relative = "library.json"
    elif request_path.startswith("/artifacts/"):
        relative = request_path[len("/artifacts/") :]
    else:
        return None
    if not relative:
        return None
    decoded = urllib.parse.unquote(relative)
    try:
        root = artifact_root.resolve()
        candidate = (root / decoded).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if candidate == root or root not in candidate.parents:
        return None
    return candidate


def task_log_path_from_state(state_path: Path, task_key: str) -> Path | None:
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    tasks = data.get("tasks")
    if not isinstance(tasks, list):
        return None
    for task in tasks:
        if not isinstance(task, dict) or task.get("key") != task_key:
            continue
        log_path = task.get("log_path")
        if isinstance(log_path, str) and log_path:
            return Path(log_path)
    return None


def run_state_path_for_id(state_dir: Path, run_id: str) -> Path | None:
    if not run_id:
        return None
    runs_root = (state_dir / "runs").resolve()
    candidate = (runs_root / f"{run_id}.json").resolve()
    if candidate.parent != runs_root:
        return None
    return candidate


def hud_task_log_path(state_dir: Path, run_id: str, task_key: str) -> Path | None:
    state_path = run_state_path_for_id(state_dir, run_id)
    if state_path is None:
        return None
    state = read_json_object(state_path, {})
    tasks = state.get("tasks")
    if not isinstance(tasks, list):
        return None
    for task in tasks:
        if not isinstance(task, dict) or task.get("key") != task_key:
            continue
        log_path = task.get("log_path")
        if isinstance(log_path, str) and log_path:
            return Path(log_path).expanduser()
        taskdir = task.get("taskdir")
        if isinstance(taskdir, str) and taskdir:
            return Path(taskdir).expanduser() / "worker.log"
    return None


def serve_artifact_path(handler: BaseHTTPRequestHandler, artifact_root: Path, path: str) -> bool:
    import ringer

    artifact_path = resolve_artifact_http_path(artifact_root, path)
    if artifact_path is None:
        return False
    try:
        if not artifact_path.is_file():
            raise FileNotFoundError
        body = artifact_path.read_bytes()
    except (FileNotFoundError, OSError):
        handler.send_error(HTTPStatus.NOT_FOUND)
        return True
    relative = artifact_path.relative_to(artifact_root.resolve())
    # Collected files are worker-controlled, including SVG, PDF and markup
    # with unusual extensions. Generated reports and legacy wrappers are
    # static previews, with sandboxing enforced even on direct navigation.
    is_preview = (
        relative == Path("library.json")
        or (relative.parts[0] != "deliverables" and ringer.is_html_artifact(artifact_path))
        or artifact_path.suffix.lower() in ringer.IMAGE_DELIVERABLE_SUFFIXES - {".svg"}
    )
    headers = {
        "Content-Security-Policy": (
            "sandbox allow-downloads; default-src 'none'; style-src 'unsafe-inline'; "
            "img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'self'"
        ),
    }
    if not is_preview:
        name = urllib.parse.quote(artifact_path.name, safe="")
        headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{name}"
    send_response_body(
        handler,
        HTTPStatus.OK,
        body,
        content_type=artifact_content_type(artifact_path) if is_preview else "application/octet-stream",
        no_store=True,
        headers=headers,
    )
    return True


def folder_opener() -> list[str]:
    if sys.platform == "darwin":
        opener = shutil.which("open")
        return [opener] if opener else []
    if sys.platform.startswith("linux"):
        if opener := shutil.which("xdg-open"):
            return [opener]
        if opener := shutil.which("gio"):
            return [opener, "open"]
    return []


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


class PersistentHudServer:
    def __init__(
        self,
        state_dir: Path,
        preferred_port: int = DEFAULT_HUD_PORT,
        *,
        open_viewer: bool = True,
    ) -> None:
        self.state_dir = state_dir
        self.preferred_port = preferred_port
        self.open_viewer = open_viewer
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.port: int | None = None
        self.model_log_path: Path | None = None
        self.default_model_log_path: Path = state_dir / "runs.jsonl"
        self.model_db_path: Path | None = None
        self.model_notes_path: Path | None = None
        self.update_status: dict[str, Any] | None = None
        self.last_viewer_ping: float | None = None
        self.token: str = ""

    def start(self) -> int:
        import ringer

        state_dir = self.state_dir
        artifact_root = ringer.artifacts_dir(state_dir)
        preferred_port = self.preferred_port
        server_ref = self
        token = secrets.token_urlsafe(32)

        class Handler(BaseHTTPRequestHandler):
            def do_OPTIONS(self) -> None:  # noqa: N802
                # CORS preflight for the native shell's token header and POST.
                origin = native_hud_origin(self)
                if not loopback_host_allowed(self, server_ref.port) or origin is None:
                    self.send_error(HTTPStatus.FORBIDDEN)
                    return
                self.send_response(HTTPStatus.NO_CONTENT)
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Access-Control-Allow-Methods", "GET, POST")
                self.send_header("Access-Control-Allow-Headers", HUD_TOKEN_HEADER)
                self.send_header("Access-Control-Max-Age", "600")
                self.send_header("Vary", "Origin")
                self.end_headers()

            def do_POST(self) -> None:  # noqa: N802
                if not loopback_host_allowed(self, server_ref.port):
                    self.send_error(HTTPStatus.FORBIDDEN, "unexpected Host header")
                    return
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path != "/api/open-folder":
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                if not hud_origin_allowed(self, server_ref.port):
                    self.send_error(HTTPStatus.FORBIDDEN, "unexpected Origin")
                    return
                if not dashboard_private_request(self, server_ref.token):
                    return
                self.open_folder(parsed.query)

            def open_folder(self, query: str) -> None:
                params = urllib.parse.parse_qs(query)
                run_id = (params.get("run") or [""])[0]
                artifact_root_dir = (state_dir / "artifacts").resolve()
                target = artifact_root_dir / "deliverables"
                if run_id:
                    target = target / ringer.sanitize_artifact_name(run_id)
                try:
                    resolved = target.resolve()
                    if resolved != artifact_root_dir and artifact_root_dir not in resolved.parents:
                        self.send_error(HTTPStatus.NOT_FOUND)
                        return
                    if not resolved.is_dir():
                        resolved = artifact_root_dir
                    opener = ringer.folder_opener()
                    if not opener:
                        self.send_error(HTTPStatus.NOT_IMPLEMENTED)
                        return
                    subprocess.Popen(
                        [*opener, str(resolved)], stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                    send_response_body(
                        self, HTTPStatus.NO_CONTENT, b"", content_type="text/plain; charset=utf-8", no_store=True,
                    )
                except (OSError, RuntimeError):
                    self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR)

            def do_GET(self) -> None:  # noqa: N802
                if not loopback_host_allowed(self, server_ref.port):
                    self.send_error(HTTPStatus.FORBIDDEN, "unexpected Host header")
                    return
                path = urllib.parse.urlparse(self.path).path
                if path == "/":
                    send_dashboard_page(self, read_ringside_html().encode("utf-8"))
                    return
                if serve_artifact_frontend(self, path):
                    return
                if path.startswith(("/api/", "/artifacts/", "/logs/")) and not dashboard_private_request(
                    self, server_ref.token,
                ):
                    return
                if path == "/api/viewer-ping":
                    server_ref.last_viewer_ping = time.monotonic()
                    self.send_response(HTTPStatus.NO_CONTENT)
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    return
                if path == "/api/viewer-status":
                    last_ping = server_ref.last_viewer_ping
                    send_json_response(self, {
                        "seconds_since_viewer": None if last_ping is None else time.monotonic() - last_ping,
                    })
                    return
                frontend_assets = {
                    "/ringside.css": (ringer.RINGSIDE_HTML_PATH.with_name("ringside.css"), "text/css; charset=utf-8"),
                    "/ringside.js": (ringer.RINGSIDE_HTML_PATH.with_name("ringside.js"), "text/javascript; charset=utf-8"),
                    "/hud.js": (Path(__file__).resolve().parent / "hud" / "frontend" / "hud.js", "text/javascript; charset=utf-8"),
                    "/assets/ringside-mark.svg": (ringer.RINGSIDE_HTML_PATH.parent / "assets" / "ringside-mark.svg", "image/svg+xml"),
                    "/assets/ringside-live.svg": (ringer.RINGSIDE_HTML_PATH.parent / "assets" / "ringside-live.svg", "image/svg+xml"),
                    "/assets/ringside-attention.svg": (ringer.RINGSIDE_HTML_PATH.parent / "assets" / "ringside-attention.svg", "image/svg+xml"),
                }
                if path in frontend_assets:
                    asset_path, content_type = frontend_assets[path]
                    try:
                        body = asset_path.read_bytes()
                    except OSError:
                        self.send_error(HTTPStatus.NOT_FOUND)
                        return
                    send_response_body(self, HTTPStatus.OK, body, content_type=content_type, no_store=True)
                    return
                if path == "/api/runs":
                    send_json_response(
                        self,
                        {
                            "runs": scan_hud_run_states(state_dir),
                            "active": ringer.read_active_runs_file(),
                            "update": server_ref.update_status,
                            "open_folder": bool(ringer.folder_opener()),
                        },
                    )
                    return
                if path == "/api/models":
                    try:
                        payload = ringer.build_models_api_payload(
                            log_path=server_ref.model_log_path or (state_dir / "runs.jsonl"),
                            default_log_path=server_ref.default_model_log_path,
                            db_path=server_ref.model_db_path,
                            notes_path=server_ref.model_notes_path,
                        )
                    except Exception as exc:
                        payload = {
                            "generated_at": ringer.utc_now_iso(),
                            "columns": list(ringer.MODEL_SCOREBOARD_COLUMNS),
                            "groups": [],
                            "rollup": [],
                            "error": str(exc) or exc.__class__.__name__,
                        }
                    send_json_response(self, payload)
                    return
                if path == "/api/open-folder":
                    # A side effect must not ride on a GET any page can fire blindly.
                    self.send_error(HTTPStatus.METHOD_NOT_ALLOWED)
                    return
                if path == "/api/library":
                    # A run that died without cleanup must not sit "live"
                    # forever in the rail — reconcile against real pids on read.
                    with contextlib.suppress(Exception):
                        ringer.reconcile_artifact_library_dead_runs(state_dir)
                    send_json_response(
                        self,
                        read_json_object(ringer.artifact_library_path(state_dir), {"artifacts": {}}),
                    )
                    return
                if path.startswith("/artifacts/"):
                    if not serve_artifact_path(self, artifact_root, path):
                        self.send_error(HTTPStatus.NOT_FOUND)
                    return
                if path.startswith("/logs/"):
                    relative = path[len("/logs/") :]
                    if "/" not in relative:
                        self.send_error(HTTPStatus.NOT_FOUND)
                        return
                    run_id_raw, task_key_raw = relative.split("/", 1)
                    run_id = urllib.parse.unquote(run_id_raw)
                    task_key = urllib.parse.unquote(task_key_raw)
                    log_path = hud_task_log_path(state_dir, run_id, task_key)
                    if log_path is None or not log_path.is_file():
                        self.send_error(HTTPStatus.NOT_FOUND)
                        return
                    body = tail_file_text(log_path, max_bytes=WORKER_LOG_TAIL_BYTES).encode("utf-8")
                    send_response_body(
                        self,
                        HTTPStatus.OK,
                        body,
                        content_type="text/plain; charset=utf-8",
                        no_store=True,
                    )
                    return
                self.send_error(HTTPStatus.NOT_FOUND)

            def log_message(self, _format: str, *_args: Any) -> None:
                return

        try:
            self.httpd = ringer.ReusableThreadingHTTPServer(("127.0.0.1", preferred_port), Handler)
        except OSError as exc:
            raise RuntimeError(
                f"could not start Ringside on 127.0.0.1:{preferred_port}; "
                "that port is already in use. Use --port to choose another port."
            ) from exc
        self.port = int(self.httpd.server_address[1])
        # Written only after the bind succeeds, so a failed second launch
        # cannot replace the token a running HUD is checking.
        try:
            write_hud_token(state_dir, token)
        except OSError:
            self.httpd.server_close()
            self.httpd = None
            self.port = None
            raise
        self.token = token
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="ringer-hud", daemon=True)
        self.thread.start()
        url = f"http://127.0.0.1:{self.port}"
        if self.open_viewer:
            try:
                open_dashboard_browser(state_dir, self.port, token)
            except OSError:
                self.stop()
                raise
        print(f"Ringside: {url}", flush=True)
        return self.port

    def start_background(self) -> int:
        return self.start()

    def stop(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
        if self.thread is not None:
            self.thread.join(timeout=2)


class Dashboard:
    def __init__(
        self,
        state_path: Path,
        preferred_port: int,
        hud_app_path: Path | None = None,
        force_browser: bool = False,
        open_viewer: bool = True,
    ) -> None:
        self.state_path = state_path
        self.preferred_port = preferred_port
        self.hud_app_path = hud_app_path
        self.force_browser = force_browser
        self.open_viewer = open_viewer
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.port: int | None = None
        self.token = ""

    def start(self) -> int:
        import ringer

        state_path = self.state_path
        artifact_root = state_path.parent.parent / "artifacts"
        dashboard_ref = self
        self.token = secrets.token_urlsafe(32)

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if not loopback_host_allowed(self, dashboard_ref.port):
                    self.send_error(HTTPStatus.FORBIDDEN, "unexpected Host header")
                    return
                path = urllib.parse.urlparse(self.path).path
                if path == "/":
                    send_dashboard_page(self, read_dashboard_html().encode("utf-8"))
                    return
                if serve_artifact_frontend(self, path):
                    return
                if not dashboard_private_request(self, dashboard_ref.token):
                    return
                if path == "/state.json":
                    try:
                        body = state_path.read_bytes()
                    except FileNotFoundError:
                        body = b'{"run_name":"ringer","identity":"unknown","started_at":"","port":null,"dashboard_port":null,"tasks":[],"totals":{"running":0,"done":0,"pass":0,"fail":0,"tokens":0}}'
                    send_response_body(
                        self,
                        HTTPStatus.OK,
                        body,
                        content_type="application/json; charset=utf-8",
                        no_store=True,
                    )
                    return
                if path.startswith("/logs/"):
                    task_key = urllib.parse.unquote(path[len("/logs/") :])
                    log_path = task_log_path_from_state(state_path, task_key)
                    if log_path is None:
                        self.send_error(HTTPStatus.NOT_FOUND)
                        return
                    body = tail_file_text(log_path, max_bytes=WORKER_LOG_TAIL_BYTES).encode("utf-8")
                    send_response_body(
                        self,
                        HTTPStatus.OK,
                        body,
                        content_type="text/plain; charset=utf-8",
                        no_store=True,
                    )
                    return
                if serve_artifact_path(self, artifact_root, path):
                    return
                self.send_error(HTTPStatus.NOT_FOUND)

            def log_message(self, _format: str, *_args: Any) -> None:
                return

        last_error: OSError | None = None
        for port in range(self.preferred_port, self.preferred_port + 50):
            try:
                self.httpd = ringer.ThreadingHTTPServer(("127.0.0.1", port), Handler)
            except OSError as exc:
                last_error = exc
                continue
            self.port = int(self.httpd.server_address[1])
            break
        if self.httpd is None or self.port is None:
            raise RuntimeError(f"could not start dashboard: {last_error}")
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="ringer-dashboard", daemon=True)
        self.thread.start()
        url = f"http://127.0.0.1:{self.port}"
        # Browser-first: the persistent hud (ensure_hud_running, called from the
        # run path) is what the human watches. Only --browser opens this
        # per-run page directly; the parked Tauri app is never auto-launched.
        if self.open_viewer and self.force_browser:
            try:
                open_dashboard_browser(state_path.parent.parent, self.port, self.token)
            except OSError:
                self.stop()
                raise
        # The persistent hud (:8700) is the one watch surface; this per-run
        # server is an internal state/log feed. Only advertise it when the
        # user explicitly chose the per-run page with --browser.
        if self.force_browser:
            print(f"Dashboard: {url}", flush=True)
        return self.port

    def stop(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
        if self.thread is not None:
            self.thread.join(timeout=2)
