"""C7 TTL prune: ``janitor.prune_expired`` runs and deletes only expired drawers.

Until the 3.12 port it could not run at all: a function-local re-import of
``MempalaceConfig`` made the name local to the whole function, so its first
line raised UnboundLocalError.
"""

from mempalace import janitor


def _seed(collection):
    collection.add(
        ids=["expired", "current", "plain"],
        documents=[
            "task state\nexpires_at: 2001-01-01\n",
            "task state\nexpires_at: 2999-01-01\n",
            "a fact with no TTL",
        ],
        metadatas=[
            {"wing": "scratchpad", "room": "tasks"},
            {"wing": "scratchpad", "room": "tasks"},
            {"wing": "scratchpad", "room": "facts"},
        ],
    )


def test_prune_expired_dry_run_reports_without_deleting(palace_path, collection, monkeypatch):
    monkeypatch.setattr("mempalace.cli_workspace._hub_for", lambda _palace: None)
    _seed(collection)
    scanned, deleted = janitor.prune_expired(palace_path=palace_path, dry_run=True)
    assert (scanned, deleted) == (3, 1)
    assert sorted(collection.get()["ids"]) == ["current", "expired", "plain"]


def test_prune_expired_deletes_only_the_expired_drawer(palace_path, collection, monkeypatch):
    seen = []
    monkeypatch.setattr(
        "mempalace.cli_workspace._hub_for", lambda palace: seen.append(palace) or None
    )
    _seed(collection)
    scanned, deleted = janitor.prune_expired(palace_path=palace_path, dry_run=False)
    assert (scanned, deleted) == (3, 1)
    assert seen == [palace_path], "hub lookup must use the palace being pruned"
    assert sorted(collection.get()["ids"]) == ["current", "plain"]
