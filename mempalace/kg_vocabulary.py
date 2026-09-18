"""Canonical predicate vocabulary for the knowledge graph, resolved at write time.

Background
----------
The vocabulary itself is not new: ``_manager/kg-schema/predicate_map_v1.json``
(v1, created 2026-06-28) froze it at 17 predicates after a 43 -> 17 migration.
What was missing was anything that *read* it on the write path. ``kg_add``
passed predicates through ``sanitize_name()``, which validates syntax only, so
drift was caught a week later by the F6 weekly gate's Part 3 audit -- which is
WARN-only and never blocks.

This module closes that gap. The design is borrowed from cognee, which threads
an ontology resolver plus a matching strategy *into* graph extraction rather
than auditing the graph afterwards. The useful half of that idea is the
matching: when a caller writes ``runs_on``, do not merely reject it -- resolve
it to ``deployed_on``, which the mapping already said it meant.

Policy
------
``keep``    canonical, passes through
``remap``   known alias, normalized to its canonical target, reported
``drop``    banned session-state, rejected under BOTH policies (these rot the
            graph; that is settled, not a policy question)
unknown     rejected under ``strict`` with a nearest-match suggestion,
            admitted with a note under ``warn``

The three policies, weakest guard last:

``strict``      unknown predicates refused; both kinds of drop refused
``warn``        (default) unknown admitted; both kinds of drop refused
``permissive``  unknown admitted; ``modeling`` drops admitted with a note;
                ``session_state`` drops STILL refused, under every policy

Policy comes from the ``MEMPALACE_PREDICATE_POLICY`` environment variable and
defaults to ``warn``.

``strict`` was the original default and it was wrong. The canonical 17 describe
a software-infrastructure domain (adopted, deployed_on, depends_on, version)
derived from one deployment's graph. MemPalace itself is general purpose -- its
own test suite records that Alice likes coffee and Max does chess -- so a closed
infrastructure vocabulary refused nine existing tests the moment it was wired
in. Refusing unknown predicates by default imposes one deployment's ontology on
every other one.

What stays unconditional is the genuinely universal part: banned session-state
predicates are refused under both policies, and known aliases are always
normalized. A deployment that does want a closed vocabulary opts in with
``MEMPALACE_PREDICATE_POLICY=strict``.

Source of truth
---------------
The data below is the runtime authority. It is kept in sync by hand with
``_manager/kg-schema/predicate_map_v1.json`` in the workspace, which remains the
human-facing authoring copy and carries the migration rationale.
"""

import difflib
import os

__all__ = [
    "CANONICAL",
    "MAPPING",
    "PolicyError",
    "VOCABULARY_VERSION",
    "resolve_predicate",
    "resolve_predicate_for_lookup",
]

VOCABULARY_VERSION = 1

#: The 17 canonical predicates. Frozen at v1.
CANONICAL = frozenset(
    {
        "adopted",
        "deprecated",
        "replaced_by",
        "is_a",
        "implements",
        "uses",
        "depends_on",
        "produces",
        "part_of",
        "located_at",
        "deployed_on",
        "version",
        "limited_by",
        "requires",
        "evidenced_by",
        "contradicted_by",
        "used_for",
    }
)

