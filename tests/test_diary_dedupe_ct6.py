"""CT6 regression: tool_diary_write must not re-ingest text already in the palace.

The 2026-09-17 recovery re-ingested from the abandoned palace over entries that
were already present, producing 44 byte-identical rows. The capability to stop
it existed (`mempalace_check_duplicate`, 162 calls in telemetry); the diary path
never called it.

The load-bearing case is `test_legacy_row_without_entry_sha_is_still_caught`.
Rows written before the fix carry no `entry_sha` metadata, and those are exactly
the rows a recovery collides with — a metadata-only guard would have missed all
nine real cases while passing a naive test suite.

Copy into the fork's tests/ and run:
    pytest tests/test_diary_dedupe_ct6.py -q
"""

import hashlib

import pytest

from mempalace import mcp_server


ENTRY = "# Pending Diary — CT6 test\n\nSESSION:2026-09-18|ct6|★★★\n\nbody text here\n"
SHA = hashlib.sha256(ENTRY.encode()).hexdigest()[:12]


class FakeCollection:
    """Minimal stand-in for the chroma collection.

    `supports_where` models the two worlds the guard must work in: a palace
    where rows carry `entry_sha` metadata, and one where they do not.
    """

    def __init__(self, rows=None, supports_where=True):
        # rows: list of (id, metadata)
        self.rows = list(rows or [])
        self.supports_where = supports_where
        self.added = []

    def get(self, ids=None, where=None, include=None, limit=None, offset=None):
        if where is not None:
            if not self.supports_where:
                raise RuntimeError("where unsupported")
            hit = [r for r in self.rows if all(r[1].get(k) == v for k, v in where.items())]
            return {"ids": [r[0] for r in hit]}
        if offset:
            return {"ids": []}
        return {"ids": [r[0] for r in self.rows]}

    def add(self, ids=None, documents=None, metadatas=None, **kw):
        self.added.append((ids, documents, metadatas))
        for i, rid in enumerate(ids or []):
            self.rows.append((rid, (metadatas or [{}])[i]))

    def upsert(self, **kw):
        self.add(**kw)


@pytest.fixture
def patched(monkeypatch):
    def _install(col):
        # _get_collection is called with kwargs (e.g. create=True), so the stub
        # must accept them — a bare `lambda: col` raises TypeError inside the
        # function's own try/except and surfaces as a misleading write failure.
        monkeypatch.setattr(mcp_server, "_get_collection", lambda *a, **k: col)
        monkeypatch.setattr(mcp_server, "_invalidate_overview_caches", lambda *a, **k: None)
        return col

    return _install


class TestDiaryDedupe:
    def test_first_write_is_filed(self, patched):
        col = patched(FakeCollection())
        res = mcp_server.tool_diary_write("cowork", ENTRY, topic="ct6")
        assert res["success"] is True
        assert not res.get("duplicate")
        assert col.added, "the entry should have been written"

    def test_identical_second_write_is_skipped(self, patched):
        col = patched(FakeCollection())
        mcp_server.tool_diary_write("cowork", ENTRY, topic="ct6")
        before = len(col.added)
        res = mcp_server.tool_diary_write("cowork", ENTRY, topic="ct6")
        assert res["duplicate"] is True
        assert res["skipped"] is True
        assert res["success"] is True, "a replayed flush is a no-op, not a failure"
        assert len(col.added) == before, "nothing should have been written"

    def test_legacy_row_without_entry_sha_is_still_caught(self, patched):
        """THE REAL CASE. A 2026-09-17-style re-ingest hitting a pre-patch row.

        The existing row has no `entry_sha`, so only the id scan can see it.
        """
        legacy_id = f"diary_cowork_20260716_034658671264_{SHA}"
        col = patched(FakeCollection(rows=[(legacy_id, {"wing": "cowork", "room": "diary"})]))
        res = mcp_server.tool_diary_write("cowork", ENTRY, topic="ct6")
        assert res["duplicate"] is True
        assert res["entry_id"] == legacy_id
        assert not col.added

    def test_legacy_chunked_row_is_caught(self, patched):
        """A chunked legacy entry: the sha sits before the _chunk_ suffix."""
        legacy_id = f"diary_cowork_20260716_034658671264_{SHA}_chunk_000000"
        col = patched(FakeCollection(rows=[(legacy_id, {"room": "diary"})]))
        res = mcp_server.tool_diary_write("cowork", ENTRY, topic="ct6")
        assert res["duplicate"] is True
        assert not col.added

    def test_different_text_is_not_a_duplicate(self, patched):
        col = patched(FakeCollection())
        mcp_server.tool_diary_write("cowork", ENTRY, topic="ct6")
        res = mcp_server.tool_diary_write("cowork", ENTRY + "one more line\n", topic="ct6")
        assert not res.get("duplicate")
        assert len(col.added) == 2

    def test_force_overrides(self, patched):
        col = patched(FakeCollection())
        mcp_server.tool_diary_write("cowork", ENTRY, topic="ct6")
        res = mcp_server.tool_diary_write("cowork", ENTRY, topic="ct6", force=True)
        assert not res.get("duplicate")
        assert len(col.added) == 2

    def test_a_broken_where_query_does_not_block_writes(self, patched):
        """Fail open on probe failure: a dedupe probe must never lose a diary."""
        col = patched(FakeCollection(supports_where=False))
        res = mcp_server.tool_diary_write("cowork", ENTRY, topic="ct6")
        assert res["success"] is True
        assert not res.get("duplicate")
        assert col.added

    def test_the_guard_can_actually_fire_and_block(self, patched):
        """Mutant check: prove the skip path is reachable, not just asserted."""
        col = patched(FakeCollection())
        mcp_server.tool_diary_write("cowork", ENTRY, topic="ct6")
        results = [mcp_server.tool_diary_write("cowork", ENTRY, topic="ct6") for _ in range(3)]
        assert all(r.get("duplicate") for r in results)
        assert len(col.added) == 1, "three replays must add nothing"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
