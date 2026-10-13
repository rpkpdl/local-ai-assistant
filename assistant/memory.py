"""Pluggable memory backends for the assistant.

Any class implementing `MemoryStore` can be dropped in. Two are provided:
  - SQLiteMemoryStore: SQLite + FTS5 keyword search (default)
  - JSONMemoryStore:   a plain JSON file with naive substring search

Swap in your own (vector DB, embeddings, Postgres, ...) by implementing the
same four methods.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Memory:
    content: str
    tags: list[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    created_at: float = field(default_factory=time.time)

    def render(self) -> str:
        tags = f" [{', '.join(self.tags)}]" if self.tags else ""
        when = time.strftime("%Y-%m-%d", time.localtime(self.created_at))
        return f"({self.id}, {when}){tags} {self.content}"


class MemoryStore(ABC):
    @abstractmethod
    def add(self, content: str, tags: list[str] | None = None) -> Memory: ...

    @abstractmethod
    def search(self, query: str, limit: int = 5) -> list[Memory]: ...

    @abstractmethod
    def delete(self, memory_id: str) -> bool: ...

    @abstractmethod
    def all(self) -> list[Memory]: ...


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"\w+", text.lower()) if len(w) > 2]


class SQLiteMemoryStore(MemoryStore):
    def __init__(self, path: str | Path = "memories.db"):
        # check_same_thread=False: the web UI runs turns in a worker thread (one at a time)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS memories "
            "USING fts5(id UNINDEXED, content, tags, created_at UNINDEXED)"
        )

    def add(self, content, tags=None):
        m = Memory(content=content, tags=tags or [])
        self.db.execute(
            "INSERT INTO memories VALUES (?, ?, ?, ?)",
            (m.id, m.content, " ".join(m.tags), m.created_at),
        )
        self.db.commit()
        return m

    def search(self, query, limit=5):
        words = _words(query)
        if not words:
            return []
        # OR the words together, quoted so FTS5 syntax chars can't break the query
        fts = " OR ".join(f'"{w}"' for w in words)
        rows = self.db.execute(
            "SELECT id, content, tags, created_at FROM memories "
            "WHERE memories MATCH ? ORDER BY bm25(memories) LIMIT ?",
            (fts, limit),
        ).fetchall()
        return [self._row(r) for r in rows]

    def delete(self, memory_id):
        cur = self.db.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        self.db.commit()
        return cur.rowcount > 0

    def all(self):
        rows = self.db.execute(
            "SELECT id, content, tags, created_at FROM memories ORDER BY created_at"
        ).fetchall()
        return [self._row(r) for r in rows]

    @staticmethod
    def _row(r) -> Memory:
        return Memory(id=r[0], content=r[1], tags=r[2].split() if r[2] else [], created_at=r[3])


class JSONMemoryStore(MemoryStore):
    def __init__(self, path: str | Path = "memories.json"):
        self.path = Path(path)
        self.items: list[Memory] = (
            [Memory(**d) for d in json.loads(self.path.read_text())] if self.path.exists() else []
        )

    def _save(self):
        self.path.write_text(json.dumps([asdict(m) for m in self.items], indent=2))

    def add(self, content, tags=None):
        m = Memory(content=content, tags=tags or [])
        self.items.append(m)
        self._save()
        return m

    def search(self, query, limit=5):
        words = set(_words(query))
        scored = [
            (len(words & set(_words(m.content + " " + " ".join(m.tags)))), m) for m in self.items
        ]
        return [m for score, m in sorted(scored, key=lambda x: -x[0]) if score > 0][:limit]

    def delete(self, memory_id):
        before = len(self.items)
        self.items = [m for m in self.items if m.id != memory_id]
        self._save()
        return len(self.items) < before

    def all(self):
        return list(self.items)
