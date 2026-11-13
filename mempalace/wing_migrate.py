"""Re-key stored wings to their canonical name (fork; ``ws-migrate-wings``).

Since the 3.12 port a write stores upstream's slug plus the fork's alias
table (``wing_aliases.canonical_stored_wing``): ``jarvis-ops`` ->
``jarvis_ops``, ``wing_bnm`` -> ``brave_new_media``. A palace filed before
that holds the old spellings; reads span them (``wing_aliases.read_names``),
and this module moves them, so every store agrees on one name per wing.

Every stored wing ``W`` whose canonical name differs is renamed to it, in:

* ``drawers``: the drawer collection's ``wing`` metadata;
* ``closets``: the closet (index) collection's ``wing`` metadata;
* ``aaak``: the AAAK collection's ``wing`` metadata;
* ``hallways``: ``hallways.sqlite3`` records (``hallways.rename_wing_hallways``);
* ``tunnels``: explicit tunnel endpoints in ``tunnels.json``
  (``palace_graph.rename_tunnel_wings``);
* ``documents``: the fork's F8 ``documents`` table in the palace's knowledge
  graph (``wing`` column);
* ``topics_by_wing``: the per-user ``known_entities.json`` keys, upstream's
  own wing migration step, only when the palace is the configured one (the
  registry is not stored in the palace, so a copy must not change it).

Not moved: drawer, closet and diary ids (they embed the wing as an opaque
prefix that nothing decodes, the same rule as upstream's
``migrate --wing-names``), the WAL audit log, ``diary_ingest`` state keys,
knowledge-graph triples (not wing-keyed), and ``mempalace.yaml`` files in
project directories.

Metadata collections move through upstream's ``migrate.plan_wing_renames``
and ``_apply_wing_updates`` (batched backend ``update``, ids untouched, no
re-embedding). Wings whose targets coincide merge. An apply holds the palace
writer lease (``mine_palace_lock``); every run refuses while a hub serves the
palace. A dry run reads ``chroma.sqlite3`` and the other sqlite stores
read-only, without a Chroma client or the lease's lock files, so it changes no
file. Idempotent: a second run finds nothing to rename.
"""

from __future__ import annotations

import os
import sqlite3
from collections import defaultdict
from typing import Optional

STORES = ("drawers", "closets", "aaak", "hallways", "tunnels", "documents", "topics_by_wing")


class HubHoldsPalace(RuntimeError):
    """A live hub, or another writer, holds the palace's writer lease."""


def target_for(wing) -> Optional[str]:
    """The canonical name for a stored wing when it differs, else ``None``.

    ``None`` too for a stored name upstream's validation rejects: it is left
    where it is rather than guessed at.
    """
    from .wing_aliases import canonical_stored_wing

    if not isinstance(wing, str) or not wing.strip():
        return None
    try:
        target = canonical_stored_wing(wing)
    except ValueError:
        return None
    return target if target != wing else None


# ── stores ───────────────────────────────────────────────────────────────────


def _collections(palace_path: str, read_only: bool = False) -> dict:
    """``{store: collection}`` for the metadata collections that exist.

    The plan reads through read-only handles, so a dry run (or a run with
    nothing to rename) leaves every palace file as it found it.
    """
    from .backends import CollectionNotInitializedError
    from .palace import get_aaak_collection, get_closets_collection, get_collection

    out = {}
    for store, opener in (
        ("drawers", lambda: get_collection(palace_path, create=False, read_only=read_only)),
        (
            "closets",
            lambda: get_closets_collection(palace_path, create=False, read_only=read_only),
        ),
        ("aaak", lambda: get_aaak_collection(palace_path, create=False, read_only=read_only)),
    ):
        try:
            out[store] = opener()
        except CollectionNotInitializedError:
            continue
    return out


def _read_only(db: str, read):
    """``read(conn)`` on ``db`` through a connection that creates no file.

    ``mode=ro`` alone still creates the ``-wal`` and ``-shm`` sidecars of a WAL
    database nothing holds open. With both absent the database is fully
    checkpointed and ``immutable=1`` reads it as it is; otherwise some
    connection has it open and ``mode=ro`` reads through the sidecars it made.
    """
    from pathlib import Path

    from .config import _is_wal_without_sidecars

    uri = Path(os.path.abspath(db)).as_uri() + "?mode=ro"
    if _is_wal_without_sidecars(db):
        uri += "&immutable=1"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    try:
        return read(conn)
    finally:
        conn.close()


