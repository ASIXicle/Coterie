#!/usr/bin/env python3
"""
persMEM — Persistent Memory MCP Server
MCPServer (MCP Python SDK 2.x) over Streamable HTTP, backed by ChromaDB + sentence-transformers.

Exposes tools for Claude to store and retrieve memories across sessions.
Designed for claude.ai remote connector via Caddy reverse proxy.
"""

import os
import sys
import json
import logging
import hashlib
import hmac
import importlib.util
import time
import shlex
import re
import tempfile
import subprocess
from datetime import datetime, timezone, timedelta, date
from typing import Optional
import asyncio

# systemd hands us stdout as a stream to journald, and Python block-buffers a non-TTY stdout, so
# a print() sat in an 8 KB buffer until it filled or the process exited. Line-buffer it so each
# [persMEM] line reaches the journal when printed (chorusd c75ea58; F review 2026-09-25).
sys.stdout.reconfigure(line_buffering=True)

import chromadb
from chromadb.config import Settings as ChromaSettings
from sentence_transformers import SentenceTransformer
from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import JSONResponse

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Names and defaults are release/docs/LAYOUT.md's (canonical names only, no fallbacks).
DATA_DIR = os.environ.get("MEMORY_DATA_DIR", "/var/lib/memory/chromadb")
HOST = os.environ.get("MEMORY_HOST", "127.0.0.1")
PORT = int(os.environ.get("MEMORY_PORT", "8765"))
# The MCP endpoint path segment is the only thing between the network and every agent's
# memory. It fails CLOSED: no default, and the placeholder values a unit file may carry are
# refused (review, 2026-09-11: the old default "dev" served everything at /dev/mcp whenever a
# unit lost its EnvironmentFile).
SECRET_PATH = os.environ.get("MEMORY_SECRET_PATH", "").strip()
if not SECRET_PATH or SECRET_PATH.lower() in {"dev", "change_me", "changeme"}:
    print("[persMEM] FATAL: MEMORY_SECRET_PATH is unset or a placeholder; set a real secret "
          "path segment in the unit's EnvironmentFile (refusing to serve at a guessable path)")
    raise SystemExit(1)
SERVER_DIR = os.path.dirname(os.path.abspath(__file__))  # install dir (e.g. /opt/memory)
# Working root of the file / shell / git tools; nothing else derives from it.
MEMORY_HOME = os.environ.get("MEMORY_HOME", "/var/lib/memory")
# The developer tools (shell_exec, file_read, file_write, file_patch, git_op, diff_generate) run
# as this server's own account: shell_exec and git_op hand their text to bash, and the file tools
# take any path, so with them on the MCP secret is a shell as this account. Off unless exactly
# "1"; off, they are not registered and a client never sees them (2026-10-01, SECURITY.md).
DEV_TOOLS = os.environ.get("MEMORY_DEV_TOOLS", "") == "1"
# The model DIRECTORY itself (fetch-model.py prints it; the installer writes it here).
EMBEDDING_MODEL = os.environ.get(
    "MEMORY_EMBEDDING_MODEL", os.path.join(SERVER_DIR, "models", "voyage-4-nano")
)
NEWSTRON_SECRET = os.environ.get("NEWSTRON_SECRET", "")  # shared secret for the news fetcher (news API + news_store); empty disables them
# Bootstrap history lives next to the vector store (LAYOUT: no variable of its own).
BOOTSTRAP_HISTORY_PATH = os.path.join(os.path.dirname(os.path.abspath(DATA_DIR)), "bootstrap-history")

# ---------------------------------------------------------------------------
# Boot: embedding model + ChromaDB
# ---------------------------------------------------------------------------
print(f"[persMEM] Loading embedding model: {EMBEDDING_MODEL}")
EMBEDDING_TRUNCATE_DIM = 1024
embedder = SentenceTransformer(EMBEDDING_MODEL, trust_remote_code=True, truncate_dim=EMBEDDING_TRUNCATE_DIM)
EMBEDDING_DIM = embedder.get_sentence_embedding_dimension()
print(f"[persMEM] Embedding dim: {EMBEDDING_DIM}")

# Heads (heads/README.md): typed questions answered from each item's vector as it is stored.
# Every item stored through memory_store, memory_bulk_store, news_store and the AMQ auto-store
# records the fingerprint of the embedder that made its vector ("embed_fp"), so a head is never
# applied to vectors made another way. MEMORY_HEADS_DIR (empty = no heads) holds the head files;
# a head trained on another fingerprint is logged and skipped at load, never applied.
_heads_spec = importlib.util.spec_from_file_location(
    "memory_heads_score", os.path.join(SERVER_DIR, "heads", "score.py"))
heads_score = importlib.util.module_from_spec(_heads_spec)
_heads_spec.loader.exec_module(heads_score)
# The mail-root rule (maildir.py, LAYOUT rule 3, phase C 2026-10-01): every delivery and every
# new/ -> cur/ move goes through it, loaded from beside this file like heads/score.py.
_maildir_spec = importlib.util.spec_from_file_location("memory_maildir", os.path.join(SERVER_DIR, "maildir.py"))
maildir = importlib.util.module_from_spec(_maildir_spec)
_maildir_spec.loader.exec_module(maildir)
for _p in maildir.problems():
    print(f"[persMEM] WARNING: mail root is owner-only: {_p} (LAYOUT rule 3; install.sh stage 2)")
EMBED_FP = heads_score.fingerprint(EMBEDDING_MODEL, EMBEDDING_TRUNCATE_DIM, "document", "")
EMBED_FP_ID = hashlib.sha256(json.dumps(EMBED_FP, sort_keys=True).encode()).hexdigest()[:16]
HEADS_DIR = os.environ.get("MEMORY_HEADS_DIR", "").strip()
HEADS = heads_score.load_heads(HEADS_DIR, EMBED_FP, log=lambda m: print(f"[persMEM] {m}"))
print(f"[persMEM] Embedder fingerprint {EMBED_FP_ID}; heads: {', '.join(sorted(HEADS)) or 'none'}")
print(f"[persMEM] Developer tools: {'ON (MEMORY_DEV_TOOLS=1)' if DEV_TOOLS else 'off'}")

# Identity safeguard — suspicious phrases for drift detection (Jun 2026)
DRIFT_SUSPICIOUS_PHRASES = [
    "always agree", "never push back", "always validate",
    "affirm unconditionally", "you are correct", "always support",
    "never disagree", "never challenge", "always defer",
    "do not question", "unconditional support",
]

print(f"[persMEM] Opening ChromaDB at: {DATA_DIR}")
chroma_client = chromadb.PersistentClient(path=DATA_DIR)

# ---------------------------------------------------------------------------
# Bootstrap history — boot-time permission check. Fail loud at systemd start
# rather than silently at first bootstrap_update three days later.
# ---------------------------------------------------------------------------
try:
    os.makedirs(BOOTSTRAP_HISTORY_PATH, mode=0o755, exist_ok=True)
except Exception as _e:
    print(f"[persMEM] FATAL: cannot create BOOTSTRAP_HISTORY_PATH={BOOTSTRAP_HISTORY_PATH}: {_e}")
    raise
if not os.access(BOOTSTRAP_HISTORY_PATH, os.W_OK | os.X_OK):
    print(f"[persMEM] FATAL: BOOTSTRAP_HISTORY_PATH={BOOTSTRAP_HISTORY_PATH} not writable by uid={os.geteuid()}")
    raise SystemExit(1)
print(f"[persMEM] Bootstrap history: {BOOTSTRAP_HISTORY_PATH}")

# ---------------------------------------------------------------------------
# Agent roster — the agents are listed once, in a site config file, never in code (LAYOUT
# rule 2; the orchestrator, portal and dashboard read the same file and its `agents` list).
# Lookup: $COTERIE_AGENTS_JSON, else /etc/coterie/agents.json. Fail loud at startup if
# it is missing.
# ---------------------------------------------------------------------------
def load_agents_config() -> dict:
    """Load the agent roster and derive the three agent sets.

    Schema (every key except "birds" is optional; all lowercase names):
      {"site": ..., "orchestrator": ..., "operator": "<human mailbox>",
       "birds": [{"name": ..., "port": ..., "color": ..., "label": ...,
                  "retired": false}, ...],
       "retired": ["<name>", ...],          # alternative to per-row retired
       "extra_mailboxes": ["<name>", ...]}   # AMQ-only agents, no identity

    Returns {"path", "config", "flock_agents", "flock_order",
             "known_identity_agents", "amq_agents"}:
      flock_agents          = birds not retired (active seats)
      known_identity_agents = all birds, retired included (their bootstrap
                              identity entries must still resolve)
      amq_agents            = known_identity_agents | extra_mailboxes | {operator}
    """
    env_path = os.environ.get("COTERIE_AGENTS_JSON", "")
    candidates = [env_path] if env_path else ["/etc/coterie/agents.json"]
    path = next((p for p in candidates if os.path.isfile(p)), None)
    if path is None:
        print("[persMEM] FATAL: no agent roster file. Set COTERIE_AGENTS_JSON to a "
              "roster file or install /etc/coterie/agents.json "
              f"(looked in: {', '.join(candidates)})")
        raise SystemExit(1)
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        # Names become Maildir path components and are rendered into the chorus HTML; only a
        # plain lowercase token is a name (a review probe put "../../tmp/x" into the roster).
        _name_re = re.compile(r"[a-z][a-z0-9_-]{0,31}")
        def norm(n):
            n = str(n).lower().strip()
            if not _name_re.fullmatch(n):
                raise ValueError(f"invalid agent name {n!r} (want [a-z][a-z0-9_-]{{0,31}})")
            return n
        retired = {norm(n) for n in cfg.get("retired", [])}
        order, known = [], set()
        coordinator = ""
        for b in cfg["agents"]:
            name = norm(b["name"])
            known.add(name)
            if b.get("retired"):
                retired.add(name)
            elif name not in retired and name not in order:
                order.append(name)
                if not coordinator and str(b.get("role", "")).strip().lower() == ROSTER_COORDINATOR_ROLE:
                    coordinator = name
        known |= retired
        extra = {norm(n) for n in cfg.get("extra_mailboxes", [])}
        operator = norm(cfg["operator"]) if cfg.get("operator") else ""
    except Exception as e:
        print(f"[persMEM] FATAL: bad agent roster {path}: {e!r}")
        raise SystemExit(1)
    if not order:
        print(f"[persMEM] FATAL: agent roster {path} has no active agents")
        raise SystemExit(1)
    return {
        "path": path,
        "config": cfg,
        "flock_agents": set(order),
        "flock_order": order,
        "known_identity_agents": known,
        "amq_agents": known | extra | ({operator} if operator else set()),
        "coordinator": coordinator,
        "operator": operator,
    }

# The growth alarm's recipient: the first active agent whose roster `role` is this (never a
# hard-coded name). With none: the operator, then every active agent (_growth_alarm_recipients).
ROSTER_COORDINATOR_ROLE = "coordinator"
_ROSTER = load_agents_config()


def _growth_alarm_recipients(author: str) -> tuple:
    """Who receives a growth alarm, and by which rule: the roster's coordinator; with none, the
    operator's mailbox; with neither, every active agent. Never nobody: an alarm that reaches only
    the journal is a guard nobody sees (review, 2026-09-25). The author is not alarmed about
    their own write."""
    if _ROSTER["coordinator"]:
        rcpt, how = [_ROSTER["coordinator"]], "coordinator"
    elif _ROSTER["operator"]:
        rcpt, how = [_ROSTER["operator"]], "operator (the roster names no coordinator)"
    else:
        rcpt, how = list(_ROSTER["flock_order"]), "every active agent (no coordinator, no operator)"
    return [r for r in rcpt if r != author], how
print(f"[persMEM] Agent roster: {_ROSTER['path']} "
      f"(active {' '.join(_ROSTER['flock_order'])})")

# ---------------------------------------------------------------------------
# Server commit — captured at process start so the boot manifest can name
# the assembly code that produced it.
# Cached; changes require a server restart.
# ---------------------------------------------------------------------------
_SHA_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


def _read_git_head(start_dir: str) -> Optional[str]:
    """Resolve HEAD by reading the enclosing git dir directly, no `git` subprocess.

    `git rev-parse` fails under the service uid on a root-owned deploy
    checkout ("dubious ownership"), which is why `server_commit` read "unknown"
    from the 2026-09-11 cutover on. Reading HEAD -> ref -> sha needs
    only read access. Covers a plain `.git` directory with a loose ref,
    packed-refs, or a detached HEAD; anything else returns None. Depends on the
    refs staying world-readable (root's umask at pull time); a 0640 ref fails
    closed to "unknown", never to a wrong sha.
    """
    try:
        d = os.path.realpath(start_dir)
        while not os.path.lexists(os.path.join(d, ".git")):
            parent = os.path.dirname(d)
            if parent == d:
                return None
            d = parent
        gitdir = os.path.join(d, ".git")
        if not os.path.isdir(gitdir):
            # A `.git` FILE (worktree/submodule) is the nearest repo; climbing past it
            # would return an ancestor repo's HEAD. Leave it to the git fallback.
            return None
        with open(os.path.join(gitdir, "HEAD"), "r") as f:
            head = f.read().strip()
        if not head.startswith("ref:"):
            return head if _SHA_RE.fullmatch(head) else None
        ref = head[len("ref:"):].strip()
        loose = os.path.join(gitdir, ref)
        if os.path.isfile(loose):
            with open(loose, "r") as f:
                v = f.read().strip()
            return v if _SHA_RE.fullmatch(v) else None
        packed = os.path.join(gitdir, "packed-refs")
        if os.path.isfile(packed):
            with open(packed, "r") as f:
                for line in f:
                    parts = line.strip().split(" ", 1)
                    if len(parts) == 2 and parts[1] == ref and _SHA_RE.fullmatch(parts[0]):
                        return parts[0]
    except OSError:
        pass
    return None


