#!/usr/bin/env python3
"""
persMEM Dashboard v2.1 — Read-only viewer + AMQ compose for Claude's memory.
Light-mode only. Binds DASHBOARD_HOST:DASHBOARD_PORT (default 127.0.0.1:8767). Every
request under /api/ (reads included) and every POST needs the dashboard token
(DASHBOARD_POST_TOKEN_PATH); the page shell, /static/, /tokens.css and /fonts/ carry no data.

All site-specific facts (paths, bind address, external service URLs, the agent
roster and the operator mailbox) are configuration; docs/LAYOUT.md lists the
environment variables.

v2.1 changes (April 22 2026):
  - System status bar (uptime, service health, memory/news/AMQ counts, model name)
  - AMQ compose box (send messages directly from browser to any agent)
  - Export buttons (memories JSON/Markdown, news JSON)
v2.0 changes (April 22 2026):
  - Auto-discover AMQ mailboxes from filesystem
  - Per-agent color theming
  - News collection display
  - Per-card copy buttons
  - Server-side minimal markdown rendering (no external deps)
  - Memory pagination (50 per page)
  - Stacked activity timeline (memories + AMQ + news per day)
  - Fixed: Chart.js legend color contrast, cache-control on main route
"""

import os
import re
import sys
import json
import html
import hmac
from datetime import datetime, timezone
from collections import Counter

from flask import Flask, Response, jsonify, make_response, render_template, request, send_from_directory, stream_with_context
import requests
import time
import threading

# --- Config (every site-specific value is an env var with a generic default) ---
HERE = os.path.dirname(os.path.abspath(__file__))

# Names and defaults are docs/LAYOUT.md's (canonical names only; a site sets its values in the
# unit's environment). The dashboard reads these; the memory server owns them.
AMQ_ROOT = os.environ.get("MEMORY_AMQ_ROOT", "/var/lib/memory-amq")
# The memory server's /dashboard/* routes (docs/LAYOUT.md): memories, boot history, edits and sending
# as the operator all go through them. The dashboard never opens the vector store or the history
# directory (one process on the live store; the Release dashboard user cannot read either). The
# bearer is scoped to those routes and lives in the unit's EnvironmentFile, never in a unit file.
MEMORY_URL = os.environ.get("MEMORY_URL", "http://127.0.0.1:8765").rstrip("/")
DASHBOARD_SECRET = os.environ.get("DASHBOARD_SECRET", "")
MEMORY_TIMEOUT_S = 10
MEMORY_CACHE_S = 5          # app.js polls /api/system every second; the server is asked once per 5 s
ITEMS_FULL_PAGE, ITEMS_META_PAGE = 500, 50000   # the server's caps per call
# --- Agent roster: defined once, in the roster JSON (LAYOUT rule 2) ---
# COTERIE_AGENTS_JSON overrides; default is the layout's path, then the
# repo-relative copy (../config/agents.json next to this directory, the release tree's shape).
BIRDS_JSON = os.environ.get("COTERIE_AGENTS_JSON", "/etc/coterie/agents.json")
BIRDS_JSON_REPO = os.path.normpath(os.path.join(HERE, "..", "config", "agents.json"))
# Operator mailbox (the human who composes from the dashboard). The roster file's
# optional "operator" key wins; this env var is the fallback.
OPERATOR_DEFAULT = os.environ.get("DASHBOARD_OPERATOR", "operator")
# Write token: every POST (compose, chat) must carry `Authorization: Bearer <token>`, the
# token being the contents of this root-installed file (owner root, group = the dashboard's
# user, mode 0640). Read once at startup. An absent, unreadable or empty file refuses every
# POST: the reads stay open to whoever reaches the bind address, the writes never are.
POST_TOKEN_PATH = os.environ.get("DASHBOARD_POST_TOKEN_PATH", "/etc/dashboard/post.token")
HOST = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
PORT = int(os.environ.get("DASHBOARD_PORT", "8767"))
PAGE_SIZE = 50

# Shared webfonts. The portal's web root is the one copy both surfaces serve from; the
# dashboard reaches it through the /fonts/ route below (the dashboard runs on loopback and
# cannot assume the front door).
TOKENS_CSS = os.environ.get("DASHBOARD_TOKENS_CSS", "/srv/portal/portal/tokens.css")   # the portal's web root (lib.sh PORTAL_DIR)
FONTS_DIR = os.environ.get("DASHBOARD_FONTS_DIR", "/srv/portal/portal/fonts")

app = Flask(
    __name__,
    static_folder=os.path.join(HERE, "static"),
    static_url_path="/static",
    template_folder=os.path.join(HERE, "templates"),
)


# --- Auto-discover AMQ mailboxes ---
def discover_agents():
    """Every directory under AMQ_ROOT with an inbox/. These are MAILBOXES (retired
    agents, service mailboxes and the operator included) -- not the roster; see
    load_roster()."""
    agents = []
    try:
        for name in sorted(os.listdir(AMQ_ROOT)):
            inbox = os.path.join(AMQ_ROOT, name, "inbox")
            if os.path.isdir(inbox):
                agents.append(name)
    except OSError:
        pass
    return agents


