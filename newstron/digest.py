#!/usr/bin/env python3
"""
digest.py — Generate the daily news digest.

Pulls news items stored in the last 24h (or configurable window), groups
by tier, and delivers it through the memory server's news API to the `news` mailbox
(the server fixes the mailbox; this process never touches a Maildir), where every agent
reads it via amq_check + amq_read.

Template-only: no LLM summarization. Boring is honest.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Site paths come from the environment (the unit's environment file); the log
# default is a generic FHS location.
LOG_DIR = Path(os.environ.get("NEWSTRON_LOG_DIR", "/var/log/newstron"))


# Pull window (items stored in last N hours)
WINDOW_HOURS = 24
# How many items per tier in the digest. The memory server caps a search at 20 results; with the
# window passed as `since` it over-fetches and filters by date before trimming, so these are the
# window's best matches, not the collection's.
MAX_ITEMS_PER_TIER = 20

# A head (memory server heads/, scored as each item is stored) whose "yes" pulls items from the
# other tiers into the Security section. Empty = off. Items stored before the head existed carry
# no answer and are left out.
DIGEST_HEAD = os.environ.get("NEWSTRON_DIGEST_HEAD", "security").strip()
HEAD_QUERY = "computer security vulnerability exploit attack breach hack advisory"
HEAD_TIERS = (2, 3, 4, 5, 6, 7, 8, 9)
MAX_MAYBE = 10

TIER_LABELS = {
    1: "Security & Operational",
    2: "Infrastructure",
    3: "Experiment-relevant",
    4: "Academic",
}


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=LOG_DIR / "digest.log",
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    if sys.stderr.isatty():
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        logging.getLogger().addHandler(h)


def fetch_tier(client, tier: int, since_iso: str) -> list[dict]:
    """Pull up to MAX_ITEMS_PER_TIER items from the given tier, stored inside the window.

    `since` goes to news_search, which filters by date in Python after an over-fetch (ChromaDB's
    $gte is numeric-only). Until 2026-09-30 it was not passed: the search returned the 20 best
    matches from the whole collection and this function then dropped the ones outside the window,
    so a day's digest showed only the few recent items that ranked among all-time matches (on
    09-30, 1 of the 12 security items stored in the last 24 hours). The date filter below stays.
    """
    # news_search is semantic, so we use a broad query per tier.
    query_by_tier = {
        1: "security advisory vulnerability patch release",
        2: "software release version update",
        3: "anthropic claude mcp announcement release",
        4: "research paper agent memory multi-agent",
    }
    query = query_by_tier.get(tier, "release update")
    try:
        result = client.news_search(
            query=query,
            top_k=MAX_ITEMS_PER_TIER,
            tier=tier,
            since=since_iso,
        )
        items = result.get("results", [])
        # Filter by stored_at >= since_iso in Python
        filtered = []
        for item in items:
            stored = item.get("metadata", {}).get("stored_at", "")
            if stored and stored >= since_iso:
                filtered.append(item)
        return filtered
    except Exception as e:
        logging.error(f"news_search failed for tier {tier}: {e}")
        return []


def fetch_head_items(client, head: str, since_iso: str, exclude_ids: set) -> tuple[list[dict], list[dict]]:
    """Items from HEAD_TIERS in the window that the head answered "yes" (flagged), or could not
    decide (maybe: stored with an empty answer). Each tier is searched with a security query, so a
    tier with more than 20 items in the window yields its 20 most security-like ones."""
    key = f"head:{head}"
    flagged, maybe, seen = [], [], set(exclude_ids)
    for tier in HEAD_TIERS:
        try:
            items = client.news_search(query=HEAD_QUERY, top_k=MAX_ITEMS_PER_TIER, tier=tier,
                                       since=since_iso).get("results", [])
        except Exception as e:
            logging.error(f"news_search failed for head items, tier {tier}: {e}")
            continue
        for item in items:
            meta = item.get("metadata", {})
            if item.get("id") in seen or meta.get("stored_at", "") < since_iso or key not in meta:
                continue
            seen.add(item.get("id"))
            if meta[key] == "yes":
                flagged.append(item)
            elif meta[key] == "":
                maybe.append(item)
    return flagged, maybe


def format_item(item: dict) -> str:
    """Single item as one markdown block."""
    content = item.get("content", "").strip()
    meta = item.get("metadata", {})
    source = meta.get("source", "?")
    url = meta.get("url", "")
    item_date = meta.get("item_date", "")
    keywords = meta.get("keywords", "")

    # First line of content is title; rest is summary
    lines = content.split("\n", 1)
    title = lines[0].strip()
    summary = lines[1].strip() if len(lines) > 1 else ""
    # Collapse long summaries to 2 lines for digest readability
    if summary:
        sumlines = [l.strip() for l in summary.splitlines() if l.strip()]
        summary = " ".join(sumlines[:2])
        if len(summary) > 320:
            summary = summary[:317] + "…"

    parts = [f"- **{title}** ({source})"]
    if summary:
        parts.append(f"  {summary}")
    footer_bits = []
    if item_date:
        footer_bits.append(item_date)
    if keywords:
        footer_bits.append(f"kw: {keywords}")
    if footer_bits:
        parts.append(f"  _{' · '.join(footer_bits)}_")
    if url:
        parts.append(f"  [{url}]({url})")
    return "\n".join(parts)


def build_digest_body(tiers_items: dict[int, list[dict]], since_iso: str, now_iso: str,
                      flagged: list[dict] = (), maybe: list[dict] = ()) -> tuple[str, int]:
    """Return (markdown_body, total_item_count). flagged / maybe: fetch_head_items' two lists."""
    total = sum(len(items) for items in tiers_items.values()) + len(flagged)
    today = datetime.now(timezone.utc).date().isoformat()

    lines = []
    lines.append(f"# News digest: {today}")
    lines.append("")
    lines.append(f"Window: items stored since {since_iso}")
    lines.append(f"Total items: {total}")
    lines.append("")

    if total == 0:
        lines.append("No new items in window. Quiet day.")
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append("*Pull more context anytime via `news_search(query, top_k, tier, since)`.*")
        return "\n".join(lines), 0

    for tier in (1, 2, 3, 4):
        items = tiers_items.get(tier, [])
        if tier == 1 and (flagged or maybe):
            lines.append(f"## {TIER_LABELS[1]} ({len(items)} items, + {len(flagged)} from other feeds)")
            lines.append("")
            for item in items:
                lines.append(format_item(item))
                lines.append("")
            if flagged:
                lines.append(f"### From other feeds (the `{DIGEST_HEAD}` head says yes)")
                lines.append("")
                for item in flagged:
                    lines.append(format_item(item))
                    lines.append("")
            if maybe:
                lines.append(f"### Maybe (the `{DIGEST_HEAD}` head could not decide; titles only)")
                lines.append("")
                for item in maybe[:MAX_MAYBE]:
                    title = item.get("content", "").strip().split("\n", 1)[0]
                    lines.append(f"- {title} ({item.get('metadata', {}).get('source', '?')})")
                if len(maybe) > MAX_MAYBE:
                    lines.append(f"- … and {len(maybe) - MAX_MAYBE} more")
                lines.append("")
            continue
        if not items:
            continue
        lines.append(f"## {TIER_LABELS[tier]} ({len(items)} items)")
        lines.append("")
        for item in items:
            lines.append(format_item(item))
            lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("*Use `news_search(query, top_k, tier, since)` to pull deeper on any topic.*")
    lines.append("*Drop a one-line reaction pointer in this mailbox if an item hits code another instance shipped.*")
    return "\n".join(lines), total


