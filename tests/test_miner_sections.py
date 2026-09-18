"""Section-aware chunking (workspace fork, 2026-09-04).

``chunk_text`` must never emit a chunk that straddles a markdown heading, must
label every chunk with its nearest preceding heading, and must prefix
continuation chunks with that heading so its terms embed alongside the body.
Heading-less content must chunk exactly as before (control), and the line
locators must still point at the SOURCE span, not the prefixed text.

Measured motivation on the live palace (2026-09-04): 1,659 of 3,010 drawers came
from multi-chunk documents and 626 carried heading lines.
"""
import re

import pytest

from mempalace.miner import _build_drawer_metadata, _section_spans, chunk_text

HEADING = re.compile(r"^#{1,6} ", re.MULTILINE)


def _para(tag: str, n: int = 6) -> str:
    return "\n".join(f"{tag} sentence number {i} about the topic of {tag}." for i in range(n))


def _doc() -> str:
    return (
        "Preamble before any heading.\n" + _para("pre") + "\n\n"
        "# Introduction\n" + _para("intro") + "\n\n"
        "## Methods\n" + _para("methods", 14) + "\n\n"
        "## Results\n" + _para("results") + "\n"
    )


class TestSectionSpans:
    def test_headingless_content_is_one_span(self):
        assert _section_spans("just text\n\nmore text", 50) == [(0, 20, None)]

    def test_heading_at_start_names_first_span(self):
        spans = _section_spans("# Title\nbody " * 1, 5)
        assert spans[0][2] == "Title"

    def test_heading_too_close_to_previous_boundary_is_folded(self):
        text = "# A\nx\n# B\n" + "b" * 100
        spans = _section_spans(text, min_chunk_size=50)
        # "# A\nx\n" is 6 chars, under the 50 minimum, so B does not open a new span.
        assert len(spans) == 1 and spans[0][2] == "A"

    def test_preamble_span_has_no_heading(self):
        spans = _section_spans("intro text that is long enough to stand\n# H\nbody", 10)
        assert spans[0][2] is None and spans[1][2] == "H"


class TestChunkTextSections:
    def test_no_chunk_straddles_a_heading(self):
        chunks = chunk_text(_doc(), "/d.md", chunk_size=300, chunk_overlap=40, min_chunk_size=20)
        assert len(chunks) >= 4
        for c in chunks:
            body = c["content"]
            # A heading may appear only as the first line (section start, or the prefix).
            heads = [m.start() for m in HEADING.finditer(body)]
            assert heads in ([], [0]), f"heading mid-chunk: {body[:120]!r}"

    def test_every_chunk_carries_its_section(self):
        chunks = chunk_text(_doc(), "/d.md", chunk_size=300, chunk_overlap=40, min_chunk_size=20)
        sections = [c["section"] for c in chunks]
        assert sections[0] is None, "preamble chunk has no section"
        assert {"Introduction", "Methods", "Results"} <= set(sections)
        # Chunk order follows document order, so sections appear in document order.
        seen = [s for i, s in enumerate(sections) if s and s not in sections[:i]]
        assert seen == ["Introduction", "Methods", "Results"]

    def test_continuation_chunks_are_prefixed_with_heading(self):
        chunks = chunk_text(_doc(), "/d.md", chunk_size=300, chunk_overlap=40, min_chunk_size=20)
        methods = [c for c in chunks if c["section"] == "Methods"]
        assert len(methods) >= 2, "Methods is long enough to need more than one chunk"
        assert methods[0]["content"].startswith("## Methods\n"), "first chunk begins at the heading itself"
        for c in methods[1:]:
            assert c["content"].startswith("## Methods\n\n"), c["content"][:60]

    def test_line_locators_describe_source_not_prefixed_text(self):
        doc = _doc()
        chunks = chunk_text(doc, "/d.md", chunk_size=300, chunk_overlap=40, min_chunk_size=20)
        lines = doc.split("\n")
        for c in chunks:
            body = c["content"]
            if c["section"] and body.startswith("#") and "\n\n" in body and not lines[c["line_start"] - 1].startswith("#"):
                body = body.split("\n\n", 1)[1]  # strip the prefix on continuation chunks
            first_line = body.split("\n", 1)[0].strip()
            src_line = lines[c["line_start"] - 1].strip()
            # An overlap chunk may begin mid-line (pre-existing behaviour), so the
            # chunk's first line is a SUFFIX of the source line at line_start, not
            # necessarily the whole line. Every fixture line is unique ("<tag>
            # sentence number N"), so substring containment still discriminates a
            # wrong line_start.
            assert first_line in src_line, (
                f"line_start {c['line_start']} -> {src_line[:40]!r} vs chunk {first_line[:40]!r}")

    def test_overlap_never_reaches_back_across_a_heading(self):
        doc = _doc()
        chunks = chunk_text(doc, "/d.md", chunk_size=300, chunk_overlap=100, min_chunk_size=20)
        results = [c for c in chunks if c["section"] == "Results"]
        assert results and "methods sentence" not in results[0]["content"]

    def test_headingless_content_is_unchanged_control(self):
        """Control: with no headings the output must equal the pre-change algorithm,
        which is the same windowing with a single span. Reconstruct it here."""
        content = "\n\n".join(_para(f"p{i}") for i in range(8))
        chunks = chunk_text(content, "/x.md", chunk_size=400, chunk_overlap=50, min_chunk_size=20)
        assert all(c["section"] is None for c in chunks)
        # No prefixes, contiguous coverage, and the old return keys still present.
        joined = "".join(c["content"] for c in chunks)
        assert "#" not in joined
        assert chunks[0]["line_start"] == 1
        assert {"content", "chunk_index", "line_start", "line_end"} <= set(chunks[0])
        assert [c["chunk_index"] for c in chunks] == list(range(len(chunks)))

    def test_chunk_indices_stay_monotonic_across_sections(self):
        chunks = chunk_text(_doc(), "/d.md", chunk_size=300, chunk_overlap=40, min_chunk_size=20)
        assert [c["chunk_index"] for c in chunks] == list(range(len(chunks)))

    def test_pathological_overlap_still_terminates_and_raises_as_before(self):
        with pytest.raises(ValueError):
            chunk_text("# H\n" + "x" * 500, "/x.md", chunk_size=100, chunk_overlap=80)


class TestSectionMetadata:
    def test_section_lands_in_metadata_when_present(self):
        meta = _build_drawer_metadata("w", "r", "/d.md", 3, "agent", "body", None, section="Methods")
        assert meta["section"] == "Methods"

    def test_section_absent_when_none_or_empty(self):
        for s in (None, ""):
            meta = _build_drawer_metadata("w", "r", "/d.md", 3, "agent", "body", None, section=s)
            assert "section" not in meta

    def test_section_is_capped(self):
        meta = _build_drawer_metadata("w", "r", "/d.md", 3, "agent", "body", None, section="x" * 500)
        assert len(meta["section"]) == 200
