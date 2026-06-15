"""
test_kg_active_dedup.py — Atomic dedup of active triples (G1b / issue #1799).

Verifies that:
  - idx_triples_active_unique partial index is created by _migrate_schema
  - Adding the same active triple twice produces exactly one DB row
    and returns the same id both times (no TOCTOU gap)
  - Invalidate-then-re-add creates a fresh active row (partial index only
    covers valid_to IS NULL rows, so the old invalidated row is transparent)
"""

import pytest

from mempalace.knowledge_graph import KnowledgeGraph


@pytest.fixture
def kg(tmp_path):
    """Fresh in-memory-like KG backed by a temp-dir SQLite file."""
    return KnowledgeGraph(db_path=str(tmp_path / "kg_dedup.sqlite3"))


class TestActiveTripleDedup:
    def test_partial_index_exists(self, kg):
        """_migrate_schema must create idx_triples_active_unique on startup."""
        conn = kg._conn()
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_triples_active_unique'"
        ).fetchone()
        assert row is not None, "partial unique index idx_triples_active_unique not found"

    def test_add_same_active_triple_twice_returns_same_id(self, kg):
        """INSERT OR IGNORE absorbs the second write; callers get the same id."""
        id1 = kg.add_triple("Alice", "knows", "Bob", valid_from="2025-01-01")
        id2 = kg.add_triple("Alice", "knows", "Bob", valid_from="2025-01-01")
        assert id1 == id2

    def test_add_same_active_triple_twice_produces_one_row(self, kg):
        """Only one active row must exist after two identical add_triple calls."""
        kg.add_triple("Alice", "knows", "Bob", valid_from="2025-01-01")
        kg.add_triple("Alice", "knows", "Bob", valid_from="2025-01-01")
        conn = kg._conn()
        count = conn.execute(
            "SELECT COUNT(*) FROM triples"
            " WHERE subject='alice' AND predicate='knows' AND object='bob'"
            " AND valid_to IS NULL"
        ).fetchone()[0]
        assert count == 1, f"expected 1 active row, got {count}"

    def test_invalidate_then_readd_creates_new_active_row(self, kg):
        """
        After invalidation valid_to is set, so the row falls outside the
        partial index.  A fresh add_triple must succeed and return a different id.
        """
        id1 = kg.add_triple("Alice", "works_at", "Acme", valid_from="2020-01-01")
        kg.invalidate("Alice", "works_at", "Acme", ended="2024-12-31")
        id2 = kg.add_triple("Alice", "works_at", "Acme", valid_from="2025-01-01")

        assert id1 != id2, "re-add after invalidation must produce a new triple id"

        # Both rows must be present: one expired, one active
        conn = kg._conn()
        total = conn.execute(
            "SELECT COUNT(*) FROM triples"
            " WHERE subject='alice' AND predicate='works_at' AND object='acme'"
        ).fetchone()[0]
        active = conn.execute(
            "SELECT COUNT(*) FROM triples"
            " WHERE subject='alice' AND predicate='works_at' AND object='acme'"
            " AND valid_to IS NULL"
        ).fetchone()[0]
        assert total == 2, f"expected 2 total rows (1 expired + 1 active), got {total}"
        assert active == 1, f"expected 1 active row after re-add, got {active}"
