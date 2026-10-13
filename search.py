"""Pluggable web search backends.

Implement `SearchProvider.search` to add another backend (SearXNG, Google, ...).
"""

from __future__ import annotations

import html
import os
import re
import sqlite3
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str


class SearchProvider(ABC):
    @abstractmethod
    def search(self, query: str, count: int = 5) -> list[SearchResult]: ...


class QuotaExceeded(Exception):
    pass


class BraveSearch(SearchProvider):
    """Brave Search API. Counts queries per calendar month in SQLite and refuses
    to go over `monthly_limit` (default 1000 = the $5 monthly free credit)."""

    URL = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, db: sqlite3.Connection, api_key: str | None = None,
                 monthly_limit: int | None = None):
        self.api_key = api_key or os.environ.get("BRAVE_API_KEY")
        if not self.api_key:
            raise RuntimeError("BRAVE_API_KEY is not set (add it to .env)")
        self.monthly_limit = monthly_limit or int(os.environ.get("BRAVE_MONTHLY_LIMIT", "1000"))
        self.db = db
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS search_usage (month TEXT PRIMARY KEY, count INTEGER)"
        )

    def used_this_month(self) -> int:
        row = self.db.execute(
            "SELECT count FROM search_usage WHERE month = ?", (time.strftime("%Y-%m"),)
        ).fetchone()
        return row[0] if row else 0

    def search(self, query, count=5):
        if self.used_this_month() >= self.monthly_limit:
            raise QuotaExceeded(f"Monthly search limit of {self.monthly_limit} reached")
        r = httpx.get(
            self.URL,
            params={"q": query, "count": max(1, min(count, 20))},
            headers={"Accept": "application/json", "X-Subscription-Token": self.api_key},
            timeout=15,
        )
        # Count every request Brave received, even failed ones
        self.db.execute(
            "INSERT INTO search_usage VALUES (?, 1) "
            "ON CONFLICT(month) DO UPDATE SET count = count + 1",
            (time.strftime("%Y-%m"),),
        )
        self.db.commit()
        r.raise_for_status()
        results = r.json().get("web", {}).get("results", [])
        return [
            SearchResult(title=_clean(x.get("title", "")), url=x.get("url", ""),
                         snippet=_clean(x.get("description", "")))
            for x in results
        ]


def _clean(text: str) -> str:
    """Strip Brave's highlight tags (<strong>) and decode entities like &amp;."""
    return html.unescape(re.sub(r"<[^>]+>", "", text))
