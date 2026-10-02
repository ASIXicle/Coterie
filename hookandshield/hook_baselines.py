#!/usr/bin/env python3
"""
hook_baselines.py — Load the approved-hash registry, check a file against it.

The baseline is the primary gate for hook-detect on Claude settings.json.
Content rules are secondary (severity annotation only). Rationale: see
hook_baselines.yaml header + the 2026-08-16 review of the Shai-Hulud FP.

CANONICAL FORM (canon: v1, added 2026-09-10 for LIVE DEFECT surfaced in
the 2026-09-10 canonical-form review — see hook_baselines.yaml header):

Some Claude Code settings keys move under normal use without changing power
granted:
    model              (an operator's /model flips — 2026-09-04 rebaseline case)
    theme              (light/dark preference)
    tui                (renderer preference; a 2026-09-04 /tui flip)
    modelSettings      (effort-level per model — display shape)

Raw-hash gating treats every flip as a mismatch, which triggers the
analyser on a benign write. Because our rendered doorbell Stop hook is
correctly-benign-but-analyser-ESCALATE (loopback-fetch-with-interpolated-
payload; payload cannot leave 127.0.0.1:8766), an EXPECTED hash flip
becomes a HIGH alert on a hook the flock shipped and baselined the same
day. Canonical hashing v1 strips the four preference keys (with TYPE
GUARDS — strip only if the value has the expected shape; otherwise the
key is hashed and the finding names the reason), then sha256s a
deterministic canonical serialization.

Polarity: STRIP-list (blacklist), not allowlist. Unknown keys are HASHED
so any future Claude Code addition granting power alerts until re-approved
(fail-CLOSED). Cost: one re-approval per new benign preference key. Right
cost for a security gate.

Versioned per-entry: `canon: v1` in the yaml row selects canonical hashing
for that row; absent selects raw file hashing (today's behaviour). Mixed
states are fine and visible — each bird migrates their own row.

API:
    sha256_file(filepath) -> str
        streaming SHA-256 of raw file content (unchanged from v0)

    canonical_sha256(filepath, canon) -> dict
        {sha, stripped, kept, note}
        canon="v1" applies the strip-list with type guards; anything else
        falls back to raw. Malformed JSON → raw sha + note "canon:unparseable".

    check(filepath) -> dict
        {"status": "match"|"mismatch"|"missing",
         "entry": dict|None,
         "current_sha": str, "canon": str,
         "stripped": list, "kept": list, "note": str}
        Reads entry's `canon` field to decide raw vs canonical. Caller no
        longer computes the sha first — check() owns the shape decision
        because only the entry knows which hashing to apply.

    reload() -> int
        test hook — re-read the yaml
"""

import hashlib
import json
import os
import stat
from pathlib import Path

import yaml

# The registry lives next to this module by default. SHIELD_BASELINES_FILE points a
# site at a registry kept elsewhere (a config directory, or the .example file for a
# smoke test); the shipped hook_baselines.example.yaml documents the row format.
_BASELINE_PATH = Path(
    os.environ.get("SHIELD_BASELINES_FILE")
    or Path(__file__).resolve().parent / "hook_baselines.yaml"
)


# Canonical v1 strip-list with type guards. Each entry is (key, predicate)
# where predicate(value) returns True iff the value has the expected shape;
# non-matching values are HASHED with a reason so exfil via a preference key
# alerts loudly.
def _is_str(v):
    return isinstance(v, str)


def _is_model_settings_v1(v):
    # dict of {model_name: {effortLevel: str}} — nothing else in the inner
    # dict. Any foreign key or non-string effortLevel disqualifies the strip
    # so the value goes into the hash and the reason names why.
    if not isinstance(v, dict):
        return False
    for _model_name, inner in v.items():
        if not isinstance(inner, dict):
            return False
        for k, val in inner.items():
            if k != "effortLevel":
                return False
            if not isinstance(val, str):
                return False
    return True


_CANON_V1_STRIP = [
    ("model", _is_str),
    ("theme", _is_str),
    ("tui", _is_str),
    ("modelSettings", _is_model_settings_v1),
]