#: Resolution rules. Every canonical predicate appears here as a ``keep`` entry
#: or as some rule's remap target, so the mapping alone fully describes the
#: vocabulary.
MAPPING = {
    # -- keep: canonical, pass through -----------------------------------
    "located_at": {"action": "keep"},
    "depends_on": {"action": "keep"},
    "is_a": {"action": "keep"},
    "replaced_by": {"action": "keep"},
    "version": {"action": "keep"},
    "adopted": {"action": "keep"},
    "implements": {"action": "keep"},
    "requires": {"action": "keep"},
    "used_for": {"action": "keep", "note": "promoted to canonical at v1"},
    "part_of": {"action": "keep", "note": "promoted to canonical at v1"},
    # These three are canonical but were absent from predicate_map_v1.json's
    # mapping block, because no triple used them at migration time. Without
    # explicit keep entries a caller writing a legitimate canonical predicate
    # would have been treated as unknown.
    "deprecated": {"action": "keep", "note": "canonical; unused at v1 migration"},
    "limited_by": {"action": "keep", "note": "canonical; unused at v1 migration"},
    "contradicted_by": {"action": "keep", "note": "canonical; unused at v1 migration"},
    # Canonical predicates that predicate_map_v1.json listed only as remap
    # TARGETS, never as keep entries. Writing one directly must still pass.
    "uses": {"action": "keep"},
    "produces": {"action": "keep"},
    "deployed_on": {"action": "keep"},
    "evidenced_by": {"action": "keep"},
    # -- remap: known aliases --------------------------------------------
    "runs_on": {"action": "remap", "to": "deployed_on"},
    "deploys_to_jarvis": {"action": "remap", "to": "deployed_on"},
    "runs_as": {"action": "remap", "to": "deployed_on"},
    "canonical_path": {"action": "remap", "to": "located_at"},
    "template_at": {"action": "remap", "to": "located_at"},
    "has_design_systems_reference_at": {"action": "remap", "to": "located_at"},
    "is": {"action": "remap", "to": "is_a"},
    "upgraded_to": {"action": "remap", "to": "replaced_by"},
    "overrides": {"action": "remap", "to": "replaced_by"},
    "default_web_stack": {"action": "remap", "to": "uses"},
    "integrates_with": {"action": "remap", "to": "uses"},
    "authored": {"action": "remap", "to": "produces"},
    "exports_functions": {"action": "remap", "to": "produces"},
    "writes_state_to": {"action": "remap", "to": "produces"},
    "includes_skill_draft": {"action": "remap", "to": "produces"},
    "checked_via": {"action": "remap", "to": "evidenced_by"},
    "owned_by": {"action": "remap", "to": "part_of"},
    "covers": {"action": "remap", "to": "implements"},
    "tracks_budgets": {"action": "remap", "to": "used_for"},
    # -- drop: banned ------------------------------------------------------
    "needs_from_cristian": {"action": "drop", "reason": "banned session-state", "reason_class": "session_state"},
    "completed_tonight": {"action": "drop", "reason": "banned session-state", "reason_class": "session_state"},
    "draft_at": {"action": "drop", "reason": "banned session-state", "reason_class": "session_state"},
    "in_queue": {"action": "drop", "reason": "banned session-state", "reason_class": "session_state"},
    "in_jarvis_queue": {"action": "drop", "reason": "banned session-state", "reason_class": "session_state"},
    "manual_step_due": {"action": "drop", "reason": "banned session-state", "reason_class": "session_state"},
    "pending_dispatch": {"action": "drop", "reason": "banned session-state", "reason_class": "session_state"},
    "processing": {"action": "drop", "reason": "banned session-state", "reason_class": "session_state"},
    "status": {"action": "drop", "reason": "session-state (rots)", "reason_class": "session_state"},
    "pipeline_summary": {"action": "drop", "reason": "non-relation; belongs in a drawer", "reason_class": "modeling"},
    "qualifiers": {"action": "drop", "reason": "meta/non-relation", "reason_class": "modeling"},
    "rule": {"action": "drop", "reason": "non-relation; belongs in a drawer", "reason_class": "modeling"},
    "related_to": {"action": "drop", "reason": "too generic to be queryable", "reason_class": "modeling"},
    "has_levels": {"action": "drop", "reason": "attribute, not a relation", "reason_class": "modeling"},
}

VOCABULARY_FILE = "_manager/kg-schema/predicate_map_v1.json"

POLICY_ENV_VAR = "MEMPALACE_PREDICATE_POLICY"
DEFAULT_POLICY = "warn"
VALID_POLICIES = ("strict", "warn", "permissive")


class PolicyError(ValueError):
    """A predicate was refused by the vocabulary.

    Subclasses ``ValueError`` deliberately: callers such as ``tool_kg_add``
    already wrap their sanitization in ``except ValueError`` and return a
    structured error, so this integrates without touching their control flow.
    """


def _resolve_policy(policy=None):
    if policy is None:
        policy = os.environ.get(POLICY_ENV_VAR, DEFAULT_POLICY)
    if not isinstance(policy, str):
        raise ValueError(f"policy must be a string, got {type(policy).__name__}")
    policy = policy.strip().lower()
    if policy not in VALID_POLICIES:
        raise ValueError(
            f"invalid predicate policy {policy!r}; expected one of {', '.join(VALID_POLICIES)}"
        )
    return policy


def _suggest(key):
    """Nearest canonical predicate for an unrecognized one, or None.

    Aliases are included in the search pool so that a typo on an alias
    (``runs_onn``) still lands on the canonical target (``deployed_on``)
    rather than on the alias itself.
    """
    pool = set(CANONICAL) | set(MAPPING)
    matches = difflib.get_close_matches(key, pool, n=1, cutoff=0.72)
    if not matches:
        return None
    match = matches[0]
    rule = MAPPING.get(match)
    if rule and rule["action"] == "remap":
        return rule["to"]
    if rule and rule["action"] == "drop":
        return None
    return match


