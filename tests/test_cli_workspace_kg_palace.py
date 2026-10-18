"""cli_workspace in-process calls use the knowledge graph of an explicit palace.

3.10 raised ``_palace_flag_given`` at import from ``sys.argv``, so
``mempalace kg-query --palace X`` read ``X/knowledge_graph.sqlite3``. 3.11
raises it only through ``_apply_server_flags()``, and the port's in-process
route read the per-user ``DEFAULT_KG_PATH`` instead (no active facts against a
snapshot palace that had them).
"""

import argparse
import io
import json

from mempalace import cli_workspace
from mempalace.knowledge_graph import KnowledgeGraph
from tests._mcp_server_helpers import _keep_server_command_line_state


def _parse(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--palace", default=None)
    sub = parser.add_subparsers(dest="command")
    cli_workspace.register(sub)
    return parser.parse_args(argv)


def _isolate(monkeypatch, tmp_path):
    from mempalace import mcp_server

    _keep_server_command_line_state(monkeypatch)
    monkeypatch.setattr(mcp_server, "_palace_flag_given", False)
    default_kg = tmp_path / "home" / "knowledge_graph.sqlite3"
    default_kg.parent.mkdir()
    monkeypatch.setattr(mcp_server, "DEFAULT_KG_PATH", str(default_kg))
    monkeypatch.setattr(mcp_server, "_kg_by_path", {})
    monkeypatch.setattr(cli_workspace, "_hub_for", lambda _palace: None)
    # --json output goes to the stdout captured at import, not sys.stdout.
    monkeypatch.setattr(cli_workspace, "_REAL_STDOUT", io.StringIO())

    palace = tmp_path / "palace"
    palace.mkdir()
    kg = KnowledgeGraph(db_path=str(palace / "knowledge_graph.sqlite3"))
    kg.add_triple("Alice", "works_at", "Acme")
    kg.close()
    return mcp_server, palace, default_kg


def _close_cached(mcp_server):
    for kg in mcp_server._kg_by_path.values():
        kg.close()


def _active_objects(out):
    return {f["object"] for f in out["active_facts"]}


def test_kg_query_verb_palace_reads_the_palace_graph(monkeypatch, tmp_path):
    mcp_server, palace, default_kg = _isolate(monkeypatch, tmp_path)
    try:
        rc = cli_workspace.maybe_dispatch(
            _parse(["kg-query", "Alice", "--palace", str(palace), "--json"])
        )
    finally:
        _close_cached(mcp_server)
    assert rc == 0
    out = json.loads(cli_workspace._REAL_STDOUT.getvalue())
    assert _active_objects(out) == {"Acme"}
    assert not default_kg.exists()


def test_kg_query_global_palace_reads_the_palace_graph(monkeypatch, tmp_path):
    mcp_server, palace, default_kg = _isolate(monkeypatch, tmp_path)
    try:
        rc = cli_workspace.maybe_dispatch(
            _parse(["--palace", str(palace), "kg-query", "Alice", "--json"])
        )
    finally:
        _close_cached(mcp_server)
    assert rc == 0
    assert _active_objects(json.loads(cli_workspace._REAL_STDOUT.getvalue())) == {"Acme"}
    assert not default_kg.exists()


def test_call_tool_with_palace_path_reads_the_palace_graph(monkeypatch, tmp_path):
    mcp_server, palace, default_kg = _isolate(monkeypatch, tmp_path)
    try:
        out = cli_workspace.call_tool("kg_query", {"entity": "Alice"}, palace_path=str(palace))
    finally:
        _close_cached(mcp_server)
    assert _active_objects(out) == {"Acme"}
    assert not default_kg.exists()


def test_no_palace_given_keeps_the_default_graph(monkeypatch, tmp_path):
    """Control: without --palace the 3.10 default graph is still the one used."""
    mcp_server, palace, default_kg = _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(palace))
    try:
        out = cli_workspace.call_tool("kg_query", {"entity": "Alice"})
    finally:
        _close_cached(mcp_server)
    assert out["active_facts"] == []
    assert mcp_server._palace_flag_given is False
