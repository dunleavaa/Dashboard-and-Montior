"""
Local HTTP server for the dashboard page.

Runs in a background thread inside the main service process. Two routes:

  GET /            -> the dashboard HTML (single-file, dark, mobile-friendly)
  GET /api/snapshot -> current data as JSON, used by the page's refresh button

The server reads from a thread-safe snapshot the service updates every poll.
No external requests, no auth — local network only. Bind to 0.0.0.0 so the
phone can reach it; if you want to lock it down further, change to your
exact LAN IP or use a firewall rule.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

log = logging.getLogger(__name__)


# Single dark page. Inlined CSS + JS, no build step, no external dependencies.
# Fits comfortably on a phone screen, scrolls vertically.
DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#1a1d24">
<title>NT Monitor</title>
<style>
  :root {
    --bg: #1a1d24;
    --panel: #242832;
    --panel-2: #2c313c;
    --text: #e6e8eb;
    --text-dim: #9aa0a6;
    --border: #353b47;
    --green: #2ecc71;
    --red: #e74c3c;
    --yellow: #f1c40f;
    --blue: #3498db;
    --gray: #95a5a6;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    font-size: 14px;
    line-height: 1.4;
    padding: env(safe-area-inset-top) env(safe-area-inset-right)
            env(safe-area-inset-bottom) env(safe-area-inset-left);
  }
  header {
    padding: 16px;
    border-bottom: 1px solid var(--border);
    position: sticky; top: 0;
    background: var(--bg);
    z-index: 10;
  }
  .banner {
    border-radius: 8px;
    padding: 12px 16px;
    font-size: 16px;
    font-weight: 600;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }
  .banner.green { background: color-mix(in srgb, var(--green) 18%, var(--panel)); border: 1px solid var(--green); }
  .banner.red   { background: color-mix(in srgb, var(--red)   18%, var(--panel)); border: 1px solid var(--red); }
  .banner.yellow{ background: color-mix(in srgb, var(--yellow)18%, var(--panel)); border: 1px solid var(--yellow); }
  .banner.gray  { background: var(--panel); border: 1px solid var(--border); }

  .meta {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-top: 10px;
    color: var(--text-dim);
    font-size: 12px;
  }
  button.refresh {
    background: var(--panel-2);
    color: var(--text);
    border: 1px solid var(--border);
    padding: 8px 14px;
    border-radius: 6px;
    font-size: 13px;
    cursor: pointer;
  }
  button.refresh:active { background: var(--blue); border-color: var(--blue); }

  main { padding: 16px; }
  section { margin-bottom: 24px; }
  h2 {
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-dim);
    margin: 0 0 8px 0;
    font-weight: 600;
  }

  .card {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 12px 14px;
    margin-bottom: 8px;
  }
  .card-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
  }
  .card-name {
    font-weight: 600;
    font-size: 15px;
  }
  .card-meta {
    color: var(--text-dim);
    font-size: 12px;
    margin-top: 2px;
  }
  .pnl-pos { color: var(--green); font-weight: 600; }
  .pnl-neg { color: var(--red); font-weight: 600; }
  .pnl-zero { color: var(--text-dim); }
  .cash { font-variant-numeric: tabular-nums; }

  table {
    width: 100%;
    border-collapse: collapse;
    background: var(--panel);
    border-radius: 8px;
    overflow: hidden;
    border: 1px solid var(--border);
  }
  th, td {
    padding: 10px 12px;
    text-align: left;
    font-size: 13px;
    border-bottom: 1px solid var(--border);
  }
  th {
    background: var(--panel-2);
    color: var(--text-dim);
    font-weight: 600;
    text-transform: uppercase;
    font-size: 11px;
    letter-spacing: 0.05em;
  }
  td.num { text-align: right; font-variant-numeric: tabular-nums; }
  tr:last-child td { border-bottom: none; }

  .pill {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 10px;
    font-size: 11px;
    font-weight: 600;
  }
  .pill.green { background: color-mix(in srgb, var(--green) 25%, transparent); color: var(--green); }
  .pill.red   { background: color-mix(in srgb, var(--red)   25%, transparent); color: var(--red); }
  .pill.gray  { background: color-mix(in srgb, var(--gray)  25%, transparent); color: var(--gray); }

  .empty {
    color: var(--text-dim);
    text-align: center;
    padding: 16px;
    font-style: italic;
  }
  .conn-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; vertical-align: middle; }
  .conn-dot.on  { background: var(--green); }
  .conn-dot.off { background: var(--gray); }

  /* Mobile tweaks */
  @media (max-width: 480px) {
    body { font-size: 13px; }
    th, td { padding: 8px 10px; font-size: 12px; }
    .banner { padding: 10px 12px; font-size: 14px; }
  }
</style>
</head>
<body>

<header>
  <div id="banner" class="banner gray">Loading…</div>
  <div class="meta">
    <span id="updated">—</span>
    <button class="refresh" id="refresh">Refresh</button>
  </div>
</header>

<main>
  <section>
    <h2>Accounts</h2>
    <div id="accounts"></div>
  </section>

  <section>
    <h2>P&amp;L by symbol (today)</h2>
    <div id="pnl-table-wrap"></div>
  </section>

  <section>
    <h2>Strategies</h2>
    <div id="strategies"></div>
  </section>

  <section>
    <h2>Open positions</h2>
    <div id="positions"></div>
  </section>
</main>

<script>
const $ = (id) => document.getElementById(id);
const fmtMoney = (n) => {
  if (n === null || n === undefined) return "—";
  const sign = n < 0 ? "-" : "";
  return sign + "$" + Math.abs(n).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
};
const pnlClass = (n) => n > 0 ? "pnl-pos" : n < 0 ? "pnl-neg" : "pnl-zero";
const pnlText = (n) => (n > 0 ? "+" : "") + fmtMoney(n);

async function refresh() {
  $("refresh").disabled = true;
  $("refresh").textContent = "Loading…";
  try {
    const r = await fetch("/api/snapshot", {cache: "no-store"});
    if (!r.ok) throw new Error("HTTP " + r.status);
    render(await r.json());
  } catch (e) {
    $("banner").className = "banner red";
    $("banner").textContent = "Cannot reach service: " + e.message;
  } finally {
    $("refresh").disabled = false;
    $("refresh").textContent = "Refresh";
  }
}

function render(d) {
  // Banner
  $("banner").className = "banner " + (d.status_color || "gray");
  $("banner").innerHTML = `<span>${d.status_title || "—"}</span>
                            <span style="font-weight:400;font-size:13px;opacity:0.85">${d.status_subtitle || ""}</span>`;

  // Last updated
  const ts = d.snapshot_at ? new Date(d.snapshot_at) : null;
  $("updated").textContent = ts
    ? "Snapshot: " + ts.toLocaleTimeString() + (d.heartbeat_age_sec !== null
        ? "  ·  NT age: " + Math.round(d.heartbeat_age_sec) + "s"
        : "")
    : "—";

  // Accounts
  const acctEl = $("accounts");
  if (!d.accounts || d.accounts.length === 0) {
    acctEl.innerHTML = '<div class="empty">No accounts visible. Edit accounts.yaml to add them.</div>';
  } else {
    acctEl.innerHTML = d.accounts.map(a => `
      <div class="card">
        <div class="card-row">
          <div>
            <span class="conn-dot ${a.connected ? 'on' : 'off'}"></span>
            <span class="card-name">${a.alias}</span>
            <span class="pill gray" style="margin-left:6px">${a.type_label}</span>
          </div>
          <div class="cash">${fmtMoney(a.cash_value)}</div>
        </div>
        <div class="card-row" style="margin-top:6px">
          <div class="card-meta">${a.connection || "—"}</div>
          <div class="${pnlClass(a.realized_pnl_today)}">${pnlText(a.realized_pnl_today)}</div>
        </div>
      </div>
    `).join("");
  }

  // P&L table
  const wrap = $("pnl-table-wrap");
  if (!d.pnl_rows || d.pnl_rows.length === 0) {
    wrap.innerHTML = '<div class="empty">No fills yet this session.</div>';
  } else {
    wrap.innerHTML = `
      <table>
        <thead><tr>
          <th>Account</th><th>Symbol</th>
          <th class="num">P&amp;L</th>
          <th class="num">Trades</th>
          <th class="num">Win %</th>
          <th class="num">Open</th>
        </tr></thead>
        <tbody>
          ${d.pnl_rows.map(r => `
            <tr>
              <td>${r.account_alias}</td>
              <td>${r.symbol}</td>
              <td class="num ${pnlClass(r.net_pnl)}">${pnlText(r.net_pnl)}</td>
              <td class="num">${r.trades}</td>
              <td class="num">${r.trades > 0 ? Math.round(r.win_rate * 100) + "%" : "—"}</td>
              <td class="num ${r.open_position > 0 ? 'pnl-pos' : r.open_position < 0 ? 'pnl-neg' : ''}">${r.open_position || "—"}</td>
            </tr>
          `).join("")}
        </tbody>
      </table>`;
  }

  // Strategies
  const strEl = $("strategies");
  if (!d.strategies || d.strategies.length === 0) {
    strEl.innerHTML = '<div class="empty">No strategies reporting.</div>';
  } else {
    strEl.innerHTML = d.strategies.map(s => `
      <div class="card">
        <div class="card-row">
          <div>
            <span class="card-name">${s.name}</span>
            <div class="card-meta">${s.instrument} on ${s.account_alias}</div>
          </div>
          <span class="pill ${s.enabled ? 'green' : 'red'}">${s.state}</span>
        </div>
      </div>
    `).join("");
  }

  // Open positions
  const posEl = $("positions");
  if (!d.open_positions || d.open_positions.length === 0) {
    posEl.innerHTML = '<div class="empty">No open positions.</div>';
  } else {
    posEl.innerHTML = d.open_positions.map(p => `
      <div class="card">
        <div class="card-row">
          <div>
            <span class="card-name">${p.side} ${p.qty} ${p.instrument}</span>
            <div class="card-meta">${p.account_alias}  ·  avg ${p.avg_price.toFixed(2)}</div>
          </div>
        </div>
      </div>
    `).join("");
  }
}

$("refresh").addEventListener("click", refresh);
refresh();  // initial load
</script>

</body>
</html>
"""


