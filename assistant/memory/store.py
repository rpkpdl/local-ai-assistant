"""SQLite memory store: entities, memories and relationships in one file. No LLM in here.

The librarian (librarian.py) decides what to store and how important it is; this module only
writes the rows and reads them back:
  - search():    direct recall. FTS5 keyword match ranked by bm25 x salience x recency.
  - profile():   core facts (user facts with an attribute, relationships, tracked series),
                 always put in the agent's context so they don't depend on a search.
  - associate(): associative recall. Lived experiences whose themes or keywords match the
                 current message, passed through a plain-code gate.

Old databases (the single FTS5 `memories` table) are migrated on open; the old table is kept
as `memories_v1`.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

KINDS = ("fact", "event", "series_point", "lesson")
PROVENANCES = ("stated", "observed", "inferred")
MODES = ("task", "personal")
ENTITY_TYPES = ("user", "person", "pet", "place", "org", "thing")

# Associative recall gate
SURFACE_MIN_SALIENCE = 0.75
SURFACE_COOLDOWN_DAYS = 2
PROFILE_LIMIT = 25

SCHEMA = """
CREATE TABLE IF NOT EXISTS entities (
    id         INTEGER PRIMARY KEY,
    type       TEXT NOT NULL,
    name       TEXT NOT NULL,
    aliases    TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL,
    UNIQUE(type, name)
);
CREATE TABLE IF NOT EXISTS memories (
    id               INTEGER PRIMARY KEY,
    entity_id        INTEGER NOT NULL REFERENCES entities(id),
    kind             TEXT NOT NULL,
    attribute        TEXT,
    content          TEXT NOT NULL,
    value            TEXT,
    theme            TEXT NOT NULL DEFAULT '',
    keywords         TEXT NOT NULL DEFAULT '',
    salience         REAL NOT NULL DEFAULT 0.5,
    provenance       TEXT NOT NULL DEFAULT 'stated',
    mode             TEXT NOT NULL DEFAULT 'task',
    occurred_at      TEXT,
    recorded_at      REAL NOT NULL,
    last_seen_at     REAL,
    last_surfaced_at REAL,
    hit_count        INTEGER NOT NULL DEFAULT 0,
    superseded_by    INTEGER REFERENCES memories(id),
    supersede_reason TEXT
);
CREATE TABLE IF NOT EXISTS relationships (
    id            INTEGER PRIMARY KEY,
    entity_a      INTEGER NOT NULL REFERENCES entities(id),
    entity_b      INTEGER NOT NULL REFERENCES entities(id),
    link_type     TEXT NOT NULL,
    salience      REAL NOT NULL DEFAULT 0.5,
    provenance    TEXT NOT NULL DEFAULT 'stated',
    recorded_at   REAL NOT NULL,
    superseded_by INTEGER REFERENCES relationships(id)
);
CREATE TABLE IF NOT EXISTS memory_meta (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS idx_mem_series ON memories(entity_id, attribute, occurred_at);
CREATE INDEX IF NOT EXISTS idx_mem_sal ON memories(salience);

-- Full-text index over the searchable text, kept in sync by the triggers below.
-- porter: "drinks" matches "drink", "flights" matches "flight".
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content, theme, keywords, attribute, entity, tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content, theme, keywords, attribute, entity)
    VALUES (new.id, new.content, new.theme, new.keywords, coalesce(new.attribute, ''),
            (SELECT name FROM entities WHERE id = new.entity_id));
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    DELETE FROM memories_fts WHERE rowid = old.id;
END;
CREATE TRIGGER IF NOT EXISTS memories_au
AFTER UPDATE OF content, theme, keywords, attribute, entity_id ON memories BEGIN
    DELETE FROM memories_fts WHERE rowid = old.id;
    INSERT INTO memories_fts(rowid, content, theme, keywords, attribute, entity)
    VALUES (new.id, new.content, new.theme, new.keywords, coalesce(new.attribute, ''),
            (SELECT name FROM entities WHERE id = new.entity_id));
