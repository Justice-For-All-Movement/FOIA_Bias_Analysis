"""FBA-04 read-only, resumable consumer for partitioned Aggregator exports.

Consumes a local export laid out per ``aggregator_contract`` (manifest plus
JSONL partitions). It never fetches sources, never writes the export and never
falls back to crawling. Memory is bounded by one record, one batch and the snapshot's record-ID set
used for duplicate detection during verification.

Every partition is first verified (SHA-256, count, every row validated, no
duplicate IDs across the snapshot) before anything is ingested; then records
from the durable cursor onward are ingested in batches; each batch commits records and cursor in one transaction.
A batch cap pauses progress but never shrinks the census: the summary always
reports the manifest's full total and how much remains.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from foia_bias import aggregator_contract as contract
from foia_bias.checkpoints import (acquire_lease, advance, check_lease, compatibility_hash, open_cursor,
                                   release_lease, verify_and_repair)
from foia_bias.corpus import MAX_MANIFEST_BYTES, MAX_RECORD_BYTES, local_path, strict_json


class ConsumerError(ValueError):
    pass


def load_manifest(export_dir, expected_fixture: bool):
    root = Path(export_dir)
    path = local_path(root, "manifest.json")
    with path.open("rb") as stream:
        raw = stream.read(MAX_MANIFEST_BYTES + 1)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ConsumerError("manifest exceeds 1 MiB boundary")
    manifest = contract.validate_manifest(strict_json(raw.decode("utf-8")), expected_fixture=expected_fixture)
    return manifest, hashlib.sha256(raw).hexdigest()


def _iter_rows(path):
    with path.open("rb") as stream:
        ordinal = 0
        while True:
            line = stream.readline(MAX_RECORD_BYTES + 1)
            if not line:
                return
            if len(line) > MAX_RECORD_BYTES:
                raise ConsumerError("record exceeds 16 MiB boundary")
            if not line.strip():
                raise ConsumerError("blank line in partition")
            yield ordinal, line, strict_json(line.decode("utf-8"))
            ordinal += 1


def verify_partition(root, partition, fixture, seen_ids):
    path = local_path(root, partition["path"])
    digest = hashlib.sha256()
    count = 0
    for _, line, row in _iter_rows(path):
        digest.update(line)
        contract.validate_record(row, fixture)
        if row["record_id"] in seen_ids:
            raise ConsumerError(f"duplicate record_id {row['record_id']} within snapshot")
        seen_ids.add(row["record_id"])
        count += 1
    if digest.hexdigest() != partition["sha256"]:
        raise ConsumerError(f"partition {partition['partition_id']} SHA-256 mismatch")
    if count != partition["count"]:
        raise ConsumerError(f"partition {partition['partition_id']} count mismatch (truncated or padded)")
    return path


def consume(ledger, export_dir, *, job_id="consume", batch_size=500, max_batches=None, expected_fixture=True,
            analysis_config=None, owner="consumer", now=None, hooks=None):
    if type(batch_size) is not int or not 1 <= batch_size <= 10000:
        raise ConsumerError("batch_size must be 1..10000")
    if max_batches is not None and (type(max_batches) is not int or max_batches < 1):
        raise ConsumerError("max_batches must be a positive integer")
    hooks = hooks or {}
    now = now or ledger.clock()
    root = Path(export_dir).resolve(strict=True)
    manifest, manifest_sha = load_manifest(root, expected_fixture)
    fixture = manifest["fixture_mode"]
    snapshot_id = manifest["snapshot_id"]
    ledger.register_snapshot(manifest, manifest_sha)  # raises if the snapshot token changed
    compat = compatibility_hash(snapshot_id, manifest_sha,
                                {"contract_version": manifest["contract_version"], **(analysis_config or {})})
    acquire_lease(ledger, job_id, owner, now)
    try:
        open_cursor(ledger, job_id, snapshot_id, compat)
        partitions = manifest["partitions"]
        cursor = verify_and_repair(ledger, job_id, [(p["partition_id"], p["count"]) for p in partitions])
        batches = 0
        seen_ids: set = set()
        paused = False
        # Verify the entire snapshot before acknowledging any of it, so an
        # invalid export never yields a partial ingest.
        paths = [verify_partition(root, partition, fixture, seen_ids) for partition in partitions]
        seen_ids.clear()
        for index, partition in enumerate(partitions):
            path = paths[index]
            if index < cursor.partition_index:
                continue
            start = cursor.record_offset if index == cursor.partition_index else 0
            batch = []
            for ordinal, _, row in _iter_rows(path):
                if ordinal < start:
                    continue
                batch.append((ordinal, row))
                if len(batch) == batch_size:
                    _commit(ledger, job_id, snapshot_id, partition, index, batch, owner, now, hooks, batches)
                    batches += 1
                    batch = []
                    if max_batches is not None and batches >= max_batches:
                        paused = True
                        break
            if paused:
                break
            if batch:
                _commit(ledger, job_id, snapshot_id, partition, index, batch, owner, now, hooks, batches)
                batches += 1
            with ledger.transaction():
                check_lease(ledger, job_id, owner, now)
                advance(ledger, job_id, index + 1, 0, 0)
            if max_batches is not None and batches >= max_batches and index + 1 < len(partitions):
                paused = True
                break
        committed = ledger.record_count(snapshot_id)
        total = manifest["totals"]["records"]
        status = "paused_batch_cap" if paused and committed < total else "complete"
        if status == "complete":
            if committed != total:
                raise ConsumerError("committed records do not match manifest census")
            census = ledger.census(snapshot_id)
            for key, column in (("source_families", "source_family"), ("record_types", "record_type")):
                if census[column] != {k: v for k, v in manifest["coverage"][key].items() if v}:
                    raise ConsumerError(f"census disagrees with manifest coverage.{key}")
            with ledger.transaction():
                advance(ledger, job_id, len(partitions), 0, 0, status="complete")
        return {
            "status": status, "snapshot_id": snapshot_id, "manifest_sha256": manifest_sha,
            "contract_version": manifest["contract_version"], "producer": manifest["producer"],
            "fixture_mode": fixture, "records_total": total, "records_committed": committed,
            "records_remaining": total - committed, "batches_this_run": batches,
            "unresolved_failures": manifest["unresolved_failures"],
            "analysis_performed": False, "research_eligible": False,
        }
    finally:
        release_lease(ledger, job_id, owner)


def _commit(ledger, job_id, snapshot_id, partition, index, batch, owner, now, hooks, batch_number):
    with ledger.transaction():
        check_lease(ledger, job_id, owner, now)
        for ordinal, row in batch:
            if not ledger.ingest_record(snapshot_id, row, partition["partition_id"], ordinal):
                raise ConsumerError(f"record {row['record_id']} already stored; cursor inconsistent")
        if "before_commit" in hooks:
            hooks["before_commit"](batch_number)
        advance(ledger, job_id, index, batch[-1][0] + 1, len(batch))
