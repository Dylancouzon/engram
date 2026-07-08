"""`engram serve` — the opt-in, private, interactive local app.

Unlike `engram dashboard` (a static file, no process, no port), this runs a
small HTTP server so you can manage memories, chat with the local model
about them, and trigger the sleep pass — none of which a static file can do.

Trust model (read before touching auth):
- Any process running as you can already reach the 0600 daemon socket and act
  as the implicit `cli` client with full scope. The web token is NOT a defense
  against a hostile same-user process — it exists solely to keep *browser*
  traffic (other sites, the wider network) out of your memory.
- The server binds 127.0.0.1 only. A Host-header allowlist closes DNS
  rebinding (a page re-resolving to 127.0.0.1 is same-origin in the browser).
- Auth is double-submit: `GET /?k=<token>` sets an HttpOnly, SameSite=Strict
  session cookie and redirects; every /api/* call must carry BOTH that cookie
  (auto) AND an `X-Engram-Token` header (set by our own page script from an
  injected constant a cross-origin attacker can't read). The header is the
  CSRF defense; SameSite alone is not enough (localhost ports share a "site").
- A strict CSP (`script-src 'nonce-…'`) keeps an escaping slip in
  attacker-influenced memory text from becoming code execution / write access.
- Only the in-memory SPA and JSON routes are served — never the filesystem
  (no SimpleHTTPRequestHandler, so no path traversal).

Every store operation goes through the existing daemon as a per-request
client (the daemon stays the single writer). Chat calls Ollama directly.
"""

from __future__ import annotations

import hmac
import json
import secrets
import webbrowser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from engram.client import Client, DaemonUnavailable
from engram.config import Config
from engram.llm import LocalLLM
from engram.protocol import ProtocolError, memory_to_wire, review_to_wire
from engram.webui import MAP_JS, THEME_CSS

CONSOLIDATE_TIMEOUT = 600.0  # the sleep pass holds the write lock through LLM calls


def serve(config: Config, port: int = 0, open_browser: bool = True) -> None:
    token = secrets.token_urlsafe(32)

    class Handler(_BaseHandler):
        pass

    Handler.config = config
    Handler.token = token
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True  # a hung chat thread must not block shutdown
    bound_port = httpd.server_address[1]
    Handler.port = bound_port
    url = f"http://127.0.0.1:{bound_port}/?k={token}"
    print(f"engram serve → {url}")
    print("  private: bound to 127.0.0.1, token-gated. Ctrl-C to stop.")
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()


