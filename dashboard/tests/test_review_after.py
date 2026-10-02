"""The review-after panel consumes the memory server's parse; the dashboard keeps no parser.

`python3 dashboard/tests/test_review_after.py` (from the memory venv, which has Flask)

Until 2026-10-01 the dashboard carried its own copy of memory's `_parse_review_after`, and this
file proved the two functions agreed (regex comparison until 2026-09-24, output comparison after
a 2026-09-24 change moved the rule out of the regex). Since the /dashboard/* routes, every
bootstrap row arrives with `review_after` parsed by the server, so there is one rule and nothing
to compare. What this guards now: (a) no parser grows back in dashboard.py; (b) /api/reviews
reads the server's field, classifies it with review_status, skips rows without a date, never
sends an entry's content to the browser, and fails loudly when the server is down.
"""
import ast, json, os, sys, tempfile
from datetime import date, timedelta

TMP = tempfile.mkdtemp(prefix="review-after-test-")
ROSTER = os.path.join(TMP, "agents.json"); TOKEN = os.path.join(TMP, "post.token")
json.dump({"operator": "operator", "birds": [{"name": "alpha"}]}, open(ROSTER, "w"))
open(TOKEN, "w").write("t\n"); H = {"Authorization": "Bearer t"}
os.environ.update({"COTERIE_AGENTS_JSON": ROSTER, "DASHBOARD_POST_TOKEN_PATH": TOKEN, "MEMORY_AMQ_ROOT": os.path.join(TMP, "amq")})
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import dashboard as d  # noqa: E402

fail = 0
def ok(c, msg):
    global fail
    print(("  PASS " if c else "  FAIL ") + msg)
    if not c: fail = 1

# (a) no parser in the dashboard
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "dashboard.py"), encoding="utf-8").read()
names = {n.name for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef)}
ok("parse_review_after" not in names and "REVIEW_AFTER_RE" not in src, "dashboard.py carries no review-after parser (the server's is the rule)")

# (b) the panel over the server's rows
today = date.today()
def row(i, review_after, content):
    return {"id": i, "metadata": {"type": "state", "stored_at": "2026-09-01T00:00:00+00:00"}, "content": content, "review_after": review_after}
ROWS = [
    row("mem-overdue", (today - timedelta(days=3)).isoformat(), "I am overdue\nsecret second line"),
    row("mem-soon", (today + timedelta(days=5)).isoformat(), "due soon"),
    row("mem-ok", (today + timedelta(days=60)).isoformat(), "fine"),
    row("mem-none", None, "no date at all"),
    row("mem-bad", "not-a-date", "garbage date"),
]
CALLS = []
def fake_memory(route, body=None):
    CALLS.append((route, dict(body or {})))
    if route == "items" and body["collection"] == "bootstrap":
        return {"collection": "bootstrap", "total": len(ROWS), "items": ROWS, "next": None}
    raise AssertionError(f"unexpected call {route} {body}")
d._memory = fake_memory
c = d.app.test_client()
r = c.get("/api/reviews", headers=H); j = r.get_json()
ok(r.status_code == 200 and [x["id"] for x in j["reviews"]] == ["mem-overdue", "mem-soon", "mem-ok"],
   f"worst first, rows without a parseable date skipped: {[x['id'] for x in j['reviews']]}")
ok([x["status"] for x in j["reviews"]] == ["overdue", "due-soon", "ok"] and j["overdue"] == 1 and j["due_soon"] == 1, "statuses and counts")
ok(j["reviews"][0]["review_after"] == ROWS[0]["review_after"] and j["reviews"][0]["title"] == "I am overdue", "the server's date and the first line as title")
ok("secret second line" not in json.dumps(j), "an entry's content never reaches the browser from this panel")
ok(CALLS and CALLS[0][0] == "items" and CALLS[0][1]["collection"] == "bootstrap" and CALLS[0][1]["fields"] == "full",
   "one items call on bootstrap (full, read in-process for the title)")

def down(route, body=None):
    raise d.MemoryUnavailable("memory server answered 503 on items: dashboard API disabled")
d._memory = down
r = c.get("/api/reviews", headers=H)
ok(r.status_code == 503 and r.get_json()["overdue"] is None and "disabled" in r.get_json()["error"],
   f"server down: 503 with the reason, never 'nothing overdue' ({r.status_code})")

import shutil; shutil.rmtree(TMP)
print("RESULT: " + ("FAIL" if fail else "all pass")); sys.exit(1 if fail else 0)