END;
"""

# Words that carry no meaning for matching. Most questions start with one of these.
STOPWORDS = set("""
about after again all also and any are around because been before being but can could did does
doing don't for from get going got had has have having her here hers him his how i'm into its it's
just let let's like make many more most much need not now off once only other our out over own
should some such than that the their them then there these they this those too under until very
was way were what what's when where which while who why will with would you your yours i've i'll
""".split())

def words(text: str) -> list[str]:
    """Meaningful lowercase words (3+ characters, not a stopword), in order, deduplicated."""
    out = []
    text = re.sub(r"['’]s\b", "", text.lower())  # "Biscuit's" -> "biscuit"
    for w in re.findall(r"[\w']+", text.replace("’", "'")):
        w = w.strip("'")
        if len(w) > 2 and w not in STOPWORDS and w not in out:
            out.append(w)
    return out


def fts_query(text: str) -> str | None:
    # OR the words together, each quoted so FTS5 syntax characters can't break the query
    return " OR ".join(f'"{w}"' for w in words(text)) or None


@dataclass
class Memory:
    id: int
    entity: str
    kind: str
    content: str
    attribute: str | None = None
    value: str | None = None
    theme: str = ""
    keywords: str = ""
    salience: float = 0.5
    provenance: str = "stated"
    mode: str = "task"
    occurred_at: str | None = None
    recorded_at: float = 0.0
    last_seen_at: float | None = None
    last_surfaced_at: float | None = None
    hit_count: int = 0
    superseded_by: int | None = None
    supersede_reason: str | None = None
    entity_type: str = "user"

    def render(self) -> str:
        """One line for the model: id, date, kind/attribute, who it's about, the content."""
        when = self.occurred_at or time.strftime("%Y-%m-%d", time.localtime(self.recorded_at))
        tag = self.kind + (f":{self.attribute}" if self.attribute else "")
        who = "" if self.entity_type == "user" else f" [{self.entity}]"
        val = f" = {self.value}" if self.value and self.value not in self.content else ""
        return f"(m{self.id}, {when}, {tag}){who} {self.content}{val}"

    def to_dict(self) -> dict:
        return dict(vars(self))


@dataclass
class Relationship:
    id: int
    entity: str  # entity_b, e.g. "Maya"
    of: str  # entity_a, usually the user
    link_type: str  # "<entity> is <of>'s <link_type>", e.g. sister
    salience: float
    provenance: str
    recorded_at: float
    entity_type: str = "person"

    def render(self) -> str:
        of = "user" if self.of == "user" else self.of
        return f"(r{self.id}) {self.entity} is {of}'s {self.link_type.replace('_', ' ')}"


_MEM_COLS = ("m.id, e.name, m.kind, m.content, m.attribute, m.value, m.theme, m.keywords, "
             "m.salience, m.provenance, m.mode, m.occurred_at, m.recorded_at, m.last_seen_at, "
             "m.last_surfaced_at, m.hit_count, m.superseded_by, m.supersede_reason, e.type")


