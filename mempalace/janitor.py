#!/usr/bin/env python3
"""
janitor.py — F5 Phase 3 self-healing decay scoring (read-only by default).

Computes decay_score = importance * recency_weight per ChromaDB drawer.
Per-namespace half-lives:

    Drawer   14 days
    Room     45 days
    Hall    180 days
    Wing    never (1.0 multiplier)

The decay score affects PROMOTION CANDIDATES (Layer1 Essential Story
re-ranking via layers.py), not direct-query reads. Decayed drawers stay
retrievable via `mempalace_search` (Layer 3 Deep Search) — they are
deprioritized only in the always-loaded L1 wake-up surface.

Default mode is dry-run report. Backfill of `accessed_at = created_at` for
drawers missing the metadata field is opt-in via `--backfill --apply`.
"""

import argparse
import json
import math
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from .config import MempalaceConfig
from .palace import get_collection


NAMESPACE_HALFLIVES_DAYS = {
    "drawer": 14.0,
    "room": 45.0,
    "hall": 180.0,
    "wing": float("inf"),
}

DRY_RUN_SENTINEL = Path.home() / ".mempalace" / "janitor-dry-run-since.txt"
REPORT_DIR = Path.home() / ".mempalace" / "janitor-reports"
DRY_RUN_MIN_DAYS = 7


