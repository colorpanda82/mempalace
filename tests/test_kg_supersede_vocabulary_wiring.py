"""tool_kg_supersede must resolve predicates through the canonical vocabulary.

Mirror of test_kg_add_vocabulary_wiring.py. Supersede opens a NEW fact, so it
carries the write-time policy of kg_add, not the lookup leniency of
kg_invalidate. Storage is faked; these tests are about what reaches it.
"""

import pytest

from mempalace import mcp_server


@pytest.fixture
def captured(monkeypatch):
    seen = {"call": None, "wal": None, "calls": 0}

    class FakeKG:
        def supersede(self, subject, predicate, old_obj, new_obj, at=None):
            seen["call"] = {"subject": subject, "predicate": predicate,
                            "old": old_obj, "new": new_obj, "at": at}
            seen["calls"] += 1
            return 7

    monkeypatch.setattr(mcp_server, "_call_kg", lambda fn: fn(FakeKG()))
    monkeypatch.setattr(
        mcp_server, "_wal_log", lambda op, payload: seen.__setitem__("wal", payload)
    )
    monkeypatch.delenv("MEMPALACE_PREDICATE_POLICY", raising=False)
    return seen


def _sup(**kw):
    args = {"subject": "mempalace", "predicate": "version",
            "old_object": "3.5.0", "new_object": "3.9.0"}
    args.update(kw)
    return mcp_server.tool_kg_supersede(**args)


def test_canonical_predicate_reaches_storage_unchanged(captured):
    result = _sup(predicate="version")
    assert result["success"] is True
    assert captured["call"]["predicate"] == "version"
    assert "note" not in result
    assert set(("success", "triple_id", "fact", "superseded")).issubset(result)


def test_alias_is_normalized_and_reported(captured):
    result = _sup(predicate="runs_on", old_object="macbook", new_object="jarvis")
    assert captured["call"]["predicate"] == "deployed_on"
    assert "deployed_on" in result["fact"] and "runs_on" not in result["fact"]
    assert "runs_on" in result["note"] and "deployed_on" in result["note"]


def test_wal_records_submitted_predicate_on_remap(captured):
    _sup(predicate="runs_on", old_object="macbook", new_object="jarvis")
    assert captured["wal"]["predicate"] == "deployed_on"
    assert captured["wal"]["predicate_submitted"] == "runs_on"


def test_wal_omits_submitted_key_when_nothing_changed(captured):
    _sup(predicate="version")
    assert "predicate_submitted" not in captured["wal"]


def test_banned_predicate_is_refused_before_storage_and_wal(captured):
    result = _sup(predicate="status", old_object="idle", new_object="busy")
    assert result["success"] is False
    assert captured["calls"] == 0
    assert captured["wal"] is None


def test_unknown_predicate_is_refused_under_strict(captured, monkeypatch):
    monkeypatch.setenv("MEMPALACE_PREDICATE_POLICY", "strict")
    result = _sup(predicate="deployed_onn")
    assert result["success"] is False
    assert "deployed_on" in result["error"]
    assert captured["calls"] == 0
