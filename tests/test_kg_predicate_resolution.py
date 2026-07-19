"""Tests for canonical-vocabulary resolution of KG predicates.

Borrowed pattern: cognee threads an ontology resolver + matching strategy INTO
extraction rather than auditing the graph afterwards. We already owned the
vocabulary (_manager/kg-schema/predicate_map_v1.json, v1, 2026-06-28); nothing
read it at write time. These tests pin the resolver's contract.
"""

import pytest

from mempalace.kg_vocabulary import (
    CANONICAL,
    MAPPING,
    PolicyError,
    resolve_predicate,
)


@pytest.fixture(autouse=True)
def _hermetic_policy_env(monkeypatch):
    """Clear the policy env var for every test in this module.

    Without this, a developer (or a job) exporting
    MEMPALACE_PREDICATE_POLICY=warn silently turns the strict-path tests
    green for the wrong reason.
    """
    monkeypatch.delenv("MEMPALACE_PREDICATE_POLICY", raising=False)


# --------------------------------------------------------------------------
# Vocabulary self-consistency. These catch a bad edit to the vocabulary
# itself, independently of any caller.
# --------------------------------------------------------------------------


def test_canonical_has_expected_size():
    # v1 froze the vocabulary at 17 after the 43 -> 17 migration.
    assert len(CANONICAL) == 17


def test_every_remap_target_is_canonical():
    for source, rule in MAPPING.items():
        if rule["action"] == "remap":
            assert rule["to"] in CANONICAL, (
                f"{source} remaps to {rule['to']!r}, which is not canonical"
            )


def test_every_keep_entry_is_canonical():
    for source, rule in MAPPING.items():
        if rule["action"] == "keep":
            assert source in CANONICAL, f"{source} is marked keep but is not canonical"


def test_every_canonical_predicate_is_reachable():
    """Each canonical predicate is either a keep entry or some remap's target."""
    keeps = {p for p, r in MAPPING.items() if r["action"] == "keep"}
    targets = {r["to"] for r in MAPPING.values() if r["action"] == "remap"}
    for predicate in CANONICAL:
        assert predicate in keeps or predicate in targets, (
            f"canonical predicate {predicate!r} is unreachable from the mapping"
        )


def test_drop_entries_carry_a_reason():
    for source, rule in MAPPING.items():
        if rule["action"] == "drop":
            assert rule.get("reason"), f"{source} is dropped with no reason given"


def test_no_predicate_has_two_actions():
    for source, rule in MAPPING.items():
        assert rule["action"] in {"keep", "remap", "drop"}, (
            f"{source} has unknown action {rule['action']!r}"
        )


# --------------------------------------------------------------------------
# keep: canonical predicates pass through untouched
# --------------------------------------------------------------------------


@pytest.mark.parametrize("predicate", sorted(CANONICAL))
def test_canonical_predicates_pass_through(predicate):
    resolved, action, _note = resolve_predicate(predicate)
    assert resolved == predicate
    assert action == "keep"


# --------------------------------------------------------------------------
# remap: known aliases normalize to their canonical target
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "alias,expected",
    [
        ("runs_on", "deployed_on"),
        ("deploys_to_jarvis", "deployed_on"),
        ("runs_as", "deployed_on"),
        ("canonical_path", "located_at"),
        ("template_at", "located_at"),
        ("is", "is_a"),
        ("upgraded_to", "replaced_by"),
        ("overrides", "replaced_by"),
        ("integrates_with", "uses"),
        ("authored", "produces"),
        ("checked_via", "evidenced_by"),
        ("owned_by", "part_of"),
        ("covers", "implements"),
        ("tracks_budgets", "used_for"),
    ],
)
def test_known_aliases_are_remapped(alias, expected):
    resolved, action, note = resolve_predicate(alias)
    assert resolved == expected
    assert action == "remap"
    # The note must name both ends, so the caller can report the rewrite.
    assert alias in note and expected in note


def test_remap_is_reported_not_silent():
    _resolved, _action, note = resolve_predicate("runs_on")
    assert note, "a remap must return a non-empty note so callers can surface it"


# --------------------------------------------------------------------------
# drop: banned session-state predicates are rejected under BOTH policies.
# These are not a policy question - they rot the graph.
# --------------------------------------------------------------------------


BANNED = [
    "status",
    "in_queue",
    "in_jarvis_queue",
    "processing",
    "pending_dispatch",
    "completed_tonight",
    "needs_from_cristian",
    "manual_step_due",
    "draft_at",
    "related_to",
    "has_levels",
    "rule",
    "qualifiers",
    "pipeline_summary",
]


@pytest.mark.parametrize("predicate", BANNED)
def test_banned_predicates_rejected_under_strict(predicate):
    with pytest.raises(PolicyError):
        resolve_predicate(predicate, policy="strict")


