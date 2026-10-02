#!/usr/bin/env python3
"""boot_render_size.py — measure what `chorus_init(agent=X)` would ship, per bird,
against the MCP tool-result cap. Read-only. Runnable by any bird (no credentials:
reads chroma.sqlite3 in ro mode and the roster file; never opens an AMQ inbox).

Why this exists (2026-09-15 boot-payload trim round): the cost
that matters is a threshold, not a byte count. When the rendered JSON exceeds the
harness cap (`MAX_MCP_OUTPUT_TOKENS`, default 25,000 tokens) every cold boot has
to slice a file. This tool renders the same dict `chorus_init` returns, minus the
AMQ section (unreadable across homes; add it from a real boot), and reports chars
and an estimated token count per active bird.

Modes:
  default            measure the live store under the live server rules
  --state-rule       model the proposed rule: state-typed entries carrying a bird
                     tag ship `self` to the owner and `truncated:N` to others,
                     exactly as identity entries do today
  --model DIR        overlay proposed entries from DIR (<id>.md body + <id>.json
                     metadata {type,tags,status}) on top of the store, so the
                     after-state is measured BEFORE any bootstrap_update lands
  --gate             exit 1 if any bird exceeds cap*(1-margin), or (with
                     --state-rule) if any per-bird state entry's first paragraph
                     ends past OTHER_IDENTITY_TRUNCATE_CHARS — that is the roster
                     line the other birds boot on, and the server hard-cuts it at
                     500 chars mid-sentence otherwise (server.py `_build_bootstrap_and_manifest`)

Fidelity: entries are emitted sorted by id, the server emits them in chroma
order — same bytes, different order. Verified 2026-09-15 against a real
`chorus_init(agent=<one agent>)`: tool 98,707 chars, real boot 98,708 (the one
char is `assembled_at` microseconds); bootstrap section byte-equal, entries
equal as sets.

Token estimate: chars / --chars-per-token (default 4.0). Calibration, two samples
from the 2026-08-16 payload-budget log: ~116 kB ≈ 29K tokens, ~52 kB ≈ 13K tokens.
It is an estimate; calibrate against a real overflow/inline pair when in doubt.
"""
import argparse, hashlib, json, os, re, sqlite3, sys
from datetime import datetime, timedelta, timezone

DB = os.path.join(os.environ.get("MEMORY_DATA_DIR", "/var/lib/memory/chromadb"), "chroma.sqlite3")
TRUNC = 500              # OTHER_IDENTITY_TRUNCATE_CHARS
HANDOFF_DAYS = 15        # HANDOFF_RECENCY_DAYS
_REVIEW_AFTER_RE = re.compile(r"review[-_ ]?after\s*[:\s·]\s*(\d{4}-\d{2}-\d{2})", re.I)
_OWN_REVIEW_AFTER_RE = re.compile(r"^[ \t*_`>#-]*as[-_ ]?of:?\s*\d{4}-\d{2}-\d{2}[^\n]*?review[-_ ]?after\s*[:\s·]\s*(\d{4}-\d{2}-\d{2})", re.I | re.M)  # mirrors server.py: line-anchored declaration first

def sha(t): return hashlib.sha256(t.encode()).hexdigest()

def load_roster():
    env = os.environ.get("COTERIE_AGENTS_JSON", "")
    cands = [env] if env else ["/etc/coterie/agents.json"]
    path = next((p for p in cands if os.path.isfile(p)), None)
    if not path:
        sys.exit(f"no roster file (looked in {cands})")
    cfg = json.load(open(path, encoding="utf-8"))
    retired = {str(n).lower() for n in cfg.get("retired", [])}
    order = []
    for b in cfg.get("agents", []):
        n = str(b.get("name", "")).lower()
        if b.get("retired"): retired.add(n)
        elif n and n not in retired: order.append(n)
    return path, order, set(order) | retired

def load_collection(con, name):
    rows = con.execute("""select e.embedding_id, m.key, m.string_value, m.int_value, m.float_value, m.bool_value
        from embedding_metadata m join embeddings e on e.id=m.id join segments s on s.id=e.segment_id
        join collections col on col.id=s.collection where col.name=?""", (name,)).fetchall()
    ents = {}
    for eid, k, sv, iv, fv, bv in rows:
        v = sv if sv is not None else (iv if iv is not None else (fv if fv is not None else bv))
        ents.setdefault(eid, {})[k] = v
    return ents

