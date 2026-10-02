#!/usr/bin/env python3
"""
purger.py — Per-tier TTL purge for the news collection.

Asks the memory server's news API (/news/purge) to purge each tier with
the TTL appropriate for that tier. Designed to run on its own daily schedule, ~30 min after
the daily fetch so anything just-stored never gets caught in the
same-cycle prune (paranoid clock-skew defense).

TTL table:
  T1 security                12 days
  T2 infrastructure          12 days
  T3 experiment-relevant     12 days
  T4 academic AI/cog-sci     30 days
  T5 academic broader        30 days
  T6 general news            14 days
  T7 arts & culture          60 days
  T8 long-form / lifestyle   60 days
  T9 wildcard / weird        90 days

Usage:
  purger.py             # actual purge
  purger.py --dry-run   # report only, no deletions

Auth: memory_client reads MEMORY_URL and NEWSTRON_SECRET from the environment (the unit's
EnvironmentFile), as the fetcher does.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from memory_client import NewsClient

# Site path from the environment (the unit's environment file); generic default.
LOG_DIR = Path(os.environ.get("NEWSTRON_LOG_DIR", "/var/log/newstron"))

# Per-tier TTL in days. Keep in sync with the docstring above and the tier list in feeds.example.yaml.
TIER_TTL_DAYS = {
    1: 12,
    2: 12,
    3: 12,
    4: 30,
    5: 30,
    6: 14,
    7: 60,
    8: 60,
    9: 90,
}


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=LOG_DIR / "purger.log",
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    if sys.stderr.isatty():
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        logging.getLogger().addHandler(h)


def main() -> int:
    setup_logging()
    dry_run = "--dry-run" in sys.argv
    mode = "DRY RUN" if dry_run else "PURGE"
    logging.info("=" * 60)
    logging.info(f"newstron per-tier purger starting ({mode})")

    client = NewsClient()
    total_stale = 0
    total_purged = 0

    for tier, max_age in sorted(TIER_TTL_DAYS.items()):
        try:
            result = client.news_purge(max_age_days=max_age, dry_run=dry_run, tier=tier)
        except Exception as e:
            logging.error(f"tier {tier} purge failed: {e}")
            continue

        status = result.get("status", "?")
        if status == "empty":
            logging.info(f"tier {tier} (TTL {max_age}d): empty collection")
        elif status == "clean":
            logging.info(
                f"tier {tier} (TTL {max_age}d): clean — "
                f"{result.get('total', 0)} entries, none stale"
            )
        elif status == "dry_run":
            stale = result.get("stale", 0)
            total_stale += stale
            logging.info(
                f"tier {tier} (TTL {max_age}d): {stale}/"
                f"{result.get('total', 0)} would be purged"
            )
        elif status == "purged":
            deleted = result.get("deleted", 0)
            total_purged += deleted
            logging.info(
                f"tier {tier} (TTL {max_age}d): purged {deleted}, "
                f"{result.get('remaining', 0)} remain"
            )
        else:
            logging.warning(f"tier {tier} unexpected status: {result}")

    if dry_run:
        logging.info(
            f"DRY RUN complete: {total_stale} entries across all "
            f"tiers would be purged"
        )
    else:
        logging.info(
            f"purge complete: {total_purged} entries deleted across "
            f"all tiers"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