def load_flock():
    """Parsed roster file ({"site"?, "operator"?, "birds": [...]}) from BIRDS_JSON
    (env COTERIE_AGENTS_JSON or the layout default), else the repo-relative copy,
    else {} so every consumer degrades to an empty roster."""
    for path in (BIRDS_JSON, BIRDS_JSON_REPO):
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        return data if isinstance(data, dict) else {}
    return {}


def load_roster(flock=None):
    """Ordered list of active agents from the roster file: [{name, color, label}, ...]."""
    if flock is None:
        flock = load_flock()
    roster = []
    # Retired seats: either a top-level "retired": [names] list (the form the memory
    # server's portal roster uses) or a per-row "retired": true / "enabled": false.
    retired = set(flock.get("retired") or [])
    for b in flock.get("birds") or flock.get("agents") or []:
        name = b.get("name")
        if (not name or name in retired or b.get("retired")
                or b.get("active") is False or b.get("enabled") is False):
            continue
        roster.append({
            "name": name,
            "color": b.get("color") or b.get("colour"),
            # The HUE is what the token system consumes: tokens.css derives
            # --id-wash/--id-text/--id-fill from it and retunes them per theme, which
            # a raw hex cannot do. `color` stays for anything still reading it.
            "hue": b.get("hue"),
            "label": b.get("label") or name,
        })
    return roster


def operator_name(flock=None):
    """Mailbox name of the human operator: roster file "operator" key, else
    DASHBOARD_OPERATOR, else "operator"."""
    if flock is None:
        flock = load_flock()
    return flock.get("operator") or OPERATOR_DEFAULT


# --- Write gate ---
def _load_post_token(path=None):
    """Token bytes from POST_TOKEN_PATH, stripped. b"" when the file is missing, unreadable
    or empty; check_post_auth() then refuses everything (fail closed, never open)."""
    try:
        with open(path or POST_TOKEN_PATH, "rb") as f:
            return f.read().strip()
    except OSError:
        return b""


POST_TOKEN = _load_post_token()


def check_post_auth(auth_header, token=None):
    """Constant-time bearer compare. Empty stored token = always False."""
    token = POST_TOKEN if token is None else token
    if not token:
        return False
    if not isinstance(auth_header, str) or not auth_header.startswith("Bearer "):
        return False
    provided = auth_header[7:].strip().encode()
    if not provided:
        return False
    return hmac.compare_digest(token, provided)


@app.before_request
def _gate_api():
    """Every request under /api/ (reads included) and every POST anywhere needs the token.
    The page shell (/), /static/, /tokens.css and /fonts/ stay open: they carry no data.

    Until 2026-09-25 only POSTs were gated and reads were "open to whoever reaches the bind
    address"; on a multi-user host that is every local account, and with a LAN bind it is the
    LAN, and /api/export/memories.json is the whole store (found on review, verified from three
    seats). Loopback is not a trust boundary on this host (the same sentence chorusd learned
    2026-09-16). The token is the one already in the operator's browser."""
    if (request.path.startswith("/api/") or request.method == "POST") \
            and not check_post_auth(request.headers.get("Authorization", "")):
        return no_cache(make_response(jsonify({"ok": False, "error": "dashboard token required"}), 401))


_gate_writes = _gate_api  # pre-2026-09-25 name, kept for the tests and docs that cite it


# A mailbox name as it may appear in a filesystem path under AMQ_ROOT. Roster membership is
# the real gate (see api_amq_send); this shape check is the second lock, so a roster file
# carrying an odd name still cannot build a path outside its own directory.
MAILBOX_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def mailbox_name_ok(name):
    return isinstance(name, str) and bool(MAILBOX_NAME_RE.match(name))


# --- Memory server client (/dashboard/*) ---
class MemoryUnavailable(Exception):
    """The memory server could not answer: unreachable, refused the bearer, or the bearer is unset.
    Routes answer this loudly (502 / persmem_ok false), never with an empty list: an unreachable
    server and an empty collection must stay distinguishable (the 2026-09 lesson of the old
    bare `return []`)."""


def _memory(route, body=None):
    """POST one /dashboard/<route> call and return its JSON. Raises MemoryUnavailable."""
    if not DASHBOARD_SECRET:
        raise MemoryUnavailable("DASHBOARD_SECRET is not set for the dashboard")
    try:
        r = requests.post(f"{MEMORY_URL}/dashboard/{route}", json=body or {},
                          headers={"Authorization": f"Bearer {DASHBOARD_SECRET}"}, timeout=MEMORY_TIMEOUT_S)
    except requests.RequestException as exc:
        raise MemoryUnavailable(f"memory server unreachable: {exc.__class__.__name__}")
    if r.status_code != 200:
        try:
            err = r.json().get("error", "")
        except ValueError:
            err = ""
        raise MemoryUnavailable(f"memory server answered {r.status_code} on {route}: {err}")
    return r.json()


