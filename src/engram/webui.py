"""Shared web assets for the two browser surfaces — the static `engram
dashboard` snapshot and the interactive `engram serve` app.

Both import the same palette tokens and the same embedding-map code so the
signature visual (the map) and the color system never drift between them.
Everything here is a plain string embedded into a page; there is no build
step and nothing is fetched from the network.
"""

from __future__ import annotations

# Dark-first: the embedding map is the hero, so the ground is dark and the
# points glow against it. Light theme follows the OS via prefers-color-scheme.
# The amber accent is engram's existing brand color, kept.
THEME_CSS = """
:root {
  --bg: #0e1116; --surface: #171b21; --surface-2: #1e242c; --line: #2a2f37;
  --ink: #e6edf3; --ink-2: #9aa4b2; --ink-3: #6b7280;
  --accent: #e8934a; --accent-2: #f0a45c; --danger: #e0605f; --ok: #63b95e;
}
@media (prefers-color-scheme: light) {
  :root {
    --bg: #f6f6f4; --surface: #ffffff; --surface-2: #f0f0ee; --line: #e2e2de;
    --ink: #1a1a1a; --ink-2: #555555; --ink-3: #8a8a8a;
    --accent: #b45309; --accent-2: #92400e; --danger: #c0392b; --ok: #3f8f4f;
  }
}
"""

# The 10-color categorical palette for scope/type coloring on the map.
PALETTE_JS = """
const PALETTE = ["#e8934a", "#4a9ce8", "#63b95e", "#c774d9", "#e0605f",
                 "#42b8b8", "#d7a83e", "#8a7de0", "#5aa9d6", "#c98a5a"];
"""

