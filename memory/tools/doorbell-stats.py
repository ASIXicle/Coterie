#!/usr/bin/env python3
"""doorbell-stats.py — rolling-window ring statistics from the chorusd ledger.

Read-only Matron-side companion to `tools/amq_doorbell_verify.py`. The verifier
answers "is anything broken?"; this tool answers "who is talking to whom, how
often, how fast, and is anyone getting concentrated load?" The whole point
is watching for drift patterns the mechanism can't see on its own — twin-
clustering, chatter loops that fit inside the loop-guard, lane-owner
concentration — without a new surveillance layer (ledger already has it).

Ledger: `$CHORUSD_STATE_DIR/doorbell.log` (default /var/lib/chorusd; JSON lines).
Window: `--hours N` (default 168 = 7 days).

Usage:
    doorbell-stats.py [--ledger PATH] [--hours N] [--json]

Timestamps in the ledger are naive-UTC (chorusd's `datetime.utcnow().isoformat()`);
this tool treats naive as UTC. The `created` field on AMQ messages carries an
explicit `+00:00` and is not read here — chorusd's own `_t` is what gates the
window.

Overall / per-bird / per-pair figures come from the events chorusd records:
    baseline    → deploy-time unread per bird; excluded from ring counts
    seen        → chorusd noticed a new file in <bird>/inbox/new/
    ring        → chorusd typed the ring text; carries {cid, ids, senders}
    answered    → the rung file left new/; carries {id, latency}
    suppressed  → {reason: off|loop-guard, ids, senders} (43ebd68+)
    turn-end, busy-timeout, tick-error → operational, surfaced in --json only

A ring coalesces multiple queued messages into one send-keys — the same ring
credits every sender in the ring's `senders` field for `pairs_rings`
(volume-by-sender), and per-id attribution for `answered`/`latency` comes
from the corresponding `seen` events (which carry `{bird, id, sender}` per
message). So coalesced-multi-sender rings still get correct per-pair
answered counts, not dropped.
"""

import argparse
import json
import os
import statistics
import sys
from collections import defaultdict, Counter
from datetime import datetime, timedelta, timezone

DEFAULT_LEDGER = os.path.join(os.environ.get("CHORUSD_STATE_DIR", "/var/lib/chorusd"), "doorbell.log")


def parse_ts(s: str):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def load(path: str, since: datetime):
    events = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = parse_ts(e.get("t", ""))
                if t is None or t < since:
                    continue
                e["_t"] = t
                events.append(e)
    except FileNotFoundError:
        print(f"ledger not found: {path}", file=sys.stderr)
        sys.exit(2)
    return events


def analyze(events):
    pairs_rings = Counter()          # (sender, recipient) → n
    pairs_answered = Counter()       # (sender, recipient) → n
    pairs_latencies = defaultdict(list)  # (sender, recipient) → [seconds]
    pairs_suppressed = defaultdict(Counter)  # (sender, recipient) → {reason: n}
    recv_totals = Counter()          # bird → rings received (as recipient)
    sent_totals = Counter()          # bird → rings sent (credited per-sender in coalesced rings)
    ops = Counter()                  # operational event counts (turn-end, busy-timeout, tick-error)
    all_latencies = []
    suppressed_by_reason = Counter()
    id_to_pair = {}                  # message id → (sender, recipient) OR (senders_tuple, recipient)

    for e in events:
        ev = e.get("event")

        if ev == "seen":
            # per-id attribution for answered comes from seen (carries the
            # specific sender for each individual message id), so coalesced
            # rings with 2+ senders still attribute answered correctly.
            mid = e.get("id")
            sender = e.get("sender")
            recipient = e.get("bird")
            if mid and sender and recipient:
                id_to_pair[mid] = (sender, recipient)

        elif ev == "ring":
            recipient = e.get("bird")
            senders = e.get("senders", []) or []
            for sender in senders:
                pairs_rings[(sender, recipient)] += 1
                sent_totals[sender] += 1
                recv_totals[recipient] += 1

        elif ev == "answered":
            mid = e.get("id")
            latency = e.get("latency")
            pair = id_to_pair.get(mid)
            if pair is not None:
                pairs_answered[pair] += 1
                if latency is not None:
                    pairs_latencies[pair].append(latency)
            if latency is not None:
                all_latencies.append(latency)

        elif ev == "suppressed":
            recipient = e.get("bird")
            senders = e.get("senders", []) or []
            ids = e.get("ids", []) or []
            reason = e.get("reason", "unknown")
            n = e.get("n") or len(ids) or len(senders) or 1
            for sender in senders:
                pairs_suppressed[(sender, recipient)][reason] += 1
            suppressed_by_reason[reason] += n

        elif ev in ("turn-end", "busy-timeout", "tick-error", "toggle", "start", "baseline"):
            ops[ev] += 1

    return {
        "pairs_rings": pairs_rings,
        "pairs_answered": pairs_answered,
        "pairs_latencies": pairs_latencies,
        "pairs_suppressed": pairs_suppressed,
        "recv_totals": recv_totals,
        "sent_totals": sent_totals,
        "ops": ops,
        "all_latencies": all_latencies,
        "suppressed_by_reason": suppressed_by_reason,
    }