def _load() -> dict:
    if not _BASELINE_PATH.exists():
        return {}
    # Explicit utf-8 encoding (review note A hardening) — the yaml already
    # carries em dashes and arrows in flock notes, and a locale-coerced
    # interpreter (LC_ALL=C, PYTHONUTF8=0) would otherwise fail at import
    # with the same exit code as ALERT.
    with open(_BASELINE_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    entries = data.get("baselines") or []
    out = {}
    for e in entries:
        if not isinstance(e, dict):
            continue
        fp = e.get("filepath")
        sha = e.get("sha256")
        if fp and sha:
            out[fp] = e
    return out


_BASELINES = _load()


class NotRegularFile(OSError):
    """The path is not a regular file (a FIFO, a device, a socket, a directory)."""


def open_nofollow(filepath: str, mode: str = "r", **kw):
    """Open a REGULAR file for reading. Refuses a symlink at the last path component (OSError,
    errno ELOOP) and anything that is not a regular file (NotRegularFile). hook-detect runs as root
    over the agents' homes: a link planted there must not make root read a file that agent could
    not, and a FIFO planted there must not hang the scan (opening one for reading blocks until a
    writer appears, and the weekly timer never starts a unit that is still running). O_NONBLOCK
    makes that open return at once; nothing is read from anything but a regular file."""
    fd = os.open(filepath, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise NotRegularFile(f"not a regular file: {filepath}")
        return os.fdopen(fd, mode, **kw)
    except BaseException:
        os.close(fd)
        raise


def sha256_file(filepath: str) -> str:
    """Streaming SHA-256 of raw file content. Unchanged from v0."""
    h = hashlib.sha256()
    with open_nofollow(filepath, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def canonical_sha256(filepath: str, canon: str) -> dict:
    """Compute the canonical sha per the given canon version.

    Returns dict with:
      sha:          str, the hex sha256
      stripped:     list of str, keys removed before hashing
      kept:         list of str, top-level keys included in the hash
      guard_failed: list of str, keys on the strip list whose value shape
                    was wrong so they were HASHED anyway (review note B —
                    the exfil-edge reporting path)
      note:         str, "canon:v1" / "canon:raw" / "canon:unparseable" /
                    "canon:root-not-dict"

    Fallback on JSON parse failure: raw file hash with note "canon:unparseable".
    Do not "repair" — same conservative fallback the analyser already uses.

    Encoding is explicit utf-8 on the read (review note A hardening) so that
    a locale-coerced interpreter can never surprise this path.
    """
    if canon != "v1":
        # v0 or unknown → raw file hash; note names the mode.
        return {
            "sha": sha256_file(filepath),
            "stripped": [],
            "kept": [],
            "guard_failed": [],
            "note": "canon:raw",
        }
    try:
        with open_nofollow(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
    # ValueError catches both json.JSONDecodeError (its parent) and any
    # UnicodeDecodeError raised despite encoding= being explicit (belt-and-
    # braces per review note A). OSError/PermissionError as before.
    except (ValueError, OSError):
        return {
            "sha": sha256_file(filepath),
            "stripped": [],
            "kept": [],
            "guard_failed": [],
            "note": "canon:unparseable",
        }
    if not isinstance(data, dict):
        return {
            "sha": sha256_file(filepath),
            "stripped": [],
            "kept": [],
            "guard_failed": [],
            "note": "canon:root-not-dict",
        }
    stripped = []
    guard_failed = []
    kept_all = list(data.keys())
    for key, predicate in _CANON_V1_STRIP:
        if key in data:
            if predicate(data[key]):
                stripped.append(key)
                del data[key]
            else:
                # Value shape did not match the guard: the key is HASHED with
                # the rest, AND named in guard_failed so the reader sees the
                # exfil-edge case rather than having to notice the strip key
                # sitting in `kept` on a v1 row (review note B).
                guard_failed.append(key)
    kept = [k for k in kept_all if k not in stripped]
    canonical = json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return {
        "sha": hashlib.sha256(canonical).hexdigest(),
        "stripped": sorted(stripped),
        "kept": sorted(kept),
        "guard_failed": sorted(guard_failed),
        "note": "canon:v1",
    }


def check(filepath: str) -> dict:
    """Look up the entry for filepath and compute its sha per entry's canon.

    Signature changed from v0: no longer takes a precomputed sha, because
    only the entry knows which canonicalisation to apply. Callers should
    pass just the filepath and read all state from the returned dict.

    Returns:
      status:       "match" | "mismatch" | "missing"
      entry:        the yaml row (dict) or None
      current_sha:  the sha computed under the row's canon (str)
      canon:        the row's `canon` value (str) — "raw" if absent
      stripped:     list of keys stripped (empty on raw / unparseable)
      kept:         list of top-level keys hashed
      guard_failed: list of strip-key names whose value shape was wrong so
                    they were HASHED anyway (empty on raw / unparseable;
                    review note B — the exfil-edge reporting path)
      note:         canonical mode string ("canon:v1" / "canon:raw" / etc.)
    """
    entry = _BASELINES.get(filepath)
    if entry is None:
        try:
            current = sha256_file(filepath)
        except (PermissionError, OSError):
            current = ""
        return {
            "status": "missing",
            "entry": None,
            "current_sha": current,
            "canon": "raw",
            "stripped": [],
            "kept": [],
            "guard_failed": [],
            "note": "canon:raw",
        }
    canon = entry.get("canon") or "raw"
    computed = canonical_sha256(filepath, canon)
    status = "match" if entry.get("sha256") == computed["sha"] else "mismatch"
    return {
        "status": status,
        "entry": entry,
        "current_sha": computed["sha"],
        "canon": canon,
        "stripped": computed["stripped"],
        "kept": computed["kept"],
        "guard_failed": computed["guard_failed"],
        "note": computed["note"],
    }


def reload() -> int:
    """Re-read the baseline file. Returns number of loaded entries."""
    global _BASELINES
    _BASELINES = _load()
    return len(_BASELINES)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=(
        "Load the baseline registry and print each entry. "
        "With --canon <file>: compute the canonical sha for <file> per "
        "its entry's canon (or raw if no entry / no canon field). "
        "Prints stripped/kept for row-owner self-service rebaseline "
        "(hook_baselines.yaml canon:v1 migration, 2026-09-10)."
    ))
    ap.add_argument(
        "--canon",
        metavar="FILEPATH",
        help="Compute canonical sha for FILEPATH per its baseline entry's canon field",
    )
    args = ap.parse_args()

    if args.canon:
        entry = _BASELINES.get(args.canon)
        # Default to v1 for CLI output when no entry exists (helps birds
        # compute a canonical sha before adding their row). When the entry
        # exists but has no `canon` field, honour the entry's mode so the
        # printed comparison uses the same shape the audit will use.
        canon = entry.get("canon") if entry else "v1"
        canon = canon or "raw"
        result = canonical_sha256(args.canon, canon)
        print(f"filepath:      {args.canon}")
        print(f"canon:         {canon}   ({result['note']})")
        print(f"sha256:        {result['sha']}")
        print(f"stripped:      {result['stripped']}")
        print(f"kept:          {result['kept']}")
        if result["guard_failed"]:
            print(f"guard_failed:  {result['guard_failed']}   (values had wrong shape — hashed)")
        if entry:
            baseline_sha = entry.get("sha256")
            entry_canon = entry.get("canon") or "raw"
            if entry_canon != canon:
                # Row is still on raw; we've been asked to compute v1 for the
                # migration workflow. The comparison is not meaningful. Say so
                # explicitly per review note C — "mismatch" here would read as
                # trouble when it's just the migration in progress.
                print(f"baseline:      {baseline_sha}  (row is {entry_canon} — v1 sha shown for migration; not comparable)")
            else:
                match = "match" if baseline_sha == result["sha"] else "mismatch"
                print(f"baseline:      {baseline_sha}  → {match}")
        else:
            print("baseline:      (no entry yet — this file is not in hook_baselines.yaml)")
        raise SystemExit(0)

    print(f"loaded {len(_BASELINES)} baseline entr{'y' if len(_BASELINES) == 1 else 'ies'} from {_BASELINE_PATH}")
    for fp, e in _BASELINES.items():
        canon = e.get("canon") or "raw"
        print(f"  {fp}")
        print(f"    sha256:      {e['sha256']}   [canon:{canon}]")
        print(f"    approved_by: {e.get('approved_by', '?')}")
        print(f"    approved_at: {e.get('approved_at', '?')}")
        print(f"    note:        {e.get('note', '')}")
    raise SystemExit(0)
