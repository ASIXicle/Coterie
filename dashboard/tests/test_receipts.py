"""The receipts endpoints. Since 2026-10-01 the memory server reads the bootstrap history
(/dashboard/edits and /dashboard/boots) and the dashboard keeps only the derived view, so the
edits and boots here come from a stubbed server fed the same fixtures the old test wrote to
disk; the rings ledger is still read by the dashboard itself. Scratch fixtures, so another bird
can run this with no access to the live history or ledger.

`python3 dashboard/tests/test_receipts.py` (from the memory venv, which has Flask)

- /api/edits: one row per pre-image sidecar, newest first, `pinned` iff the writer
  supplied expected_after_sha256; a malformed sidecar is skipped, not a crash.
- /api/boots: `drifted` compares the last two boots in the same lane; `explained` is
  whether an edit was recorded between them. Same hash = steady; changed after an edit =
  explained; changed with no edit = the signal. Audit lines (kind "manifest") are not
  boots; legacy two-column lines are.
- /api/rings: rings/answers/latency per roster agent from the ledger tail; local-time
  stamps; a missing ledger is a 503 with the path, never a 200 with zeros.
"""
import json, os, sys, tempfile
from datetime import datetime, timedelta

TMP = tempfile.mkdtemp(prefix="receipts-test-")
HIST = os.path.join(TMP, "bootstrap-history"); LEDGER = os.path.join(TMP, "doorbell.log")
ROSTER = os.path.join(TMP, "agents.json"); TOKEN = os.path.join(TMP, "post.token")
json.dump({"operator": "operator", "birds": [{"name": "alpha"}, {"name": "bravo"}, {"name": "charlie"}]}, open(ROSTER, "w"))
open(TOKEN, "w").write("t\n"); H = {"Authorization": "Bearer t"}  # reads are gated since 2026-09-25
os.makedirs(os.path.join(HIST, "manifest-hashes")); os.makedirs(os.path.join(HIST, "state-alpha")); os.makedirs(os.path.join(HIST, "mem-x"))

EDITS = []    # what the server's /dashboard/edits answers (newest first), in its shape
BOOTS = {}    # what /dashboard/boots answers: {agent: {count, recent: [{at, sha, lane}]}}
def side(entry, idx, at, author, pinned, reason="why"):
    EDITS.append({"entry": entry, "version": idx, "at": at, "author": author, "reason": reason[:240],
                  "before": "a" * 12, "after": "b" * 12, "after_verified": bool(pinned), "landed_at": at, "type": "state"})
    EDITS.sort(key=lambda e: e["at"], reverse=True)
def boots(agent, *recs):
    """recs: (at, sha, lane_or_None, kind) as the log would hold them; the server keeps init and legacy lines only."""
    kept = [{"at": a, "sha": h, "lane": ln} for a, h, ln, kind in recs if kind in ("init", None)]
    BOOTS[agent] = {"count": len(kept), "recent": kept[-20:]}

# An edit to ANY bootstrap entry moves every agent's manifest, so "between" is measured
# against all edits, not the agent's own.
# alpha: boots at 10:00 and 12:00 with different hashes, an edit at 11:00 -> explained
# bravo: boots at 13:00 and 14:00 with different hashes, NO edit in that hour -> the signal
# charlie: two boots, same hash -> steady
side("state-alpha", 1, "2026-09-25T11:00:00+00:00", "alpha", True)
side("mem-x", 3, "2026-09-25T08:00:00+00:00", "bravo", False, "older, unpinned")
# (a malformed sidecar is the server's to skip now: memory's dashboard_api_test covers it)
boots("alpha", ("2026-09-25T10:00:00+00:00", "1" * 64, None, None), ("2026-09-25T12:00:00+00:00", "2" * 64, None, None))
boots("bravo", ("2026-09-25T13:00:00+00:00", "1" * 64, None, None), ("2026-09-25T14:00:00+00:00", "2" * 64, None, None))
boots("charlie", ("2026-09-25T10:00:00+00:00", "1" * 64, None, None), ("2026-09-25T12:00:00+00:00", "1" * 64, None, None))

