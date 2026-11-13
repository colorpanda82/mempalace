"""Canonical wing names for the MCP write paths (fork-local, 2026-09-30).

Background
----------
The MCP write tools (``tool_add_drawer``, ``tool_update_drawer``,
``tool_diary_write``) accepted any wing string that passed
``sanitize_name``. Writers that used the upstream AAAK convention
(``wing_<project>``) or underscores minted duplicate wings next to the
hyphenated canonical ones: on 2026-09-30, 37 drawers had to be moved by hand
out of ``wing_cowork``, ``wing_bnm``, ``wing_jarvis``, ``wing_marketing_ops``
and ``wing_video_studio``.

Rule
----
1. lowercase, underscores -> hyphens;
2. strip leading ``wing-`` prefixes (the old ``wing_`` after step 1);
3. map through ``WING_ALIASES``.

A well-formed name that is not an alias passes through unchanged: this
normalises, it never rejects, so no existing writer breaks. Every wing live
in the palace on 2026-09-30 is already lowercase-hyphenated, so the rule
leaves all of them as they are.

Source of truth
---------------
``WING_ALIASES`` is a hand copy of ``WING_ALIASES`` in the workspace's
``scripts/diary-flusher.py`` (keys shown here in their normalised form),
plus the two merges applied by hand on 2026-09-30 that normalisation alone
does not reach (``wing_bnm``, ``wing_jarvis``). The installed package cannot
read the iCloud workspace, so a new alias needs a fork edit plus a redeploy,
the same arrangement as ``kg_entities.MERGES``.
"""

from __future__ import annotations

_PREFIX = "wing-"

# normalised key -> canonical wing
WING_ALIASES = {
    # diary-flusher.py WING_ALIASES
    "claude": "cowork",  # wing_claude
    # "wing_cowork" -> "cowork" is handled by prefix stripping alone
    "client-bnm": "brave-new-media",
    "project-jarvis-ops": "jarvis-ops",
    # 2026-09-30 manual merge (wing_marketing_ops and wing_video_studio
    # reach marketing-ops / video-studio through normalisation alone)
    "bnm": "brave-new-media",  # wing_bnm
    "jarvis": "jarvis-ops",  # wing_jarvis
}


def normalise_wing(name: str) -> str:
    """Lowercase, underscores to hyphens, strip leading ``wing-`` prefixes."""
    w = name.strip().lower().replace("_", "-")
    # a loop, not an if: "wing_wing_x" must not become "wing-x" on one call
    # and "x" on the next (idempotence)
    while w.startswith(_PREFIX) and len(w) > len(_PREFIX):
        w = w[len(_PREFIX) :]
    return w


def canonical_wing(name: str) -> str:
    """Return the canonical wing for ``name`` (already validated by sanitize_name)."""
    w = normalise_wing(name)
    return WING_ALIASES.get(w, w)
