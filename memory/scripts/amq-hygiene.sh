#!/usr/bin/env bash
# amq-hygiene.sh — Move AMQ 'new/' messages older than N days into 'cur/'.
#
# Runs weekly under systemd as the memory server's user. Keeps chorus_init and
# amq_check output legible by clearing the perpetually-unread backlog
# from stale threads. Messages are NOT deleted — they remain fully
# readable via amq_read after the move, just no longer flagged unread.
#
# Design notes:
#   - AMQ is a Maildir; `new/` = unread, `cur/` = read. Moving between
#     the two changes read-state and nothing else.
#   - Every AMQ message is also auto-stored in the memory collection
#     at send time (server.py; see amq_send). So the message
#     body survives even if the Maildir file were ever lost.
#   - Sweeps every mailbox under the one mail root (LAYOUT rule 3), news
#     included: a digest older than DAYS moves to cur/ like any message.
#
# Env:
#   MEMORY_AMQ_ROOT (default /var/lib/memory-amq)
# DAYS is a constant (30), as the layout states.
#
# Exit codes:
#   0 — sweep completed (may have moved 0 files, that's fine)
#   2 — the mail root is missing or unreadable
#
# 2026-08-16 (operator request, from the AMQ-hygiene logistics thread; an
# on-demand MCP tool was deferred).

set -euo pipefail

AMQ_ROOT="${MEMORY_AMQ_ROOT:-/var/lib/memory-amq}"
DAYS=30

if [ ! -d "$AMQ_ROOT" ]; then
    echo "amq-hygiene: MEMORY_AMQ_ROOT not found: $AMQ_ROOT" >&2
    exit 2
fi

total=0
for newdir in "$AMQ_ROOT"/*/inbox/new; do
    [ -d "$newdir" ] || continue
    curdir="$(dirname "$newdir")/cur"
    agent="$(basename "$(dirname "$(dirname "$newdir")")")"
    # cur/ under maildir.py's directory rule (0750, group amq-poll; owner-only where the group is
    # absent or this user is not in it). A bare mkdir made it 0755 before phase C (2026-10-01).
    if [ ! -d "$curdir" ]; then
        install -d -m 0750 -g amq-poll "$curdir" 2>/dev/null || install -d -m 0750 "$curdir"
    fi

    count=$(find "$newdir" -maxdepth 1 -type f -mtime +"$DAYS" | wc -l)
    if [ "$count" -gt 0 ]; then
        find "$newdir" -maxdepth 1 -type f -mtime +"$DAYS" \
            -exec mv -t "$curdir" {} +
        printf 'amq-hygiene: %-10s moved %5d messages older than %s days\n' \
            "$agent" "$count" "$DAYS"
        total=$((total + count))
    fi
done

printf 'amq-hygiene: swept %d messages total (new/ -> cur/) across %s\n' \
    "$total" "$AMQ_ROOT"
