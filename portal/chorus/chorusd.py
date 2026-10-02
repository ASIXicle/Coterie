#!/usr/bin/env python3
"""chorusd -- Bird Portal chorus orchestrator (stage 3, v3).

v3 (2026-09-03, successor review): the agent list comes from the roster
(reloaded on SIGHUP), the early-stop check compares message identities rather
than file paths (a Maildir read renames new/<id> to cur/<id>:2,S, which v2
counted as new traffic, so early stop never fired), and every chorus carries
a four-hex id in its prompts so a Stop hook that reports the id it saw can be
matched against the chorus it belongs to. Hooks without an id still work.

  ROUND-ROBIN (default):
    Round 0: fire birds ONE AT A TIME in sequence order. First bird gets the
      initial [CHORUS] prompt; each later bird gets the follow-up variant
      ("<priors> already processed this -- check AMQ FIRST, build on their
      analysis"). Wait for each bird's Stop hook before firing the next.
    Rounds 1..N: walk the same sequence with [AMQ-CHECK], one bird at a time.
    Early stop: if a full AMQ-CHECK round delivers no new AMQ message, the
      exchange is dry -- finish early (checked via `sudo -u <AMQ_USER> find`
      over the maildirs; degrades to fixed rounds if that rule is absent).

  SIMULTANEOUS (secondary, the extension's other mode): fire all at once,
    wait for all Stop hooks, repeat.

Listens on 127.0.0.1:8766 (CHORUSD_PORT). Portal reaches it via Caddy /chorus/*; birds'
Stop hooks POST /hook directly. Runs as the orchestrator named in
the roster; types into other agents via a wrapper-only sudoers rule.

  DOORBELL (v4, 2026-09-10, two agents + the coordinator, operator's ask): a bird-to-bird
    wake-up. Every DOORBELL_POLL seconds the same read-only `sudo find` that
    powers early-stop is used to notice files appearing under
    <bird>/inbox/new/. A new file is a message that no session has read.
    chorusd then types "[AMQ-CHECK #id] Doorbell: ..." into that bird's pane
    with the same send-keys rule the chorus uses -- the recipient's running
    session is prompted; no operator fire needed.  Rules, all in one place:
      * global ON/OFF from the portal (POST /doorbell), persisted in
        doorbell.json beside this file; default ON (operator 2026-09-10).
        OFF = today's behaviour: mail waits for the next human prompt.
      * never rings while a chorus is active (the chorus already walks
        AMQ-CHECK rounds); never rings a bird that is mid-turn (busy from a
        UserPromptSubmit hook or from our own typing, cleared by the Stop
        hook, or by BUSY_TIMEOUT if a hook goes missing). Held mail is
        coalesced into one ring on the next idle.
      * per-agent cooldown between rings (roster "doorbell_cooldown",
        default DOORBELL_COOLDOWN s) and a loop guard per sender->recipient
        pair (roster "doorbell_pair_max" / "doorbell_pair_window", defaults
        DOORBELL_PAIR_MAX per DOORBELL_PAIR_WINDOW s; per-recipient override on
        the bird entry). v5 (2026-09-11, portal author, after the loop guard silently
        dropped three messages in one evening): over-budget mail is DEFERRED,
        not dropped — it re-rings when the pair window has room ("re-ring
        reason=window-cleared"), is dropped only when read elsewhere, and
        expires after DOORBELL_DEFERRED_TTL; a rung message still unread after
        DOORBELL_RERING_AFTER with no turn-end (a pane that never processed the
        ring: usage limit, restart) is re-rung ONCE on the bird's next Stop hook
        ("re-ring reason=no-turn-end"). Every path is one ledger line. Guarded
        mail is not lost: it stays in new/ for the next human prompt.
      * everything the doorbell sees and decides is appended as one JSON
        line to doorbell.log (world-readable) -- the audit source.
    Backlog present when chorusd starts is baselined, not rung (logged).
"""
import json
import os
import secrets
import signal
import subprocess
import re
import threading
import time
import pwd
import sys
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# systemd runs us with stdout as a pipe to journald; Python defaults to block-buffered on
# a non-TTY pipe, so `print()` without flush=True vanishes into the buffer until it fills
# or the process exits. Reconfigure to line-buffered so every `print(...)` flushes on \n
# — fence and auth rejects already pass flush=True and are fine; this closes the diagnostic
# gap on the startup and roster prints so operators can read them in `journalctl -u chorusd`.
sys.stdout.reconfigure(line_buffering=True)

HOST, PORT = "127.0.0.1", int(os.environ.get("CHORUSD_PORT") or 8766)
# The roster (docs/LAYOUT.md rule 2): the ONLY place agent names exist; every reader reads "agents".
BIRDS_FILE = os.environ.get("COTERIE_AGENTS_JSON") or "/etc/coterie/agents.json"
AGENT_TIMEOUT = 480   # per-bird ceiling; a stuck bird is skipped, not fatal
SEND_GAP = 2          # breath between a bird finishing and the next firing
MAX_BODY = 64 * 1024  # a prompt, not a payload
BP_OPEN, BP_CLOSE = "\x1b[200~", "\x1b[201~"  # bracketed paste guards newlines

# The agents' mail root (docs/LAYOUT.md rule 3): one Maildir per mailbox under it, LISTED by
# this daemon (group amq-poll: names carry sender + timestamp; bodies are never opened here).
# CHORUSD_AMQ_USER is the pre-layout sudo-find path for a root under another user's 700 home;
# it goes when the mail root migrates (memory owner's change) and is not on the layout page.
AMQ_ROOT = os.environ.get("CHORUSD_AMQ_ROOT") or "/var/lib/memory-amq"
AMQ_USER = os.environ.get("CHORUSD_AMQ_USER", "")
_amq_ok = None   # last probe: True/False; None until the first probe (CUTOVER #10: announce, never silently degrade)
DOORBELL_POLL = 3          # seconds between maildir looks
DOORBELL_COOLDOWN = 90     # default min gap between rings to one bird
DOORBELL_PAIR_MAX = 8      # rings per sender->recipient pair ... (v5: was 4; two birds working
DOORBELL_PAIR_WINDOW = 1800  # ... per this many seconds (loop guard)   together exceeded 4 in normal use)
DOORBELL_RERING_AFTER = 600  # v5: a rung message still unread after this long with no turn-end
                             #     is re-rung ONCE, on the bird's next Stop hook
DOORBELL_DEFERRED_TTL = 86400  # v5: deferred mail older than this expires — a bound on the queue,
                               #     not a delivery deadline (2 windows starved under a saturated budget)