class _BaseHandler(BaseHTTPRequestHandler):
    config: Config
    token: str
    port: int

    # -- security helpers -----------------------------------------------------

    def log_message(self, fmt, *args):
        # Log method + path only — never the query string (it carries the token).
        import sys
        sys.stderr.write(f"engram serve: {self.command} {urlparse(self.path).path}\n")

    def _host_ok(self) -> bool:
        host = self.headers.get("Host", "")
        return host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}")

    def _tok_eq(self, supplied: str | None) -> bool:
        return isinstance(supplied, str) and hmac.compare_digest(supplied, self.token)

    def _cookie_ok(self) -> bool:
        raw = self.headers.get("Cookie")
        if not raw:
            return False
        jar = SimpleCookie(raw)
        m = jar.get("engram_session")
        return bool(m and self._tok_eq(m.value))

    def _api_authed(self) -> bool:
        # Double-submit: session cookie (auto-sent) AND custom header (our page
        # only). The header is what a cross-origin attacker cannot forge.
        return self._cookie_ok() and self._tok_eq(self.headers.get("X-Engram-Token"))

    # -- transport ------------------------------------------------------------

    def _client(self, timeout: float = 120.0) -> Client:
        return Client(self.config, client_name="cli", timeout=timeout).connect(spawn=True)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return {}

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status: int = 200) -> None:
        self._send(status, json.dumps(obj).encode(), "application/json")

    def _deny(self, status: int, msg: str) -> None:
        self._send(status, msg.encode(), "text/plain; charset=utf-8")

    # -- routing --------------------------------------------------------------

    def do_GET(self):
        if not self._host_ok():
            return self._deny(403, "bad host")
        parsed = urlparse(self.path)
        path, qs = parsed.path, parse_qs(parsed.query)

        if path == "/":
            k = (qs.get("k") or [None])[0]
            if k is not None:  # token handoff -> cookie, then drop it from the URL
                if not self._tok_eq(k):
                    return self._deny(403, "bad token")
                self.send_response(303)
                self.send_header("Location", "/")
                self.send_header(
                    "Set-Cookie",
                    f"engram_session={self.token}; HttpOnly; SameSite=Strict; Path=/",
                )
                self.end_headers()
                return
            if not self._cookie_ok():
                return self._deny(403, "open the URL printed by `engram serve` "
                                       "(it carries your one-time key).")
            return self._serve_spa()

        if path == "/api/state":
            if not self._api_authed():
                return self._deny(403, "unauthorized")
            try:
                return self._state()
            except DaemonUnavailable as e:
                return self._json({"error": f"daemon unavailable: {e}"}, 503)
            except ProtocolError as e:
                return self._json({"error": e.code + ": " + str(e)}, 400)
        return self._deny(404, "not found")

    def do_POST(self):
        if not self._host_ok():
            return self._deny(403, "bad host")
        if not self._api_authed():
            return self._deny(403, "unauthorized")
        path = urlparse(self.path).path
        try:
            if path == "/api/chat":
                return self._chat()
            if path == "/api/remember":
                return self._remember()
            if path == "/api/forget":
                return self._forget()
            if path == "/api/edit":
                return self._edit()
            if path == "/api/review":
                return self._review()
            if path == "/api/consolidate":
                return self._consolidate()
        except DaemonUnavailable as e:
            return self._json({"error": f"daemon unavailable: {e}"}, 503)
        except ProtocolError as e:
            return self._json({"error": e.code + ": " + str(e)}, 400)
        return self._deny(404, "not found")

    # -- API handlers ---------------------------------------------------------

    def _state(self):
        with self._client() as c:
            memories = [
                {"id": m.id, "text": m.text, "type": m.type.value, "scope": m.scope,
                 "tags": m.tags, "created_at": m.created_at, "importance": m.importance,
                 "access_count": m.access_count, "valid": m.is_valid}
                for m in c.list(include_invalid=True)
            ]
            points = c.map_points()
            events = c.recent_events(100)
            stats = c.stats()
            reviews = [review_to_wire(r) for r in c.pending_reviews()]
        self._json({"memories": memories, "points": points, "events": events,
                    "stats": stats, "reviews": reviews})

    def _remember(self):
        b = self._read_json()
        with self._client() as c:
            actions = c.remember(
                text=str(b.get("text", "")).strip(),
                scope=b.get("scope") or "default",
                tags=b.get("tags") or None,
                importance=b.get("importance"),
            )
        self._json({"actions": [{"op": a.op.value,
                                 "id": a.memory.id if a.memory else None} for a in actions]})

    def _forget(self):
        b = self._read_json()
        with self._client() as c:
            gone = c.forget(str(b["id"]), mode=b.get("mode", "soft"))
        self._json({"forgotten": gone})

    def _edit(self):
        b = self._read_json()
        with self._client() as c:
            m = c.edit(str(b["id"]), scope=b.get("scope"), tags=b.get("tags"),
                       importance=b.get("importance"))
        self._json({"memory": memory_to_wire(m) if m else None})

    def _review(self):
        b = self._read_json()
        with self._client() as c:
            ok = c.resolve_review(int(b["seq"]), bool(b["accept"]))
        self._json({"resolved": ok})

    def _consolidate(self):
        with self._client(timeout=CONSOLIDATE_TIMEOUT) as c:
            report = c.consolidate()
        self._json(report)

    def _chat(self):
        b = self._read_json()
        message = str(b.get("message", "")).strip()
        history = [h for h in (b.get("history") or [])
                   if isinstance(h, dict) and h.get("role") in ("user", "assistant")][-8:]
        if not message:
            return self._json({"error": "empty message"}, 400)

        with self._client() as c:
            hits = c.recall(message, k=6)
        used = [h.memory.id for h in hits]
        context = "\n".join(f"- {h.memory.text}" for h in hits)
        system = (
            "You are engram, the user's personal memory assistant. These are the "
            "user's stored memories most relevant to their message:\n"
            f"{context or '(none matched)'}\n\n"
            "Ground your answer in them when relevant and say which fact you used. "
            "If they don't cover the question, say so and answer normally. Be concise."
        )
        messages = [{"role": "system", "content": system}, *history,
                    {"role": "user", "content": message}]

        # Close-delimited NDJSON: no Content-Length, connection closes to end.
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Connection", "close")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if not self._line({"used": used}):
            return

        llm = LocalLLM(self.config.ollama_url, self.config.extraction_model)
        if not llm.available():
            self._line({"token": "(local model offline — run `ollama serve` and "
                                 "pull the model to chat about your memories)"})
            self._line({"done": True})
            return
        for chunk in llm.chat(messages):
            if not self._line({"token": chunk}):
                return  # client disconnected; llm.chat closes its upstream
        self._line({"done": True})

    def _line(self, obj) -> bool:
        """Write one NDJSON frame; False if the client has gone."""
        try:
            self.wfile.write(json.dumps(obj).encode() + b"\n")
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False

    # -- the page -------------------------------------------------------------

    def _serve_spa(self):
        nonce = secrets.token_urlsafe(16)
        csp = ("default-src 'none'; "
               f"script-src 'nonce-{nonce}'; "
               "style-src 'unsafe-inline'; "
               "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'")
        html = (_SPA
                .replace("__NONCE__", nonce)
                .replace("__TOKEN__", self.token)
                .replace("__THEME__", THEME_CSS)
                .replace("__MAPJS__", MAP_JS))
        self._send(200, html.encode(), "text/html; charset=utf-8",
                   {"Content-Security-Policy": csp,
                    "Referrer-Policy": "no-referrer"})