class MemoryStore:
    def __init__(self, path: str | Path = "memories.db"):
        # check_same_thread=False: turns run on a worker thread (one at a time)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA foreign_keys = ON")
        if self._is_legacy():
            self.db.execute("ALTER TABLE memories RENAME TO memories_v1")
        self.db.executescript(SCHEMA)
        self.user_id = self.entity("user", "user")
        if self._has_table("memories_v1") and self.meta("v1_migrated") is None:
            self._migrate_v1()  # one transaction with the flag, so it can't half-happen
        self.db.commit()

    def meta(self, key: str, value: str | None = None) -> str | None:
        """Read a small persistent setting, or write it when value is given."""
        if value is not None:
            self.db.execute("INSERT OR REPLACE INTO memory_meta VALUES (?, ?)", (key, value))
            self.db.commit()
            return value
        row = self.db.execute("SELECT value FROM memory_meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    # --- entities ------------------------------------------------------------

    def entity(self, type: str, name: str) -> int:
        """Id of the entity with this name (or alias), creating it if it's new."""
        type = type if type in ENTITY_TYPES else "thing"
        if type == "user":
            name = "user"
        name = name.strip() or "unknown"
        rows = self.db.execute(
            "SELECT id, type FROM entities WHERE lower(name) = lower(?) "
            "OR EXISTS (SELECT 1 FROM json_each(aliases) WHERE lower(value) = lower(?))",
            (name, name)).fetchall()
        if rows:
            return min(rows, key=lambda r: r[1] != type)[0]  # prefer the same type
        cur = self.db.execute("INSERT INTO entities (type, name, created_at) VALUES (?, ?, ?)",
                              (type, name, time.time()))
        return cur.lastrowid

    def add_alias(self, entity_id: int, alias: str) -> None:
        (aliases,) = self.db.execute("SELECT aliases FROM entities WHERE id = ?",
                                     (entity_id,)).fetchone()
        names = json.loads(aliases)
        if alias not in names:
            self.db.execute("UPDATE entities SET aliases = ? WHERE id = ?",
                            (json.dumps(names + [alias]), entity_id))
            self.db.commit()

    def entities(self) -> list[tuple[str, str]]:
        """(type, name) of every entity except the user."""
        return self.db.execute("SELECT type, name FROM entities WHERE type != 'user' "
                               "ORDER BY created_at DESC LIMIT 100").fetchall()

    # --- writes --------------------------------------------------------------

    def add(self, content: str, kind: str = "fact", *, entity: str = "user",
            entity_type: str = "user", attribute: str | None = None, value: str | None = None,
            theme: list[str] | str = "", keywords: list[str] | str = "", salience: float = 0.6,
            provenance: str = "stated", mode: str = "task", occurred_at: str | None = None,
            supersedes: int | None = None, supersede_reason: str | None = None) -> Memory:
        if kind not in KINDS:
            raise ValueError(f"unknown kind {kind!r}")
        provenance = provenance if provenance in PROVENANCES else "inferred"
        mode = mode if mode in MODES else "task"
        salience = min(max(float(salience), 0.0), 1.0)
        if provenance == "inferred":
            salience = min(salience, 0.4)  # inferred never outranks lived experience
        eid = self.user_id if entity_type == "user" else self.entity(entity_type, entity)
        cur = self.db.execute(
            "INSERT INTO memories (entity_id, kind, attribute, content, value, theme, keywords, "
            "salience, provenance, mode, occurred_at, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (eid, kind, _norm_attr(attribute), content.strip(), value, _join(theme),
             _join(keywords), salience, provenance, mode, occurred_at, time.time()))
        mid = cur.lastrowid
        if supersedes is not None:
            self.supersede(supersedes, mid, supersede_reason, commit=False)
        self.db.commit()
        return self.get(mid)

    def supersede(self, old_id: int, new_id: int, reason: str | None = None,
                  commit: bool = True) -> bool:
        """Mark old_id as replaced by new_id. The old row is kept as history."""
        cur = self.db.execute(
            "UPDATE memories SET superseded_by = ?, supersede_reason = ? "
            "WHERE id = ? AND id != ? AND superseded_by IS NULL", (new_id, reason, old_id, new_id))
        if commit:
            self.db.commit()
        return cur.rowcount > 0

    def relate(self, entity: str, entity_type: str, link_type: str, *, of: str = "user",
               of_type: str = "user", salience: float = 0.7,
               provenance: str = "stated") -> Relationship:
        a = self.user_id if of_type == "user" else self.entity(of_type, of)
        b = self.entity(entity_type, entity)
        link = re.sub(r"\W+", "_", link_type.strip().lower()).strip("_") or "related"
        existing = self.db.execute(
            "SELECT id FROM relationships WHERE entity_a = ? AND entity_b = ? AND link_type = ? "
            "AND superseded_by IS NULL", (a, b, link)).fetchone()
        if existing:
            rid = existing[0]
        else:
            rid = self.db.execute(
                "INSERT INTO relationships (entity_a, entity_b, link_type, salience, provenance, "
                "recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
                (a, b, link, min(max(float(salience), 0.0), 1.0),
                 provenance if provenance in PROVENANCES else "inferred", time.time())).lastrowid
        self.db.commit()
        return next(r for r in self.relationships() if r.id == rid)

    def update(self, memory_id: int, **fields) -> None:
        """Set columns on one memory (used by enrich and the consolidator)."""
        allowed = {"kind", "attribute", "value", "theme", "keywords", "salience", "provenance",
                   "mode", "occurred_at", "entity_id"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if "theme" in fields:
            fields["theme"] = _join(fields["theme"])
        if "keywords" in fields:
            fields["keywords"] = _join(fields["keywords"])
        if "attribute" in fields:
            fields["attribute"] = _norm_attr(fields["attribute"])
        if fields:
            sets = ", ".join(f"{k} = ?" for k in fields)
            self.db.execute(f"UPDATE memories SET {sets} WHERE id = ?",
                            (*fields.values(), memory_id))
            self.db.commit()

    def delete(self, memory_id: int | str) -> bool:
        """Hard delete (the user asked to forget). Accepts 12, "m12" or "r3"."""
        ref = str(memory_id).strip().lower()
        if ref.startswith("r") and ref[1:].isdigit():
            cur = self.db.execute("DELETE FROM relationships WHERE id = ?", (int(ref[1:]),))
            self.db.commit()
            return cur.rowcount > 0
        ref = ref.removeprefix("m").removeprefix("#")
        if not ref.isdigit():
            return False
        # Forgetting removes the memory and the older versions it replaced: the user wants
        # it gone, not an outdated version brought back.
        ids, frontier = [], [int(ref)]
        while frontier:
            ids += frontier
            marks = ",".join("?" * len(frontier))
            frontier = [r[0] for r in self.db.execute(
                f"SELECT id FROM memories WHERE superseded_by IN ({marks})", frontier)]
        marks = ",".join("?" * len(ids))
        self.db.execute(f"UPDATE memories SET superseded_by = NULL WHERE id IN ({marks})", ids)
        cur = self.db.execute(f"DELETE FROM memories WHERE id IN ({marks})", ids)
        self.db.commit()
        return cur.rowcount > 0

    # --- reads ---------------------------------------------------------------

    def get(self, memory_id: int) -> Memory | None:
        row = self.db.execute(f"SELECT {_MEM_COLS} FROM memories m JOIN entities e "
                              "ON e.id = m.entity_id WHERE m.id = ?", (memory_id,)).fetchone()
        return Memory(*row) if row else None

    def all(self, include_superseded: bool = False) -> list[Memory]:
        where = "" if include_superseded else "WHERE m.superseded_by IS NULL"
        rows = self.db.execute(f"SELECT {_MEM_COLS} FROM memories m JOIN entities e "
                               f"ON e.id = m.entity_id {where} ORDER BY m.recorded_at, m.id")
        return [Memory(*r) for r in rows]

    def relationships(self) -> list[Relationship]:
        rows = self.db.execute(
            "SELECT r.id, b.name, a.name, r.link_type, r.salience, r.provenance, r.recorded_at, "
            "b.type FROM relationships r JOIN entities a ON a.id = r.entity_a "
            "JOIN entities b ON b.id = r.entity_b WHERE r.superseded_by IS NULL "
            "ORDER BY r.salience DESC, r.recorded_at")
        return [Relationship(*r) for r in rows]

    def series(self, attribute: str, entity_id: int | None = None) -> list[Memory]:
        """Every current point of one series, newest first."""
        rows = self.db.execute(
            f"SELECT {_MEM_COLS} FROM memories m JOIN entities e ON e.id = m.entity_id "
            "WHERE m.kind = 'series_point' AND m.attribute = ? AND m.entity_id = ? "
            "AND m.superseded_by IS NULL ORDER BY coalesce(m.occurred_at, ''), m.recorded_at",
            (_norm_attr(attribute), entity_id or self.user_id))
        return [Memory(*r) for r in rows][::-1]

    def search(self, query: str, limit: int = 5, touch: bool = True) -> list[Memory]:
        """Direct recall: keyword match over content, themes, keywords, attribute and entity.

        Ranked by bm25 x salience x recency. Superseded rows never show. When a series point
        matches, the series is returned newest first in its place.
        """
        q = fts_query(query)
        if not q:
            return []
        rows = self.db.execute(
            f"SELECT {_MEM_COLS}, bm25(memories_fts, 1.0, 0.8, 0.5, 1.0, 1.0) FROM memories_fts "
            "JOIN memories m ON m.id = memories_fts.rowid JOIN entities e ON e.id = m.entity_id "
            "WHERE memories_fts MATCH ? AND m.superseded_by IS NULL "
            "ORDER BY bm25(memories_fts, 1.0, 0.8, 0.5, 1.0, 1.0) LIMIT 50", (q,)).fetchall()
        now = time.time()

        def score(r) -> float:
            m, bm = Memory(*r[:-1]), r[-1]
            age_days = (now - (m.last_seen_at or m.recorded_at)) / 86400
            return -bm * (0.5 + m.salience) / (1 + age_days / 365)

        ranked = [Memory(*r[:-1]) for r in sorted(rows, key=score, reverse=True)]
        out, seen_series = [], set()
        for m in ranked:
            if m.kind == "series_point" and m.attribute:
                key = (m.entity, m.attribute)
                if key in seen_series:
                    continue
                seen_series.add(key)
                out += self.series(m.attribute, self._entity_id(m.id))
            else:
                out.append(m)
        out = out[:limit]
        if touch:
            self.touch([m.id for m in out])
        return out

    def profile(self, limit: int = PROFILE_LIMIT) -> str:
        """Core facts for the agent's context: always present, so no search is needed."""
        facts = self.db.execute(
            f"SELECT {_MEM_COLS} FROM memories m JOIN entities e ON e.id = m.entity_id "
            "WHERE m.entity_id = ? AND m.kind = 'fact' AND m.attribute IS NOT NULL "
            "AND m.superseded_by IS NULL ORDER BY m.salience DESC, m.recorded_at DESC LIMIT ?",
            (self.user_id, limit)).fetchall()
        lines = [Memory(*r).render() for r in facts]
        rels = [r.render() for r in self.relationships()[:limit]]
        series = self.db.execute(
            "SELECT attribute, count(*) FROM memories WHERE kind = 'series_point' "
            "AND entity_id = ? AND superseded_by IS NULL GROUP BY attribute ORDER BY attribute",
            (self.user_id,)).fetchall()
        tracked = []
        for attr, n in series:
            latest = self.series(attr)[0]
            when = latest.occurred_at or time.strftime("%Y-%m-%d",
                                                       time.localtime(latest.recorded_at))
            tracked.append(f"{attr}: latest {latest.value or latest.content} ({when}), "
                           f"{n} point{'s' if n > 1 else ''}")
        parts = []
        if lines:
            parts.append("Facts:\n" + "\n".join(lines))
        if rels:
            parts.append("People and things:\n" + "\n".join(rels))
        if tracked:
            parts.append("Tracked over time (use recall for the history):\n" + "\n".join(tracked))
        return "\n\n".join(parts)

    def associate(self, text: str, limit: int = 3, mark: bool = True,
                  now: float | None = None) -> list[Memory]:
        """Associative recall: lived experiences that match the current message.

        Matches only theme and keywords, the retrieval keys the librarian wrote for this
        purpose; incidental words in the content ("go back") would surface noise. Plain-code
        gate; the agent makes the final call on whether to bring one up.
        Passes only if: lesson or event (or a fact without an attribute, i.e. not already in
        the profile), salience >= SURFACE_MIN_SALIENCE, stated or observed, current, and not
        surfaced in the last SURFACE_COOLDOWN_DAYS.
        """
        q = fts_query(text)
        if not q:
            return []
        q = f"{{theme keywords}} : ({q})"
        now = now or time.time()
        cutoff = now - SURFACE_COOLDOWN_DAYS * 86400
        rows = self.db.execute(
            f"SELECT {_MEM_COLS} FROM memories_fts JOIN memories m ON m.id = memories_fts.rowid "
            "JOIN entities e ON e.id = m.entity_id WHERE memories_fts MATCH ? "
            "AND m.superseded_by IS NULL AND m.salience >= ? "
            "AND m.provenance IN ('stated', 'observed') "
            "AND (m.kind IN ('lesson', 'event') OR (m.kind = 'fact' AND m.attribute IS NULL)) "
            "AND (m.last_surfaced_at IS NULL OR m.last_surfaced_at < ?) "
            "ORDER BY bm25(memories_fts, 1.0, 1.0, 0.7, 1.0, 1.0) LIMIT ?",
            (q, SURFACE_MIN_SALIENCE, cutoff, limit)).fetchall()
        hits = [Memory(*r) for r in rows]
        if mark and hits:
            self.db.executemany("UPDATE memories SET last_surfaced_at = ? WHERE id = ?",
                                [(now, m.id) for m in hits])
            self.touch([m.id for m in hits])
        return hits

    def touch(self, ids: list[int]) -> None:
        """Record that these memories were recalled (feeds the consolidator)."""
        if ids:
            self.db.executemany("UPDATE memories SET last_seen_at = ?, hit_count = hit_count + 1 "
                                "WHERE id = ?", [(time.time(), i) for i in ids])
            self.db.commit()

    # --- internals -----------------------------------------------------------

    def _entity_id(self, memory_id: int) -> int:
        return self.db.execute("SELECT entity_id FROM memories WHERE id = ?",
                               (memory_id,)).fetchone()[0]

    def _has_table(self, name: str) -> bool:
        return self.db.execute("SELECT 1 FROM sqlite_master WHERE name = ?",
                               (name,)).fetchone() is not None

    def _is_legacy(self) -> bool:
        row = self.db.execute("SELECT sql FROM sqlite_master WHERE name = 'memories'").fetchone()
        return bool(row) and "fts5" in row[0].lower()

    def _migrate_v1(self) -> None:
        """Copy rows from the old FTS5 table. They become plain user facts; enrich adds the rest."""
        rows = self.db.execute(
            "SELECT content, tags, created_at FROM memories_v1 ORDER BY created_at").fetchall()
        self.db.executemany(
            "INSERT INTO memories (entity_id, kind, content, theme, salience, provenance, "
            "recorded_at) VALUES (?, 'fact', ?, ?, 0.6, 'stated', ?)",
            [(self.user_id, c, ",".join((t or "").split()), float(ts)) for c, t, ts in rows])
        self.db.execute("INSERT OR REPLACE INTO memory_meta VALUES ('v1_migrated', ?)",
                        (str(len(rows)),))


def _join(v: list[str] | str | None) -> str:
    if not v:
        return ""
    items = v.split(",") if isinstance(v, str) else v
    return ",".join(dict.fromkeys(s.strip().lower() for s in items if s and s.strip()))


def _norm_attr(a: str | None) -> str | None:
    if not a:
        return None
    a = re.sub(r"\W+", "_", a.strip().lower()).strip("_")
    return a or None
