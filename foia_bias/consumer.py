"""FBA-04 read-only, resumable consumer for FC-13 catalog snapshots.

Consumes a snapshot written by FOIA_Aggregator ``python main.py export-catalog
<dir>`` (``foia-catalog-export/1``: ``manifest.json`` + ``records-NNNNN.jsonl``),
or the same records replayed from the paginated API (``aggregator_api``). It
never fetches sources, never writes the export and never falls back to
crawling.

Every file is verified first (SHA-256, count, every record against the pinned
JSON Schema and invariants, record-ID uniqueness, manifest archived/indexed/
agency counts) before anything is ingested; then records from the durable
cursor onward are ingested in batches that commit records and cursor in one
transaction. A batch cap pauses progress but never shrinks the census.

Fixture snapshots carry a ``FIXTURE.json`` marker; a live snapshot with one is
rejected. Document text comes from the separate ``foia-document-text-export/1``
snapshot (``text_export``); each text entry must belong to a catalog record with
the same URL and the same archived-binary SHA-256, or the run fails.
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
    manifest = contract.validate_catalog_manifest(strict_json(raw.decode("utf-8")))
    marker = root / contract.FIXTURE_MARKER
    fixture = marker.is_file()
    if fixture:
        data = strict_json(local_path(root, contract.FIXTURE_MARKER).read_text(encoding="utf-8"))
        if data != {"synthetic": True, "note": data.get("note")} or not isinstance(data.get("note"), str):
            raise ConsumerError("FIXTURE.json must be {\"synthetic\": true, \"note\": \"...\"}")
    if fixture != expected_fixture:
        raise contract.ContractError("fixture mode mismatch: fixture snapshots can never pass as live (or vice versa)")
    return manifest, hashlib.sha256(raw).hexdigest(), fixture


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
                raise ConsumerError("blank line in snapshot file")
            yield ordinal, line, strict_json(line.decode("utf-8"))
            ordinal += 1


def verify_files(root, manifest):
    """Verify all files; return (paths, tallies). Raises before any ingest."""
    seen, paths, catalog = set(), [], {}
    tallies = {"archived": 0, "text_indexed": 0, "agencies": set()}
    for entry in manifest["files"]:
        path = local_path(root, entry["path"])
        digest, count = hashlib.sha256(), 0
        for _, line, row in _iter_rows(path):
            digest.update(line)
            contract.validate_catalog_record(row)
            if row["record_id"] in seen:
                raise ConsumerError(f"duplicate record_id {row['record_id']} within snapshot")
            seen.add(row["record_id"])
            catalog[row["record_id"]] = (row["url"], row["preservation"]["sha256"], row["text"]["indexed"])
            tallies["archived"] += row["preservation"]["status"] == "archived"
            tallies["text_indexed"] += row["text"]["indexed"]
            if row["agency"]:
                tallies["agencies"].add(row["agency"]["slug"])
            count += 1
        if digest.hexdigest() != entry["sha256"]:
            raise ConsumerError(f"{entry['path']} SHA-256 mismatch")
        if count != entry["records"]:
            raise ConsumerError(f"{entry['path']} record count mismatch (truncated or padded)")
        paths.append(path)
    counts = manifest["counts"]
    if tallies["archived"] != counts["archived"] or tallies["text_indexed"] != counts["text_indexed"]:
        raise ConsumerError("manifest archived/text_indexed counts disagree with records")
    if len(tallies["agencies"]) != counts["agencies"]:
        raise ConsumerError("manifest agency count disagrees with records")
    return paths, catalog


def consume(ledger, export_dir, *, job_id="consume", batch_size=500, max_batches=None, expected_fixture=True,
            analysis_config=None, owner="consumer", now=None, hooks=None, text_export=None):
    if type(batch_size) is not int or not 1 <= batch_size <= 10000:
        raise ConsumerError("batch_size must be 1..10000")
    if max_batches is not None and (type(max_batches) is not int or max_batches < 1):
        raise ConsumerError("max_batches must be a positive integer")
    hooks = hooks or {}
    now = now or ledger.clock()
    root = Path(export_dir).resolve(strict=True)
    manifest, manifest_sha, fixture = load_manifest(root, expected_fixture)
    paths, catalog = verify_files(root, manifest)
    texts, text_info = {}, None
    if text_export is not None:
        from foia_bias.extensions import load_text_export
        loaded = load_text_export(text_export, expected_fixture=fixture)
        for record_id, entry in loaded["texts"].items():
            known = catalog.get(record_id)
            if known is None or known[0] != entry["url"] or not known[2] \
                    or entry["source_document"]["sha256"] != known[1]:
                raise ConsumerError(f"text for {record_id} does not associate with a matching catalog record")
            texts[record_id] = entry["text"]
        text_info = {"snapshot_id": loaded["manifest"]["snapshot_id"], "manifest_sha256": loaded["manifest_sha256"],
                     "producer_commit": loaded["producer_commit"], "records": len(texts)}
    snapshot_id = manifest["snapshot_id"]
    ledger.register_snapshot(contract.snapshot_info(manifest, manifest_sha, fixture))
    compat = compatibility_hash(snapshot_id, manifest_sha,
                                {"contract_version": manifest["schema_version"],
                                 "contract_commit": contract.AGGREGATOR_CONTRACT_COMMIT,
                                 "text_export": text_info["manifest_sha256"] if text_info else None,
                                 **(analysis_config or {})})
    acquire_lease(ledger, job_id, owner, now)
    try:
        open_cursor(ledger, job_id, snapshot_id, compat)
        files = manifest["files"]
        cursor = verify_and_repair(ledger, job_id, [(f["path"], f["records"]) for f in files])
        batches, paused = 0, False
        for index, entry in enumerate(files):
            if index < cursor.partition_index:
                continue
            start = cursor.record_offset if index == cursor.partition_index else 0
            batch = []
            for ordinal, _, row in _iter_rows(paths[index]):
                if ordinal < start:
                    continue
                batch.append((ordinal, contract.to_analysis_record(row, fixture=fixture,
                                                                    text=texts.get(row["record_id"]))))
                if len(batch) == batch_size:
                    _commit(ledger, job_id, snapshot_id, entry["path"], index, batch, owner, now, hooks, batches)
                    batches += 1
                    batch = []
                    if max_batches is not None and batches >= max_batches:
                        paused = True
                        break
            if paused:
                break
            if batch:
                _commit(ledger, job_id, snapshot_id, entry["path"], index, batch, owner, now, hooks, batches)
                batches += 1
            with ledger.transaction():
                check_lease(ledger, job_id, owner, now)
                advance(ledger, job_id, index + 1, 0, 0)
            if max_batches is not None and batches >= max_batches and index + 1 < len(files):
                paused = True
                break
        committed = ledger.record_count(snapshot_id)
        total = manifest["counts"]["records"]
        status = "paused_batch_cap" if paused and committed < total else "complete"
        if status == "complete":
            if committed != total:
                raise ConsumerError("committed records do not match manifest census")
            census = ledger.census(snapshot_id)
            if census["archive_state"].get("preserved", 0) != manifest["counts"]["archived"]:
                raise ConsumerError("ledger census disagrees with manifest archived count")
            with ledger.transaction():
                advance(ledger, job_id, len(files), 0, 0, status="complete")
        return {
            "status": status, "snapshot_id": snapshot_id, "manifest_sha256": manifest_sha,
            "contract_version": manifest["schema_version"],
            "contract_commit": contract.AGGREGATOR_CONTRACT_COMMIT, "fixture_mode": fixture,
            "records_total": total, "records_committed": committed, "records_remaining": total - committed,
            "batches_this_run": batches, "text_supplied": len(texts), "text_export": text_info,
            "producer_commit": contract.producer_commit(manifest, fixture),
            "analysis_performed": False, "research_eligible": False,
        }
    finally:
        release_lease(ledger, job_id, owner)


def _commit(ledger, job_id, snapshot_id, partition_id, index, batch, owner, now, hooks, batch_number):
    with ledger.transaction():
        check_lease(ledger, job_id, owner, now)
        for ordinal, row in batch:
            if not ledger.ingest_record(snapshot_id, row, partition_id, ordinal):
                raise ConsumerError(f"record {row['record_id']} already stored; cursor inconsistent")
        if "before_commit" in hooks:
            hooks["before_commit"](batch_number)
        advance(ledger, job_id, index, batch[-1][0] + 1, len(batch))