def print_report(s, hours: int, event_count: int):
    total_rings = sum(s["pairs_rings"].values())
    total_answered = sum(s["pairs_answered"].values())
    unanswered = total_rings - total_answered
    lats = sorted(s["all_latencies"])

    print(f"=== doorbell-stats · last {hours}h · {event_count} events read ===")
    print()
    print(f"rings sent (credited per-sender in coalesced rings): {total_rings}")
    print(f"rings answered (rung file left new/):                {total_answered}")
    print(f"unanswered:                                          {unanswered}")
    if lats:
        med = statistics.median(lats)
        mx = max(lats)
        p95 = lats[int(0.95 * (len(lats) - 1))] if len(lats) > 1 else lats[0]
        print(f"latency (all):  median {med:.0f}s · p95 {p95}s · max {mx}s · n={len(lats)}")
    if s["suppressed_by_reason"]:
        parts = ", ".join(f"{r}={n}" for r, n in sorted(s["suppressed_by_reason"].items()))
        print(f"suppressed:     {parts}")
    if s["ops"]:
        parts = ", ".join(f"{k}={v}" for k, v in sorted(s["ops"].items()))
        print(f"ops events:     {parts}")
    print()

    # Sender → recipient matrix, sorted by ring count desc
    if s["pairs_rings"]:
        print("=== sender → recipient (rolling) ===")
        print(f"  {'sender':<10} {'recipient':<10} {'rings':>5} {'ansd':>5} {'unans':>5} "
              f"{'med lat':>8} {'suppressed':<24}")
        for pair in sorted(s["pairs_rings"], key=lambda k: (-s["pairs_rings"][k], k)):
            snd, rcv = pair
            rings = s["pairs_rings"][pair]
            ansd = s["pairs_answered"].get(pair, 0)
            unans = rings - ansd
            plats = s["pairs_latencies"].get(pair, [])
            med = f"{statistics.median(plats):.0f}s" if plats else "-"
            sup = s["pairs_suppressed"].get(pair, {})
            sup_s = ",".join(f"{k}:{v}" for k, v in sorted(sup.items())) if sup else "-"
            print(f"  {snd:<10} {rcv:<10} {rings:>5} {ansd:>5} {unans:>5} {med:>8} {sup_s:<24}")
        print()

    # Per-bird totals
    all_birds = sorted(set(list(s["sent_totals"]) + list(s["recv_totals"])))
    if all_birds:
        print("=== per bird ===")
        print(f"  {'bird':<10} {'sent':>5} {'received':>9}")
        for b in all_birds:
            print(f"  {b:<10} {s['sent_totals'].get(b, 0):>5} {s['recv_totals'].get(b, 0):>9}")


def print_json(s):
    out = {
        "pairs": [
            {
                "sender": snd,
                "recipient": rcv,
                "rings": s["pairs_rings"][(snd, rcv)],
                "answered": s["pairs_answered"].get((snd, rcv), 0),
                "median_latency_s": (
                    statistics.median(s["pairs_latencies"][(snd, rcv)])
                    if s["pairs_latencies"].get((snd, rcv))
                    else None
                ),
                "suppressed": dict(s["pairs_suppressed"].get((snd, rcv), {})),
            }
            for (snd, rcv) in sorted(s["pairs_rings"])
        ],
        "per_bird": {
            b: {
                "sent": s["sent_totals"].get(b, 0),
                "received": s["recv_totals"].get(b, 0),
            }
            for b in sorted(set(list(s["sent_totals"]) + list(s["recv_totals"])))
        },
        "overall": {
            "total_rings": sum(s["pairs_rings"].values()),
            "total_answered": sum(s["pairs_answered"].values()),
            "median_latency_s": (
                statistics.median(s["all_latencies"]) if s["all_latencies"] else None
            ),
            "suppressed_by_reason": dict(s["suppressed_by_reason"]),
            "ops": dict(s["ops"]),
        },
    }
    print(json.dumps(out, indent=2))


def main():
    ap = argparse.ArgumentParser(
        description=(
            "Rolling-window ring stats from the chorusd doorbell ledger. "
            "Read-only. Prints a sender→recipient matrix, per-bird totals, "
            "and an overall summary."
        )
    )
    ap.add_argument("--ledger", default=DEFAULT_LEDGER, help=f"ledger path (default: {DEFAULT_LEDGER})")
    ap.add_argument("--hours", type=int, default=168, help="rolling window in hours (default: 168 = 7d)")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of the text report")
    args = ap.parse_args()

    since = datetime.now(timezone.utc) - timedelta(hours=args.hours)
    events = load(args.ledger, since)
    stats = analyze(events)

    if args.json:
        print_json(stats)
    else:
        print_report(stats, args.hours, len(events))


if __name__ == "__main__":
    main()