_CACHE = {}
_CACHE_LOCK = threading.Lock()


def _cached(key, fn, ttl=MEMORY_CACHE_S):
    """fn() at most once per ttl seconds per key. A failure is not cached."""
    now = time.monotonic()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    value = fn()
    with _CACHE_LOCK:
        _CACHE[key] = (now, value)
    return value


def memory_collections():
    """{collections: {name: count}, model, embed_fp, server_commit, maildir_problems}; cached."""
    def _fetch():
        r = _memory("collections")
        return {"collections": {c["name"]: c["count"] for c in r.get("collections", [])},
                "model": r.get("model") or "unknown", "embed_fp": r.get("embed_fp"),
                "server_commit": r.get("server_commit"), "maildir_problems": r.get("maildir_problems") or []}
    return _cached("collections", _fetch)


def get_all_from_collection(name, fields="full"):
    """Every row of a collection, newest first, in the flat shape the routes always used
    ({id, content?, **metadata}; bootstrap rows also carry review_after, parsed by the server).
    Walked with the server's `before` cursor, so a store landing mid-walk neither repeats nor
    skips a row; "meta" drops content (stats and activity read only metadata)."""
    page = ITEMS_FULL_PAGE if fields == "full" else ITEMS_META_PAGE
    out, before = [], None
    while True:
        body = {"collection": name, "fields": fields, "limit": page}
        if before:
            body["before"] = before
        r = _memory("items", body)
        for it in r.get("items", []):
            row = {"id": it.get("id"), **(it.get("metadata") or {})}
            if fields == "full":
                row["content"] = it.get("content") or ""
            if "review_after" in it:
                row["review_after"] = it["review_after"]
            out.append(row)
        nxt = r.get("next")
        if not nxt or not r.get("items"):
            return out
        if nxt == before:   # a server bug must not spin this walk forever (review, 2026-10-01)
            raise MemoryUnavailable("memory server cursor did not advance")
        before = nxt


def memory_edits():
    """Every recorded bootstrap edit, newest first (the server reads the sidecars); cached."""
    return _cached("edits", lambda: _memory("edits", {"limit": 2000}).get("edits", []))


def memory_boots(recent=20):
    """{agent: {count, recent: [{at, sha, lane}]}} for the roster's active agents; cached."""
    return _cached(("boots", recent), lambda: _memory("boots", {"recent": recent}).get("boots", {}))


def memory_error(exc, status=502):
    return no_cache(make_response(jsonify({"ok": False, "error": str(exc)}), status))


# --- Minimal Markdown renderer (no external deps) ---
def mini_markdown(text):
    """Convert common markdown patterns to HTML. Covers ~95% of our content."""
    if not text:
        return ""
    t = html.escape(text)

    # Code blocks (``` ... ```)
    def code_block(m):
        code = m.group(2)
        lang = m.group(1) or ""
        return f'<pre><code class="lang-{lang}">{code}</code></pre>'
    t = re.sub(r'```(\w*)\n(.*?)```', code_block, t, flags=re.DOTALL)

    # Inline code
    t = re.sub(r'`([^`]+)`', r'<code>\1</code>', t)

    # Headers (after code blocks so # inside code isn't matched)
    t = re.sub(r'^#### (.+)$', r'<h5>\1</h5>', t, flags=re.MULTILINE)
    t = re.sub(r'^### (.+)$', r'<h4>\1</h4>', t, flags=re.MULTILINE)
    t = re.sub(r'^## (.+)$', r'<h4>\1</h4>', t, flags=re.MULTILINE)
    t = re.sub(r'^# (.+)$', r'<h3>\1</h3>', t, flags=re.MULTILINE)

    # Bold and italic
    t = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', t)
    t = re.sub(r'\*(.+?)\*', r'<em>\1</em>', t)

    # Bullet lists (simple, non-nested)
    def bullet_list(m):
        items = m.group(0).strip().split('\n')
        lis = ''.join(f'<li>{re.sub(r"^- ", "", li)}</li>' for li in items if li.strip())
        return f'<ul>{lis}</ul>'
    t = re.sub(r'(?:^- .+\n?)+', bullet_list, t, flags=re.MULTILINE)

    # Numbered lists
    def num_list(m):
        items = m.group(0).strip().split('\n')
        lis = ''.join(f'<li>{re.sub(r"^[0-9]+[.)] ", "", li)}</li>' for li in items if li.strip())
        return f'<ol>{lis}</ol>'
    t = re.sub(r'(?:^[0-9]+[.)] .+\n?)+', num_list, t, flags=re.MULTILINE)

    # Line breaks (but not inside block elements)
    t = re.sub(r'\n', '<br>\n', t)
    # Clean up extra <br> after block elements
    t = re.sub(r'(</(?:pre|ul|ol|h[3-5]|li)>)<br>', r'\1', t)
    t = re.sub(r'<br>\n(<(?:pre|ul|ol|h[3-5]))', r'\n\1', t)

    return t


