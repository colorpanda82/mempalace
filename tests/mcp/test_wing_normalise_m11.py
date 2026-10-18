"""M11 (2026-09-30): the MCP write paths canonicalise the wing.

Malformed ``wing_*`` names used to mint duplicate wings (37 drawers moved by
hand on 2026-09-30). These tests pin the rule in ``mempalace/wing_aliases.py``
and prove it is applied by tool_add_drawer, tool_update_drawer,
tool_diary_write (explicit and default wing) and tool_diary_read.
"""

import chromadb
import pytest

from mempalace import mcp_server
from mempalace.wing_aliases import WING_ALIASES, canonical_wing

# Every wing live in the palace on 2026-09-30 (mempalace_list_wings). The rule
# must leave each of them unchanged, or deploying it would split a live wing.
LIVE_WINGS_2026_09_30 = [
    "aidendo", "black-cat-media", "bnm-physician-landing", "brave-new-media",
    "bravenew", "claude-code", "clients", "competitive-intel", "content-pipeline",
    "cowork", "cris-smile-studio", "dsc", "infrastructure", "innotech",
    "jarvis-infra", "jarvis-ops", "jarvis-setup", "marketing-ops", "roc",
    "roc-emails", "scratchpad", "secondbrain", "smoke-test", "tech-radar",
    "video-studio", "workspace",
]


@pytest.mark.parametrize(
    "raw, expected",
    [
        # the five malformed wings merged by hand on 2026-09-30
        ("wing_cowork", "cowork"),
        ("wing_bnm", "brave-new-media"),
        ("wing_jarvis", "jarvis-ops"),
        ("wing_marketing_ops", "marketing-ops"),
        ("wing_video_studio", "video-studio"),
        # diary-flusher.py WING_ALIASES
        ("wing_claude", "cowork"),
        ("client-bnm", "brave-new-media"),
        ("project-jarvis-ops", "jarvis-ops"),
        # case and separator variants
        ("WING_Cowork", "cowork"),
        ("Wing-Marketing-Ops", "marketing-ops"),
        ("Brave-New-Media", "brave-new-media"),
        ("claude_code", "claude-code"),
        # unknown well-formed wings pass through
        ("some-new-client", "some-new-client"),
        ("some_new_client", "some-new-client"),
        # a bare "wing" is not stripped to empty
        ("wing", "wing"),
        ("wing_", "wing-"),
    ],
)
def test_canonical_wing(raw, expected):
    assert canonical_wing(raw) == expected


@pytest.mark.parametrize("wing", LIVE_WINGS_2026_09_30)
def test_live_wings_are_fixed_points(wing):
    assert canonical_wing(wing) == wing


def test_alias_targets_are_canonical():
    for target in WING_ALIASES.values():
        assert canonical_wing(target) == target


def test_idempotent():
    for raw in ["wing_bnm", "WING_video_studio", "client-bnm", "x_y", "wing_wing_x"]:
        once = canonical_wing(raw)
        assert canonical_wing(once) == once


# ---------------------------------------------------------------- tool paths


@pytest.fixture
def mcp(monkeypatch, config, kg, collection):
    monkeypatch.setattr(mcp_server, "_config", config)
    monkeypatch.setattr(mcp_server, "_get_kg", lambda *a, **kw: kg)
    return mcp_server


def _wings_for(palace_path, where):
    client = chromadb.PersistentClient(path=palace_path)
    col = client.get_collection("mempalace_drawers")
    got = col.get(where=where, include=["metadatas"])
    return sorted({(m or {}).get("wing") for m in got["metadatas"]})


def test_add_drawer_canonicalises_wing(mcp, palace_path):
    res = mcp.tool_add_drawer(wing="wing_cowork", room="m11", content="M11 add_drawer probe text")
    assert res.get("success"), res
    assert res["wing"] == "cowork"
    assert _wings_for(palace_path, {"room": "m11"}) == ["cowork"]
    wings = mcp.tool_list_wings().get("wings", {})
    assert "cowork" in wings and "wing_cowork" not in wings


def test_update_drawer_canonicalises_wing(mcp, palace_path):
    res = mcp.tool_add_drawer(wing="cowork", room="m11u", content="M11 update_drawer probe text")
    assert res.get("success"), res
    upd = mcp.tool_update_drawer(res["drawer_id"], wing="wing_Marketing_Ops")
    assert upd.get("success"), upd
    assert _wings_for(palace_path, {"room": "m11u"}) == ["marketing-ops"]


def test_update_drawer_alias_to_same_wing_is_noop_move(mcp, palace_path):
    res = mcp.tool_add_drawer(wing="brave-new-media", room="m11n", content="M11 noop move probe")
    upd = mcp.tool_update_drawer(res["drawer_id"], wing="wing_bnm")
    assert upd.get("success"), upd
    assert _wings_for(palace_path, {"room": "m11n"}) == ["brave-new-media"]


def test_diary_write_explicit_wing(mcp, palace_path):
    res = mcp.tool_diary_write(agent_name="m11tester", entry="M11 diary explicit wing probe", wing="wing_jarvis")
    assert res.get("success"), res
    assert _wings_for(palace_path, {"agent": "m11tester"}) == ["jarvis-ops"]
    # a reader using the same malformed name still finds it
    read = mcp.tool_diary_read(agent_name="m11tester", wing="wing_jarvis")
    assert read.get("showing", 0) >= 1, read


def test_diary_write_default_wing(mcp, palace_path):
    # upstream default is f"wing_{agent_name}"; for agent "claude" that was the
    # wing_claude alias the flusher already maps to cowork
    res = mcp.tool_diary_write(agent_name="claude", entry="M11 diary default wing probe")
    assert res.get("success"), res
    assert _wings_for(palace_path, {"agent": "claude"}) == ["cowork"]
