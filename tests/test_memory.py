import sqlite3
import time

import pytest

from assistant.memory import MemoryStore
from assistant.memory.consolidate import FLOOR_LIVED, consolidate


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path / "memories.db")


def test_add_and_search(store):
    store.add("User drinks green tea every morning", theme=["food"])
    store.add("User's sister is named Maya")
    hits = store.search("what tea does the user drink")
    assert hits and hits[0].content == "User drinks green tea every morning"


def test_search_ranks_better_match_first(store):
    store.add("User likes hiking")
    store.add("User likes hiking in the mountains on weekends")
    hits = store.search("mountains hiking weekends")
    assert hits[0].content == "User likes hiking in the mountains on weekends"


def test_keywords_find_paraphrases(store):
    """Write-time keywords are what let keyword search answer a paraphrase."""
    store.add("User drinks green tea every morning", keywords=["beverage", "drink", "coffee"])
    assert store.search("what's my usual hot beverage")[0].content.startswith("User drinks")


def test_salience_breaks_ties(store):
    store.add("User once tried kayaking", salience=0.3)
    store.add("User loves kayaking", salience=0.9)
    assert store.search("kayaking")[0].content == "User loves kayaking"


def test_superseded_memories_are_hidden_but_kept(store):
    old = store.add("User lives in Austin", attribute="home_city")
    new = store.add("User lives in Denver", attribute="home_city", supersedes=old.id,
                    supersede_reason="moved")
    assert [m.id for m in store.search("lives")] == [new.id]
    kept = store.get(old.id)
    assert kept.superseded_by == new.id and kept.supersede_reason == "moved"
    assert "Austin" not in store.profile() and "Denver" in store.profile()


def test_series_returned_newest_first(store):
    for value, day in [("84.0", "2026-08-01"), ("82.2", "2026-10-01"), ("83.1", "2026-09-01")]:
        store.add(f"User weighed {value} kg", "series_point", attribute="weight", value=value,
                  occurred_at=day)
    assert [m.value for m in store.search("weight")] == ["82.2", "83.1", "84.0"]
    assert "weight: latest 82.2 (2026-10-01), 3 points" in store.profile()


def test_profile_has_attribute_facts_and_relationships(store):
    store.add("User is vegetarian", attribute="diet")
    store.add("User likes hiking")  # no attribute: not a core fact
    store.relate("Maya", "person", "sister")
    p = store.profile()
    assert "User is vegetarian" in p and "Maya is user's sister" in p and "hiking" not in p


def test_entities_resolve_by_name_and_alias(store):
    a = store.entity("person", "Maya")
    assert store.entity("person", "maya") == a
    store.add_alias(a, "Mai")
    assert store.entity("person", "Mai") == a
    store.relate("Maya", "person", "sister")
    store.relate("maya", "person", "sister")  # same link again: no duplicate
    assert len(store.relationships()) == 1


def test_inferred_salience_is_capped(store):
    assert store.add("User might like jazz", provenance="inferred", salience=0.9).salience == 0.4


def test_delete_removes_memory_and_its_history(store):
    old = store.add("User lives in Austin", attribute="home_city")
    new = store.add("User lives in Denver", attribute="home_city", supersedes=old.id)
    assert store.delete(f"m{new.id}")
    assert store.all(include_superseded=True) == []
    assert not store.delete(new.id)


def test_delete_relationship(store):
    r = store.relate("Tom", "person", "manager")
    assert store.delete(f"r{r.id}") and store.relationships() == []


def test_search_and_associate_touch_memories(store):
    m = store.add("User likes hiking")
    store.search("hiking")
    assert store.get(m.id).hit_count == 1 and store.get(m.id).last_seen_at


def test_associate_gate(store):
    lesson = store.add("User's flight was delayed and they were starving with no snacks",
                       "lesson", keywords=["flying", "airport"], salience=0.9)
    store.add("User might enjoy flying lessons", provenance="inferred", salience=0.9)
    store.add("User sometimes flies for work", provenance="observed", salience=0.5)
    store.add("User's home airport is DEN", attribute="home_airport", salience=0.9)
    assert [m.id for m in store.associate("I'm flying to Portland", mark=False)] == [lesson.id]
    assert store.associate("is it going to rain tomorrow", mark=False) == []


def test_associate_cooldown(store):
    store.add("User got food poisoning at Sakura Sushi", "lesson", salience=0.9, keywords=["sushi"])
    assert store.associate("sushi tonight?")
    assert store.associate("sushi tonight?") == []  # surfaced just now
    assert store.associate("sushi tonight?", now=time.time() + 3 * 86400)


@pytest.mark.parametrize("query", ['"unbalanced', "tea AND OR NOT", "tea*", "NEAR(tea coffee)",
                                   "'; DROP TABLE memories; --"])
def test_query_syntax_cannot_break_search(store, query):
    store.add("User drinks green tea")
    store.search(query)  # must not raise
    store.associate(query)
    assert len(store.all()) == 1


def test_search_ignores_short_and_stop_words(store):
    store.add("User is a PM")
    assert store.search("is a") == []
    assert store.search("what should you do") == []


def test_persists_across_instances(tmp_path):
    MemoryStore(tmp_path / "m.db").add("User's dog is called Biscuit")
    assert MemoryStore(tmp_path / "m.db").search("dog")[0].content.endswith("Biscuit")


def test_fts_follows_updates(store):
    m = store.add("User drinks green tea")
    store.update(m.id, keywords=["beverage"])
    assert store.search("beverage")[0].id == m.id
    store.db.execute("UPDATE memories SET content = 'User drinks coffee' WHERE id = ?", (m.id,))
    assert store.search("tea") == [] and store.search("coffee")


def test_migrates_first_schema(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE VIRTUAL TABLE memories USING fts5(id UNINDEXED, content, tags, "
               "created_at UNINDEXED)")
    db.execute("INSERT INTO memories VALUES ('ab12cd34', 'User drinks green tea', 'food drink', "
               "1700000000.0)")
    db.commit()
    db.close()
    store = MemoryStore(path)
    (m,) = store.all()
    assert (m.content, m.kind, m.theme, m.recorded_at) == ("User drinks green tea", "fact",
                                                           "food,drink", 1700000000.0)
    assert store.search("tea")[0].id == m.id
    # the old table is kept, and reopening doesn't migrate twice
    assert store.db.execute("SELECT count(*) FROM memories_v1").fetchone() == (1,)
    assert len(MemoryStore(path).all()) == 1


def test_consolidate_reinforces_and_decays(store):
    now = time.time()
    seen = store.add("User likes hiking", salience=0.6)
    idle = store.add("User likes chess", salience=0.6)
    lesson = store.add("User burned their hand on a skillet", "lesson", salience=0.75)
    store.meta("consolidated_at", str(now - 10 * 86400))
    store.db.execute("UPDATE memories SET recorded_at = ?", (now - 90 * 86400,))
    store.touch([seen.id])
    consolidate(store, now=now)
    assert store.get(seen.id).salience > 0.6
    assert store.get(idle.id).salience < 0.6
    assert store.get(lesson.id).salience >= FLOOR_LIVED