def load_handoffs(con, project, agent, skip_superseded=False):
    """Mirrors chorus_init's handoff query (type=handoff, project, 15-day floor,
    to_agent). The server does NOT skip status=superseded here (it does for
    bootstrap entries) — receipt 2026-09-15: three handoffs to one agent shipped on a
    lane-scoped boot, two of them explicitly superseded by the third.
    --handoff-skip-superseded models the proposed fix."""
    ids = [r[0] for r in con.execute("""select e.embedding_id from embedding_metadata m
        join embeddings e on e.id=m.id join segments s on s.id=e.segment_id
        join collections col on col.id=s.collection
        where col.name='memories' and m.key='type' and m.string_value='handoff'""")]
    if not ids: return []
    q = ",".join("?" * len(ids))
    rows = con.execute(f"""select e.embedding_id, m.key, m.string_value from embedding_metadata m
        join embeddings e on e.id=m.id where e.embedding_id in ({q})""", ids).fetchall()
    ents = {}
    for eid, k, sv in rows: ents.setdefault(eid, {})[k] = sv
    cutoff = (datetime.now(timezone.utc) - timedelta(days=HANDOFF_DAYS)).isoformat()
    pairs = [(i, m) for i, m in ents.items()
             if m.get("project") == project and (m.get("stored_at") or "") >= cutoff
             and not (skip_superseded and m.get("status") == "superseded")]
    if agent:
        pairs = [(i, m) for i, m in pairs if not m.get("to_agent") or m["to_agent"].lower() == agent]
    pairs.sort(key=lambda p: p[1].get("stored_at", ""), reverse=True)
    return [{"id": i, "content": m.get("chroma:document", ""), "stored_at": m.get("stored_at", ""),
             "project": m.get("project", "")} for i, m in pairs[:3]]

def overlay_model(ents, model_dir):
    """Proposed entries: <id>.md is the body, <id>.json is {type,tags,status,...}.
    A .md WITHOUT its .json sidecar is not an entry (a README, notes) and is
    skipped — receipt 2026-09-15: model-AB/README.md was silently overlaid as
    a bootstrap entry and added 1,578 chars to one run."""
    loaded = []
    for fn in sorted(os.listdir(model_dir)):
        if not fn.endswith(".md"): continue
        eid = fn[:-3]
        meta_path = os.path.join(model_dir, eid + ".json")
        if not os.path.isfile(meta_path):
            print(f"[model] skipping {fn}: no {eid}.json sidecar, not an entry", file=sys.stderr)
            continue
        body = open(os.path.join(model_dir, fn), encoding="utf-8").read()
        meta = json.load(open(meta_path))
        loaded.append(eid)
        row = dict(ents.get(eid, {}))
        row.update(meta)
        row["chroma:document"] = body
        ents[eid] = row
    print(f"[model] overlaid {len(loaded)} entries: {' '.join(loaded)}", file=sys.stderr)
    return ents

