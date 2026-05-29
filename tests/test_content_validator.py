"""Unit tests for the I6 content validator (lenient/quarantine policy)."""
import os, sys
sys.path.insert(0, "/Volumes/Jarvis Working/repos/mempalace-eg384")
from mempalace.content_validator import validate_content


def test_clean_passes():
    ok, _ = validate_content("SESSION:2026-05-29|test|***\ndecisions: none")
    assert ok

def test_tool_result_block_rejected():
    ok, reason = validate_content("done.\n<tool_result>x</tool_result>\nmore.")
    assert not ok and "tool_result" in reason

def test_base64_blob_rejected():
    ok, reason = validate_content("text\n" + "A" * 150 + "==\nmore")
    assert not ok and "base64" in reason

def test_long_line_rejected():
    # spaces break the base64-charset run so the line-length check is what fires
    ok, reason = validate_content("word " * 500)
    assert not ok and "exceeds" in reason

def test_validator_never_raises_on_bad_input():
    ok, _ = validate_content(None)
    assert ok  # non-str passes through, no exception
