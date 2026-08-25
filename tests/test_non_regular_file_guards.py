"""Non-regular-file guards — fork-scoped subset of upstream's module.

Upstream's tests/test_non_regular_file_guards.py (3.7.1 #2221 + 3.8.0 #2244)
covers guards across convo_miner, repair, hook_shell and format_miner that
this 3.4.1-based fork has not ported. This file tests exactly the guards the
fork did adopt: the miner read path, the project-scanner manifest gate, and
the sweeper's rglob classification. A FIFO with no writer is the probe: an
unguarded blocking open parks in the kernel forever, so each call runs on a
worker thread and must finish within a timeout.
"""

import os
import stat as stat_module
import threading
from pathlib import Path

import pytest

from mempalace.miner import _read_text_no_follow
from mempalace.project_scanner import _collect_manifest_names
from mempalace.sweeper import sweep_directory

pytestmark = pytest.mark.skipif(
    not hasattr(os, "mkfifo"), reason="FIFO guards are POSIX-only"
)


def _run_with_timeout(fn, timeout=10.0):
    """Run fn on a worker thread; fail the test instead of hanging the run."""
    result = {}

    def target():
        try:
            result["value"] = fn()
        except BaseException as exc:  # surfaced below
            result["error"] = exc

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout)
    assert not t.is_alive(), "call blocked — the FIFO guard is not working"
    if "error" in result:
        raise result["error"]
    return result["value"]


def test_read_text_no_follow_returns_none_for_writerless_fifo(tmp_path):
    fifo = tmp_path / "notes.md"
    os.mkfifo(fifo)

    out = _run_with_timeout(lambda: _read_text_no_follow(fifo, tmp_path))

    assert out is None


def test_read_text_no_follow_rejects_fifo_with_live_writer(tmp_path):
    """The refusal is on file type, not on writer absence."""
    fifo = tmp_path / "notes.md"
    os.mkfifo(fifo)
    # A writer's blocking open completes once the guarded reader opens the
    # FIFO; hold it from a thread so both sides can proceed.
    stop = threading.Event()

    def writer():
        try:
            fd = os.open(fifo, os.O_WRONLY)
            stop.wait(5)
            os.close(fd)
        except OSError:
            pass

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    try:
        out = _run_with_timeout(lambda: _read_text_no_follow(fifo, tmp_path))
        assert out is None
    finally:
        stop.set()


def test_read_text_no_follow_regular_file_roundtrip(tmp_path):
    """Regular files read back byte-identical, with the mtime of that read."""
    f = tmp_path / "doc.md"
    f.write_text("hello palace", encoding="utf-8")

    out = _run_with_timeout(lambda: _read_text_no_follow(f, tmp_path))

    assert out is not None
    content, mtime = out
    assert content == "hello palace"
    assert mtime == pytest.approx(f.stat().st_mtime)


def test_read_text_no_follow_refuses_symlink(tmp_path):
    target = tmp_path / "real.md"
    target.write_text("x", encoding="utf-8")
    link = tmp_path / "link.md"
    link.symlink_to(target)

    assert _run_with_timeout(lambda: _read_text_no_follow(link, tmp_path)) is None


def test_collect_manifest_names_skips_fifo_manifest(tmp_path):
    """A FIFO named like a manifest must not hang the scan or yield a name."""
    os.mkfifo(tmp_path / "package.json")

    names = _run_with_timeout(lambda: _collect_manifest_names(tmp_path))

    assert names == []


def test_sweep_directory_skips_fifo_and_books_failed_stat(tmp_path, capsys):
    """A FIFO *.jsonl is nothing to sweep; a dangling symlink is a booked
    failure — silence there would report success on an unreadable transcript."""
    convos = tmp_path / "convos"
    convos.mkdir()
    os.mkfifo(convos / "pipe.jsonl")
    (convos / "dangling.jsonl").symlink_to(convos / "gone-target.jsonl")
    palace = tmp_path / "palace"

    summary = _run_with_timeout(
        lambda: sweep_directory(str(convos), str(palace)), timeout=30.0
    )

    err = capsys.readouterr().err
    assert "SKIP: pipe.jsonl (not a regular file)" in err
    failures = summary.get("failures", [])
    assert any("dangling.jsonl" in str(f) for f in failures), (
        f"dangling symlink must be booked as a failure, got: {summary}"
    )
    assert not any("pipe.jsonl" in str(f) for f in failures), (
        "a FIFO is a skip, not a failure"
    )