_SPA = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>engram</title>
<style>
__THEME__
* { box-sizing: border-box; margin: 0; }
html, body { height: 100%; }
body { background: var(--bg); color: var(--ink); overflow: hidden;
  font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif; }
.app { display: grid; grid-template-rows: auto 1fr; height: 100vh; }
header { display: flex; align-items: center; gap: 1rem; padding: 0.6rem 1rem;
  border-bottom: 1px solid var(--line); background: var(--surface); flex-wrap: wrap; }
header h1 { font-size: 1.05rem; font-weight: 680; letter-spacing: -0.01em; }
header h1 .dot { color: var(--accent); }
.stat { font-size: 0.76rem; color: var(--ink-3); }
.stat b { color: var(--ink); font-variant-numeric: tabular-nums; }
.grow { flex: 1; }
button.act { background: var(--surface-2); color: var(--ink); border: 1px solid var(--line);
  border-radius: 7px; padding: 0.35rem 0.7rem; font: inherit; font-size: 0.82rem; cursor: pointer; }
button.act:hover { border-color: var(--accent); }
button.act:disabled { opacity: 0.5; cursor: default; }
button.act.pri { background: var(--accent); color: #1a1206; border-color: var(--accent); font-weight: 600; }
.stage { position: relative; display: grid; grid-template-columns: 1fr 380px; min-height: 0; }
#map { display: block; width: 100%; height: 100%; cursor: grab; background:
  radial-gradient(120% 120% at 30% 20%, color-mix(in srgb, var(--accent) 6%, transparent), transparent 60%); }
