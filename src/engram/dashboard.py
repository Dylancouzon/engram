"""The local dashboard: one self-contained HTML file, generated on demand.

This is the "what do you know about me" surface in visual form — every
memory, the hook activity log, and store health, browsable in a browser.
Deliberately a static snapshot, not a served app: no port, no server, no
new attack surface; the file lands inside the 0700 memory folder and never
leaves the machine.
"""

from __future__ import annotations

import datetime as dt
import json

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>engram — your memory</title>
<style>
:root {
  --bg: #f7f7f5; --surface: #ffffff; --ink: #1a1a1a; --ink-2: #555555;
  --ink-3: #8a8a8a; --line: #e4e4e0; --accent: #b45309;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #161618; --surface: #1f1f22; --ink: #ececec; --ink-2: #a8a8a8;
    --ink-3: #737373; --line: #313136; --accent: #e8934a;
  }
}
* { box-sizing: border-box; margin: 0; }
body {
  background: var(--bg); color: var(--ink);
  font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  padding: 2rem clamp(1rem, 4vw, 3rem); max-width: 72rem; margin: 0 auto;
}
h1 { font-size: 1.3rem; font-weight: 650; }
h2 { font-size: 0.85rem; font-weight: 600; text-transform: uppercase;
     letter-spacing: 0.06em; color: var(--ink-3); margin: 2.2rem 0 0.8rem; }
header p { color: var(--ink-2); font-size: 0.85rem; margin-top: 0.2rem; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(9.5rem, 1fr));
         gap: 0.75rem; margin-top: 1.4rem; }
.tile { background: var(--surface); border: 1px solid var(--line);
        border-radius: 8px; padding: 0.8rem 1rem; }
.tile b { display: block; font-size: 1.45rem; font-weight: 650;
          font-variant-numeric: tabular-nums; }
.tile span { font-size: 0.78rem; color: var(--ink-3); }
.controls { display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: center;
            margin-bottom: 0.8rem; }
.controls input[type=search] {
  flex: 1 1 14rem; padding: 0.45rem 0.7rem; font: inherit; color: var(--ink);
  background: var(--surface); border: 1px solid var(--line); border-radius: 6px;
}
.chip { border: 1px solid var(--line); background: var(--surface); color: var(--ink-2);
        border-radius: 999px; padding: 0.2rem 0.75rem; font: inherit;
        font-size: 0.8rem; cursor: pointer; }
.chip.on { border-color: var(--accent); color: var(--accent); }
.tablewrap { overflow-x: auto; background: var(--surface);
             border: 1px solid var(--line); border-radius: 8px; }
table { border-collapse: collapse; width: 100%; font-size: 0.86rem; }
th { text-align: left; font-weight: 600; color: var(--ink-3); font-size: 0.75rem;
     text-transform: uppercase; letter-spacing: 0.05em; }
th, td { padding: 0.55rem 0.8rem; border-bottom: 1px solid var(--line);
         vertical-align: top; }
tr:last-child td { border-bottom: none; }
td.text { min-width: 20rem; }
td .tags { color: var(--ink-3); font-size: 0.78rem; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
        font-size: 0.78rem; color: var(--ink-2); white-space: nowrap; }
.dim { color: var(--ink-3); }
tr.invalid td { color: var(--ink-3); }
tr.invalid td.text { text-decoration: line-through; text-decoration-color: var(--ink-3); }
.badge { display: inline-block; border: 1px solid var(--line); border-radius: 4px;
         padding: 0 0.4rem; font-size: 0.72rem; color: var(--ink-2);
         white-space: nowrap; }
footer { margin-top: 2.5rem; color: var(--ink-3); font-size: 0.78rem; }
.empty { color: var(--ink-3); padding: 1rem; }
</style>
</head>
<body>
<header>
  <h1>engram</h1>
  <p>__SUBTITLE__</p>
</header>

<div class="tiles">__TILES__</div>

<h2>Memories</h2>
<div class="controls">
  <input type="search" id="q" placeholder="filter by text, tag, or scope…">
  <span id="typechips"></span>
  <button class="chip" id="showinvalid">show superseded</button>
</div>
<div class="tablewrap">
  <table>
    <thead><tr><th>memory</th><th>type · scope</th><th>created</th>
    <th>recalled</th><th>id</th></tr></thead>
    <tbody id="rows"></tbody>
  </table>
  <div class="empty" id="empty" hidden>no memories match.</div>
</div>

<h2>Hook activity</h2>
<div class="tablewrap">
  <table>
    <thead><tr><th>when</th><th>trigger</th><th>result</th></tr></thead>
    <tbody id="events"></tbody>
  </table>
  <div class="empty" id="noevents" hidden>no hook activity yet.</div>
