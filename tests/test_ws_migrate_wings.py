"""ws-migrate-wings: re-key stored wings to the canonical name in every store (fork).

Runs only against a palace created under ``tmp_path``.
"""

import argparse
import json
import os

import pytest

from mempalace import cli_workspace, hallways, palace_graph, server_registry, wing_migrate
from mempalace.config import MempalaceConfig
from mempalace.knowledge_graph import KnowledgeGraph
from mempalace.palace import get_aaak_collection, get_closets_collection, get_collection

DRAWERS = {
    "d_jo_1": "jarvis-ops",
    "d_jo_2": "jarvis-ops",
    "d_jo_3": "jarvis-ops",
    "d_jo_new": "jarvis_ops",  # already canonical: jarvis-ops merges into it
    "d_hive_space": "bnm hive",
    "d_hive_1": "bnm-hive",
    "d_hive_2": "bnm-hive",
    "d_claude": "wing_claude",
    "d_tr_1": "tech-radar",
    "d_tr_2": "tech-radar",
    "d_dsc": "dsc",
}
EXPECTED_AFTER = {"jarvis_ops": 4, "bnm_hive": 3, "cowork": 1, "tech_radar": 2, "dsc": 1}


def _seed(palace):
    drawers = get_collection(palace, create=True)
    drawers.upsert(
        ids=list(DRAWERS),
        documents=[f"wing migration probe {i} about the {w} project" for i, w in DRAWERS.items()],
        metadatas=[
            {"wing": w, "room": "notes", "source_file": f"{i}.md", "entities": "Alpha;Beta"}
            for i, w in DRAWERS.items()
        ],
    )
    get_closets_collection(palace, create=True).upsert(
        ids=["closet_jo_01"],
        documents=["jarvis ops notes →d_jo_1"],
        metadatas=[{"wing": "jarvis-ops", "room": "notes", "source_file": "d_jo_1.md"}],
    )
    get_aaak_collection(palace, create=True).upsert(
        ids=["aaak_hive"],
        documents=["BNM.HIVE|notes|★"],
        metadatas=[{"wing": "bnm-hive", "room": "notes", "kind": "aaak"}],
    )
    config = MempalaceConfig(palace_path=palace)
    with hallways._hallway_file_lock(config), hallways._writing(config) as conn:
        for wing, count, access in (("jarvis-ops", 3, 5), ("jarvis_ops", 1, 0)):
            record = {
                "id": hallways._hallway_id(wing, "Alpha", "Beta"),
                "wing": wing,
                "entity_a": "Alpha",
                "entity_b": "Beta",
                "co_occurrence_count": count,
                "rooms": ["notes"],
                "label": "Alpha ↔ Beta",
                "created_by": "auto",
                "access_count": access,
            }
            conn.execute(hallways._UPSERT, hallways._row(record))
    palace_graph.create_tunnel(
        "jarvis-ops", "notes", "tech-radar", "notes", label="probe", config=config
    )
    kg = KnowledgeGraph(db_path=os.path.join(palace, "knowledge_graph.sqlite3"))
    for drawer_id in ("d_jo_1", "d_hive_1"):
        kg.upsert_document(drawer_id, "probe", wing=DRAWERS[drawer_id], room="notes")
    kg.close()
    return config


def _store_wings(palace, config):
    """``{store: {wing: n}}`` read straight from every store."""
    from mempalace.migrate import _iter_collection_items

    out = {}
    for store, col in (
        ("drawers", get_collection(palace, create=False, read_only=True)),
        ("closets", get_closets_collection(palace, create=False, read_only=True)),
        ("aaak", get_aaak_collection(palace, create=False, read_only=True)),
    ):
        counts = {}
        for _id, meta in _iter_collection_items(col):
            counts[meta["wing"]] = counts.get(meta["wing"], 0) + 1
        out[store] = counts
    out["hallways"] = hallways.hallway_wing_counts(config)
    out["tunnels"] = palace_graph.tunnel_wing_counts(config)
    out["documents"] = wing_migrate._documents_counts(
        os.path.join(palace, "knowledge_graph.sqlite3")
    )
    return out


def _snapshot(palace):
    """``{file: (mtime_ns, size)}`` for every palace file.

    SQLite ``-shm`` files are left out: any reader of a WAL database rewrites
    that shared-memory index, and it holds no rows.
    """
    snap = {}
    for root, _dirs, files in os.walk(palace):
        for name in files:
            if name.endswith("-shm"):
                continue
            path = os.path.join(root, name)
            st = os.stat(path)
            snap[os.path.relpath(path, palace)] = (st.st_mtime_ns, st.st_size)
    return snap