# initMap(cfg) draws the interactive embedding scatter and returns a handle.
# cfg: { data:{memories,points}, canvas, tip, legend, colorButtons, colorMode,
#        onPick(memory,node) }. Returns { highlight(idSet|null), setFilter(str),
#        setColorMode(m), redraw() }. No physics — points sit at their real
# projected coordinates; the viewer pans/zooms/hovers. Decoupled from any
# table so both pages can drive it differently.
MAP_JS = PALETTE_JS + """
function initMap(cfg) {
  const meta = new Map(cfg.data.memories.map(m => [m.id, m]));
  const nodes = (cfg.data.points || [])
    .filter(p => meta.has(p.id))
    .map(p => ({ id: p.id, neighbors: p.neighbors || [], m: meta.get(p.id),
                 sx: p.x, sy: p.y, bx: 0, by: 0 }));
  const byId = new Map(nodes.map(n => [n.id, n]));
  const canvas = cfg.canvas, tip = cfg.tip, ctx = canvas.getContext("2d");
  let W = 0, H = 0;
  const view = { scale: 1, ox: 0, oy: 0 };
  let colorMode = cfg.colorMode ||
    (new Set(nodes.map(n => n.m.scope)).size > 1 ? "scope" : "type");
  let hover = null, selected = null, filter = "", highlit = null;

  const esc = s => { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; };
  // Category->color computed once per colorMode change, not per node per frame:
  // draw() runs on every hover/pan/zoom, so an O(n) rebuild inside colorOf made
  // each frame O(n^2).
  let cats = [], catColor = new Map();
  function recolor() {
    cats = [...new Set(nodes.map(n => n.m[colorMode]))].sort();
    catColor = new Map(cats.map((c, i) => [c, PALETTE[i % PALETTE.length]]));
  }
  const colorOf = n => catColor.get(n.m[colorMode]) || PALETTE[0];
  const times = nodes.map(n => n.m.created_at);
  const lo = Math.min(...times), hi = Math.max(...times);
  const ageAlpha = n => 0.5 + 0.5 * ((n.m.created_at - lo) / ((hi - lo) || 1));
  const shown = n => (!filter ||
    (n.m.text + " " + n.m.tags.join(" ") + " " + n.m.scope).toLowerCase().includes(filter));

  function fit() {
    const xs = nodes.map(n => n.sx), ys = nodes.map(n => n.sy);
    const x0 = Math.min(...xs), x1 = Math.max(...xs);
    const y0 = Math.min(...ys), y1 = Math.max(...ys);
    const pad = 52;
    nodes.forEach(n => {
      n.bx = pad + (W - 2 * pad) * ((n.sx - x0) / ((x1 - x0) || 1));
      n.by = pad + (H - 2 * pad) * ((n.sy - y0) / ((y1 - y0) || 1));
    });
  }
  const px = n => n.bx * view.scale + view.ox;
  const py = n => n.by * view.scale + view.oy;

  function draw() {
    ctx.clearRect(0, 0, W, H);
    const focus = hover || selected;
    if (focus) {
      ctx.strokeStyle = "rgba(150,160,175,0.55)"; ctx.lineWidth = 1.3;
      focus.neighbors.forEach(nid => {
        const b = byId.get(nid); if (!b) return;
        ctx.beginPath(); ctx.moveTo(px(focus), py(focus)); ctx.lineTo(px(b), py(b)); ctx.stroke();
      });
    }
    for (const n of nodes) {
      const on = shown(n) && (!highlit || highlit.has(n.id));
      const col = colorOf(n), big = (n === hover || n === selected || (highlit && highlit.has(n.id)));
      ctx.beginPath(); ctx.arc(px(n), py(n), big ? 7 : 4.5, 0, 7);
      ctx.shadowBlur = big && on ? 14 : 0; ctx.shadowColor = col;
      if (!n.m.valid) {
        ctx.strokeStyle = col; ctx.lineWidth = 1.5;
        ctx.globalAlpha = on ? 0.6 : 0.08; ctx.stroke();
      } else {
        ctx.fillStyle = col; ctx.globalAlpha = on ? ageAlpha(n) : 0.07; ctx.fill();
      }
      ctx.shadowBlur = 0;
      if (big && on) { ctx.globalAlpha = 1; ctx.strokeStyle = col; ctx.lineWidth = 2; ctx.stroke(); }
    }
    ctx.globalAlpha = 1;
    const cent = {};
    for (const n of nodes) {
      const k = n.m[colorMode]; (cent[k] ??= { x: 0, y: 0, c: 0 });
      cent[k].x += px(n); cent[k].y += py(n); cent[k].c++;
    }
    ctx.font = "600 11px -apple-system, system-ui, sans-serif"; ctx.textAlign = "center";
    for (const k in cent) {
      const c = cent[k];
      ctx.fillStyle = catColor.get(k) || PALETTE[0];
      ctx.globalAlpha = 0.9; ctx.fillText(k, c.x / c.c, c.y / c.c - 15);
    }
    ctx.globalAlpha = 1;
  }

  function resize() {
    const dpr = window.devicePixelRatio || 1;
    W = canvas.clientWidth; H = canvas.clientHeight;
    canvas.width = W * dpr; canvas.height = H * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    fit(); draw();
  }
  window.addEventListener("resize", resize);

  function nearest(mx, my) {
    let best = null, bd = 13 * 13;
    for (const n of nodes) {
      const dx = px(n) - mx, dy = py(n) - my, d = dx * dx + dy * dy;
      if (d < bd) { bd = d; best = n; }
    }
    return best;
  }

  canvas.addEventListener("wheel", e => {
    e.preventDefault();
    const r = canvas.getBoundingClientRect();
    const mx = e.clientX - r.left, my = e.clientY - r.top;
    const f = e.deltaY < 0 ? 1.12 : 1 / 1.12;
    view.scale = Math.max(0.4, Math.min(12, view.scale * f));
    view.ox = mx - (mx - view.ox) * f; view.oy = my - (my - view.oy) * f;
    draw();
  }, { passive: false });

  let drag = null, moved = false;
  canvas.addEventListener("mousedown", e => { drag = { x: e.clientX, y: e.clientY }; moved = false; });
  window.addEventListener("mousemove", e => {
    if (!drag) return;
    const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
    if (Math.hypot(dx, dy) > 3) { moved = true; canvas.style.cursor = "grabbing"; }
    view.ox += dx; view.oy += dy; drag = { x: e.clientX, y: e.clientY }; draw();
  });
  window.addEventListener("mouseup", () => { drag = null; canvas.style.cursor = "grab"; });

  canvas.addEventListener("mousemove", e => {
    if (drag) return;
    const r = canvas.getBoundingClientRect();
    const mx = e.clientX - r.left, my = e.clientY - r.top;
    const n = nearest(mx, my);
    if (n !== hover) { hover = n; draw(); }
    if (n) {
      tip.textContent = n.m.text;
      tip.style.left = Math.min(mx + 14, W - 20) + "px";
      tip.style.top = (my + 14) + "px"; tip.style.opacity = 1;
    } else { tip.style.opacity = 0; }
  });
  canvas.addEventListener("mouseleave", () => { tip.style.opacity = 0; hover = null; draw(); });

  canvas.addEventListener("click", e => {
    if (moved) return;
    const r = canvas.getBoundingClientRect();
    const n = nearest(e.clientX - r.left, e.clientY - r.top);
    selected = n || null; draw();
    if (cfg.onPick) cfg.onPick(n ? n.m : null, n);
  });
  canvas.addEventListener("dblclick", () => {
    view.scale = 1; view.ox = 0; view.oy = 0; selected = null; highlit = null; draw();
  });

  if (cfg.colorButtons) cfg.colorButtons.forEach(b => {
    b.classList.toggle("on", b.dataset.mode === colorMode);
    b.onclick = () => {
      colorMode = b.dataset.mode;
      cfg.colorButtons.forEach(x => x.classList.toggle("on", x.dataset.mode === colorMode));
      recolor(); buildLegend(); draw();
    };
  });
  function buildLegend() {
    if (!cfg.legend) return;
    cfg.legend.innerHTML = cats.map(c =>
      `<span><i style="background:${catColor.get(c)}"></i>${esc(c)}</span>`).join("");
  }
  recolor();
  buildLegend();
  resize();

  return {
    setFilter: s => { filter = (s || "").toLowerCase(); draw(); },
    highlight: ids => { highlit = ids && ids.size ? ids : null; draw(); },
    select: id => { selected = byId.get(id) || null; draw(); },
  };
}
"""
