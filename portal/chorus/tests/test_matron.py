"""matron_prompt under the test roster: the v6 pointer (repo, file, commit, section) and every
refusal. `python3 test_matron.py`. The pointer is what an agent's CLAUDE.md block checks, so the
shape assertions here are the contract with that block."""
import os, sys, importlib.util, tempfile
HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["COTERIE_AGENTS_JSON"] = os.path.join(HERE, "birds-test.json")
os.environ.setdefault("CHORUSD_STATE_DIR", tempfile.mkdtemp())
os.environ["CHORUSD_PANE_KEY_TOKEN_PATH"] = "/nonexistent-token-path-for-tests"

spec = importlib.util.spec_from_file_location("chorusd", os.path.join(HERE, "..", "chorusd.py"))
c = importlib.util.module_from_spec(spec); spec.loader.exec_module(c)
c.load_birds()   # ALL_BIRDS = ["orch", "bee"] from birds-test.json

GOOD = {"bird": "bee", "repo": "~/repos/coterie", "file": "init/README.md", "commit": "ff12466", "lines": "346-700",
        "section": "### Bee — default lane", "sender": "orch"}

def run(**over):
    body = dict(GOOD); body.update(over)
    return c.matron_prompt({k: v for k, v in body.items() if v is not None}, "a1b2c3")

# M1 happy — the pointer names the section, file, commit and clone, and how to read it
status, resp, prompt = run()
assert status == 200 and resp["bird"] == "bee" and resp["commit"] == "ff12466", (status, resp)
lines = prompt.split("\n")
assert lines[0] == "[MATRON INIT — BEE] #a1b2c3", lines[0]
assert '"### Bee — default lane"' in prompt and "init/README.md" in prompt and "~/repos/coterie" in prompt
assert "git -C ~/repos/coterie show ff12466:init/README.md | sed -n '346,700p'" in prompt
assert "fetch -q &&" not in prompt, "a failed fetch must not block reading a commit the clone already has"
assert "(if your clone doesn't have that commit yet, run git -C ~/repos/coterie fetch -q first)" in prompt
assert "git -C ~/repos/coterie merge-base --is-ancestor ff12466 origin/main must exit 0" in prompt, "v6.2: the commit must be on main"
assert "stop and tell the operator" in prompt
assert "lines 346-700 of init/README.md" in prompt and resp["lines"] == "346-700"
assert "/tmp" not in prompt and "payload" not in prompt.lower(), "no drop file, no payload wording"
assert resp["bytes"] == len(prompt) and len(lines) == 3
print("M1 happy: pointer is 3 lines, names lines/section/file/commit/clone, on-main check, no drop file")

# M2 note — one extra line, attributed to the sender
status, resp, prompt = run(note="state row moved twice; expected.")
assert status == 200 and prompt.split("\n")[-1] == "Note from orch: state row moved twice; expected.", prompt
print("M2 note: appended as its own line")

# M3 refusals — each field, one at a time
cases = [
    ({"bird": "hawk"}, "unknown bird"),
    ({"text": "old-style full prompt"}, "text is retired"),
    ({"repo": "/tmp/evil"}, "repo must be"),
    ({"repo": "~/.hidden/x"}, "repo must be"),
    ({"file": "../../etc/passwd"}, "file must be"),
    ({"file": "/etc/passwd"}, "file must be"),
    ({"commit": "HEAD"}, "commit must be"),
    ({"commit": "abc"}, "commit must be"),
    ({"lines": "700-346"}, "lines must be"),
    ({"lines": "0-10"}, "lines must be"),
    ({"lines": "1-5000"}, "lines must be"),
    ({"lines": "346,700"}, "lines must be"),
    ({"lines": None}, "lines must be"),
    ({"section": "no hash mark"}, "section must be"),
    ({"section": "### two\nlines"}, "section must be"),
    ({"section": "### " + "x" * 200}, "section must be"),
    ({"note": "line one\nline two"}, "note must be"),
    ({"note": "esc \x1b[2J"}, "note must be"),
    ({"note": "c1 \u009b"}, "note must be"),
    ({"sender": "two words"}, "sender must be"),
]
for over, want in cases:
    status, resp, prompt = run(**over)
    assert status == 400 and prompt is None and want in resp["error"], (over, status, resp)
print(f"M3 refusals: {len(cases)} malformed bodies -> 400, nothing typed")

# M4 repo with a ".." segment: every segment must start with a name character
status, resp, prompt = run(repo="~/../other")
assert status == 400, "a '..' segment must not escape the agent's home"
print("M4 repo: '~/../other' -> 400")

# M5 full commit id accepted as-is (callers send 40 hex)
full = "52e7540" + "0" * 33
status, resp, prompt = run(commit=full)
assert status == 200 and resp["commit"] == full and f"show {full}:init/README.md" in prompt
print("M5 full 40-hex commit id: accepted and used verbatim")

print("ALL OK")
