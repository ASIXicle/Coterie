"""pane_key_check + check_pane_key_auth under stubbed state: kill switch, type, roster, whitelist,
busy, cooldown, exact-match, auth. `python3 test_pane_key.py`. Companion to spec.md T1-T7."""
import os, sys, importlib.util, tempfile
HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["COTERIE_AGENTS_JSON"] = os.path.join(HERE, "birds-test.json")
os.environ.setdefault("CHORUSD_STATE_DIR", tempfile.mkdtemp())
os.environ["CHORUSD_PANE_KEY_ENABLED"] = "1"
os.environ["CHORUSD_PANE_KEY_TOKEN_PATH"] = "/nonexistent-token-path-for-tests"

spec = importlib.util.spec_from_file_location("chorusd", os.path.join(HERE, "..", "chorusd.py"))
c = importlib.util.module_from_spec(spec); spec.loader.exec_module(c)
c.load_birds()   # ALL_BIRDS = ["orch", "bee"] from birds-test.json

now = [1000.0]
def check(bird, cmd, dt=0):
    return c.pane_key_check(bird, cmd, now[0] + dt)


# T1 happy — /clear on a known bird accepts, no ledger kwargs
assert check("bee", "/clear") == (200, None, None), "T1 happy path"
print("T1 happy: /clear on bee -> 200")

# T2 whitelist — anything else rejects with ledger reason=whitelist
status, resp, ledger = check("bee", "/model claude-sonnet-4")
assert status == 403 and resp["error"] == "cmd not in whitelist" and resp["allowed"] == ["/clear"], (status, resp)
assert ledger == {"reason": "whitelist"}, ledger
print("T2 whitelist: /model -> 403, ledger reason=whitelist")

# T3 roster — unknown bird rejects with NO ledger
status, resp, ledger = check("hawk", "/clear")
assert status == 400 and resp["error"] == "unknown bird" and ledger is None, (status, resp, ledger)
print("T3 roster: unknown bird -> 400, no ledger")

# T4 busy — mark bee busy, /clear rejects with ledger reason=busy
c.mark_busy("bee", "prompt")
status, resp, ledger = check("bee", "/clear")
assert status == 409 and resp["error"] == "bird busy" and ledger == {"reason": "busy"}, (status, resp, ledger)
c.mark_idle("bee")
print("T4 busy: /clear on busy bird -> 409, ledger reason=busy")

# T5 cooldown — successful call sets state; second call within window rejects; window expiry accepts
c._pane_key_last["bee"] = now[0]
status, resp, ledger = check("bee", "/clear", dt=30)
assert status == 429 and resp["error"] == "cooldown" and resp["retry_after"] > 0, (status, resp)
assert ledger["reason"] == "cooldown" and ledger["retry_after"] > 0, ledger
status, resp, ledger = check("bee", "/clear", dt=c.PANE_KEY_COOLDOWN + 1)
assert status == 200, (status, resp)
print("T5 cooldown: 30s in -> 429 retry_after>0; 61s out -> 200")

# T6 whitelist is exactly {"/clear"} (invariant: adding entries requires code+review)
assert c.PANE_KEY_WHITELIST == frozenset(["/clear"]), c.PANE_KEY_WHITELIST
print("T6 whitelist invariant: frozenset({'/clear'}) exactly")

# T7 exact-match — case variants and trailing whitespace reject at whitelist
c._pane_key_last.clear()
for variant in ("/CLEAR", "/clear ", " /clear", "/clear\n", "clear", ""):
    status, resp, ledger = check("bee", variant, dt=c.PANE_KEY_COOLDOWN + 10)
    assert status == 403, (variant, status, resp)
    assert ledger == {"reason": "whitelist"}, (variant, ledger)
print("T7 exact-match: /CLEAR, '/clear ', ' /clear', '/clear\\n', 'clear', '' all -> 403")

