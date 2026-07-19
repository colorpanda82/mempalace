"""tool_kg_add must resolve predicates through the canonical vocabulary.

Storage is faked. These tests are about what reaches the storage layer and
what comes back to the caller, not about SQLite.
"""

import pytest

from mempalace import mcp_server
from mempalace.kg_vocabulary import PolicyError  # noqa: F401  (documents intent)


@pytest.fixture
def captured(monkeypatch):
    """Intercept the storage call and the WAL, return what each received."""
    seen = {"triple": None, "wal": None, "calls": 0}

    class FakeKG:
        def add_triple(self, subject, predicate, obj, **kwargs):
            seen["triple"] = {"subject": subject, "predicate": predicate, "object": obj}
            seen["triple"].update(kwargs)
            seen["calls"] += 1
            return 4242

    monkeypatch.setattr(mcp_server, "_call_kg", lambda fn: fn(FakeKG()))
    monkeypatch.setattr(
        mcp_server, "_wal_log", lambda op, payload: seen.__setitem__("wal", payload)
    )
    monkeypatch.delenv("MEMPALACE_PREDICATE_POLICY", raising=False)
    return seen


def _add(**kw):
    args = {"subject": "jarvis", "predicate": "is_a", "object": "mac mini"}
    args.update(kw)
    return mcp_server.tool_kg_add(**args)


# -- canonical -------------------------------------------------------------


def test_canonical_predicate_reaches_storage_unchanged(captured):
    result = _add(predicate="deployed_on")
    assert result["success"] is True
    assert captured["triple"]["predicate"] == "deployed_on"


def test_canonical_write_carries_no_note(captured):
    result = _add(predicate="deployed_on")
    assert "note" not in result


def test_canonical_write_keeps_existing_response_keys(captured):
    """Backward compatibility: callers already read these three."""
    result = _add(predicate="is_a")
    assert set(("success", "triple_id", "fact")).issubset(result)
    assert result["triple_id"] == 4242


# -- remap -----------------------------------------------------------------


def test_alias_is_normalized_before_storage(captured):
    result = _add(predicate="runs_on")
    assert result["success"] is True
    assert captured["triple"]["predicate"] == "deployed_on", (
        "storage must receive the canonical predicate, not the alias"
    )


def test_remap_is_reported_to_the_caller(captured):
    """The whole point of borrowing 'match, do not just reject'."""
    result = _add(predicate="runs_on")
    assert "note" in result, "a silent rewrite defeats the design"
    assert "runs_on" in result["note"]
    assert "deployed_on" in result["note"]


def test_remap_fact_string_shows_what_was_stored(captured):
    result = _add(predicate="runs_on")
    assert "deployed_on" in result["fact"]
    assert "runs_on" not in result["fact"]


def test_wal_records_both_submitted_and_stored_on_remap(captured):
    _add(predicate="runs_on")
    wal = captured["wal"]
    assert wal["predicate"] == "deployed_on"
    assert wal["predicate_submitted"] == "runs_on", (
        "the audit trail must not lose the fact that a rewrite happened"
    )


def test_wal_omits_submitted_key_when_nothing_changed(captured):
    _add(predicate="is_a")
    assert "predicate_submitted" not in captured["wal"]


# -- rejection -------------------------------------------------------------


def test_banned_predicate_is_refused(captured):
    result = _add(predicate="status")
    assert result["success"] is False
    assert "session-state" in result["error"]


def test_banned_predicate_never_reaches_storage(captured):
    _add(predicate="status")
    assert captured["calls"] == 0
    assert captured["triple"] is None


def test_banned_predicate_is_not_written_to_the_wal(captured):
    _add(predicate="in_queue")
    assert captured["wal"] is None, "a refused write must not be logged as attempted"


def test_unknown_predicate_is_refused_with_a_suggestion(captured, monkeypatch):
    monkeypatch.setenv("MEMPALACE_PREDICATE_POLICY", "strict")
    result = _add(predicate="deployed_onn")
    assert result["success"] is False
    assert "deployed_on" in result["error"]


def test_unknown_predicate_never_reaches_storage(captured, monkeypatch):
    monkeypatch.setenv("MEMPALACE_PREDICATE_POLICY", "strict")
    _add(predicate="frobnicates")
    assert captured["calls"] == 0


# -- ordering --------------------------------------------------------------


