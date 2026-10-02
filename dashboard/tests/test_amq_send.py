"""POST /api/amq/send: the release blocker's three defects, each with its counterfactual.

`python3 dashboard/tests/test_amq_send.py` (from the memory venv, which has Flask)

Before 2026-09-25 the route took `from` from the request body (any name; the doorbell then
rang the recipient under that name), joined `to`/`from` into filesystem paths unchecked, and
had no auth. Since 2026-10-01 the dashboard writes no mailbox at all: a request that passes the
gate and the dashboard's own refusals is handed to the memory server's /dashboard/send (which
fixes the operator again, fans out `all`, and delivers under maildir.py's rule; memory's
dashboard_api_test covers that side). So the receipts here are CALLS: after every refused
request the stubbed server must have been asked nothing, and an accepted one must be asked
exactly what the operator typed.
"""
import json, os, sys, tempfile

TMP = tempfile.mkdtemp(prefix="amq-send-test-")
ROSTER = os.path.join(TMP, "agents.json")
TOKEN_PATH = os.path.join(TMP, "post.token")
TOKEN = "t0k3n-for-the-test-only"
json.dump({
    "operator": "operator",
    "retired": ["retired1"],
    "birds": [
        {"name": "alpha", "hue": 270},
        {"name": "bravo", "hue": 30},
        {"name": "retired1", "hue": 200},                 # retired: never a recipient
        {"name": "charlie", "enabled": False},        # disabled: never a recipient
        {"name": "../evil", "hue": 0},                # a roster file can be wrong; the shape check holds
    ],
}, open(ROSTER, "w"))
open(TOKEN_PATH, "w").write(TOKEN + "\n")

os.environ["MEMORY_AMQ_ROOT"] = os.path.join(TMP, "amq")
os.environ["COTERIE_AGENTS_JSON"] = ROSTER
os.environ["DASHBOARD_POST_TOKEN_PATH"] = TOKEN_PATH
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import dashboard as d  # noqa: E402

CALLS = []
def fake_memory(route, body=None):
    """The memory server, stubbed: records every call; answers send the way the server does."""
    CALLS.append((route, dict(body or {})))
    if route != "send":
        raise AssertionError(f"unexpected route {route}")
    to = body["to"]
    rcpts = ["alpha", "bravo"] if to == "all" else [to]
    return {"ok": True, "from": "operator", "delivered": [{"to": r, "id": f"id-{r}"} for r in rcpts], "count": len(rcpts)}
d._memory = fake_memory

c = d.app.test_client()
fail = 0
def ok(cond, msg):
    global fail
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        fail = 1

def post(payload, token=TOKEN, raw=None):
    headers = {}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    if raw is not None:
        return c.post("/api/amq/send", data=raw, headers=headers, content_type="text/plain")
    return c.post("/api/amq/send", data=json.dumps(payload), headers=headers, content_type="application/json")

good = {"to": "alpha", "subject": "s", "body": "hello"}

# --- auth: the gate is on every POST, and it fails closed ---
r = post(good, token=None)
ok(r.status_code == 401 and r.get_json()["ok"] is False, f"no token: 401 ({r.status_code})")
ok(CALLS == [], "no token: the server was not asked")
r = post(good, token="wrong-" + TOKEN)
ok(r.status_code == 401, f"wrong token: 401 ({r.status_code})")
r = post(good, token="")
ok(r.status_code == 401, f"empty bearer: 401 ({r.status_code})")
r = c.post("/api/amq/send", data=json.dumps(good), headers={"Authorization": "Basic abc"}, content_type="application/json")
ok(r.status_code == 401, f"non-bearer scheme: 401 ({r.status_code})")
r = c.post("/api/no-such-route", data=json.dumps({"x": 1}), content_type="application/json")
ok(r.status_code == 401, f"POST to an unknown route without token: 401 ({r.status_code})")
r = c.post("/api/no-such-route", data=json.dumps({"x": 1}), headers={"Authorization": "Bearer " + TOKEN}, content_type="application/json")
ok(r.status_code == 404, f"POST to an unknown route with token: 404 ({r.status_code})")
r = c.post("/api/amq", data="{}", headers={"Authorization": "Bearer " + TOKEN}, content_type="application/json")
ok(r.status_code == 405, f"POST to a GET-only route with token: 405 ({r.status_code})")
r = c.get("/api/amq")
ok(r.status_code == 401, f"GET /api/amq without token: 401 ({r.status_code}) -- reads are gated since 2026-09-25")
r = c.get("/api/amq", headers={"Authorization": "Bearer " + TOKEN})
ok(r.status_code == 200, f"GET /api/amq with the token: 200 ({r.status_code})")
ok(CALLS == [], "after every refused auth: the server was not asked")

