"""Reads during the hyphen -> underscore wing transition (fork, 3.12 port).

Since the 3.12 port the MCP writes store upstream's slug (``jarvis_ops``),
but a live palace still holds hyphen wings (``jarvis-ops``) and legacy alias
wings (``wing_claude``, ``bnm``) until ``mempalace ws-migrate-wings`` re-keys
them. A read filtered on either spelling must return every spelling's drawers.
"""

from _mcp_server_helpers import _patch_mcp_server

LEGACY = {
    "legacy_hyphen": "jarvis-ops",
    "legacy_prefix": "wing_jarvis",
    "legacy_alias": "jarvis",
    "legacy_claude": "wing_claude",
    "legacy_bnm": "bnm",
}


def _seed(collection):
    collection.add(
        ids=list(LEGACY),
        documents=[f"transition probe filed under {wing}" for wing in LEGACY.values()],
        metadatas=[
            {
                "wing": wing,
                "room": "notes",
                "source_file": f"{rid}.md",
                "filed_at": "2026-01-01T00:00:00",
            }
            for rid, wing in LEGACY.items()
        ],
    )


def _mcp(monkeypatch, config, kg, collection):
    _seed(collection)
    _patch_mcp_server(monkeypatch, config, kg)
    from mempalace import mcp_server

    written = mcp_server.tool_add_drawer(
        wing="jarvis-ops", room="notes", content="transition probe filed after the port"
    )
    assert written.get("success"), written
    assert written["wing"] == "jarvis_ops"
    return mcp_server


JARVIS_STORED = {"jarvis_ops", "jarvis-ops", "wing_jarvis", "jarvis"}


def test_list_drawers_reads_every_spelling(monkeypatch, config, palace_path, collection, kg):
    mcp = _mcp(monkeypatch, config, kg, collection)
    for asked in ("jarvis_ops", "jarvis-ops", "Jarvis Ops", "wing_jarvis"):
        listed = mcp.tool_list_drawers(wing=asked, limit=50)
        assert {d["wing"] for d in listed["drawers"]} == JARVIS_STORED, (asked, listed)
        assert listed["total"] == 4, (asked, listed)


def test_search_reads_every_spelling(monkeypatch, config, palace_path, collection, kg):
    mcp = _mcp(monkeypatch, config, kg, collection)
    for asked in ("jarvis_ops", "jarvis-ops"):
        found = mcp.tool_search(query="transition probe filed", wing=asked, limit=20)
        assert {h["wing"] for h in found["results"]} == JARVIS_STORED, (asked, found)


def test_cli_search_reads_every_spelling(monkeypatch, config, palace_path, collection, kg):
    _mcp(monkeypatch, config, kg, collection)
    from mempalace.searcher import search_memories

    for asked in ("jarvis_ops", "jarvis-ops"):
        out = search_memories("transition probe filed", palace_path, wing=asked, n_results=20)
        assert {h["wing"] for h in out["results"]} == JARVIS_STORED, (asked, out)


def test_alias_only_legacy_wing_reads_through_canonical(
    monkeypatch, config, palace_path, collection, kg
):
    """wing_claude -> cowork and bnm -> brave_new_media only through the alias
    table: the drawers under the legacy name are still read, both ways."""
    mcp = _mcp(monkeypatch, config, kg, collection)
    assert mcp.tool_add_drawer(wing="cowork", room="notes", content="cowork after the port")[
        "success"
    ]
    for asked in ("cowork", "wing_claude", "wing_cowork", "claude"):
        listed = mcp.tool_list_drawers(wing=asked, limit=50)
        assert {d["wing"] for d in listed["drawers"]} == {"cowork", "wing_claude"}, (asked, listed)
    for asked in ("brave_new_media", "brave-new-media", "bnm", "wing_bnm"):
        listed = mcp.tool_list_drawers(wing=asked, limit=50)
        assert {d["wing"] for d in listed["drawers"]} == {"bnm"}, (asked, listed)
    found = mcp.tool_search(query="transition probe filed", wing="cowork", limit=20)
    assert {h["wing"] for h in found["results"]} >= {"wing_claude"}, found


def test_list_rooms_and_diary_read_span_spellings(monkeypatch, config, palace_path, collection, kg):
    mcp = _mcp(monkeypatch, config, kg, collection)
    assert mcp.tool_list_rooms(wing="jarvis-ops")["rooms"].get("notes") == 4
    collection.add(
        ids=["legacy_diary"],
        documents=["legacy hyphen diary entry"],
        metadatas=[
            {
                "wing": "jarvis-ops",
                "room": "diary",
                "agent": "probe",
                "topic": "general",
                "date": "2026-01-01",
                "filed_at": "2026-01-01T00:00:00",
            }
        ],
    )
    assert mcp.tool_diary_write("probe", "new underscore diary entry", wing="jarvis-ops")["success"]
    for asked in ("jarvis_ops", "jarvis-ops"):
        read = mcp.tool_diary_read("probe", last_n=10, wing=asked)
        assert {e["content"] for e in read["entries"]} == {
            "legacy hyphen diary entry",
            "new underscore diary entry",
        }, (asked, read)


def test_unrelated_wings_are_not_pulled_in():
    from mempalace.config import wing_aliases

    known = {"cowork", "wing_claude", "claude_code", "claude-code", "bnm", "bnm_hive", "bnm-hive"}
    assert set(wing_aliases("cowork", known)) & known == {"cowork", "wing_claude"}
    assert set(wing_aliases("brave_new_media", known)) & known == {"bnm"}
    assert set(wing_aliases("bnm_hive", known)) & known == {"bnm_hive", "bnm-hive"}