def test_sanitization_runs_before_vocabulary(captured):
    """A path-traversal attempt must fail on charset, not get spell-checked."""
    result = _add(predicate="../../etc/passwd")
    assert result["success"] is False
    assert "vocabulary" not in result["error"].lower()


# -- policy ----------------------------------------------------------------


def test_warn_policy_admits_unknown_through_the_tool(captured, monkeypatch):
    monkeypatch.setenv("MEMPALACE_PREDICATE_POLICY", "warn")
    result = _add(predicate="frobnicates")
    assert result["success"] is True
    assert captured["triple"]["predicate"] == "frobnicates"
    assert "note" in result


def test_warn_policy_still_refuses_banned_through_the_tool(captured, monkeypatch):
    monkeypatch.setenv("MEMPALACE_PREDICATE_POLICY", "warn")
    result = _add(predicate="status")
    assert result["success"] is False


def test_default_policy_admits_a_personal_fact_predicate(captured):
    """Regression guard for the strict-by-default mistake.

    MemPalace's own suite writes "Alice likes coffee". A workspace-specific
    infrastructure vocabulary must never refuse that by default.
    """
    result = _add(subject="Alice", predicate="likes", object="coffee")
    assert result["success"] is True
    assert captured["triple"]["predicate"] == "likes"


# ==========================================================================
# End-to-end against a REAL KnowledgeGraph. Everything above fakes storage,
# so "runs_on is stored as deployed_on" was asserted but never observed.
# ==========================================================================


class TestVocabularyEndToEnd:
    def test_remapped_predicate_persists_as_canonical(
        self, monkeypatch, config, palace_path, kg
    ):
        """Write an alias, read back the canonical predicate from SQLite."""
        from tests.test_mcp_server import _patch_mcp_server

        _patch_mcp_server(monkeypatch, config, kg)
        monkeypatch.delenv("MEMPALACE_PREDICATE_POLICY", raising=False)
        from mempalace.mcp_server import tool_kg_add

        result = tool_kg_add(
            subject="_e2e_probe",
            predicate="runs_on",
            object="jarvis",
        )
        assert result["success"] is True
        assert "note" in result

        facts = kg.query_entity("_e2e_probe")
        assert len(facts) == 1
        assert facts[0]["predicate"] == "deployed_on", (
            f"alias did not persist as canonical: {facts[0]['predicate']!r}"
        )

    def test_remapped_fact_is_queryable_under_canonical_name(
        self, monkeypatch, config, palace_path, kg
    ):
        """The point of normalizing: one query finds facts written either way."""
        from tests.test_mcp_server import _patch_mcp_server

        _patch_mcp_server(monkeypatch, config, kg)
        monkeypatch.delenv("MEMPALACE_PREDICATE_POLICY", raising=False)
        from mempalace.mcp_server import tool_kg_add

        tool_kg_add(subject="_e2e_a", predicate="runs_on", object="jarvis")
        tool_kg_add(subject="_e2e_b", predicate="deployed_on", object="jarvis")

        preds = set()
        for subj in ("_e2e_a", "_e2e_b"):
            for fact in kg.query_entity(subj):
                preds.add(fact["predicate"])
        assert preds == {"deployed_on"}, (
            f"alias and canonical did not converge on one predicate: {preds}"
        )

    def test_banned_predicate_writes_nothing_to_a_real_kg(
        self, monkeypatch, config, palace_path, kg
    ):
        from tests.test_mcp_server import _patch_mcp_server

        _patch_mcp_server(monkeypatch, config, kg)
        monkeypatch.delenv("MEMPALACE_PREDICATE_POLICY", raising=False)
        from mempalace.mcp_server import tool_kg_add

        result = tool_kg_add(subject="_e2e_banned", predicate="status", object="wip")
        assert result["success"] is False
        assert kg.query_entity("_e2e_banned") == []

    def test_personal_fact_predicate_persists_unchanged(
        self, monkeypatch, config, palace_path, kg
    ):
        """MemPalace's native use case must survive the vocabulary layer."""
        from tests.test_mcp_server import _patch_mcp_server

        _patch_mcp_server(monkeypatch, config, kg)
        monkeypatch.delenv("MEMPALACE_PREDICATE_POLICY", raising=False)
        from mempalace.mcp_server import tool_kg_add

        result = tool_kg_add(subject="Alice", predicate="likes", object="coffee")
        assert result["success"] is True
        facts = kg.query_entity("Alice")
        assert len(facts) == 1
        assert facts[0]["predicate"] == "likes"