.maptools { position: absolute; top: 0.7rem; left: 0.7rem; display: flex; gap: 0.4rem; align-items: center; }
.chip { border: 1px solid var(--line); background: var(--surface); color: var(--ink-2);
  border-radius: 999px; padding: 0.2rem 0.7rem; font: inherit; font-size: 0.78rem; cursor: pointer; }
.chip.on { border-color: var(--accent); color: var(--accent); }
#mapq { cursor: text; min-width: 12rem; }
.legend { position: absolute; bottom: 0.6rem; left: 0.7rem; display: flex; flex-wrap: wrap;
  gap: 0.3rem 0.8rem; font-size: 0.74rem; color: var(--ink-2); max-width: 60%; }
.legend span { display: inline-flex; align-items: center; gap: 0.3rem; }
.legend i { width: 0.65rem; height: 0.65rem; border-radius: 50%; }
#maptip { position: absolute; pointer-events: none; max-width: 22rem; z-index: 6;
  background: var(--ink); color: var(--bg); padding: 0.35rem 0.55rem; border-radius: 6px;
  font-size: 0.78rem; opacity: 0; transition: opacity 0.08s; }
.panel { border-left: 1px solid var(--line); background: var(--surface);
  display: grid; grid-template-rows: auto 1fr; min-height: 0; }
.tabs { display: flex; border-bottom: 1px solid var(--line); }
.tabs button { flex: 1; background: none; border: none; color: var(--ink-3); font: inherit;
  font-size: 0.8rem; padding: 0.6rem 0; cursor: pointer; border-bottom: 2px solid transparent; }
.tabs button.on { color: var(--ink); border-bottom-color: var(--accent); }
.tabbody { overflow: auto; padding: 0.8rem; min-height: 0; }
.tabbody[hidden] { display: none; }
.rev { border: 1px solid var(--accent); background: color-mix(in srgb, var(--accent) 8%, transparent);
  border-radius: 8px; padding: 0.6rem; margin-bottom: 0.7rem; font-size: 0.82rem; }
.rev .row { display: flex; gap: 0.4rem; margin-top: 0.5rem; }
.mem { border: 1px solid var(--line); border-radius: 8px; padding: 0.55rem 0.65rem;
  margin-bottom: 0.5rem; background: var(--surface-2); }
.mem.inv { opacity: 0.55; }
.mem .t { font-size: 0.85rem; }
.mem .meta { font-size: 0.72rem; color: var(--ink-3); margin-top: 0.25rem;
  display: flex; gap: 0.5rem; flex-wrap: wrap; align-items: center; }
.mem .tag { color: var(--accent-2); }
.mem .tools { display: none; gap: 0.35rem; margin-top: 0.5rem; flex-wrap: wrap; }
.mem.open .tools { display: flex; }
.mem input, .mem select { background: var(--bg); color: var(--ink); border: 1px solid var(--line);
  border-radius: 5px; padding: 0.2rem 0.4rem; font: inherit; font-size: 0.76rem; }
.field { display: block; width: 100%; margin-bottom: 0.5rem; }
textarea#add { width: 100%; min-height: 3.4rem; resize: vertical; background: var(--bg);
  color: var(--ink); border: 1px solid var(--line); border-radius: 7px; padding: 0.45rem; font: inherit; }
.chatlog { display: flex; flex-direction: column; gap: 0.5rem; }
.bub { padding: 0.5rem 0.7rem; border-radius: 10px; font-size: 0.85rem; white-space: pre-wrap;
  max-width: 92%; }
.bub.user { align-self: flex-end; background: var(--accent); color: #1a1206; }
.bub.assistant { align-self: flex-start; background: var(--surface-2); border: 1px solid var(--line); }
.bub .src { font-size: 0.7rem; color: var(--ink-3); margin-top: 0.3rem; }
.chatbar { display: flex; gap: 0.4rem; padding: 0.6rem; border-top: 1px solid var(--line); }
.chatbar input { flex: 1; background: var(--bg); color: var(--ink); border: 1px solid var(--line);
  border-radius: 7px; padding: 0.45rem 0.6rem; font: inherit; }
.docs { font-size: 0.82rem; line-height: 1.55; }
.docs h3 { font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--ink-3);
  margin: 1rem 0 0.35rem; }