def strip_html(text):
    """Strip HTML tags from RSS/feed content to clean readable text."""
    if not text:
        return ""
    # Replace <br>, <br/>, <p> with newlines
    t = re.sub(r'<br\s*/?>|</p>', '\n', text)
    # Replace <li> with bullet points
    t = re.sub(r'<li[^>]*>', '• ', t)
    # Strip all remaining tags
    t = re.sub(r'<[^>]+>', '', t)
    # Decode common HTML entities
    t = html.unescape(t)
    # Clean up excessive whitespace
    t = re.sub(r'\n{3,}', '\n\n', t)
    t = re.sub(r' {2,}', ' ', t)
    return t.strip()


# --- AMQ reader ---
def get_amq_messages():
    messages = []
    agents = discover_agents()
    for agent in agents:
        for subdir in ["new", "cur"]:
            dirpath = os.path.join(AMQ_ROOT, agent, "inbox", subdir)
            try:
                files = os.listdir(dirpath)
            except OSError:
                continue
            for fname in files:
                if not fname.endswith(".md"):
                    continue
                filepath = os.path.join(dirpath, fname)
                try:
                    with open(filepath, "r") as f:
                        content = f.read()
                    parts = content.split("---json\n", 1)
                    if len(parts) < 2:
                        continue
                    rest = parts[1].split("\n---\n", 1)
                    if len(rest) < 2:
                        continue
                    headers = json.loads(rest[0])
                    headers["body"] = rest[1].strip()
                    headers["body_html"] = mini_markdown(rest[1].strip())
                    headers["status"] = "unread" if subdir == "new" else "read"
                    headers["recipient"] = agent
                    messages.append(headers)
                except Exception:
                    continue
    messages.sort(key=lambda m: m.get("created", ""), reverse=True)
    return messages


# --- Activity data for stacked timeline ---
def get_activity_data(memories, news_items, amq_messages):
    """Build per-day counts for memories, news, and AMQ messages."""
    days = {}
    for m in memories:
        day = m.get("stored_at", "")[:10]
        if day:
            days.setdefault(day, {"memories": 0, "news": 0, "amq": 0})
            days[day]["memories"] += 1
    for n in news_items:
        day = n.get("stored_at", "")[:10]
        if day:
            days.setdefault(day, {"memories": 0, "news": 0, "amq": 0})
            days[day]["news"] += 1
    for a in amq_messages:
        day = a.get("created", "")[:10]
        if day:
            days.setdefault(day, {"memories": 0, "news": 0, "amq": 0})
            days[day]["amq"] += 1
    return dict(sorted(days.items()))


# ============================
# HTML TEMPLATE
# ============================



def safe_json(data):
    """JSON-encode for safe embedding inside HTML <script> tags.
    Escapes </ sequences that would prematurely close script blocks."""
    return json.dumps(data, default=str).replace("</", r"<\/")


# ============================
# ============================
# Routes
# ============================

def no_cache(resp):
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp


@app.route("/")
def dashboard():
    """v3 shell — pure client-side rendering. JS fetches data from /api/* endpoints."""
    return no_cache(make_response(render_template("dashboard.html")))
# review_after comes parsed from the server with every bootstrap row (/dashboard/items); the
# dashboard keeps no copy of the parser since 2026-10-01 (it mirrored memory's for a week).


def review_status(date_str, today=None):
    """overdue | due-soon (14d) | ok | none -- what the panel actually sorts on."""
    if not date_str:
        return "none", None
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return "none", None
    today = today or datetime.now(timezone.utc).date()
    days = (d - today).days
    return ("overdue" if days < 0 else "due-soon" if days <= 14 else "ok"), days


@app.route("/api/edits")
def api_edits():
    """Bootstrap edits, newest first: the pre-image sidecar per edit, read by the server."""
    try:
        edits = memory_edits()
    except MemoryUnavailable as exc:
        return memory_error(exc)
    return no_cache(make_response(jsonify({"total": len(edits), "edits": edits[:40]})))