# T8 kill switch — pane_key_check returns 503 without ledger when PANE_KEY_ENABLED is False (review 2026-09-16:
# moved gate into helper so tests exercise it; 503 distinguishes "here, disabled" from a missing-route 404)
c._pane_key_last.clear()
c.PANE_KEY_ENABLED = False
status, resp, ledger = check("bee", "/clear", dt=c.PANE_KEY_COOLDOWN + 100)
assert status == 503 and "disabled" in resp["error"] and ledger is None, (status, resp, ledger)
c.PANE_KEY_ENABLED = True
status, resp, ledger = check("bee", "/clear", dt=c.PANE_KEY_COOLDOWN + 101)
assert status == 200, (status, resp, ledger)
print("T8 kill switch: PANE_KEY_ENABLED=False -> 503 no ledger; True -> 200")

# T9 type check — non-string bird or cmd rejects at 400 no ledger (review 2026-09-16 item 3: was TypeError/
# AttributeError crash raised inside `with lock` before this check landed)
for bad_bird in (None, 42, ["bee"], {"x": 1}):
    status, resp, ledger = check(bad_bird, "/clear", dt=c.PANE_KEY_COOLDOWN + 200)
    assert status == 400 and "must be strings" in resp["error"] and ledger is None, (bad_bird, status, resp, ledger)
for bad_cmd in (None, ["/clear"], {"cmd": "/clear"}, 7):
    status, resp, ledger = check("bee", bad_cmd, dt=c.PANE_KEY_COOLDOWN + 201)
    assert status == 400 and "must be strings" in resp["error"] and ledger is None, (bad_cmd, status, resp, ledger)
print("T9 type: non-string bird or cmd -> 400 'must be strings', no ledger")

# T10 auth helper — empty token, wrong scheme, wrong value, right value
assert c.PANE_KEY_TOKEN == b"", "test env should have unreadable token path"
assert c.check_pane_key_auth("Bearer whatever") is False, "empty stored token rejects everything"
c.PANE_KEY_TOKEN = b"correcthorse"
assert c.check_pane_key_auth("") is False, "empty header rejects"
assert c.check_pane_key_auth("Basic Zm9v") is False, "non-Bearer scheme rejects"
assert c.check_pane_key_auth("Bearer ") is False, "empty token in Bearer rejects"
assert c.check_pane_key_auth("Bearer wrongtoken") is False, "wrong token rejects"
assert c.check_pane_key_auth("Bearer correcthorse") is True, "matching token accepts"
assert c.check_pane_key_auth("Bearer  correcthorse ") is True, "whitespace around token still matches (strip)"
assert c.check_pane_key_auth(None) is False, "non-string header rejects"
c.PANE_KEY_TOKEN = b""
print("T10 auth: empty-stored / wrong-scheme / empty-token / wrong-value reject; matching accepts")

# T11 proxy fence — is_proxied_request detects Caddy's X-Forwarded-{For,Proto,Host} on stub
# dicts AND on a real HTTPMessage (case-insensitive lookup — review R3 caught that a plain
# dict test alone would miss the property the handler actually relies on).
import http.client, io
assert c.is_proxied_request({}) is False
assert c.is_proxied_request({"Authorization": "Bearer x"}) is False
assert c.is_proxied_request({"X-Forwarded-For": "203.0.113.10"}) is True
assert c.is_proxied_request({"X-Forwarded-Proto": "https"}) is True
assert c.is_proxied_request({"X-Forwarded-Host": "portal.internal"}) is True
assert c.is_proxied_request({"X-Forwarded-For": ""}) is False, "empty header shouldn't trip"
assert c.is_proxied_request({"X-Forwarded-For": "203.0.113.10", "Authorization": "Bearer x"}) is True

def hdrs(raw): return http.client.parse_headers(io.BytesIO(raw + b"\r\n"))
assert c.is_proxied_request(hdrs(b"x-forwarded-for: 203.0.113.10\r\n")) is True, "lowercase-header case-insensitive lookup"
assert c.is_proxied_request(hdrs(b"X-FORWARDED-PROTO: https\r\n")) is True, "uppercase-header case-insensitive lookup"
assert c.is_proxied_request(hdrs(b"Authorization: Bearer x\r\n")) is False, "unrelated headers don't trip"
print("T11 proxy fence: X-Forwarded-{For,Proto,Host} trip on dict + real HTTPMessage (case-insensitive)")

print("ALL PASS")