def assemble(agent, ents, known, state_rule, shared_id=None,
             project="general", para_max=500):
    boot, man, gate_fail = [], [], []
    today = datetime.now(timezone.utc).date()
    for eid, m in sorted(ents.items()):
        doc = m.get("chroma:document", "") or ""
        status = m.get("status"); typ = m.get("type", "") or ""
        tags_str = m.get("tags", "") or ""
        tags = {t.strip().lower() for t in tags_str.split(",")}
        # Lane rule: an entry tagged lane:<name> ships only when the
        # caller's `project` is that lane; otherwise skipped:lane. Modelled only
        # under --state-rule (it is part of the same proposed server change).
        lanes = {t[5:] for t in tags if t.startswith("lane:")}
        ra = _OWN_REVIEW_AFTER_RE.search(doc) or _REVIEW_AFTER_RE.search(doc); ra = ra.group(1) if ra else None
        stale = False
        if ra:
            try: stale = today > datetime.strptime(ra, "%Y-%m-%d").date()
            except ValueError: pass
        entry_agent = None
        if typ == "identity" or (state_rule and typ == "state"):
            entry_agent = next((a for a in sorted(known) if a in tags), None)
        if state_rule and eid == shared_id and entry_agent is not None:
            # Tag-keyed fragility: a bird
            # name in the SHARED state entry's tags would truncate the roster for
            # every other bird. Same class as an identity losing its name tag.
            gate_fail.append(f"{eid}: shared state entry resolves to bird {entry_agent!r} "
                             f"via tags {tags_str!r}; it would ship truncated to the other birds")
        meta_sha = sha(json.dumps({"type": typ, "status": status, "tags": tags_str,
                                   "history_skipped": bool(m.get("history_skipped", False))}, sort_keys=True))
        def row(rule, shipped, n=0):
            r = {"id": eid, "type": typ, "rule_fired": rule, "stored_sha256": m.get("stored_sha256", "") or "",
                 "read_sha256": sha(doc), "shipped_sha256": shipped, "meta_sha256": meta_sha,
                 "history_skipped": bool(m.get("history_skipped", False)), "review_after": ra,
                 "stale": stale, "content_bytes": n}
            if stale: r["owner"] = entry_agent
            return r
        if status == "superseded":
            man.append(row("skipped:superseded", None)); continue
        if status == "retired" and not (agent and entry_agent == agent):
            man.append(row("skipped:retired", None)); continue
        if state_rule and lanes and project not in lanes:
            man.append(row("skipped:lane", None)); continue
        content = doc; truncated = False
        if state_rule and typ == "state" and entry_agent is not None:
            pe_spec = content.find("\n\n")
            if pe_spec == -1 or pe_spec > para_max:
                gate_fail.append(f"{eid}: first paragraph ends at {pe_spec if pe_spec != -1 else 'EOF'}, "
                                 f"spec allows <= {para_max} (writer discipline; server truth is 700)")
        rule = "self" if (entry_agent is not None and entry_agent == agent) else "full"
        if entry_agent is not None and entry_agent != agent and len(content) > TRUNC:
            pe = content.find("\n\n", 0, TRUNC + 200)
            if 100 < pe: content = content[:pe]
            else:
                content = content[:TRUNC].rstrip() + "…"
                if state_rule and typ == "state":
                    gate_fail.append(f"{eid}: first paragraph ends past {TRUNC} chars; "
                                     f"other birds would boot on a mid-sentence stub")
            truncated = True; rule = f"truncated:{len(content)}"
        e = {"id": eid, "content": content, "tags": tags_str, "type": typ}
        if truncated:
            e["truncated"] = True
            e["full_available_via"] = f"memory_search(query='{entry_agent} identity', collection='bootstrap', top_k=1)"
        boot.append(e); man.append(row(rule, sha(content), len(content)))
    return boot, man, gate_fail