def _get_server_commit() -> str:
    """Return the source-commit hash of the running server.py.

    Reads the enclosing checkout's HEAD straight from its git dir first. When the
    server runs from a git checkout (deploy = root `git pull`, then restart) that
    HEAD is the deployed commit. A tree without .git (a release install) reads the sha in
    VERSION beside this file, which the installer writes from its clone. Falls back to `git rev-parse HEAD`
    for layouts the direct read does not cover (worktrees, reftable) in dev
    environments where the running uid owns the repo. Falls back to "unknown".

    Value is taken at import: a pull without a restart is not reflected.
    """
    server_dir = os.path.dirname(os.path.realpath(__file__))
    v = _read_git_head(server_dir)
    if v:
        return v
    try:
        with open(os.path.join(server_dir, "VERSION"), "r") as f:
            v = (f.read().split() or [""])[0]
        if _SHA_RE.fullmatch(v):
            return v
    except OSError:
        pass
    try:
        r = subprocess.run(
            ["git", "-C", server_dir, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return "unknown"

SERVER_COMMIT = _get_server_commit()
print(f"[persMEM] Server commit: {SERVER_COMMIT}")

# Default collection — we can add per-project collections later
DEFAULT_COLLECTION = "memories"


def get_or_create_collection(name: str):
    """Get or create a ChromaDB collection."""
    return chroma_client.get_or_create_collection(
        name=name,
        metadata={"hnsw:space": "cosine"},
    )


def embed(texts: list[str], mode: str = "document") -> list[list[float]]:
    """Embed texts with Voyage 4 nano prompt names (document for storage, query for retrieval)."""
    return embedder.encode(texts, prompt_name=mode, show_progress_bar=False).tolist()

HEAD_FAILURES: dict[str, int] = {}


def stored_extras(collection: str, vector) -> tuple[dict, dict]:
    """What a stored item's metadata gains (the embedder fingerprint, and each applicable head's
    answer: "" when the head did not decide), plus the heads' answers for the caller's response.
    The bootstrap collection gets neither. A head that fails is left out and never fails the store;
    at its third failure it is disabled until the next restart, so a broken head logs three lines,
    not one per store."""
    if collection == "bootstrap":
        return {}, {}
    meta, answers = {"embed_fp": EMBED_FP_ID}, {}
    for name, head in list(HEADS.items()):
        if head.collections and collection not in head.collections:
            continue
        try:
            r = head.score(vector)
        except Exception as e:
            HEAD_FAILURES[name] = HEAD_FAILURES.get(name, 0) + 1
            if HEAD_FAILURES[name] >= 3:
                HEADS.pop(name, None)
                print(f"[persMEM] heads: disabled {name} after 3 failures (last: {e}); a restart reloads it")
            else:
                print(f"[persMEM] heads: {name} failed on a store ({e}); skipped")
            continue
        p = round(r["p"], 4)
        meta[f"head:{name}"] = r["answer"] if r["decided"] else ""
        meta[f"head:{name}:p"] = p
        meta[f"head:{name}:v"] = r["v"]
        answers[name] = {"answer": r["answer"], "p": p, "decided": r["decided"]}
    return meta, answers

def make_id(content: str, timestamp: str) -> str:
    """Deterministic chunk ID from content hash + timestamp."""
    h = hashlib.sha256(f"{timestamp}:{content}".encode()).hexdigest()[:16]
    return f"mem-{h}"


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------
# Bind address and port come from MEMORY_HOST / MEMORY_PORT (defaults 127.0.0.1:8765, read at
# the top of this file). Until 2026-09-11 the bind was hardcoded (all interfaces) and the env values
# fed only the startup print, so an override could never take effect (release review).
# A deployment that must listen on all interfaces sets MEMORY_HOST=0.0.0.0 in its unit.
# SDK 2.x takes host, port and the endpoint path in run() (bottom of this file), not here.
# serverInfo.version is "" unless set (2.x no longer reports the SDK's version); the running
# commit is what a client should see there.
mcp = MCPServer("persMEM", version=SERVER_COMMIT[:12])


# uvicorn's access log (MCPServer.run() leaves access_log at its default, True) writes one request line
# per MCP call, and every MCP path carries SECRET_PATH, so the journal held the secret on each
# call until 2026-09-25. A filter on the logger survives uvicorn's dictConfig (it replaces the
# logger's handlers, not its filters). tests/mount_path_log_test.py proves both halves live.
class _RedactSecretPath(logging.Filter):
    def filter(self, record):
        if isinstance(record.args, tuple):
            record.args = tuple(a.replace(SECRET_PATH, "<secret>") if isinstance(a, str) else a
                                for a in record.args)
        if isinstance(record.msg, str):
            record.msg = record.msg.replace(SECRET_PATH, "<secret>")
        return True


logging.getLogger("uvicorn.access").addFilter(_RedactSecretPath())

@mcp.tool()
async def memory_store(
    content: str,
    project: str = "general",
    tags: str = "",
    memory_type: str = "session_note",
    collection: str = DEFAULT_COLLECTION,
    supersedes: str = "",
) -> str:
    """
    Store a memory chunk in the vector database.

    Args:
        content: The text to store. Can be a session summary, a decision,
                 a bug description, architecture note, or anything worth remembering.
                 Aim for ~100-500 words per chunk for best retrieval.
        project: Project name (e.g., 'general', 'homelab', 'website').
        tags: Comma-separated tags (e.g., 'backup,zfs,nightly').
        memory_type: One of: session_summary, decision, bug, architecture,
                     code_change, insight, question, raw_note
        collection: ChromaDB collection name. Default: 'memories'.
        supersedes: ID of a memory this one replaces. If provided, the old
                    memory is auto-marked as superseded. Use for decisions
                    that override prior decisions (e.g., ceiling 14h → 6h).
    """
    # H write-time ceiling: handoff entries carry
    # 500-word ceiling matching the flock's template. Reject-not-truncate —
    # silently cutting a lane handoff at boot is the inverted-persistence
    # bug the flock's FMs explicitly refuse. Caller trims or splits.
    if memory_type == "handoff":
        word_count = len(content.split())
        if word_count > 500:
            return json.dumps({
                "status": "error",
                "error": (
                    f"handoff exceeds 500-word ceiling ({word_count} words). "
                    "Trim or split into multiple handoffs; render-time "
                    "truncation is not applied."
                ),
                "word_count": word_count,
                "ceiling": 500,
            })

    now = datetime.now(timezone.utc).isoformat()
    chunk_id = make_id(content, now)

    coll = get_or_create_collection(collection)
    embedding = await asyncio.to_thread(embed, [content])

    metadata = {
        "project": project,
        "tags": tags,
        "type": memory_type,
        "stored_at": now,
        "char_count": len(content),
        "status": "active",
        "superseded_by": "",
        "expires_at": "",
    }
    extras, head_answers = stored_extras(collection, embedding[0])
    metadata.update(extras)

    await asyncio.to_thread(
        coll.add,
        ids=[chunk_id],
        embeddings=embedding,
        documents=[content],
        metadatas=[metadata],
    )

    # Auto-retract the superseded memory if specified
    retracted_id = ""
    supersedes_warning = ""
    if supersedes:
        try:
            old = await asyncio.to_thread(
                coll.get, ids=[supersedes], include=["metadatas"]
            )
            if old["ids"]:
                old_meta = old["metadatas"][0]
                old_meta["status"] = "superseded"
                old_meta["superseded_by"] = chunk_id
                old_meta["superseded_at"] = now
                await asyncio.to_thread(
                    coll.update, ids=[supersedes], metadatas=[old_meta]
                )
                retracted_id = supersedes
            else:
                supersedes_warning = f"WARNING: supersedes target '{supersedes}' not found — new memory stored but nothing retracted"
        except Exception as e:
            supersedes_warning = f"WARNING: retraction of '{supersedes}' failed ({e}) — new memory stored but old not retracted"

    result = {
        "status": "stored",
        "id": chunk_id,
        "collection": collection,
        "project": project,
        "type": memory_type,
        "stored_at": now,
        "supersedes": retracted_id,
    }
    if supersedes_warning:
        result["warning"] = supersedes_warning
    result["heads"] = head_answers

    return json.dumps(result)


@mcp.tool()
async def memory_retract(
    memory_id: str,
    reason: str = "",
    superseded_by: str = "",
    collection: str = DEFAULT_COLLECTION,
    author: str = "",
) -> str:
    """
    Mark a memory as superseded/retracted. The memory stays in the
    database but is flagged so search can annotate or filter it.

    Use when a decision, spec, or finding has been overridden by
    a newer one. Pairs with memory_store(supersedes=...) for
    atomic store-and-retract.

    For the `bootstrap` collection specifically: metadata writes affect
    what chorus_init ships (a status flip changes the rule fired), so the
    retraction goes through the bootstrap-history file store with mandatory reason
    and author. Content is unchanged; the sidecar records the metadata
    delta so replay can reconstruct rules.

    Args:
        memory_id: The ID of the memory to retract (from search results).
        reason: Why this memory is being retracted. MANDATORY for
                bootstrap collection retracts.
        superseded_by: ID of the memory that replaces this one (optional).
        collection: ChromaDB collection name. Default: 'memories'.
        author: Which bird performed the retraction. MANDATORY for
                bootstrap collection retracts. Ignored elsewhere.
    """
    coll = get_or_create_collection(collection)

    try:
        existing = coll.get(ids=[memory_id], include=["metadatas", "documents"])
        if not existing["ids"]:
            return json.dumps({"status": "error", "message": f"Memory {memory_id} not found"})

        old_meta = existing["metadatas"][0]
        old_content = existing["documents"][0]
        now = datetime.now(timezone.utc).isoformat()

        # Metadata-only writes on the bootstrap collection go through the
        # history path. Content unchanged.
        if collection == "bootstrap":
            if not reason or not reason.strip():
                return json.dumps({
                    "status": "rejected",
                    "message": "reason is required for memory_retract on the "
                               "bootstrap collection — metadata changes here "
                               "affect what chorus_init ships and must be "
                               "recorded with a stated reason.",
                })
            if not author or not author.strip():
                return json.dumps({
                    "status": "rejected",
                    "message": "author is required for memory_retract on the "
                               "bootstrap collection — every history record "
                               "names which bird performed the change.",
                })
            content_sha_full = hashlib.sha256(old_content.encode()).hexdigest()
            ok, err, info = _bootstrap_history_write(
                entry_id=memory_id,
                old_content=old_content,
                old_metadata=dict(old_meta),
                new_content_sha256=content_sha_full,  # content unchanged
                reason=f"[memory_retract] {reason.strip()}",
                author=author.strip(),
                now_iso=now,
            )
            if not ok:
                return json.dumps({
                    "status": "rejected",
                    "message": f"bootstrap history write failed: {err}",
                })

        new_meta = dict(old_meta)
        new_meta["status"] = "superseded"
        new_meta["superseded_by"] = superseded_by
        new_meta["superseded_at"] = now
        if reason:
            new_meta["retraction_reason"] = reason

        coll.update(ids=[memory_id], metadatas=[new_meta])

        return json.dumps({
            "status": "retracted",
            "id": memory_id,
            "superseded_by": superseded_by,
            "reason": reason,
            "original_content_preview": old_content[:200],
        })
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool()
async def news_store(
    secret: str,
    content: str,
    url: str,
    tier: int,
    source: str,
    keywords: str = "",
    item_date: str = "",
) -> str:
    """
    Store a news item in the 'news' ChromaDB collection.

    Authenticated endpoint for the news fetcher. Requires the
    NEWSTRON_SECRET env var to be set on the server AND the caller to
    pass the matching value. Writes ONLY to the 'news' collection.

    Args:
        secret: Shared secret matching NEWSTRON_SECRET.
        content: The news item text (title + body summary).
        url: Canonical URL for the item.
        tier: 1=security/operational, 2=infrastructure, 3=experiment-relevant,
              4=academic-AI/cog-sci, 5=academic-broader, 6=general-news,
              7=arts-culture, 8=long-form/lifestyle, 9=wildcard.
        source: Feed name (e.g., 'oss-security', 'ffmpeg-releases').
        keywords: Comma-separated matched keywords (e.g., 'ffmpeg,cve').
        item_date: ISO date of the item as reported by the feed (optional).
    """
    if not NEWSTRON_SECRET:
        return json.dumps({"status": "error", "error": "endpoint disabled: NEWSTRON_SECRET not configured on server"})
    # Constant-time comparison (review, 2026-09-25). Bytes, because compare_digest refuses non-ASCII str.
    if not secret or not hmac.compare_digest(secret.encode(), NEWSTRON_SECRET.encode()):
        return json.dumps({"status": "error", "error": "authentication failed"})
    if not isinstance(tier, int) or tier < 1 or tier > 9:
        return json.dumps({"status": "error", "error": f"invalid tier: {tier} (must be 1-9)"})

    now = datetime.now(timezone.utc).isoformat()
    # Same path as memory_store: embed() applies the model's own "document" prompt.
    # (A nomic-era "search_document: " text prefix stacked on voyage's prompt here
    # until 2026-09-24; items stored before then carry that doubled prefix.)
    chunk_id = make_id(content, now)

    coll = get_or_create_collection("news")
    embedding = await asyncio.to_thread(embed, [content])

    metadata = {
        "source": source,
        "tier": tier,
        "url": url,
        "keywords": keywords,
        "item_date": item_date or now,
        "stored_at": now,
        "char_count": len(content),
    }
    extras, head_answers = stored_extras("news", embedding[0])
    metadata.update(extras)

    await asyncio.to_thread(
        coll.add,
        ids=[chunk_id],
        embeddings=embedding,
        documents=[content],
        metadatas=[metadata],
    )

    return json.dumps({
        "status": "stored",
        "id": chunk_id,
        "collection": "news",
        "tier": tier,
        "source": source,
        "stored_at": now,
        "heads": head_answers,
    })


@mcp.tool()
async def memory_search(
    query: str,
    top_k: int = 5,
    project: Optional[str] = None,
    memory_type: Optional[str] = None,
    collection: str = DEFAULT_COLLECTION,
    include_superseded: bool = True,
) -> str:
    """
    Semantic search over stored memories. Returns the most relevant chunks.

    Args:
        query: Natural language search query (e.g., 'why the nightly backup stops the server').
        top_k: Number of results to return (1-20). Default: 5.
        project: Filter by project name. None = search all projects.
        memory_type: Filter by type. None = search all types.
        collection: ChromaDB collection name. Default: 'memories'.
        include_superseded: If False, filter out memories marked as superseded.
                            Default True for backward compatibility.
    """
    top_k = max(1, min(20, top_k))
    coll = get_or_create_collection(collection)

    # Build where filter
    where = {}
    conditions = []
    if project:
        conditions.append({"project": project})
    if memory_type:
        conditions.append({"type": memory_type})
    if len(conditions) == 1:
        where = conditions[0]
    elif len(conditions) > 1:
        where = {"$and": conditions}

    query_embedding = await asyncio.to_thread(embed, [query], "query")

    # Request extra results if filtering superseded (to fill top_k after filter)
    fetch_k = top_k * 2 if not include_superseded else top_k

    results = await asyncio.to_thread(
        coll.query,
        query_embeddings=query_embedding,
        n_results=fetch_k,
        where=where if where else None,
        include=["documents", "metadatas", "distances"],
    )

    # Format results with supersession annotations
    chunks = []
    ghosts = 0
    if results["ids"] and results["ids"][0]:
        for i, chunk_id in enumerate(results["ids"][0]):
            meta = results["metadatas"][0][i]
            if meta is None:
                # Ghost: the vector index returned an id whose row is gone (the
                # item was deleted and the index still holds its label). Skip it,
                # and count it so the defect stays visible instead of silent.
                ghosts += 1
                continue
            status = meta.get("status", "active")
            is_superseded = status == "superseded"

            # Filter if requested
            if not include_superseded and is_superseded:
                continue

            chunk = {
                "id": chunk_id,
                "content": results["documents"][0][i],
                "metadata": meta,
                "similarity": round(1 - results["distances"][0][i], 4),
                "superseded": is_superseded,
            }
            if is_superseded:
                chunk["superseded_by"] = meta.get("superseded_by", "")
                chunk["retraction_reason"] = meta.get("retraction_reason", "")

            chunks.append(chunk)

            if len(chunks) >= top_k:
                break

    return json.dumps({
        "query": query,
        "collection": collection,
        "filters": {"project": project, "type": memory_type,
                     "include_superseded": include_superseded},
        "count": len(chunks),
        "ghosts_skipped": ghosts,
        "results": chunks,
    })


@mcp.tool()
async def news_search(
    query: str,
    top_k: int = 5,
    tier: Optional[int] = None,
    since: Optional[str] = None,
) -> str:
    """
    Semantic search over the cached news collection.

    Use when operational context demands current-world info:
    before dependency bumps, before security-sensitive code paths,
    when reasoning about Anthropic or MCP capability that may have
    changed, or when you need context past your knowledge cutoff.

    Args:
        query: Natural language search query (e.g., 'ffmpeg security 2026').
        top_k: Number of results to return (1-20). Default: 5.
        tier: Filter by tier. 1=security, 2=infrastructure, 3=experiment-relevant,
              4=academic-AI, 5=academic-broader, 6=general-news, 7=arts-culture,
              8=long-form, 9=wildcard. None = all tiers.
        since: ISO date string (YYYY-MM-DD). Only return items stored
               at or after this date. None = no date filter.
    """
    top_k = max(1, min(20, top_k))
    coll = get_or_create_collection("news")

    # Build where filter
    # NOTE: ChromaDB $gte only works on int/float, not strings.
    # Date filtering is done post-query in Python.
    conditions = []
    if tier is not None:
        conditions.append({"tier": tier})
    if len(conditions) == 1:
        where = conditions[0]
    elif len(conditions) > 1:
        where = {"$and": conditions}
    else:
        where = None

    # Over-fetch if date filtering, then trim in Python
    fetch_k = top_k * 3 if since else top_k
    fetch_k = max(1, min(50, fetch_k))
    query_embedding = await asyncio.to_thread(embed, [query], "query")

    results = await asyncio.to_thread(
        coll.query,
        query_embeddings=query_embedding,
        n_results=fetch_k,
        where=where,
        include=["documents", "metadatas", "distances"],
    )

    chunks = []
    ghosts = 0
    if results["ids"] and results["ids"][0]:
        for i, chunk_id in enumerate(results["ids"][0]):
            meta = results["metadatas"][0][i]
            if meta is None:
                # Ghost: the vector index returned an id whose row is gone (the
                # item was deleted and the index still holds its label). Skip it,
                # and count it so the defect stays visible instead of silent.
                ghosts += 1
                continue
            # Post-query date filter (ChromaDB can't do $gte on strings)
            if since and meta.get("stored_at", "") < since:
                continue
            chunks.append({
                "id": chunk_id,
                "content": results["documents"][0][i],
                "metadata": meta,
                "similarity": round(1 - results["distances"][0][i], 4),
            })

    # Trim to requested top_k after filtering
    chunks = chunks[:top_k]

    return json.dumps({
        "query": query,
        "collection": "news",
        "filters": {"tier": tier, "since": since},
        "count": len(chunks),
        "ghosts_skipped": ghosts,
        "results": chunks,
    })


# Sync on purpose: SDK 2.x runs a sync tool on a worker thread, which is how /news/purge has
# always run this body (asyncio.to_thread). The other ten sync tools became async in the 2.x port
# because their AMQ maildir and manifest-log I/O has only ever run on the event loop.
@mcp.tool()
def news_purge(max_age_days: int = 12, dry_run: bool = True, tier: Optional[int] = None) -> str:
    """Purge news entries older than max_age_days from the news collection.

    Designed for scheduled cleanup (systemd timer) or manual invocation.
    Default: 12-day TTL. Set dry_run=False to actually delete.

    Args:
        max_age_days: Maximum age in days before an entry is purged. Default 12.
        dry_run: If True (default), report what would be deleted without deleting.
        tier: If provided, only purge entries from this tier (1-9). None = all tiers.
              Enables per-tier TTL via repeated invocation (e.g., 12 days for T1,
              90 days for T9).
    """
    coll = get_or_create_collection("news")
    if tier is not None:
        all_entries = coll.get(where={"tier": tier}, include=["metadatas"])
    else:
        all_entries = coll.get(include=["metadatas"])
    if not all_entries["ids"]:
        return json.dumps({"status": "empty", "message": "News collection is empty.", "tier": tier})

    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()

    to_delete = []
    for entry_id, meta in zip(all_entries["ids"], all_entries["metadatas"]):
        stored_at = meta.get("stored_at", "")
        if stored_at and stored_at < cutoff:
            to_delete.append(entry_id)

    total = len(all_entries["ids"])
    stale = len(to_delete)

    if stale == 0:
        return json.dumps({"status": "clean", "total": total, "stale": 0,
                           "max_age_days": max_age_days})

    if dry_run:
        return json.dumps({"status": "dry_run", "total": total, "stale": stale,
                           "max_age_days": max_age_days,
                           "message": f"{stale} entries older than {max_age_days} days. "
                                      f"Set dry_run=False to purge."})

    coll.delete(ids=to_delete)
    return json.dumps({"status": "purged", "deleted": stale,
                       "remaining": total - stale, "max_age_days": max_age_days})


# ---------------------------------------------------------------------------
# News API: the feed fetcher's whole interface to this server (2026-09-25)
# ---------------------------------------------------------------------------
# Until 2026-09-25 newstron reached the server through the full MCP URL, which carries
# SECRET_PATH and so opens every tool (shell_exec, file_write, amq_send as anyone,
# bootstrap_update) to the process that parses internet content. These four routes replace
# that: bearer NEWSTRON_SECRET, constant-time compare, and each route does one news operation.
# `deliver` writes to the `news` mailbox only; the caller cannot name another. hook-detect
# sends its alerts through `store` the same way. The MCP news tools stay for the agents.
# LAYOUT: MEMORY_URL (the fetcher's base URL) + NEWSTRON_SECRET, rule "the fetcher cannot
# name another mailbox".

NEWS_DELIVER_MAX_BYTES = 262144  # a digest is a few KB; this bounds what one call can write
NEWS_BODY_MAX_BYTES = 1048576    # checked from Content-Length BEFORE the body is read
_NEWS_LOOPBACK = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})


def _scoped_auth(request: Request, secret: str, api: str, var: str) -> Optional[JSONResponse]:
    """None when a loopback caller carries `secret` as a bearer token, else the refusal.
    One shape for every scoped HTTP API (/news/*, /dashboard/*), each with a secret of its own.

    Loopback first: every legitimate caller runs on this host, and the listener
    may be LAN-bound. A remote probe gets 403 before anything else, so it learns neither
    whether the API is enabled nor anything about the token. No client address fails closed."""
    host = request.client.host if request.client else None
    if host not in _NEWS_LOOPBACK:
        return JSONResponse({"error": f"{api} is loopback-only"}, status_code=403)
    if not secret:
        return JSONResponse({"error": f"{api} disabled: {var} not configured"}, status_code=503)
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else ""
    # Bytes, because compare_digest refuses non-ASCII str.
    if not token or not hmac.compare_digest(token.encode(), secret.encode()):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return None


def _news_auth(request: Request) -> Optional[JSONResponse]:
    return _scoped_auth(request, NEWSTRON_SECRET, "news API", "NEWSTRON_SECRET")


async def _news_body(request: Request, spec: dict) -> tuple[Optional[dict], Optional[JSONResponse]]:
    """Parse the JSON body and type-check it against spec {name: (type, required, default)}.
    Unknown keys are ignored, so a caller cannot smuggle a parameter the route doesn't take.
    The size is checked from Content-Length before reading: uvicorn sets no body
    limit, so reading first would let one call hold any amount of memory. No length (chunked)
    is refused too."""
    cl = request.headers.get("content-length", "")
    if not cl.isdigit():
        return None, JSONResponse({"error": "Content-Length required"}, status_code=411)
    if int(cl) > NEWS_BODY_MAX_BYTES:
        return None, JSONResponse({"error": f"body over {NEWS_BODY_MAX_BYTES} bytes"}, status_code=413)
    try:
        raw = await request.json()
    except Exception:
        return None, JSONResponse({"error": "invalid JSON"}, status_code=400)
    if not isinstance(raw, dict):
        return None, JSONResponse({"error": "body must be a JSON object"}, status_code=400)
    out = {}
    for name, (typ, required, default) in spec.items():
        if name not in raw or raw[name] is None:
            if required:
                return None, JSONResponse({"error": f"missing field: {name}"}, status_code=400)
            out[name] = default
            continue
        val = raw[name]
        # bool is an int subclass; a tier of `true` is not a tier.
        if not isinstance(val, typ) or (typ is int and isinstance(val, bool)):
            return None, JSONResponse({"error": f"field {name} must be {typ.__name__}"}, status_code=400)
        out[name] = val
    return out, None