.docs code { font-family: ui-monospace, Menlo, monospace; background: var(--surface-2);
  border: 1px solid var(--line); border-radius: 4px; padding: 0 0.3rem; font-size: 0.9em; }
.docs p { margin-bottom: 0.5rem; }
.toast { position: fixed; bottom: 1rem; left: 50%; transform: translateX(-50%); background: var(--ink);
  color: var(--bg); padding: 0.4rem 0.8rem; border-radius: 7px; font-size: 0.8rem; opacity: 0;
  transition: opacity 0.15s; z-index: 20; }
.toast.on { opacity: 1; }
.chatpane { display: grid; grid-template-rows: 1fr auto; min-height: 0; height: 100%; }
</style></head>
<body>
<div class="app">
  <header>
    <h1>engram<span class="dot">.</span></h1>
    <span class="stat" id="hstat">loading…</span>
    <span class="grow"></span>
    <button class="act" id="sleep" title="Run the idle consolidation pass now">Sleep now</button>
  </header>
  <div class="stage">
    <div style="position:relative">
      <canvas id="map"></canvas>
      <div class="maptools">
        <span class="chip" data-mode="scope">scope</span>
        <span class="chip" data-mode="type">type</span>
        <input type="search" id="mapq" class="chip" placeholder="highlight on map…">
      </div>
      <div id="maptip"></div>
      <div class="legend" id="legend"></div>
    </div>
    <div class="panel">
      <div class="tabs">
        <button data-tab="memories" class="on">Memories</button>
        <button data-tab="chat">Chat</button>
        <button data-tab="docs">Docs</button>
      </div>
      <div class="tabbody" data-body="memories">
        <div id="reviews"></div>
        <textarea id="add" placeholder="Remember something… (runs the full pipeline)"></textarea>
        <div style="display:flex;gap:0.4rem;margin:0.4rem 0 0.9rem">
          <input id="addscope" class="chip" style="flex:1" placeholder="scope (default)">
          <button class="act pri" id="addbtn">Remember</button>
        </div>
        <div id="memlist"></div>
      </div>
      <div class="tabbody" data-body="chat" hidden>
        <div class="chatpane">
          <div class="chatlog" id="chatlog"></div>
          <div class="chatbar">
            <input id="chatin" placeholder="Ask about your memories…" autocomplete="off">
            <button class="act pri" id="chatsend">Send</button>
          </div>
        </div>
      </div>
      <div class="tabbody docs" data-body="docs" hidden></div>
    </div>
  </div>
</div>
<div class="toast" id="toast"></div>
<script nonce="__NONCE__">
__MAPJS__
const TOKEN = "__TOKEN__";
const H = { "X-Engram-Token": TOKEN, "Content-Type": "application/json" };
const esc = s => { const d = document.createElement("div"); d.textContent = s ?? ""; return d.innerHTML; };
const $ = s => document.querySelector(s);
let STATE = null, MAP = null;

function toast(m) { const t = $("#toast"); t.textContent = m; t.classList.add("on");
  setTimeout(() => t.classList.remove("on"), 1800); }
async function api(path, body) {
  const r = await fetch(path, { method: "POST", headers: H, body: JSON.stringify(body) });
  if (!r.ok) { toast("error: " + (await r.text())); throw new Error(r.status); }
  return r.json();
}

