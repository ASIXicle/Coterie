"""Tests for addressed handoffs: memory_store's `to_agent` and chorus_init's handoff selection.

Pure-logic tests via AST extraction of `_handoff_recipient` and `_select_handoffs` from server.py
(the pattern bootstrap_patch_test.py uses), plus structural checks that memory_store and
chorus_init still call them. Runs without server startup, chroma, or the server's uid.

Run: python3 tests/handoff_recipient_test.py
Exit 0 on all pass, exit 1 on any failure.
"""

import ast
import os
import sys

SERVER = os.environ.get(
    "PERSMEM_SERVER_PY",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server.py"),
)
SRC = open(SERVER).read()
TREE = ast.parse(SRC)


def _extract():
    ns: dict = {}
    want = {"HANDOFF_BOOT_LIMIT", "HANDOFF_SHARED_PROJECT", "_handoff_recipient", "_select_handoffs",
            "_handoff_scope_warning"}
    for n in TREE.body:
        if isinstance(n, ast.Assign) and any(getattr(t, "id", "") in want for t in n.targets):
            exec(ast.get_source_segment(SRC, n), ns)
    for n in TREE.body:
        if isinstance(n, ast.FunctionDef) and n.name in want:
            exec(ast.get_source_segment(SRC, n), ns)
    missing = want - set(ns)
    assert not missing, f"not found in server.py: {missing}"
    return ns


NS = _extract()
recipient = NS["_handoff_recipient"]
scope_warning = NS["_handoff_scope_warning"]
select = NS["_select_handoffs"]
LIMIT = NS["HANDOFF_BOOT_LIMIT"]
SHARED = NS["HANDOFF_SHARED_PROJECT"]

ROSTER = ["alpha", "beta", "gamma"]
CUTOFF = "2026-01-01T00:00:00+00:00"
P = SHARED  # most cases boot the shared project; the lane cases below use others

_failures: list[str] = []
_passes: list[str] = []


def _assert(cond, label):
    (_passes if cond else _failures).append(label)


def row(hid, stored_at, to_agent=None, status="active", project=None):
    meta = {"stored_at": stored_at, "status": status, "type": "handoff", "project": project or SHARED}
    if to_agent is not None:
        meta["to_agent"] = to_agent
    return (hid, f"doc {hid}", meta)


def ids(rows):
    return [r[0] for r in rows]


# --- _handoff_recipient -------------------------------------------------------------------------

_assert(recipient("handoff", "", ROSTER) == ("", ""), "empty to_agent: unaddressed, no error")
_assert(recipient("handoff", "  Beta ", ROSTER) == ("beta", ""), "name is trimmed and lowercased")
name, err = recipient("handoff", "betta", ROSTER)
_assert(name == "" and "not an agent on the roster" in err, "a name off the roster is refused")
name, err = recipient("decision", "beta", ROSTER)
_assert(name == "" and "handoffs only" in err, "to_agent on a non-handoff is refused")
_assert(recipient("decision", "", ROSTER) == ("", ""), "non-handoff without to_agent is untouched")

# --- _handoff_scope_warning ---------------------------------------------------------------------

_assert(scope_warning("beta", SHARED) == "", "addressed under the shared project: no warning")
_assert(scope_warning("", "lane-x") == "", "unaddressed under a lane: no warning")
w = scope_warning("beta", "lane-x")
_assert("lane-x" in w and SHARED in w, "addressed under a lane: the warning names the lane and the fix")

# --- _select_handoffs ---------------------------------------------------------------------------

# The failure this fixes: three newer handoffs for one agent pushed another agent's out of its boot.
pairs = [
    row("for-beta", "2026-01-10T00:00:00+00:00", "beta"),
    row("for-alpha-1", "2026-01-11T00:00:00+00:00", "alpha"),
    row("for-alpha-2", "2026-01-12T00:00:00+00:00", "alpha"),
    row("for-alpha-3", "2026-01-13T00:00:00+00:00", "alpha"),
]
_assert(ids(select(pairs, "beta", P, CUTOFF)) == ["for-beta"],
        "beta's handoff survives three newer ones addressed to alpha")
_assert(ids(select(pairs, "alpha", P, CUTOFF)) == ["for-alpha-3", "for-alpha-2", "for-alpha-1"],
        "alpha gets its own three, newest first, and not beta's")
_assert(ids(select(pairs, "gamma", P, CUTOFF)) == [], "gamma gets none of the addressed handoffs")