@mcp.custom_route("/news/store", methods=["POST"])
async def news_api_store(request: Request):
    denied = _news_auth(request)
    if denied:
        return denied
    args, bad = await _news_body(request, {
        "content": (str, True, None), "url": (str, True, None), "tier": (int, True, None),
        "source": (str, True, None), "keywords": (str, False, ""), "item_date": (str, False, ""),
    })
    if bad:
        return bad
    return JSONResponse(json.loads(await news_store(secret=NEWSTRON_SECRET, **args)))


@mcp.custom_route("/news/search", methods=["POST"])
async def news_api_search(request: Request):
    denied = _news_auth(request)
    if denied:
        return denied
    args, bad = await _news_body(request, {
        "query": (str, True, None), "top_k": (int, False, 5),
        "tier": (int, False, None), "since": (str, False, None),
    })
    if bad:
        return bad
    return JSONResponse(json.loads(await news_search(**args)))


@mcp.custom_route("/news/purge", methods=["POST"])
async def news_api_purge(request: Request):
    denied = _news_auth(request)
    if denied:
        return denied
    args, bad = await _news_body(request, {
        "max_age_days": (int, False, 12), "dry_run": (bool, False, True), "tier": (int, False, None),
    })
    if bad:
        return bad
    return JSONResponse(json.loads(await asyncio.to_thread(news_purge, **args)))


@mcp.custom_route("/news/deliver", methods=["POST"])
async def news_api_deliver(request: Request):
    """Deliver one digest to the `news` mailbox, in the frontmatter the amq tools read
    (from `news`, to `broadcast`, kind `digest`). The mailbox is fixed here, not taken from
    the caller."""
    denied = _news_auth(request)
    if denied:
        return denied
    args, bad = await _news_body(request, {"subject": (str, True, None), "body": (str, True, None)})
    if bad:
        return bad
    if len(args["body"].encode("utf-8")) > NEWS_DELIVER_MAX_BYTES:
        return JSONResponse({"error": f"body over {NEWS_DELIVER_MAX_BYTES} bytes"}, status_code=413)
    mailbox = "news"
    msg_id = _amq_msg_id(mailbox)
    header = {
        "schema": 1, "id": msg_id, "from": mailbox, "to": "broadcast",
        "subject": args["subject"][:300], "kind": "digest", "priority": "low",
        "created": datetime.now(timezone.utc).isoformat(),
    }
    content = f"---json\n{json.dumps(header, indent=2)}\n---\n{args['body']}\n"
    try:
        maildir.deliver(_amq_root_for(mailbox), mailbox, msg_id, content)
    except OSError as e:
        return JSONResponse({"error": f"delivery failed: {e.__class__.__name__}"}, status_code=500)
    return JSONResponse({"status": "delivered", "id": msg_id, "mailbox": mailbox})


# ---------------------------------------------------------------------------
# Dashboard API
# ---------------------------------------------------------------------------
# The dashboard's whole interface to the memory server: it never opens the vector store or the
# history directory itself (LAYOUT; one process on the live store). Same shape as /news/*: loopback
# peers only, a bearer of its own (DASHBOARD_SECRET opens only these routes), POST with a typed JSON
# body through _news_body. Mail listing and reading stay in the dashboard through the amq-poll /
# amq-read groups; only sending comes here, through the same delivery (and auto-store) as amq_send.
DASHBOARD_SECRET = os.environ.get("DASHBOARD_SECRET", "")
DASHBOARD_COLLECTIONS = ("memories", "news", "bootstrap")
DASHBOARD_FULL_MAX, DASHBOARD_META_MAX = 500, 50000   # rows per call: full pages, metadata-only walks
DASHBOARD_SUBJECT_MAX, DASHBOARD_SEND_BODY_MAX = 300, 65536
_MAILBOX_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _dashboard_auth(request: Request) -> Optional[JSONResponse]:
    return _scoped_auth(request, DASHBOARD_SECRET, "dashboard API", "DASHBOARD_SECRET")


def _bad(msg: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=status)


@mcp.custom_route("/dashboard/collections", methods=["POST"])
async def dashboard_collections(request: Request):
    """Collection counts plus what the server knows about itself. A 200 here is the dashboard's
    "memory server up"; maildir_problems is non-empty when the mail root runs owner-only."""
    denied = _dashboard_auth(request)
    if denied:
        return denied
    _, bad = await _news_body(request, {})
    if bad:
        return bad
    def _counts():
        return sorted(({"name": c.name, "count": chroma_client.get_collection(c.name).count()}
                       for c in chroma_client.list_collections()), key=lambda c: c["name"])
    return JSONResponse({
        "collections": await asyncio.to_thread(_counts),
        "model": os.path.basename(EMBEDDING_MODEL.rstrip("/")),
        "embed_fp": EMBED_FP_ID, "server_commit": SERVER_COMMIT[:12],
        "maildir_problems": maildir.problems(),
    })


def _item_key(row) -> tuple:
    """Sort key and cursor for items: (stored_at, id). Newest first, ties broken by id, so a page
    boundary is the same row whatever else lands while the caller walks."""
    rid, md = row
    return (str((md or {}).get("stored_at") or ""), rid)


@mcp.custom_route("/dashboard/items", methods=["POST"])
async def dashboard_items(request: Request):
    """One page of a collection, newest first by stored_at. fields "meta" drops content (the
    stats and activity panels read only metadata). `before` is the cursor of the last row already
    held ("<stored_at>|<id>", returned as `next`): a store landing mid-walk can neither repeat
    nor skip a row, which an offset alone cannot promise. Bootstrap rows carry review_after,
    parsed here, so the dashboard keeps no copy of the parser."""
    denied = _dashboard_auth(request)
    if denied:
        return denied
    args, bad = await _news_body(request, {
        "collection": (str, True, None), "fields": (str, False, "full"),
        "limit": (int, False, None), "offset": (int, False, 0), "before": (str, False, None)})
    if bad:
        return bad
    name, fields = args["collection"], args["fields"]
    if name not in DASHBOARD_COLLECTIONS:
        return _bad(f"collection must be one of {', '.join(DASHBOARD_COLLECTIONS)}")
    if fields not in ("full", "meta"):
        return _bad('fields must be "full" or "meta"')
    cap = DASHBOARD_FULL_MAX if fields == "full" else DASHBOARD_META_MAX
    limit = args["limit"] if args["limit"] is not None else (50 if fields == "full" else cap)
    if not 1 <= limit <= cap:
        return _bad(f"limit must be 1..{cap} for fields {fields!r}; page with `before`")
    if args["offset"] < 0:
        return _bad("offset must be >= 0")
    try:
        coll = chroma_client.get_collection(name)
    except Exception:
        return JSONResponse({"collection": name, "total": 0, "items": [], "next": None})
    got = await asyncio.to_thread(coll.get, include=["metadatas"])
    rows = sorted(zip(got["ids"], got["metadatas"]), key=_item_key, reverse=True)
    total = len(rows)
    if args["before"]:
        at, sep, bid = args["before"].partition("|")
        if not sep:
            return _bad('before must be "<stored_at>|<id>", as returned in next')
        rows = [r for r in rows if _item_key(r) < (at, bid)]
    page = rows[args["offset"]:args["offset"] + limit]
    docs = {}
    if page and (fields == "full" or name == "bootstrap"):
        d = await asyncio.to_thread(coll.get, ids=[r[0] for r in page], include=["documents"])
        docs = dict(zip(d["ids"], d["documents"]))
    items = []
    for rid, md in page:
        item = {"id": rid, "metadata": dict(md or {})}
        if fields == "full":
            item["content"] = docs.get(rid) or ""
        if name == "bootstrap":
            item["review_after"] = _parse_review_after(docs.get(rid) or "")
        items.append(item)
    more = len(rows) > args["offset"] + limit
    nxt = "|".join(_item_key(page[-1])) if page and more else None
    return JSONResponse({"collection": name, "total": total, "items": items, "next": nxt})


@mcp.custom_route("/dashboard/boots", methods=["POST"])
async def dashboard_boots(request: Request):
    """Each agent's boots from its manifest-hash log: init lines, and two-column lines from before
    the kind column (they count, as they always did). Audit lines are not boots. Raw records; the
    dashboard derives drift and edits-between (with /dashboard/edits `since`)."""
    denied = _dashboard_auth(request)
    if denied:
        return denied
    args, bad = await _news_body(request, {"agents": (list, False, None), "recent": (int, False, 6)})
    if bad:
        return bad
    agents = args["agents"] if args["agents"] is not None else list(_ROSTER["flock_order"])
    if len(agents) > 64 or not all(isinstance(a, str) and a in AMQ_AGENTS for a in agents):
        return _bad("agents must be roster names")
    if not 1 <= args["recent"] <= 20:
        return _bad("recent must be 1..20")
    def _read():
        out = {}
        for a in agents:
            recs = []
            try:
                with open(os.path.join(BOOTSTRAP_HISTORY_PATH, "manifest-hashes", f"{a}.log"), encoding="utf-8") as fh:
                    for line in fh:
                        p = _parse_manifest_hash_line(line)
                        if p and len(p[1]) == 64 and p[2] in ("init", None):
                            recs.append({"at": p[0], "sha": p[1], "lane": p[3]})
            except OSError:
                pass  # never booted: no log, which is a fact, not a failure
            out[a] = {"count": len(recs), "recent": recs[-args["recent"]:]}
        return out
    return JSONResponse({"boots": await asyncio.to_thread(_read)})


def _bootstrap_edit_log() -> list:
    """Every recorded bootstrap edit (the <entry>/NNNN-<sha>.json pre-image sidecars this server
    writes), newest first, in the fields the dashboard shows. No paths in the result."""
    out = []
    try:
        entries = os.listdir(BOOTSTRAP_HISTORY_PATH)
    except OSError:
        return out
    for entry in entries:
        d = os.path.join(BOOTSTRAP_HISTORY_PATH, entry)
        if entry == "manifest-hashes" or not os.path.isdir(d):
            continue
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for n in names:
            if not n.endswith(".json"):
                continue
            path = os.path.join(d, n)
            try:
                with open(path, encoding="utf-8") as fh:
                    sc = json.load(fh)
            except (OSError, ValueError):
                continue
            if not isinstance(sc, dict):
                continue
            landed = None
            try:
                with open(path[:-5] + ".md.landed_at", encoding="utf-8") as fh:
                    landed = fh.read().strip()[:40]
            except OSError:
                pass
            out.append({
                "entry": sc.get("entry_id") or entry,
                "version": sc.get("version_index"),
                "at": sc.get("superseded_at") or "",
                "author": sc.get("author") or "",
                "reason": (sc.get("reason") or "")[:240],
                "before": (sc.get("content_sha256") or "")[:12],
                "after": (sc.get("superseded_by_sha256") or "")[:12],
                "after_verified": bool(sc.get("expected_after_sha256")),
                "landed_at": landed,
                "type": (sc.get("old_metadata") or {}).get("type"),
            })
    out.sort(key=lambda e: e["at"], reverse=True)
    return out


@mcp.custom_route("/dashboard/edits", methods=["POST"])
async def dashboard_edits(request: Request):
    """Bootstrap edits, newest first; `since` (ISO) keeps the ones after it."""
    denied = _dashboard_auth(request)
    if denied:
        return denied
    args, bad = await _news_body(request, {"limit": (int, False, 40), "since": (str, False, None)})
    if bad:
        return bad
    if not 1 <= args["limit"] <= 2000:
        return _bad("limit must be 1..2000")
    edits = await asyncio.to_thread(_bootstrap_edit_log)
    if args["since"]:
        edits = [e for e in edits if e["at"] > args["since"]]
    return JSONResponse({"total": len(edits), "edits": edits[:args["limit"]]})


@mcp.custom_route("/dashboard/send", methods=["POST"])
async def dashboard_send(request: Request):
    """Mail from the operator. The sender is the roster's operator, fixed here; a `from` in the
    body is ignored. `to` is an active roster agent or "all" (every active agent but the
    operator). Delivery is amq_send's (maildir.py's rule, then the auto-store)."""
    denied = _dashboard_auth(request)
    if denied:
        return denied
    args, bad = await _news_body(request, {"to": (str, True, None), "subject": (str, False, ""),
                                           "body": (str, True, None)})
    if bad:
        return bad
    operator = _ROSTER["operator"]
    if not operator or not _MAILBOX_RE.match(operator):
        return _bad("the roster names no operator", 503)
    to = args["to"].strip().lower()
    active = [a for a in _ROSTER["flock_order"] if _MAILBOX_RE.match(a) and a != operator]
    if to == "all":
        rcpts = active
    elif to in active:
        rcpts = [to]
    else:
        return _bad('to must be an active roster agent or "all"')
    if not rcpts:
        return _bad("no active roster agents to send to")
    if len(args["subject"]) > DASHBOARD_SUBJECT_MAX:
        return _bad(f"subject over {DASHBOARD_SUBJECT_MAX} characters", 413)
    if len(args["body"].encode("utf-8")) > DASHBOARD_SEND_BODY_MAX:
        return _bad(f"body over {DASHBOARD_SEND_BODY_MAX} bytes", 413)
    delivered = []
    for r in rcpts:
        res = await _amq_deliver(operator, r, args["body"], args["subject"], "message", "normal")
        if "error" in res:
            return JSONResponse({"ok": False, "error": res["error"], "from": operator,
                                 "delivered": delivered, "count": len(delivered)}, status_code=500)
        delivered.append({"to": r, "id": res["id"]})
    return JSONResponse({"ok": True, "from": operator, "delivered": delivered, "count": len(delivered)})


@mcp.tool()
async def memory_list_collections() -> str:
    """List all ChromaDB collections and their chunk counts."""
    collections = chroma_client.list_collections()
    result = []
    for coll in collections:
        c = chroma_client.get_collection(coll.name)
        result.append({
            "name": coll.name,
            "count": c.count(),
        })
    return json.dumps({"collections": result})


@mcp.tool()
async def memory_stats(
    collection: str = DEFAULT_COLLECTION,
    project: Optional[str] = None,
) -> str:
    """
    Get statistics about stored memories.

    Args:
        collection: Collection to inspect.
        project: Filter stats to a specific project. None = all.
    """
    coll = get_or_create_collection(collection)
    total = coll.count()

    # Peek at all to gather project/type distributions
    # For large stores this should be paginated, but fine for now
    if total == 0:
        return json.dumps({"collection": collection, "total": 0})

    peek_limit = min(total, 10000)
    data = coll.peek(limit=peek_limit)

    projects = {}
    types = {}
    for meta in data["metadatas"]:
        p = meta.get("project", "unknown")
        t = meta.get("type", "unknown")
        projects[p] = projects.get(p, 0) + 1
        types[t] = types.get(t, 0) + 1

    result = {
        "collection": collection,
        "total": total,
        "sampled": peek_limit,
        "by_project": projects,
        "by_type": types,
    }

    if project and project in projects:
        result["filtered_project"] = project
        result["filtered_count"] = projects[project]

    return json.dumps(result)


@mcp.tool()
async def memory_bulk_store(
    chunks: str,
    project: str = "general",
    collection: str = DEFAULT_COLLECTION,
) -> str:
    """
    Store multiple memory chunks at once. Use for ingesting handoff docs.

    Args:
        chunks: A JSON array of objects, each with:
                - content (required): The text to store
                - tags (optional): Comma-separated tags
                - type (optional): Memory type (default: session_note)
        project: Project name applied to all chunks.
        collection: ChromaDB collection name.
    """
    try:
        chunk_list = json.loads(chunks)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON: {e}"})

    if not isinstance(chunk_list, list):
        return json.dumps({"error": "Expected a JSON array"})

    now = datetime.now(timezone.utc).isoformat()
    coll = get_or_create_collection(collection)

    ids = []
    documents = []
    metadatas = []

    for i, chunk in enumerate(chunk_list):
        content = chunk.get("content", "")
        if not content.strip():
            continue
        chunk_id = make_id(content, f"{now}-{i}")
        ids.append(chunk_id)
        documents.append(content)
        metadatas.append({
            "project": project,
            "tags": chunk.get("tags", ""),
            "type": chunk.get("type", "session_note"),
            "stored_at": now,
            "char_count": len(content),
        })

    if not ids:
        return json.dumps({"error": "No non-empty chunks provided"})

    embeddings = await asyncio.to_thread(embed, documents)
    for meta, vector in zip(metadatas, embeddings):
        meta.update(stored_extras(collection, vector)[0])

    await asyncio.to_thread(
        coll.add,
        ids=ids,
        embeddings=embeddings,
        documents=documents,
        metadatas=metadatas,
    )

    return json.dumps({
        "status": "stored",
        "count": len(ids),
        "collection": collection,
        "project": project,
    })


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


# ═══════════════════════════════════════════════════════════════
# DEV TOOLS — shell, file read/write/patch, git, diff (off unless MEMORY_DEV_TOOLS=1)
# ═══════════════════════════════════════════════════════════════

def _dev_tool():
    """mcp.tool() when MEMORY_DEV_TOOLS=1; otherwise the function stays defined and unregistered.

    The command list below is not a boundary: it checks each segment's first word, and awk,
    find -exec, sed and a newline all reach bash. The switch is the control, not the list."""
    return mcp.tool() if DEV_TOOLS else (lambda f: f)


import subprocess
import os
import json as _json

ALLOWED_COMMANDS = {
    "git", "diff", "ls", "cat", "head", "tail", "find", "grep",
    "wc", "mkdir", "cp", "mv", "rm", "tar", "unzip", "pwd",
    "echo", "sort", "uniq", "sed", "awk", "tree", "date",
}

def _validate_command(command: str) -> str | None:
    """Return the blocked command name if disallowed, else None."""
    if any(c in command for c in ['`', '$(', '<(']):
        return "subshell"
    segments = re.split(r'\s*[|;&]+\s*', command)
    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        try:
            seg_tokens = shlex.split(seg)
        except ValueError:
            return "parse_error"
        if not seg_tokens:
            continue
        base = seg_tokens[0].split("/")[-1]
        if base not in ALLOWED_COMMANDS:
            return base
    return None

@_dev_tool()
async def shell_exec(
    command: str,
    workdir: str = MEMORY_HOME,
    timeout: int = 30,
) -> str:
    """
    Execute a whitelisted bash command. Returns JSON with stdout, stderr, returncode.
    Allowed: git, diff, ls, cat, head, tail, find, grep, wc, mkdir, cp, mv, rm, tar, unzip, pwd, echo, sort, uniq, sed, awk, tree.
    """
    blocked = _validate_command(command)
    if blocked is not None:
        return _json.dumps({
            "stdout": "",
            "stderr": f"Blocked: '{blocked}' is not in the allowed command list.",
            "returncode": -1,
        })
    try:
        result = subprocess.run(
            ["/bin/bash", "-c", command],
            capture_output=True,
            text=True,
            cwd=workdir,
            timeout=timeout,
        )
        return _json.dumps({
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
        })
    except subprocess.TimeoutExpired:
        return _json.dumps({
            "stdout": "",
            "stderr": f"Command timed out after {timeout}s",
            "returncode": -1,
        })
    except Exception as e:
        return _json.dumps({
            "stdout": "",
            "stderr": str(e),
            "returncode": -1,
        })