async function load() {
  STATE = await (await fetch("/api/state", { headers: { "X-Engram-Token": TOKEN } })).json();
  const s = STATE.stats;
  $("#hstat").innerHTML = `<b>${s.points}</b> memories · <b>${Object.keys(s.shards||{}).length}</b> shards`
    + ` · ${esc(s.disk?.data||"")} on disk · ${esc(s.extraction||"")}`;
  MAP = initMap({ data: STATE, canvas: $("#map"), tip: $("#maptip"), legend: $("#legend"),
    colorButtons: document.querySelectorAll(".maptools [data-mode]"),
    onPick: m => { if (m) { switchTab("memories"); openMem(m.id); } } });
  renderReviews(); renderMems();
}

function renderReviews() {
  const box = $("#reviews");
  box.innerHTML = (STATE.reviews || []).map(r => `<div class="rev" data-seq="${r.seq}">
    <b>${esc(r.proposed_op)}?</b> "${esc(r.new.text)}"<br>vs "${esc(r.target.text)}"
    <div class="row"><button class="act pri" data-rev="accept">Accept</button>
    <button class="act" data-rev="reject">Reject</button></div></div>`).join("");
  box.querySelectorAll("[data-rev]").forEach(b => b.onclick = async () => {
    const seq = +b.closest(".rev").dataset.seq;
    await api("/api/review", { seq, accept: b.dataset.rev === "accept" });
    toast("review resolved"); await load();
  });
}

function memHtml(m) {
  return `<div class="mem ${m.valid ? "" : "inv"}" data-id="${esc(m.id)}">
    <div class="t">${esc(m.text)}</div>
    <div class="meta"><span>${esc(m.type)} · ${esc(m.scope)}</span>
      ${m.tags.length ? `<span class="tag">${esc(m.tags.join(", "))}</span>` : ""}
      <span>${m.access_count ? m.access_count + "× recalled" : ""}</span></div>
    <div class="tools">
      <input class="field ed-scope" value="${esc(m.scope)}" placeholder="scope">
      <input class="field ed-tags" value="${esc(m.tags.join(", "))}" placeholder="tags, comma-separated">
      <label class="field">importance <input class="ed-imp" type="number" min="0" max="1" step="0.1" value="${m.importance ?? 0.5}"></label>
      <button class="act pri" data-do="save">Save</button>
      <button class="act" data-do="soft">Soft-forget</button>
      <button class="act" data-do="hard" style="color:var(--danger)">Hard-forget</button>
    </div></div>`;
}
function renderMems() {
  const box = $("#memlist");
  box.innerHTML = STATE.memories.map(memHtml).join("");
  box.querySelectorAll(".mem").forEach(el => {
    el.querySelector(".t").onclick = () => el.classList.toggle("open");
    el.querySelectorAll("[data-do]").forEach(b => b.onclick = e => { e.stopPropagation(); memAction(el, b.dataset.do); });
  });
}
function openMem(id) {
  const el = $(`.mem[data-id="${CSS.escape(id)}"]`);
  if (el) { el.classList.add("open"); el.scrollIntoView({ block: "center", behavior: "smooth" });
    if (MAP) MAP.select(id); }
}
async function memAction(el, act) {
  const id = el.dataset.id;
  if (act === "save") {
    const tags = el.querySelector(".ed-tags").value.split(",").map(s => s.trim()).filter(Boolean);
    await api("/api/edit", { id, scope: el.querySelector(".ed-scope").value.trim() || null,
      tags, importance: parseFloat(el.querySelector(".ed-imp").value) });
    toast("saved"); await load();
  } else if (act === "soft" || act === "hard") {
    if (act === "hard" && !confirm("Hard-forget purges the bytes and rebuilds the shard. Irreversible. Continue?")) return;
    await api("/api/forget", { id, mode: act }); toast(act + "-forgot"); await load();
  }
}

