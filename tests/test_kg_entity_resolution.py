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

    monkeypatch.setattr(mcp_server, "_call_kg", lambda fn, read=False: fn(FakeKG()))
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

    monkeypatch.setattr(mcp_server, "_call_kg", lambda fn, read=False: fn(FakeKG()))
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
        from tests._mcp_server_helpers import _patch_mcp_server

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
        from tests._mcp_server_helpers import _patch_mcp_server

        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_kg_add

        tool_kg_add(subject="macbook", predicate="is_a", object="laptop")
        tool_kg_add(subject="jarvis", predicate="is_a", object="mac mini")

        assert len(kg.query_entity("macbook")) == 1
        assert len(kg.query_entity("jarvis")) == 1

    def test_kg_query_end_to_end_resolves_and_notes(self, monkeypatch, config, palace_path, kg):
        from tests._mcp_server_helpers import _patch_mcp_server

        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_kg_add, tool_kg_query

        tool_kg_add(subject="research-scout", predicate="uses", object="python")
        result = tool_kg_query(entity="research_scout")
        assert result["entity"] == "research-scout"
        assert result["count"] == 1
        assert "resolved_entity" in result


# ==========================================================================
# Hardening round 2 (adversarial review of 7b3ae40): F1/F2/F4/F5.
# These construct real KnowledgeGraph state and exercise the actual TOOL
# wiring (not a mocked fact_count) wherever the bug lives in the wiring
# itself, per the review's requirement to test against a real KG.
# ==========================================================================


class TestFuzzyLeakGuard:
    """F1: kg.query_entity's single-candidate fuzzy fallback must never
    leak into fact_count, or a candidate spelling with no node of its own
    borrows another entity's facts and kg_add creates a phantom node."""

    def test_foo_bar_baz_fuzzy_trap_does_not_create_a_phantom_node(
        self, monkeypatch, config, palace_path, kg
    ):
        from tests._mcp_server_helpers import _patch_mcp_server

        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_kg_add

        # Seed ONLY foo_bar_baz. foo_bar and foo-bar have no node at all.
        kg.add_triple("foo_bar_baz", "is_a", "seed")

        result = tool_kg_add(subject="foo-bar", predicate="is_a", object="thing")
        assert result["success"] is True
        assert "resolved_entity" not in result, (
            "foo-bar has no EXACT facts of its own or of a real variant; "
            "query_entity's single-candidate fuzzy fallback against "
            "foo_bar_baz must not count as a hit"
        )
        assert kg.timeline_total("foo-bar") == 1
        assert kg.timeline_total("foo_bar") == 0, (
            "a phantom 'foo_bar' node must not be created from the fuzzy leak"
        )

    def test_date_like_literal_is_not_rewritten_by_a_fuzzy_sibling(
        self, monkeypatch, config, palace_path, kg
    ):
        from tests._mcp_server_helpers import _patch_mcp_server

        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_kg_add

        # A real underscore-separated sibling that a fuzzy LIKE match could
        # borrow from if fact_count is not exact-id.
        kg.add_triple("2026_09_23_standup", "is_a", "seed")

        result = tool_kg_add(subject="x", predicate="noted_on", object="2026-09-23")
        assert result["success"] is True
        assert "resolved_entity" not in result
        assert kg.timeline_total("2026-09-23") == 1
        assert kg.timeline_total("2026_09_23") == 0


class TestAliasHeldFactsNotRedirected:
    """F2: kg_invalidate/kg_supersede must operate on the literal name when
    it already holds the fact being targeted, even if that literal is a
    recorded merge alias -- redirecting would silently fail to find/close
    the fact and misreport success."""

    def test_invalidate_operates_on_the_alias_that_actually_holds_the_fact(
        self, monkeypatch, config, palace_path, kg
    ):
        from tests._mcp_server_helpers import _patch_mcp_server

        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_kg_invalidate

        # Written directly under the alias spelling, bypassing resolution --
        # simulates a pre-existing fact from before the resolver existed.
        kg.add_triple("research_scout", "version", "1.0")

        result = tool_kg_invalidate(subject="research_scout", predicate="version", object="1.0")
        assert result["success"] is True, result
        assert result["matched"] == 1
        assert "resolved_entity" in result, "must say the merge target was not used"

        facts = kg.query_entity("research_scout", direction="both")
        assert len(facts) == 1 and facts[0]["current"] is False
        assert kg.timeline_total("research-scout") == 0, (
            "nothing should have been created under the canonical name"
        )

    def test_supersede_operates_on_the_alias_that_actually_holds_the_fact(
        self, monkeypatch, config, palace_path, kg
    ):
        from tests._mcp_server_helpers import _patch_mcp_server

        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_kg_supersede

        kg.add_triple("research_scout", "version", "1.0")

        result = tool_kg_supersede(
            subject="research_scout", predicate="version", old_object="1.0", new_object="2.0"
        )
        assert result["success"] is True, result

        old_fact = [
            f for f in kg.query_entity("research_scout", direction="both") if f["object"] == "1.0"
        ]
        assert old_fact and old_fact[0]["current"] is False, (
            "the old fact under the alias must actually be closed"
        )
        new_fact = [
            f for f in kg.query_entity("research_scout", direction="both") if f["object"] == "2.0"
        ]
        assert new_fact and new_fact[0]["current"] is True


