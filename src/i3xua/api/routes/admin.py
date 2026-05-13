"""Admin / ops endpoints: /info, /healthz, /admin/refresh, /admin/openapi/rebuild,
/admin/config, /admin/state."""

from __future__ import annotations

import asyncio
import datetime as dt
import sys
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

from i3xua import __version__
from i3xua.api.deps import get_state, require_auth
from i3xua.api.state import AppState

router = APIRouter()


# ---------------------------------------------------------------- config redaction


_SECRET_KEYS = frozenset({"password", "tokens"})


def _redact(obj: Any) -> Any:
    """Replace secrets (passwords, bearer tokens) with '***' so /admin/config
    can be shown safely in the browser. Tokens live under server.auth.tokens;
    passwords live under server.auth.users[*].password and
    connections[*].auth.password."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if k == "tokens" and isinstance(v, list):
                out[k] = ["***"] * len(v)
            elif k == "password":
                out[k] = "***"
            else:
                out[k] = _redact(v)
        return out
    if isinstance(obj, list):
        return [_redact(x) for x in obj]
    return obj


def _repo_root(start: Path | None = None) -> Path | None:
    """Walk up from `start` (default: this module's location) until we find
    a directory containing pyproject.toml. Returns the repo root, or None
    if no pyproject.toml is found before hitting the filesystem root.

    Used by /admin/andon/regenerate to set the subprocess cwd. Returning
    None means the source tree isn't available (e.g. wheel install with
    no source), and the route surfaces that as a 503.
    """
    here = Path(start) if start is not None else Path(__file__).resolve()
    if here.is_file():
        here = here.parent
    for candidate in [here, *here.parents]:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return None


async def _run_andon_subprocess(repo_root: Path) -> dict[str, Any]:
    """Run tools/andon_report.py from `repo_root` and return a result dict.

    Synchronous from the caller's perspective — awaits the subprocess
    completion. Captures stdout/stderr; the script writes
    andon-report.html to repo_root as a side effect.
    """
    started = dt.datetime.now(dt.UTC)
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "tools/andon_report.py",
        cwd=str(repo_root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=120.0)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    duration_s = (dt.datetime.now(dt.UTC) - started).total_seconds()
    return {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "duration_s": round(duration_s, 2),
        "generated_at": started.isoformat(),
        "stderr_tail": stderr_bytes.decode("utf-8", "replace")[-2000:]
        if proc.returncode != 0
        else "",
    }


@router.get("/info")
async def info() -> dict[str, object]:
    """v1 version-detection endpoint probed by i3X-Explorer.

    Returns the `ServerInfo` shape from the reference server: specVersion,
    serverVersion, serverName, and capabilities (query/update/subscribe).
    """
    from i3xua.i3x.types import ServerInfo

    return ServerInfo(
        specVersion="1.0",
        serverVersion=__version__,
        serverName="i3xua",
    ).model_dump(by_alias=True)


@router.get("/healthz")
async def healthz(state: Annotated[AppState, Depends(get_state)]) -> dict[str, object]:
    return {
        "status": "ok",
        "version": __version__,
        "connections": {c.name: "configured" for c in state.config.connections},
    }


@router.post("/admin/refresh", dependencies=[Depends(require_auth)])
async def refresh(request: Request) -> dict[str, object]:
    """Trigger a re-browse."""
    _ = request  # reserved for future use (emit telemetry, etc.)
    return {"status": "scheduled"}


@router.post("/admin/openapi/rebuild", dependencies=[Depends(require_auth)])
async def rebuild_openapi(request: Request) -> dict[str, object]:
    request.app.openapi_schema = None
    request.app.openapi()
    return {"status": "ok"}


# ---------------------------------------------------------------- inspection


@router.get("/admin/config", dependencies=[Depends(require_auth)])
async def get_admin_config(
    state: Annotated[AppState, Depends(get_state)],
) -> dict[str, Any]:
    """Current AppConfig as JSON, with secrets redacted. Backs the
    read-only /admin/ui explorer."""
    redacted: dict[str, Any] = _redact(state.config.model_dump(mode="json"))
    return redacted


_ADMIN_UI_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>i3xua — Dev Admin</title>
<style>
  :root { color-scheme: light dark; --fg: #000; --fg-2: #222; --label: #333;
          --bg: #eef0f3; --panel: #fff; --border: #bbb; --hr: #ccc; }
  @media (prefers-color-scheme: dark) {
    :root { --fg: #fff; --fg-2: #eaeaea; --label: #cfcfcf;
            --bg: #0b0d11; --panel: #161a20; --border: #3a404a; --hr: #2a2f38; }
  }
  body { font: 13px/1.45 ui-monospace, Menlo, Consolas, monospace;
         margin: 0; padding: 1rem; background: var(--bg); color: var(--fg); }
  .hdr { position: sticky; top: 0; background: var(--panel);
         padding: .5rem 1rem; border-bottom: 1px solid var(--border);
         display: flex; gap: 1rem; align-items: center; margin: -1rem -1rem 1rem; }
  .hdr h1 { font-size: 14px; margin: 0; font-weight: 700; color: var(--fg); }
  .conn-lights { display: flex; gap: .5rem; align-items: center; flex-wrap: wrap; }
  .conn-dot { display: inline-flex; align-items: center; gap: .35rem;
              padding: .15rem .5rem; border-radius: 999px;
              font-size: 11px; font-weight: 600;
              border: 1px solid var(--border); background: var(--panel); }
  .conn-dot::before { content: ""; width: 8px; height: 8px;
                      border-radius: 50%; display: inline-block; }
  .conn-dot.up::before   { background: #4ade80; box-shadow: 0 0 4px #4ade80; }
  .conn-dot.down::before { background: #f87171; box-shadow: 0 0 4px #f87171; }
  .conn-dot.unk::before  { background: #9ca3af; }
  input, button { font: inherit; padding: .3rem .6rem; color: var(--fg);
                  background: var(--panel); border: 1px solid var(--border);
                  border-radius: 4px; }
  input[type="password"] { min-width: 24ch; }
  button { cursor: pointer; font-weight: 600; }
  main { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }
  @media (max-width: 900px) { main { grid-template-columns: 1fr; } }
  .panel { background: var(--panel); border: 1px solid var(--border);
           border-radius: 6px; padding: .75rem 1rem; overflow: auto;
           color: var(--fg); }
  .panel h2 { font-size: 12px; text-transform: uppercase; letter-spacing: .05em;
              margin: 1rem 0 .5rem; color: var(--label); font-weight: 700; }
  .panel h2:first-child { margin-top: 0; }
  pre { margin: 0; white-space: pre-wrap; word-break: break-all;
        color: var(--fg); font-weight: 500; }
  pre.muted { color: var(--label); font-weight: normal; }
  table { border-collapse: collapse; width: 100%; font-size: 12px;
          color: var(--fg-2); }
  th { color: var(--label); font-weight: 700; }
  th, td { text-align: left; padding: .3rem .5rem;
           border-bottom: 1px solid var(--hr); vertical-align: top; }
  .muted { color: var(--label); }
  .kpi { display: flex; gap: 1.5rem; margin: .25rem 0 .75rem;
         font-variant-numeric: tabular-nums; }
  .kpi span { color: var(--label); font-size: 11px; display: block;
              font-weight: 600; }
  .kpi strong { font-size: 20px; color: var(--fg); }
  .err { color: #d33; font-weight: 700; }
  .andon-section {
    margin-top: 2rem; padding-top: 1rem;
    border-top: 1px solid #ccc;
  }
  .andon-header {
    display: flex; align-items: center; gap: 1rem;
    margin-bottom: 0.5rem;
  }
  .andon-header button {
    font: inherit; padding: 0.4rem 0.9rem;
    background: #2563eb; color: #fff; border: 0;
    border-radius: 4px; cursor: pointer;
  }
  .andon-header button:disabled {
    background: #888; cursor: not-allowed;
  }
  .andon-status { color: #555; font-size: 13px; }
  .andon-frame {
    width: 100%; height: 70vh;
    border: 1px solid #ccc; border-radius: 4px;
    background: #fff;
  }
  @media (prefers-color-scheme: dark) {
    .andon-section { border-top-color: #333; }
    .andon-status { color: #aaa; }
    .andon-frame { border-color: #333; background: #161a20; }
  }
</style>
</head>
<body>
  <div class="hdr">
    <h1>Dev Admin</h1>
    <span id="conn-lights" class="conn-lights"></span>
    <input id="tok" type="text" placeholder="bearer token"
           autocomplete="off" spellcheck="false">
    <button id="reload">Reload</button>
    <button id="clear" title="Clear stored token">Clear</button>
    <span id="status" class="muted"></span>
  </div>
  <main>
    <section class="panel">
      <h2>config.yaml (runtime, secrets redacted)</h2>
      <pre id="config" class="muted">Enter token and click Reload.</pre>
    </section>
    <section class="panel">
      <h2>State</h2>
      <div id="state" class="muted">—</div>
    </section>
  </main>
<section class="andon-section">
  <div class="andon-header">
    <button id="andon-btn" type="button">Run health check</button>
    <span id="andon-status" class="andon-status">never run</span>
  </div>
  <iframe id="andon-frame" class="andon-frame" sandbox="allow-same-origin"></iframe>
</section>
<script>
(() => {
  const $ = (id) => document.getElementById(id);
  const tok = $("tok");
  const saved = sessionStorage.getItem("opcua_admin_tok");
  if (saved) tok.value = saved;

  function esc(s) {
    return String(s).replace(/[&<>]/g, c =>
      ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
  }

  async function fetchJson(path) {
    const r = await fetch(path, { headers: { Authorization: "Bearer " + tok.value }});
    if (r.status === 401) {
      // Stale/wrong token — drop it so user isn't stuck with a silent bad value.
      sessionStorage.removeItem("opcua_admin_tok");
      throw new Error(path + " → 401 (wrong token; cleared)");
    }
    if (!r.ok) throw new Error(path + " → " + r.status);
    return r.json();
  }

  function renderConnLights(connections) {
    // One pill per configured connection. Green when browse_metrics is
    // populated (last connect completed a browse); red otherwise.
    const lights = document.getElementById("conn-lights");
    lights.innerHTML = (connections || []).map(c => {
      const populated = c.browse_metrics
                     && Object.keys(c.browse_metrics).length > 0;
      const cls = populated ? "up" : "down";
      const label = populated ? "connected" : "down";
      return `<span class="conn-dot ${cls}" title="${esc(c.endpoint)} — ${label}">${esc(c.name)}</span>`;
    }).join("");
  }

  function renderState(s) {
    renderConnLights(s.connections);
    const rows = [];
    rows.push(`<div class="kpi">
      <div><span>namespaces</span><strong>${s.namespaces.count}</strong></div>
      <div><span>types</span><strong>${s.types.count}</strong></div>
      <div><span>instances</span><strong>${s.instances.count}</strong></div>
      <div><span>subscriptions</span><strong>${s.subscriptions.length}</strong></div>
      <div><span>history elements</span><strong>${s.history.elements_tracked}</strong></div>
    </div>`);

    rows.push("<h2>Connections</h2><table><tr><th>name</th><th>endpoint</th><th>last browse</th><th>total</th><th>browse</th><th>types</th><th>snapshot</th></tr>" +
      s.connections.map(c => {
        const m = c.browse_metrics || {};
        const t = m.total_s !== undefined ? m.total_s.toFixed(2) + "s" : "—";
        const b = m.browse_s !== undefined ? m.browse_s.toFixed(2) + "s" : "—";
        const ty = m.types_s !== undefined ? m.types_s.toFixed(2) + "s" : "—";
        const sn = m.snapshot_s !== undefined ? m.snapshot_s.toFixed(2) + "s" : "—";
        const at = m.completed_at ? new Date(m.completed_at).toLocaleTimeString() : "—";
        return `<tr><td>${esc(c.name)}</td><td>${esc(c.endpoint)}<td>${esc(at)}</td><td>${t}</td><td>${b}</td><td>${ty}</td><td>${sn}</td></tr>`;
      }).join("") +
      "</table>");

    rows.push("<h2>Namespaces</h2><pre>" + s.namespaces.uris.map(esc).join("\\n") + "</pre>");

    if (s.subscriptions.length) {
      rows.push("<h2>Subscriptions</h2><table><tr><th>id</th><th>elements</th><th>ring</th><th>seq</th><th>streamers</th><th>dropped</th></tr>" +
        s.subscriptions.map(x =>
          `<tr><td>${esc(x.id)}</td><td>${x.element_count}</td><td>${x.ring_depth}</td><td>${x.next_sequence}</td><td>${x.streamers}</td><td>${x.dropped}</td></tr>`
        ).join("") + "</table>");
    } else {
      rows.push('<h2>Subscriptions</h2><div class="muted">none active</div>');
    }

    if (s.history.elements_tracked) {
      rows.push("<h2>History</h2><table><tr><th>elementId</th><th>depth</th></tr>" +
        s.history.elements.map(e =>
          `<tr><td>${esc(e.elementId)}</td><td>${e.depth}</td></tr>`
        ).join("") + "</table>");
    } else {
      rows.push('<h2>History</h2><div class="muted">no samples buffered</div>');
    }

    $("state").innerHTML = rows.join("");
  }

  async function reload() {
    const st = $("status");
    try {
      st.className = "muted"; st.textContent = "loading...";
      sessionStorage.setItem("opcua_admin_tok", tok.value);
      const [cfg, state] = await Promise.all([
        fetchJson("/v1/admin/config"),
        fetchJson("/v1/admin/state"),
      ]);
      $("config").className = "";
      $("config").textContent = JSON.stringify(cfg, null, 2);
      renderState(state);
      st.className = "muted";
      st.textContent = "loaded " + new Date().toLocaleTimeString();
    } catch (e) {
      st.className = "err";
      st.textContent = e.message;
    }
  }

  $("reload").addEventListener("click", reload);
  $("clear").addEventListener("click", () => {
    sessionStorage.removeItem("opcua_admin_tok");
    tok.value = "";
    $("status").className = "muted";
    $("status").textContent = "token cleared";
    tok.focus();
  });
  tok.addEventListener("keydown", e => { if (e.key === "Enter") reload(); });
  if (tok.value) reload();

  // ─── andon section ─────────────────────────────────────────────
  const andonBtn = document.getElementById("andon-btn");
  const andonStatus = document.getElementById("andon-status");
  const andonFrame = document.getElementById("andon-frame");

  async function loadAndonReport() {
    try {
      const r = await fetch("/v1/admin/andon/report", {
        headers: { Authorization: "Bearer " + tok.value },
      });
      const html = await r.text();
      andonFrame.srcdoc = html;
    } catch (e) {
      andonFrame.srcdoc = "<pre>error: " + esc(String(e)) + "</pre>";
    }
  }

  async function runAndon() {
    andonBtn.disabled = true;
    andonStatus.textContent = "running… (~15-30 s)";
    let keepDisabled = false;
    try {
      const r = await fetch("/v1/admin/andon/regenerate", {
        method: "POST",
        headers: { Authorization: "Bearer " + tok.value },
      });
      if (r.status === 409) {
        andonStatus.textContent = "another regen is already running; try again in a moment";
      } else if (r.status === 503) {
        andonStatus.textContent = "andon tool unavailable on this install";
        keepDisabled = true;
      } else if (!r.ok) {
        andonStatus.textContent = "error: " + r.status;
      } else {
        const result = await r.json();
        const ts = new Date().toLocaleTimeString();
        const tag = result.ok
          ? "last run at " + ts + " (took " + result.duration_s + "s)"
          : "andon battery reported errors at " + ts + " — see report below";
        andonStatus.textContent = tag;
        await loadAndonReport();
      }
    } catch (e) {
      andonStatus.textContent = "error: " + String(e);
    } finally {
      if (!keepDisabled) andonBtn.disabled = false;
    }
  }

  andonBtn.addEventListener("click", runAndon);
  // Initial load — shows the friendly 404 if no report exists yet.
  loadAndonReport();
})();
</script>
</body>
</html>
"""