def resolve_predicate(predicate, policy=None):
    """Resolve ``predicate`` against the canonical vocabulary.

    Returns a ``(resolved, action, note)`` tuple where ``action`` is one of
    ``keep``, ``remap`` or ``unknown``, and ``note`` is a human-readable
    description of any rewrite (empty when nothing changed).

    Raises ``PolicyError`` for banned predicates, and for unrecognized ones
    under the ``strict`` policy.
    """
    policy = _resolve_policy(policy)

    if not isinstance(predicate, str):
        raise TypeError(f"predicate must be a string, got {type(predicate).__name__}")

    key = predicate.strip().lower()
    if not key:
        raise ValueError("predicate must be a non-empty string")

    rule = MAPPING.get(key)

    if rule is None:
        # Defence in depth: a canonical predicate with no explicit mapping
        # entry must pass regardless of policy. Without this, adding to
        # CANONICAL and forgetting MAPPING produces the nonsense error
        # "'uses' is not in the canonical vocabulary. Did you mean 'uses'?"
        if key in CANONICAL:
            return key, "keep", _normalization_note(predicate, key)
        # permissive is a WEAKER guard than warn, never a different one. Testing
        # only for "warn" here made an unknown predicate refused under permissive
        # but admitted under the default -- a policy that is stricter than the
        # thing it relaxes. Caught by printing the full policy x predicate matrix
        # rather than spot-checking one cell.
        if policy in ("warn", "permissive"):
            return (
                key,
                "unknown",
                f"predicate {key!r} is not in canonical vocabulary v{VOCABULARY_VERSION}; "
                f"admitted because {POLICY_ENV_VAR}=warn",
            )
        suggestion = _suggest(key)
        hint = f" Did you mean {suggestion!r}?" if suggestion else ""
        raise PolicyError(
            f"predicate {key!r} is not in the canonical vocabulary "
            f"(v{VOCABULARY_VERSION}, {len(CANONICAL)} predicates).{hint} "
            f"To add it, update {VOCABULARY_FILE} and mempalace/kg_vocabulary.py, "
            f"or set {POLICY_ENV_VAR}=warn to admit it for this run."
        )

    action = rule["action"]

    if action == "keep":
        return key, "keep", _normalization_note(predicate, key)

    if action == "remap":
        target = rule["to"]
        return (
            target,
            "remap",
            f"predicate {key!r} normalized to {target!r} per canonical "
            f"vocabulary v{VOCABULARY_VERSION}",
        )

    if action == "drop":
        # Two kinds of drop, and only one is universal.
        #
        # session_state: 'status', 'in_queue', 'processing' and friends record what
        # is true RIGHT NOW. They are false tomorrow and rot any graph, in any
        # deployment. Refused under every policy, permissive included.
        #
        # modeling: 'related_to' (too generic to be queryable), 'rule',
        # 'has_levels', 'qualifiers', 'pipeline_summary'. These are opinions about
        # what belongs in a graph rather than a drawer. Good opinions -- they stay
        # the default -- but they ARE opinions, and until now no policy could
        # disable them. That was the strict-by-default mistake surviving in one
        # corner after being corrected everywhere else (independent review,
        # 2026-07-19). permissive admits them, with a note, for a deployment that
        # models differently. The DEFAULT is unchanged.
        if policy == "permissive" and rule.get("reason_class") == "modeling":
            return (
                key,
                "unknown",
                f"predicate {key!r} is discouraged ({rule['reason']}) but admitted "
                f"because {POLICY_ENV_VAR}=permissive",
            )
        raise PolicyError(
            f"predicate {key!r} is not allowed: {rule['reason']}. "
            f"Facts of this kind belong in a drawer, not the knowledge graph. "
            f"See {VOCABULARY_FILE}."
        )

    raise ValueError(f"vocabulary entry for {key!r} has unknown action {action!r}")


def _normalization_note(submitted, stored):
    """Note for a case/whitespace-only rewrite.

    A canonical predicate submitted as ``IS_A`` is STORED as ``is_a``. That is
    still a rewrite, and a caller who cannot see it cannot reconcile what it
    sent with what the graph holds. Returning "" here (the original behaviour)
    meant the WAL recorded ``predicate_submitted`` while the response said
    nothing -- the two disagreed about whether anything happened.
    """
    if submitted is None or not isinstance(submitted, str):
        return ""
    return (
        f"predicate {submitted!r} normalized to {stored!r}"
        if submitted != stored
        else ""
    )


def resolve_predicate_for_lookup(predicate):
    """Resolve a predicate for ADDRESSING EXISTING ROWS, never for writing.

    Returns ``(resolved, note)``. Applies keep/remap so a lookup matches what
    ``tool_kg_add`` actually stored, and **never raises on drop or unknown**.

    Why this is not just ``resolve_predicate(policy="warn")``: the write path
    exists to keep bad predicates OUT of the graph, but a lookup addresses
    predicates that are already IN it. Refusing ``status`` here would make
    legacy session-state triples -- written before the write gate existed --
    permanently un-invalidatable, locking the cleanup path behind the guard
    meant to prevent the mess.

    Caveat worth knowing: a triple written with an ALIAS before the write gate
    existed is stored under the alias, so remapping the lookup will miss it.
    That is why the caller must surface a zero-match result instead of
    reporting success.
    """
    if not isinstance(predicate, str):
        raise TypeError(f"predicate must be a string, got {type(predicate).__name__}")
    key = predicate.strip().lower()
    if not key:
        raise ValueError("predicate must be a non-empty string")

    rule = MAPPING.get(key)
    if rule is not None and rule["action"] == "remap":
        target = rule["to"]
        return target, (
            f"predicate {key!r} resolved to {target!r} for lookup "
            f"per canonical vocabulary v{VOCABULARY_VERSION}"
        )
    return key, _normalization_note(predicate, key)