$("#addbtn").onclick = async () => {
  const text = $("#add").value.trim(); if (!text) return;
  await api("/api/remember", { text, scope: $("#addscope").value.trim() || "default" });
  $("#add").value = ""; toast("remembered"); await load();
};
$("#sleep").onclick = async () => {
  const b = $("#sleep"); b.disabled = true; b.textContent = "Sleeping… (writes pause)";
  try { const r = await api("/api/consolidate", {});
    toast(`slept: ${r.pruned} pruned, ${r.deduped} deduped, ${r.summarized} summarized`); await load(); }
  finally { b.disabled = false; b.textContent = "Sleep now"; }
};

// map highlight box
$("#mapq").addEventListener("input", e => { if (MAP) MAP.setFilter(e.target.value); });

// tabs
function switchTab(name) {
  document.querySelectorAll(".tabs button").forEach(b => b.classList.toggle("on", b.dataset.tab === name));
  document.querySelectorAll(".tabbody").forEach(x => x.hidden = x.dataset.body !== name);
}
document.querySelectorAll(".tabs button").forEach(b => b.onclick = () => switchTab(b.dataset.tab));

// chat — streamed NDJSON; retrieved memories light up on the map
const history = [];
async function sendChat() {
  const inp = $("#chatin"), msg = inp.value.trim(); if (!msg) return;
  inp.value = "";
  const log = $("#chatlog");
  log.insertAdjacentHTML("beforeend", `<div class="bub user">${esc(msg)}</div>`);
  const bub = document.createElement("div"); bub.className = "bub assistant"; bub.textContent = "";
  log.appendChild(bub); log.scrollTop = log.scrollHeight;
  let answer = "";
  const res = await fetch("/api/chat", { method: "POST", headers: H,
    body: JSON.stringify({ message: msg, history }) });
  const reader = res.body.getReader(), dec = new TextDecoder(); let buf = "";
  for (;;) {
    const { done, value } = await reader.read(); if (done) break;
    buf += dec.decode(value, { stream: true });
    let nl; while ((nl = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, nl); buf = buf.slice(nl + 1);
      if (!line.trim()) continue;
      const o = JSON.parse(line);
      if (o.used && MAP) { MAP.highlight(new Set(o.used));
        const names = o.used.map(id => (STATE.memories.find(m => m.id === id) || {}).text).filter(Boolean);
        if (names.length) bub.insertAdjacentHTML("afterend",
          `<div class="bub assistant src">grounded in ${names.length} memories (lit on the map)</div>`); }
      if (o.token) { answer += o.token; bub.textContent = answer; log.scrollTop = log.scrollHeight; }
    }
  }
  history.push({ role: "user", content: msg }, { role: "assistant", content: answer });
}
$("#chatsend").onclick = sendChat;
$("#chatin").addEventListener("keydown", e => { if (e.key === "Enter") sendChat(); });

$(".docs").innerHTML = `
  <h3>What this is</h3>
  <p>A private, local memory for your AI assistants. Your memories live in a folder you
  own (<code>~/.engram</code>); nothing leaves this machine.</p>
  <h3>How it works</h3>
  <p>A SQLite <b>journal</b> is the source of truth; the vector index (Qdrant Edge) is a
  rebuildable projection over it. On write, text is redacted, facts extracted, and a local
  model decides add / update / supersede / noop against what's already known.</p>
  <p>The <b>map</b> plots every memory by meaning — nearby dots are semantically similar.
  Color is scope or type; dots fade with age. Scroll to zoom, drag to pan, hover to read.</p>
  <h3>This app</h3>
  <p><b>Chat</b> answers from your memories (the ones it used light up on the map).
  <b>Sleep now</b> runs consolidation: prune stale episodes, dedupe, summarize — writes
  pause while it runs. Edit scope/tags/importance inline; a text correction is a new
  <code>remember</code> (the model supersedes the old one).</p>
  <h3>Commands</h3>
  <p><code>engram remember</code> · <code>recall</code> · <code>forget</code> ·
  <code>list</code> · <code>review</code> · <code>consolidate</code> ·
  <code>dashboard</code> (static) · <code>serve</code> (this) · <code>--help</code> for all.</p>`;

load();
</script>
</body></html>
"""