_COLLECTION_WINGS_SQL = """
    SELECT wm.string_value, COUNT(*)
    FROM embeddings e
    JOIN segments s ON e.segment_id = s.id AND s.scope = 'METADATA'
    JOIN collections c ON s.collection = c.id
    JOIN embedding_metadata wm ON wm.id = e.id AND wm.key = 'wing'
    WHERE c.name = ? AND wm.string_value IS NOT NULL
    GROUP BY wm.string_value
"""


def _sqlite_collection_counts(palace_path: str) -> Optional[dict]:
    """``{store: {wing: n}}`` for the metadata collections, read from ``chroma.sqlite3``.

    The same rows :func:`_collection_counts` reads through a Chroma client,
    without opening one: a client open records a write and rewrites the HNSW
    files even when nothing is changed. ``None`` when the palace has no
    ``chroma.sqlite3`` or the read fails, so the caller opens the collections.
    """
    from .config import get_configured_collection_name
    from .palace import AAAK_COLLECTION_NAME
    from .stored_text import decode_value

    db = os.path.join(palace_path, "chroma.sqlite3")
    if not os.path.isfile(db):
        return None
    names = {
        "drawers": get_configured_collection_name(),
        "closets": "mempalace_closets",
        "aaak": AAAK_COLLECTION_NAME,
    }

    def read(conn):
        out = {}
        for store, name in names.items():
            if not conn.execute("SELECT 1 FROM collections WHERE name = ?", (name,)).fetchone():
                continue
            counts: dict = defaultdict(int)
            for wing, n in conn.execute(_COLLECTION_WINGS_SQL, (name,)):
                wing = decode_value(wing)
                if isinstance(wing, str):
                    counts[wing] += int(n)
            out[store] = dict(counts)
        return out

    try:
        return _read_only(db, read)
    except sqlite3.Error:
        return None


def _hallway_counts_read_only(config) -> dict:
    """:func:`hallways.hallway_wing_counts` through a connection that creates no file."""
    from .hallways import _IMPORTED_KEY, _get_hallway_db, _load_hallways_json

    def read(conn):
        has_meta = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'"
        ).fetchone()
        if (
            not has_meta
            or not conn.execute("SELECT 1 FROM meta WHERE key = ?", (_IMPORTED_KEY,)).fetchone()
        ):
            return None
        rows = conn.execute("SELECT wing, COUNT(*) FROM hallways GROUP BY wing").fetchall()
        return {w: n for w, n in rows if isinstance(w, str)}

    db = _get_hallway_db(config)
    counts = None
    if os.path.exists(db):
        try:
            counts = _read_only(db, read)
        except sqlite3.Error:
            counts = None
    if counts is None:
        # Nothing imported yet: the records are still in hallways.json.
        counts = {}
        for h in _load_hallways_json(config):
            wing = h.get("wing") if isinstance(h, dict) else None
            if isinstance(wing, str):
                counts[wing] = counts.get(wing, 0) + 1
    return counts


def _collection_counts(col) -> dict:
    from .migrate import _iter_collection_items

    counts: dict = defaultdict(int)
    for _id, meta in _iter_collection_items(col):
        wing = (meta or {}).get("wing")
        if isinstance(wing, str):
            counts[wing] += 1
    return dict(counts)


def _documents_db(palace_path: str, explicit: bool) -> Optional[str]:
    from . import paths

    path = paths.knowledge_graph_path(palace_path, explicit=explicit)
    return path if os.path.isfile(path) else None


def _documents_counts(db: Optional[str], *, create_nothing: bool = False) -> dict:
    if not db:
        return {}
    if create_nothing:

        def read(conn):
            return conn.execute("SELECT wing, COUNT(*) FROM documents GROUP BY wing").fetchall()

        try:
            rows = _read_only(db, read)
        except sqlite3.OperationalError:
            return {}
        return {w: n for w, n in rows if isinstance(w, str)}
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT wing, COUNT(*) FROM documents GROUP BY wing").fetchall()
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()
    return {w: n for w, n in rows if isinstance(w, str)}


def _rename_documents(db: Optional[str], renames: dict) -> int:
    if not db or not renames:
        return 0
    conn = sqlite3.connect(db, timeout=30)
    moved = 0
    try:
        with conn:
            for old, new in renames.items():
                moved += conn.execute(
                    "UPDATE documents SET wing = ? WHERE wing = ?", (new, old)
                ).rowcount
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
    finally:
        conn.close()
    return moved


