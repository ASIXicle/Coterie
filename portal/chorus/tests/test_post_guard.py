"""POST guard: every POST takes JSON; a request carrying an Origin must come from the address it was
sent to; and the page's routes (/fire, /doorbell, /abort) answer only requests that came through the
front door, which Caddy marks with a secret only it and the daemon hold. A page the operator opens
elsewhere, or any local account on loopback, must not fire a round, flip the doorbell or abort.
Drives the real Handler over loopback, on the routes that are harmless here (doorbell, abort), and
on /fire with the typing stubbed. `python3 test_post_guard.py`."""
import os, json, tempfile, importlib.util, threading, urllib.request, urllib.error
HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["COTERIE_AGENTS_JSON"] = os.path.join(HERE, "birds-test.json")
os.environ.setdefault("CHORUSD_STATE_DIR", tempfile.mkdtemp())
os.environ["CHORUSD_MATRON_DIR"] = tempfile.mkdtemp()
SECRET = "front-door-secret-for-the-test"
front = os.path.join(tempfile.mkdtemp(), "front.token")
with open(front, "w") as f:
    f.write(SECRET + "\n")
os.environ["CHORUSD_FRONT_TOKEN_PATH"] = front
spec = importlib.util.spec_from_file_location("chorusd", os.path.join(HERE, "..", "chorusd.py"))
c = importlib.util.module_from_spec(spec); spec.loader.exec_module(c)
c.load_birds()
typed = []
c.send_async = lambda bird, text, delay=0: typed.append((bird, text))   # never type into a real pane
srv = c.ThreadingHTTPServer(("127.0.0.1", 0), c.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_address[1]}"
SITE = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "192.0.2.10", "X-Forwarded-For": "192.0.2.20"}
FRONT = {"X-Coterie-Front": SECRET}


def post(path, body, headers):
    req = urllib.request.Request(base + path, data=body.encode(), method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def doorbell_on():
    with urllib.request.urlopen(base + "/doorbell") as r:
        return json.loads(r.read())["on"]


def setbell(on, headers, path="/doorbell"):
    return post(path, json.dumps({"on": on}), headers)


JSON = {"Content-Type": "application/json"}
PAGE = {**JSON, **SITE, **FRONT, "Origin": "https://192.0.2.10"}   # the portal page, through Caddy
now = doorbell_on()
assert setbell(not now, JSON) == 401 and doorbell_on() == now
assert setbell(not now, {**JSON, "X-Coterie-Front": "wrong"}) == 401 and doorbell_on() == now
assert setbell(not now, {**JSON, **SITE}) == 401 and doorbell_on() == now
print("1 a local account on loopback: JSON, no secret / a wrong one / forwarded headers forged: 401, state unchanged")
assert post("/fire", json.dumps({"text": "injected", "birds": ["bird1"]}), JSON) == 401 and typed == []
assert post("/chorus/xfire", json.dumps({"text": "injected"}), JSON) == 401 and typed == []
assert setbell(not now, JSON, path="/chorus/doorbell/") == 401 and doorbell_on() == now
print("2 /fire without the secret, and the suffix spellings (/chorus/xfire, a trailing slash): 401, nothing typed")
assert setbell(not now, {**FRONT, "Content-Type": "text/plain"}) == 415 and doorbell_on() == now
assert setbell(not now, {**FRONT, "Content-Type": "application/x-www-form-urlencoded"}) == 415 and doorbell_on() == now
assert setbell(not now, dict(FRONT)) == 415 and doorbell_on() == now
print("3 with the secret but text/plain, a form, no type: 415, state unchanged")
assert setbell(not now, {**PAGE, "Origin": "https://evil.example"}) == 403 and doorbell_on() == now
assert setbell(not now, {**PAGE, "Origin": "null"}) == 403 and doorbell_on() == now
assert setbell(not now, {**PAGE, "Origin": "http://192.0.2.10"}) == 403 and doorbell_on() == now
assert setbell(not now, {**JSON, **FRONT, "Origin": "http://127.0.0.1"}) == 403 and doorbell_on() == now
print("4 with the secret but a foreign Origin, Origin null, the wrong scheme, an Origin not through the proxy: 403")
assert setbell(not now, {**PAGE, "Content-Type": "application/json; charset=utf-8"}) == 200 and doorbell_on() == (not now)
print("5 the portal page itself (JSON, same origin through the proxy, Caddy's secret): 200")
assert post("/abort", "{}", {**SITE, **FRONT, "Content-Type": "text/plain", "Origin": "https://192.0.2.10"}) == 415
assert post("/abort", "{}", PAGE) == 200
print("6 /abort: refused as text/plain, accepted as the page sends it through Caddy")
c.FRONT_TOKEN = b""
assert setbell(now, PAGE) == 401
print("7 no secret on file (missing or unreadable): the page's routes refuse everything")
srv.shutdown(); print("ALL PASS")