saved = d.POST_TOKEN
d.POST_TOKEN = b""
r = post(good)
ok(r.status_code == 401, f"empty token file (nothing installed): 401 even with a header ({r.status_code})")
d.POST_TOKEN = saved
ok(d._load_post_token(os.path.join(TMP, "absent")) == b"", "absent token file loads as empty, not an exception")
ok(d.check_post_auth("Bearer " + TOKEN) is True, "the real token is accepted")
ok(d.check_post_auth("Bearer  " + TOKEN + " ") is True, "surrounding whitespace is stripped")

# --- sender: the operator mailbox, decided server-side (here, and again on the memory server) ---
r = post(dict(good, **{"from": "bravo"}))
ok(r.status_code == 400 and "sender" in r.get_json()["error"], f"forged from=bravo: 400 ({r.status_code}: {r.get_json()['error']})")
ok(CALLS == [], "forged sender: the server was not asked")
r = post(dict(good, **{"from": "operator"}))
ok(r.status_code == 200 and r.get_json()["from"] == "operator", "from=<operator> (what the old UI sent): accepted")
r = post(good)
ok(r.status_code == 200 and r.get_json()["ok"] is True, "no from: accepted, sender is the operator")
ok(len(CALLS) == 2 and all(route == "send" and body == {"to": "alpha", "subject": "s", "body": "hello"} for route, body in CALLS),
   f"two accepted sends -> two /dashboard/send calls carrying exactly to/subject/body: {CALLS}")
ok(r.get_json()["id"] == "id-alpha" and r.get_json()["delivered"] == [{"to": "alpha", "id": "id-alpha"}] and r.get_json()["count"] == 1,
   "the response keeps the single-recipient shape (id, delivered, count) from the server's answer")

# --- recipients: the roster, and nothing that is not on it (refused here, before any call) ---
before = list(CALLS)
for bad in ("../../etc", "alpha/../news", "news", "retired1", "charlie", "../evil", "operator", "", "ALPHA", "a b", "/alpha"):
    r = post(dict(good, to=bad))
    ok(r.status_code == 400, f"to={bad!r}: 400 ({r.status_code}: {r.get_json()['error']})")
r = post(dict(good, to=["alpha"]))
ok(r.status_code == 400, f"to as a list: 400 ({r.status_code})")
r = post(dict(good, body=["x"]))
ok(r.status_code == 400, f"body as a list: 400 ({r.status_code})")
r = post(dict(good, body=""))
ok(r.status_code == 400, f"empty body: 400 ({r.status_code})")
r = post(None, raw="not json")
ok(r.status_code == 400, f"non-JSON body: 400, not a 500 ({r.status_code})")
r = post(["alpha"])
ok(r.status_code == 400, f"JSON array body: 400 ({r.status_code})")
ok(CALLS == before, "after every refused recipient: the server was not asked")

r = post(dict(good, to="bravo"))
ok(r.status_code == 200 and r.get_json()["delivered"] == [{"to": "bravo", "id": "id-bravo"}], "to=bravo (roster): delivered once")

# --- broadcast: the server fans out; the dashboard asks once, with to=all ---
before = list(CALLS)
r = post(dict(good, to="all"))
j = r.get_json()
ok(r.status_code == 200 and sorted(x["to"] for x in j["delivered"]) == ["alpha", "bravo"] and j["id"] is None and j["count"] == 2,
   f"to=all: one call, the server's fan-out reported, no single id: {[x['to'] for x in j['delivered']]}")
ok(CALLS[len(before):] == [("send", {"to": "all", "subject": "s", "body": "hello"})], "broadcast is one /dashboard/send with to=all")

# --- an operator name that is itself unsafe is refused here, before any call ---
saved_flock = d.load_flock
d.load_flock = lambda: {"operator": "../x", "birds": [{"name": "alpha"}]}
before = list(CALLS)
r = post(good)
ok(r.status_code == 500 and "operator" in r.get_json()["error"] and CALLS == before, f"unsafe operator name: refused, server not asked ({r.status_code})")
d.load_flock = saved_flock

# --- the memory server down or refusing: a loud 502, never a silent success ---
def down(route, body=None):
    raise d.MemoryUnavailable("memory server unreachable: ConnectionError")
d._memory = down
r = post(good)
ok(r.status_code == 502 and r.get_json()["ok"] is False and "unreachable" in r.get_json()["error"], f"server down: 502 with the reason ({r.status_code})")
d._memory = fake_memory

# --- the shape check itself ---
for name, want in (("alpha", True), ("a-b_c1", True), ("../x", False), ("a/b", False), ("", False), ("A", False), (None, False), ("x" * 65, False), ("a.b", False)):
    ok(d.mailbox_name_ok(name) is want, f"mailbox_name_ok({name!r}) is {want}")

print("RESULT: " + ("FAIL" if fail else "all pass"))
sys.exit(fail)