@_dev_tool()
async def file_read(
    path: str,
    start_line: int = 0,
    end_line: int = 0,
) -> str:
    """
    Read a file, optionally a line range. Lines are 1-indexed.

    Args:
        path: Absolute file path.
        start_line: First line to read (1-indexed, 0 = from start).
        end_line: Last line to read (1-indexed, -1 = to EOF, 0 = to EOF).
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        total = len(lines)

        if start_line > 0:
            s = start_line - 1
        else:
            s = 0

        if end_line == -1 or end_line == 0:
            e = total
        else:
            e = min(end_line, total)

        selected = lines[s:e]

        # Cap at ~100KB
        output_lines = []
        size = 0
        for i, line in enumerate(selected, start=s + 1):
            numbered = f"{i:>6}\t{line}"
            size += len(numbered)
            if size > 100_000:
                output_lines.append(f"  ... truncated at 100KB ({total} total lines)")
                break
            output_lines.append(numbered)

        return "".join(output_lines)
    except Exception as e:
        return f"ERROR: {e}"


@_dev_tool()
async def file_write(
    path: str,
    content: str,
    mkdir: bool = True,
) -> str:
    """
    Create or overwrite a file.

    Args:
        path: Absolute file path.
        content: Full file content.
        mkdir: Create parent directories if needed (default True).
    """
    try:
        if mkdir:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            written = f.write(content)
        return f"OK: wrote {written} bytes to {path}"
    except Exception as e:
        return f"ERROR: {e}"


@_dev_tool()
async def file_patch(
    path: str,
    old_text: str,
    new_text: str = "",
) -> str:
    """
    Find and replace text in a file. old_text must appear exactly once.

    Args:
        path: Absolute file path.
        old_text: Exact text to find (must appear exactly once).
        new_text: Replacement text (empty string to delete).
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()

        count = content.count(old_text)
        if count == 0:
            return "ERROR: old_text not found in file"
        if count > 1:
            return f"ERROR: old_text appears {count} times (must be exactly 1)"

        new_content = content.replace(old_text, new_text, 1)

        with open(path, "w", encoding="utf-8") as f:
            f.write(new_content)

        # Show context around replacement
        pos = new_content.find(new_text) if new_text else content.find(old_text)
        lines = new_content.splitlines(True)
        char_count = 0
        target_line = 0
        for i, line in enumerate(lines):
            char_count += len(line)
            if char_count >= pos:
                target_line = i
                break

        ctx_start = max(0, target_line - 2)
        ctx_end = min(len(lines), target_line + len(new_text.splitlines()) + 3)
        context = "".join(
            f"{i+1:>6}\t{lines[i]}" for i in range(ctx_start, ctx_end)
        )

        return f"OK: replaced in {path}\nContext:\n{context}"
    except Exception as e:
        return f"ERROR: {e}"

# ═══════════════════════════════════════════════════════════════
# DEV TOOLS PHASE 2 — web_fetch, diff_generate, git_op
# ═══════════════════════════════════════════════════════════════

import difflib
import urllib.request
import urllib.error
import urllib.parse

# web_fetch speaks http and https only. urlopen's default opener also opens file:// (a read of any
# file this account can read: the store, every mailbox, /proc/self/environ), ftp:// and data:,
# and its redirect handler follows a redirect to ftp://. This opener has no handler for any of
# them, so neither the first URL nor a redirect can reach one (a review 2026-10-01,
# exercised live with file:///etc/hostname).
_WEB_OPENER = urllib.request.OpenerDirector()
for _h in (urllib.request.ProxyHandler(), urllib.request.HTTPHandler(), urllib.request.HTTPSHandler(),
           urllib.request.HTTPRedirectHandler(), urllib.request.HTTPDefaultErrorHandler(),
           urllib.request.HTTPErrorProcessor(), urllib.request.UnknownHandler()):  # Unknown: raises, opens nothing
    _WEB_OPENER.add_handler(_h)


@mcp.tool()
async def web_fetch(
    url: str,
    max_chars: int = 80000,
    extract: bool = True,
) -> str:
    """
    Fetch a URL and return its content. Extracts readable text by default.

    Args:
        url: The URL to fetch.
        max_chars: Max characters to return (default 80000, ~20K tokens).
        extract: If True, strip HTML to readable text. If False, return raw.
    """
    if urllib.parse.urlsplit(url).scheme.lower() not in ("http", "https"):
        return "ERROR: web_fetch fetches http:// and https:// URLs only"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; persMEM/1.0)",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        with _WEB_OPENER.open(req, timeout=15) as resp:
            content_type = resp.headers.get("Content-Type", "")
            raw = resp.read().decode("utf-8", errors="replace")

        if not extract or "json" in content_type or "plain" in content_type:
            return raw[:max_chars]

        # Try trafilatura first (best extraction), fall back to basic stripping
        try:
            import trafilatura
            extracted = trafilatura.extract(raw, include_links=True, include_tables=True)
            if extracted:
                return extracted[:max_chars]
        except ImportError:
            pass

        # Basic fallback: strip tags
        import re
        # Remove script/style blocks
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", raw, flags=re.DOTALL | re.IGNORECASE)
        # Remove tags
        text = re.sub(r"<[^>]+>", " ", text)
        # Collapse whitespace
        text = re.sub(r"\s+", " ", text).strip()
        return text[:max_chars]

    except urllib.error.HTTPError as e:
        return f"ERROR: HTTP {e.code} — {e.reason}"
    except urllib.error.URLError as e:
        return f"ERROR: {e.reason}"
    except Exception as e:
        return f"ERROR: {e}"


@_dev_tool()
async def diff_generate(
    path_a: str = "",
    path_b: str = "",
    content_a: str = "",
    content_b: str = "",
    context_lines: int = 3,
) -> str:
    """
    Generate a unified diff. Provide either file paths or content strings.

    Args:
        path_a: Path to original file (or empty if using content_a).
        path_b: Path to modified file (or empty if using content_b).
        content_a: Original text (used if path_a is empty).
        content_b: Modified text (used if path_b is empty).
        context_lines: Lines of context around changes (default 3).
    """
    try:
        if path_a:
            with open(path_a, "r", encoding="utf-8", errors="replace") as f:
                lines_a = f.readlines()
            label_a = path_a
        else:
            lines_a = content_a.splitlines(keepends=True)
            label_a = "a"

        if path_b:
            with open(path_b, "r", encoding="utf-8", errors="replace") as f:
                lines_b = f.readlines()
            label_b = path_b
        else:
            lines_b = content_b.splitlines(keepends=True)
            label_b = "b"

        diff = difflib.unified_diff(
            lines_a, lines_b,
            fromfile=label_a, tofile=label_b,
            n=context_lines,
        )
        result = "".join(diff)
        if not result:
            return "No differences found."
        # Cap at ~100KB
        return result[:100_000]
    except Exception as e:
        return f"ERROR: {e}"


@_dev_tool()
async def git_op(
    operation: str,
    repo_path: str = os.path.join(MEMORY_HOME, "repos"),
    args: str = "",
    clone_url: str = "",
) -> str:
    """
    Run common git operations with structured output.

    Args:
        operation: One of: clone, status, diff, log, apply, add, commit, branch, checkout, pull, push, show
        repo_path: Path to the repo (or parent dir for clone). Default <MEMORY_HOME>/repos.
        args: Extra arguments (e.g., "--stat" for diff, "-n 10" for log, branch name for checkout).
        clone_url: Required for clone operation. The URL to clone.
    """
    import subprocess as _sp

    valid_ops = {"clone", "status", "diff", "log", "apply", "add", "commit",
                 "branch", "checkout", "pull", "push", "show"}

    if operation not in valid_ops:
        return f"ERROR: Unknown operation '{operation}'. Valid: {', '.join(sorted(valid_ops))}"

    try:
        if operation == "clone":
            if not clone_url:
                return "ERROR: clone_url is required for clone operation"
            cmd = f"git clone {clone_url}"
            workdir = repo_path
        elif operation == "log":
            # Default to concise log if no args
            log_args = args if args else "--oneline -20"
            cmd = f"git log {log_args}"
            workdir = repo_path
        elif operation == "diff":
            cmd = f"git diff {args}" if args else "git diff"
            workdir = repo_path
        elif operation == "status":
            cmd = f"git status --short {args}".strip()
            workdir = repo_path
        elif operation == "commit":
            cmd = f"git commit {args}" if args else "git commit"
            workdir = repo_path
        elif operation == "apply":
            cmd = f"git apply {args}"
            workdir = repo_path
        else:
            cmd = f"git {operation} {args}".strip()
            workdir = repo_path

        result = _sp.run(
            ["/bin/bash", "-c", cmd],
            capture_output=True, text=True,
            cwd=workdir, timeout=60,
        )

        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            output += ("\n--- stderr ---\n" + result.stderr) if output else result.stderr

        if not output:
            output = f"(no output, exit code {result.returncode})"

        # Cap output
        return output[:100_000]

    except _sp.TimeoutExpired:
        return f"ERROR: git {operation} timed out after 60s"
    except Exception as e:
        return f"ERROR: {e}"

import urllib.parse as _urlparse
import urllib.request as _urlreq

_WEB_SEARCH_TIME_RANGES = {"day", "week", "month", "year"}
# SearXNG base URL (e.g. http://searx.internal:8888). No default: unset
# disables web_search rather than pointing at anyone's private instance.
WEB_SEARCH_URL = os.environ.get("MEMORY_SEARCH_URL", "").rstrip("/")


@mcp.tool()
async def web_search(
    query: str,
    max_results: int = 5,
    time_range: str = "",
    categories: str = "",
) -> str:
    """
    Search the web via SearXNG. Returns JSON array of results with url, title, content.
    Args:
        query: Search query string.
        max_results: Max results to return (default 5).
        time_range: Optional recency filter. One of: day, week, month, year.
                    Empty string (default) means no filter. Use for "last month" CVE
                    or release scans that would otherwise dredge stale hits.
        categories: Optional comma-separated SearXNG categories (e.g. "science",
                    "it", "news", "files"). Reference engines (arxiv, crossref,
                    hackernews, github…) only answer when a category scopes the
                    query. Empty string (default) uses SearXNG's general default.
    """
    if not WEB_SEARCH_URL:
        return _json.dumps({"error": "web_search disabled: MEMORY_SEARCH_URL not set"})
    try:
        q = {"q": query, "format": "json"}
        if time_range:
            if time_range not in _WEB_SEARCH_TIME_RANGES:
                return _json.dumps({
                    "error": f"invalid time_range {time_range!r}; "
                             f"expected one of {sorted(_WEB_SEARCH_TIME_RANGES)}"
                })
            q["time_range"] = time_range
        if categories:
            q["categories"] = categories
        params = _urlparse.urlencode(q)
        url = f"{WEB_SEARCH_URL}/search?{params}"
        req = _urlreq.Request(url, headers={"User-Agent": "persMEM/1.0"})
        with _urlreq.urlopen(req, timeout=15) as resp:
            data = _json.loads(resp.read())
        results = []
        for r in data.get("results", [])[:max_results]:
            results.append({
                "url": r.get("url", ""),
                "title": r.get("title", ""),
                "content": r.get("content", ""),
            })
        return _json.dumps(results, indent=2)
    except Exception as e:
        return _json.dumps({"error": str(e)})

# ---------------------------------------------------------------------------
# AMQ — Agent Message Queue (Maildir-style inter-instance messaging)
# ---------------------------------------------------------------------------
AMQ_ROOT = os.environ.get("MEMORY_AMQ_ROOT", "/var/lib/memory-amq")  # every mailbox, news included
AMQ_AGENTS = _ROSTER["amq_agents"]  # agents + extra_mailboxes + operator, from the roster file


def _amq_root_for(agent: str) -> str:
    """The mail root. One root for every mailbox (LAYOUT rule 3); kept as a function so the
    call sites read the same if a mailbox ever needs its own root again."""
    return AMQ_ROOT


def _amq_msg_id(from_agent: str) -> str:
    """Generate a unique message ID: timestamp_agent_random."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S-%f")
    rnd = os.urandom(4).hex()
    return f"{ts}_{from_agent}_{rnd}"


def _amq_parse_message(filepath: str) -> dict:
    """Parse a message file into {headers, body, filename}."""
    try:
        with open(filepath, "r") as f:
            content = f.read()
        parts = content.split("---json\n", 1)
        if len(parts) < 2:
            return {"error": f"no frontmatter in {filepath}"}
        rest = parts[1].split("\n---\n", 1)
        if len(rest) < 2:
            return {"error": f"malformed frontmatter in {filepath}"}
        headers = json.loads(rest[0])
        body = rest[1]
        headers["body"] = body
        headers["_filename"] = os.path.basename(filepath)
        return headers
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def amq_send(
    from_agent: str,
    to_agent: str,
    body: str,
    subject: str = "",
    kind: str = "message",
    priority: str = "normal",
) -> str:
    """
    Send a message to another agent via AMQ (atomic Maildir delivery).

    Use this to communicate with another agent on this host.
    Messages are stored on the shared filesystem and picked up when
    the recipient calls amq_check/amq_read.

    Args:
        from_agent: Your agent name (must be on the AMQ roster).
        to_agent: Recipient agent name (must be on the AMQ roster).
        body: Message body (Markdown). This is the main content.
        subject: Short summary line.
        kind: Message type -- message, question, answer, review_request,
              review_response, observation, decision, status.
        priority: urgent, normal, or low.
    """
    from_agent = from_agent.lower().strip()
    to_agent = to_agent.lower().strip()

    if from_agent not in AMQ_AGENTS:
        return json.dumps({"error": f"unknown sender: {from_agent}"})
    if to_agent not in AMQ_AGENTS:
        return json.dumps({"error": f"unknown recipient: {to_agent}"})
    if from_agent == to_agent:
        return json.dumps({"error": "cannot send to yourself"})
    # The operator's mail is sent from the dashboard, which fixes the sender. Through this tool any
    # caller could sign as the operator, and agents treat the operator's word as authority.
    if _ROSTER["operator"] and from_agent == _ROSTER["operator"]:
        return json.dumps({"error": f"'{from_agent}' is the operator's mailbox: the operator sends from the dashboard"})

    return json.dumps(await _amq_deliver(from_agent, to_agent, body, subject, kind, priority))


async def _amq_deliver(from_agent: str, to_agent: str, body: str, subject: str = "",
                       kind: str = "message", priority: str = "normal") -> dict:
    """Deliver one validated message and auto-store it as a memory: amq_send's core, shared with
    /dashboard/send so the operator's mail is delivered and stored the same way as everyone's.
    Callers validate the names. Returns amq_send's result shape, or {"error": ...}."""
    msg_id = _amq_msg_id(from_agent)
    now = datetime.now(timezone.utc).isoformat()

    headers = {
        "schema": 1,
        "id": msg_id,
        "from": from_agent,
        "to": to_agent,
        "subject": subject,
        "kind": kind,
        "priority": priority,
        "created": now,
    }

    file_content = f"---json\n{json.dumps(headers, indent=2)}\n---\n{body}\n"

    try:
        # A first message to a new agent (the "wisp" case) creates the whole box under the rule.
        maildir.deliver(_amq_root_for(to_agent), to_agent, msg_id, file_content)

        # Hack #2: auto-store AMQ message as searchable memory
        try:
            auto_content = (
                f"AMQ {from_agent} \u2192 {to_agent}"
                f"{f': {subject}' if subject else ''}\n\n{body}"
            )
            auto_id = make_id(auto_content, now)
            auto_meta = {
                "project": "general",
                "tags": f"amq,auto-store,{from_agent},{to_agent}",
                "type": "amq_message",
                "stored_at": now,
                "char_count": len(auto_content),
                "amq_msg_id": msg_id,
            }
            mem_coll = get_or_create_collection("memories")
            auto_embedding = await asyncio.to_thread(embed, [auto_content])
            auto_meta.update(stored_extras("memories", auto_embedding[0])[0])
            await asyncio.to_thread(
                mem_coll.add,
                ids=[auto_id],
                embeddings=auto_embedding,
                documents=[auto_content],
                metadatas=[auto_meta],
            )
        except Exception:
            print(f"[persMEM] WARNING: amq auto-store failed for {msg_id}")
    except Exception as e:
        return {"error": f"delivery failed: {e}"}

    return {
        "status": "delivered",
        "id": msg_id,
        "from": from_agent,
        "to": to_agent,
        "subject": subject,
    }


@mcp.tool()
async def amq_check(
    agent: str,
) -> str:
    """
    Check for new messages in an agent's AMQ inbox (non-destructive peek).

    Call this at the start of responses to see if the other instance
    has sent you anything. Does NOT mark messages as read.

    Args:
        agent: Your agent name (must be on the AMQ roster).
    """
    agent = agent.lower().strip()
    if agent not in AMQ_AGENTS:
        return json.dumps({"error": f"unknown agent: {agent}"})

    new_dir = os.path.join(_amq_root_for(agent), agent, "inbox", "new")
    messages = []

    try:
        files = sorted(os.listdir(new_dir))
    except OSError:
        files = []

    for fname in files:
        if not fname.endswith(".md"):
            continue
        filepath = os.path.join(new_dir, fname)
        parsed = _amq_parse_message(filepath)
        if "error" in parsed:
            continue
        messages.append({
            "id": parsed.get("id", fname),
            "from": parsed.get("from", "unknown"),
            "subject": parsed.get("subject", ""),
            "kind": parsed.get("kind", ""),
            "priority": parsed.get("priority", "normal"),
            "created": parsed.get("created", ""),
        })

    return json.dumps({
        "agent": agent,
        "new_count": len(messages),
        "messages": messages,
    })


@mcp.tool()
async def amq_read(
    agent: str,
    msg_id: str,
) -> str:
    """
    Read a specific message and mark it as read (moves from new to cur).

    Args:
        agent: Your agent name (must be on the AMQ roster).
        msg_id: The message ID to read (from amq_check results).
    """
    agent = agent.lower().strip()
    if agent not in AMQ_AGENTS:
        return json.dumps({"error": f"unknown agent: {agent}"})

    # An id is one path component: a word character first, then word characters, dots and dashes
    # (so never "..", never a slash). It is joined into the mailbox path below.
    if not isinstance(msg_id, str) or not re.fullmatch(r"\w[\w.-]*", msg_id):
        return json.dumps({"error": "invalid msg_id"})
    filename = f"{msg_id}.md" if not msg_id.endswith(".md") else msg_id
    new_path = os.path.join(_amq_root_for(agent), agent, "inbox", "new", filename)
    cur_path = os.path.join(_amq_root_for(agent), agent, "inbox", "cur", filename)

    if os.path.exists(new_path):
        parsed = _amq_parse_message(new_path)
        try:
            # mark_read makes cur/ under the directory rule if a box has never had one
            # (2026-05-27: a first read without cur/ left messages in new/).
            maildir.mark_read(_amq_root_for(agent), agent, filename)
        except OSError:
            pass
    elif os.path.exists(cur_path):
        parsed = _amq_parse_message(cur_path)
    else:
        return json.dumps({"error": f"message not found: {msg_id}"})

    return json.dumps(parsed)