@router.get("/admin/ui", response_class=HTMLResponse)
async def admin_ui() -> HTMLResponse:
    """Read-only explorer page. The HTML itself has no secrets and no auth;
    the JS inside prompts for a bearer token and hits /admin/config and
    /admin/state (which ARE auth-protected)."""
    return HTMLResponse(_ADMIN_UI_HTML)


@router.get("/admin/state", dependencies=[Depends(require_auth)])
async def get_admin_state(
    state: Annotated[AppState, Depends(get_state)],
) -> dict[str, Any]:
    """Runtime snapshot: connections configured, namespaces discovered,
    type/instance counts, active i3X subscriptions, history buffer depths.
    No mutation. Live connection status is a future Port extension."""
    namespaces = state.namespaces.snapshot()
    types = state.types.by_hash()
    instances = state.instances.snapshot()
    sub_summaries = await state.subscriptions.summaries()
    hist_depths = await state.history.depths()

    return {
        "connections": [
            {
                "name": c.name,
                "endpoint": c.endpoint,
                # Browse-phase timings recorded by the adapter on the most
                # recent successful connect. Empty dict if the connection
                # hasn't completed a browse yet.
                "browse_metrics": state.browse_metrics.get(c.name, {}),
            }
            for c in state.config.connections
        ],
        "namespaces": {
            "count": len(namespaces),
            "uris": [ns.i3x_uri for ns in namespaces.values()],
        },
        "types": {"count": len(types)},
        "instances": {"count": len(instances)},
        "subscriptions": sub_summaries,
        "history": {
            "elements_tracked": len(hist_depths),
            "elements": [{"elementId": eid, "depth": depth} for eid, depth in hist_depths.items()],
        },
    }


