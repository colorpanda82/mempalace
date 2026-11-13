"""add_drawer stays idempotent across spellings of one wing (fork, 3.12 port).

A palace filed before the port holds ``drawer_jarvis-ops_notes_<hash>``; the
port's writes store ``jarvis_ops`` and mint ``drawer_jarvis_ops_notes_<hash>``,
and ``ws-migrate-wings`` keeps the old ids. Re-filing the same text under any
spelling of the wing (a hook retry, a flusher replay) must find the old drawer.
"""

import pytest
from _mcp_server_helpers import _patch_mcp_server

from mempalace.ids import make_drawer_id_from_content

JARVIS = "Jarvis ops note: the backup job runs at 3am nightly on the mini."
BNM = "Brave New Media client note: invoice cadence is monthly."
LONG = "Jarvis ops runbook line: rotate the offsite key and verify restores. " * 40


def _seed_311(collection, wing, content, chunk_size=None):
    """Store ``content`` the way 3.11's add_drawer did: wing and id as given."""
    base = make_drawer_id_from_content(wing, "notes", content)
    meta = {"wing": wing, "room": "notes", "source_file": "", "added_by": "mcp"}
    if chunk_size is None or len(content) <= chunk_size:
        collection.add(ids=[base], documents=[content], metadatas=[{**meta, "chunk_index": 0}])
        return [base]
    chunks = [content[i : i + chunk_size] for i in range(0, len(content), chunk_size)]
    ids = [f"{base}_chunk_{i:06d}" for i in range(len(chunks))]
    collection.add(
        ids=ids,
        documents=chunks,
        metadatas=[{**meta, "chunk_index": i} for i in range(len(chunks))],
    )
    return ids


def _count(collection):
    return len(collection.get(include=[])["ids"])


@pytest.mark.parametrize(
    ("stored", "content", "asked"),
    [
        ("jarvis-ops", JARVIS, "jarvis-ops"),
        ("jarvis-ops", JARVIS, "jarvis_ops"),
        ("jarvis-ops", JARVIS, "Jarvis Ops"),
        ("brave-new-media", BNM, "bnm"),
        ("brave-new-media", BNM, "brave_new_media"),
    ],
    ids=["hyphen", "underscore", "spaced", "alias-bnm", "alias-canonical"],
)
def test_refiling_under_another_spelling_finds_the_old_drawer(
    monkeypatch, config, kg, collection, stored, content, asked
):
    [old_id] = _seed_311(collection, stored, content)
    _patch_mcp_server(monkeypatch, config, kg)
    from mempalace import mcp_server

    before = _count(collection)
    out = mcp_server.tool_add_drawer(wing=asked, room="notes", content=content)

    assert out == {"success": True, "reason": "already_exists", "drawer_id": old_id}
    assert _count(collection) == before


def test_refiling_after_migration_finds_the_old_id(monkeypatch, config, kg, collection):
    """ws-migrate-wings rewrote the wing and kept the hyphen id."""
    [old_id] = _seed_311(collection, "jarvis-ops", JARVIS)
    collection.update(ids=[old_id], metadatas=[{"wing": "jarvis_ops"}])
    _patch_mcp_server(monkeypatch, config, kg)
    from mempalace import mcp_server

    out = mcp_server.tool_add_drawer(wing="jarvis-ops", room="notes", content=JARVIS)
    assert out["reason"] == "already_exists" and out["drawer_id"] == old_id
    assert _count(collection) == 1


def test_refiling_chunked_text_under_another_spelling(monkeypatch, config, kg, collection):
    _patch_mcp_server(monkeypatch, config, kg)
    from mempalace import mcp_server

    chunk_size = mcp_server._config.chunk_size
    assert len(LONG) > chunk_size
    ids = _seed_311(collection, "jarvis-ops", LONG, chunk_size=chunk_size)
    out = mcp_server.tool_add_drawer(wing="jarvis_ops", room="notes", content=LONG)
    assert out["reason"] == "already_exists"
    assert _count(collection) == len(ids)


def test_new_text_is_still_filed_under_the_canonical_wing(monkeypatch, config, kg, collection):
    _seed_311(collection, "jarvis-ops", JARVIS)
    _patch_mcp_server(monkeypatch, config, kg)
    from mempalace import mcp_server

    out = mcp_server.tool_add_drawer(
        wing="jarvis-ops", room="notes", content="A different note filed after the port."
    )
    assert out["success"] and out.get("reason") != "already_exists"
    assert out["drawer_id"].startswith("drawer_jarvis_ops_notes_")
    assert _count(collection) == 2