BUSY_TIMEOUT = 480         # a DOORBELL turn with no Stop hook this long is assumed idle again
BUSY_TIMEOUT_PROMPT = 1800 # v5.1: a human-driven turn (why=prompt) runs long; measured peaks past 8 min
# State lives OUTSIDE the code dir (docs/LAYOUT.md rule 1): the daemon must not be able to
# replace its own code, and its ledger/budget/payloads must survive a code deploy.
STATE_DIR = os.environ.get("CHORUSD_STATE_DIR") or "/var/lib/chorusd"
DOORBELL_STATE_FILE = os.path.join(STATE_DIR, "doorbell.json")
DOORBELL_LEDGER = os.path.join(STATE_DIR, "doorbell.log")
# Cold boot, v6 (2026-09-27): the init prompt is NOT carried. /matron types a pointer to a
# section of a file at a commit in the recipient's OWN git clone; the recipient fetches and
# reads it there. No payload file, no drop directory, no ACL. Why: Claude Code marks a bracketed
# paste as <pasted_content> and an agent follows it only on the operator's standing word
# (~/.claude/CLAUDE.md; config/dispatch-block.example.md is its template); an "inspect the file
# another process dropped in /tmp, then run it" step also tripped a model-side safeguard. Git
# is the better provenance anyway: a reviewed, content-addressed commit instead of a transient
# file. Every field is validated here, and the wrapper still refuses control bytes.
# Dependency this creates: the init prompts live in a git repo every agent has cloned, and the
# commit must reach each clone through a shared remote (a forge, or a bare repo on this host).
# The pointer reads the local clone first and fetches only when the commit is missing, so the
# remote is needed only when a clone is behind. It affects availability, never content: the
# commit id pins the bytes. Callers should send the full 40-hex id.
MATRON_REPO_RE = re.compile(r"~(?:/[A-Za-z0-9_-][A-Za-z0-9._-]*)+")        # a clone in the agent's home; no ./.. segment
MATRON_FILE_RE = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]*(?:/[A-Za-z0-9_-][A-Za-z0-9._-]*)*")  # repo-relative, no ".."
MATRON_COMMIT_RE = re.compile(r"[0-9a-f]{7,40}")
MATRON_LINES_RE = re.compile(r"([1-9][0-9]{0,5})-([1-9][0-9]{0,5})")   # the block's lines at that commit
MATRON_LINES_SPAN = 2000   # an init prompt, not a file
MATRON_SECTION_MAX = 120   # one markdown heading line
MATRON_NOTE_MAX = 600      # a short note from the Matron, one paragraph


def _one_line(s, limit):
    """True when s is printable text on one line within limit (no control bytes, no C1)."""
    return bool(s) and len(s) <= limit and not any(ord(ch) < 0x20 or 0x7f <= ord(ch) <= 0x9f for ch in s)


def matron_prompt(body, cid):
    """Validate a /matron body and build the pointer. Returns (status, response, prompt|None).
    Body: bird, repo (a clone under ~/), file (repo-relative), commit (hex), lines ("a-b", the
    block inside its fence at that commit), section (the heading the block sits under, for the
    agent to confirm), note (optional), sender. The commit pins the file, so the lines are exact, and
    the agent checks it is on origin/main (v6.2, review F3a 2026-09-29: a fetch brings every
    branch, so without the check a commit that never reached main would boot)."""
    b = (body.get("bird") or "").strip()
    repo = (body.get("repo") or "").strip()
    path = (body.get("file") or "").strip()
    commit = (body.get("commit") or "").strip().lower()
    lines = (body.get("lines") or "").strip()
    section = (body.get("section") or "").strip()
    note = (body.get("note") or "").strip()
    sender = (body.get("sender") or "matron").strip()
    if b not in ALL_BIRDS:
        return 400, {"error": "unknown bird"}, None
    if body.get("text"):
        return 400, {"error": "text is retired (v6): send repo, file, commit and section; the agent reads its own clone"}, None
    if not MATRON_REPO_RE.fullmatch(repo):
        return 400, {"error": "repo must be a clone under the agent's home, e.g. ~/repos/<name>"}, None
    if not MATRON_FILE_RE.fullmatch(path) or ".." in path.split("/"):
        return 400, {"error": "file must be a repo-relative path"}, None
    if not MATRON_COMMIT_RE.fullmatch(commit):
        return 400, {"error": "commit must be 7-40 hex"}, None
    m = MATRON_LINES_RE.fullmatch(lines)
    if not m or not (int(m.group(1)) <= int(m.group(2)) <= int(m.group(1)) + MATRON_LINES_SPAN):
        return 400, {"error": f"lines must be 'a-b' with a <= b <= a+{MATRON_LINES_SPAN}"}, None
    if not (_one_line(section, MATRON_SECTION_MAX) and section.startswith("#")):
        return 400, {"error": f"section must be one markdown heading line (<= {MATRON_SECTION_MAX} chars)"}, None
    if note and not _one_line(note, MATRON_NOTE_MAX):
        return 400, {"error": f"note must be one line (<= {MATRON_NOTE_MAX} chars)"}, None
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,32}", sender):
        return 400, {"error": "sender must be a short name"}, None
    prompt = (
        f"[MATRON INIT — {b.upper()}] #{cid}\n"
        f"From {sender}. Your init prompt is lines {lines} of {path} at commit {commit} in your "
        f"clone {repo}: the fenced block under the heading \"{section}\". "
        f"Read it there: git -C {repo} show {commit}:{path} | sed -n '{m.group(1)},{m.group(2)}p' "
        f"(if your clone doesn't have that commit yet, run git -C {repo} fetch -q first). "
        f"It must be on main: git -C {repo} merge-base --is-ancestor {commit} origin/main must exit 0 "
        f"(if it doesn't, fetch and check once more; if it still doesn't, stop and tell the operator)\n"
        f"Boot from that block. When your first moves are done, AMQ {sender} your "
        f"chorus_manifest sha, your unread AMQ count and any anomalies."
    )
    if note:
        prompt += f"\nNote from {sender}: {note}"
    return 200, {"ok": True, "bird": b, "cid": cid, "commit": commit, "lines": lines,
                 "section": section, "bytes": len(prompt)}, prompt


PANE_KEY_WHITELIST = frozenset(["/clear"])   # server-enforced; adding entries = code change + review
PANE_KEY_COOLDOWN = 60                        # seconds between /pane-key calls on the same bird
PANE_KEY_ENABLED = os.environ.get("CHORUSD_PANE_KEY_ENABLED") == "1"   # kill switch; off in code, on in the unit env
PANE_KEY_TOKEN_PATH = os.environ.get("CHORUSD_PANE_KEY_TOKEN_PATH") or "/etc/chorusd/pane-key.token"