@pytest.mark.parametrize("predicate", BANNED)
def test_banned_predicates_rejected_under_warn(predicate):
    """warn mode exists to admit NEW predicates, not to re-admit banned ones."""
    with pytest.raises(PolicyError):
        resolve_predicate(predicate, policy="warn")


def test_ban_error_quotes_the_stored_reason():
    with pytest.raises(PolicyError) as exc:
        resolve_predicate("status")
    assert "session-state" in str(exc.value)


# --------------------------------------------------------------------------
# unknown: strict rejects with a suggestion, warn admits with a note
# --------------------------------------------------------------------------


def test_unknown_rejected_under_strict():
    with pytest.raises(PolicyError):
        resolve_predicate("frobnicates", policy="strict")


def test_unknown_error_suggests_nearest_canonical():
    """The borrowed idea: don't just say no, match."""
    with pytest.raises(PolicyError) as exc:
        resolve_predicate("deployed_onn", policy="strict")
    assert "deployed_on" in str(exc.value)


def test_unknown_error_without_a_near_match_still_explains():
    with pytest.raises(PolicyError) as exc:
        resolve_predicate("zzzzzzzz", policy="strict")
    message = str(exc.value)
    assert "zzzzzzzz" in message
    # Must tell the caller how to proceed, not just refuse.
    assert "predicate_map" in message or "vocabulary" in message


def test_unknown_admitted_under_warn():
    resolved, action, note = resolve_predicate("frobnicates", policy="warn")
    assert resolved == "frobnicates"
    assert action == "unknown"
    assert note


# --------------------------------------------------------------------------
# normalization of the lookup key
# --------------------------------------------------------------------------


def test_lookup_is_case_insensitive():
    resolved, action, _ = resolve_predicate("Runs_On")
    assert resolved == "deployed_on"
    assert action == "remap"


def test_lookup_strips_surrounding_whitespace():
    resolved, action, _ = resolve_predicate("  runs_on  ")
    assert resolved == "deployed_on"
    assert action == "remap"


def test_canonical_returned_in_lowercase():
    resolved, _action, _note = resolve_predicate("IS_A")
    assert resolved == "is_a"


# --------------------------------------------------------------------------
# input validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "   ", None, 42, [], {}])
def test_empty_or_non_string_rejected(bad):
    with pytest.raises((ValueError, TypeError)):
        resolve_predicate(bad)


def test_policy_defaults_to_warn():
    """Default admits unknowns.

    The canonical 17 are one deployment's infrastructure ontology; MemPalace is
    general purpose. Closing the vocabulary by default would refuse legitimate
    personal-fact predicates like "likes" or "does". strict is opt-in.
    """
    resolved, action, note = resolve_predicate("frobnicates")
    assert resolved == "frobnicates"
    assert action == "unknown"
    assert note


def test_default_still_refuses_banned():
    """The drop list is universal and does not depend on policy."""
    with pytest.raises(PolicyError):
        resolve_predicate("status")


def test_default_still_normalizes_aliases():
    """Alias remapping is universal too."""
    resolved, action, _note = resolve_predicate("runs_on")
    assert resolved == "deployed_on"
    assert action == "remap"


def test_invalid_policy_name_rejected():
    with pytest.raises(ValueError):
        resolve_predicate("is_a", policy="whatever")


# --------------------------------------------------------------------------
# PolicyError must be catchable as ValueError so tool_kg_add's existing
# `except ValueError` block keeps working unchanged.
# --------------------------------------------------------------------------


def test_policy_error_is_a_value_error():
    assert issubclass(PolicyError, ValueError)


# --------------------------------------------------------------------------
# The env var is the documented escape hatch. It had no coverage at all.
# --------------------------------------------------------------------------


def test_env_var_can_select_warn(monkeypatch):
    monkeypatch.setenv("MEMPALACE_PREDICATE_POLICY", "warn")
    resolved, action, _note = resolve_predicate("frobnicates")
    assert resolved == "frobnicates"
    assert action == "unknown"


def test_env_var_tolerates_case_and_whitespace(monkeypatch):
    monkeypatch.setenv("MEMPALACE_PREDICATE_POLICY", "  WARN  ")
    _resolved, action, _note = resolve_predicate("frobnicates")
    assert action == "unknown"


def test_invalid_env_var_value_raises(monkeypatch):
    monkeypatch.setenv("MEMPALACE_PREDICATE_POLICY", "loose")
    with pytest.raises(ValueError):
        resolve_predicate("is_a")


def test_explicit_policy_argument_beats_env(monkeypatch):
    monkeypatch.setenv("MEMPALACE_PREDICATE_POLICY", "warn")
    with pytest.raises(PolicyError):
        resolve_predicate("frobnicates", policy="strict")


def test_env_var_warn_still_rejects_banned(monkeypatch):
    monkeypatch.setenv("MEMPALACE_PREDICATE_POLICY", "warn")
    with pytest.raises(PolicyError):
        resolve_predicate("status")