# Unaddressed handoffs (and every row from before the field existed) still reach everyone.
pairs = [
    row("legacy", "2026-01-05T00:00:00+00:00"),
    row("blank", "2026-01-06T00:00:00+00:00", ""),
    row("for-alpha", "2026-01-07T00:00:00+00:00", "alpha"),
]
_assert(ids(select(pairs, "beta", P, CUTOFF)) == ["blank", "legacy"], "unaddressed rows ship to beta")
_assert(ids(select(pairs, "alpha", P, CUTOFF)) == ["for-alpha", "blank", "legacy"],
        "alpha gets its own plus the unaddressed ones")

# A caller with no name (identity-confused boot) sees everything, as before.
_assert(ids(select(pairs, "", P, CUTOFF)) == ["for-alpha", "blank", "legacy"],
        "no caller name: no recipient filter")

# A stored mixed-case name still matches (the filter lowercases).
_assert(ids(select([row("mixed", "2026-01-07T00:00:00+00:00", "Alpha")], "alpha", P, CUTOFF)) == ["mixed"],
        "stored to_agent compares case-insensitively")

# Superseded and stale rows never ship, addressed or not.
pairs = [
    row("old", "2025-12-01T00:00:00+00:00", "beta"),
    row("replaced", "2026-01-08T00:00:00+00:00", "beta", status="superseded"),
    row("current", "2026-01-09T00:00:00+00:00", "beta"),
]
_assert(ids(select(pairs, "beta", P, CUTOFF)) == ["current"], "superseded and pre-cutoff rows are dropped")

# Scope: the boot's project, plus handoffs addressed to the caller under the shared project.
pairs = [
    row("shared-to-beta", "2026-01-10T00:00:00+00:00", "beta"),
    row("shared-unaddressed", "2026-01-11T00:00:00+00:00"),
    row("shared-to-alpha", "2026-01-12T00:00:00+00:00", "alpha"),
    row("lane-y-to-beta", "2026-01-13T00:00:00+00:00", "beta", project="lane-y"),
    row("lane-x-unaddressed", "2026-01-14T00:00:00+00:00", project="lane-x"),
]
_assert(ids(select(pairs, "beta", "lane-x", CUTOFF)) == ["lane-x-unaddressed", "shared-to-beta"],
        "a lane boot gets its lane's rows plus shared rows addressed to it, nothing else")
_assert(ids(select(pairs, "beta", "lane-y", CUTOFF)) == ["lane-y-to-beta", "shared-to-beta"],
        "a row addressed under a lane ships only in that lane's boot")
_assert(ids(select(pairs, "beta", SHARED, CUTOFF)) == ["shared-unaddressed", "shared-to-beta"],
        "a shared-project boot: shared rows for it or for everyone; no lane rows")
_assert(ids(select(pairs, "", "lane-x", CUTOFF)) == ["lane-x-unaddressed"],
        "no caller name: only the boot's project")

# The limit still holds.
pairs = [row(f"h{i}", f"2026-01-{10 + i:02d}T00:00:00+00:00") for i in range(LIMIT + 2)]
_assert(len(select(pairs, "beta", P, CUTOFF)) == LIMIT, f"at most HANDOFF_BOOT_LIMIT ({LIMIT}) ship")

# --- wiring: the tools still call the helpers ----------------------------------------------------


def _fn(name):
    for n in TREE.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    raise AssertionError(f"{name} not found")


store = _fn("memory_store")
store_src = ast.get_source_segment(SRC, store)
_assert("to_agent" in [a.arg for a in store.args.args], "memory_store takes to_agent")
_assert("_handoff_recipient(" in store_src, "memory_store validates to_agent through _handoff_recipient")
_assert('metadata["to_agent"]' in store_src, "memory_store writes to_agent into the stored metadata")
_assert("_handoff_scope_warning(recipient, project)" in store_src,
        "memory_store returns the scope warning to its caller")
init_src = ast.get_source_segment(SRC, _fn("chorus_init"))
_assert("_select_handoffs(pairs, agent, project, cutoff)" in init_src,
        "chorus_init selects handoffs through _select_handoffs with the boot's project")
_assert('{"type": "handoff"}' in init_src and '{"project": project}' not in init_src,
        "chorus_init's query doesn't pre-filter by project (scope is _select_handoffs')")

for p in _passes:
    print(f"PASS  {p}")
for f in _failures:
    print(f"FAIL  {f}")
print(f"\n{len(_passes)} passed, {len(_failures)} failed")
sys.exit(1 if _failures else 0)
