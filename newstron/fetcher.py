#!/usr/bin/env python3
"""
fetcher.py — Fetch RSS/Atom feeds, filter, dedupe, store via news_store MCP.

Security notes:
- feedparser 6.0.12+ disables external entity resolution by default.
  We rely on that default; do NOT pass resolve_relative_uris=False or
  sanitize_html=False to feedparser.parse().
- Runs as an unprivileged service user. Writes $NEWSTRON_STATE_DIR/seen.json
  (dedup hashes) and logs to $NEWSTRON_LOG_DIR/fetcher.log. Both default to
  /var/lib/newstron and /var/log/newstron; a site overrides them in the
  environment file the units load.
- Does not handle GitHub token auth yet; when added, the token lives in that
  environment file (root:newstron 0640, docs/LAYOUT.md), never here.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import requests
import yaml

# Site paths come from the environment (the units' environment file); the defaults are
# generic FHS locations so a fresh install works without editing code.
STATE_DIR = Path(os.environ.get("NEWSTRON_STATE_DIR", "/var/lib/newstron"))
LOG_DIR = Path(os.environ.get("NEWSTRON_LOG_DIR", "/var/log/newstron"))
SEEN_FILE = STATE_DIR / "seen.json"
# The feed list is configuration, not code (docs/LAYOUT.md: config under /etc, never in the
# root-owned code tree). The release ships newstron/feeds.example.yaml; copy it here and edit.
FEEDS_FILE = Path(os.environ.get("NEWSTRON_FEEDS_FILE", "/etc/newstron/feeds.yaml"))

# Max items to ingest per feed per run (guard against massive feeds)
MAX_ITEMS_PER_FEED = 20
# Max total items per full run (all feeds)
MAX_ITEMS_PER_RUN = 500
# Item body truncation (embedding model context limit)
MAX_CONTENT_CHARS = 4000
# HTTP timeout per feed
FETCH_TIMEOUT_S = 15
# User-Agent identifying this tool: the project, not the person who runs it. A site sets its own
# with NEWSTRON_USER_AGENT; a feed entry's user_agent still wins for that feed.
USER_AGENT = os.environ.get("NEWSTRON_USER_AGENT") or "newstron/0.1 (+https://github.com/ASIXicle/Coterie)"


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=LOG_DIR / "fetcher.log",
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    # Also log to stdout when run interactively
    if sys.stderr.isatty():
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        logging.getLogger().addHandler(h)


def load_feeds() -> list[dict]:
    with open(FEEDS_FILE) as f:
        data = yaml.safe_load(f)
    return data.get("feeds", [])


def load_seen() -> set[str]:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text()))
        except Exception:
            logging.warning("seen.json corrupt, starting fresh")
    return set()


def save_seen(seen: set[str]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    # Atomic write: tmp + rename
    tmp = SEEN_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(sorted(seen)))
    tmp.replace(SEEN_FILE)


def item_hash(url: str, title: str, published: str) -> str:
    h = hashlib.sha256(f"{url}|{title}|{published}".encode()).hexdigest()[:16]
    return h


def keyword_match(text: str, keywords: list[str]) -> bool:
    """Case-insensitive substring match. Empty keywords list = always match."""
    if not keywords:
        return True
    lower = text.lower()
    return any(kw.lower() in lower for kw in keywords)


def truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def fetch_feed(feed: dict) -> list[dict]:
    """Fetch one feed and return list of dict items (empty on failure)."""
    name = feed["name"]
    url = feed["url"]
    logging.info(f"fetching: {name} ({url})")
    try:
        # feedparser.parse() handles ETag/Last-Modified itself but we use
        # requests for a tight timeout and explicit UA.
        # Allow per-feed UA override (e.g. for Reddit/Poetry Foundation
        # which reject non-browser UAs).
        ua = feed.get("user_agent") or USER_AGENT
        r = requests.get(
            url,
            headers={"User-Agent": ua},
            timeout=FETCH_TIMEOUT_S,
        )
        r.raise_for_status()
    except Exception as e:
        logging.error(f"fetch failed for {name}: {e}")
        return []

    # feedparser 6.0.12+ has XML external entity resolution disabled by
    # default. Do not override that default here.
    parsed = feedparser.parse(r.content)
    if parsed.bozo and not parsed.entries:
        logging.warning(
            f"parse issue for {name}: {parsed.bozo_exception}"
        )
        return []

    return list(parsed.entries[:MAX_ITEMS_PER_FEED])


def format_item_content(entry: dict, feed_name: str) -> tuple[str, str, str]:
    """Return (content_text, canonical_url, item_date_iso)."""
    title = entry.get("title", "(untitled)")
    link = entry.get("link", "")
    summary = entry.get("summary", "") or entry.get("description", "")
    # Strip HTML from summary if feedparser didn't already
    # (feedparser sets entry.summary to plain text when sanitize works)
    published = entry.get("published", "") or entry.get("updated", "")
    try:
        # feedparser gives us parsed_time for most feeds
        pt = entry.get("published_parsed") or entry.get("updated_parsed")
        if pt:
            item_date = datetime(*pt[:6], tzinfo=timezone.utc).date().isoformat()
        else:
            item_date = ""
    except Exception:
        item_date = ""

    body_parts = [title]
    if summary:
        body_parts.append(summary)
    content = "\n\n".join(body_parts)
    content = truncate(content, MAX_CONTENT_CHARS)
    return content, link, item_date


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="newstron feed fetcher")
    parser.add_argument("--tier", type=int, default=0,
                       help="Only fetch feeds of this tier (0 = all)")
    args = parser.parse_args()

    setup_logging()
    logging.info("=" * 60)
    logging.info(f"newstron fetcher starting (tier filter: {args.tier or 'all'})")

    feeds = load_feeds()
    logging.info(f"{len(feeds)} feeds listed in {FEEDS_FILE}")
    if args.tier:
        feeds = [f for f in feeds if f.get("tier") == args.tier]
        logging.info(f"tier {args.tier} filter: {len(feeds)} feeds selected")
    seen = load_seen()
    new_items_total = 0
    store_failures = 0

    # Import here so --help works without a configured environment
    from memory_client import NewsClient

    client = NewsClient()

    for feed in feeds:
        if new_items_total >= MAX_ITEMS_PER_RUN:
            logging.warning(f"hit MAX_ITEMS_PER_RUN ({MAX_ITEMS_PER_RUN}), stopping")
            break

        name = feed["name"]
        tier = feed["tier"]
        keywords = feed.get("keywords", []) or []

        entries = fetch_feed(feed)
        stored_this_feed = 0
        for entry in entries:
            if new_items_total >= MAX_ITEMS_PER_RUN:
                break
            title = entry.get("title", "")
            link = entry.get("link", "")
            published = entry.get("published", "") or entry.get("updated", "")
            h = item_hash(link, title, published)
            if h in seen:
                continue

            content, url, item_date = format_item_content(entry, name)
            text_for_match = f"{title}\n{entry.get('summary', '')}"
            if not keyword_match(text_for_match, keywords):
                # Mark seen even on keyword miss to avoid re-checking.
                # Trade-off: if keywords expand later, previously-seen items
                # won't be reconsidered. Acceptable — new items will match.
                seen.add(h)
                continue

            matched_kws = [kw for kw in keywords if kw.lower() in text_for_match.lower()]

            try:
                result = client.news_store(
                    content=content,
                    url=url,
                    tier=tier,
                    source=name,
                    keywords=",".join(matched_kws),
                    item_date=item_date,
                )
                if result.get("status") == "stored":
                    seen.add(h)
                    stored_this_feed += 1
                    new_items_total += 1
                    logging.info(f"stored: {name} :: {title[:80]}")
                else:
                    store_failures += 1
                    logging.error(f"news_store rejected: {result}")
            except Exception as e:
                store_failures += 1
                logging.error(f"news_store failed for {name}: {e}")

        logging.info(f"feed done: {name} ({stored_this_feed} stored)")

    save_seen(seen)
    logging.info(f"fetcher complete: {new_items_total} new items")
    # news_store has no benign refusal (duplicates never reach it: `seen` filters them), so any
    # failure means the server, its auth or a feed's config is broken. Exit non-zero so systemd
    # records a failed run; the failed items are not in `seen`, so the next run retries them.
    if store_failures:
        logging.error(f"fetcher: {store_failures} news_store call(s) failed; exiting 1")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
