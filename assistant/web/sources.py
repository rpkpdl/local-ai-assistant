"""Cache of known-good URLs ("sources") for recurring tasks.

A source is a URL template like
  https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current=temperature_2m
plus notes on how to fill it in. Success/failure counts are tracked; sources
with 3 consecutive failures are hidden until they're saved again.
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass

MAX_CONSECUTIVE_FAILURES = 3


@dataclass
class Source:
    id: int
    task: str
    url_template: str
    notes: str
    successes: int
    failures: int
    consecutive_failures: int
    last_ok: float | None
    check: str | None = None

    def render(self) -> str:
        last = time.strftime("%Y-%m-%d", time.localtime(self.last_ok)) if self.last_ok else "never"
        return (f"[source {self.id}] task: {self.task}\n  url: {self.url_template}\n"
                f"  notes: {self.notes}\n  ok {self.successes} / failed {self.failures}, "
                f"last ok {last}" + (f"\n  check: {self.check}" if self.check else ""))


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"\w+", text.lower()) if len(w) > 2}


class SourceStore:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        db.execute("""
            CREATE TABLE IF NOT EXISTS sources (
                id INTEGER PRIMARY KEY, task TEXT, url_template TEXT UNIQUE, notes TEXT,
                successes INTEGER DEFAULT 0, failures INTEGER DEFAULT 0,
                consecutive_failures INTEGER DEFAULT 0, last_ok REAL, created_at REAL,
                check_reason TEXT)
        """)
        cols = {r[1] for r in db.execute("PRAGMA table_info(sources)")}
        if "check_reason" not in cols:  # databases created before the source check existed
            db.execute("ALTER TABLE sources ADD COLUMN check_reason TEXT")

    def find(self, task: str, limit: int = 3) -> list[Source]:
        words = _words(task)
        rows = self.db.execute(
            "SELECT id, task, url_template, notes, successes, failures, consecutive_failures, "
            "last_ok, check_reason FROM sources WHERE consecutive_failures < ?", (MAX_CONSECUTIVE_FAILURES,)
        ).fetchall()
        scored = []
        for r in rows:
            score = len(words & _words(r[1] + " " + r[3]))
            if score:
                scored.append((score, r[4] - r[5], Source(*r)))
        scored.sort(key=lambda x: (-x[0], -x[1]))
        return [s for *_, s in scored[:limit]]

    def save(self, task: str, url_template: str, notes: str, check: str | None = None) -> int:
        cur = self.db.execute(
            "INSERT INTO sources (task, url_template, notes, successes, last_ok, created_at, "
            "check_reason) VALUES (?, ?, ?, 1, ?, ?, ?) "
            "ON CONFLICT(url_template) DO UPDATE SET task = excluded.task, "
            "notes = excluded.notes, check_reason = excluded.check_reason, "
            "consecutive_failures = 0 RETURNING id",
            (task, url_template, notes, time.time(), time.time(), check),
        )
        source_id = cur.fetchone()[0]
        self.db.commit()
        return source_id

    def delete(self, source_id: int) -> bool:
        cur = self.db.execute("DELETE FROM sources WHERE id = ?", (source_id,))
        self.db.commit()
        return cur.rowcount > 0

    def record(self, source_id: int, ok: bool) -> None:
        if ok:
            self.db.execute("UPDATE sources SET successes = successes + 1, "
                            "consecutive_failures = 0, last_ok = ? WHERE id = ?",
                            (time.time(), source_id))
        else:
            self.db.execute("UPDATE sources SET failures = failures + 1, "
                            "consecutive_failures = consecutive_failures + 1 WHERE id = ?",
                            (source_id,))
        self.db.commit()

    def all(self) -> list[Source]:
        rows = self.db.execute(
            "SELECT id, task, url_template, notes, successes, failures, consecutive_failures, "
            "last_ok, check_reason FROM sources ORDER BY id"
        ).fetchall()
        return [Source(*r) for r in rows]
