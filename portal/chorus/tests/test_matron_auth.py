"""/matron requires the pane-key Bearer token (2026-09-24). Drives the real Handler over
loopback on an ephemeral port; only send_async (the tmux keystroke) is stubbed.
`python3 test_matron_auth.py`."""
import os, sys, json, importlib.util, tempfile, threading, urllib.request, urllib.error
HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["COTERIE_AGENTS_JSON"] = os.path.join(HERE, "agents-test.json")
os.environ.setdefault("CHORUSD_STATE_DIR", tempfile.mkdtemp())
tok = tempfile.NamedTemporaryFile("w", delete=False); tok.write("t0ken-for-tests\n"); tok.close()
os.environ["CHORUSD_PANE_KEY_TOKEN_PATH"] = tok.name
spec = importlib.util.spec_from_file_location("chorusd", os.path.join(HERE, "..", "chorusd.py"))
c = importlib.util.module_from_spec(spec); spec.loader.exec_module(c)
c.load_birds()
sent = []
c.send_async = lambda bird, text, delay=0: sent.append((bird, text))
c.db_log = lambda *a, **k: None
srv = c.ThreadingHTTPServer(("127.0.0.1", 0), c.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{srv.server_address[1]}/matron"

# a valid v6 body (repo pointer), so only the auth decides the status
BODY = {"bird": "bee", "repo": "~/repos/agents", "file": "chorus-init/README.md", "commit": "0" * 40,
        "lines": "10-20", "section": "## Bee", "sender": "alpha"}

def post(headers):
    req = urllib.request.Request(url, data=json.dumps(BODY).encode(),
                                 headers={"content-type": "application/json", **headers}, method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code

assert post({}) == 401 and not sent, "no token -> 401, nothing typed"
print("M1 no token: 401, nothing typed")
assert post({"Authorization": "Bearer wrong"}) == 401 and not sent, "wrong token -> 401"
print("M2 wrong token: 401, nothing typed")
assert post({"Authorization": "Bearer t0ken-for-tests", "X-Forwarded-For": "192.0.2.9"}) == 403 and not sent, "proxied -> 403 even with token"
print("M3 proxied with token: 403 (proxy fence first), nothing typed")
assert post({"Authorization": "Bearer t0ken-for-tests"}) == 200 and sent and sent[0][0] == "bee", "right token -> 200, pointer typed"
print("M4 right token: 200, pointer typed to bee")
c.PANE_KEY_TOKEN = b""
sent.clear()
assert post({"Authorization": "Bearer "}) == 401 and not sent, "unreadable token file -> every call rejected"
print("M5 empty token (unreadable file): 401, fails closed")
srv.shutdown()
print("ALL PASS")
