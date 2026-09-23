"""resolve_entity must apply the workspace's recorded KG entity merges and
resolve hyphen/underscore/space fragmentation deterministically -- the
entity-side counterpart to test_kg_add_vocabulary_wiring.py.
"""

import pytest

from mempalace import mcp_server
from mempalace.kg_entities import resolve_entity


def _counts(mapping):
    def fact_count(name):
        return mapping.get(name, 0)

    return fact_count


# -- resolve_entity, unit level (fake fact_count) ---------------------------


def test_merges_hit_resolves_to_canonical():
    resolved, note = resolve_entity(
        "research_scout", fact_count=_counts({"research_scout": 1, "research-scout": 12})
    )
    assert resolved == "research-scout"
    assert note and "research_scout" in note and "research-scout" in note


def test_merges_hit_fires_even_when_original_name_has_facts():
    """A stray write under the old name after a merge decision must still
    resolve forward -- this is why merges is checked before fact_count."""
    resolved, _note = resolve_entity(
        "mac_mini_m4", fact_count=_counts({"mac_mini_m4": 3, "jarvis": 40})
    )
    assert resolved == "jarvis"


def test_hyphen_variant_is_found_when_original_has_no_facts():
    resolved, note = resolve_entity("claude_code", fact_count=_counts({"claude-code": 5}))
    assert resolved == "claude-code"
    assert note and "claude_code" in note


def test_multiple_variants_picks_most_facts():
    resolved, note = resolve_entity(
        "foo-bar_baz",  # mixed separators
        fact_count=_counts({"foo_bar_baz": 2, "foo-bar-baz": 9}),
    )
    assert resolved == "foo-bar-baz"
    assert "9" in note and "most of" in note


def test_macbook_is_never_merged_into_jarvis_by_the_real_flagged_list():
    resolved, note = resolve_entity("macbook", fact_count=_counts({"macbook": 0, "jarvis": 40}))
    assert resolved == "macbook"
    assert note is None


def test_flagged_pair_blocks_a_would_be_variant_merge(monkeypatch):
    """Construct a case where the variant heuristic WOULD suggest merging
    two names, and confirm the flagged-pair guard refuses it."""
    monkeypatch.setattr(
        "mempalace.kg_entities.FLAGGED_NOT_MERGED_PAIRS",
        {frozenset({"foo-bar", "foo_bar"})},
    )
    resolved, note = resolve_entity("foo-bar", fact_count=_counts({"foo-bar": 0, "foo_bar": 7}))
    assert resolved == "foo-bar"
    assert note is None


def test_unknown_name_passes_through_unchanged():
    resolved, note = resolve_entity("Totally Unrelated Thing", fact_count=_counts({}))
    assert resolved == "Totally Unrelated Thing"
    assert note is None


def test_name_with_no_separator_skips_variant_lookup_entirely():
    calls = []

    def fact_count(n):
        calls.append(n)
        return 0

    resolved, note = resolve_entity("plainword", fact_count=fact_count)
    assert resolved == "plainword"
    assert note is None
    assert calls == [], "no separator to vary -- must not spend a read checking"


def test_empty_name_is_a_no_op():
    resolved, note = resolve_entity("", fact_count=_counts({}))
    assert resolved == ""
    assert note is None


def test_no_fact_count_callable_still_resolves_merges():
    resolved, note = resolve_entity("jarvis_dashboard")
    assert resolved == "jarvis-dashboard"
    assert note


def test_no_fact_count_callable_skips_variant_step():
    resolved, note = resolve_entity("claude_code")
    assert resolved == "claude_code"
    assert note is None


# -- wiring: tool_kg_query ---------------------------------------------------


