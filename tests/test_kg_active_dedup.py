"""Atomic dedup of identical active triples (G1b), narrowed for v3.11 rewrite()."""

import sqlite3

from mempalace.knowledge_graph import KnowledgeGraph


def _indexes(kg):
    return {
        r[0]
        for r in kg._conn().execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_triples_active%'"
        )
    }


def test_duplicate_active_add_keeps_one_row_and_one_id(tmp_path):
    with KnowledgeGraph(db_path=str(tmp_path / "kg.sqlite3")) as kg:
        first = kg.add_triple("Alice", "likes", "tea")
        second = kg.add_triple("Alice", "likes", "tea")
        assert first == second
        assert kg._conn().execute("SELECT COUNT(*) FROM triples").fetchone()[0] == 1
        assert _indexes(kg) == {"idx_triples_active_unique_v2"}


def test_index_rejects_an_identical_racing_insert(tmp_path):
    with KnowledgeGraph(db_path=str(tmp_path / "kg.sqlite3")) as kg:
        kg.add_triple("Alice", "likes", "tea", source_drawer_id="d1")
        conn = kg._conn()
        cur = conn.execute(
            "INSERT OR IGNORE INTO triples (id, subject, predicate, object, source_drawer_id) "
            "VALUES ('racer', 'alice', 'likes', 'tea', 'd1')"
        )
        assert cur.rowcount == 0


def test_legacy_v1_index_is_replaced_on_open(tmp_path):
    db = tmp_path / "kg.sqlite3"
    with KnowledgeGraph(db_path=str(db)):
        pass
    conn = sqlite3.connect(db)
    conn.execute("DROP INDEX idx_triples_active_unique_v2")
    conn.execute(
        "CREATE UNIQUE INDEX idx_triples_active_unique "
        "ON triples(subject, predicate, object) WHERE valid_to IS NULL"
    )
    conn.commit()
    conn.close()
    with KnowledgeGraph(db_path=str(db)) as kg:
        assert _indexes(kg) == {"idx_triples_active_unique_v2"}


def _open_rows(kg, predicate):
    return kg._conn().execute(
        "SELECT id, confidence, source_closet FROM triples "
        "WHERE predicate=? AND valid_to IS NULL",
        (predicate,),
    ).fetchall()


def test_rewrite_aliases_with_different_confidence_share_one_successor(tmp_path):
    """Two aliases folding to one predicate at one boundary: the v2 index ignores
    confidence, so the second rewrite returns the first successor, not IntegrityError."""
    with KnowledgeGraph(db_path=str(tmp_path / "kg.sqlite3")) as kg:
        a = kg.add_triple("Alice", "works_at", "Acme", confidence=0.9)
        b = kg.add_triple("Alice", "employed_by", "Acme", confidence=0.6)
        first = kg.rewrite(a, "works_for", "Acme", at="2026-01-01T00:00:00Z")
        second = kg.rewrite(b, "works_for", "Acme", at="2026-01-01T00:00:00Z")
        assert first == second
        rows = _open_rows(kg, "works_for")
        assert [(r["id"], r["confidence"]) for r in rows] == [(first, 0.9)]
        assert _open_rows(kg, "works_at") == [] and _open_rows(kg, "employed_by") == []


def test_rewrite_aliases_with_null_and_empty_closet_share_one_successor(tmp_path):
    with KnowledgeGraph(db_path=str(tmp_path / "kg.sqlite3")) as kg:
        a = kg.add_triple("Alice", "works_at", "Acme", source_closet=None)
        b = kg.add_triple("Alice", "employed_by", "Acme", source_closet="")
        first = kg.rewrite(a, "works_for", "Acme", at="2026-01-01T00:00:00Z")
        second = kg.rewrite(b, "works_for", "Acme", at="2026-01-01T00:00:00Z")
        assert first == second
        assert len(_open_rows(kg, "works_for")) == 1
        assert _open_rows(kg, "employed_by") == []