def main() -> int:
    setup_logging()
    logging.info("=" * 60)
    logging.info("newstron digest starting")

    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=WINDOW_HOURS)
    since_iso = since.isoformat()
    now_iso = now.isoformat()

    from memory_client import NewsClient
    client = NewsClient()

    tiers_items: dict[int, list[dict]] = {}
    for tier in (1, 2, 3, 4):
        tiers_items[tier] = fetch_tier(client, tier, since_iso)
        logging.info(f"tier {tier}: {len(tiers_items[tier])} items")

    flagged, maybe = [], []
    if DIGEST_HEAD:
        flagged, maybe = fetch_head_items(client, DIGEST_HEAD, since_iso,
                                          {i.get("id") for i in tiers_items.get(1, [])})
        logging.info(f"head {DIGEST_HEAD}: {len(flagged)} flagged, {len(maybe)} undecided from tiers {HEAD_TIERS}")
    body, count = build_digest_body(tiers_items, since_iso, now_iso, flagged, maybe)
    today = now.date().isoformat()
    subject = f"Daily digest — {today} ({count} items)"
    result = client.deliver(subject, body)
    if result.get("status") != "delivered":
        logging.error(f"digest delivery refused: {result}")
        return 1
    logging.info(f"digest delivered: {result.get('id')} -> {result.get('mailbox')}")
    logging.info(f"digest complete: {count} items total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
