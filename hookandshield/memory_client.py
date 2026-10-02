"""Client for the memory server's news API: the feed fetcher's whole interface to it.

Until 2026-09-25 this was an MCP client on the server's full MCP URL. That URL carries the
server's secret path segment and so opens every tool (shell_exec, file_write, amq_send,
bootstrap_update) to the process that parses internet content. The news API is four routes
gated by NEWSTRON_SECRET as a bearer token: store, search, purge, and deliver (to the `news`
mailbox, fixed server-side). hook-detect and pip-audit-scan send their alerts through `store`.

Configuration comes from the process environment only; every unit loads it from its
environment file. For a manual run: `set -a; . <env file>; set +a`.
  MEMORY_URL       the server's base URL (default http://127.0.0.1:8765)
  NEWSTRON_SECRET  the bearer token (required)
Stdlib only, so it runs on the system python as well as a venv.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Optional


class NewsClient:
    def __init__(self, base_url: Optional[str] = None, secret: Optional[str] = None,
                 timeout: int = 30) -> None:
        self.base = (base_url or os.environ.get("MEMORY_URL") or "http://127.0.0.1:8765").rstrip("/")
        self.secret = secret if secret is not None else os.environ.get("NEWSTRON_SECRET", "")
        if not self.secret:
            raise SystemExit("ERROR: NEWSTRON_SECRET is not set in the environment")
        self.timeout = timeout

    def _post(self, op: str, payload: dict[str, Any]) -> dict[str, Any]:
        req = urllib.request.Request(
            f"{self.base}/news/{op}", data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.secret}"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            # The secret travels in a header, never the URL, so no error text can carry it.
            detail = e.read()[:300].decode("utf-8", errors="replace")
            raise RuntimeError(f"news API {op}: HTTP {e.code} {detail}") from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise RuntimeError(f"news API {op}: {e.__class__.__name__}: {getattr(e, 'reason', e)}") from None

    def news_store(self, content: str, url: str, tier: int, source: str,
                   keywords: str = "", item_date: str = "") -> dict[str, Any]:
        return self._post("store", {"content": content, "url": url, "tier": tier, "source": source,
                                    "keywords": keywords, "item_date": item_date})

    def news_search(self, query: str, top_k: int = 5, tier: Optional[int] = None,
                    since: Optional[str] = None) -> dict[str, Any]:
        return self._post("search", {"query": query, "top_k": top_k, "tier": tier, "since": since})

    def news_purge(self, max_age_days: int = 12, dry_run: bool = True,
                   tier: Optional[int] = None) -> dict[str, Any]:
        return self._post("purge", {"max_age_days": max_age_days, "dry_run": dry_run, "tier": tier})

    def deliver(self, subject: str, body: str) -> dict[str, Any]:
        """Deliver a digest to the `news` mailbox. The server picks the mailbox."""
        return self._post("deliver", {"subject": subject, "body": body})


if __name__ == "__main__":
    print(json.dumps(NewsClient().news_search("test", top_k=1), indent=2))
