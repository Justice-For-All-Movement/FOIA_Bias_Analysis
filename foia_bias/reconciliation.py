"""FBA-05 shared-corpus deduplication weights and legacy artifact reconciliation.

Archive identity and raw preservation belong to FOIA_Aggregator. Bias only
declares an *analytical* duplicate policy and reconciles its legacy cache.

* ``duplicate_clusters``: records sharing a preserved binary SHA-256 (FC-13
  ``preservation.sha256``) or an explicit ``duplicate_of`` relation form one
  disclosure. Each cluster keeps the union of all provenance and source
  families, so no association is lost. Different redactions (different hashes)
  stay distinct.
* ``analytical_weights``: ``representative`` (1 for the smallest record ID per
  cluster, else 0) or ``fractional`` (1/cluster size); weights sum to the
  number of distinct disclosures.
* ``reconcile_legacy_artifacts``: hashes legacy cached files (read-only, regular
  files only, size-bounded, no symlinks), matches them to snapshot records by
  binary hash, and records unmatched files as upstream import candidates.
  Legacy files never become a separate Bias archive.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

from foia_bias.ledger import canonical

MAX_LEGACY_FILE = 2 * 1024 * 1024 * 1024


def duplicate_clusters(rows):
    parent = {r["record_id"]: r["record_id"] for r in rows}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        a, b = find(a), find(b)
        if a != b:
            parent[max(a, b)] = min(a, b)
    first = {}
    for r in sorted(rows, key=lambda r: r["record_id"]):
        if r.get("original_sha256"):
            union(r["record_id"], first.setdefault(r["original_sha256"], r["record_id"]))
        for rel in r.get("relations", []):
            if rel["kind"] == "duplicate_of" and rel["record_id"] in parent:
                union(r["record_id"], rel["record_id"])
    clusters = {}
    for r in rows:
        clusters.setdefault(find(r["record_id"]), []).append(r)
    out = []
    for root, members in sorted(clusters.items()):
        provenance = sorted({(p["source_id"], p["source_url"]) for m in members for p in m["provenance"]})
        out.append({"cluster_id": root, "members": sorted(m["record_id"] for m in members),
                    "source_families": sorted({m["source_family"] for m in members}),
                    "provenance": [{"source_id": s, "source_url": u} for s, u in provenance]})
    return out


def analytical_weights(rows, policy="representative"):
    if policy not in ("representative", "fractional"):
        raise ValueError("unknown weighting policy")
    weights = {}
    for cluster in duplicate_clusters(rows):
        size = len(cluster["members"])
        for rid in cluster["members"]:
            if policy == "fractional":
                weights[rid] = 1.0 / size
            else:
                weights[rid] = 1.0 if rid == cluster["cluster_id"] else 0.0
    return weights


def _hash_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def reconcile_legacy_artifacts(ledger, snapshot_id, legacy_dir):
    root = Path(legacy_dir)
    by_binary = {}
    for row in ledger.records(snapshot_id):
        if row["original_sha256"]:
            by_binary.setdefault(row["original_sha256"], []).append(row["record_id"])
    matched, candidates, skipped = [], [], []
    for path in sorted(root.rglob("*")):
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode):
            skipped.append({"path": str(path.relative_to(root)), "reason": "symlink not followed"})
            continue
        if not stat.S_ISREG(info.st_mode):
            continue
        if info.st_size > MAX_LEGACY_FILE:
            skipped.append({"path": str(path.relative_to(root)), "reason": "exceeds size bound"})
            continue
        digest = _hash_file(path)
        relative = str(path.relative_to(root))
        records = sorted(by_binary.get(digest, []))
        detail = {"snapshot_id": snapshot_id, "matched_records": records, "size": info.st_size,
                  "status": "matched" if records else "upstream_import_candidate"}
        with ledger.transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO legacy_artifacts VALUES (?,?,?,?,?)",
                         (relative, digest, "legacy_cached_file", canonical(detail), ledger.clock()))
        (matched if records else candidates).append({"path": relative, "sha256": digest, "records": records})
    report = {"snapshot_id": snapshot_id, "matched": matched, "upstream_import_candidates": candidates,
              "skipped": skipped,
              "note": "legacy files are left untouched and are not added to the analytical corpus"}
    return dict(report, report_sha256=hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest())