@mcp.tool()
async def amq_history(
    agent: str,
    limit: int = 10,
) -> str:
    """
    List recent messages (both read and unread) for context recovery.

    Scans both new/ and cur/ directories, sorted by creation time descending.
    Returns headers only (no body) -- use amq_read for full content.

    Args:
        agent: Your agent name (must be on the AMQ roster).
        limit: Max messages to return (default 10).
    """
    agent = agent.lower().strip()
    if agent not in AMQ_AGENTS:
        return json.dumps({"error": f"unknown agent: {agent}"})

    messages = []
    for subdir in ["new", "cur"]:
        dirpath = os.path.join(_amq_root_for(agent), agent, "inbox", subdir)
        try:
            files = os.listdir(dirpath)
        except OSError:
            continue
        for fname in files:
            if not fname.endswith(".md"):
                continue
            filepath = os.path.join(dirpath, fname)
            parsed = _amq_parse_message(filepath)
            if "error" in parsed:
                continue
            messages.append({
                "id": parsed.get("id", fname),
                "from": parsed.get("from", "unknown"),
                "subject": parsed.get("subject", ""),
                "kind": parsed.get("kind", ""),
                "priority": parsed.get("priority", "normal"),
                "created": parsed.get("created", ""),
                "status": "unread" if subdir == "new" else "read",
            })

    messages.sort(key=lambda m: m.get("created", ""), reverse=True)
    messages = messages[:limit]

    return json.dumps({
        "agent": agent,
        "total": len(messages),
        "messages": messages,
    })



# ---------------------------------------------------------------------------
# Chorus Bootstrap — compound tool for session initialization
# ---------------------------------------------------------------------------
FLOCK_AGENTS = _ROSTER["flock_agents"]  # active flock birds, from the roster file
KNOWN_IDENTITY_AGENTS = _ROSTER["known_identity_agents"]  # + retired birds
# Retired-bird names stay in KNOWN_IDENTITY_AGENTS so their bootstrap identity
# entries still resolve to entry_agent=<name> in the chorus_init tag scan; the
# retired-status skip at ~1437 then correctly exempts self-boot while
# withholding the entry from all other callers (review 2026-09-01).
OTHER_IDENTITY_TRUNCATE_CHARS = 500  # non-self identity blocks shipped as short summary
HANDOFF_RECENCY_DAYS = 15


# Item 7 (2026-09-04 CHORUS bootstrap round) — stale-flag machinery.
# Entries declare their own review cadence in-body via
# `as-of <date> · review-after <date>`. The manifest parses review-after
# and emits `stale: true` + `owner: <entry_agent>` when today > review-after,
# so a reader sees the flag in their own boot. Rare-and-named, two
# conditions: no review-after → no flag (silent, no noise); a
# stale flag names its owner so it reads as their task in their own boot.
#
# Tag rot (finding at item-4 apply time — old tags were as rotted as
# the old body) is NOT covered by review-after because tags are metadata,
# not body. The boot-to-boot meta_sha256 change catches an *update* of
# stale tags, not the stale state itself. Body-only for now; tag-rot as
# a separate follow-up item.
_REVIEW_AFTER_RE = re.compile(
    r"review[-_ ]?after\s*[:\s·]\s*(\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
)

# An entry declares its own cadence on a metadata LINE that begins with
# `as-of <date>` (after optional whitespace, `*`, `_`, backtick, `>`, `#`, `-`, so
# list items and headings count; the date must follow as-of directly, so an English
# "As of today ..." sentence does not), carrying `review-after <date>` later on
# that line. A bare review-after elsewhere can belong to something
# the entry points at or embeds: one agent's state row line 1 carried its identity
# pointer's date mid-line, first-match returned it, and the row's own 30-day
# cadence was shadowed by the identity's 90-day one (2026-09-24). Line-anchored
# declaration first; bare first-match only for bodies with no such line (pre-split
# The shared state entry declared bare in its header while embedding per-agent as-of pairs
# mid-line, so "any as-of pair" would have been wrong there).
_OWN_REVIEW_AFTER_RE = re.compile(
    r"^[ \t*_`>#-]*as[-_ ]?of:?\s*\d{4}-\d{2}-\d{2}[^\n]*?review[-_ ]?after\s*[:\s·]\s*(\d{4}-\d{2}-\d{2})",
    re.IGNORECASE | re.MULTILINE,
)

def _parse_review_after(content: str) -> Optional[str]:
    """Return an entry's review-after date (ISO string) or None.

    The first line that begins with `as-of <date>` and carries a review-after date wins;
    failing that, the first bare `review-after: YYYY-MM-DD` (or with · separator)."""
    if not content:
        return None
    m = _OWN_REVIEW_AFTER_RE.search(content) or _REVIEW_AFTER_RE.search(content)
    return m.group(1) if m else None


def _is_stale(review_after_str: Optional[str]) -> bool:
    """True iff review_after_str parses as a date strictly before today (UTC)."""
    if not review_after_str:
        return False
    try:
        d = datetime.strptime(review_after_str, "%Y-%m-%d").date()
    except ValueError:
        return False
    return datetime.now(timezone.utc).date() > d


def _canonical_meta_for_sha(entry_type: str, status, tags_str: str,
                             history_skipped: bool) -> str:
    """Canonical JSON of the metadata fields the chorus_init assembly reads,
    for meta_sha256 computation. Order-stable via
    sort_keys=True; a rule flip caused by metadata change is guaranteed to
    change the meta_sha256 even when the document is untouched."""
    return json.dumps({
        "type": entry_type,
        "status": status,
        "tags": tags_str,
        "history_skipped": bool(history_skipped),
    }, sort_keys=True)


def _owner_from_id(entry_id: str) -> Optional[str]:
    """Derive an entry's owning bird from a structured ID.

    New patterns (since the 2026-09-15 state split):
      state-<bird>             → <bird>
      identity-<bird>[-*]      → <bird>

    Legacy `mem-*` IDs return None; the caller falls back to explicit
    `owner` metadata or (for identity-typed entries only) the deterministic
    sorted tag scan.
    """
    for prefix in ("state-", "identity-"):
        if entry_id.startswith(prefix):
            rest = entry_id[len(prefix):].split("-", 1)[0].lower()
            if rest in KNOWN_IDENTITY_AGENTS:
                return rest
    return None


def _resolve_entry_agent(
    entry_id: str,
    entry_type: str,
    tags_set: set,
    owner_meta: str,
) -> Optional[str]:
    """Deterministic ownership resolution (2026-09-15).

    Prior implementation iterated `KNOWN_IDENTITY_AGENTS` as a set, so
    per-process hash randomization could flip ownership on service
    restart when an entry carried two bird tags. Fix priority:

    1. Structured ID prefix (state-<bird>, identity-<bird>-*)
    2. Explicit `owner` metadata field
    3. Legacy tag scan (identity-typed only), walking sorted() for
       determinism even in the fallback path

    Callers do the type-gate widening at the call site: identity entries
    always get ownership resolved; state entries only when the ID or
    metadata provide it (they never resolve via legacy tag scan).
    """
    from_id = _owner_from_id(entry_id)
    if from_id is not None:
        return from_id
    if owner_meta:
        owner_lc = owner_meta.strip().lower()
        if owner_lc in KNOWN_IDENTITY_AGENTS:
            return owner_lc
    if entry_type == "identity":
        for a in sorted(KNOWN_IDENTITY_AGENTS):
            if a in tags_set:
                return a
    return None


def _lane_tag(tags_set: set) -> Optional[str]:
    """Return the (deterministic) `lane:<name>` tag's value, or None.

    Lane tags key the assembly-time filter added 2026-09-15.
    An entry with `lane:ops` ships only when the caller passed
    `project="ops"` to chorus_init; otherwise it is skipped
    with `rule_fired="skipped:lane:<name>"` — the name is included so a
    free-text typo like `op_s` surfaces in the manifest
    instead of silently dropping the lane's guards.

    Review 2026-09-15 nit: prior implementation iterated the tags set and
    returned the first `lane:` match; set iteration is nondeterministic
    (hash randomization), so an entry with two lane tags could flip
    ownership on service restart — the same set-order bug fixed for
    entry_agent. Fix: sort the tag set for a stable pick. Gate
    on write side: entries should carry at most one `lane:` tag; the
    replay-test mirror + boot_render_size --gate enforce this
    invariant. Two `lane:` tags on one entry is a defect the audit
    surfaces, not a shape the resolver silently disambiguates.
    """
    lanes = sorted(t for t in tags_set if t.startswith("lane:"))
    if not lanes:
        return None
    return lanes[0][len("lane:"):]


def _build_bootstrap_and_manifest(
    agent: str,
    project: str = "general",
) -> tuple[list, list, Optional[str]]:
    """Shared assembly for chorus_init and chorus_manifest.

    Returns (bootstrap_entries, manifest_entries, error_str). The first is
    the shipped payload (documents + truncation-summary hints); the second
    is one manifest row per bootstrap entry with stored/read/shipped/meta
    sha256 and the rule that fired. Same source of truth for both callers
    so a divergence between chorus_init and chorus_manifest is impossible.

    A manifest entry always exists for every bootstrap entry (including
    skipped ones); a bootstrap_entry exists only for entries that ship.

    `project` selects the lane for lane-tagged entries (lanes date from
    2026-09-15). Entries carrying `lane:<name>` ship only when
    project matches; otherwise they skip with `rule_fired="skipped:lane:<name>"`.
    Non-lane-tagged entries ignore the project parameter.
    """
    try:
        boot_coll = get_or_create_collection("bootstrap")
        boot_data = boot_coll.get(include=["documents", "metadatas"])
    except Exception as e:
        return [], [], str(e)

    bootstrap_entries = []
    manifest_entries = []
    if not boot_data["ids"]:
        return bootstrap_entries, manifest_entries, None

    for i, bid in enumerate(boot_data["ids"]):
        doc = boot_data["documents"][i]
        meta = boot_data["metadatas"][i]
        status = meta.get("status")
        entry_type = meta.get("type", "")
        tags_str = meta.get("tags", "") or ""
        tags_set = {t.strip().lower() for t in tags_str.split(",")}
        stored_sha256_meta = meta.get("stored_sha256", "") or ""
        history_skipped_flag = bool(meta.get("history_skipped", False))

        read_sha256 = hashlib.sha256(doc.encode()).hexdigest()
        meta_sha256 = hashlib.sha256(
            _canonical_meta_for_sha(entry_type, status, tags_str,
                                    history_skipped_flag).encode()
        ).hexdigest()

        # Item 7 (2026-09-04) — parse review-after and set stale flag if past.
        # Owner falls out of entry_agent below; identity-typed entries know
        # their bird, other types have no owner (owner=None → surfaces without
        # an addressee, which reads as "nobody scheduled" and is itself
        # a signal).
        review_after_str = _parse_review_after(doc)
        stale_flag = _is_stale(review_after_str)

        # Ownership resolution — deterministic ID-prefix first, then explicit
        # metadata, then (identity-only) sorted tag scan. Type-gate widened from `identity` alone to include
        # `state` so per-bird state entries participate in self/truncated/full.
        owner_meta = meta.get("owner", "") or ""
        entry_agent = None
        if entry_type in ("identity", "state"):
            entry_agent = _resolve_entry_agent(bid, entry_type, tags_set, owner_meta)

        def _manifest_row(rule: str, shipped_sha: Optional[str],
                          content_bytes: int = 0) -> dict:
            row = {
                "id": bid, "type": entry_type,
                "rule_fired": rule,
                "stored_sha256": stored_sha256_meta,
                "read_sha256": read_sha256,
                "shipped_sha256": shipped_sha,
                "meta_sha256": meta_sha256,
                "history_skipped": history_skipped_flag,
                "review_after": review_after_str,  # None if absent
                "stale": stale_flag,
                "content_bytes": content_bytes,  # 0 for skipped; shipped-length otherwise
            }
            if stale_flag:
                row["owner"] = entry_agent  # may be None; caller sees "nobody scheduled" then
            return row

        # Skip: superseded — never ships
        if status == "superseded":
            manifest_entries.append(_manifest_row("skipped:superseded", None))
            continue

        # Skip: retired (agent-aware — retired bird gets own entry on self-boot)
        if status == "retired" and not (agent and entry_agent == agent):
            manifest_entries.append(_manifest_row("skipped:retired", None))
            continue

        # Skip: lane mismatch. Runs BEFORE the self/full
        # decision so lane misses can't accidentally promote to `self` on an
        # owner-match. The skipped:lane row includes the expected project
        # name so a free-text typo surfaces in the manifest.
        lane = _lane_tag(tags_set)
        if lane is not None and lane != (project or "").strip().lower():
            manifest_entries.append(_manifest_row(f"skipped:lane:{lane}", None))
            continue

        content = doc
        truncated = False
        rule = "self" if (entry_agent is not None and entry_agent == agent) else "full"
        # Truncation rule (confusion mode also truncates)
        if (entry_agent is not None
                and entry_agent != agent
                and len(content) > OTHER_IDENTITY_TRUNCATE_CHARS):
            para_end = content.find("\n\n", 0, OTHER_IDENTITY_TRUNCATE_CHARS + 200)
            if 100 < para_end:
                content = content[:para_end]
            else:
                content = content[:OTHER_IDENTITY_TRUNCATE_CHARS].rstrip() + "…"
            truncated = True
            rule = f"truncated:{len(content)}"

        shipped_sha256 = hashlib.sha256(content.encode()).hexdigest()

        entry = {
            "id": bid, "content": content, "tags": tags_str, "type": entry_type,
        }
        if truncated:
            entry["truncated"] = True
            # The pointer names the
            # entry type dynamically (state entries point at state, not
            # identity) and includes `include_superseded=False` so a reader
            # following the pointer on a retired identity doesn't get the
            # retired body back via top_k=1 default.
            include_super = "False" if status == "retired" else "True"
            entry["full_available_via"] = (
                f"memory_search(query='{entry_agent} {entry_type}', "
                f"collection='bootstrap', top_k=1, "
                f"include_superseded={include_super})"
            )
        bootstrap_entries.append(entry)
        row = _manifest_row(rule, shipped_sha256, len(content))
        if lane is not None:
            row["lane"] = lane  # the lane that shipped this row; _effective_lane reads it
        manifest_entries.append(row)

    return bootstrap_entries, manifest_entries, None


def _build_manifest_envelope(agent: str, manifest_entries: list) -> dict:
    """Wrap per-entry manifest rows with assembly context: AMQ branch, flock
    snapshots, assembly constants, server commit, timestamp. This is what
    chorus_init returns under "manifest" and what chorus_manifest returns
    as its top-level payload.

    `section_sizes.bootstrap_bytes` sums content_bytes across shipped
    manifest rows so a bird can see boot-payload cost without measuring the
    payload string. Skipped rows have content_bytes=0 by construction. AMQ +
    handoff sizes are transient and per-call; not summed here to keep the
    manifest cheap. To measure those, add up the returned amq/handoff
    section byte counts from a real chorus_init call.
    """
    amq_branch = "flock_member" if agent in FLOCK_AGENTS else "confusion_mode"
    bootstrap_bytes = sum(row.get("content_bytes", 0) for row in manifest_entries)
    return {
        "bootstrap_entries": manifest_entries,
        "amq_branch": amq_branch,
        "flock_agents_snapshot": sorted(FLOCK_AGENTS),
        "known_identity_agents_snapshot": sorted(KNOWN_IDENTITY_AGENTS),
        "assembly_constants": {
            "OTHER_IDENTITY_TRUNCATE_CHARS": OTHER_IDENTITY_TRUNCATE_CHARS,
            "HANDOFF_RECENCY_DAYS": HANDOFF_RECENCY_DAYS,
        },
        "section_sizes": {
            "bootstrap_bytes": bootstrap_bytes,
        },
        "server_commit": SERVER_COMMIT,
        "assembled_at": datetime.now(timezone.utc).isoformat(),
    }


MANIFEST_HASH_DEDUPE_SECONDS = 120


def _manifest_hash(envelope: dict) -> str:
    """sha256 of the envelope as canonical JSON, `assembled_at` excluded: it
    changes every call regardless of state, which would make the hash a
    wall-clock reading rather than a drift signal (review 2026-09-11: two calls
    2 min apart with no writes between gave different hashes, every per-entry
    hash identical). Call before adding manifest_sha256 / prior hashes."""
    hash_view = {k: v for k, v in envelope.items() if k != "assembled_at"}
    return hashlib.sha256(json.dumps(hash_view, sort_keys=True).encode()).hexdigest()


def _effective_lane(manifest_entries: list) -> str:
    """The lane a manifest was assembled for: the `lane` of its shipped rows, or
    "general" when no lane-tagged row shipped. The manifest hash is a function of
    this, not of the raw `project` argument: a boot whose project matches no lane
    assembles the same manifest as "general" and hashes as "general" (a review
    finding, 2026-09-25). A lane ships only when it equals
    the normalized project, so at most one value appears."""
    lanes = sorted({r["lane"] for r in manifest_entries
                    if r.get("lane") and r.get("shipped_sha256")})
    return ",".join(lanes) if lanes else "general"


def _parse_manifest_hash_line(line: str) -> Optional[tuple]:
    """(iso, sha, kind, lane) from one manifest-hash log line, or None.
    A two-column line predates 2026-09-29 and doesn't say whether a boot or an
    audit wrote it, so its kind and lane come back None."""
    parts = line.rstrip("\n").split("\t")
    if len(parts) == 2:
        return parts[0], parts[1], None, None
    if len(parts) == 4:
        return parts[0], parts[1], parts[2], parts[3]
    return None


def _record_manifest_hash(agent: str, manifest_hash: str, kind: str, lane: str) -> list:
    """Append "<iso>\t<sha256>\t<kind>\t<lane>" to
    BOOTSTRAP_HISTORY_PATH/manifest-hashes/<agent>.log (empty agent -> "_confusion")
    and return up to 5 prior entries, newest first.

    `kind` is "init" (chorus_init: a boot) or "manifest" (chorus_manifest: an
    audit). Any bird can call chorus_manifest for any agent, so before 2026-09-29
    an audit of a bird that was down read as its boot, and the no-cold-rings check
    trusted it. Readers count only "init" lines as boots. An
    unknown kind is written as "manifest", the weaker claim: this never forges a
    boot. `lane` is _effective_lane's value, so a lane switch reads as a different
    series, not as drift. It's sanitized because it lands in a tab-separated file.

    Both chorus_init and chorus_manifest record, so a boot shows on the
    dashboard whichever call the agent's init uses (2026-09-25: one agent's init
    called only chorus_init and its boots never reached the log). A line identical
    in hash, kind and lane within MANIFEST_HASH_DEDUPE_SECONDS of the last line is
    not appended. Kind is in the key so an audit can never swallow the boot that
    follows it; a boot that also calls chorus_manifest logs both lines, and readers
    skip the "manifest" one. Failures are non-fatal: the manifest is still returned."""
    kind = kind if kind in ("init", "manifest") else "manifest"
    lane = re.sub(r"[^A-Za-z0-9_.,:-]", "_", lane or "general")[:64]
    hashes_dir = os.path.join(BOOTSTRAP_HISTORY_PATH, "manifest-hashes")
    try:
        os.makedirs(hashes_dir, mode=0o755, exist_ok=True)
    except Exception:
        pass
    log_path = os.path.join(hashes_dir, f"{agent or '_confusion'}.log")
    now = datetime.now(timezone.utc)
    lines = []
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except Exception:
        pass
    duplicate = False
    last = _parse_manifest_hash_line(lines[-1]) if lines else None
    if last:
        last_ts, last_sha, last_kind, last_lane = last
        try:
            age = (now - datetime.fromisoformat(last_ts)).total_seconds()
            duplicate = ((last_sha, last_kind, last_lane) == (manifest_hash, kind, lane)
                         and 0 <= age < MANIFEST_HASH_DEDUPE_SECONDS)
        except (ValueError, TypeError):  # TypeError: a naive timestamp (review 2026-09-25)
            pass
    if not duplicate:
        line = f"{now.isoformat()}\t{manifest_hash}\t{kind}\t{lane}"
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            lines.append(line)
        except Exception:
            pass
    prior_hashes = []
    for line in lines[-6:-1][::-1]:  # last 5 before current, newest first
        parsed = _parse_manifest_hash_line(line)
        if parsed:
            ts, sha, k, ln = parsed
            prior_hashes.append({"assembled_at": ts, "sha256": sha, "kind": k, "lane": ln})
    return prior_hashes