class TestDirectionInsensitiveCounting:
    """F4: the resolver's own fact-existence check must never be scoped to
    the caller's requested query direction, or an entity with only
    incoming facts reads as empty and a real variant goes unfound."""

    def test_kg_query_resolves_an_entity_with_only_incoming_facts(
        self, monkeypatch, config, palace_path, kg
    ):
        from tests._mcp_server_helpers import _patch_mcp_server

        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_kg_query

        # aa_bb has ONLY an incoming fact; aa-bb has no node at all.
        kg.add_triple("zz_source", "part_of", "aa_bb")

        result = tool_kg_query(entity="aa-bb", direction="outgoing")
        assert result["entity"] == "aa_bb", (
            "resolution must find aa_bb's incoming-only fact even though "
            "the caller asked for direction=outgoing"
        )
        assert "resolved_entity" in result


class TestTieIsNamedNotClaimedAsMost:
    """F5: an equal-count tie must say 'tie' and name the tie-break rule,
    never claim 'most'."""

    def test_equal_count_candidates_are_reported_as_a_tie(self, kg):
        kg.add_triple("a_b_c", "is_a", "seed1")
        kg.add_triple("a-b-c", "is_a", "seed2")

        from mempalace.kg_entities import resolve_entity

        resolved, note = resolve_entity("a-b_c", fact_count=lambda n: kg.timeline_total(n))
        assert resolved == "a-b-c", "alphabetically-first tie-break"
        assert note is not None
        assert "tie" in note.lower()
        assert "most" not in note.lower()


# -- DE1 (2026-10-02): mixed hyphen/underscore canonical spellings -----------


def test_mixed_separator_canonical_is_found():
    resolved, note = resolve_entity(
        "design_systems_database", fact_count=_counts({"design-systems_database": 3})
    )
    assert resolved == "design-systems_database"
    assert note


def test_mixed_separator_from_hyphenated_input():
    resolved, _ = resolve_entity(
        "mempalace-mcp-sidecar", fact_count=_counts({"mempalace-mcp_sidecar": 2})
    )
    assert resolved == "mempalace-mcp_sidecar"


def test_separator_variants_cap_falls_back_to_extremes():
    from mempalace.kg_entities import _separator_variants

    name = "a_b_c_d_e_f"  # 5 separator slots, above the cap of 4
    variants = _separator_variants(name)
    assert variants == ["a-b-c-d-e-f"]
    assert "a-b_c_d_e_f" not in variants


# -- rebase-312: punctuation-preserving entity ids (#2770) --------------------


class TestSpacedFallbackUnderPunctuationIds:
    """3.12 keeps a literal underscore in the id (``%5F``), so ``alpha_beta``
    no longer shares the id of an entity stored as ``Alpha Beta``. The
    resolver reaches it through the all-space spelling, as 3.11 did by id."""

    def test_underscore_spelling_reaches_a_space_named_entity(self, kg):
        kg.add_triple("Alpha Beta", "is_a", "seed")
        assert kg.timeline_total("alpha_beta") == 0, "precondition: ids differ in 3.12"
        resolved, note = resolve_entity("alpha_beta", fact_count=kg.timeline_total)
        assert resolved == "alpha beta"
        assert kg.timeline_total(resolved) == 1
        assert note and "alpha beta" in note

    def test_hyphen_variant_still_wins_over_the_space_fallback(self, kg):
        kg.add_triple("gamma_delta", "is_a", "seed")
        resolved, _note = resolve_entity("gamma-delta", fact_count=kg.timeline_total)
        assert resolved == "gamma_delta"

    def test_space_fallback_not_tried_when_literal_has_facts(self, kg):
        kg.add_triple("Eps Zeta", "is_a", "seed")
        kg.add_triple("eps_zeta", "is_a", "other")
        resolved, note = resolve_entity("eps_zeta", fact_count=kg.timeline_total)
        assert (resolved, note) == ("eps_zeta", None)
