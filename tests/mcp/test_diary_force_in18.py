"""IN18: diary_write's ``force`` must survive the MCP argument whitelist and the CLI.

Before the fix the diary_write schema had no ``force`` property and the handler
takes no ``**kwargs``, so ``call_tool`` dropped ``force=True`` before the handler
ran; an MCP caller (or a CLI verb forwarded to the hub) could never file an
intended duplicate. The in-process path was covered by
``test_diary_dedupe_ct6.py::test_force_overrides``; this covers the protocol path.
"""

import argparse

from mempalace import cli_workspace, mcp_server


def test_schema_declares_force_boolean():
    props = mcp_server.TOOLS["mempalace_diary_write"]["input_schema"]["properties"]
    assert props["force"]["type"] == "boolean"


def test_tools_call_passes_force_to_the_handler(monkeypatch):
    seen = {}

    def fake(agent_name, entry, topic="general", wing="", force=False):
        seen.update(agent_name=agent_name, entry=entry, force=force)
        return {"success": True}

    monkeypatch.setitem(mcp_server.TOOLS["mempalace_diary_write"], "handler", fake)
    mcp_server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "mempalace_diary_write",
                "arguments": {"agent_name": "in18", "entry": "same text", "force": True},
            },
        }
    )
    assert seen.get("force") is True, f"force did not reach the handler: {seen}"


def test_cli_diary_write_forwards_force(monkeypatch, capsys):
    seen = {}

    def fake_invoke(args, fn_name, **kwargs):
        seen.update(fn=fn_name, **kwargs)
        return {"success": True}

    monkeypatch.setattr(cli_workspace, "_invoke", fake_invoke)
    args = argparse.Namespace(
        agent_name="in18", entry="same text", topic="general", wing="", force=True, json=True
    )
    cli_workspace._h_diary_write(args)
    assert seen["fn"] == "tool_diary_write"
    assert seen["force"] is True