@router.post("/admin/andon/regenerate", dependencies=[Depends(require_auth)])
async def regenerate_andon(
    state: Annotated[AppState, Depends(get_state)],
) -> JSONResponse:
    """Synchronously run tools/andon_report.py under a single-process lock.

    Returns 200 on success, 409 if a regen is already in flight, 503 if
    the source tree isn't available, 504 on subprocess timeout.
    """
    if state.andon_regen_lock.locked():
        return JSONResponse(status_code=409, content={"error": "regen in progress"})
    repo_root = _repo_root()
    if repo_root is None:
        return JSONResponse(
            status_code=503,
            content={"error": "andon tool unavailable (no source tree)"},
        )
    async with state.andon_regen_lock:
        try:
            result = await _run_andon_subprocess(repo_root)
        except TimeoutError:
            return JSONResponse(
                status_code=504,
                content={"error": "andon regenerate timed out (>120s)"},
            )
    return JSONResponse(status_code=200, content=result)


_ANDON_NOT_GENERATED_HTML = """<!doctype html>
<html><head><title>andon report not generated</title>
<style>
body { font: 14px/1.5 ui-monospace, Menlo, Consolas, monospace;
       padding: 2rem; color: #333; }
.hint { padding: 1rem; border-left: 3px solid #888;
        background: #f6f6f6; margin: 1rem 0; }
</style></head>
<body>
<h2>No andon report yet</h2>
<div class="hint">Click <strong>Run health check</strong> in the admin
page to generate one. Takes about 15-20 seconds.</div>
</body></html>"""


@router.get("/admin/andon/report", dependencies=[Depends(require_auth)])
async def get_andon_report() -> HTMLResponse:
    """Serve andon-report.html from the repo root, or a friendly 404 if
    it hasn't been generated yet."""
    repo_root = _repo_root()
    if repo_root is None:
        return HTMLResponse(_ANDON_NOT_GENERATED_HTML, status_code=404)
    report_path = repo_root / "andon-report.html"
    if not report_path.is_file():
        return HTMLResponse(_ANDON_NOT_GENERATED_HTML, status_code=404)
    try:
        body = report_path.read_text(encoding="utf-8")
    except OSError:
        return HTMLResponse(
            "<h2>andon-report.html could not be read</h2>",
            status_code=500,
        )
    return HTMLResponse(body, status_code=200)