def _is_configured_palace(palace_path: str) -> bool:
    from . import paths
    from .config import MempalaceConfig

    return paths.same_path(palace_path, MempalaceConfig().palace_path)


def _topics_counts() -> dict:
    try:
        from .miner import _load_known_entities_raw

        tbw = _load_known_entities_raw().get("topics_by_wing")
    except Exception:
        return {}
    return {w: 1 for w in tbw} if isinstance(tbw, dict) else {}


# ── plan / apply ─────────────────────────────────────────────────────────────


def _counts(palace_path: str, config, explicit: bool, cols, with_topics: bool) -> dict:
    """``{store: {wing: n}}`` for every store this migration covers.

    ``cols`` is ``None`` for a dry run: every store is then read without
    opening a Chroma client and through sqlite connections that create no
    file, so the plan leaves the palace byte for byte as it was.
    """
    from .hallways import hallway_wing_counts
    from .palace_graph import tunnel_wing_counts

    dry = cols is None
    if dry:
        counts = _sqlite_collection_counts(palace_path)
        if counts is None:
            cols = _collections(palace_path, read_only=True)
    if cols is not None:
        counts = {store: _collection_counts(col) for store, col in cols.items()}
    counts["hallways"] = _hallway_counts_read_only(config) if dry else hallway_wing_counts(config)
    counts["tunnels"] = tunnel_wing_counts(config)
    counts["documents"] = _documents_counts(
        _documents_db(palace_path, explicit), create_nothing=dry
    )
    counts["topics_by_wing"] = _topics_counts() if with_topics else {}
    for store in STORES:
        counts.setdefault(store, {})
    return counts


def plan(counts: dict) -> dict:
    """``{old: new}`` for every stored wing, in any store, that is not canonical."""
    wings = set()
    for per_wing in counts.values():
        wings.update(per_wing)
    renames = {}
    for wing in sorted(wings):
        target = target_for(wing)
        if target is not None:
            renames[wing] = target
    return renames


def _check_no_hub(palace_path: str) -> None:
    from . import server_registry

    info = server_registry.read_live_serverinfo(palace_path)
    if info:
        raise HubHoldsPalace(
            f"a mempalace hub (pid {info.get('pid')}) is serving {palace_path}; "
            "stop it, or pass --palace pointing at a copy of the palace"
        )


def migrate_wings(palace_path: str, *, apply: bool = False, explicit: bool = False) -> dict:
    """Plan (``apply=False``) or perform the wing re-key of ``palace_path``.

    Returns ``{"palace", "applied", "renames": [{old, new, merge, <store>: n}],
    "moved": {store: n}, "before": {wing: drawers}, "after": {wing: drawers},
    "counts_ok": bool, "topics_by_wing": "included" | "skipped: ..."}``.
    ``before``/``after`` are drawer counts per canonical wing: the sum over a
    wing's sources before, and what the store holds after (equal to
    ``before`` on a dry run). Raises :class:`HubHoldsPalace` when a hub or
    another writer holds the palace.
    """
    from .config import MempalaceConfig
    from .palace import MineAlreadyRunning, mine_palace_lock

    palace_path = os.path.abspath(os.path.expanduser(palace_path))
    _check_no_hub(palace_path)
    config = MempalaceConfig(palace_path=palace_path)
    with_topics = _is_configured_palace(palace_path)
    if not apply:
        # A dry run writes nothing, not even the writer lease's lock files.
        return _migrate_locked(palace_path, config, False, explicit, with_topics)
    try:
        with mine_palace_lock(palace_path):
            return _migrate_locked(palace_path, config, apply, explicit, with_topics)
    except MineAlreadyRunning as exc:
        raise HubHoldsPalace(f"another writer holds {palace_path}: {exc}") from exc


def _expected_drawers(drawers: dict, renames: dict) -> dict:
    out: dict = defaultdict(int)
    for wing, n in drawers.items():
        out[renames.get(wing, wing)] += n
    return dict(out)


