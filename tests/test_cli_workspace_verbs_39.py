"""kg-supersede verb and search-json date window (workspace fork, v3.9.0 adopt)."""
import argparse

from mempalace import cli_workspace


def _parse(argv):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    cli_workspace.register(sub)
    return parser.parse_args(argv)


def test_kg_supersede_is_registered_and_wrapped():
    assert "kg-supersede" in cli_workspace.COMMANDS
    assert "tool_kg_supersede" in cli_workspace.WRAPPED_TOOLS
    from mempalace import mcp_server
    assert hasattr(mcp_server, "tool_kg_supersede")


def test_kg_supersede_forwards_all_five_fields(monkeypatch):
    seen = {}

    def fake_call_tool(name, args=None, *, palace_path=None):
        seen["name"], seen["args"] = name, args
        return {"success": True, "triple_id": 1, "fact": "x", "superseded": "y"}

    monkeypatch.setattr(cli_workspace, "call_tool", fake_call_tool)
    ns = _parse(["kg-supersede", "--subject", "mempalace", "--predicate", "version",
                 "--old-object", "3.5.0", "--new-object", "3.9.0", "--at", "2026-09-02",
                 "--json"])
    rc = cli_workspace.maybe_dispatch(ns)
    assert rc == 0
    assert seen["name"] == "kg_supersede"
    assert seen["args"] == {"subject": "mempalace", "predicate": "version",
                            "old_object": "3.5.0", "new_object": "3.9.0",
                            "at": "2026-09-02"}


def test_search_json_passes_date_window(monkeypatch):
    seen = {}

    def fake_call_tool(name, args=None, *, palace_path=None):
        seen["name"], seen["args"] = name, args
        return {"results": []}

    monkeypatch.setattr(cli_workspace, "call_tool", fake_call_tool)
    ns = _parse(["search-json", "wave schedule", "--since", "2026-08-01",
                 "--before", "2026-09-01"])
    assert cli_workspace.maybe_dispatch(ns) == 0
    assert seen["name"] == "search"
    assert seen["args"]["since"] == "2026-08-01"
    assert seen["args"]["before"] == "2026-09-01"


def test_search_json_window_defaults_to_none(monkeypatch):
    """Control: no flags -> None, which call_tool omits on the hub route (3df6ff5)."""
    seen = {}
    monkeypatch.setattr(cli_workspace, "call_tool",
                        lambda name, args=None, *, palace_path=None: seen.setdefault("args", args) or {"results": []})
    cli_workspace.maybe_dispatch(_parse(["search-json", "q"]))
    assert seen["args"]["since"] is None and seen["args"]["before"] is None
