"""The librarian's output is validated before anything is written. No API calls: a fake client."""

import json
from types import SimpleNamespace

import pytest

from assistant.agent import build_user_message, system_prompt
from assistant.memory import MemoryStore
from assistant.memory.librarian import SCHEMA, Librarian


class FakeClient:
    def __init__(self, items, stop_reason="end_turn"):
        self.items, self.stop_reason, self.requests = items, stop_reason, []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.requests.append(kwargs)
        text = json.dumps({"items": self.items})
        return SimpleNamespace(stop_reason=self.stop_reason, model="claude-opus-5-5",
                               content=[SimpleNamespace(type="text", text=text)])


def item(**kw):
    base = {"type": "memory", "entity_name": "user", "entity_type": "user", "kind": "fact",
            "attribute": None, "content": "", "value": None, "theme": [], "keywords": [],
            "salience": 0.6, "provenance": "stated", "mode": "task", "occurred_at": None,
            "supersedes": None, "supersede_reason": None, "link_type": None}
    return {**base, **kw}


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path / "memories.db")


def test_schema_only_uses_supported_features():
    """Structured outputs need additionalProperties false and every property required."""
    s = json.dumps(SCHEMA)
    for unsupported in ("minimum", "maximum", "minLength", "maxLength"):
        assert unsupported not in s
    props = SCHEMA["properties"]["items"]["items"]
    assert set(props["required"]) == set(props["properties"])


def test_stores_validated_items(store):
    lib = Librarian(FakeClient([item(content="User is vegetarian", attribute="Diet",
                                     keywords=["meat", "steak"])]), store)
    saved = lib("I'm vegetarian")
    assert [(s.action, s.text) for s in saved] == [("saved", "User is vegetarian")]
    (m,) = store.all()
    assert (m.attribute, m.keywords) == ("diet", "meat,steak")


def test_nothing_to_store(store):
    assert Librarian(FakeClient([]), store)("thanks!") == [] and store.all() == []


def test_supersedes_existing(store):
    old = store.add("User lives in Austin", attribute="home_city")
    lib = Librarian(FakeClient([item(content="User lives in Denver", attribute="home_city",
                                     supersedes=old.id, supersede_reason="moved")]), store)
    (s,) = lib("I moved to Denver")
    assert s.action == "updated"
    assert store.get(old.id).superseded_by is not None


def test_ignores_bad_supersede_ids_and_series_supersedes(store):
    point = store.add("User weighed 82 kg", "series_point", attribute="weight", value="82")
    lib = Librarian(FakeClient([
        item(content="User likes tea", supersedes=999),
        item(kind="series_point", attribute="weight", value="81", content="User weighed 81 kg",
             supersedes=point.id)]), store)
    assert [s.action for s in lib("...")] == ["saved", "saved"]
    assert store.get(point.id).superseded_by is None


def test_relationships_and_facts_about_others(store):
    lib = Librarian(FakeClient([
        item(type="relationship", entity_name="Priya", entity_type="person", link_type="sister"),
        item(entity_name="Priya", entity_type="person", content="Priya lives in Denver")]), store)
    saved = lib("My sister Priya moved to Denver")
    assert [s.action for s in saved] == ["linked", "saved"]
    assert store.relationships()[0].render().endswith("Priya is user's sister")
    assert store.all()[0].entity == "Priya"


def test_skips_invalid_items(store):
    lib = Librarian(FakeClient([item(kind="nonsense", content="x"), item(content="  "),
                                item(type="relationship", entity_name="user", link_type="self"),
                                {"type": "memory"}]), store)
    assert lib("...") == [] and store.all() == []


def test_incomplete_response_stores_nothing(store):
    lib = Librarian(FakeClient([item(content="User is vegetarian")], stop_reason="max_tokens"),
                    store)
    assert lib("I'm vegetarian") == []
    assert lib.error == "stopped early (max_tokens)"


def test_prompt_contains_turn_and_existing_memories(store):
    store.add("User lives in Austin", attribute="home_city")
    client = FakeClient([])
    Librarian(client, store)("I moved", reply="Welcome!", previous_user="hi", today="2026-10-14")
    prompt = client.requests[0]["messages"][0]["content"]
    for part in ("Today: 2026-10-14", "fact:home_city) User lives in Austin",
                 "<user_message>\nI moved", "<assistant_reply>\nWelcome!",
                 "<previous_user_message>\nhi"):
        assert part in prompt


def test_enrich_fills_structure(store):
    m = store.add("User drinks green tea")
    lib = Librarian(FakeClient([{"id": m.id, "kind": "fact", "attribute": "drink", "value": None,
                                 "theme": ["food"], "keywords": ["beverage"], "salience": 0.65,
                                 "mode": "task"}]), store)
    assert lib.enrich() == 1
    assert store.search("beverage")[0].attribute == "drink"


def test_agent_context_has_profile_and_associations(store):
    store.add("User lives in Denver", attribute="home_city")
    store.add("User's flight was delayed and they had no snacks", "lesson", salience=0.9,
              keywords=["flying", "airport"])
    system = system_prompt(store)
    assert "User lives in Denver" in system[1]["text"]
    msg = build_user_message("I'm flying to Portland", store, auto_recall=False)
    assert "<possibly_relevant_memories>" in msg["content"][1]["text"]
    assert len(build_user_message("what's 2+2", store, auto_recall=False)["content"]) == 1


def test_series_point_without_value_takes_number_from_content(store):
    lib = Librarian(FakeClient([item(kind="series_point", attribute="weight",
                                     content="User weighed 82.4 kg on 2026-10-14")]), store)
    lib("Weighed 82.4 this morning")
    assert store.all()[0].value == "82.4"
