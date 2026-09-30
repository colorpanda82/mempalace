"""DR30 / DR41 (2026-09-30): search fusion depth, drawer dedupe, read-filter wings.

DR30: tool_search handed search_memories a fixed depth of 100 (vector pool 300)
whatever the limit, and several chunks of one drawer could each take a top-N
slot. DR41: the read filters did not run canonical_wing, so a reader asking for
``wing_cowork`` missed what the M11 write paths filed under ``cowork``; legacy
drawers under a raw ``wing_*`` name must stay reachable.

Written to fail on the pre-patch tree (depth 100, duplicates kept, raw wings).
"""

import pytest

from mempalace import mcp_server


@pytest.fixture
def mcp(monkeypatch, config, kg, collection):
    monkeypatch.setattr(mcp_server, "_config", config)
    monkeypatch.setattr(mcp_server, "_get_kg", lambda *a, **kw: kg)
    return mcp_server


class _Capture:
    """Stands in for search_memories; records kwargs, returns canned hits."""

    def __init__(self, hits=None):
        self.calls = []
        self.hits = hits or []

    def __call__(self, query, **kw):
        self.calls.append(kw)
        return {"query": query, "filters": {}, "total_before_filter": len(self.hits),
                "results": [dict(h) for h in self.hits]}


def _hit(drawer_id, text, bm25=1.0):
    return {"drawer_id": drawer_id, "text": text, "wing": "cowork", "room": "diary",
            "similarity": 0.7, "bm25_score": bm25}


# ------------------------------------------------------------------ pool scaling


@pytest.mark.parametrize("limit, depth", [(1, 200), (5, 200), (50, 200), (60, 240),
                                          (100, 400), (1000, 400)])
def test_tool_search_depth_scales_with_limit(mcp, monkeypatch, limit, depth):
    cap = _Capture([_hit("d1", "alpha")])
    monkeypatch.setattr(mcp_server, "search_memories", cap)
    mcp.tool_search(query="alpha", limit=limit)
    assert cap.calls, "search_memories was not called"
    # limit is clamped to _MAX_RESULTS (100) before the depth is derived
    assert cap.calls[0]["n_results"] == depth


def test_depth_never_below_limit(mcp, monkeypatch):
    cap = _Capture()
    monkeypatch.setattr(mcp_server, "search_memories", cap)
    for limit in (1, 5, 20, 99, 100):
        mcp.tool_search(query="alpha", limit=limit)
    assert all(c["n_results"] >= 100 for c in cap.calls)
    assert [c["n_results"] for c in cap.calls] == sorted(c["n_results"] for c in cap.calls)


# ------------------------------------------------------------------ chunk dedupe


def test_chunks_of_one_drawer_take_one_slot(mcp, monkeypatch):
    hits = [
        _hit("A", "A chunk 0 best"),
        _hit("A", "A chunk 1"),
        _hit("B", "B"),
        _hit("C", "C"),
        _hit("A", "A chunk 2"),
        _hit("D", "D"),
        _hit("E", "E"),
        _hit("F", "F"),
    ]
    monkeypatch.setattr(mcp_server, "search_memories", _Capture(hits))
    out = mcp.tool_search(query="x", limit=5)
    assert [h["drawer_id"] for h in out["results"]] == ["A", "B", "C", "D", "E"]
    # the best-ranked chunk is the one kept
    assert out["results"][0]["text"] == "A chunk 0 best"


def test_hits_without_drawer_id_are_kept(mcp, monkeypatch):
    hits = [{"text": "no id 1"}, {"text": "no id 2"}, _hit("A", "a"), _hit("A", "a2")]
    monkeypatch.setattr(mcp_server, "search_memories", _Capture(hits))
    out = mcp.tool_search(query="x", limit=5)
    assert [h.get("drawer_id") for h in out["results"]] == [None, None, "A"]


def test_distinct_drawers_unchanged(mcp, monkeypatch):
    hits = [_hit(f"d{i}", f"t{i}") for i in range(8)]
    monkeypatch.setattr(mcp_server, "search_memories", _Capture(hits))
    out = mcp.tool_search(query="x", limit=5)
    assert [h["drawer_id"] for h in out["results"]] == ["d0", "d1", "d2", "d3", "d4"]


