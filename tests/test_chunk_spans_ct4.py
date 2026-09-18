"""CT4 regression: every drawer-content slicer merges a runt tail.

Fork commit 6edaa68 (2026-07-22) added the min_chunk_size merge to
``_build_chunk_rows`` and shipped no tests. Two sibling slicers kept emitting
footer-only tail chunks for two months: 53 of them in the live palace on
2026-09-18, split 31 from ``tool_add_drawer`` and 22 from ``tool_diary_write``,
which matches those two paths exactly and is how they were found.

These tests pin the property at the seam that actually failed -- the number of
independent slicers -- not only at the one function that already worked. The
AST test is the load-bearing one: a future call site that re-inlines
``range(0, len(content), chunk_size)`` is exactly the regression this file
exists to catch, and no behavioural test on the three known paths would see it.

Copy into the fork's tests/ directory and run:
    pytest tests/test_chunk_spans_ct4.py -q
"""

import ast
import pathlib

import pytest

from mempalace import mcp_server


def _mcp_server_source() -> str:
    """Source of the whole mcp_server unit, whatever its layout.

    v3.10.0 split mcp_server.py into a package whose fragments are exec'd into
    one namespace, so ``mcp_server.__file__`` is now ``__init__.py`` and holds
    none of the tool bodies. Concatenate every fragment: the "one slicer"
    invariant is about the unit's namespace, not about one file.
    """
    p = pathlib.Path(mcp_server.__file__)
    if p.name != "__init__.py":
        return p.read_text()
    return "\n".join(f.read_text() for f in sorted(p.parent.glob("*.py")))


CHUNK = 800
MIN_CHUNK = 50


def _runt_content(tail_len):
    """Content that slices to two full chunks plus a tail of ``tail_len``."""
    return "x" * (CHUNK * 2) + "y" * tail_len


class TestChunkSpansMergesRunt:
    def test_short_tail_is_merged_into_predecessor(self):
        spans = mcp_server._chunk_spans(_runt_content(7), CHUNK)
        assert len(spans) == 2, "the 7-char tail should not survive as its own span"
        assert spans[-1][1].endswith("y" * 7)
        assert len(spans[-1][1]) == CHUNK + 7

    def test_tail_at_the_threshold_is_kept(self):
        """A tail of exactly min_chunk_size is NOT a runt — guard is `<`, not `<=`."""
        spans = mcp_server._chunk_spans(_runt_content(MIN_CHUNK), CHUNK)
        assert len(spans) == 3
        assert spans[-1][1] == "y" * MIN_CHUNK

    def test_no_content_is_lost_at_any_tail_length(self):
        """Merging must never drop the footer — that is why it merges, not drops."""
        for tail in (1, 7, 49, 50, 51, 200):
            content = _runt_content(tail)
            spans = mcp_server._chunk_spans(content, CHUNK)
            assert "".join(doc for _, doc in spans) == content, f"content lost at tail={tail}"

    def test_span_starts_stay_contiguous_and_positional(self):
        """chunk_index is derived as start // chunk_size, so starts must stay aligned."""
        spans = mcp_server._chunk_spans(_runt_content(7), CHUNK)
        assert [start for start, _ in spans] == [0, CHUNK]
        assert [start // CHUNK for start, _ in spans] == [0, 1]

    def test_empty_content_still_yields_one_span(self):
        assert mcp_server._chunk_spans("", CHUNK) == [(0, "")]

    def test_single_short_drawer_is_not_merged_away(self):
        """len(spans) > 1 guards this: a lone sub-threshold drawer must survive."""
        spans = mcp_server._chunk_spans("tiny", CHUNK)
        assert spans == [(0, "tiny")]


class TestOnlyOneSlicerExists:
    """The actual CT4 defect was duplication, so pin the count, not the behaviour.

    A behavioural test on the three known paths passes happily while a fourth
    copy is added elsewhere — which is the precise way 6edaa68's fix rotted.
    """

    def test_no_call_site_re_inlines_the_slice(self):
        source = _mcp_server_source()
        tree = ast.parse(source)

        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not (isinstance(node.func, ast.Name) and node.func.id == "range"):
                continue
            if len(node.args) != 3:
                continue
            step = node.args[2]
            if not (isinstance(step, ast.Name) and step.id == "chunk_size"):
                continue
            # range(0, len(<something>), chunk_size) — the slicer shape.
            stop = node.args[1]
            if isinstance(stop, ast.Call) and isinstance(stop.func, ast.Name) and stop.func.id == "len":
                offenders.append(node.lineno)

        helper_line = next(
            n.lineno
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "_chunk_spans"
        )
        helper_end = next(
            n.end_lineno
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "_chunk_spans"
        )

        outside = [ln for ln in offenders if not helper_line <= ln <= helper_end]
        assert not outside, (
            f"drawer content is sliced outside _chunk_spans at line(s) {outside}; "
            "route it through the helper so the min_chunk_size merge applies (CT4)"
        )

    def test_the_three_known_call_sites_use_the_helper(self):
        source = _mcp_server_source()
        assert source.count("_chunk_spans(") >= 4, (
            "expected the definition plus three call sites: _build_chunk_rows, "
            "tool_add_drawer's oversized branch, tool_diary_write's entry branch"
        )


class TestMutantWouldFail:
    """Prove the tests can fail — a guard nobody has seen go red is not a guard."""

    def test_merge_is_what_makes_the_first_test_pass(self, monkeypatch):
        class _NoMinChunk:
            min_chunk_size = 0

        monkeypatch.setattr(mcp_server, "_config", _NoMinChunk())
        spans = mcp_server._chunk_spans(_runt_content(7), CHUNK)
        assert len(spans) == 3, (
            "with min_chunk_size=0 the runt must survive — if this fails, the "
            "merge is unconditional and the threshold is not being read"
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
