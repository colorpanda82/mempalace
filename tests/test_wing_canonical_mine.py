"""Mining a project after its wing was re-keyed by ``ws-migrate-wings`` (fork).

A palace filed by 3.11 holds drawers under the wing a project's
``mempalace.yaml`` names as written (``jarvis-ops``), with ids that embed that
spelling. ``ws-migrate-wings --apply`` rewrites the ``wing`` metadata to the
canonical name (``jarvis_ops``) and keeps the ids, and it does not touch the
yaml. The next mine must still file an edited file, prune its old chunks and
store (and report) the canonical wing, on a migrated palace and on one that
was never migrated.
"""

import os

import pytest

from mempalace import miner, wing_migrate
from mempalace.ids import make_drawer_id_from_chunk
from mempalace.palace import get_closets_collection, get_collection

OLD = "Alpha release notes. The backup job runs nightly at three. " * 12
NEW = "Bravo release notes. The restore drill runs weekly on Fridays. " * 12


def _project(tmp_path):
    proj = tmp_path / "proj"
    (proj / "docs").mkdir(parents=True)
    (proj / "mempalace.yaml").write_text(
        "wing: jarvis-ops\nrooms:\n  - name: docs\n    description: docs\n", encoding="utf-8"
    )
    note = proj / "docs" / "notes.md"
    note.write_text(f"# Notes\n\n{OLD}\n", encoding="utf-8")
    return proj, note


def _as_311(palace, old="jarvis-ops"):
    """Re-key what this code just mined into the shape 3.11 wrote.

    3.11 stored the yaml wing as written and built ids from it. Rows already
    in that shape are left alone, so this is a no-op on code that stores the
    yaml wing verbatim.
    """
    canon = wing_migrate.target_for(old) or old
    renamed = {}
    col = get_collection(palace)
    got = col.get(include=["documents", "metadatas", "embeddings"])
    rows = []
    for rid, doc, meta, emb in zip(
        got["ids"], got["documents"], got["metadatas"], got["embeddings"]
    ):
        if meta.get("wing") not in (canon, old):
            continue
        if meta.get("chunk_index") is not None and meta.get("source_file"):
            new_id = make_drawer_id_from_chunk(
                old, meta["room"], meta["source_file"], meta["chunk_index"]
            )
        else:
            new_id = rid.replace(f"_{canon}_", f"_{old}_", 1)
        if new_id == rid and meta["wing"] == old:
            continue
        renamed[rid] = new_id
        rows.append((rid, new_id, doc, {**meta, "wing": old}, list(emb)))
    if rows:
        col.delete(ids=[r[0] for r in rows])
        col.upsert(
            ids=[r[1] for r in rows],
            documents=[r[2] for r in rows],
            metadatas=[r[3] for r in rows],
            embeddings=[r[4] for r in rows],
        )
    closets = get_closets_collection(palace)
    got = closets.get(include=["documents", "metadatas"])
    rows = []
    for rid, doc, meta in zip(got["ids"], got["documents"], got["metadatas"]):
        if meta.get("wing") != canon:
            continue
        for a, b in renamed.items():
            doc = doc.replace(a, b)
        rows.append((rid, rid.replace(f"closet_{canon}_", f"closet_{old}_", 1), doc, meta))
    if rows:
        closets.delete(ids=[r[0] for r in rows])
        closets.upsert(
            ids=[r[1] for r in rows],
            documents=[r[2] for r in rows],
            metadatas=[{**r[3], "wing": old} for r in rows],
        )


def _source_rows(palace, note):
    got = get_collection(palace).get(
        where={"source_file": str(note)}, include=["documents", "metadatas"]
    )
    return list(zip(got["ids"], got["documents"], got["metadatas"]))


def _edit(note):
    note.write_text(f"# Notes\n\n{NEW}\n", encoding="utf-8")
    st = os.stat(note)
    os.utime(note, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000_000))