def _migrate_locked(palace_path, config, apply, explicit, with_topics) -> dict:
    from .hallways import rename_wing_hallways
    from .migrate import (
        _apply_topics_by_wing_renames,
        _apply_wing_updates,
        _iter_collection_items,
        _plan_topics_by_wing_renames,
        plan_wing_renames,
    )
    from .palace_graph import rename_tunnel_wings

    counts = _counts(
        palace_path,
        config,
        explicit,
        _collections(palace_path, read_only=True) if apply else None,
        with_topics,
    )
    renames = plan(counts)
    stored = set()
    for per_wing in counts.values():
        stored.update(per_wing)
    sources: dict = defaultdict(int)
    for new in renames.values():
        sources[new] += 1
    rows = []
    for old, new in renames.items():
        row = {"old": old, "new": new, "merge": new in stored or sources[new] > 1}
        for store in STORES:
            row[store] = counts[store].get(old, 0)
        rows.append(row)
    before = _expected_drawers(counts["drawers"], renames)
    result = {
        "palace": palace_path,
        "apply": bool(apply),
        "applied": False,
        "renames": rows,
        "moved": {store: 0 for store in STORES},
        "before": before,
        "after": dict(before),
        "counts_ok": True,
        "topics_by_wing": (
            "included"
            if with_topics
            else "skipped: the registry is per-user and this is not the configured palace"
        ),
    }
    if not apply or not renames:
        return result

    def target(wing):
        return renames.get(wing) if isinstance(wing, str) else None

    cols = _collections(palace_path)
    for store, col in cols.items():
        _summary, updates = plan_wing_renames(_iter_collection_items(col), target_for=target)
        _apply_wing_updates(col, updates)
        result["moved"][store] = len(updates)
    hallway_renames = {o: n for o, n in renames.items() if counts["hallways"].get(o)}
    if hallway_renames:
        result["moved"]["hallways"] = rename_wing_hallways(hallway_renames, config)["moved"]
    tunnel_renames = {o: n for o, n in renames.items() if counts["tunnels"].get(o)}
    result["moved"]["tunnels"] = rename_tunnel_wings(tunnel_renames, config)
    result["moved"]["documents"] = _rename_documents(
        _documents_db(palace_path, explicit),
        {o: n for o, n in renames.items() if counts["documents"].get(o)},
    )
    if with_topics:
        topic_renames = _plan_topics_by_wing_renames(target_for=target)
        _apply_topics_by_wing_renames(topic_renames)
        result["moved"]["topics_by_wing"] = len(topic_renames)

    after = _collection_counts(cols["drawers"]) if "drawers" in cols else {}
    result["applied"] = True
    result["after"] = {wing: after.get(wing, 0) for wing in before}
    result["counts_ok"] = result["after"] == before and set(after) <= set(before)
    return result


def format_report(result: dict) -> list[str]:
    """Human-readable lines for :func:`migrate_wings`' result."""
    mode = "APPLIED" if result["applied"] else ("APPLY" if result.get("apply") else "DRY RUN")
    lines = [f"  ws-migrate-wings: {result['palace']} ({mode})"]
    if not result["renames"]:
        lines.append("  Every stored wing is already canonical; nothing to migrate.")
        return lines
    for row in result["renames"]:
        merge = f"  (MERGE into {row['new']})" if row["merge"] else ""
        detail = ", ".join(f"{row[s]} {s}" for s in STORES if row[s])
        lines.append(
            f"    {row['old']} -> {row['new']}: {row['drawers']} drawers [{detail}]{merge}"
        )
    lines.append(f"  topics_by_wing: {result['topics_by_wing']}")
    if not result["applied"]:
        lines.append("  DRY RUN: nothing changed. Re-run with --apply to migrate.")
        return lines
    moved = ", ".join(f"{n} {s}" for s, n in result["moved"].items() if n)
    lines.append(f"  Moved: {moved or 'nothing'}")
    lines.append("  Drawers per migrated wing (before -> after):")
    targets = sorted({row["new"] for row in result["renames"]})
    for wing in targets:
        b, a = result["before"].get(wing, 0), result["after"].get(wing, 0)
        lines.append(f"    {wing}: {b} -> {a}{'' if a == b else '  MISMATCH'}")
    others = [w for w in result["before"] if w not in targets]
    changed = [w for w in others if result["after"].get(w, 0) != result["before"][w]]
    lines.append(
        f"    {len(others)} other wing(s): "
        + (f"MISMATCH in {', '.join(sorted(changed))}" if changed else "unchanged")
    )
    lines.append("  Counts match." if result["counts_ok"] else "  COUNT MISMATCH: see above.")
    return lines
