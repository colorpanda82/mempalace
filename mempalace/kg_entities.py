"""Deterministic entity resolution for the knowledge graph, applied at
read/write time -- the entity-side counterpart to kg_vocabulary.py.

Background
----------
``_manager/kg-schema/entity_aliases_v1.json`` (workspace) records two kinds
of human-confirmed decisions about KG entity names:

  merges              old spelling -> canonical spelling, already reviewed
                       and merged in the workspace's own bookkeeping, but
                       never applied at read/write time. ``research_scout``
                       (2026-07-19 merge target ``research-scout``) is still
                       being written under the old name because nothing
                       enforced the merge.
  flagged_not_merged   pairs that LOOK like the same entity under naive
                       normalization but are deliberately kept separate (a
                       concept vs. its script file, two ends of a
                       replaced_by edge, two different machines).

Measured gap (workspace research-findings/2026-09-23-self-evolving-ontology):
30.5% of kg_query calls (53/174, 2026-08-24..09-22) returned count=0. The
store already normalizes case and treats space=underscore
(``KnowledgeGraph._entity_id``), but never hyphens. Of the near-misses, the
real fragmentation is hyphen-vs-underscore/space -- exactly what stayed
unfixed after the 2026-07-19 merge decisions were recorded but never
enforced.

Source of truth
----------------
The data below is the runtime authority, kept in sync BY HAND with
``_manager/kg-schema/entity_aliases_v1.json`` in the workspace -- the same
convention ``kg_vocabulary.py`` uses for its ``MAPPING``, and for the same
reason: the installed package has no access to the workspace's iCloud-synced
path, so there is nothing to read at runtime. The JSON file remains the
human-facing authoring copy and carries the merge rationale.

Policy
------
Deterministic, no LLM, no auto-merge, never migrates or deletes a node.
``resolve_entity`` only tells a caller which spelling to actually use
against the store; it never touches the store itself.

Order of resolution, per ``resolve_entity(name)``:
  1. Exact ``MERGES`` hit (case/space/underscore-normalized key match) ->
     the recorded canonical name. Applies even when the name-as-given
     already has facts -- a stray write under the old name after a merge
     decision must still resolve forward.
  2. Otherwise, if the name-as-given has NO facts in the store but a
     hyphen/underscore/space variant of it DOES, resolve to that variant
     (the one with the most facts, if more than one candidate has any).
  3. Otherwise, the name is returned unchanged.

``FLAGGED_NOT_MERGED_PAIRS`` is consulted before any resolution from step 1
or step 2 is returned: if the (name, resolved) pair is one of these,
resolution is refused and the name is returned unchanged. None of the
entity_aliases_v1.json flagged pairs are actually hyphen/underscore/space
variants of each other (they differ by suffix, by a dot, or are unrelated
machines), so in practice this guard is defense-in-depth rather than
something the algorithm would otherwise hit -- but it is unconditional, so
a future addition to the flagged list is honored even if it later WOULD
collide with the variant heuristic.
"""

import re

__all__ = [
    "ENTITY_ALIASES_VERSION",
    "MERGES",
    "FLAGGED_NOT_MERGED_PAIRS",
    "resolve_entity",
]

ENTITY_ALIASES_VERSION = 1

#: _manager/kg-schema/entity_aliases_v1.json "merges" block, as of the
#: 2026-07-19 merge decisions.
MERGES = {
    "mac_mini_m4": "jarvis",
    "mac_mini_m4_16gb": "jarvis",
    "jarvis_mac_mini": "jarvis",
    "design-systems-database": "design-systems_database",
    "jarvis_dashboard": "jarvis-dashboard",
    "research_scout": "research-scout",
}

#: _manager/kg-schema/entity_aliases_v1.json "flagged_not_merged" block.
#: Each pair is normalized the same way MERGES keys are matched, so a
#: resolution can never fire in either direction for these names.
FLAGGED_NOT_MERGED_PAIRS = {
    frozenset({"llm-router", "llm-router.sh"}),
    frozenset({"jarvis-watcher", "jarvis-watcher.sh"}),
    frozenset({"macbook", "jarvis"}),
    frozenset({"brave_new_media", "bravenew.media"}),
    frozenset({"clients/brave-new", "clients/bravenew"}),
}


def _normkey(name: str) -> str:
    """Match the store's own normalization (case + space/underscore), but
    leave hyphens untouched -- the store does not fold them either
    (``KnowledgeGraph._entity_id``)."""
    return re.sub(r"[\s_]+", "_", str(name).strip().lower())


def _dehyphenate(name: str) -> str:
    return name.replace("-", "_")


def _hyphenate(name: str) -> str:
    return re.sub(r"[\s_]+", "-", name.strip())


def _flagged(a: str, b: str) -> bool:
    pair = frozenset({_normkey(a), _normkey(b)})
    return pair in FLAGGED_NOT_MERGED_PAIRS


def resolve_entity(name: str, *, fact_count=None):
    """Resolve ``name`` to its canonical/existing spelling.

    ``fact_count`` is an injected callable, ``fact_count(candidate) -> int``,
    consulted only for step 2 (variant lookup) and only when the name as
    given has no facts. It is the caller's read-only lookup against the
    store (e.g. ``len(kg.query_entity(candidate, direction="both"))``).
    Omitting it skips step 2 entirely; step 1 (merges) still applies.

    Returns ``(canonical_name, note_or_None)``.
    """
    if not name or not str(name).strip():
        return name, None
    original = str(name)
    key = _normkey(original)

    # -- 1. exact merges hit, unconditional on whether `original` has facts
    if key in MERGES:
        target = MERGES[key]
        if target != original and not _flagged(original, target):
            return target, f"entity {original!r} resolved to {target!r} (recorded merge)"
        return original, None

    # -- 2. hyphen/underscore/space variant, only when `original` has none
    if fact_count is None:
        return original, None
    if not re.search(r"[-_\s]", original):
        # nothing a hyphen<->underscore/space swap could change; do not
        # spend a store read confirming it.
        return original, None

    try:
        if fact_count(original):
            return original, None
    except Exception:
        return original, None

    candidates = []
    for cand in (_dehyphenate(original), _hyphenate(original)):
        if cand and cand != original and cand not in candidates:
            candidates.append(cand)

    scored = []
    for cand in candidates:
        if _flagged(original, cand):
            continue
        try:
            n = fact_count(cand)
        except Exception:
            continue
        if n:
            scored.append((n, cand))

    if not scored:
        return original, None

    scored.sort(key=lambda t: (-t[0], t[1]))
    best_count, best = scored[0]
    if len(scored) > 1:
        note = (
            f"entity {original!r} has no facts; resolved to existing variant "
            f"{best!r} ({best_count} facts, most of {len(scored)} candidates)"
        )
    else:
        note = f"entity {original!r} has no facts; resolved to existing variant {best!r}"
    return best, note