@pytest.fixture
def query_captured(monkeypatch):
    seen = {"queried": []}

    class FakeKG:
        def query_entity(self, name, as_of=None, direction="both"):
            seen["queried"].append(name)
            if name == "research-scout":
                return [
                    {
                        "direction": "outgoing",
                        "subject": name,
                        "predicate": "uses",
                        "object": "python",
                        "valid_from": None,
                        "valid_to": None,
                    }
                ]
            return []

        def find_entity_candidates(self, name):
            return []

    monkeypatch.setattr(mcp_server, "_call_kg", lambda fn: fn(FakeKG()))
    return seen


def test_kg_query_resolves_a_recorded_merge(query_captured):
    result = mcp_server.tool_kg_query(entity="research_scout")
    assert result["entity"] == "research-scout"
    assert result["count"] == 1
    assert "resolved_entity" in result


def test_kg_query_leaves_an_unresolvable_entity_alone(query_captured):
    result = mcp_server.tool_kg_query(entity="something-else")
    assert result["entity"] == "something-else"
    assert "resolved_entity" not in result


# -- wiring: tool_kg_add ------------------------------------------------------


@pytest.fixture
def add_captured(monkeypatch):
    seen = {"triple": None, "queried": []}

    class FakeKG:
        def add_triple(self, subject, predicate, obj, **kwargs):
            seen["triple"] = {"subject": subject, "predicate": predicate, "object": obj}
            return 999

        def query_entity(self, name, as_of=None, direction="both"):
            seen["queried"].append(name)
            return [{"x": 1}] if name == "jarvis-dashboard" else []

    monkeypatch.setattr(mcp_server, "_call_kg", lambda fn: fn(FakeKG()))
    monkeypatch.setattr(mcp_server, "_wal_log", lambda op, payload: None)
    monkeypatch.delenv("MEMPALACE_PREDICATE_POLICY", raising=False)
    return seen


def test_kg_add_writes_subject_to_the_canonical_node(add_captured):
    result = mcp_server.tool_kg_add(subject="jarvis_dashboard", predicate="is_a", object="tool")
    assert result["success"] is True
    assert add_captured["triple"]["subject"] == "jarvis-dashboard"
    assert "resolved_entity" in result


def test_kg_add_resolves_object_too(add_captured):
    mcp_server.tool_kg_add(subject="x", predicate="uses", object="jarvis_dashboard")
    assert add_captured["triple"]["object"] == "jarvis-dashboard"


# ==========================================================================
# End-to-end against a REAL KnowledgeGraph.
# ==========================================================================


class TestEntityResolutionEndToEnd:
    def test_merge_target_receives_the_write(self, monkeypatch, config, palace_path, kg):
        from tests.test_mcp_server import _patch_mcp_server

        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_kg_add

        result = tool_kg_add(subject="jarvis_dashboard", predicate="is_a", object="dashboard")
        assert result["success"] is True

        facts_canonical = kg.query_entity("jarvis-dashboard")
        facts_old = kg.query_entity("jarvis_dashboard")
        assert len(facts_canonical) == 1
        assert facts_canonical[0]["predicate"] == "is_a"
        # store already treats space=underscore, so the old underscore name
        # and the hyphenated canonical are genuinely different nodes; the
        # write must land on the canonical one.
        assert len(facts_old) == 0

    def test_flagged_pair_is_never_conflated_end_to_end(self, monkeypatch, config, palace_path, kg):
        from tests.test_mcp_server import _patch_mcp_server

        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_kg_add

        tool_kg_add(subject="macbook", predicate="is_a", object="laptop")
        tool_kg_add(subject="jarvis", predicate="is_a", object="mac mini")

        assert len(kg.query_entity("macbook")) == 1
        assert len(kg.query_entity("jarvis")) == 1

    def test_kg_query_end_to_end_resolves_and_notes(self, monkeypatch, config, palace_path, kg):
        from tests.test_mcp_server import _patch_mcp_server

        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_kg_add, tool_kg_query

        tool_kg_add(subject="research-scout", predicate="uses", object="python")
        result = tool_kg_query(entity="research_scout")
        assert result["entity"] == "research-scout"
        assert result["count"] == 1
        assert "resolved_entity" in result