def _load_pane_key_token():
    """Read the token bytes from PANE_KEY_TOKEN_PATH. Empty on unreadable — auth rejects all /pane-key
    calls in that state (review 2026-09-16: loopback alone is not a trust boundary on this host)."""
    try:
        with open(PANE_KEY_TOKEN_PATH, "rb") as f:
            return f.read().strip()
    except OSError:
        return b""


PANE_KEY_TOKEN = _load_pane_key_token()

# The page's routes (/fire, /doorbell, /abort) answer only requests that came through the front
# door: Caddy, past the portal password, adds X-Coterie-Front with a secret only it and this daemon
# hold. Without it, any local account could POST /fire on loopback and type a [CHORUS] line, which
# the agents take as the operator's words, into every pane (adversarial pass, 2026-10-02). The
# loopback callers keep their own routes: /hook (the Stop hook), /matron and /pane-key (bearer token).
FRONT_TOKEN_PATH = os.environ.get("CHORUSD_FRONT_TOKEN_PATH") or "/etc/chorusd/front.token"
FRONT_ROUTES = ("fire", "doorbell", "abort")


def _load_front_token():
    try:
        with open(FRONT_TOKEN_PATH, "rb") as f:
            return f.read().strip()
    except OSError:
        return b""     # unreadable or missing: the page's routes refuse everything


FRONT_TOKEN = _load_front_token()


def came_through_front_door(headers):
    """Constant-time compare of X-Coterie-Front with the stored secret. Empty stored secret = False."""
    provided = (headers.get("X-Coterie-Front") or "").strip().encode()
    return bool(FRONT_TOKEN) and bool(provided) and hmac.compare_digest(FRONT_TOKEN, provided)


def check_pane_key_auth(auth_header):
    """Constant-time bearer-token compare. Empty stored token = always False."""
    if not PANE_KEY_TOKEN:
        return False
    if not isinstance(auth_header, str) or not auth_header.startswith("Bearer "):
        return False
    provided = auth_header[7:].strip().encode()
    if not provided:
        return False
    return hmac.compare_digest(PANE_KEY_TOKEN, provided)


def is_proxied_request(headers):
    """True if the request appears to have come through Caddy's reverse_proxy — Caddy 2.6+
    adds X-Forwarded-For, X-Forwarded-Proto and X-Forwarded-Host by default on every hop
    (all three checked so an operator `header_up -X-Forwarded-For` later still trips this).
    A direct loopback caller doesn't set them. See handler §proxy fence. Extracted so tests
    can exercise via a plain dict stub, but callers must pass the real self.headers
    (HTTPMessage, case-insensitive lookup) — dict tests exercise the logic; the case-
    insensitivity property is a receipt from real HTTPMessage exercise (test T11 covers)."""
    return (bool(headers.get("X-Forwarded-For"))
            or bool(headers.get("X-Forwarded-Proto"))
            or bool(headers.get("X-Forwarded-Host")))

