#!/usr/bin/env python3
"""Verification instrument for the AMQ doorbell (chorusd v4). Read-only.

Sources: the doorbell ledger chorusd appends (one JSON line per event) and, if reachable,
GET http://127.0.0.1:$CHORUSD_PORT/doorbell (default 8766). No maildir access needed: the ledger already records what
chorusd saw through its own read-only `sudo find`, so this audits chorusd's sight, not the store
directly (stated limit; the store-side ground truth is the same find, run as an agent's account).

Event chain per message, all from the ledger:
    seen  -> ring (or suppressed:<reason>, or read-elsewhere) -> turn-end -> answered
  seen         a file appeared under <bird>/inbox/new/
  ring         chorusd typed the prompt (cid); latency = ring.t - seen.t
  turn-end     the bird's Stop hook arrived while busy from that doorbell
  answered     the rung file left new/ (amq_read moved it to cur/): the bird read it
Defects (exit 1):
  STUCK        seen, doorbell ON, no ring/suppressed/read-elsewhere after --sla-hold seconds
  UNANSWERED   ring older than --sla-answer seconds with no answered for one of its ids
  TICK-ERROR   any tick-error / state-file-error line
Modes:
  default            audit the whole ledger and print the chain table
  --expect ID        wait up to --sla-answer for message ID to reach `answered` (end-to-end probe:
                     send a test AMQ first, then run this with its id)
Usage: amq_doorbell_verify.py [--ledger PATH] [--expect ID] [--sla-hold 600] [--sla-answer 480]
"""
import argparse, json, os, sys, time, urllib.request

def load(path):
    out = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try: out.append(json.loads(line))
                    except ValueError: out.append({"event": "UNPARSEABLE", "raw": line[:120]})
    except FileNotFoundError:
        pass
    return out

def ts(e):
    return time.mktime(time.strptime(e["t"], "%Y-%m-%dT%H:%M:%S"))

def status(url):
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            return json.load(r)
    except Exception:
        return None

def chains(events):
    """message id -> dict(seen, ring, answered, suppressed, read_elsewhere)"""
    by = {}
    for e in events:
        ev = e.get("event")
        if ev == "seen":
            by.setdefault(e["id"], {})["seen"] = e
        elif ev == "ring":
            for i in e.get("ids", []):
                by.setdefault(i, {})["ring"] = e
        elif ev == "answered":
            by.setdefault(e["id"], {})["answered"] = e
        elif ev == "suppressed":
            for i in ([e["id"]] if "id" in e else e.get("ids", [])):
                by.setdefault(i, {})["suppressed"] = e
    return by

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", default=os.path.join(os.environ.get("CHORUSD_STATE_DIR", "/var/lib/chorusd"), "doorbell.log"))
    ap.add_argument("--url", default=f"http://127.0.0.1:{os.environ.get('CHORUSD_PORT', '8766')}/doorbell")
    ap.add_argument("--expect", help="message id to wait for (end-to-end probe)")
    ap.add_argument("--sla-hold", type=int, default=600)
    ap.add_argument("--sla-answer", type=int, default=480)
    a = ap.parse_args()

    if a.expect:
        deadline = time.time() + a.sla_answer
        last = ""
        while time.time() < deadline:
            c = chains(load(a.ledger)).get(a.expect, {})
            stage = "answered" if "answered" in c else "ring" if "ring" in c else "suppressed" if "suppressed" in c else "seen" if "seen" in c else "-"
            if stage != last:
                print(f"{time.strftime('%H:%M:%S')} {a.expect}: {stage}" + (f" ({c['suppressed'].get('reason')})" if stage == "suppressed" else ""))
                last = stage
            if stage in ("answered", "suppressed"):
                return 0 if stage == "answered" else 2
            time.sleep(2)
        print(f"TIMEOUT: {a.expect} reached only '{last or '-'}' within {a.sla_answer}s"); return 1

    ev = load(a.ledger)
    st = status(a.url)
    print(f"ledger {a.ledger}: {len(ev)} events; chorusd: " + (f"on={st['on']} rings={st['rings']} busy={st['busy']} pending={st['pending']} chorus_active={st['chorus_active']}" if st else "unreachable"))
    on = st["on"] if st else True
    now = time.time(); defects = []
    errs = [e for e in ev if e.get("event") in ("tick-error", "state-file-error", "UNPARSEABLE")]
    for e in errs: defects.append(("TICK-ERROR", e))
    by = chains(ev)
    print(f"{'message id':44s} {'bird':8s} {'seen':8s} {'ring(+s)':10s} {'answered(+s)':13s} note")
    for mid, c in sorted(by.items(), key=lambda kv: kv[1].get("seen", kv[1].get("ring", {})).get("t", "")):
        s = c.get("seen"); r = c.get("ring"); an = c.get("answered"); sp = c.get("suppressed")
        bird = (s or r or an or sp or {}).get("bird", "?")
        seen_t = s["t"][11:19] if s else "-"
        ring_s = f"+{int(ts(r) - ts(s))}" if (r and s) else ("ring" if r else "-")
        ans_s = f"+{an.get('latency', '?')}" if an else "-"
        note = ""
        if sp: note = f"suppressed:{sp.get('reason')}"
        elif s and not r and not an:
            if on and now - ts(s) > a.sla_hold: note = "STUCK"; defects.append(("STUCK", s))
            else: note = "held"
        elif r and not an:
            if now - ts(r) > a.sla_answer: note = "UNANSWERED"; defects.append(("UNANSWERED", r))
            else: note = "awaiting read"
        print(f"{mid:44s} {bird:8s} {seen_t:8s} {ring_s:10s} {ans_s:13s} {note}")
    rings = [e for e in ev if e.get("event") == "ring"]; answered = [e for e in ev if e.get("event") == "answered"]
    if answered:
        lat = sorted(e.get("latency", 0) for e in answered)
        print(f"\nrings {len(rings)}, answered {len(answered)}, read-latency median {lat[len(lat)//2]}s max {lat[-1]}s")
    supp = {}
    for e in ev:
        if e.get("event") == "suppressed": supp[e.get("reason")] = supp.get(e.get("reason"), 0) + 1
    print(f"suppressed by reason: {supp or 'none'}; baseline/read-elsewhere: {sum(1 for e in ev if e.get('event') in ('baseline','read-elsewhere'))}")
    print(f"defects: {len(defects)} {[(k, e.get('id') or e.get('bird') or e.get('error')) for k, e in defects]}")
    return 1 if defects else 0

if __name__ == "__main__":
    sys.exit(main())
