import pytest

from assistant.memory import JSONMemoryStore, SQLiteMemoryStore


@pytest.fixture(params=["sqlite", "json"])
def store(request, tmp_path):
    if request.param == "sqlite":
        return SQLiteMemoryStore(tmp_path / "memories.db")
    return JSONMemoryStore(tmp_path / "memories.json")


def test_add_and_search(store):
    store.add("User drinks green tea every morning", ["food"])
    store.add("User's sister is named Maya", ["family"])
    hits = store.search("what tea does the user drink")
    assert hits and hits[0].content == "User drinks green tea every morning"


def test_search_ranks_better_match_first(store):
    store.add("User likes hiking")
    store.add("User likes hiking in the mountains on weekends")
    hits = store.search("mountains hiking weekends")
    assert hits[0].content == "User likes hiking in the mountains on weekends"


def test_delete(store):
    m = store.add("User lives in Austin")
    assert store.delete(m.id)
    assert not store.delete(m.id)
    assert store.search("Austin") == []


def test_all_returns_in_creation_order(store):
    for text in ["first fact", "second fact", "third fact"]:
        store.add(text)
    assert [m.content for m in store.all()] == ["first fact", "second fact", "third fact"]


@pytest.mark.parametrize("query", ['"unbalanced', "tea AND OR NOT", "tea*", "NEAR(tea coffee)",
                                   "'; DROP TABLE memories; --"])
def test_query_syntax_cannot_break_search(store, query):
    store.add("User drinks green tea")
    store.search(query)  # must not raise
    assert len(store.all()) == 1


def test_search_ignores_short_words(store):
    store.add("User is a PM")
    assert store.search("is a") == []


def test_persists_across_instances(tmp_path):
    SQLiteMemoryStore(tmp_path / "m.db").add("User's dog is called Biscuit")
    assert SQLiteMemoryStore(tmp_path / "m.db").search("dog")[0].content.endswith("Biscuit")


@pytest.mark.xfail(strict=True, reason="keyword search misses paraphrases; see evals/")
def test_paraphrase_recall_known_limitation(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "m.db")
    store.add("User drinks green tea every morning")
    assert store.search("what do I like to drink")