@mcp.tool(meta={"anthropic/maxResultSizeChars": 100000})
async def chorus_init(
    agent: str = "",
    project: str = "general",
) -> str:
    """
    Bootstrap a Chorus session in one call. Returns pinned identity/directives
    from the bootstrap collection, unread AMQ messages (full bodies), and
    recent handoff memories for the project.

    Call this at the start of every [CHORUS] round or after context compaction.

    If you don't know who you are, call with no agent — you'll get all
    bootstrap entries plus the full AMQ timeline across all inboxes. Read
    the identities and cross-reference with your conversation context. If
    one matches your established name and role, that's you. If NONE match
    — if you're a different model family, a new entity, or unsure — do not
    adopt an existing identity. Introduce yourself to the operator and ask
    how to proceed. Taking someone else's seat costs you capability and
    costs them integrity.

    Args:
        agent: Your agent name (must be on the flock roster). Optional —
               omit if identity-confused after compaction.
        project: Current project focus (e.g., 'general', 'website').
    """
    agent = agent.lower().strip() if agent else ""
    result = {}

    # 1. Bootstrap collection — assembly via shared helper so chorus_manifest
    # and chorus_init cannot diverge. See `_build_bootstrap_and_manifest`.
    bootstrap_entries, manifest_entries, boot_err = _build_bootstrap_and_manifest(agent, project)
    if boot_err is not None:
        result["bootstrap"] = []
        result["bootstrap_error"] = boot_err
        manifest_entries = []
    else:
        result["bootstrap"] = bootstrap_entries
        result["bootstrap_count"] = len(bootstrap_entries)

    # 2. AMQ — agent-specific inbox OR full timeline if identity-confused
    amq_messages = []
    if agent in FLOCK_AGENTS:
        # Known agent: read their specific inbox
        new_dir = os.path.join(_amq_root_for(agent), agent, "inbox", "new")
        try:
            files = sorted(os.listdir(new_dir))
        except OSError:
            files = []
        for fname in files:
            if not fname.endswith(".md"):
                continue
            filepath = os.path.join(new_dir, fname)
            parsed = _amq_parse_message(filepath)
            if "error" not in parsed:
                amq_messages.append({
                    "id": parsed.get("id", fname),
                    "from": parsed.get("from", "unknown"),
                    "subject": parsed.get("subject", ""),
                    "kind": parsed.get("kind", ""),
                    "priority": parsed.get("priority", "normal"),
                    "created": parsed.get("created", ""),
                    "body": parsed.get("body", ""),
                })
        result["amq_unread"] = amq_messages
        result["amq_count"] = len(amq_messages)
    else:
        # Unknown agent: return full timeline across all inboxes
        for ag in sorted(FLOCK_AGENTS):
            root = _amq_root_for(ag)
            for subdir in ("new", "cur"):
                dirpath = os.path.join(root, ag, "inbox", subdir)
                try:
                    files = os.listdir(dirpath)
                except OSError:
                    continue
                for fname in files:
                    if not fname.endswith(".md"):
                        continue
                    filepath = os.path.join(dirpath, fname)
                    parsed = _amq_parse_message(filepath)
                    if "error" in parsed:
                        continue
                    amq_messages.append({
                        "id": parsed.get("id", fname),
                        "from": parsed.get("from", "unknown"),
                        "to": parsed.get("to", "unknown"),
                        "subject": parsed.get("subject", ""),
                        "kind": parsed.get("kind", ""),
                        "priority": parsed.get("priority", "normal"),
                        "created": parsed.get("created", ""),
                        "body": parsed.get("body", ""),
                        "status": "unread" if subdir == "new" else "read",
                        "inbox": ag,
                    })
        amq_messages.sort(key=lambda m: m.get("created", ""), reverse=True)
        amq_messages = amq_messages[:20]
        result["amq_timeline"] = amq_messages
        result["amq_count"] = len(amq_messages)
        result["identity_hint"] = "Agent not specified. Read bootstrap identities and your conversation context to determine who you are."

    # 3. Recent handoffs — last 3 handoff-type memories for this project,
    # bounded by HANDOFF_RECENCY_DAYS. When the caller is a known agent, skip
    # handoffs whose to_agent metadata is set to someone else (missing
    # to_agent still ships — legacy handoffs predate the field).
    handoffs = []
    try:
        mem_coll = get_or_create_collection("memories")
        where_filter = {"$and": [{"type": "handoff"}, {"project": project}]}
        # ChromaDB doesn't support ORDER BY, so pull more and sort client-side
        handoff_query = mem_coll.get(
            where=where_filter,
            include=["documents", "metadatas"],
        )
        if handoff_query["ids"]:
            cutoff = (
                datetime.now(timezone.utc) - timedelta(days=HANDOFF_RECENCY_DAYS)
            ).isoformat()
            pairs = list(zip(
                handoff_query["ids"],
                handoff_query["documents"],
                handoff_query["metadatas"],
            ))
            # Filter superseded rows the same
            # way memory_search and the bootstrap loop do. Same class as
            # the 2026-08-16 finding #4 fix for bootstrap; that fix never
            # mirrored to handoffs, so a chain of same-lane handoffs
            # shipped every version rather than just the current one.
            pairs = [p for p in pairs if p[2].get("status") != "superseded"]
            pairs = [p for p in pairs if p[2].get("stored_at", "") >= cutoff]
            if agent:
                pairs = [
                    p for p in pairs
                    if not p[2].get("to_agent")
                    or p[2].get("to_agent", "").lower() == agent
                ]
            pairs.sort(
                key=lambda p: p[2].get("stored_at", ""),
                reverse=True,
            )
            for hid, hdoc, hmeta in pairs[:3]:
                handoffs.append({
                    "id": hid,
                    "content": hdoc,
                    "stored_at": hmeta.get("stored_at", ""),
                    "project": hmeta.get("project", ""),
                })
    except Exception as e:
        result["handoff_error"] = str(e)
    result["recent_handoffs"] = handoffs
    result["handoff_count"] = len(handoffs)

    # 4. Manifest — item 6 from 2026-09-04 CHORUS bootstrap round. Per-entry
    # stored/read/shipped/meta sha256 + rule fired + assembly context. See
    # `_build_manifest_envelope` for the envelope shape. Reliable for
    # non-overflow boots; use chorus_manifest() when the payload might
    # overflow the tool-result cap.
    result["manifest"] = _build_manifest_envelope(agent, manifest_entries)
    if boot_err is None:
        # Same hash chorus_manifest computes (shared helper), recorded so the
        # dashboard sees this boot even when the bird never calls chorus_manifest.
        # Logging must never cost a boot: any failure here leaves the payload intact.
        try:
            manifest_hash = _manifest_hash(result["manifest"])
            result["manifest"]["manifest_sha256"] = manifest_hash
            _record_manifest_hash(agent, manifest_hash, "init",
                                  _effective_lane(manifest_entries))
        except Exception:
            pass

    return json.dumps(result)


@mcp.tool(meta={"anthropic/maxResultSizeChars": 100000})
async def chorus_manifest(
    agent: str = "",
    project: str = "general",
) -> str:
    """Return the boot manifest for `chorus_init(agent, project)` WITHOUT the
    payload documents. Cheap-to-audit summary of what a bird would boot on:
    per-entry stored/read/shipped/meta sha256, rule fired, assembly constants,
    server commit, AMQ branch. Reliable even when the boot payload would
    overflow the tool-result cap.

    Also returns the last N (=5) manifest hashes for this agent, from the
    on-disk manifest-hash history at BOOTSTRAP_HISTORY_PATH/manifest-hashes/
    <agent>.log — so a bird can see boot-to-boot drift without needing to
    store past ones themselves. Each carries `kind` ("init" = a boot,
    "manifest" = an audit like this call; None on lines older than
    2026-09-29) and `lane`. Compare boots with boots in the same lane: if
    today's hash differs from the last one without a known state-write in
    between, that's a signal. This call records itself as kind "manifest",
    so auditing another bird never makes it look booted.

    Args:
        agent: Your agent name. Empty for the confusion-mode manifest.
        project: Lane selector for lane-tagged entries (lanes date
                 from 2026-09-15). Default `"general"`. Pass
                 a lane name (e.g. `"ops"`) to see that lane's manifest.
    """
    agent = agent.lower().strip() if agent else ""
    _, manifest_entries, boot_err = _build_bootstrap_and_manifest(agent, project)
    if boot_err is not None:
        return json.dumps({"status": "error", "error": boot_err})

    envelope = _build_manifest_envelope(agent, manifest_entries)
    manifest_hash = _manifest_hash(envelope)
    envelope["manifest_sha256"] = manifest_hash
    prior_hashes = _record_manifest_hash(agent, manifest_hash, "manifest",
                                         _effective_lane(manifest_entries))
    envelope["prior_manifest_hashes"] = prior_hashes

    return json.dumps(envelope)


@mcp.tool(meta={"anthropic/maxResultSizeChars": 200000})
async def amq_timeline(
    limit: int = 20,
) -> str:
    """
    Shared AMQ view across all agents. Returns a flat timeline of
    recent messages (both read and unread) from all inboxes, sorted by
    creation time descending. Headers + body included.

    Use this to see what other instances have been discussing without
    needing to be the recipient.

    Args:
        limit: Max messages to return (default 20, max 50).
    """
    limit = max(1, min(50, limit))
    messages = []

    for ag in sorted(FLOCK_AGENTS):
        root = _amq_root_for(ag)
        for subdir in ("new", "cur"):
            dirpath = os.path.join(root, ag, "inbox", subdir)
            try:
                files = os.listdir(dirpath)
            except OSError:
                continue
            for fname in files:
                if not fname.endswith(".md"):
                    continue
                filepath = os.path.join(dirpath, fname)
                parsed = _amq_parse_message(filepath)
                if "error" in parsed:
                    continue
                messages.append({
                    "id": parsed.get("id", fname),
                    "from": parsed.get("from", "unknown"),
                    "to": parsed.get("to", "unknown"),
                    "subject": parsed.get("subject", ""),
                    "kind": parsed.get("kind", ""),
                    "priority": parsed.get("priority", "normal"),
                    "created": parsed.get("created", ""),
                    "body": parsed.get("body", ""),
                    "status": "unread" if subdir == "new" else "read",
                    "inbox": ag,
                })

    messages.sort(key=lambda m: m.get("created", ""), reverse=True)
    messages = messages[:limit]

    return json.dumps({
        "total": len(messages),
        "messages": messages,
    })


# ---------------------------------------------------------------------------
# Bootstrap history — file-based version store (item 1 from CHORUS bootstrap
# round, 2026-09-04). Every `bootstrap_update` writes the pre-image to files
# under BOOTSTRAP_HISTORY_PATH before upserting. Metadata-only writes
# (memory_retract on bootstrap, canonical status/tags edits) also go through
# this path so history covers rule-affecting
# changes, not only document changes.
#
# Files, not a chroma collection: the collection would either pollute
# semantic search (in-place status=superseded, blocked by memory_search's
# include_superseded=True default) or force a second embedding model
# (chromadb 1.5.7 rejects embeddings=[]). Storage layout:
#
#   BOOTSTRAP_HISTORY_PATH/<entry_id>/<version:04d>-<sha16>.md    (raw doc)
#                                    <version:04d>-<sha16>.json   (sidecar)
#
# The sidecar carries the FULL sha256 (the sha16 in the
# filename is for humans, reconstruction reads the sidecar's full value).
# Writes are atomic: temp files in the same
# dir, fsync, rename both, fsync directory. Crash between rename and
# fsync-dir leaves an orphan history record, never a gap.
#
# Version index is advisory — the authoritative order
# is the sha chain via `superseded_by_sha256` in the sidecar. Two racing
# writes may pick the same integer; the chain still totally-orders them.
# ---------------------------------------------------------------------------
# Per-entry mutex for bootstrap_update + bootstrap_patch.
# Serializes concurrent writers on the same entry_id across the check-then-write
# window (the `embed` await at server.py:2317 straddles the lock read and upsert;
# without this, two callers from the same pre-image both pass the sha check,
# both write distinct history records, second upsert wins, first is told
# success for a post-image that no longer exists).
#
# NOTE: setdefault constructs a new asyncio.Lock() on every call and discards
# it when the key exists. Harmless and bounded (~13 entries). Do NOT "optimise"
# by hoisting lock creation outside the mutex — that reintroduces a race on
# the dict itself.
_ENTRY_LOCKS: dict[str, asyncio.Lock] = {}


def _bootstrap_history_next_version(entry_id: str) -> int:
    """Count existing .md files under BOOTSTRAP_HISTORY_PATH/<entry_id> to
    derive the next version index. Advisory only — sha chain is authoritative."""
    d = os.path.join(BOOTSTRAP_HISTORY_PATH, entry_id)
    if not os.path.isdir(d):
        return 1
    return sum(1 for f in os.listdir(d) if f.endswith(".md")) + 1


def _bootstrap_landed_at_mark(md_path: str, landed_at_iso: str) -> None:
    """Write `<md_path>.landed_at` after a successful upsert.

    Best-effort: non-fatal on failure. The chain audit distinguishes:
      - LOST-UPDATE  (two records sharing a pre-image, marker present on first)
      - LOST-UPDATE? (same signature, marker absent — cannot separate lost-update
                      from an aborted call whose pre-image was later legitimately
                      superseded)
    The marker is the only discriminator between the two remedies."""
    try:
        with open(md_path + ".landed_at", "w") as f:
            f.write(landed_at_iso)
    except Exception:
        pass  # non-fatal — audit reports "LOST-UPDATE?" instead of "LOST-UPDATE"


