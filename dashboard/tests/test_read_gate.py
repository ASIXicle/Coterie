"""Read gate (2026-09-25): every /api/* request needs the token, reads included; the page shell,
/tokens.css and /fonts/ stay open; POSTs stay gated; an empty token file refuses everything.
Run: /opt/memory/venv/bin/python3 dashboard/tests/test_read_gate.py ; exit 0 all pass, 1 otherwise."""
import json, os, sys, tempfile
TMP = tempfile.mkdtemp()
TOKEN_PATH = os.path.join(TMP, "post.token"); TOKEN = "r3ad-gate-test-token"
open(TOKEN_PATH, "w").write(TOKEN + "\n")
ROSTER = os.path.join(TMP, "agents.json")
json.dump({"operator": "op", "agents": [{"name": "alpha", "port": 7681}]}, open(ROSTER, "w"))
os.makedirs(os.path.join(TMP, "amq", "alpha", "inbox", "new"))
os.environ.update({"MEMORY_AMQ_ROOT": os.path.join(TMP, "amq"), "COTERIE_AGENTS_JSON": ROSTER,
                   "DASHBOARD_POST_TOKEN_PATH": TOKEN_PATH, "MEMORY_DATA_DIR": os.path.join(TMP, "chromadb"),
                   "DASHBOARD_DOORBELL_LEDGER": os.path.join(TMP, "doorbell.log")})
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import dashboard as d  # noqa: E402
c = d.app.test_client()
fail = 0
def ok(cond, msg):
    global fail
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond: fail = 1
H = {"Authorization": "Bearer " + TOKEN}
print("G1 reads under /api/ need the token")
for path in ("/api/system", "/api/export/memories.json", "/api/memories", "/api/stats", "/api/amq/agents"):
    r = c.get(path)
    ok(r.status_code == 401 and b"token" in r.data, f"GET {path} without a token -> 401 ({r.status_code})")
    ok(c.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401, f"GET {path} wrong token -> 401")
r = c.get("/api/system", headers=H)
ok(r.status_code == 200, f"GET /api/system with the token -> 200 ({r.status_code})")
print("G2 the page shell stays open (it carries no data)")
for path in ("/", "/tokens.css", "/fonts/nothing.woff2", "/static/app.js"):
    r = c.get(path)
    ok(r.status_code != 401, f"GET {path} without a token is not 401 ({r.status_code})")
print("G3 writes stay gated")
ok(c.post("/api/amq/send", json={"to": "alpha", "body": "x"}).status_code == 401, "POST without a token -> 401")
d._memory = lambda route, body=None: {"ok": True, "from": "op", "delivered": [{"to": "alpha", "id": "x"}], "count": 1}  # the memory server, stubbed (2026-10-01)
ok(c.post("/api/amq/send", json={"to": "alpha", "body": "x"}, headers=H).status_code == 200, "POST with the token -> 200 (delivery is the server's; stubbed)")
print("G4 an empty token file refuses every read (fail closed)")
saved = d.POST_TOKEN; d.POST_TOKEN = b""
ok(c.get("/api/system", headers=H).status_code == 401, "right token vs empty stored token -> 401")
d.POST_TOKEN = saved
print("RESULT:", "FAIL" if fail else "PASS"); sys.exit(fail)