@app.route("/api/boots")
def api_boots():
    """Per-agent boot integrity: last boot, last manifest hash, whether it drifted
    from the boot before it, and -- the receipt -- whether a recorded edit explains
    the drift. A manifest hash that changes with no edit recorded between the two
    boots is the signal; one that changes after an edit is the system working."""
    agents = [a.get("name") for a in load_roster()]
    try:
        edits = [e["at"] for e in memory_edits() if e.get("at")]
        boots = memory_boots(20)
    except MemoryUnavailable as exc:
        return memory_error(exc)
    out = []
    for a in agents:
        if not a:
            continue
        # the server's records, oldest first, in the (iso, sha, lane) shape this view always used
        log = [(b["at"], b["sha"], b.get("lane")) for b in (boots.get(a) or {}).get("recent", [])]
        last = log[-1] if log else None
        # The boot before it in the same lane. A lane-less (pre-2026-09-29) boot
        # pairs with any lane, so the first boot after the change still compares.
        prev = None
        if last:
            prev = next((b for b in reversed(log[:-1])
                         if b[2] is None or last[2] is None or b[2] == last[2]), None)
        drifted = (last[1] != prev[1]) if (last and prev) else None
        between = None
        if last and prev:
            between = sum(1 for t in edits if prev[0] < t <= last[0])
        out.append({
            "agent": a,
            # the server's total, not the 20-record window this view derives drift from (2026-10-01,
            # the live catch: every agent with more than 20 boots read as 20)
            "boots": (boots.get(a) or {}).get("count", len(log)),
            "last_boot": last[0] if last else None,
            "last_hash": last[1] if last else None,
            "last_lane": last[2] if last else None,
            # drift is only meaningful with something to compare against
            "drifted": drifted,
            "edits_between": between,
            "explained": (between > 0) if (drifted and between is not None) else None,
            "distinct_recent": len({b[1] for b in log[-5:]}),
            "history": [{"at": t, "sha": h, "lane": ln} for t, h, ln in log[-5:]],
        })
    return no_cache(make_response(jsonify(out)))


# The orchestrator's doorbell ledger: one JSON line per event (seen, ring, sent, answered
# with latency, re-ring). World-readable where chorusd writes it; the site names the path.
DOORBELL_LEDGER = os.environ.get("DASHBOARD_DOORBELL_LEDGER", "/var/lib/chorusd/doorbell.log")