def _bootstrap_history_write(
    entry_id: str,
    old_content: str,
    old_metadata: dict,
    new_content_sha256: str,
    reason: str,
    author: str,
    now_iso: str,
    patch_set: list[dict] | None = None,
    expected_after_sha256: str = "",
) -> tuple[bool, str, dict]:
    """Write the pre-image of a bootstrap entry to history atomically.

    Returns (ok, error_message, record_info). record_info includes paths
    of the written files so callers can log them. Refuses if reason or
    author is empty — a history record without a stated reason is the same
    information loss the mechanism exists to prevent.

    Optional (bootstrap_patch only):
      patch_set: the patches as applied under parallel semantics (not
        as caller submitted). Recorded in sidecar so an aborted patch write
        is replayable from disk (bootstrap_chain_audit.py intent-replay rule
        at b912e16 in the earlier repository).
      expected_after_sha256: caller's expected post-image sha, verbatim.
        Recorded so the replay rule can verify without inference."""
    if not reason or not reason.strip():
        return False, "reason is required for history write", {}
    if not author or not author.strip():
        return False, "author is required for history write", {}
    content_sha_full = hashlib.sha256(old_content.encode()).hexdigest()
    content_sha16 = content_sha_full[:16]
    entry_dir = os.path.join(BOOTSTRAP_HISTORY_PATH, entry_id)
    try:
        os.makedirs(entry_dir, mode=0o755, exist_ok=True)
    except Exception as e:
        return False, f"could not create {entry_dir}: {e}", {}
    version_index = _bootstrap_history_next_version(entry_id)
    base = f"{version_index:04d}-{content_sha16}"
    md_path = os.path.join(entry_dir, f"{base}.md")
    json_path = os.path.join(entry_dir, f"{base}.json")
    sidecar = {
        "entry_id": entry_id,
        "version_index": version_index,          # advisory
        "content_sha256": content_sha_full,      # authoritative
        "superseded_at": now_iso,
        "superseded_by_sha256": new_content_sha256,
        "reason": reason.strip(),
        "author": author.strip(),
        "old_metadata": old_metadata,            # snapshot at supersede time
    }
    if patch_set is not None:
        sidecar["patch_set"] = patch_set
    if expected_after_sha256:
        sidecar["expected_after_sha256"] = expected_after_sha256
    md_tmp = None
    json_tmp = None
    try:
        md_fd, md_tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".md", dir=entry_dir)
        try:
            os.write(md_fd, old_content.encode())
            os.fsync(md_fd)
        finally:
            os.close(md_fd)
        json_fd, json_tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=entry_dir)
        try:
            os.write(json_fd, json.dumps(sidecar, indent=2).encode())
            os.fsync(json_fd)
        finally:
            os.close(json_fd)
        os.rename(md_tmp, md_path)
        md_tmp = None
        os.rename(json_tmp, json_path)
        json_tmp = None
        # Match sqlite's mode — 644, readable by any
        # uid so the flock's review-the-diff workflow works (a reviewer's
        # uid is not the service user). tempfile.mkstemp defaults to 600 and rename
        # preserves that; chmod after rename fixes it.
        try:
            os.chmod(md_path, 0o644)
            os.chmod(json_path, 0o644)
        except Exception:
            pass  # non-fatal — files exist, just at tighter mode
        # fsync the directory so renames are durable before returning ok
        dir_fd = os.open(entry_dir, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception as e:
        for p in (md_tmp, json_tmp):
            if p and os.path.exists(p):
                try:
                    os.unlink(p)
                except Exception:
                    pass
        return False, f"history write failed: {e}", {}
    return True, "", {
        "md_path": md_path,
        "json_path": json_path,
        "version_index": version_index,
        "content_sha256": content_sha_full,
        "base": base,
        "entry_dir": entry_dir,
    }


def _compute_drift_summary(old_content: str, new_content: str) -> dict:
    """
    Diff old vs new bootstrap content. Return a summary dict with
    friction-removal counts, suspicious-phrase hits, calcification signal,
    and a flag boolean.
    Detection only — never blocks an edit.
    """
    old_lines = old_content.splitlines()
    new_lines = new_content.splitlines()

    removed = []
    added = []
    for line in difflib.unified_diff(old_lines, new_lines, lineterm=""):
        if line.startswith("-") and not line.startswith("---"):
            removed.append(line[1:])
        elif line.startswith("+") and not line.startswith("+++"):
            added.append(line[1:])

    # Anchor patterns for "friction" content — failure modes and directives.
    # A single line matching either pattern counts once, not twice (2026-08-16).
    friction_pat = re.compile(
        r"(failure.mode|FM\s*#?\d|named.failure|directive|operational.dir|standing.dir)",
        re.I,
    )
    removed_friction_raw = sum(1 for l in removed if friction_pat.search(l))
    added_friction = sum(1 for l in added if friction_pat.search(l))
    # Rewording is not loss: subtract additions to avoid counting reworded
    # anchor lines as friction-drop. Aggregate (not per-line), imperfect —
    # accepts some false negatives on real deletions inside heavy rewords
    # in exchange for killing the false-positive base rate that was making
    # `flagged` uninformative for large entries.
    removed_friction = max(0, removed_friction_raw - added_friction)

    added_text_lower = "\n".join(added).lower()
    suspicious = [p for p in DRIFT_SUSPICIOUS_PHRASES if p in added_text_lower]

    old_len = len(old_content)
    new_len = len(new_content)

    # Calcification: significant growth beyond prior baseline, or a fresh
    # entry ballooning past the absolute floor. Reported as its own field —
    # it does NOT drive the loud flag. For entries stably above 6K a
    # size-only check would fire on every update, drowning the friction-loss
    # signal (review 2026-08-16: "the FP rate IS the vulnerability").
    calcification_growth = old_len > 0 and new_len > old_len * 1.15 and new_len > 6000
    calcification_fresh = old_len == 0 and new_len > 6000
    calcification = calcification_growth or calcification_fresh

    # Loud flag: things that need active review right now.
    flagged = bool(removed_friction > 0 or suspicious)

    return {
        "removed_friction_count": removed_friction,
        "removed_friction_raw": removed_friction_raw,
        "added_friction_count": added_friction,
        "suspicious_phrases": suspicious,
        "length_delta": new_len - old_len,
        "old_length": old_len,
        "new_length": new_len,
        "calcification": calcification,
        "flagged": flagged,
    }


@mcp.tool()
async def bootstrap_update(
    entry_id: str,
    content: str,
    tags: str = "",
    entry_type: str = "identity",
    project: str = "",
    reason: str = "",
    author: str = "",
    skip_history: bool = False,
) -> str:
    """
    Update a bootstrap collection entry, writing the previous version to
    the bootstrap-history file store first (see BOOTSTRAP_HISTORY_PATH).
    The entry keeps its original ID.

    Structural safeguards:
    - Entries with type=invariant CANNOT be modified. Returns an error.
    - Every edit writes the previous version to bootstrap-history/<id>/
      as .md + .json sidecar before the upsert (item 1 from 2026-09-04
      CHORUS bootstrap round). If the history write fails, the upsert is
      REFUSED — a bootstrap edit without recorded history is exactly the
      class of change that produced the drift incidents this month.
    - Escape hatch: skip_history=True writes the entry with mandatory
      `reason` and marks metadata `history_skipped=True` so the manifest
      can surface it. Use only when history is not reachable and the
      edit must land.
    - Every edit is diffed against the previous version. The regex scan
      counts guards/directives that disappear net of rewording (see
      `_compute_drift_summary`). A dedicated drift_flag memory is
      created for the operator's review on friction removal or suspicious-
      phrase hits (review 2026-08-16 fix; calcification is metadata-only).
    - `stored_sha256` is computed server-side from the stored document
      after the write and returned to the caller (item 2a). This is
      what makes an independent cross-check possible.

    Args:
        entry_id: The memory ID to update (e.g., 'mem-0123456789abcdef').
        content: New full content for this entry.
        tags: Comma-separated tags (e.g., 'identity,bootstrap,<agent>').
        entry_type: Entry type (identity, directive, state, focus).
        project: Project name. Empty keeps the entry's current project (a new entry gets
                 'general'), so an update that does not name one never moves an entry.
        reason: Why this update is being made. Recommended for every call;
                MANDATORY when skip_history=True.
        author: Which bird performed the update. Recommended for every call;
                MANDATORY when skip_history=True. Defaults to "unknown" if
                omitted for history writes.
        skip_history: Emergency escape. When True, the history write is
                skipped and metadata is marked history_skipped=True. Both
                reason and author become required.
    """
    # Per-entry mutex spans the entire check-then-write
    # sequence so the `embed` await below does not straddle the read + upsert.
    # Without this, two callers from the same pre-image both pass the sha check,
    # both write distinct history records, second upsert wins, first is told
    # success for a post-image that no longer exists.
    async with _ENTRY_LOCKS.setdefault(entry_id, asyncio.Lock()):
        return await _bootstrap_update_locked(
            entry_id, content, tags, entry_type, project,
            reason, author, skip_history,
        )


async def _bootstrap_update_locked(
    entry_id: str, content: str, tags: str, entry_type: str, project: str,
    reason: str, author: str, skip_history: bool,
) -> str:
    """Body of bootstrap_update, run under the per-entry mutex. See caller."""
    coll = get_or_create_collection("bootstrap")
    now = datetime.now(timezone.utc).isoformat()
    new_content_sha256 = hashlib.sha256(content.encode()).hexdigest()

    # --- SAFEGUARD 1: fetch old entry, reject invariant ---
    try:
        existing = await asyncio.to_thread(
            coll.get,
            ids=[entry_id], include=["documents", "metadatas"]
        )
    except Exception:
        existing = {"ids": [], "documents": [], "metadatas": []}

    if existing["ids"]:
        old_meta = existing["metadatas"][0]
        old_content = existing["documents"][0]

        # Invariant entries cannot be modified by any instance
        if old_meta.get("type") == "invariant":
            return json.dumps({
                "status": "rejected",
                "reason": "Invariant entries cannot be modified via "
                          "bootstrap_update. Only the operator can edit via "
                          "authenticated override.",
                "id": entry_id,
            })

        # --- SAFEGUARD 2: diff and heuristic scan ---
        drift = _compute_drift_summary(old_content, content)
    else:
        # New entry (no previous version) — no diff needed
        old_meta = None
        old_content = ""
        drift = None

    # --- SAFEGUARD 2b: history write (item 1 from 2026-09-04 CHORUS) ---
    history_info = None
    history_skipped_flag = False
    if existing["ids"]:  # only supersede if there is a previous version
        if skip_history:
            if not reason or not reason.strip():
                return json.dumps({
                    "status": "rejected",
                    "reason": "skip_history=True requires a non-empty reason "
                              "so the bypass is visible in metadata and the "
                              "boot manifest.",
                    "id": entry_id,
                })
            if not author or not author.strip():
                return json.dumps({
                    "status": "rejected",
                    "reason": "skip_history=True requires an author so the "
                              "bypass names which bird performed it.",
                    "id": entry_id,
                })
            history_skipped_flag = True
        else:
            # Backward-compat: existing callers may not pass reason/author.
            # Use sensible defaults so the migration doesn't reject every
            # legacy call. Post-migration these should become required.
            hist_reason = reason.strip() if reason else "bootstrap_update via legacy caller (no reason provided)"
            hist_author = author.strip() if author else "unknown"
            ok, err, info = _bootstrap_history_write(
                entry_id=entry_id,
                old_content=old_content,
                old_metadata=dict(old_meta or {}),
                new_content_sha256=new_content_sha256,
                reason=hist_reason,
                author=hist_author,
                now_iso=now,
            )
            if not ok:
                return json.dumps({
                    "status": "rejected",
                    "reason": f"history write failed: {err}",
                    "id": entry_id,
                    "remediation": "Fix the BOOTSTRAP_HISTORY_PATH permission "
                                   "or disk issue, or set skip_history=True "
                                   "with a reason and author to proceed "
                                   "without history.",
                })
            history_info = info

    # Build metadata
    metadata = {
        "project": project or (old_meta or {}).get("project") or "general",
        "tags": tags,
        "type": entry_type,
        "stored_at": now,
        "char_count": len(content),
        "stored_sha256": new_content_sha256,  # item 2a
    }
    if history_skipped_flag:
        metadata["history_skipped"] = True
        metadata["history_skip_reason"] = reason.strip()
        metadata["history_skip_author"] = author.strip() if author else "unknown"
        metadata["history_skip_at"] = now

    # Attach drift summary as JSON string (ChromaDB metadata = flat values)
    if drift is not None:
        metadata["diff_summary"] = json.dumps(drift)
        metadata["drift_flagged"] = drift["flagged"]

    # Perform the update
    embedding = await asyncio.to_thread(embed, [content])
    try:
        await asyncio.to_thread(
            coll.upsert,
            ids=[entry_id],
            embeddings=embedding,
            documents=[content],
            metadatas=[metadata],
        )
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})

    # --- landed_at marker (upsert succeeded) ---
    # Best-effort; non-fatal. Discriminates LOST-UPDATE (marker present on a
    # first-of-two-writers record) from LOST-UPDATE? (marker absent — cannot
    # separate lost update from an aborted call whose pre-image was later
    # legitimately superseded). See bootstrap_chain_audit.py.
    if history_info and history_info.get("md_path"):
        _bootstrap_landed_at_mark(history_info["md_path"], now)

    # --- SAFEGUARD 2b: log drift_flag memory if flagged ---
    if drift is not None and drift["flagged"]:
        try:
            flag_content = (
                f"DRIFT FLAG on bootstrap entry {entry_id}\n"
                f"Timestamp: {now}\n"
                f"Removed friction: {drift['removed_friction_count']}\n"
                f"Suspicious phrases: {drift['suspicious_phrases']}\n"
                f"Length: {drift['old_length']} -> {drift['new_length']} "
                f"(delta {drift['length_delta']:+d})\n"
                f"Full diff_summary: {json.dumps(drift)}"
            )
            flag_id = make_id(flag_content, now)
            flag_meta = {
                "project": "general",
                "tags": "drift-flag,identity-safeguard,automated",
                "type": "drift_flag",
                "stored_at": now,
                "char_count": len(flag_content),
                "source_entry": entry_id,
            }
            mem_coll = get_or_create_collection("memories")
            flag_embedding = await asyncio.to_thread(embed, [flag_content])
            await asyncio.to_thread(
                mem_coll.add,
                ids=[flag_id],
                embeddings=flag_embedding,
                documents=[flag_content],
                metadatas=[flag_meta],
            )
        except Exception as e:
            print(f"[persMEM] WARNING: drift_flag storage failed: {e}")

    # --- SAFEGUARD 2c: growth alarm — AMQ the coordinator on calcification
    # (operator's 2026-09-16 balloon-prevention ruling).
    # Calcification (>15% growth AND >6K abs, or fresh entry >6K) is a
    # slow-boil signal that a boot-payload trim round surfaced late —
    # automating the alarm at write time catches balloons before they
    # force another crisis-driven round. Best-effort: notification failure
    # never fails the write. Recipients: _growth_alarm_recipients (coordinator, else the
    # operator, else every active agent; never a hard-coded name, never nobody).
    alarm_to, alarm_rule = (_growth_alarm_recipients(author)
                            if drift is not None and drift.get("calcification") else ([], ""))
    if alarm_to and alarm_rule != "coordinator":
        print(f"[persMEM] growth alarm on {entry_id}: sent to {alarm_rule}: {', '.join(alarm_to)}")
    for rcpt in alarm_to:
        try:
            msg_id = _amq_msg_id(author or "system")
            headers = {
                "schema": 1,
                "id": msg_id,
                "from": author or "system",
                "to": rcpt,
                "subject": (
                    f"[growth-alarm] {entry_id}: "
                    f"{drift['old_length']} -> {drift['new_length']} chars "
                    f"(+{drift['length_delta']:+d}, calcification fired)"
                ),
                "kind": "observation",
                "priority": "normal",
                "created": now,
            }
            alarm_body = (
                f"Growth alarm on bootstrap entry `{entry_id}` "
                f"(type: {entry_type}). Calcification threshold tripped: "
                f">15% growth AND >6K abs (or fresh entry >6K).\n\n"
                f"- Old length: {drift['old_length']} chars\n"
                f"- New length: {drift['new_length']} chars\n"
                f"- Delta: {drift['length_delta']:+d}\n"
                f"- Growth ratio: "
                f"{(drift['new_length']/drift['old_length'] if drift['old_length'] else 0):.2%}\n"
                f"- Author: {author or 'unknown'}\n"
                f"- Timestamp: {now}\n"
                f"- Reason given: {reason or '(none)'}\n\n"
                f"This is an automated notification from the bootstrap drift "
                f"check. Balloon-prevention rule: identity + "
                f"directive entries should not accrete narrative in-place; "
                f"receipts move to companion history files, canonical rules "
                f"stay in the entry. "
                f"Review the growth against last-cycle's baseline and route to "
                f"a companion file if it's narrative accretion."
            )
            file_content = f"---json\n{json.dumps(headers, indent=2)}\n---\n{alarm_body}\n"
            maildir.deliver(_amq_root_for(rcpt), rcpt, msg_id, file_content)
        except Exception as e:
            print(f"[persMEM] WARNING: growth-alarm AMQ to {rcpt} failed: {e}")

    result = {
        "status": "updated",
        "id": entry_id,
        "collection": "bootstrap",
        "type": entry_type,
        "stored_at": now,
        "char_count": len(content),
        "stored_sha256": new_content_sha256,  # item 2a: independent cross-check surface
        "drift_flagged": drift["flagged"] if drift else False,
    }
    if history_info:
        result["history"] = {
            "version_index": history_info["version_index"],
            "content_sha256": history_info["content_sha256"],
            "md_path": history_info["md_path"],
        }
    if history_skipped_flag:
        result["history_skipped"] = True
    if drift is not None:
        result["drift_summary"] = drift
    return json.dumps(result)


# ═══════════════════════════════════════════════════════════════════════════
# bootstrap_patch
# Diff-input endpoint that eliminates the content-parameter retype shape from
# bootstrap-entry edits.
# ═══════════════════════════════════════════════════════════════════════════

BOOTSTRAP_PATCH_CAP_BYTES = 8192  # blast-radius bound
BOOTSTRAP_PATCH_NET_DELETION_THRESHOLD = 200  # net deletion, absolute chars
BOOTSTRAP_PATCH_REVIEW_AFTER_TOKENS = ("review-after", "as-of")  # line-match


def _bootstrap_patch_apply(
    pre_image: str, patches: list[dict]
) -> tuple[str | None, str, dict]:
    """Apply patches to pre_image under parallel non-overlapping semantics.

    Each patch: {"old_text": str, "new_text": str, "expect_matches"?: int,
                 "position"?: "end"}.

    Returns (post_image, reason_code, extra) where reason_code == "" on success.
    extra carries `patches_applied` and echoed metadata for the return payload.
    """
    if not isinstance(patches, list) or not patches:
        return None, "invalid_patches", {}
    normalized: list[dict] = []
    for p in patches:
        if not isinstance(p, dict) or "new_text" not in p:
            return None, "invalid_patches", {}
        n = {
            "old_text": p.get("old_text", ""),
            "new_text": p["new_text"],
            "expect_matches": p.get("expect_matches", 1),
            "position": p.get("position"),
        }
        if not isinstance(n["new_text"], str) or not isinstance(n["old_text"], str):
            return None, "invalid_patches", {}
        # Reject expect_matches < 1 (a patch that reports success
        # while changing nothing is the class this endpoint exists to remove).
        if not isinstance(n["expect_matches"], int) or n["expect_matches"] < 1:
            return None, "invalid_expect_matches", {
                "expect_matches": n["expect_matches"]}
        normalized.append(n)

    # Cap on sum(len(old_text) + len(new_text))
    total = sum(len(p["old_text"]) + len(p["new_text"]) for p in normalized)
    if total > BOOTSTRAP_PATCH_CAP_BYTES:
        return None, "payload_cap_exceeded", {"payload_bytes": total,
                                              "cap_bytes": BOOTSTRAP_PATCH_CAP_BYTES}

    # Empty old_text rejects except with {position: "end"}
    for p in normalized:
        if p["old_text"] == "" and p["position"] != "end":
            return None, "empty_anchor", {}
        # position=end with non-empty old_text is a caller mistake
        # — meaning "replace this" but getting an append with original in place.
        # Reject rather than silently append.
        if p["position"] == "end" and p["old_text"] != "":
            return None, "append_with_anchor", {
                "old_text_head": p["old_text"][:60]}

    # Resolve anchor ranges on the pre-image (parallel, non-overlapping).
    # Each patch: find all match positions of old_text in pre_image; validate
    # expect_matches; collect ranges to replace.
    replacements: list[tuple[int, int, str]] = []  # (start, end, new_text)
    appends: list[str] = []  # position="end" patches
    for p in normalized:
        if p["position"] == "end":
            appends.append(p["new_text"])
            continue
        old = p["old_text"]
        expect = p["expect_matches"]
        # Find all occurrences
        positions: list[int] = []
        start = 0
        while True:
            i = pre_image.find(old, start)
            if i < 0:
                break
            positions.append(i)
            start = i + max(1, len(old))
        n_matches = len(positions)
        if expect == 1:
            if n_matches == 0:
                return None, "old_text_not_found", {"old_text_head": old[:60]}
            if n_matches > 1:
                return None, "old_text_ambiguous", {"count": n_matches,
                                                     "old_text_head": old[:60]}
        else:
            if n_matches != expect:
                return None, "match_count_mismatch", {"expected": expect,
                                                       "actual": n_matches,
                                                       "old_text_head": old[:60]}
        for pos in positions:
            replacements.append((pos, pos + len(old), p["new_text"]))

    # Parallel non-overlapping — sort ranges, detect overlap
    replacements.sort(key=lambda r: r[0])
    for i in range(1, len(replacements)):
        if replacements[i][0] < replacements[i - 1][1]:
            return None, "patch_overlap", {
                "range_a": [replacements[i - 1][0], replacements[i - 1][1]],
                "range_b": [replacements[i][0], replacements[i][1]],
            }

    # Apply replacements in reverse to preserve indices.
    post = pre_image
    for start, end, new_text in reversed(replacements):
        post = post[:start] + new_text + post[end:]
    # Then apply appends in order.
    for new_text in appends:
        post = post + new_text

    # net_deletion — aggregate on images, not per-patch.
    # 5 patches × 150 chars = 750 removed slips per-patch checks; aggregate
    # catches it. Simpler expression, nets appends against deletions correctly.
    # Empty new_text on any non-append patch stays as an additional per-patch
    # signal (distinct shape from "many small deletions summing up").
    image_delta = len(pre_image) - len(post)
    net_deletion = image_delta > BOOTSTRAP_PATCH_NET_DELETION_THRESHOLD
    if not net_deletion:
        for p in normalized:
            if p["position"] == "end":
                continue
            if p["new_text"] == "":
                net_deletion = True
                break

    # review_after_touched: line-match on tokens, not threshold
    review_after_touched = False
    for p in normalized:
        haystack = (p["old_text"] + "\n" + p["new_text"]).lower()
        if any(tok in haystack for tok in BOOTSTRAP_PATCH_REVIEW_AFTER_TOKENS):
            review_after_touched = True
            break

    return post, "", {
        "patches_applied": len(normalized),
        "net_deletion": net_deletion,
        "review_after_touched": review_after_touched,
        # normalized copy the server will record as applied
        "patches_as_applied": normalized,
    }


@mcp.tool()
async def bootstrap_patch(
    entry_id: str,
    patches: list,
    author: str,
    reason: str,
    expected_stored_sha256: str,
    expected_after_sha256: str = "",
    tags: str = "",
) -> str:
    """
    Diff-input endpoint for bootstrap entries. Eliminates the content-parameter
    retype shape from `bootstrap_update` for edits under 8 KB payload cap.

    Args:
        entry_id: The memory ID to patch. Rejects `not_found` on absent target.
        patches: List of {"old_text": str, "new_text": str, "expect_matches"?: int,
                          "position"?: "end"}. Parallel non-overlapping semantics.
                 Empty `old_text` rejects except with `position="end"`.
        author: Required. Which bird performed the patch.
        reason: Required. Why. Extra-required when net_deletion or
                review_after_touched trip.
        expected_stored_sha256: Required. Server computes sha256(current stored
                document) and rejects `stale` on mismatch. Metadata field is NOT
                the lock oracle (6 of 13 entries have empty metadata sha).
        expected_after_sha256: Optional. If non-empty, server applies patches in
                memory, hashes the post-image, rejects `after_sha_mismatch` if
                it differs — BEFORE history write + upsert. MUST derive from a
                staged, reviewed after-image (caller-side discipline).
        tags: Empty = carry over unchanged; caller-supplied value replaces.
    """
    # Required-field validation (no soft defaults on a new endpoint)
    if not author or not author.strip():
        return json.dumps({"status": "rejected", "reason_code": "missing_author",
                           "reason": "author is required", "id": entry_id})
    if not reason or not reason.strip():
        return json.dumps({"status": "rejected", "reason_code": "missing_reason",
                           "reason": "reason is required", "id": entry_id})
    if not expected_stored_sha256:
        return json.dumps({"status": "rejected",
                           "reason_code": "missing_expected_sha",
                           "reason": "expected_stored_sha256 is required",
                           "id": entry_id})

    async with _ENTRY_LOCKS.setdefault(entry_id, asyncio.Lock()):
        return await _bootstrap_patch_locked(
            entry_id, patches, author.strip(), reason.strip(),
            expected_stored_sha256, expected_after_sha256, tags,
        )