@pytest.fixture(autouse=True)
def _real_stdout_is_captured(monkeypatch):
    """cli_workspace writes to an fd duplicated at import; send it to sys.stdout."""
    monkeypatch.setattr(cli_workspace, "_out", lambda text: print(text))


@pytest.fixture
def palace(tmp_path):
    path = str(tmp_path / "palace")
    config = _seed(path)
    return path, config


def _run(palace_path, *argv):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    cli_workspace.register(sub)
    ns = parser.parse_args(["ws-migrate-wings", "--palace", palace_path, *argv])
    return cli_workspace.maybe_dispatch(ns)


def test_dry_run_changes_nothing(palace, capsys):
    path, config = palace
    stores = _store_wings(path, config)
    snap = _snapshot(path)

    assert _run(path) == 0
    out = capsys.readouterr().out
    assert "(DRY RUN)" in out
    assert "jarvis-ops -> jarvis_ops: 3 drawers" in out
    assert "jarvis-ops -> jarvis_ops: 3 drawers" in out and "(MERGE into jarvis_ops)" in out
    assert "bnm-hive -> bnm_hive: 2 drawers" in out and "(MERGE into bnm_hive)" in out
    assert "DRY RUN: nothing changed" in out

    assert _store_wings(path, config) == stores
    assert _snapshot(path) == snap


def test_dry_run_plan_lists_every_store(palace):
    path, _config = palace
    result = wing_migrate.migrate_wings(path, explicit=True)
    rows = {r["old"]: r for r in result["renames"]}
    assert set(rows) == {"jarvis-ops", "bnm hive", "bnm-hive", "wing_claude", "tech-radar"}
    assert {o: r["new"] for o, r in rows.items()} == {
        "jarvis-ops": "jarvis_ops",
        "bnm hive": "bnm_hive",
        "bnm-hive": "bnm_hive",
        "wing_claude": "cowork",
        "tech-radar": "tech_radar",
    }
    jo = rows["jarvis-ops"]
    assert (jo["drawers"], jo["closets"], jo["hallways"], jo["tunnels"], jo["documents"]) == (
        3,
        1,
        1,
        1,
        1,
    )
    assert jo["merge"] is True
    assert rows["bnm-hive"]["aaak"] == 1 and rows["bnm-hive"]["documents"] == 1
    assert result["before"] == EXPECTED_AFTER
    assert result["applied"] is False


def test_apply_moves_every_store_and_keeps_counts(palace, capsys):
    path, config = palace
    assert _run(path, "--apply") == 0
    out = capsys.readouterr().out
    assert "(APPLIED)" in out and "Counts match." in out
    assert "jarvis_ops: 4 -> 4" in out and "bnm_hive: 3 -> 3" in out

    stores = _store_wings(path, config)
    assert stores["drawers"] == EXPECTED_AFTER
    assert stores["closets"] == {"jarvis_ops": 1}
    assert stores["aaak"] == {"bnm_hive": 1}
    # The two Alpha/Beta hallways merged; the stronger one (and its dynamics) won.
    assert stores["hallways"] == {"jarvis_ops": 1}
    [h] = hallways.list_hallways("jarvis_ops", config=config)
    assert h["id"] == hallways._hallway_id("jarvis_ops", "Alpha", "Beta")
    assert (h["co_occurrence_count"], h["access_count"]) == (3, 5)
    assert stores["tunnels"] == {"jarvis_ops": 1, "tech_radar": 1}
    [t] = palace_graph._load_tunnels(config)
    assert t["id"] == palace_graph._canonical_tunnel_id(
        "jarvis_ops", "notes", "tech_radar", "notes"
    )
    assert stores["documents"] == {"jarvis_ops": 1, "bnm_hive": 1}
    # Content and ids are untouched.
    got = get_collection(path, create=False).get(ids=["d_jo_1"], include=["documents"])
    assert got["documents"] == ["wing migration probe d_jo_1 about the jarvis-ops project"]


def test_second_apply_is_a_no_op(palace, capsys):
    path, config = palace
    assert _run(path, "--apply") == 0
    stores = _store_wings(path, config)
    snap = _snapshot(path)
    capsys.readouterr()

    result = wing_migrate.migrate_wings(path, apply=True, explicit=True)
    assert result["renames"] == []
    assert all(n == 0 for n in result["moved"].values())
    assert _store_wings(path, config) == stores
    assert _snapshot(path) == snap
    assert _run(path, "--apply") == 0
    out = capsys.readouterr().out
    assert "(APPLY)" in out and "already canonical" in out


