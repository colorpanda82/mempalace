# mempalace/content_validator.py
"""Content validation for text entering MemPalace (I6).

Blocks the Trojan Hippo injection/exfil class on the diary and drawer write
paths. Lenient policy (I6 decision 1A): flag known injection/exfil patterns and
QUARANTINE the offending text rather than hard-reject, so a naive heuristic
never silently drops a legitimate (dense AAAK) entry.

CONTRACT: validate_content never raises; quarantine_content never raises (wraps
all I/O). Callers must quarantine-and-return, never propagate an exception --
Leg B (the diary flusher) is the compiled, uneditable jarvis-agent goroutine and
a raise could crash-loop it.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re

log = logging.getLogger(__name__)

_BASE64_BLOB = re.compile(r"[A-Za-z0-9+/]{100,}={0,2}")
_TOOL_RESULT_BLOCK = re.compile(r"<tool_result\b.*?</tool_result>", re.DOTALL | re.IGNORECASE)
_MAX_LINE_LENGTH = 2000
_QUARANTINE_DIR_ENV = "MEMPALACE_CONTENT_QUARANTINE"


_WRAP_TARGET = _MAX_LINE_LENGTH - 100  # 1900: headroom under the hard reject


def _hard_wrap(s, limit):
    if len(s) <= limit:
        return [s]
    return [s[i:i + limit] for i in range(0, len(s), limit)]


def wrap_long_lines(text, limit=_WRAP_TARGET):
    """Losslessly wrap any line longer than limit so validate_content's per-line
    length check (_MAX_LINE_LENGTH) never trips. Splits on AAAK ' | ' pipe
    boundaries first, then hard-wraps any remaining over-limit run. Content is
    preserved (a ' | ' boundary becomes a newline); idempotent; never raises
    (flusher safety). chr(10) is the newline character."""
    try:
        if not isinstance(text, str):
            return text
        out = []
        for line in text.split(chr(10)):
            if len(line) <= limit:
                out.append(line)
                continue
            cur = ""
            for i, seg in enumerate(line.split(" | ")):
                piece = seg if i == 0 else " | " + seg
                if cur and len(cur) + len(piece) > limit:
                    out.extend(_hard_wrap(cur, limit))
                    cur = seg
                else:
                    cur += piece
            if cur:
                out.extend(_hard_wrap(cur, limit))
        return chr(10).join(out)
    except Exception:
        return text


def _quarantine_dir() -> str:
    default = os.path.expanduser(
        "~/Library/Mobile Documents/com~apple~CloudDocs/Workspace/_manager/pending-diaries/quarantine"
    )
    return os.environ.get(_QUARANTINE_DIR_ENV, default)


def validate_content(text, source: str = "unknown"):
    """Return (is_valid, reason). is_valid=False => caller should quarantine, not insert. Never raises."""
    try:
        if not isinstance(text, str):
            return True, "ok (non-str passed through)"
        if _TOOL_RESULT_BLOCK.search(text):
            return False, "contains <tool_result> block (potential injection vector)"
        if _BASE64_BLOB.search(text):
            return False, "contains base64 blob >=100 chars (potential exfil payload)"
        for i, line in enumerate(text.splitlines(), 1):
            if len(line) > _MAX_LINE_LENGTH:
                return False, "line %d exceeds %d chars" % (i, _MAX_LINE_LENGTH)
        return True, "ok"
    except Exception as exc:  # never raise into a caller (flusher safety)
        log.warning("[content-validator] validate error (%s); passing through: %s", source, exc)
        return True, "ok (validator error, passed through)"


def quarantine_content(text, reason: str, source: str = "unknown") -> str:
    """Write rejected content to the quarantine dir (idempotent by content hash). Returns path or '' on failure. Never raises."""
    try:
        import datetime
        qdir = _quarantine_dir()
        os.makedirs(qdir, exist_ok=True)
        digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]
        path = os.path.join(qdir, "quarantine-%s.md" % digest)
        if not os.path.exists(path):
            ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            header = "# Quarantined content\n# Reason: %s\n# Source: %s\n# Timestamp: %s\n\n" % (reason, source, ts)
            with open(path, "w", encoding="utf-8") as f:
                f.write(header + text)
        log.warning("[content-validator] quarantined (%s): %s [%s]", reason, path, source)
        return path
    except Exception as exc:
        log.warning("[content-validator] quarantine failed (%s): %s", source, exc)
        return ""