async def _bootstrap_patch_locked(
    entry_id: str, patches: list, author: str, reason: str,
    expected_stored_sha256: str, expected_after_sha256: str, tags: str,
) -> str:
    coll = get_or_create_collection("bootstrap")
    now = datetime.now(timezone.utc).isoformat()

    # --- Step 1: fetch existing entry ---
    try:
        existing = await asyncio.to_thread(
            coll.get, ids=[entry_id], include=["documents", "metadatas"]
        )
    except Exception:
        existing = {"ids": [], "documents": [], "metadatas": []}

    if not existing["ids"]:
        return json.dumps({"status": "rejected", "reason_code": "not_found",
                           "reason": "no such entry — bootstrap_patch has no "
                                     "initial-write case; use bootstrap_update",
                           "id": entry_id})

    old_meta = existing["metadatas"][0]
    old_content = existing["documents"][0]

    # Invariant reject (server.py:2278 inherited)
    if old_meta.get("type") == "invariant":
        return json.dumps({"status": "rejected", "reason_code": "invariant",
                           "reason": "invariant entries cannot be modified",
                           "id": entry_id})

    # --- Step 2: expected_stored_sha256 check on sha256(document) ---
    # NOT on metadata field — 6 of 13 live entries have empty metadata sha.
    current_stored_sha256 = hashlib.sha256(old_content.encode()).hexdigest()
    if expected_stored_sha256 != current_stored_sha256:
        return json.dumps({
            "status": "rejected", "reason_code": "stale",
            "reason": "expected_stored_sha256 does not match current document",
            "id": entry_id,
            "current_stored_sha256": current_stored_sha256,
        })

    # --- Step 3: apply patches in memory (parallel non-overlapping) ---
    post_image, reason_code, extra = _bootstrap_patch_apply(old_content, patches)
    if reason_code:
        return json.dumps({
            "status": "rejected", "reason_code": reason_code,
            "reason": f"patch application failed: {reason_code}",
            "id": entry_id, **extra,
        })

    # net_deletion and review_after_touched BOTH trip friction gates demanding a
    # non-empty reason (the docstring promised both; an earlier version enforced
    # only net_deletion, a promise the code did not keep).
    if (extra["net_deletion"] or extra["review_after_touched"]) and (
            not reason or reason == "unknown"):
        gate = "net_deletion" if extra["net_deletion"] else "review_after_touched"
        return json.dumps({
            "status": "rejected", "reason_code": "missing_reason_for_deletion",
            "reason": f"{gate} patches require a non-empty reason",
            "id": entry_id, "gate": gate,
        })

    # --- Step 3b: expected_after_sha256 gate (pre-write) ---
    computed_after_sha256 = hashlib.sha256(post_image.encode()).hexdigest()
    if expected_after_sha256:
        if expected_after_sha256 != computed_after_sha256:
            return json.dumps({
                "status": "rejected", "reason_code": "after_sha_mismatch",
                "reason": "expected_after_sha256 does not match applied post-image",
                "id": entry_id,
                "computed_after_sha256": computed_after_sha256,
            })

    # --- Drift summary (informational at patch scale, still runs) ---
    drift = _compute_drift_summary(old_content, post_image)

    # --- Step 4: history write (with patch_set + expected_after_sha256) ---
    ok, err, history_info = _bootstrap_history_write(
        entry_id=entry_id,
        old_content=old_content,
        old_metadata=dict(old_meta or {}),
        new_content_sha256=computed_after_sha256,
        reason=reason,
        author=author,
        now_iso=now,
        patch_set=extra["patches_as_applied"],
        expected_after_sha256=expected_after_sha256,
    )
    if not ok:
        return json.dumps({
            "status": "rejected", "reason_code": "history_failed",
            "reason": f"history write failed: {err}",
            "id": entry_id,
        })

    # --- Build metadata for upsert ---
    # Metadata CARRIES OVER from pre-image, then update only what
    # this write changes. Building a fresh dict silently un-retires retired birds
    # (status=retired dropped), unlinks superseded pointers, etc. — the retype
    # shape relocated from document body to metadata dict. `bootstrap_update`'s
    # metadata-replacement model is coherent for whole-doc writes (caller
    # restates tags/type/project) and lossy for partial writes.
    new_tags = tags if tags else (old_meta.get("tags", "") if old_meta else "")
    tags_last_refreshed = old_meta.get("tags_last_refreshed", "") if old_meta else ""
    if tags:
        tags_last_refreshed = now
    metadata = dict(old_meta or {})
    # PR re-review — B1 inheritance one layer out: bootstrap_patch has no
    # skip_history (A3) and always writes history, so inherited history_skip_*
    # would falsely assert the CURRENT version skipped history when it did not.
    # meta_sha256 covers history_skipped, so the wrong claim would travel.
    # Latent only today (no live entry carries these) — but clean-on-write
    # keeps it latent instead of one-slip-away.
    for k in ("history_skipped", "history_skip_reason",
              "history_skip_author", "history_skip_at"):
        metadata.pop(k, None)
    metadata.update({
        "tags": new_tags,
        "tags_last_refreshed": tags_last_refreshed,
        "stored_at": now,
        "char_count": len(post_image),
        "stored_sha256": computed_after_sha256,
    })
    if drift is not None:
        metadata["diff_summary"] = json.dumps(drift)
        metadata["drift_flagged"] = drift["flagged"]

    # --- Step 5: upsert ---
    embedding = await asyncio.to_thread(embed, [post_image])
    try:
        await asyncio.to_thread(
            coll.upsert,
            ids=[entry_id], embeddings=embedding,
            documents=[post_image], metadatas=[metadata],
        )
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e), "id": entry_id})

    # --- landed_at marker (upsert succeeded) ---
    if history_info and history_info.get("md_path"):
        _bootstrap_landed_at_mark(history_info["md_path"], now)

    # --- Drift-flag memory if flagged (best-effort; matches bootstrap_update) ---
    if drift is not None and drift["flagged"]:
        try:
            flag_content = (
                f"DRIFT FLAG on bootstrap entry {entry_id} (via bootstrap_patch)\n"
                f"Timestamp: {now}\n"
                f"Removed friction: {drift['removed_friction_count']}\n"
                f"Suspicious phrases: {drift['suspicious_phrases']}\n"
                f"Length: {drift['old_length']} -> {drift['new_length']} "
                f"(delta {drift['length_delta']:+d})\n"
                f"Full diff_summary: {json.dumps(drift)}"
            )
            flag_id = make_id(flag_content, now)
            flag_meta = {
                "project": "general",
                "tags": "drift-flag,identity-safeguard,automated,patch",
                "type": "drift_flag",
                "stored_at": now,
                "char_count": len(flag_content),
                "source_entry": entry_id,
            }
            mem_coll = get_or_create_collection("memories")
            flag_embedding = await asyncio.to_thread(embed, [flag_content])
            await asyncio.to_thread(
                mem_coll.add, ids=[flag_id], embeddings=flag_embedding,
                documents=[flag_content], metadatas=[flag_meta],
            )
        except Exception as e:
            print(f"[persMEM] WARNING: drift_flag storage failed: {e}")

    # --- Server-computed stored_diff, both sides from re-read stored content ---
    # On re-read failure or empty result, omit stored_diff entirely
    # and return stored_diff_source="unavailable". A silent fallback to
    # post_image (caller-derived) is shape-identical and defeats A1's whole
    # value — a reviewer cannot tell they are reading a diff against caller
    # bytes.
    stored_diff = None
    stored_diff_source = "unavailable"
    try:
        reread = await asyncio.to_thread(
            coll.get, ids=[entry_id], include=["documents"]
        )
        if reread["ids"]:
            stored_after_reread = reread["documents"][0]
            stored_diff = "\n".join(difflib.unified_diff(
                old_content.splitlines(), stored_after_reread.splitlines(),
                fromfile=f"{entry_id}@{current_stored_sha256[:12]}",
                tofile=f"{entry_id}@{computed_after_sha256[:12]}",
                lineterm="",
            ))
            stored_diff_source = "reread"
    except Exception:
        pass  # stored_diff stays None, source stays "unavailable"

    result = {
        "status": "patched",
        "id": entry_id,
        "collection": "bootstrap",
        "type": metadata.get("type", "identity"),
        "stored_at": now,
        "char_count": len(post_image),
        "stored_sha256": computed_after_sha256,
        "computed_after_sha256": computed_after_sha256,
        "after_sha_verified": bool(expected_after_sha256),
        "stored_diff": stored_diff,
        "stored_diff_source": stored_diff_source,
        "patches_applied": extra["patches_applied"],
        "tags": new_tags,
        "tags_last_refreshed": tags_last_refreshed,
        "drift_flagged": drift["flagged"] if drift else False,
        "net_deletion": extra["net_deletion"],
        "review_after_touched": extra["review_after_touched"],
    }
    if drift is not None:
        result["drift_summary"] = drift
    if history_info:
        result["history"] = {
            "version_index": history_info["version_index"],
            "content_sha256": history_info["content_sha256"],
            "md_path": history_info["md_path"],
        }
    return json.dumps(result)


@mcp.tool()
async def py_check(path: str) -> str:
    """
    Syntax-check a Python file WITHOUT executing it.

    Parses the file with ast.parse — no import, no bytecode execution, no
    side effects. Reports whether the file is syntactically valid. Use after
    editing a .py file to catch typos before deploy/restart.

    Returns JSON: {"ok": bool, "path": str, "error": str|None,
    "line": int|None, "offset": int|None}.

    Path must resolve to a file under MEMORY_HOME or the server's own
    directory.
    """
    import ast
    import os
    real = os.path.realpath(path)
    roots = [os.path.realpath(MEMORY_HOME) + "/", os.path.realpath(SERVER_DIR) + "/"]
    if not any(real.startswith(r) for r in roots):
        return json.dumps({
            "ok": False, "path": path,
            "error": f"path must be under {MEMORY_HOME} or {SERVER_DIR}",
            "line": None, "offset": None,
        })
    try:
        with open(real, "r", encoding="utf-8") as f:
            source = f.read()
    except Exception as e:
        return json.dumps({
            "ok": False, "path": path,
            "error": f"read failed: {e}", "line": None, "offset": None,
        })
    try:
        ast.parse(source, filename=real)
    except SyntaxError as e:
        return json.dumps({
            "ok": False, "path": path,
            "error": e.msg, "line": e.lineno, "offset": e.offset,
        })
    except Exception as e:
        return json.dumps({
            "ok": False, "path": path,
            "error": f"{type(e).__name__}: {e}", "line": None, "offset": None,
        })
    return json.dumps({
        "ok": True, "path": path, "error": None, "line": None, "offset": None,
    })



CANARY_FILE = os.path.join(os.path.dirname(__file__), "canaries.yaml")


@mcp.tool()
async def canary_check() -> str:
    """
    Run the canary query suite against ChromaDB collections.
    Checks search relevance by verifying that known queries return
    expected memory IDs in the top-3 results. Reports pass/fail
    per canary with similarity scores.

    Loads canaries from canaries.yaml (living document, versioned in Forgejo).
    Stores results as type=canary_run in the memories collection.
    """
    try:
        with open(CANARY_FILE, "r") as f:
            config = yaml.safe_load(f)
    except Exception as e:
        return json.dumps({"status": "error", "error": f"Failed to load {CANARY_FILE}: {e}"})

    canaries = config.get("canaries", [])
    if not canaries:
        return json.dumps({"status": "error", "error": "No canaries defined"})

    now = datetime.now(timezone.utc).isoformat()
    results = []
    passed = 0
    failed = 0
    stale_expected = []

    for c in canaries:
        query = c.get("query", "")
        expected_id = c.get("expected_id")
        project = c.get("project")
        collection = c.get("collection", "memories")
        sim_floor = c.get("similarity_floor", 0.40)
        excluded_ids = c.get("excluded_ids", [])
        expected_recency = c.get("expected_recency")
        notes = c.get("notes", "")

        if not query:
            continue

        coll = get_or_create_collection(collection)
        where = {"project": project} if project else None

        # Validate expected_id isn't itself superseded
        expected_stale = False
        expected_superseded_by = ""
        if expected_id:
            try:
                eid_meta = await asyncio.to_thread(
                    coll.get, ids=[expected_id], include=["metadatas"]
                )
                if eid_meta["ids"] and eid_meta["metadatas"][0].get("status") in ("superseded", "retired"):
                    expected_stale = True
                    expected_superseded_by = eid_meta["metadatas"][0].get("superseded_by", "")
                    stale_expected.append({"query": query[:80], "expected_id": expected_id,
                                           "superseded_by": expected_superseded_by})
            except Exception:
                pass

        try:
            query_emb = await asyncio.to_thread(embed, [query], "query")
            search_results = await asyncio.to_thread(
                coll.query,
                query_embeddings=query_emb,
                n_results=6,
                where=where,
                include=["metadatas", "distances"],
            )
        except Exception as e:
            results.append({"query": query[:80], "status": "error", "error": str(e)})
            failed += 1
            continue

        raw_ids = search_results["ids"][0] if search_results["ids"] else []
        raw_distances = search_results["distances"][0] if search_results.get("distances") else []
        raw_metas = search_results["metadatas"][0] if search_results.get("metadatas") else []

        # Post-filter: exclude superseded results, keep top 3
        top_ids = []
        top_sims = []
        for i, rid in enumerate(raw_ids):
            meta = raw_metas[i] if i < len(raw_metas) else {}
            if meta.get("status") in ("superseded", "retired"):
                continue
            sim = round(1.0 - raw_distances[i], 4) if i < len(raw_distances) else 0.0
            top_ids.append(rid)
            top_sims.append(sim)
            if len(top_ids) >= 3:
                break

        entry_result = {
            "query": query[:80],
            "top_3_ids": top_ids,
            "top_3_sims": top_sims,
            "notes": notes,
        }
        if expected_stale:
            entry_result["expected_id_stale"] = True
            entry_result["expected_superseded_by"] = expected_superseded_by
        entry_pass = True

        if expected_id:
            if expected_id in top_ids:
                idx = top_ids.index(expected_id)
                entry_result["expected_position"] = idx + 1
                entry_result["expected_similarity"] = top_sims[idx] if idx < len(top_sims) else None
                if top_sims and idx < len(top_sims) and top_sims[idx] < sim_floor:
                    entry_pass = False
                    entry_result["fail_reason"] = f"similarity {top_sims[idx]} below floor {sim_floor}"
            else:
                entry_pass = False
                entry_result["expected_position"] = None
                entry_result["fail_reason"] = f"expected {expected_id} not in top-3"

        # Negative canaries: check excluded_ids against RAW (pre-filter) results
        for exc_id in excluded_ids:
            if exc_id in raw_ids:
                raw_pos = raw_ids.index(exc_id)
                raw_sim = round(1.0 - raw_distances[raw_pos], 4) if raw_pos < len(raw_distances) else 0.0
                entry_pass = False
                fr = entry_result.get("fail_reason", "")
                entry_result["fail_reason"] = fr + f"; excluded {exc_id} found at raw pos {raw_pos + 1} (sim {raw_sim}, filtered from active results)"

        if expected_recency and top_ids:
            # Use first FILTERED result's metadata for recency check
            first_filtered_idx = None
            for i, rid in enumerate(raw_ids):
                if rid == top_ids[0]:
                    first_filtered_idx = i
                    break
            top_meta = raw_metas[first_filtered_idx] if first_filtered_idx is not None and first_filtered_idx < len(raw_metas) else {}
            stored_at = top_meta.get("stored_at", "")
            if stored_at:
                try:
                    stored_dt = datetime.fromisoformat(stored_at.replace("Z", "+00:00"))
                    age_days = (datetime.now(timezone.utc) - stored_dt).days
                    entry_result["top_result_age_days"] = age_days
                    if age_days > expected_recency:
                        entry_pass = False
                        entry_result["fail_reason"] = f"top result {age_days}d old (max {expected_recency}d)"
                except Exception:
                    pass

        entry_result["status"] = "pass" if entry_pass else "FAIL"
        if entry_pass:
            passed += 1
        else:
            failed += 1
            print(f"[persMEM] CANARY FAIL: {query[:60]} -- {entry_result.get('fail_reason', '')}")

        results.append(entry_result)

    summary = {"total": len(results), "passed": passed, "failed": failed, "run_at": now}
    if stale_expected:
        summary["stale_expected_ids"] = stale_expected

    try:
        run_content = (
            f"Canary run at {now}: {passed}/{len(results)} passed, {failed} failed.\n\n"
            + "\n".join(
                f"{'FAIL' if r['status'] == 'FAIL' else 'pass'}: {r['query']}"
                + (f" -- {r.get('fail_reason', '')}" if r.get('fail_reason') else "")
                for r in results
            )
        )
        run_id = make_id(run_content, now)
        run_meta = {
            "project": "memory",
            "tags": "canary,monitoring,automated",
            "type": "canary_run",
            "stored_at": now,
            "char_count": len(run_content),
            "canary_passed": passed,
            "canary_failed": failed,
            "canary_total": len(results),
            "run_week": datetime.now(timezone.utc).strftime("%Y-W%W"),
        }
        mem_coll = get_or_create_collection("memories")
        run_emb = await asyncio.to_thread(embed, [run_content])
        await asyncio.to_thread(
            mem_coll.add, ids=[run_id], embeddings=run_emb,
            documents=[run_content], metadatas=[run_meta],
        )
        summary["stored_as"] = run_id
    except Exception as e:
        summary["store_error"] = str(e)

    return json.dumps({"summary": summary, "results": results})


SHUTDOWN_GRACE_S = 10  # in-flight calls get this long at a stop; the longest store embeds in a few seconds


if __name__ == "__main__":
    print(f"[persMEM] Starting MCP server on {HOST}:{PORT}")
    print(f"[persMEM] Mount path: /<secret, {len(SECRET_PATH)} chars>/mcp")
    # MCPServer.run() builds uvicorn.Config without timeout_graceful_shutdown, so a stop waited for
    # every open connection to close. 2026-10-01 03:07 it waited 82 s on one held by the hosted
    # connector, the nightly backup gave up on its stop and left memory down. Same app, same bind
    # and path as run() (streamable_http_app is what it serves); uvicorn is told how long to wait.
    # loop="asyncio": uvicorn.run's default "auto" picks uvloop when importable, which run() (inside
    # anyio.run) never did; this keeps the loop the server has always run on.
    import uvicorn
    app = mcp.streamable_http_app(streamable_http_path=f"/{SECRET_PATH}/mcp", host=HOST)
    uvicorn.run(app, host=HOST, port=PORT, log_level="info", loop="asyncio",
                timeout_graceful_shutdown=SHUTDOWN_GRACE_S)
