# mempalace/canary.py
"""Canary retrieval: confirm a FIXED set of known-good drawer IDs still surface.

The ID list is seeded once into a config file and then held constant -- that is
what makes it a real canary (it detects when specific known drawers vanish or
fail to retrieve after a re-embed). Uses exact-ID get(), never vector search,
so it is immune to HNSW/dimension issues.
"""
from __future__ import annotations

import json
import os

CANARY_FILE = os.environ.get(
    "MEMPALACE_CANARY_FILE",
    os.path.expanduser("~/.mempalace/canary_ids.json"),
)


def _raw(collection):
    """Return the underlying chromadb collection (unwrap the ChromaCollection adapter)."""
    return getattr(collection, "_collection", collection)


def _result_ids(res) -> set:
    """Normalize ids from a typed GetResult, dict-compat result, or plain dict."""
    ids = getattr(res, "ids", None)
    if ids is None:
        try:
            ids = res.get("ids", [])
        except AttributeError:
            ids = []
    return set(ids or [])


def load_canary_ids() -> list:
    if not os.path.exists(CANARY_FILE):
        return []
    try:
        with open(CANARY_FILE) as f:
            return json.load(f).get("ids", [])
    except Exception:
        return []


def seed_canary_ids(collection, n: int = 5) -> list:
    """One-time: pick n current IDs and persist them as the fixed canary set."""
    raw = _raw(collection)
    sample = raw.get(limit=n, include=[])
    ids = list(_result_ids(sample))[:n]
    os.makedirs(os.path.dirname(CANARY_FILE), exist_ok=True)
    with open(CANARY_FILE, "w") as f:
        json.dump({"ids": ids, "note": "I4 canary -- fixed known-good drawers"}, f, indent=2)
    return ids


def run_canary_check(collection, canary_ids=None) -> dict:
    """Get the fixed canary IDs by exact id; report any that fail to surface."""
    canary_ids = canary_ids if canary_ids is not None else load_canary_ids()
    if not canary_ids:
        return {"status": "unseeded", "missing_ids": [], "sampled": 0}
    try:
        res = _raw(collection).get(ids=canary_ids, include=[])
    except Exception as e:
        return {"status": "fail", "error": str(e), "missing_ids": canary_ids, "sampled": len(canary_ids)}
    returned = _result_ids(res)
    missing = [cid for cid in canary_ids if cid not in returned]
    return {"status": "fail" if missing else "ok", "missing_ids": missing, "sampled": len(canary_ids)}
