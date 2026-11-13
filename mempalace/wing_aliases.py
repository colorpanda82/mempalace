"""Canonical wing names for the MCP write paths (fork-local).

Background
----------
The MCP write tools (``tool_add_drawer``, ``tool_update_drawer``,
``tool_diary_write``) accepted any wing string that passed
``sanitize_name``. Writers that used the AAAK convention (``wing_<project>``)
or other spellings minted duplicate wings next to the canonical ones: on
2026-09-30, 37 drawers had to be moved by hand out of ``wing_cowork``,
``wing_bnm``, ``wing_jarvis``, ``wing_marketing_ops`` and ``wing_video_studio``.

Rule (since the v3.12 port)
---------------------------
1. upstream's slug, ``config.normalize_wing_name`` (lowercase, spaces and
   hyphens to ``_``), so a canonical wing is exactly what the miners and
   hooks store;
2. strip leading ``wing_`` prefixes (``wing_<agent>`` -> ``<agent>``), but
   only while what is left is itself a well-formed slug, so ``wing_.claude``
   stays as upstream stores it;
3. map through ``WING_ALIASES``.

A well-formed name that is not an alias passes through as upstream slugs it:
this normalises, it never rejects, so no existing writer breaks.

Reads
-----
``read_names`` lists every spelling a drawer for a canonical wing may sit
under: the canonical wing and each alias source, in underscore, hyphen and
``wing_``-prefixed form, plus every stored wing that canonicalises to the
same name. ``config.wing_aliases`` appends these, so a palace still holding
hyphen wings, or a legacy alias wing such as ``wing_claude``, reads through
the canonical name until ``mempalace ws-migrate-wings`` re-keys it.

Source of truth
---------------
``WING_ALIASES`` is a hand copy of ``WING_ALIASES`` in the workspace's
``scripts/diary-flusher.py`` (keys shown here in their slugged form), plus the
two merges applied by hand on 2026-09-30 that normalisation alone does not
reach (``wing_bnm``, ``wing_jarvis``). The installed package cannot read the
iCloud workspace, so a new alias needs a fork edit plus a redeploy, the same
arrangement as ``kg_entities.MERGES``.
"""

from __future__ import annotations

from typing import Iterable

from .config import normalize_wing_name

_PREFIX = "wing_"

# slugged, prefix-stripped key -> canonical wing
WING_ALIASES = {
    # diary-flusher.py WING_ALIASES
    "claude": "cowork",  # claude, wing_claude
    # "wing_cowork" -> "cowork" is handled by prefix stripping alone
    "client_bnm": "brave_new_media",  # client-bnm, client_bnm
    "project_jarvis_ops": "jarvis_ops",  # project-jarvis-ops, project_jarvis_ops
    # 2026-09-30 manual merge (wing_marketing_ops and wing_video_studio
    # reach marketing_ops / video_studio through normalisation alone)
    "bnm": "brave_new_media",  # bnm, wing_bnm
    "jarvis": "jarvis_ops",  # jarvis, wing_jarvis
}


def strip_wing_prefix(slug: str) -> str:
    """Strip leading ``wing_`` prefixes from an upstream slug.

    A loop, not an if: ``wing_wing_x`` must not become ``wing_x`` on one call
    and ``x`` on the next (idempotence). A strip that would leave something
    upstream's slug would change again (``wing_.claude`` -> ``.claude``) or
    nothing at all is not taken.
    """
    w = slug
    while w.startswith(_PREFIX):
        rest = w[len(_PREFIX) :]
        if not rest or normalize_wing_name(rest) != rest:
            break
        w = rest
    return w


def normalise_wing(name: str) -> str:
    """Upstream's slug of ``name`` with leading ``wing_`` prefixes stripped."""
    return strip_wing_prefix(normalize_wing_name(name.strip()))


def canonical_wing(name: str) -> str:
    """Return the canonical wing for ``name`` (already validated by sanitize_name)."""
    w = normalise_wing(name)
    return WING_ALIASES.get(w, w)


def canonical_stored_wing(value: str, field_name: str = "wing") -> str:
    """The wing a write stores for ``value``: validated, slugged, canonical.

    Upstream's ``config.canonical_wing`` validates and slugs (raising
    ``ValueError`` on bad input), then :func:`canonical_wing` applies the
    ``wing_`` strip and the alias table. ``mcp_server.canonical_wing`` and
    ``ws-migrate-wings`` both use this, so a migrated wing is exactly what a
    new write would store.
    """
    from .config import canonical_wing as upstream_canonical_wing, sanitize_name

    slug = upstream_canonical_wing(value, field_name)
    return sanitize_name(canonical_wing(slug), field_name)


def _spellings(slug: str) -> list[str]:
    hyphens = slug.replace("_", "-")
    out = []
    for name in (slug, hyphens, f"{_PREFIX}{slug}", f"{_PREFIX}{hyphens}"):
        if name not in out:
            out.append(name)
    return out


def read_names(wing: str, known: Iterable = ()) -> list[str]:
    """Every wing name a drawer filed for ``wing`` may be stored under.

    The canonical wing and every alias source that maps to it, each in its
    underscore, hyphen and ``wing_`` forms, then each name in ``known`` (the
    palace's stored wings) that canonicalises to the same wing.
    """
    if not isinstance(wing, str) or not wing.strip():
        return []
    canon = canonical_wing(wing)
    if not canon:
        return []
    sources = [canon] + sorted(k for k, v in WING_ALIASES.items() if v == canon)
    names: list[str] = []
    for source in sources:
        for name in _spellings(source):
            if name not in names:
                names.append(name)
    for name in sorted(n for n in known if isinstance(n, str)):
        if name not in names and name.strip() and canonical_wing(name) == canon:
            names.append(name)
    return names