# ------------------------------------------------------------------ read filters (DR41)

CANON_WINGS = {"cowork", "brave-new-media", "claude-code", "jarvis-ops", "dsc"}


def test_read_wing_prefers_canonical_when_held(mcp, monkeypatch):
    rw = getattr(mcp_server, "_read_wing", None)
    assert rw is not None, "_read_wing missing"
    monkeypatch.setattr(mcp_server, "_known_wings", lambda: {"cowork", "wing_lily"})
    assert rw(None) is None
    assert rw("") == ""
    assert rw("wing_cowork") == "cowork"
    assert rw("WING_Cowork") == "cowork"
    # legacy data under the raw name stays reachable (upstream #2185 shape)
    assert rw("wing_lily") == "wing_lily"
    # neither form held: the name as given
    assert rw("wing_nothing_here") == "wing_nothing_here"


def test_read_wing_skips_lookup_for_canonical_names(mcp, monkeypatch):
    def boom():
        raise AssertionError("wing lookup ran for an already-canonical name")
    monkeypatch.setattr(mcp_server, "_known_wings", boom)
    assert mcp_server._read_wing("dsc") == "dsc"
    assert mcp_server._read_wing("tech-radar") == "tech-radar"


@pytest.mark.parametrize("raw, canon", [("wing_cowork", "cowork"), ("WING_BNM", "brave-new-media"),
                                        ("claude_code", "claude-code"), ("dsc", "dsc")])
def test_search_wing_filter_is_canonical(mcp, monkeypatch, raw, canon):
    monkeypatch.setattr(mcp_server, "_known_wings", lambda: CANON_WINGS)
    cap = _Capture()
    monkeypatch.setattr(mcp_server, "search_memories", cap)
    mcp.tool_search(query="x", wing=raw)
    assert cap.calls[0]["wing"] == canon


def test_search_without_wing_stays_unfiltered(mcp, monkeypatch):
    cap = _Capture()
    monkeypatch.setattr(mcp_server, "search_memories", cap)
    mcp.tool_search(query="x")
    assert cap.calls[0]["wing"] is None


def test_list_drawers_and_rooms_find_canonical_wing(mcp):
    added = mcp.tool_add_drawer(wing="cowork", room="dr41-room",
                                content="DR41 read filter probe drawer, unique text 7f3a.")
    assert added.get("success"), added
    assert "cowork" in mcp._known_wings()
    listed = mcp.tool_list_drawers(wing="wing_cowork")
    assert "error" not in listed, listed
    assert listed["total"] >= 1
    rooms = mcp.tool_list_rooms(wing="WING_Cowork")
    assert rooms["wing"] == "cowork"
    assert "dr41-room" in rooms["rooms"]


def test_tunnel_and_hallway_filters_are_canonical(mcp, monkeypatch):
    monkeypatch.setattr(mcp_server, "_known_wings", lambda: CANON_WINGS)
    seen = {}
    monkeypatch.setattr(mcp_server, "list_tunnels", lambda w: seen.setdefault("lt", w) or [])
    monkeypatch.setattr(mcp_server, "list_hallways", lambda w: seen.setdefault("lh", w) or [])
    monkeypatch.setattr(mcp_server, "find_tunnels",
                        lambda a, b, **kw: seen.setdefault("ft", (a, b)) or [])
    monkeypatch.setattr(mcp_server, "follow_tunnels",
                        lambda w, r, **kw: seen.setdefault("fo", (w, r)) or [])
    mcp.tool_list_tunnels(wing="wing_cowork")
    mcp.tool_list_hallways(wing="wing_jarvis")
    mcp.tool_find_tunnels(wing_a="wing_bnm", wing_b="Claude_Code")
    mcp.tool_follow_tunnels(wing="wing_cowork", room="diary")
    assert seen["lt"] == "cowork"
    assert seen["lh"] == "jarvis-ops"
    assert seen["ft"] == ("brave-new-media", "claude-code")
    assert seen["fo"] == ("cowork", "diary")