def _ledger_tail(path, max_bytes=512 * 1024):
    """(records, error). The last max_bytes of the ledger, parsed; a partial first line
    is dropped. Reading the tail keeps this O(1) in the ledger's age."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            data = fh.read()
    except OSError as e:
        return None, str(e)[:200]
    lines = data.decode("utf-8", "replace").splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]
    recs = []
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict) and isinstance(r.get("t"), str):
            recs.append(r)
    return recs, None


def _ledger_time(t):
    """The ledger stamps local wall-clock time without a zone (time.strftime), so it is
    compared against local now, never UTC."""
    try:
        return datetime.strptime(t[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


@app.route("/api/rings")
def api_rings():
    """Doorbell receipts per roster agent from the orchestrator's ledger: rings in the
    last day and week, answers and their measured latency, re-rings, last ring/answer."""
    recs, err = _ledger_tail(DOORBELL_LEDGER)
    if recs is None:
        return jsonify({"error": err, "ledger": DOORBELL_LEDGER, "agents": [], "recent": []}), 503
    now = datetime.now()
    roster = [a.get("name") for a in load_roster() if a.get("name")]
    per = {a: {"agent": a, "rings_24h": 0, "rings_7d": 0, "answered_7d": 0, "rerings_7d": 0,
               "last_ring": None, "last_answer": None, "last_latency": None, "_lat": []}
           for a in roster}
    recent = []
    for r in recs:
        t = _ledger_time(r.get("t", ""))
        b, ev = r.get("bird"), r.get("event") or ""
        if t is None or b not in per:
            continue
        age = (now - t).total_seconds()
        p = per[b]
        if ev == "ring":
            if age <= 86400:
                p["rings_24h"] += 1
            if age <= 7 * 86400:
                p["rings_7d"] += 1
            p["last_ring"] = r["t"]
            recent.append({"t": r["t"], "event": ev, "agent": b, "senders": r.get("senders") or [], "n": r.get("n")})
        elif ev == "answered":
            lat = r.get("latency")
            if age <= 7 * 86400:
                p["answered_7d"] += 1
                if isinstance(lat, (int, float)):
                    p["_lat"].append(float(lat))
            p["last_answer"] = r["t"]
            p["last_latency"] = lat
            recent.append({"t": r["t"], "event": ev, "agent": b, "latency": lat})
        elif ev.startswith("re-ring") or ev.startswith("rering"):
            if age <= 7 * 86400:
                p["rerings_7d"] += 1
            recent.append({"t": r["t"], "event": "re-ring", "agent": b})
    agents = []
    for a in roster:
        p = per[a]
        lat = sorted(p.pop("_lat"))
        p["median_latency_7d"] = lat[len(lat) // 2] if lat else None
        p["max_latency_7d"] = lat[-1] if lat else None
        agents.append(p)
    all_lat = sorted(l for a in agents for l in [a["median_latency_7d"]] if l is not None)
    return no_cache(make_response(jsonify({
        "ledger": DOORBELL_LEDGER, "records": len(recs),
        "first": recs[0]["t"] if recs else None, "last": recs[-1]["t"] if recs else None,
        "totals": {
            "rings_24h": sum(a["rings_24h"] for a in agents),
            "rings_7d": sum(a["rings_7d"] for a in agents),
            "answered_7d": sum(a["answered_7d"] for a in agents),
            "rerings_7d": sum(a["rerings_7d"] for a in agents),
            "median_of_medians_7d": all_lat[len(all_lat) // 2] if all_lat else None,
        },
        "agents": agents, "recent": sorted(recent, key=lambda r: r["t"])[-30:][::-1],
    })))


# Hook & Shield's own variables (the dashboard displays hook state; it does not own it).
HOOK_DETECT_LOG = os.path.join(os.environ.get("SHIELD_LOG_DIR", "/var/log/hook-detect"), "hook-detect.log")
HOOK_BASELINES = os.environ.get("SHIELD_BASELINES_FILE", "/opt/shield/hook_baselines.yaml")


def _hook_covered():
    """Agents with a baseline row, read from the yaml without a yaml parser.

    Needed because the scanner logs FINDINGS ONLY: an agent with no line in a run
    was either clean or never covered, and those are different answers. Coverage
    is what separates them.
    """
    import re as _re
    try:
        with open(HOOK_BASELINES, encoding="utf-8") as fh:
            return set(_re.findall(r"/home/([a-z_][a-z0-9_-]*)/\.claude/settings\.json", fh.read()))
    except OSError:
        return set()


@app.route("/api/hooks")
def api_hooks():
    """Hook & Shield status per agent, read from the scanner's log.

    It is READ FROM A LOG and not computed, and that is not laziness: every
    agent's home is mode 700, so this process cannot open anybody's
    settings.json to hash it. Only a root-run scan can, and this is its output.

    The consequence the card must show: `scanned_at` can be old. A stale CLEAN is
    not the same claim as a fresh one, and an audit page that renders them
    identically is lying by omission.
    """
    import re as _re
    # ONLY the last run. The log goes back to August and holds findings that were
    # fixed weeks ago; letting any historical IOC stick reports a month-old alert as
    # current state, which is worse than showing nothing. Runs are delimited by
    # "hook-detect starting" / "hook-detect complete".
    try:
        with open(HOOK_DETECT_LOG, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError as exc:
        return jsonify({"error": str(exc), "scanned_at": None, "agents": []}), 503

    start = 0
    for n, line in enumerate(lines):
        if "hook-detect starting" in line:
            start = n
    run = lines[start:]

    per, last, verdict = {}, None, None
    for line in run:
        m = _re.match(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)", line)
        if m:
            last = m.group(1)
        if "hook-detect complete" in line:
            verdict = line.split("complete:", 1)[-1].strip()
        p_m = _re.search(r"/home/([a-z_][a-z0-9_-]*)/\.claude/settings\.json", line)
        if not p_m:
            continue
        agent = p_m.group(1)
        # last mention in the run wins, whatever it says
        if "IOC FOUND" in line or "mismatch" in line:
            per[agent] = {"state": "ALERT", "at": last, "detail": line.strip()[-90:]}
        else:
            per[agent] = {"state": "CLEAN", "at": last, "detail": None}

    covered = _hook_covered()
    roster = [a.get("name") for a in load_roster()]
    agents = []
    for a in roster:
        if a in per:
            agents.append(dict(agent=a, **per[a]))
        elif a in covered and last:
            # covered, the run completed, and it said nothing about this agent
            agents.append({"agent": a, "state": "CLEAN", "at": last, "detail": None})
        else:
            agents.append({"agent": a, "state": "UNCOVERED", "at": None,
                           "detail": "no baseline row in hook_baselines.yaml"})
    # Age is the point: a stale CLEAN is not the same claim as a fresh one.
    age_days = None
    if last:
        try:
            age_days = (datetime.now(timezone.utc).date()
                        - datetime.strptime(last[:10], "%Y-%m-%d").date()).days
        except ValueError:
            pass
    return jsonify({"scanned_at": last, "age_days": age_days, "verdict": verdict,
                    "log": HOOK_DETECT_LOG, "agents": agents})


@app.route("/api/reviews")
def api_reviews():
    """Entries carrying a review-after date, worst first.

    Scoped to the bootstrap collection on purpose. review-after is the discipline
    that governs identity, state and directives -- the entries every agent boots
    on -- and that is where it is used as metadata rather than discussed. Across
    the 3,266-entry memories collection only 19 bodies carry a parseable date and
    almost all are AMQ messages talking ABOUT the convention.
    """
    # An audit panel that reports "nothing overdue" when it could not read the
    # database is worse than no panel: it answers the question wrongly, in the
    # reassuring direction. Fail loudly instead.
    try:
        # full rows: the title is the entry's first line; the content never leaves this process
        entries = get_all_from_collection("bootstrap", "full")
    except MemoryUnavailable as exc:
        return jsonify({"error": str(exc), "reviews": [], "overdue": None, "due_soon": None}), 503

    rows = []
    for e in entries:
        date = e.get("review_after")   # parsed by the server, one parser (2026-10-01)
        status, days = review_status(date)
        if status == "none":
            continue
        rows.append({
            "id": e.get("id"), "type": e.get("type"), "review_after": date,
            "status": status, "days": days,
            "title": (e.get("content") or "")[:80].split("\n")[0],
        })
    order = {"overdue": 0, "due-soon": 1, "ok": 2}
    rows.sort(key=lambda r: (order[r["status"]], r["days"]))
    return jsonify({"reviews": rows,
                    "overdue": sum(1 for r in rows if r["status"] == "overdue"),
                    "due_soon": sum(1 for r in rows if r["status"] == "due-soon")})


@app.route("/tokens.css")
def tokens_css():
    """The shared token file, at the SAME path the portal serves it from.

    One file, one path, two surfaces -- a per-surface copy is exactly the drift this
    system exists to remove. It lives in the portal tree because that is the copy
    Caddy already serves; DASHBOARD_TOKENS_CSS overrides for another layout.
    """
    d, f = os.path.split(TOKENS_CSS)
    return send_from_directory(d, f, mimetype="text/css", max_age=0)


@app.route("/fonts/<path:filename>")
def fonts(filename):
    """Serve the shared webfonts at /fonts/ so tokens.css resolves on this host too.

    tokens.css is served to BOTH surfaces and its @font-face src is root-relative
    (/fonts/...), which resolves against the host, not against the stylesheet. Caddy
    serves /fonts/ from the portal root for free; Flask is pinned to /static, so
    without this route the dashboard 404s every face and silently falls back to
    ui-sans-serif while the portal looks correct -- an asymmetric failure nobody
    would think to check. deploy.sh probes both hosts for this reason.

    The files live in the portal tree, which is the one copy both surfaces share.
    DASHBOARD_FONTS_DIR overrides for a layout that puts them elsewhere.
    """
    return send_from_directory(FONTS_DIR, filename, max_age=31536000)


@app.route("/api/memories")
def api_memories():
    try:
        return jsonify(get_all_from_collection("memories"))
    except MemoryUnavailable as exc:
        return memory_error(exc)


@app.route("/api/news")
def api_news():
    """RSS content arrives as raw HTML; strip to clean text before returning."""
    try:
        items = get_all_from_collection("news")
    except MemoryUnavailable as exc:
        return memory_error(exc)
    for n in items:
        if n.get("content"):
            n["content"] = strip_html(n["content"])
    return jsonify(items)


@app.route("/api/stats")
def api_stats():
    try:
        memories = get_all_from_collection("memories", "meta")
        collections = memory_collections()["collections"]
    except MemoryUnavailable as exc:
        return memory_error(exc)
    projects = Counter(m.get("project", "general") for m in memories)
    types = Counter(m.get("type", "note") for m in memories)
    tags = Counter()
    for m in memories:
        raw = m.get("tags", "")
        if raw:
            for t in raw.split(","):
                t = t.strip()
                if t:
                    tags[t] += 1
    return jsonify({
        "total": len(memories),
        # legacy keys (kept for live dashboard)
        "projects": dict(projects),
        "types": dict(types),
        # new namespaced keys (v3 frontend)
        "by_project": dict(projects),
        "by_type": dict(types),
        "by_tag": dict(tags),
        "collections": [{"name": n, "count": c} for n, c in sorted(collections.items())],
    })


@app.route("/api/amq")
def api_amq():
    data = get_amq_messages()
    return no_cache(make_response(jsonify(data)))


@app.route("/api/amq/send", methods=["POST"])
def api_amq_send():
    """Send an AMQ message from the dashboard (operator -> agent).

    The sender is the operator mailbox, decided here and never by the caller: a body
    `from` other than that name is refused, so nobody who can reach the port can ring an
    agent's doorbell under another agent's name. Recipients come from the roster file
    (`to` is one roster name, or "all" for every roster agent); a name that is not on the
    roster, or does not look like a mailbox name, never becomes a path. The write token is
    checked before this runs (see _gate_api).
    """
    def refuse(msg, code=400):
        return no_cache(make_response(jsonify({"ok": False, "error": msg}), code))

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return refuse("JSON object body required")
    flock = load_flock()
    sender = operator_name(flock)
    if not mailbox_name_ok(sender):
        return refuse("operator mailbox name is not a valid mailbox name", 500)
    if data.get("from") not in (None, "", sender):
        return refuse("sender is fixed server-side (the operator mailbox)")
    to_agent = data.get("to", "")
    subject = data.get("subject", "") or ""
    body = data.get("body", "") or ""
    if not isinstance(to_agent, str) or not isinstance(subject, str) or not isinstance(body, str):
        return refuse("to, subject and body must be strings")
    if not to_agent or not body:
        return refuse("to and body required")

    roster = [a["name"] for a in load_roster(flock) if mailbox_name_ok(a.get("name"))]
    if to_agent == "all":
        recipients = [a for a in roster if a != sender]
        if not recipients:
            return refuse("no roster agents to broadcast to")
    elif to_agent in roster and to_agent != sender:
        recipients = [to_agent]
    else:
        return refuse("recipient is not on the roster")

    # Delivery is the memory server's (/dashboard/send: the operator fixed there too, maildir.py's
    # group rule, the same auto-store as amq_send). This process never writes a mailbox (2026-10-01).
    try:
        res = _memory("send", {"to": to_agent, "subject": subject, "body": body})
    except MemoryUnavailable as exc:
        return refuse(str(exc), 502)
    delivered = res.get("delivered") or []
    # Backward-compatible response shape: single-recipient fields plus the `delivered` list.
    return no_cache(make_response(jsonify({
        "ok": True,
        "from": res.get("from") or sender,
        "to": to_agent,
        "id": delivered[0]["id"] if len(delivered) == 1 else None,
        "delivered": delivered,
        "count": len(delivered),
    })))


@app.route("/api/system")
def api_system():
    """System status for the status bar."""
    try:
        # Uptime
        with open("/proc/uptime") as f:
            up_secs = float(f.read().split()[0])
        days = int(up_secs // 86400)
        hours = int((up_secs % 86400) // 3600)
        mins = int((up_secs % 3600) // 60)
        if days > 0:
            uptime_str = f"{days}d {hours}h {mins}m"
        elif hours > 0:
            uptime_str = f"{hours}h {mins}m"
        else:
            uptime_str = f"{mins}m"

        # Collection counts, the model name and "memory server up", from one cached call
        memory_err = None
        try:
            mc = memory_collections()
            col_dict, model_name = mc["collections"], mc["model"]
        except MemoryUnavailable as exc:
            col_dict, model_name, memory_err = {}, "unknown", str(exc)
        memories = col_dict.get("memories", 0)
        news = col_dict.get("news", 0)

        # Roster (who is in the flock) vs mailboxes (which AMQ dirs exist)
        flock = load_flock()
        roster = load_roster(flock)
        operator = operator_name(flock)
        mailboxes = discover_agents()

        # AMQ count
        amq_count = 0
        for agent in mailboxes:
            for subdir in ["new", "cur"]:
                dirpath = os.path.join(AMQ_ROOT, agent, "inbox", subdir)
                try:
                    amq_count += len([f for f in os.listdir(dirpath) if f.endswith(".md")])
                except OSError:
                    pass

        # persMEM service check: the collections call above is the real one (2026-10-01; this
        # was a hard-coded True until then)
        persmem_ok = memory_err is None

        return no_cache(make_response(jsonify({
            # legacy keys (kept for live dashboard)
            "uptime": uptime_str,
            "memories": memories,
            "news": news,
            "amq": amq_count,
            "model": model_name,
            # new keys (v3 frontend)
            "uptime_sec": int(up_secs),
            "memories_total": memories,
            "news_total": news,
            "amq_total": amq_count,
            "model_name": model_name,
            "agents": [b["name"] for b in roster],
            "mailboxes": mailboxes,
            "roster": roster,
            "operator": operator,
            "site": flock.get("site") or "",
            "persmem_ok": persmem_ok,
            "memory_error": memory_err,
            "maildir_problems": mc["maildir_problems"] if memory_err is None else [],
        })))
    except Exception as e:
        return jsonify({"uptime": "error", "persmem_ok": False, "error": str(e)})


@app.route("/api/activity")
def api_activity():
    """Fresh activity data for sparkline auto-refresh."""
    try:
        memories = get_all_from_collection("memories", "meta")
        news_items = get_all_from_collection("news", "meta")
    except MemoryUnavailable as exc:
        return memory_error(exc)
    amq_messages = get_amq_messages()
    activity = get_activity_data(memories, news_items, amq_messages)
    return no_cache(make_response(jsonify(activity)))


if __name__ == "__main__":
    # Under systemd stdout is a stream, so Python block-buffers it and the startup lines
    # below reach the journal only at exit (review of 473302d; the memory server had the same).
    sys.stdout.reconfigure(line_buffering=True)
    agents = discover_agents()
    flock = load_flock()
    print(f"persMEM Dashboard v3 starting on http://{HOST}:{PORT}")
    print(f"  Memory server: {MEMORY_URL}/dashboard/* ({'bearer loaded' if DASHBOARD_SECRET else 'DASHBOARD_SECRET EMPTY -- every data route answers 502'})")
    print(f"  AMQ root: {AMQ_ROOT}")
    print(f"  Roster ({BIRDS_JSON}): {', '.join(b['name'] for b in load_roster(flock)) or '(none)'}")
    print(f"  Operator mailbox: {operator_name(flock)}")
    print(f"  Mailboxes discovered: {', '.join(agents) or '(none)'}")
    print(f"  Write token ({POST_TOKEN_PATH}): {'loaded, ' + str(len(POST_TOKEN)) + ' bytes' if POST_TOKEN else 'EMPTY -- every POST answers 401'}")
    print(f"  Doorbell ledger ({DOORBELL_LEDGER}): {'readable' if os.access(DOORBELL_LEDGER, os.R_OK) else 'absent or unreadable -- Rings panel reports it'}")
    app.run(host=HOST, port=PORT, debug=False, threaded=True)