</div>

<footer>Generated locally by <span class="mono">engram dashboard</span>.
This file lives in your memory folder and never leaves your machine.
Forget a memory with <span class="mono">engram forget &lt;id&gt;</span>.</footer>

<script type="application/json" id="data">__DATA__</script>
<script>
const data = JSON.parse(document.getElementById("data").textContent);
const rows = document.getElementById("rows");
const state = { q: "", type: null, invalid: false };

const EVENT_LABELS = {
  "prompt-recall": ["prompt recall",
    n => n ? `injected ${n} memories` : "nothing confident enough to inject"],
  "session-start-recall": ["session start",
    n => n ? `surfaced ${n} memories` : "nothing relevant"],
  "auto-capture": ["capture",
    n => n ? `stored ${n} facts` : "nothing durable found"],
};

function fmt(ts) {
  return new Date(ts * 1000).toLocaleString(undefined,
    { dateStyle: "medium", timeStyle: "short" });
}
function esc(s) {
  const d = document.createElement("div"); d.textContent = s; return d.innerHTML;
}
function render() {
  const q = state.q.toLowerCase();
  let shown = 0;
  rows.innerHTML = data.memories.map(m => {
    if (!state.invalid && !m.valid) return "";
    if (state.type && m.type !== state.type) return "";
    if (q && !(m.text + " " + m.tags.join(" ") + " " + m.scope)
          .toLowerCase().includes(q)) return "";
    shown++;
    return `<tr class="${m.valid ? "" : "invalid"}">
      <td class="text">${esc(m.text)}${m.tags.length
        ? `<div class="tags">${esc(m.tags.join(", "))}</div>` : ""}</td>
      <td><span class="badge">${esc(m.type)} · ${esc(m.scope)}</span></td>
      <td class="dim">${fmt(m.created_at)}</td>
      <td class="dim">${m.access_count ? m.access_count + "×" : "–"}</td>
      <td class="mono" title="${esc(m.id)}">${esc(m.id.split("-")[0])}</td>
    </tr>`;
  }).join("");
  document.getElementById("empty").hidden = shown > 0;
}

const types = [...new Set(data.memories.map(m => m.type))].sort();
const chipbox = document.getElementById("typechips");
types.forEach(t => {
  const b = document.createElement("button");
  b.className = "chip"; b.textContent = t;
  b.onclick = () => {
    state.type = state.type === t ? null : t;
    chipbox.querySelectorAll(".chip").forEach(c =>
      c.classList.toggle("on", c.textContent === state.type));
    render();
  };
  chipbox.appendChild(b);
});
document.getElementById("q").oninput = e => { state.q = e.target.value; render(); };
document.getElementById("showinvalid").onclick = e => {
  state.invalid = !state.invalid;
  e.target.classList.toggle("on", state.invalid);
  render();
};

const evbody = document.getElementById("events");
evbody.innerHTML = data.events.map(e => {
  const [label, describe] = EVENT_LABELS[e.kind] || [e.kind, n => `${n} hits`];
  return `<tr><td class="dim">${fmt(e.ts)}</td>
    <td><span class="badge">${esc(label)}</span></td>
    <td>${esc(describe(e.hits))}</td></tr>`;
}).join("");
document.getElementById("noevents").hidden = data.events.length > 0;

render();
</script>
</body>
</html>
"""


def _tile(value, label: str) -> str:
    return f"<div class='tile'><b>{value}</b><span>{label}</span></div>"


def render_dashboard(memories: list[dict], events: list[dict], stats: dict) -> str:
    """memories: [{id, text, type, scope, tags, created_at, access_count,
    valid}]. events: [{kind, ts, hits}]. stats: MemoryStore.stats()."""
    valid = sum(1 for m in memories if m["valid"])
    tiles = [
        _tile(valid, "memories"),
        _tile(len(memories) - valid, "superseded"),
        _tile(len(stats.get("shards", {})), "shards"),
        _tile(stats.get("pending_reviews", 0), "awaiting review"),
        _tile(stats.get("disk", {}).get("data", "?"), "on disk"),
        _tile(stats.get("extraction", "?"), "extraction"),
    ]
    generated = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    subtitle = (f"{stats.get('data_dir', '')} · generated {generated} · "
                "local snapshot — rerun `engram dashboard` to refresh")
    data = json.dumps({"memories": memories, "events": events},
                      ensure_ascii=False).replace("</", "<\\/")
    return (_PAGE
            .replace("__SUBTITLE__", subtitle)
            .replace("__TILES__", "".join(tiles))
            .replace("__DATA__", data))
