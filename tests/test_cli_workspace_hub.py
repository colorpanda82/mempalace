"""cli_workspace.call_tool: hub-first routing for every workspace writer.

Why this exists: since v3.9.0 a local palace has one process-lifetime writer
(#2079). The write-worker, diary-flusher and janitor prune used to import
``tool_*`` directly; under a running ``mempalace serve`` hub those writes are
refused ("palace is held by PID"). ``call_tool`` is the one seam they all go
through, so these tests pin its routing contract rather than any tool's logic.
"""
import json
import sys

from mempalace import cli_workspace


class _FakeHubClient:
    def __init__(self, response, hub=("http://127.0.0.1:8766", {"X": "1"})):
        self.response = response
        self.hub = hub
        self.sent = []

    def discover_hub(self, palace_path):
        return self.hub

    def forward_json_rpc(self, base_url, headers, request, **_):
        self.sent.append((base_url, headers, request))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _wire(monkeypatch, fake, tmp_path):
    import mempalace
    monkeypatch.setattr(mempalace, "hub_client", fake, raising=False)
    monkeypatch.setitem(sys.modules, "mempalace.hub_client", fake)
    # The in-process route pins MEMPALACE_PALACE_PATH for mcp_server; keep that
    # inside the test so later test modules do not inherit a tmp palace.
    monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(tmp_path))
    return str(tmp_path)


def _tool_response(payload, is_error=False):
    return {"jsonrpc": "2.0", "id": 1,
            "result": {"content": [{"type": "text", "text": json.dumps(payload)}],
                       "isError": is_error}}


def test_forwards_to_hub_with_prefixed_name_and_unwraps_result(monkeypatch, tmp_path):
    fake = _FakeHubClient(_tool_response({"success": True, "drawer_id": "d1"}))
    palace = _wire(monkeypatch, fake, tmp_path)

    out = cli_workspace.call_tool("add_drawer", {"wing": "w", "room": "r", "content": "c"},
                                  palace_path=palace)

    assert out["success"] is True and out["drawer_id"] == "d1"
    assert out["via"] == "hub"
    base_url, headers, request = fake.sent[0]
    assert base_url == "http://127.0.0.1:8766"
    assert request["method"] == "tools/call"
    assert request["params"]["name"] == "mempalace_add_drawer"
    assert request["params"]["arguments"] == {"wing": "w", "room": "r", "content": "c"}


def test_none_arguments_are_omitted_on_the_hub_route(monkeypatch, tmp_path):
    """The hub type-checks arguments; a None meaning 'not given' must not be sent
    (2026-09-02: max_distance=None broke every search-json in golden recall)."""
    fake = _FakeHubClient(_tool_response({"results": []}))
    palace = _wire(monkeypatch, fake, tmp_path)

    cli_workspace.call_tool("search", {"query": "q", "limit": 5, "wing": None,
                                       "room": None, "max_distance": None}, palace_path=palace)

    assert fake.sent[0][2]["params"]["arguments"] == {"query": "q", "limit": 5}


def test_hub_json_rpc_error_maps_to_tool_error_shape(monkeypatch, tmp_path):
    refusal = {"jsonrpc": "2.0", "id": 1, "error": {
        "code": -32001, "message": "Peer MCP writer active",
        "data": {"reason": "held by PID 1"}}}
    palace = _wire(monkeypatch, _FakeHubClient(refusal), tmp_path)

    out = cli_workspace.call_tool("add_drawer", {"content": "c"}, palace_path=palace)

    assert out["success"] is False
    assert "Peer MCP writer active" in out["error"] and "held by PID 1" in out["error"]


def test_hub_transport_failure_is_not_replayed_in_process(monkeypatch, tmp_path):
    """A write that may have reached the hub must surface, never re-run locally."""
    palace = _wire(monkeypatch, _FakeHubClient(ConnectionError("boom")), tmp_path)
    import mempalace.mcp_server as m
    called = []
    monkeypatch.setattr(m, "tool_add_drawer", lambda **kw: called.append(kw) or {"success": True})

    out = cli_workspace.call_tool("add_drawer", {"content": "c"}, palace_path=palace)

    assert out["success"] is False and "hub call failed" in out["error"]
    assert called == []


def test_no_hub_calls_tool_in_process_and_awaits_coroutines(monkeypatch, tmp_path):
    palace = _wire(monkeypatch, _FakeHubClient(None, hub=None), tmp_path)
    import mempalace.mcp_server as m

    async def fake_diary_write(**kw):
        return {"success": True, "echo": kw}

    monkeypatch.setattr(m, "tool_diary_write", fake_diary_write)

    out = cli_workspace.call_tool("diary_write", {"agent_name": "a", "entry": "e"},
                                  palace_path=palace)

    assert out == {"success": True, "echo": {"agent_name": "a", "entry": "e"}}


def test_is_error_result_without_error_key_is_normalised(monkeypatch, tmp_path):
    palace = _wire(monkeypatch,
                   _FakeHubClient(_tool_response({"note": "quarantined"}, is_error=True)),
                   tmp_path)

    out = cli_workspace.call_tool("add_drawer", {"content": "c"}, palace_path=palace)

    assert out["success"] is False and "quarantined" in out["error"]


def test_search_json_default_max_distance_is_unbounded():
    """A 1.5 default would read as a caller-set bound on v3.9.0 and switch the
    fusion-depth override off; the parser must leave it None."""
    import argparse
    sub = argparse.ArgumentParser().add_subparsers(dest="command")
    cli_workspace.register(sub)
    ns = sub.choices["search-json"].parse_args(["q"])
    assert ns.max_distance is None


def test_global_palace_flag_survives_verb_subparser():
    """`mempalace --palace X add-drawer ...` must keep X: a subparser default of
    None silently overwrote the parent's value (measured 2026-09-02), so hub
    discovery ran against the config palace while the write hit the env one."""
    import argparse
    top = argparse.ArgumentParser()
    top.add_argument("--palace")
    sub = top.add_subparsers(dest="command")
    cli_workspace.register(sub)
    ns = top.parse_args(["--palace", "/p/global", "add-drawer", "--wing", "w", "--room", "r", "--content", "c"])
    assert cli_workspace._resolve_palace(ns) == "/p/global"
    ns = top.parse_args(["add-drawer", "--palace", "/p/verb", "--wing", "w", "--room", "r", "--content", "c"])
    assert cli_workspace._resolve_palace(ns) == "/p/verb"


def test_default_palace_prefers_env_over_config_snapshot(monkeypatch, tmp_path):
    """cli.py exports MEMPALACE_PALACE_PATH after mempalace.config was imported,
    so the env var, not the import-time config snapshot, is the truth."""
    monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(tmp_path / "envpalace"))
    assert cli_workspace._default_palace() == str(tmp_path / "envpalace")
    ns = type("NS", (), {})()  # no .palace attribute at all (SUPPRESS)
    assert cli_workspace._resolve_palace(ns) == str(tmp_path / "envpalace")