def render(agent, project, boot, man, handoffs, flock, known):
    env = {"bootstrap_entries": man, "amq_branch": "flock_member" if agent in flock else "confusion_mode",
           "flock_agents_snapshot": sorted(flock), "known_identity_agents_snapshot": sorted(known),
           "assembly_constants": {"OTHER_IDENTITY_TRUNCATE_CHARS": TRUNC, "HANDOFF_RECENCY_DAYS": HANDOFF_DAYS},
           "section_sizes": {"bootstrap_bytes": sum(r.get("content_bytes", 0) for r in man)},
           "server_commit": "unknown", "assembled_at": datetime.now(timezone.utc).isoformat()}
    result = {"bootstrap": boot, "bootstrap_count": len(boot),
              "amq_unread": [], "amq_count": 0,          # AMQ excluded: add a real boot's amq section
              "recent_handoffs": handoffs, "handoff_count": len(handoffs), "manifest": env}
    s = json.dumps(result)                                # same call as server.py: ensure_ascii=True
    return s, {"bootstrap": len(json.dumps(boot)), "handoffs": len(json.dumps(handoffs)),
               "manifest": len(json.dumps(env)), "render": len(s)}

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--agents", help="comma list; default = active roster")
    ap.add_argument("--project", default="general", help="chorus_init project (handoff filter)")
    ap.add_argument("--state-rule", action="store_true", help="model per-bird state entries shipping self/truncated")
    ap.add_argument("--model", metavar="DIR", help="overlay proposed entries from DIR")
    ap.add_argument("--cap-tokens", type=int, default=25000)
    ap.add_argument("--chars-per-token", type=float, default=4.0)
    ap.add_argument("--margin", type=float, default=0.20, help="fraction of cap kept free (default 0.20)")
    ap.add_argument("--margin-for", action="append", default=[], metavar="BIRD=FRACTION",
                    help="per-bird margin override for this --project run, e.g. alpha=0.14 ("
                         "a named, smaller margin on one bird's lane; handoffs are already in the render, "
                         "so a 'handoff reserve' would double-count)")
    ap.add_argument("--amq-reserve", type=int, default=4300, metavar="CHARS",
                    help="chars added to every render for the AMQ section this tool cannot read "
                         "(default 4300: the 2026-09-15 live calibration, empty inbox); 0 to disable")
    ap.add_argument("--gate", action="store_true", help="exit 1 on any FAIL")
    ap.add_argument("--shared-id", default=None, metavar="MEM_ID",
                    help="this site's shared state entry (the one no agent owns); required with --state-rule, "
                         "whose gate fails if that entry's tags resolve to a bird")
    ap.add_argument("--para-max", type=int, default=500,
                    help="spec cap on a state entry's first paragraph (default 500; the server's own cut is 700)")
    ap.add_argument("--handoff-skip-superseded", action="store_true",
                    help="model the proposed server fix: recent_handoffs skips status=superseded")
    ap.add_argument("--invariants-only", action="store_true",
                    help="with --gate: fail on paragraph/tag invariants only, not on size (mid-sequence checkpoints)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--dump", metavar="DIR", help="write each bird's rendered JSON to DIR/<bird>.json (diff against a real boot)")
    a = ap.parse_args()
    if a.state_rule and not a.shared_id:
        ap.error("--state-rule needs --shared-id: the id of this site's shared state entry")
    if a.dump: os.makedirs(a.dump, exist_ok=True)

    roster_path, order, known = load_roster()
    agents = [x.strip().lower() for x in a.agents.split(",")] if a.agents else order
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    ents = load_collection(con, "bootstrap")
    if a.model: ents = overlay_model(ents, a.model)
    budget = a.cap_tokens * (1 - a.margin)
    margin_for = {}
    for spec in a.margin_for:
        b, _, frac = spec.partition("=")
        margin_for[b.strip().lower()] = float(frac)
    out, fails = [], []
    for ag in agents:
        budget = a.cap_tokens * (1 - margin_for.get(ag, a.margin))
        boot, man, gate_fail = assemble(ag, ents, known, a.state_rule, a.shared_id, a.project, a.para_max)
        s, sizes = render(ag, a.project, boot, man,
                          load_handoffs(con, a.project, ag, a.handoff_skip_superseded), set(order), known)
        if a.dump:
            with open(os.path.join(a.dump, f"{ag}.json"), "w", encoding="utf-8") as f: f.write(s)
        tok = (sizes["render"] + a.amq_reserve) / a.chars_per_token   # gate math includes the AMQ reserve
        ok = (tok <= budget or a.invariants_only) and not gate_fail
        rec = {"agent": ag, **sizes, "amq_reserve": a.amq_reserve, "est_tokens": round(tok),
               "margin": margin_for.get(ag, a.margin), "budget_tokens": round(budget),
               "pct_of_cap": round(100 * tok / a.cap_tokens, 1),
               "pass": ok, "gate": gate_fail,
               "entries": [(r["id"], r["rule_fired"], r["content_bytes"]) for r in man if r["shipped_sha256"]]}
        out.append(rec)
        if not ok: fails.append(ag)
    if a.json:
        print(json.dumps({"db": DB, "roster": roster_path, "state_rule": a.state_rule, "model": a.model,
                          "cap_tokens": a.cap_tokens, "chars_per_token": a.chars_per_token,
                          "margin": a.margin, "birds": out}, indent=1))
    else:
        print(f"store {DB}\nroster {roster_path} (active: {' '.join(order)})")
        print(f"rules: {'PROPOSED state-rule + lane rule' if a.state_rule else 'live server'}; project={a.project!r}"
              f"{'  model overlay: ' + a.model if a.model else ''}"
              f"{'  (gate: invariants only)' if a.invariants_only else ''}")
        print(f"cap {a.cap_tokens} tok, margin {a.margin:.0%}"
              f"{' (' + ', '.join(f'{b} {m:.0%}' for b, m in margin_for.items()) + ')' if margin_for else ''}"
              f" -> budget {a.cap_tokens * (1 - a.margin):.0f} tok "
              f"@ {a.chars_per_token} chars/tok; AMQ reserve {a.amq_reserve:,} chars added to every render "
              f"(the AMQ section itself is not readable here)\n")
        print(f"{'bird':8} {'bootstrap':>10} {'handoffs':>9} {'manifest':>9} {'render':>8} {'+reserve':>9} {'~tok':>7} {'%cap':>6}  result")
        for r in out:
            print(f"{r['agent']:8} {r['bootstrap']:10,} {r['handoffs']:9,} {r['manifest']:9,} "
                  f"{r['render']:8,} {r['render'] + r['amq_reserve']:9,} {r['est_tokens']:7,} {r['pct_of_cap']:6.1f}  {'PASS' if r['pass'] else 'FAIL'}")
            for g in r["gate"]: print(f"         GATE {g}")
        print()
        for r in out:
            big = sorted(r["entries"], key=lambda e: -e[2])[:6]
            print(f"{r['agent']:8} largest shipped: " + ", ".join(f"{i} {rule} {n:,}" for i, rule, n in big))
    if a.gate and fails:
        sys.exit(f"FAIL: {', '.join(fails)}")

if __name__ == "__main__":
    main()