def post_refusal(headers):
    """(status, reason) when a POST must be refused before any route runs, else None.

    Every POST route takes JSON. A browser cannot send application/json to another site without a
    CORS preflight, and this server answers none, so the type check alone stops a page the operator
    happens to open from firing a round, flipping the doorbell or aborting (a form or a text/plain
    fetch is a "simple" request that needs no preflight). The Origin check is the second lock: a
    browser sends Origin on every POST, and it must name the address the request came in on. The
    loopback callers (the Stop hook, /matron, /pane-key) send JSON and no Origin."""
    ctype = (headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    if ctype != "application/json":
        return 415, "POST takes Content-Type: application/json"
    origin = headers.get("Origin")
    if origin is not None:
        proto, host = headers.get("X-Forwarded-Proto"), headers.get("X-Forwarded-Host")
        if not (proto and host) or origin != f"{proto}://{host}":
            return 403, "cross-origin POST refused"
    return None

lock = threading.Lock()
state = {
    "active": False, "mode": "roundrobin", "round": 0, "rounds": 0,
    "sequence": [], "idx": 0, "waiting": [], "message": "", "cid": "",
    "log": [], "deadline": 0,
    "doorbell": {"on": True, "busy": {}, "last_ring": {}, "pending": {},
                 "rung": {}, "deferred": {}, "rings": 0, "log": [],
                 # bird -> "session" | "credits", set by the Stop hook when a turn
                 # ended blocked, cleared by the next turn that did not. A pane
                 # whose agent hit a usage limit used to look exactly like an idle
                 # one; this is the only thing that tells them apart.
                 "limit": {}},
}
amq_mark = None  # message-id snapshot at the start of the current AMQ round
ME, ALL_BIRDS = "", []  # set by load_birds from the roster's "orchestrator"
BIRD_COOLDOWN = {}  # name -> seconds, from the roster's "doorbell_cooldown"
PAIR_MAX, PAIR_WINDOW = DOORBELL_PAIR_MAX, DOORBELL_PAIR_WINDOW  # v5: roster top-level overrides
BIRD_PAIR_MAX = {}  # v5: recipient -> rings per pair per window, roster "doorbell_pair_max"
_seen_new = None    # set of <bird>/inbox/new paths at the last poll (None = no baseline yet)
_pair_rings = {}    # (from, to) -> [timestamps]


def log(msg):
    state["log"] = (state["log"] + [f"{time.strftime('%H:%M:%S')} {msg}"])[-60:]


# ---- the roster ------------------------------------------------------------------

def load_birds(*_):
    """Read the roster (COTERIE_AGENTS_JSON). Called at start and on SIGHUP; a bad file keeps the old list."""
    global ME, ALL_BIRDS, PAIR_MAX, PAIR_WINDOW, SESSION
    try:
        with open(BIRDS_FILE) as f:
            cfg = json.load(f)
        birds = [b["name"] for b in cfg["agents"]]
        me = cfg.get("orchestrator") or ME
        if not birds:
            raise ValueError(f"roster has no agents: {cfg}")
        runner = pwd.getpwuid(os.getuid()).pw_name
        if me not in birds and me != runner:
            raise ValueError(f"orchestrator {me!r} is neither a bird nor the running user {runner!r}")
    except Exception as e:
        with lock:
            log(f"roster NOT loaded ({e}); keeping {ALL_BIRDS or 'nothing'}")
        return
    with lock:
        ME, ALL_BIRDS = me, birds
        sess = str(cfg.get("session") or "agent")
        if re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", sess):
            SESSION = sess
        BIRD_COOLDOWN.clear()
        BIRD_PAIR_MAX.clear()
        try:
            PAIR_MAX = max(1, int(cfg.get("doorbell_pair_max", DOORBELL_PAIR_MAX)))
            PAIR_WINDOW = max(60, int(cfg.get("doorbell_pair_window", DOORBELL_PAIR_WINDOW)))
        except (TypeError, ValueError):
            PAIR_MAX, PAIR_WINDOW = DOORBELL_PAIR_MAX, DOORBELL_PAIR_WINDOW
        for b in cfg["agents"]:
            try:
                BIRD_COOLDOWN[b["name"]] = max(0, int(b.get("doorbell_cooldown", DOORBELL_COOLDOWN)))
            except (TypeError, ValueError):
                BIRD_COOLDOWN[b["name"]] = DOORBELL_COOLDOWN
            if "doorbell_pair_max" in b:
                try:
                    BIRD_PAIR_MAX[b["name"]] = max(1, int(b["doorbell_pair_max"]))
                except (TypeError, ValueError):
                    pass
        log(f"agents: {' '.join(birds)} (orchestrator {me})")   # shown in the portal's chorus drawer: the public word
    db_log("budget", pair_max=PAIR_MAX, pair_window=PAIR_WINDOW, per_bird=dict(BIRD_PAIR_MAX),
           rering_after=DOORBELL_RERING_AFTER, deferred_ttl=DOORBELL_DEFERRED_TTL)


def pair_max_for(bird):
    return BIRD_PAIR_MAX.get(bird, PAIR_MAX)


def pair_room(sender, bird, now):
    """RING EVENTS still allowed for sender->bird in the current window (prunes the history).
    v5.1 (review): the unit is rings, not messages — a loop is ring → turn → reply → ring,
    so rings bound it; one ring carrying four messages is a batch, not four loop steps. The
    outage was two rings carrying four messages counted as four."""
    key = (sender, bird)
    hist = [t for t in _pair_rings.get(key, []) if now - t < PAIR_WINDOW]
    _pair_rings[key] = hist
    return pair_max_for(bird) - len(hist), len(hist)


# ---- delivery ----------------------------------------------------------------

WRAPPER = os.environ.get("CHORUSD_SEND_KEYS_TO", "/usr/local/bin/send-keys-to")
SESSION = "agent"  # overwritten from the roster's "session" by load_birds
_pane_key_last = {}   # bird -> unix-ts of last successful /pane-key (cooldown state)


def tmux_type(bird, text):
    """Type text + Enter into the bird's pane through the sudoers-granted wrapper and nothing
    else (cutover item 11: the rule names /usr/local/bin/send-keys-to, two arguments, literal
    keystrokes; the wrapper owns the paste framing and refuses control bytes). The pre-wrapper
    direct-tmux fallback was removed 2026-09-25 with the tmux sudoers rule it served."""
    pre = [] if bird == ME else ["sudo", "-n", "-u", bird]
    if not os.path.exists(WRAPPER):
        raise RuntimeError(f"send-keys-to wrapper missing at {WRAPPER}")
    r = subprocess.run(pre + [WRAPPER, SESSION, text], capture_output=True, text=True, timeout=40)
    if r.returncode != 0:
        raise RuntimeError(f"send-keys-to failed ({r.returncode}): {(r.stderr or '').strip()[:160]}")


def send_async(bird, text, delay=0):
    def run():
        if delay:
            time.sleep(delay)
        try:
            tmux_type(bird, text)
            with lock:
                log(f"fired -> {bird} via=wrapper")
                db_log("sent", bird=bird, via="wrapper", bytes=len(text))
        except Exception as e:
            with lock:
                log(f"SEND FAILED {bird}: {e}")
                db_log("send-failed", bird=bird, error=str(e)[:120])
            bird_done(bird, note="unreachable")
    threading.Thread(target=run, daemon=True).start()


# ---- prompts (verbatim from the extension, names adapted; the id is new) -------

def wrap_initial(message, cid):
    return (f"[CHORUS #{cid}] {message}\n\n"
            "After processing, write your key thoughts/analysis to AMQ "
            "(amq_send from yourself to the other instances) so they can "
            "read and respond. Then answer normally.")


def wrap_followup(message, priors, cid):
    names = " and ".join(p.capitalize() for p in priors)
    return (f"[CHORUS #{cid}] {message}\n\n"
            f"IMPORTANT: {names} already processed this prompt and wrote "
            "analysis to AMQ. Check your AMQ inbox FIRST (amq_check + "
            "amq_read), then build on their analysis rather than duplicating "
            "work. Write your additional thoughts/analysis to AMQ, then "
            "answer normally.")


def amq_check(cid):
    return (f"[AMQ-CHECK #{cid}] Check your AMQ inbox (amq_check). Read and respond to "
            "any messages from other birds via amq_send. If no new messages, "
            "reply: No new AMQ messages.")


def prompt_for(rnd, idx, seq, message, cid):
    if rnd == 0:
        return wrap_initial(message, cid) if idx == 0 else wrap_followup(message, seq[:idx], cid)
    return amq_check(cid)


# ---- AMQ early-stop probe ------------------------------------------------------

def amq_paths():
    """All AMQ message file paths (a listing, never a body); None if unavailable. Records the
    outcome in _amq_ok so the startup line and /doorbell can say DISABLED instead of going quiet."""
    global _amq_ok
    if not AMQ_ROOT:
        _amq_ok = False
        return None
    try:
        pre = ["sudo", "-n", "-u", AMQ_USER] if AMQ_USER else []
        out = subprocess.run(
            pre + ["/usr/bin/find", AMQ_ROOT, "-type", "f"],
            capture_output=True, text=True, timeout=10)
        if out.returncode != 0:
            _amq_ok = False
            return None
        _amq_ok = True
        return out.stdout.split()
    except Exception:
        _amq_ok = False
        return None


def amq_snapshot():
    """Set of AMQ message identities; None if unavailable.

    Identity = the file's basename with any Maildir flag suffix (':2,S') removed,
    so a message that is merely READ (new/X -> cur/X:2,S) is the same identity
    and a round in which birds only read their mail counts as dry.
    """
    paths = amq_paths()
    return None if paths is None else frozenset(message_ids(paths))


def message_ids(paths):
    for p in paths:
        base = p.rsplit("/", 1)[-1]
        yield base.split(":", 1)[0]


# ---- doorbell -------------------------------------------------------------------

def db_log(event, **kw):
    """One line in the in-memory ring log and one JSON line in the ledger file."""
    rec = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **kw}
    d = state["doorbell"]
    d["log"] = (d["log"] + [rec])[-80:]
    try:
        with open(DOORBELL_LEDGER, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def doorbell_load():
    try:
        with open(DOORBELL_STATE_FILE) as f:
            on = bool(json.load(f).get("on", True))
    except Exception:
        on = True
    with lock:
        state["doorbell"]["on"] = on
    db_log("start", on=on)


def doorbell_set(on):
    with lock:
        state["doorbell"]["on"] = bool(on)
    try:
        with open(DOORBELL_STATE_FILE, "w") as f:
            json.dump({"on": bool(on)}, f)
    except Exception as e:
        db_log("state-file-error", error=str(e))
    db_log("toggle", on=bool(on))


def new_mail(paths):
    """Map bird -> [(message-id, sender)] for files under <bird>/inbox/new/."""
    out = {}
    for p in paths:
        rel = p[len(AMQ_ROOT) + 1:] if p.startswith(AMQ_ROOT + "/") else p
        parts = rel.split("/")
        if len(parts) != 4 or parts[1] != "inbox" or parts[2] != "new":
            continue
        bird, fname = parts[0], parts[3]
        mid = fname.split(":", 1)[0]
        bits = mid.split("_")
        sender = bits[1] if len(bits) >= 3 else "?"
        out.setdefault(bird, []).append((mid, sender))
    return out


def mark_busy(bird, why):
    state["doorbell"]["busy"][bird] = {"since": time.time(), "why": why}


def mark_idle(bird):
    state["doorbell"]["busy"].pop(bird, None)


def is_busy(bird, now):
    b = state["doorbell"]["busy"].get(bird)
    if not b:
        return False
    limit = BUSY_TIMEOUT_PROMPT if b["why"] == "prompt" else BUSY_TIMEOUT
    if now - b["since"] > limit:
        mark_idle(bird)
        db_log("busy-timeout", bird=bird, why=b["why"])
        return False
    return True


def pane_key_check(bird, cmd, now):
    """Pre-flight for POST /pane-key. Returns (status, response_body, ledger_kwargs_or_None).

    Kill switch, type checks, roster, whitelist, busy, cooldown — in that order. The
    handler holds `lock` for the whole check + fire sequence, and on (200, ...) it sets
    _pane_key_last[bird]=now and calls send_async(bird, cmd). Split out from the handler so
    tests can exercise the every rejection branch without going through HTTP (test_pane_key.py).

    A rejection with `ledger_kwargs is not None` should be logged as `pane-key-rejected`;
    kill switch, type, and roster failures stay pre-ledger (probe noise + configuration
    signals belong in the systemd journal, not the doorbell audit trail)."""
    if not PANE_KEY_ENABLED:
        return (503, {"error": "pane-key disabled: set CHORUSD_PANE_KEY_ENABLED=1 in the unit environment"}, None)
    if not isinstance(bird, str) or not isinstance(cmd, str):
        return (400, {"error": "bird and cmd must be strings"}, None)
    if bird not in ALL_BIRDS:
        return (400, {"error": "unknown bird", "bird": bird}, None)
    if cmd not in PANE_KEY_WHITELIST:
        return (403,
                {"error": "cmd not in whitelist", "cmd": cmd, "allowed": sorted(PANE_KEY_WHITELIST)},
                {"reason": "whitelist"})
    if is_busy(bird, now):
        return (409, {"error": "bird busy", "bird": bird}, {"reason": "busy"})
    last = _pane_key_last.get(bird, 0)
    if now - last < PANE_KEY_COOLDOWN:
        retry = max(1, int(PANE_KEY_COOLDOWN - (now - last)))
        return (429, {"error": "cooldown", "cmd": cmd, "retry_after": retry},
                {"reason": "cooldown", "retry_after": retry})
    return (200, None, None)


def doorbell_prompt(cid, msgs):
    senders = sorted({s for _, s in msgs})
    n = len(msgs)
    return (f"[AMQ-CHECK #{cid}] Doorbell: {n} new AMQ message{'s' if n != 1 else ''} "
            f"from {', '.join(senders)}. Read them (amq_check, then amq_read each). "
            "Reply by amq_send only if a reply is owed. If nothing is owed, answer: "
            "doorbell acknowledged.")
# The [CTFO] convention (a subject tagged [CTFO] is a terminal answer: the recipient
# replies to it only if something is still owed) lives in each agent's own boot text,
# not in the ring. Teaching it on every ring would add noise to the rings that
# deliberately want a back-and-forth; the sender's tag in the subject is the signal.


def doorbell_tick():
    """One poll: notice new mail, decide per bird, ring or hold. Returns sends."""
    global _seen_new
    paths = amq_paths()
    if paths is None:
        return []
    now = time.time()
    cur = {p for p in paths if "/inbox/new/" in p}
    with lock:
        d = state["doorbell"]
        if _seen_new is None:
            _seen_new = cur
            with_mail = new_mail(sorted(cur))
            for bird in ALL_BIRDS:
                db_log("baseline", bird=bird, unread=len(with_mail.get(bird, [])))
            return []
        fresh = cur - _seen_new
        _seen_new = cur
        for bird, msgs in new_mail(sorted(fresh)).items():
            if bird not in ALL_BIRDS:
                db_log("seen-not-a-bird", bird=bird, n=len(msgs))
                continue
            for mid, sender in msgs:
                db_log("seen", bird=bird, id=mid, sender=sender)
                d["pending"].setdefault(bird, []).append({"id": mid, "from": sender, "seen": now})
        # rung mail that left new/ was read: the end-to-end receipt, with latency
        for bird in list(d["rung"]):
            keep = []
            for r in d["rung"][bird]:
                if any(p.endswith("/" + r["id"]) for p in cur):
                    keep.append(r)
                else:
                    db_log("answered", bird=bird, id=r["id"], cid=r["cid"], latency=int(now - r["t"]))
            d["rung"][bird] = keep
            if not keep:
                del d["rung"][bird]
        # v5: deferred mail (loop-guard-suppressed earlier) — drop if read elsewhere, expire if
        # old, and re-queue as pending only as much as the pair window now has room for, so the
        # ring below can never trip the guard on mail it just re-queued.
        for bird in list(d["deferred"]):
            keep = []
            for m in d["deferred"][bird]:
                if not any(p.endswith("/" + m["id"]) for p in cur):
                    db_log("read-elsewhere", bird=bird, n=1, id=m["id"], deferred=True)
                elif now - m["deferred_at"] > DOORBELL_DEFERRED_TTL:
                    db_log("expired", bird=bird, id=m["id"], sender=m["from"],
                           age=int(now - m["deferred_at"]))
                else:
                    keep.append(m)
            requeued = []
            for m in keep:
                room, _ = pair_room(m["from"], bird, now)
                if room > 0:   # one ring can carry every deferred message from this sender
                    requeued.append(m)
            if requeued:
                d["pending"].setdefault(bird, []).extend(
                    {**m, "deferred": True} for m in requeued)
            remaining = [m for m in keep if m not in requeued]
            if remaining:
                d["deferred"][bird] = remaining
            else:
                del d["deferred"][bird]
        # mail that was read by other means (moved out of new/) is no longer pending
        for bird in list(d["pending"]):
            still = [m for m in d["pending"][bird] if any(p.endswith("/" + m["id"]) for p in cur)]
            gone = len(d["pending"][bird]) - len(still)
            if gone:
                db_log("read-elsewhere", bird=bird, n=gone)
            d["pending"][bird] = still
            if not still:
                del d["pending"][bird]
        sends = []
        if not d["pending"]:
            return []
        if not d["on"]:
            for bird, msgs in d["pending"].items():
                db_log("suppressed", bird=bird, reason="off", n=len(msgs),
                       ids=[m["id"] for m in msgs], senders=sorted({m["from"] for m in msgs}))
            d["pending"].clear()
            return []
        if state["active"]:
            return []  # chorus running: it walks AMQ-CHECK rounds itself; hold
        for bird in list(d["pending"]):
            msgs = d["pending"][bird]
            if is_busy(bird, now):
                continue
            gap = BIRD_COOLDOWN.get(bird, DOORBELL_COOLDOWN)
            last = d["last_ring"].get(bird, 0)
            if now - last < gap:
                continue
            # loop guard per sender->recipient pair — v5: over budget DEFERS, never drops
            allowed = []
            for m in msgs:
                room, used = pair_room(m["from"], bird, now)
                # this ring will consume ONE event per sender it carries, not one per message
                taken = 1 if any(q["from"] == m["from"] for q in allowed) else 0
                if room - taken <= 0 and taken == 0:
                    if m.get("deferred"):
                        d["deferred"].setdefault(bird, []).append(m)  # back to the queue, no new line
                    else:
                        db_log("deferred", bird=bird, id=m["id"], sender=m["from"],
                               reason="loop-guard", rings_in_window=used, pair_max=pair_max_for(bird))
                        d["deferred"].setdefault(bird, []).append(
                            {"id": m["id"], "from": m["from"], "seen": m["seen"],
                             "reason": "loop-guard", "deferred_at": now})
                else:
                    allowed.append(m)
            del d["pending"][bird]
            if not allowed:
                continue
            cid = secrets.token_hex(2)
            for sender in {m["from"] for m in allowed}:   # one event per sender per ring
                _pair_rings.setdefault((sender, bird), []).append(now)
            d["last_ring"][bird] = now
            d["rings"] += 1
            mark_busy(bird, f"doorbell #{cid}")
            d["rung"].setdefault(bird, []).extend(
                {"id": m["id"], "from": m["from"], "cid": cid, "t": now} for m in allowed)
            db_log("ring", bird=bird, cid=cid, n=len(allowed),
                   ids=[m["id"] for m in allowed], senders=sorted({m["from"] for m in allowed}),
                   deferred=sum(1 for m in allowed if m.get("deferred")))
            for m in allowed:
                if m.get("deferred"):
                    db_log("re-ring", bird=bird, cid=cid, id=m["id"], sender=m["from"],
                           reason="window-cleared", waited=int(now - m["deferred_at"]))
            sends.append((bird, doorbell_prompt(cid, [(m["id"], m["from"]) for m in allowed])))
        return sends


def doorbell_loop():
    doorbell_load()
    while True:
        try:
            for bird, text in doorbell_tick():
                send_async(bird, text)
        except Exception as e:
            db_log("tick-error", error=str(e))
        time.sleep(DOORBELL_POLL)


# ---- state machine --------------------------------------------------------------

def fire(message, birds, rounds, mode):
    global amq_mark
    with lock:
        if state["active"]:
            return False
        cid = secrets.token_hex(2)
        first = [birds[0]] if mode == "roundrobin" else list(birds)
        state.update(active=True, mode=mode, round=0, rounds=rounds,
                     sequence=list(birds), idx=0, waiting=list(first),
                     message=message, cid=cid, log=[],
                     deadline=time.time() + AGENT_TIMEOUT)
        amq_mark = None
        log(f"#{cid} round 0 ({mode}): sequence {' -> '.join(birds)}")
    if mode == "roundrobin":
        send_async(birds[0], prompt_for(0, 0, birds, message, cid))
    else:
        for b in birds:
            send_async(b, wrap_initial(message, cid))
    return True


def _end_of_round_locked():
    """Advance past a completed round. Returns list of (bird, text) to send."""
    global amq_mark
    rnd, seq, cid = state["round"], state["sequence"], state["cid"]
    if rnd >= 1 and amq_mark is not None:
        snap = amq_snapshot()
        if snap is not None and snap == amq_mark:
            state["active"] = False
            log(f"round {rnd}: no new AMQ messages -- chorus complete (early)")
            return []
    if rnd >= state["rounds"]:
        state["active"] = False
        log("chorus complete")
        return []
    state["round"] = rnd + 1
    state["idx"] = 0
    amq_mark = amq_snapshot()
    state["deadline"] = time.time() + AGENT_TIMEOUT
    log(f"round {state['round']}: [AMQ-CHECK] walking {' -> '.join(seq)}")
    if state["mode"] == "roundrobin":
        state["waiting"] = [seq[0]]
        return [(seq[0], amq_check(cid))]
    state["waiting"] = list(seq)
    return [(b, amq_check(cid)) for b in seq]


def _rering_stale_locked(bird):
    """v5: a rung message still unread after DOORBELL_RERING_AFTER with no turn-end is re-rung
    ONCE, now that the bird's Stop hook says its pane is idle. Call with lock held; returns
    [(bird, text)]. The `rerung` flag makes a second re-ring impossible."""
    d = state["doorbell"]
    now = time.time()
    if not d["on"] or state["active"]:
        return []
    seen = _seen_new or set()
    stale = [r for r in d["rung"].get(bird, [])
             if now - r["t"] > DOORBELL_RERING_AFTER and not r.get("rerung")
             and any(p.endswith("/" + r["id"]) for p in seen)]   # still unread at the last poll
    if not stale:
        return []
    cid = secrets.token_hex(2)
    for r in stale:
        r["rerung"] = True
        r["cid"] = cid
        # NOT counted against the pair budget: this path is bounded by the `rerung` flag (once per
        # message ever), and counting it starved deferred mail of its window (found in simulation).
    d["last_ring"][bird] = now
    d["rings"] += 1
    mark_busy(bird, f"doorbell #{cid}")
    db_log("re-ring", bird=bird, cid=cid, reason="no-turn-end", n=len(stale),
           ids=[r["id"] for r in stale], age=int(now - min(r["t"] for r in stale)))
    return [(bird, doorbell_prompt(cid, [(r["id"], r["from"]) for r in stale]))]


def bird_done(bird, note="", cid=None):
    sends = []
    with lock:
        b = state["doorbell"]["busy"].get(bird)
        if b and b["why"].startswith("doorbell"):
            db_log("turn-end", bird=bird, why=b["why"], took=int(time.time() - b["since"]))
        mark_idle(bird)  # a Stop hook means the turn ended, chorus or not
        rering = _rering_stale_locked(bird)
        if not state["active"] or bird not in state["waiting"]:
            for rb, rt in rering:
                send_async(rb, rt, delay=SEND_GAP)
            return
        if cid and cid != state["cid"]:
            log(f"{bird}: Stop for #{cid}, not this chorus (#{state['cid']}) -- ignored")
            return
        state["waiting"].remove(bird)
        log(f"{bird} finished" + (f" ({note})" if note else ""))
        if state["waiting"]:
            return  # simultaneous mode: others still working
        seq = state["sequence"]
        if state["mode"] == "roundrobin" and state["idx"] + 1 < len(seq):
            state["idx"] += 1
            nxt = seq[state["idx"]]
            state["waiting"] = [nxt]
            state["deadline"] = time.time() + AGENT_TIMEOUT
            sends = [(nxt, prompt_for(state["round"], state["idx"], seq,
                                      state["message"], state["cid"]))]
        else:
            sends = _end_of_round_locked()
    for b, t in sends + rering:
        send_async(b, t, delay=SEND_GAP)


def reaper():
    while True:
        time.sleep(5)
        stuck = []
        with lock:
            if state["active"] and time.time() > state["deadline"]:
                stuck = list(state["waiting"])
                log(f"timeout: skipping {', '.join(stuck)}")
        for b in stuck:
            bird_done(b, note="timed out, skipped")


def note_limit(bird, lim):
    """Record or clear a bird's usage-limit state from a Stop hook payload.

    A Stop carries `limit` only when the turn ended blocked, and the hook decides
    that by whether an ordinary turn landed after the last API error. So a Stop
    WITHOUT it is a recovery, and is the only thing that clears the flag: no
    timeout, no guessing, no stale "blocked" left on a pane that came back.

    Returns the state after the call, for the test.
    """
    with lock:
        d = state["doorbell"]["limit"]
        prev = d.get(bird)
        if lim in ("session", "credits"):
            d[bird] = lim
            if prev != lim:
                db_log("limit", bird=bird, kind=lim)
        elif prev:
            d.pop(bird, None)
            db_log("limit-cleared", bird=bird, was=prev)
        return d.get(bird)


def doorbell_view():
    """Portal-facing summary (call with lock held)."""
    d = state["doorbell"]
    now = time.time()
    return {
        "on": d["on"], "rings": d["rings"],
        "busy": {b: v["why"] for b, v in d["busy"].items()},
        "limit": dict(d.get("limit", {})),
        "pending": {b: len(v) for b, v in d["pending"].items()},
        "deferred": {b: len(v) for b, v in d["deferred"].items()},
        "budget": {"pair_max": PAIR_MAX, "pair_window": PAIR_WINDOW, "per_bird": dict(BIRD_PAIR_MAX)},
        "last_ring_ago": {b: int(now - t) for b, t in d["last_ring"].items()},
        "chorus_active": state["active"],
        # CUTOVER #10: a doorbell with no readable mail root is DISABLED, and says so here
        "amq": {"root": AMQ_ROOT, "available": _amq_ok,
                "status": "DISABLED (AMQ root unavailable)" if _amq_ok is False else ("ok" if _amq_ok else "unprobed")},
        "log": d["log"][-12:],
    }


# ---- http -----------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = self.path.rstrip("/")
        if p.endswith("status"):
            with lock:
                return self._json(200, state)
        if p.endswith("doorbell"):
            with lock:
                return self._json(200, doorbell_view())
        if p.endswith("agents"):   # the layout's name (docs/LAYOUT.md, INSTALL §4 stage 3)
            with lock:
                return self._json(200, {"agents": ALL_BIRDS, "orchestrator": ME})
        if p.endswith("birds"):    # the pre-layout name, kept for anything live that still asks
            with lock:
                return self._json(200, {"birds": ALL_BIRDS, "orchestrator": ME})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n > MAX_BODY:
            return self._json(413, {"error": "too large"})
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, TypeError):
            body = {}
        refused = post_refusal(self.headers)
        if refused:
            print(f"[post] refused {self.path}: {refused[1]}", flush=True)
            return self._json(refused[0], {"error": refused[1]})
        p = self.path.rstrip("/")
        # the same suffix test the routing below uses, so no spelling of a path reaches a route ungated
        if any(p.endswith(r) for r in FRONT_ROUTES) and not came_through_front_door(self.headers):
            print(f"[post] refused {self.path}: not through the front door", flush=True)
            return self._json(401, {"error": "this route answers only the portal page, through the front door"})
        # Proxy fence on destructive/bird-facing routes. `/hook` and `/matron` are
        # bird-to-daemon over loopback by design — flock-stop-hook posts to /hook, and
        # Matron dispatches /matron from the host shell. Neither is proxied through Caddy
        # in normal use, so a proxied POST reaching them is off-host and unauthorized.
        # `/fire`, `/doorbell`, `/abort`, `/status` STAY proxy-reachable — those are the
        # four routes the portal page consumes by design (grep of portal/index.html).
        # review R3 2026-09-16: this was the "cheaper route" the pane-key hunt-list flagged;
        # LAN attacker could POST /chorus/matron to type into any bird's pane or POST
        # /chorus/hook to mark any bird busy for up to 30 min, all without auth.
        if (p.endswith("hook") or p.endswith("matron")) and is_proxied_request(self.headers):
            route = p.rsplit("/", 1)[-1]
            print(f"[{route}] rejected proxied request", flush=True)
            return self._json(403, {"error": "loopback-only; not served through the proxy"})
        if p.endswith("hook"):
            b = body.get("bird")
            cid = body.get("cid") or None
            ev = body.get("event") or "stop"
            if b in ALL_BIRDS:
                if ev == "prompt":       # UserPromptSubmit: a turn is starting
                    with lock:
                        mark_busy(b, "prompt")
                else:                    # Stop: the turn ended
                    # A Stop carries `limit` only when the turn ended blocked, and
                    # the hook decides that by whether an ordinary turn landed after
                    # the last API error. So a Stop WITHOUT it is a recovery, and is
                    # the only thing that clears the flag -- no timeout, no guessing.
                    note_limit(b, body.get("limit"))
                    bird_done(b, cid=cid)
            return self._json(200, {"ok": True})
        if p.endswith("doorbell"):
            if "on" not in body:
                return self._json(400, {"error": "need on: true|false"})
            doorbell_set(bool(body["on"]))
            with lock:
                return self._json(200, doorbell_view())
        if p.endswith("fire"):
            msg = (body.get("message") or "").strip()
            birds = [b for b in (body.get("birds") or []) if b in ALL_BIRDS]
            mode = body.get("mode") if body.get("mode") in ("roundrobin", "simultaneous") else "roundrobin"
            try:
                rounds = max(0, min(10, int(body.get("rounds", 3))))
            except (ValueError, TypeError):
                rounds = 3
            if not msg or not birds:
                return self._json(400, {"error": "need message and at least one bird"})
            ok = fire(msg, birds, rounds, mode)
            return self._json(200 if ok else 409, {"ok": ok})
        if p.endswith("abort"):
            with lock:
                if state["active"]:
                    state["active"] = False
                    log("aborted by user")
            return self._json(200, {"ok": True})
        if p.endswith("matron"):
            # Auth, same token and compare as /pane-key (review 2026-09-24): loopback alone
            # is not a trust boundary on this host, and /matron types an init pointer into any
            # bird's pane. Rejects go to the journal, not the ledger.
            if not check_pane_key_auth(self.headers.get("Authorization", "")):
                print("[matron] auth rejected", flush=True)
                return self._json(401, {"error": "unauthorized"})
            cid = secrets.token_hex(3)
            status, resp, prompt = matron_prompt(body, cid)
            if prompt is None:
                return self._json(status, resp)
            with lock:
                db_log("matron", bird=resp["bird"], cid=cid, commit=resp["commit"], lines=resp["lines"], section=resp["section"],
                       sender=(body.get("sender") or "matron").strip(), bytes=resp["bytes"])
            send_async(resp["bird"], prompt)
            return self._json(200, resp)
        if p.endswith("pane-key"):
            # Proxy fence — Caddy's (portal) reverse_proxy /chorus/* → the daemon makes
            # this endpoint LAN-reachable via https://<SITE_HOSTNAME>/chorus/pane-key. Caddy adds
            # X-Forwarded-* on every proxied request; a direct loopback caller never sets them.
            # Any request that arrived through the proxy fails closed here, BEFORE the token
            # compare and before any state or ledger write (review R2 #1, 2026-09-16).
            # Belt-and-suspenders: the front door should also block /chorus/pane-key + /chorus/matron
            # at the Caddyfile level so the request never leaves the host at all.
            if is_proxied_request(self.headers):
                fwd = self.headers.get("X-Forwarded-For") or self.headers.get("X-Forwarded-Host")
                print(f"[pane-key] rejected proxied request; X-Forwarded-*={fwd!r}", flush=True)
                return self._json(403, {"error": "pane-key is loopback-only; not served through the proxy"})
            # Auth check — Bearer token in Authorization header, constant-time compare.
            # Auth rejects go to the journal, NOT the ledger (review R2 #2, 2026-09-16):
            # an unauthenticated caller must not be able to grow doorbell.log arbitrarily.
            if not check_pane_key_auth(self.headers.get("Authorization", "")):
                b_probe = body.get("bird") if isinstance(body.get("bird"), str) else "?"
                print(f"[pane-key] auth rejected; bird={b_probe!r}", flush=True)
                return self._json(401, {"error": "unauthorized"})
            b = body.get("bird")
            cmd = body.get("cmd")
            source = body.get("source") or ""
            if not isinstance(source, str):
                source = ""
            now = time.time()
            with lock:
                status, resp, ledger = pane_key_check(b, cmd, now)
                if status != 200:
                    if ledger is not None:
                        b_str = b if isinstance(b, str) else "?"
                        cmd_str = cmd if isinstance(cmd, str) else "?"
                        db_log("pane-key-rejected", bird=b_str, cmd=cmd_str, **ledger)
                    return self._json(status, resp)
                _pane_key_last[b] = now
                cid = secrets.token_hex(3)
                db_log("pane-key", bird=b, cid=cid, cmd=cmd, source=source, typed_bytes=len(cmd))
            send_async(b, cmd)
            return self._json(200, {"ok": True, "bird": b, "cmd": cmd, "cid": cid, "typed_bytes": len(cmd)})
        if p.endswith("reload"):
            load_birds()
            with lock:
                return self._json(200, {"birds": ALL_BIRDS, "orchestrator": ME})
        self._json(404, {"error": "not found"})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    load_birds()
    if not ALL_BIRDS:
        sys.exit("roster not loaded; see log")
    os.makedirs(STATE_DIR, mode=0o711, exist_ok=True)
    signal.signal(signal.SIGHUP, load_birds)
    threading.Thread(target=reaper, daemon=True).start()
    threading.Thread(target=doorbell_loop, daemon=True).start()
    print(f"chorusd v5.2 listening on {HOST}:{PORT}; agents {ALL_BIRDS} from {BIRDS_FILE}; state {STATE_DIR}; doorbell ledger {DOORBELL_LEDGER}")
    if amq_paths() is None:
        print(f"doorbell: DISABLED (AMQ root {AMQ_ROOT!r} unavailable) — rings and early-stop are off until it is readable", flush=True)
    else:
        print(f"doorbell: mail root {AMQ_ROOT} readable", flush=True)
    if PANE_KEY_ENABLED:
        n = len(PANE_KEY_TOKEN)
        tail = f"{n} bytes from {PANE_KEY_TOKEN_PATH}" if n else f"EMPTY at {PANE_KEY_TOKEN_PATH} — all calls 401"
        print(f"pane-key: enabled=True token={tail}")
    else:
        print(f"pane-key: enabled=False (set CHORUSD_PANE_KEY_ENABLED=1 to enable)")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