# ---------------------------------------------------------------------------
# Snapshot — what the service updates and what the API returns
# ---------------------------------------------------------------------------


class SnapshotStore:
    """Thread-safe holder for the most recent snapshot dict."""

    def __init__(self):
        self._lock = threading.Lock()
        self._snapshot: dict = {
            "snapshot_at": None,
            "status_color": "gray",
            "status_title": "Service starting",
            "status_subtitle": "",
            "heartbeat_age_sec": None,
            "accounts": [],
            "pnl_rows": [],
            "strategies": [],
            "open_positions": [],
        }

    def update(self, snapshot: dict) -> None:
        with self._lock:
            self._snapshot = snapshot

    def get(self) -> dict:
        with self._lock:
            return dict(self._snapshot)


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------


def _make_handler(store: SnapshotStore):
    class Handler(BaseHTTPRequestHandler):
        # Quiet by default — we don't need every GET in the service log
        def log_message(self, fmt, *args):
            log.debug("HTTP " + fmt, *args)

        def do_GET(self):
            if self.path == "/" or self.path.startswith("/index"):
                body = DASHBOARD_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return

            if self.path.startswith("/api/snapshot"):
                body = json.dumps(store.get(), default=str).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
                return

            self.send_response(404)
            self.end_headers()

    return Handler


class WebServer:
    """Background HTTP server. Start once, stop on service shutdown."""

    def __init__(self, store: SnapshotStore, host: str = "0.0.0.0", port: int = 8080):
        self.store = store
        self.host = host
        self.port = port
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        handler = _make_handler(self.store)
        self._server = ThreadingHTTPServer((self.host, self.port), handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="WebServer",
            daemon=True,
        )
        self._thread.start()
        log.info("Dashboard listening on http://%s:%d/", self.host, self.port)

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            log.info("Dashboard stopped")