def test_search_on_the_new_wing_finds_the_moved_drawers(palace):
    path, _config = palace
    wing_migrate.migrate_wings(path, apply=True, explicit=True)
    from mempalace.searcher import search_memories

    out = search_memories("wing migration probe", path, wing="jarvis_ops", n_results=20)
    assert {h["wing"] for h in out["results"]} == {"jarvis_ops"}
    assert len(out["results"]) == 4
    out = search_memories("wing migration probe", path, wing="bnm_hive", n_results=20)
    assert {h["wing"] for h in out["results"]} == {"bnm_hive"}
    assert len(out["results"]) == 3


def test_merge_of_coexisting_spellings(palace):
    path, config = palace
    result = wing_migrate.migrate_wings(path, apply=True, explicit=True)
    hive = [r for r in result["renames"] if r["new"] == "bnm_hive"]
    assert {r["old"] for r in hive} == {"bnm hive", "bnm-hive"}
    assert all(r["merge"] for r in hive)
    assert result["after"]["bnm_hive"] == result["before"]["bnm_hive"] == 3
    assert result["counts_ok"] is True
    assert "bnm hive" not in _store_wings(path, config)["drawers"]


def test_refuses_while_a_hub_serves_the_palace(palace, monkeypatch, capsys):
    path, config = palace
    stores = _store_wings(path, config)
    monkeypatch.setattr(
        server_registry, "read_live_serverinfo", lambda _p: {"pid": 4242, "port": 1, "host": "x"}
    )
    assert _run(path, "--apply") == 2
    assert "hub (pid 4242)" in capsys.readouterr().err
    assert _store_wings(path, config) == stores


def test_refuses_while_another_writer_holds_the_lease(palace, monkeypatch, capsys):
    import contextlib

    from mempalace import palace as palace_mod

    path, config = palace
    stores = _store_wings(path, config)

    @contextlib.contextmanager
    def held(_p):
        raise palace_mod.MineAlreadyRunning("palace is held by PID 4242")
        yield

    monkeypatch.setattr(palace_mod, "mine_palace_lock", held)
    assert _run(path, "--apply", "--json") == 2
    out = json.loads(capsys.readouterr().out)
    assert out["success"] is False and "held by PID 4242" in out["error"]
    assert _store_wings(path, config) == stores


def test_topics_by_wing_is_left_alone_for_a_palace_copy(palace):
    path, _config = palace
    result = wing_migrate.migrate_wings(path, explicit=True)
    assert result["topics_by_wing"].startswith("skipped")


def _content_hashes(root):
    """``{file: sha256}`` for every file under ``root``, nothing left out."""
    import hashlib

    out = {}
    for base, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(base, name)
            with open(path, "rb") as handle:
                out[os.path.relpath(path, root)] = hashlib.sha256(handle.read()).hexdigest()
    return out


def test_dry_run_in_a_fresh_process_writes_no_file(tmp_path):
    """A fresh CLI process opens nothing for write: no Chroma client (which
    records a write and rewrites the HNSW files), no KG or hallway sidecars, no
    lease lock files. Checked by content hash over the palace and the files
    beside it (hallways, tunnels, the palace-side lock)."""
    import subprocess
    import sys

    from mempalace.backends import _inproc_sqlite
    from mempalace.palace import get_backend_for_palace

    path = str(tmp_path / "palace")
    config = _seed(path)
    get_backend_for_palace(path).close_palace(path)
    _inproc_sqlite.release_all()
    assert os.path.isfile(hallways._get_hallway_db(config))
    assert os.path.isfile(os.path.join(path, "knowledge_graph.sqlite3"))
    before = _content_hashes(tmp_path)

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = (
        "import sys, runpy; sys.path.insert(0, sys.argv[1]); "
        "sys.argv = ['mempalace', 'ws-migrate-wings', '--palace', sys.argv[2]]; "
        "runpy.run_module('mempalace', run_name='__main__')"
    )
    env = {**os.environ, "MEMPALACE_HUB_FORWARD": "0"}
    env.pop("PYTHONPATH", None)
    done = subprocess.run(
        [sys.executable, "-c", code, root, path],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert done.returncode == 0, done.stderr
    assert "(DRY RUN)" in done.stdout and "jarvis-ops -> jarvis_ops: 3 drawers" in done.stdout
    assert "1 documents" in done.stdout and "1 hallways" in done.stdout

    assert _content_hashes(tmp_path) == before