@pytest.fixture
def mined_311(tmp_path):
    proj, note = _project(tmp_path)
    palace = str(tmp_path / "palace")
    miner.mine(str(proj), palace)
    _as_311(palace)
    rows = _source_rows(palace, note)
    assert rows and {m["wing"] for _i, _d, m in rows} == {"jarvis-ops"}
    assert all(i.startswith("drawer_jarvis-ops_") for i, _d, _m in rows)
    return proj, note, palace


@pytest.mark.parametrize("migrated", [True, False], ids=["migrated", "unmigrated"])
def test_mine_after_migration_files_the_edit_and_prunes_old_chunks(mined_311, migrated):
    proj, note, palace = mined_311
    if migrated:
        result = wing_migrate.migrate_wings(palace, apply=True, explicit=True)
        assert result["counts_ok"] and result["moved"]["drawers"] > 0
        assert {m["wing"] for _i, _d, m in _source_rows(palace, note)} == {"jarvis_ops"}
    _edit(note)

    miner.mine(str(proj), palace)  # raised CollisionError on a migrated palace

    rows = _source_rows(palace, note)
    assert rows
    docs = " ".join(d for _i, d, _m in rows)
    assert "Bravo release notes" in docs
    assert "Alpha release notes" not in docs
    # One drawer per chunk: the old chunks were pruned, not left beside the new.
    assert sorted(m["chunk_index"] for _i, _d, m in rows) == list(range(len(rows)))


@pytest.mark.parametrize("migrated", [True, False], ids=["migrated", "unmigrated"])
def test_mine_reports_and_stores_the_canonical_wing(mined_311, migrated, capsys):
    proj, note, palace = mined_311
    if migrated:
        wing_migrate.migrate_wings(palace, apply=True, explicit=True)
    _edit(note)
    capsys.readouterr()

    miner.mine(str(proj), palace)

    out = capsys.readouterr().out
    assert "Wing:    jarvis_ops" in out
    rows = _source_rows(palace, note)
    assert {m["wing"] for _i, _d, m in rows} == {"jarvis_ops"}
    closets = get_closets_collection(palace).get(
        where={"source_file": str(note)}, include=["metadatas"]
    )
    assert {m["wing"] for m in closets["metadatas"]} <= {"jarvis_ops"}


def test_mine_with_a_wing_override_stores_the_canonical_wing(tmp_path, capsys):
    proj, note = _project(tmp_path)
    palace = str(tmp_path / "palace")
    miner.mine(str(proj), palace, wing_override="wing_bnm")
    assert "Wing:    brave_new_media" in capsys.readouterr().out
    assert {m["wing"] for _i, _d, m in _source_rows(palace, note)} == {"brave_new_media"}


def test_convo_mine_with_a_wing_stores_the_canonical_wing(tmp_path, capsys):
    from mempalace.convo_miner import mine_convos

    convos = tmp_path / "chats"
    convos.mkdir()
    chat = convos / "chat.md"
    chat.write_text(
        "> how often does the backup job run on the mini?\n"
        + "It runs nightly at three and verifies the restore afterwards. " * 6
        + "\n\n> and the restore drill?\n"
        + "Weekly, on Fridays, against a scratch copy of the palace. " * 6
        + "\n",
        encoding="utf-8",
    )
    palace = str(tmp_path / "palace")
    mine_convos(str(convos), palace, wing="jarvis-ops")
    assert "Wing:    jarvis_ops" in capsys.readouterr().out
    got = get_collection(palace).get(where={"source_file": str(chat)}, include=["metadatas"])
    assert got["ids"] and {m["wing"] for m in got["metadatas"]} == {"jarvis_ops"}


def test_format_mine_with_a_wing_uses_the_canonical_wing(tmp_path, capsys, monkeypatch):
    from mempalace import format_miner

    monkeypatch.setattr(format_miner, "scan_formats", lambda _d: [])
    format_miner.mine_formats(str(tmp_path), str(tmp_path / "palace"), wing="jarvis-ops")
    assert "Wing:    jarvis_ops" in capsys.readouterr().out
