"""Background upkeep for memory salience. Plain code, no LLM.

  python -m assistant.memory.consolidate            run now (reinforce + decay)
  python -m assistant.memory.consolidate --enrich   also add keywords/attributes to plain
                                                    memories (calls Claude)

- Reinforce: memories recalled since the last run gain salience.
- Decay: memories not recalled for IDLE_DAYS slowly lose salience, down to a floor. Stated
  lessons and events, and corrections, keep a high floor so they're never forgotten by decay.
- Inferred memories stay capped at INFERRED_CAP, below lived experience.

The agent runs this at startup when the last run was more than a day ago. Merging duplicates
and promoting patterns (many events -> one inferred fact) need the LLM and aren't built yet.
"""

from __future__ import annotations

import argparse
import time

from .store import MemoryStore

IDLE_DAYS = 30
DECAY_PER_DAY = 0.002  # about 0.06 a month once idle
REINFORCE = 0.03
INFERRED_CAP = 0.4
FLOOR_LIVED = 0.7  # stated lessons/events and corrections
FLOOR_STATED = 0.4
FLOOR_OTHER = 0.1


def consolidate(store: MemoryStore, now: float | None = None) -> dict:
    now = now or time.time()
    last = float(store.meta("consolidated_at") or 0)
    days = min((now - last) / 86400, 30) if last else 1.0
    db = store.db
    corrections = {r[0] for r in db.execute(
        "SELECT DISTINCT superseded_by FROM memories WHERE superseded_by IS NOT NULL")}
    reinforced = decayed = 0
    rows = db.execute("SELECT id, kind, salience, provenance, recorded_at, last_seen_at "
                      "FROM memories WHERE superseded_by IS NULL").fetchall()
    for mid, kind, sal, prov, recorded, seen in rows:
        new = sal
        if seen and seen > last:
            new = sal + REINFORCE
            reinforced += 1
        elif now - (seen or recorded) > IDLE_DAYS * 86400:
            if prov == "stated" and (kind in ("lesson", "event") or mid in corrections):
                floor = FLOOR_LIVED
            elif prov == "stated":
                floor = FLOOR_STATED
            else:
                floor = FLOOR_OTHER
            new = max(floor, sal - DECAY_PER_DAY * days) if sal > floor else sal
            decayed += new < sal
        new = min(new, INFERRED_CAP if prov == "inferred" else 1.0)
        if abs(new - sal) > 1e-9:
            db.execute("UPDATE memories SET salience = ? WHERE id = ?", (round(new, 4), mid))
    db.commit()
    store.meta("consolidated_at", str(now))
    return {"reinforced": reinforced, "decayed": decayed, "memories": len(rows)}


def due(store: MemoryStore, now: float | None = None) -> bool:
    last = float(store.meta("consolidated_at") or 0)
    return (now or time.time()) - last > 86400


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default="memories.db")
    parser.add_argument("--enrich", action="store_true",
                        help="add structure to plain memories with Claude (costs a little)")
    args = parser.parse_args()
    store = MemoryStore(args.db)
    if args.enrich:
        import anthropic
        from dotenv import load_dotenv

        from .librarian import Librarian
        load_dotenv()
        print(f"enriched {Librarian(anthropic.Anthropic(), store).enrich()} memories")
    print(consolidate(store))


if __name__ == "__main__":
    main()