now = datetime.now()
def t(h): return (now - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%S")
lines = [
    {"t": t(30), "event": "ring", "bird": "alpha", "senders": ["bravo"], "n": 1},   # > 24h, < 7d
    {"t": t(30), "event": "answered", "bird": "alpha", "latency": 20},
    {"t": t(2), "event": "ring", "bird": "alpha", "senders": ["charlie"], "n": 2},
    {"t": t(2), "event": "answered", "bird": "alpha", "latency": 10},
    {"t": t(1), "event": "ring", "bird": "bravo", "senders": ["alpha"], "n": 1},
    {"t": t(1), "event": "re-ring", "bird": "bravo"},
    {"t": t(200), "event": "ring", "bird": "charlie", "senders": ["alpha"], "n": 1},   # > 7d: counted nowhere
    {"t": t(1), "event": "ring", "bird": "zulu", "senders": ["alpha"], "n": 1},        # off roster: ignored
    {"t": t(1), "event": "start", "on": True},                                         # no bird: ignored
]
with open(LEDGER, "w") as f:
    f.write("garbage line\n")
    for l in lines: f.write(json.dumps(l) + "\n")

os.environ.update({"MEMORY_DATA_DIR": os.path.join(os.path.dirname(HIST), "chromadb"), "DASHBOARD_DOORBELL_LEDGER": LEDGER,
                   "COTERIE_AGENTS_JSON": ROSTER, "DASHBOARD_POST_TOKEN_PATH": TOKEN, "MEMORY_AMQ_ROOT": os.path.join(TMP, "amq")})
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import dashboard as d  # noqa: E402
def fake_memory(route, body=None):
    if route == "edits": return {"total": len(EDITS), "edits": EDITS[:(body or {}).get("limit", 40)]}
    if route == "boots": return {"boots": {a: BOOTS.get(a, {"count": 0, "recent": []}) for a in ("alpha", "bravo", "charlie")}}
    raise AssertionError(f"unexpected route {route}")
d._memory = fake_memory
c = d.app.test_client(); fail = 0
def ok(cond, msg):
    global fail
    print(("  PASS " if cond else "  FAIL ") + msg); fail |= (not cond)

e = c.get("/api/edits", headers=H).get_json()
ok(e["total"] == 2 and "history" not in e, f"two edits from the server, no path in the response (total {e['total']})")
ok([x["entry"] for x in e["edits"]] == ["state-alpha", "mem-x"], f"newest first: {[x['entry'] for x in e['edits']]}")
ok(e["edits"][0]["after_verified"] is True and e["edits"][1]["after_verified"] is False, "pinned iff expected_after_sha256 was supplied")
ok(e["edits"][0]["landed_at"] == "2026-09-25T11:00:00+00:00", "the .landed_at sidecar is carried")
ok(e["edits"][0]["before"] == "a" * 12 and e["edits"][0]["after"] == "b" * 12, "before/after shas shortened to 12")

b = {x["agent"]: x for x in c.get("/api/boots", headers=H).get_json()}
ok(b["alpha"]["drifted"] is True and b["alpha"]["edits_between"] == 1 and b["alpha"]["explained"] is True, f"alpha: drifted, 1 edit between, explained ({b['alpha']['edits_between']})")
ok(b["bravo"]["drifted"] is True and b["bravo"]["edits_between"] == 0 and b["bravo"]["explained"] is False, "bravo: drifted with no edit recorded = the signal")
ok(b["charlie"]["drifted"] is False and b["charlie"]["explained"] is None, "charlie: same hash = steady, explained not applicable")

# Since 2026-09-29 lines carry kind and lane: only "init" is a boot, and a boot
# compares with the previous boot in the same lane. The lines above are the legacy
# two-column shape, which still reads as boots.
# alpha: legacy boot 10:00, init 12:00 (edit at 11:00), then an audit with a new hash:
#        the audit is not a boot, so the panel still shows the 12:00 boot, explained
# bravo: general 13:00, ops 13:30, general 14:00, same hash in both general
#        boots: a lane switch is not drift
# charlie: audits only (a bird that is down, audited by another bird): no boots
boots("alpha", ("2026-09-25T10:00:00+00:00", "1" * 64, None, None), ("2026-09-25T12:00:00+00:00", "2" * 64, "general", "init"),
      ("2026-09-25T12:30:00+00:00", "3" * 64, "general", "manifest"))
boots("bravo", ("2026-09-25T13:00:00+00:00", "1" * 64, "general", "init"), ("2026-09-25T13:30:00+00:00", "2" * 64, "ops", "init"),
      ("2026-09-25T14:00:00+00:00", "1" * 64, "general", "init"))
boots("charlie", ("2026-09-25T10:00:00+00:00", "1" * 64, "general", "manifest"))
d._CACHE.clear()   # the dashboard asks the server at most once per 5 s; the fixtures changed
b = {x["agent"]: x for x in c.get("/api/boots", headers=H).get_json()}
ok(b["alpha"]["boots"] == 2 and b["alpha"]["last_hash"] == "2" * 64 and b["alpha"]["drifted"] is True and b["alpha"]["explained"] is True,
   f"alpha: an audit after the boot is not a boot; legacy + init compare ({b['alpha']['boots']} boots, last {b['alpha']['last_hash'][:4]})")
ok(b["bravo"]["boots"] == 3 and b["bravo"]["drifted"] is False and b["bravo"]["last_lane"] == "general",
   f"bravo: compared with the last general boot, not the ops one (drifted {b['bravo']['drifted']})")
ok([x["lane"] for x in b["bravo"]["history"]] == ["general", "ops", "general"], "history carries the lane")
ok(b["charlie"]["boots"] == 0 and b["charlie"]["last_boot"] is None,
   "charlie: audits of a down bird never read as a boot")

# The count is the server's total, not the window: an agent with 45 boots and a 20-record window
# (2026-10-01 live regression: every bird with more than 20 boots read as 20).
BOOTS["alpha"]["count"] = 45
d._CACHE.clear()
b = {x["agent"]: x for x in c.get("/api/boots", headers=H).get_json()}
ok(b["alpha"]["boots"] == 45 and len(b["alpha"]["history"]) <= 5 and b["alpha"]["drifted"] is True,
   f"boots is the server's count (45), the window still drives drift and history ({b['alpha']['boots']})")

r = c.get("/api/rings", headers=H); j = r.get_json()
ok(r.status_code == 200 and j["records"] == 9, f"ledger tail parsed, garbage line skipped ({j['records']} records)")
a = {x["agent"]: x for x in j["agents"]}
ok(a["alpha"]["rings_24h"] == 1 and a["alpha"]["rings_7d"] == 2 and a["alpha"]["answered_7d"] == 2, f"alpha: 1 ring/24h, 2/7d, 2 answered ({a['alpha']['rings_24h']},{a['alpha']['rings_7d']},{a['alpha']['answered_7d']})")
ok(a["alpha"]["median_latency_7d"] == 20.0 and a["alpha"]["max_latency_7d"] == 20.0 and a["alpha"]["last_latency"] == 10, "alpha latencies: median/max over the week, last answer 10s")
ok(a["bravo"]["rings_24h"] == 1 and a["bravo"]["rerings_7d"] == 1 and a["bravo"]["answered_7d"] == 0, "bravo: one ring, one re-ring, unanswered")
ok(a["charlie"]["rings_7d"] == 0 and a["charlie"]["last_ring"] is not None, "charlie: an old ring counts nowhere but is still the last ring")
ok("zulu" not in a and len(j["agents"]) == 3, "off-roster mailbox ignored; one row per roster agent")
ok(j["totals"]["rings_24h"] == 2 and j["totals"]["rings_7d"] == 3 and j["totals"]["rerings_7d"] == 1, f"totals {j['totals']}")
ok(j["recent"][0]["t"] >= j["recent"][-1]["t"] and all(x["event"] in ("ring", "answered", "re-ring") for x in j["recent"]), "recent: newest first, rings/answers/re-rings only")


def down(route, body=None):
    raise d.MemoryUnavailable("memory server unreachable: ConnectionError")
d._memory = down; d._CACHE.clear()
r = c.get("/api/edits", headers=H)
ok(r.status_code == 502 and r.get_json()["ok"] is False, f"server down: /api/edits is a 502, never an empty list ({r.status_code})")
r = c.get("/api/boots", headers=H)
ok(r.status_code == 502, f"server down: /api/boots is a 502 ({r.status_code})")
d._memory = fake_memory; d._CACHE.clear()

os.remove(LEDGER)
r = c.get("/api/rings", headers=H)
ok(r.status_code == 503 and r.get_json()["ledger"] == LEDGER and r.get_json()["agents"] == [], "missing ledger: 503 naming the path, never zeros")

import shutil; shutil.rmtree(TMP)
print("RESULT: " + ("FAIL" if fail else "all pass")); sys.exit(1 if fail else 0)
