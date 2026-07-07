"""The local dashboard: one self-contained HTML file, generated on demand.

This is the "what do you know about me" surface in visual form — a map of
the memory space, every memory, the hook activity log, and store health,
browsable in a browser. Deliberately a static snapshot, not a served app:
no port, no server, no new attack surface; the file lands inside the 0700
memory folder and never leaves the machine.

The map is a force-directed layout over the memory embeddings: the store
projects vectors to 2D (PCA seed) and hands over only coordinates + nearest
neighbors — never raw vectors — and the browser settles the layout so
similar memories pull together. Points are colored by scope (work / personal
/ project) and fade with age; regions are labeled by their scope centroid.
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
.mapwrap { position: relative; background: var(--surface); border: 1px solid var(--line);
           border-radius: 8px; overflow: hidden; }
#map { display: block; width: 100%; height: 440px; touch-action: none; }
.legend { display: flex; flex-wrap: wrap; gap: 0.4rem 0.9rem; padding: 0.6rem 0.9rem;
          border-top: 1px solid var(--line); font-size: 0.78rem; color: var(--ink-2); }
.legend span { display: inline-flex; align-items: center; gap: 0.35rem; }
.legend i { width: 0.7rem; height: 0.7rem; border-radius: 50%; display: inline-block; }
#maptip { position: absolute; pointer-events: none; max-width: 22rem; z-index: 5;
          background: var(--ink); color: var(--bg); padding: 0.4rem 0.6rem;
          border-radius: 6px; font-size: 0.8rem; line-height: 1.35;
          opacity: 0; transition: opacity 0.08s; box-shadow: 0 4px 14px rgba(0,0,0,.25); }
.maphint { padding: 0 0.9rem 0.6rem; font-size: 0.75rem; color: var(--ink-3); }
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
@keyframes flashrow { from { background: var(--accent); } to { background: transparent; } }
tr.flash td { animation: flashrow 1.1s ease-out; }
.badge { display: inline-block; border: 1px solid var(--line); border-radius: 4px;
         padding: 0 0.4rem; font-size: 0.72rem; color: var(--ink-2);
         white-space: nowrap; }
footer { margin-top: 2.5rem; color: var(--ink-3); font-size: 0.78rem; }
.empty { color: var(--ink-3); padding: 1rem; }
@media (prefers-reduced-motion: reduce) { tr.flash td { animation: none; } }
</style>
</head>
<body>
<header>
  <h1>engram</h1>
  <p>__SUBTITLE__</p>
</header>

<div class="tiles">__TILES__</div>

<h2>Memory map</h2>
<div class="mapwrap" id="mapwrap">
  <canvas id="map"></canvas>
  <div id="maptip"></div>
  <div class="legend" id="legend"></div>
  <div class="maphint">Similar memories pull together; color is scope, fading with age.
    Hover to read, click a point to jump to it below.</div>
</div>

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
    return `<tr class="${m.valid ? "" : "invalid"}" data-id="${esc(m.id)}">
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

// -- the memory map ---------------------------------------------------------
(function () {
  const wrap = document.getElementById("mapwrap");
  const meta = new Map(data.memories.map(m => [m.id, m]));
  // Only points we can also describe (join projection <-> memory rows).
  const nodes = (data.points || [])
    .filter(p => meta.has(p.id))
    .map(p => ({ id: p.id, neighbors: p.neighbors || [], m: meta.get(p.id),
                 sx: p.x, sy: p.y, x: 0, y: 0, vx: 0, vy: 0 }));
  if (nodes.length < 2) { wrap.style.display = "none"; return; }
  const byId = new Map(nodes.map(n => [n.id, n]));

  // Scope drives color (meaning, not plumbing); age drives opacity.
  const scopeColor = s => {
    let h = 0; for (const c of s) h = (h * 31 + c.charCodeAt(0)) >>> 0;
    return `hsl(${h % 360} 65% 55%)`;
  };
  const times = nodes.map(n => n.m.created_at);
  const lo = Math.min(...times), hi = Math.max(...times);
  const alphaOf = n => 0.4 + 0.6 * ((n.m.created_at - lo) / ((hi - lo) || 1));

  const canvas = document.getElementById("map");
  const tip = document.getElementById("maptip");
  const ctx = canvas.getContext("2d");
  let W = 0, H = 0, dpr = 1;

  // Seed positions from the PCA projection, scaled into the canvas box.
  function seed() {
    const xs = nodes.map(n => n.sx), ys = nodes.map(n => n.sy);
    const x0 = Math.min(...xs), x1 = Math.max(...xs);
    const y0 = Math.min(...ys), y1 = Math.max(...ys);
    const pad = 46;
    nodes.forEach(n => {
      n.x = pad + (W - 2 * pad) * ((n.sx - x0) / ((x1 - x0) || 1));
      n.y = pad + (H - 2 * pad) * ((n.sy - y0) / ((y1 - y0) || 1));
    });
  }

  // Force step: neighbor springs pull similar memories together, global
  // repulsion spreads the rest, weak centering keeps it framed.
  // ponytail: O(n^2) repulsion, capped below; PCA seed alone past the cap.
  const REP = 2600, SPRING = 0.02, REST = 46, CENTER = 0.004, DAMP = 0.86;
  function stepPhysics() {
    const cx = W / 2, cy = H / 2;
    for (let i = 0; i < nodes.length; i++) {
      const a = nodes[i]; let fx = 0, fy = 0;
      for (let j = 0; j < nodes.length; j++) {
        if (i === j) continue;
        const b = nodes[j];
        let dx = a.x - b.x, dy = a.y - b.y;
        const d2 = dx * dx + dy * dy + 0.01;
        const f = REP / d2;
        fx += dx * f; fy += dy * f;
      }
      a.neighbors.forEach(nid => {
        const b = byId.get(nid); if (!b) return;
        const dx = b.x - a.x, dy = b.y - a.y;
        const dist = Math.hypot(dx, dy) || 1;
        const f = SPRING * (dist - REST);
        fx += dx / dist * f * dist; fy += dy / dist * f * dist;
      });
      fx += (cx - a.x) * CENTER; fy += (cy - a.y) * CENTER;
      a.vx = (a.vx + fx) * DAMP; a.vy = (a.vy + fy) * DAMP;
    }
    for (const a of nodes) {
      a.x = Math.max(8, Math.min(W - 8, a.x + a.vx));
      a.y = Math.max(8, Math.min(H - 8, a.y + a.vy));
    }
  }

  function draw() {
    ctx.clearRect(0, 0, W, H);
    // faint neighbor edges — structure, not clutter
    ctx.lineWidth = 1;
    for (const a of nodes) for (const nid of a.neighbors) {
      const b = byId.get(nid); if (!b) continue;
      ctx.strokeStyle = "rgba(128,128,128,0.10)";
      ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
    }
    // scope-centroid labels (grounded: scope is known, not inferred)
    const cents = {};
    for (const n of nodes) {
      const s = n.m.scope; (cents[s] ||= { x: 0, y: 0, c: 0 });
      cents[s].x += n.x; cents[s].y += n.y; cents[s].c++;
    }
    ctx.font = "600 12px -apple-system, sans-serif";
    ctx.textAlign = "center";
    for (const s in cents) {
      const c = cents[s];
      ctx.fillStyle = scopeColor(s);
      ctx.globalAlpha = 0.9;
      ctx.fillText(s, c.x / c.c, c.y / c.c - 12);
    }
    ctx.globalAlpha = 1;
    // points
    for (const n of nodes) {
      ctx.beginPath(); ctx.arc(n.x, n.y, 5, 0, 7);
      if (!n.m.valid) {
        ctx.strokeStyle = scopeColor(n.m.scope); ctx.globalAlpha = 0.5;
        ctx.lineWidth = 1.5; ctx.stroke();
      } else {
        ctx.fillStyle = scopeColor(n.m.scope); ctx.globalAlpha = alphaOf(n);
        ctx.fill();
      }
    }
    ctx.globalAlpha = 1;
  }

  function resize() {
    dpr = window.devicePixelRatio || 1;
    W = canvas.clientWidth; H = canvas.clientHeight;
    canvas.width = W * dpr; canvas.height = H * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    seed(); draw();
  }
  window.addEventListener("resize", resize);
  resize();

  // Settle the layout (skip the animation for very large stores).
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (nodes.length <= 1500 && !reduce) {
    let frame = 0;
    (function tick() {
      stepPhysics(); draw();
      if (++frame < 180) requestAnimationFrame(tick);
    })();
  } else if (nodes.length <= 1500) {
    for (let i = 0; i < 180; i++) stepPhysics();
    draw();
  }

  // Hover -> tooltip; click -> jump to the memory's row.
  function nearest(mx, my) {
    let best = null, bd = 12 * 12;
    for (const n of nodes) {
      const dx = n.x - mx, dy = n.y - my, d = dx * dx + dy * dy;
      if (d < bd) { bd = d; best = n; }
    }
    return best;
  }
  canvas.addEventListener("mousemove", e => {
    const r = canvas.getBoundingClientRect();
    const n = nearest(e.clientX - r.left, e.clientY - r.top);
    if (n) {
      tip.textContent = n.m.text;
      tip.style.left = Math.min(e.clientX - r.left + 12, W - 20) + "px";
      tip.style.top = (e.clientY - r.top + 12) + "px";
      tip.style.opacity = 1; canvas.style.cursor = "pointer";
    } else { tip.style.opacity = 0; canvas.style.cursor = "default"; }
  });
  canvas.addEventListener("mouseleave", () => { tip.style.opacity = 0; });
  canvas.addEventListener("click", e => {
    const r = canvas.getBoundingClientRect();
    const n = nearest(e.clientX - r.left, e.clientY - r.top);
    if (!n) return;
    if (!n.m.valid && !state.invalid) {
      document.getElementById("showinvalid").click();
    }
    const row = rows.querySelector(`tr[data-id="${CSS.escape(n.id)}"]`);
    if (row) {
      row.scrollIntoView({ behavior: "smooth", block: "center" });
      row.classList.remove("flash"); void row.offsetWidth; row.classList.add("flash");
    }
  });

  // Legend: one swatch per scope present.
  const legend = document.getElementById("legend");
  legend.innerHTML = [...new Set(nodes.map(n => n.m.scope))].sort()
    .map(s => `<span><i style="background:${scopeColor(s)}"></i>${esc(s)}</span>`)
    .join("");
})();
</script>
</body>
</html>
"""


def _tile(value, label: str) -> str:
    return f"<div class='tile'><b>{value}</b><span>{label}</span></div>"


def render_dashboard(memories: list[dict], events: list[dict], stats: dict,
                     points: list[dict] | None = None) -> str:
    """memories: [{id, text, type, scope, tags, created_at, access_count,
    valid}]. events: [{kind, ts, hits}]. stats: MemoryStore.stats().
    points: [{id, x, y, neighbors}] from MemoryStore.map_points() — the 2D
    projection for the map (omit to render without it)."""
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
    data = json.dumps({"memories": memories, "events": events,
                       "points": points or []},
                      ensure_ascii=False).replace("</", "<\\/")
    return (_PAGE
            .replace("__SUBTITLE__", subtitle)
            .replace("__TILES__", "".join(tiles))
            .replace("__DATA__", data))
