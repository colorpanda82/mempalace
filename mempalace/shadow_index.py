#!/usr/bin/env python3
"""
shadow_index.py — F5 HyDE-style read-only consistency flagging.

Samples N triples from the knowledge graph, synthesizes 2-3 hypothetical
queries per triple via deterministic templates (no LLM dependency in v1),
retrieves ChromaDB drawer evidence through the existing searcher, and
flags triples whose top retrieved drawers fail to mention the subject
together with either the predicate or the object.

Read-only — never mutates the KG or drawers. Flagged rows append to
`~/.mempalace/shadow-audits.jsonl`.
"""

import argparse
import json
import os
import random
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import MempalaceConfig
from .knowledge_graph import DEFAULT_KG_PATH
from .searcher import search_memories


AUDIT_PATH = Path.home() / ".mempalace" / "shadow-audits.jsonl"


def _sample_triples(kg_path, n=20, only_active=True):
    if not os.path.exists(kg_path):
        return []
    conn = sqlite3.connect(kg_path)
    conn.row_factory = sqlite3.Row
    where = "WHERE valid_to IS NULL" if only_active else ""
    rows = conn.execute(
        f"SELECT id, subject, predicate, object, valid_from, valid_to, "
        f"confidence, source_drawer_id FROM triples {where}"
    ).fetchall()
    conn.close()
    if not rows:
        return []
    return random.sample(rows, min(n, len(rows)))


def _hyde_queries(triple):
    """Deterministic HyDE — 3 query candidates per triple, no LLM call."""
    s = triple["subject"]
    p = triple["predicate"].replace("_", " ")
    o = triple["object"]
    return [
        f"{s} {p} {o}",
        f"What is the relationship between {s} and {o}?",
        f"{s} {p}",
    ]


def _evidence_supports(evidence_docs, subject, predicate, object_):
    """Loose match: any retrieved doc mentions subject AND (predicate OR object)."""
    if not evidence_docs:
        return False
    s = subject.lower()
    p = predicate.replace("_", " ").lower()
    o = object_.lower()
    for doc in evidence_docs:
        d = (doc or "").lower()
        if s in d and (p in d or o in d):
            return True
    return False


def _extract_docs(hits):
    """Normalize search_memories return shape into a flat list of doc strings."""
    if not hits:
        return []
    if isinstance(hits, list):
        return [h if isinstance(h, str) else h.get("content", "") or h.get("document", "")
                for h in hits]
    if isinstance(hits, dict):
        docs = hits.get("documents") or hits.get("results") or []
        if docs and isinstance(docs[0], list):
            docs = docs[0]
        out = []
        for d in docs:
            if isinstance(d, str):
                out.append(d)
            elif isinstance(d, dict):
                out.append(d.get("content", "") or d.get("document", ""))
        return out
    return []


def audit(palace_path=None, kg_path=None, n_samples=20, n_results=5, audit_path=None):
    cfg = MempalaceConfig()
    pp = palace_path or cfg.palace_path
    kgp = kg_path or DEFAULT_KG_PATH
    ap = Path(audit_path) if audit_path else AUDIT_PATH
    ap.parent.mkdir(parents=True, exist_ok=True)

    triples = _sample_triples(kgp, n=n_samples)
    if not triples:
        return {"sampled": 0, "flagged": 0, "audit_path": str(ap),
                "note": "no triples in KG (or kg path missing)"}

    flagged_count = 0
    audited_at = datetime.now(timezone.utc).isoformat()
    for tr in triples:
        s, p, o = tr["subject"], tr["predicate"], tr["object"]
        supports = False
        evidence_summary = []
        for q in _hyde_queries(tr):
            try:
                hits = search_memories(query=q, palace_path=pp, n_results=n_results)
            except Exception as e:
                evidence_summary.append({"q": q, "error": str(e)[:120]})
                continue
            docs = _extract_docs(hits)
            evidence_summary.append({"q": q, "n_docs": len(docs)})
            if _evidence_supports(docs, s, p, o):
                supports = True
                break
        if not supports:
            row = {
                "audited_at": audited_at,
                "triple_id": tr["id"],
                "subject": s,
                "predicate": p,
                "object": o,
                "valid_from": tr["valid_from"],
                "source_drawer_id": tr["source_drawer_id"],
                "evidence": evidence_summary,
            }
            with open(ap, "a") as f:
                f.write(json.dumps(row, sort_keys=True) + "\n")
            flagged_count += 1

    return {"sampled": len(triples), "flagged": flagged_count, "audit_path": str(ap)}


def list_flagged(audit_path=None, limit=20):
    ap = Path(audit_path) if audit_path else AUDIT_PATH
    if not ap.exists():
        return []
    lines = ap.read_text().splitlines()[-limit:]
    return [json.loads(l) for l in lines if l.strip()]


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="mempalace shadow-index",
        description="F5 HyDE read-only flagging of KG triples vs ChromaDB drawers",
    )
    sub = p.add_subparsers(dest="action", required=True)

    p_audit = sub.add_parser("audit", help="Sample triples and flag unsupported ones")
    p_audit.add_argument("--n-samples", type=int, default=20)
    p_audit.add_argument("--n-results", type=int, default=5)

    p_list = sub.add_parser("list-flagged", help="Show recent flagged triples")
    p_list.add_argument("--limit", type=int, default=20)

    args = p.parse_args(argv)
    if args.action == "audit":
        result = audit(n_samples=args.n_samples, n_results=args.n_results)
        print(json.dumps(result, sort_keys=True))
        return 0
    if args.action == "list-flagged":
        for row in list_flagged(limit=args.limit):
            print(json.dumps(row, sort_keys=True))
        return 0


if __name__ == "__main__":
    sys.exit(main())
