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
     the recorded canonical name -- UNLESS ``prefer_literal_if_present`` is
     set and the literal name already holds its own fact(s) (see below).
  2. Otherwise, if the name-as-given has NO facts in the store but a
     hyphen/underscore/space variant of it DOES, resolve to that variant
     (the one with the most facts; an equal-count tie is named as a tie,
     never claimed as "most", and broken alphabetically).
  3. Otherwise, the name is returned unchanged.

``FLAGGED_NOT_MERGED_PAIRS`` is consulted before any resolution from step 1
or step 2 is returned: if the (name, resolved) pair is one of these,
resolution is refused and the name is returned unchanged.

``prefer_literal_if_present`` (added in the F2 hardening pass): callers that
target a SPECIFIC EXISTING fact by exact identity -- ``kg_invalidate``'s
subject/object, and ``kg_supersede``'s subject/old_object -- must pass this
as True. Redirecting those to a merge target when the literal name already
holds the fact would silently operate on the wrong node: the invalidate/
supersede would report "no match" or, worse, close/open the wrong triple,
while the caller's actual fact sits untouched under the literal alias.
``kg_query``, ``kg_add``, and ``kg_supersede``'s new_object are WRITE-forward
uses (like a merge should) and keep the default (False): the merge still
applies even when the alias holds a stray fact, and when it does, a warning
is appended to the note rather than silently orphaning that stray fact.

``fact_count`` (added in the F1 hardening pass) MUST be an EXACT, non-fuzzy
counter -- e.g. ``KnowledgeGraph.timeline_total``, which counts
``WHERE subject = ? OR object = ?`` with no fallback. ``KnowledgeGraph.
query_entity`` is NOT safe to use here: upstream (confirmed present
verbatim on the v3.10.0 tag, not a fork addition) has a single-candidate
fuzzy fallback that, when the exact id has zero rows, silently borrows the
one nearest LIKE-matched entity's facts and returns them as if they
belonged to the queried name. Wiring ``fact_count`` to ``query_entity``
therefore let a nonexistent spelling appear to "have facts" (reproduced:
``foo-bar`` resolved to ``foo_bar`` when only ``foo_bar_baz`` existed; a
literal date ``2026-09-23`` resolved to ``2026_09_23`` against a
``2026_09_23_standup`` sibling), and ``kg_add`` then created a real node
under a spelling nobody typed and nobody has facts for. That upstream
fallback is intentional single-candidate-disambiguation behaviour for
``query_entity``'s own normal callers and is not touched here; the fix is
entirely on our side, in which counting primitive the resolver is handed.
Because ``timeline_total`` counts both directions unconditionally, this
also fixes the F4 direction-scoping bug: the old wiring reused the
caller's requested ``direction`` for the existence check, so an entity
holding only incoming facts read as empty under ``direction="outgoing"``
and a real variant went unresolved.
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


def resolve_entity(name: str, *, fact_count=None, prefer_literal_if_present: bool = False):
    """Resolve ``name`` to its canonical/existing spelling.

    ``fact_count`` is an injected callable, ``fact_count(candidate) -> int``,
    that MUST be an exact-id counter (see module docstring) -- callers wire
    this to ``KnowledgeGraph.timeline_total``, never to ``query_entity``.
    It is read-only against the store; omitting it skips step 2 entirely
    and skips the literal-facts check in step 1, but step 1's plain merge
    redirect still applies.

    ``prefer_literal_if_present``: set True for callers that target an
    existing fact by exact identity (kg_invalidate's subject/object,
    kg_supersede's subject/old_object). See module docstring.

    Returns ``(canonical_name, note_or_None)``.
    """
    if not name or not str(name).strip():
        return name, None
    original = str(name)
    key = _normkey(original)

    # -- 1. exact merges hit ---------------------------------------------
    if key in MERGES:
        target = MERGES[key]
        if target == original or _flagged(original, target):
            return original, None

        literal_count = 0
        if fact_count is not None:
            try:
                literal_count = fact_count(original) or 0
            except Exception:
                literal_count = 0

        if literal_count and prefer_literal_if_present:
            return original, (
                f"entity {original!r} has {literal_count} fact(s) of its own "
                f"and was NOT redirected to recorded merge target {target!r}; "
                f"operate on {target!r} directly to affect the merged history"
            )

        note = f"entity {original!r} resolved to {target!r} (recorded merge)"
        if literal_count:
            note += (
                f"; warning: {original!r} itself still holds {literal_count} "
                f"fact(s) under the un-merged alias (never auto-migrated)"
            )
        return target, note

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
    tied = sorted(c for n, c in scored if n == best_count)

    if len(tied) > 1:
        note = (
            f"entity {original!r} has no facts; {len(tied)} candidates tie at "
            f"{best_count} fact(s) each ({', '.join(tied)}); resolved to "
            f"{best!r} (tie-break: alphabetically first spelling)"
        )
    elif len(scored) > 1:
        note = (
            f"entity {original!r} has no facts; resolved to existing variant "
            f"{best!r} ({best_count} facts, most of {len(scored)} candidates)"
        )
    else:
        note = f"entity {original!r} has no facts; resolved to existing variant {best!r}"
    return best, note