def _parse_ts(value):
    """Parse various metadata timestamp shapes; return aware UTC datetime or None."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    if isinstance(value, str):
        try:
            v = value.rstrip("Z")
            dt = datetime.fromisoformat(v)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            return None
    return None


def _drawer_namespace(meta):
    """Pick a namespace label for the drawer.

    A drawer always exists, so the floor is `drawer` (14-day half-life).
    If the drawer carries a `hall` tag we widen to hall (180d). `room`
    sits in between (45d). `wing` is reserved for entries that explicitly
    opt out of decay by setting only a wing-level grouping.
    """
    if meta.get("hall"):
        return "hall"
    if meta.get("room"):
        return "room"
    if meta.get("wing"):
        return "wing"
    return "drawer"


def _importance(meta):
    """Resolve importance weight; default 3 (matches Layer1 default)."""
    for key in ("importance", "emotional_weight", "weight"):
        val = meta.get(key)
        if val is None:
            continue
        try:
            return float(val)
        except (ValueError, TypeError):
            continue
    return 3.0


def _drawer_age_days(meta, now=None):
    """Days since the drawer was last accessed (preferred) or first filed.

    Returns 0.0 when no timestamp exists at all, so pre-enrichment drawers
    are not punished by the decay multiplier on first scan.
    """
    now = now or datetime.now(timezone.utc)
    for key in ("accessed_at", "last_accessed", "created_at", "filed_at"):
        dt = _parse_ts(meta.get(key))
        if dt is not None:
            delta = now - dt
            return max(0.0, delta.total_seconds() / 86400.0)
    return 0.0


def _recency_weight(age_days, namespace):
    """Exponential decay against namespace half-life. Wing = 1.0 (no decay)."""
    half_life = NAMESPACE_HALFLIVES_DAYS[namespace]
    if math.isinf(half_life):
        return 1.0
    return 0.5 ** (age_days / half_life)


def score_drawer(meta, now=None):
    """Pure function: return scoring dict for one drawer's metadata.

    F5-v1.1: round recency before multiplying so that
    decay_score == round(importance * recency_weight, 4) holds exactly.
    """
    meta = meta or {}
    importance = _importance(meta)
    namespace = _drawer_namespace(meta)
    age_days = _drawer_age_days(meta, now=now)
    recency = round(_recency_weight(age_days, namespace), 4)
    decay_score = round(importance * recency, 4)
    return {
        "decay_score": decay_score,
        "importance": importance,
        "age_days": round(age_days, 2),
        "namespace": namespace,
        "recency_weight": recency,
    }


def scan(palace_path=None, wing=None, limit=None):
    """Scan palace drawers, score each, return list of report rows."""
    cfg = MempalaceConfig()
    pp = palace_path or cfg.palace_path
    col = get_collection(pp, create=False)

    now = datetime.now(timezone.utc)
    _BATCH = 500
    offset = 0
    rows = []
    while True:
        kwargs = {"include": ["metadatas"], "limit": _BATCH, "offset": offset}
        if wing:
            kwargs["where"] = {"wing": wing}
        try:
            batch = col.get(**kwargs)
        except Exception as e:
            print(f"janitor: chroma get failed at offset {offset}: {e}", file=sys.stderr)
            break
        ids = batch.get("ids", []) or []
        metas = batch.get("metadatas", []) or []
        if not ids:
            break
        for did, meta in zip(ids, metas):
            meta = meta or {}
            scored = score_drawer(meta, now=now)
            scored["drawer_id"] = did
            scored["wing"] = meta.get("wing")
            scored["room"] = meta.get("room")
            scored["hall"] = meta.get("hall")
            rows.append(scored)
        if limit and len(rows) >= limit:
            rows = rows[:limit]
            break
        offset += len(ids)
        if len(ids) < _BATCH:
            break
    return rows


def _ensure_dry_run_sentinel():
    if not DRY_RUN_SENTINEL.exists():
        DRY_RUN_SENTINEL.parent.mkdir(parents=True, exist_ok=True)
        DRY_RUN_SENTINEL.write_text(datetime.now(timezone.utc).isoformat() + "\n")


def write_report(rows, report_dir=None):
    rd = Path(report_dir) if report_dir else REPORT_DIR
    rd.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = rd / f"{today}.jsonl"
    with open(path, "a") as f:
        for r in rows:
            f.write(json.dumps(r, sort_keys=True) + "\n")
    return path


def summarize(rows):
    if not rows:
        return {"total": 0}
    scores = [r["decay_score"] for r in rows]
    by_ns = {}
    for r in rows:
        by_ns.setdefault(r["namespace"], 0)
        by_ns[r["namespace"]] += 1
    return {
        "total": len(rows),
        "by_namespace": by_ns,
        "decay_min": round(min(scores), 4),
        "decay_max": round(max(scores), 4),
        "decay_mean": round(sum(scores) / len(scores), 4),
        "below_one_pct": round(100 * sum(1 for s in scores if s < 1.0) / len(rows), 1),
    }


def backfill_metadata(palace_path=None, dry_run=True):
    """Seed accessed_at = created_at and access_count = 0 where missing.

    Lazy backfill — janitor-driven, not search-time. Idempotent. Skips drawers
    that already have `accessed_at`. Skips drawers with no `created_at` /
    `filed_at` to seed from.
    """
    cfg = MempalaceConfig()
    pp = palace_path or cfg.palace_path
    col = get_collection(pp, create=False)
    _BATCH = 200
    offset = 0
    touched = 0
    while True:
        try:
            batch = col.get(include=["metadatas"], limit=_BATCH, offset=offset)
        except Exception as e:
            print(f"backfill: chroma get failed at offset {offset}: {e}", file=sys.stderr)
            break
        ids = batch.get("ids", []) or []
        metas = batch.get("metadatas", []) or []
        if not ids:
            break
        to_update_ids = []
        to_update_metas = []
        for did, m in zip(ids, metas):
            m = dict(m or {})
            if "accessed_at" in m and m["accessed_at"]:
                continue
            seed = m.get("created_at") or m.get("filed_at")
            if not seed:
                continue
            m["accessed_at"] = seed
            if "access_count" not in m:
                m["access_count"] = 0
            to_update_ids.append(did)
            to_update_metas.append(m)
        if to_update_ids:
            if not dry_run:
                try:
                    col.update(ids=to_update_ids, metadatas=to_update_metas)
                except Exception as e:
                    print(f"backfill: update failed at offset {offset}: {e}", file=sys.stderr)
                    break
            touched += len(to_update_ids)
        offset += len(ids)
        if len(ids) < _BATCH:
            break
    return touched


_EXPIRES_AT_RE = re.compile(r'expires_at:\s*(\d{4}-\d{2}-\d{2})', re.MULTILINE)


def prune_expired(palace_path=None, dry_run=True):
    """Delete drawers whose content contains 'expires_at: YYYY-MM-DD' past today.

    Convention: any working/task-state drawer may include a line
        expires_at: 2026-05-25
    to request automatic TTL deletion. Semantic drawers (facts, config,
    lessons-learned) should never include this field.

    Returns (scanned, deleted) counts.
    """
    cfg = MempalaceConfig()
    pp = palace_path or cfg.palace_path
    col = get_collection(pp, create=False)

    today = date.today()
    _BATCH = 200
    offset = 0
    scanned = 0
    to_delete = []

    while True:
        try:
            batch = col.get(include=["documents", "metadatas"], limit=_BATCH, offset=offset)
        except Exception as e:
            print(f"prune_expired: chroma get failed at offset {offset}: {e}", file=sys.stderr)
            break
        ids = batch.get("ids", []) or []
        docs = batch.get("documents", []) or []
        metas = batch.get("metadatas", []) or []
        if not ids:
            break
        for did, doc, meta in zip(ids, docs, metas):
            scanned += 1
            exp_str = None
            # Check content body first
            if doc:
                m = _EXPIRES_AT_RE.search(doc)
                if m:
                    exp_str = m.group(1)
            # Fallback: check metadata field
            if exp_str is None and meta:
                exp_str = meta.get("expires_at")
            if exp_str is None:
                continue
            try:
                exp_date = date.fromisoformat(exp_str)
            except ValueError:
                continue
            if exp_date < today:
                meta = meta or {}
                to_delete.append((did, meta.get("wing", "?"), meta.get("room", "?"), exp_str))
        offset += len(ids)
        if len(ids) < _BATCH:
            break

    if to_delete:
        for did, wing, room, exp_str in to_delete:
            action = "would-delete" if dry_run else "deleting"
            print(f"prune_expired: {action} [{wing}/{room}] {did[:48]}... (expired {exp_str})")
        if not dry_run:
            # Hub-first (v3.9.0+): while a `mempalace serve` hub owns the palace a
            # direct col.delete() is exactly the racing-writer path #2079 forbids.
            # Route through the hub's delete tool when one is registered; the
            # direct delete stays for palaces no hub serves.
            from .cli_workspace import _hub_for, call_tool
            from .config import MempalaceConfig
            _palace = MempalaceConfig().palace_path
            if _hub_for(_palace) is not None:
                for did, wing, room, _exp in to_delete:
                    r = call_tool("delete_drawer", {"drawer_id": did}, palace_path=_palace)
                    if isinstance(r, dict) and (r.get("success") is False or "error" in r):
                        print(f"prune_expired: hub refused delete of {did[:48]}: {r.get('error')}")
            else:
                col.delete(ids=[d[0] for d in to_delete])

    return scanned, len(to_delete)


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="mempalace janitor",
        description="F5 decay scoring (read-only) + optional accessed_at backfill + TTL pruning",
    )
    p.add_argument("--wing", default=None, help="Limit scan to one wing")
    p.add_argument("--limit", type=int, default=None, help="Cap drawers scanned")
    p.add_argument("--backfill", action="store_true",
                   help="Seed accessed_at=created_at for drawers missing it")
    p.add_argument("--prune-expired", action="store_true",
                   help="Delete drawers with 'expires_at: YYYY-MM-DD' past today")
    p.add_argument("--apply", action="store_true",
                   help="Commit writes for --backfill or --prune-expired (default: dry-run)")
    args = p.parse_args(argv)

    _ensure_dry_run_sentinel()

    if args.backfill:
        n = backfill_metadata(dry_run=not args.apply)
        action = "backfilled" if args.apply else "would-backfill (dry-run)"
        print(f"janitor: {action} {n} drawers")
        return 0

    if args.prune_expired:
        scanned, deleted = prune_expired(dry_run=not args.apply)
        mode = "deleted" if args.apply else "would-delete (dry-run)"
        print(f"janitor: prune_expired scanned={scanned} {mode}={deleted}")
        return 0

    rows = scan(wing=args.wing, limit=args.limit)
    report_path = write_report(rows)
    summary = summarize(rows)
    print(f"janitor: wrote {len(rows)} rows -> {report_path}")
    print(f"janitor: summary {json.dumps(summary, sort_keys=True)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
