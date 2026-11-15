"""FBA-03 lossless corpus-consumer and labeling checkpoints.

A consumer cursor is bound to one frozen snapshot **and** a compatibility hash
(manifest SHA-256 + analysis configuration). Cursor advancement must happen in
the same SQLite transaction as the work it acknowledges (record ingest or label
commit), so a crash can only lose uncommitted work, never acknowledge work that
was not stored. A changed snapshot or configuration can never reuse a cursor.

Leases give one worker exclusive ownership of a job; an expired lease can be
taken over. Legacy source-specific checkpoints (MuckRock page offsets) are
recorded as migration evidence only and are never converted into a resumable
cursor for a new research run.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from foia_bias.ledger import LedgerError, canonical


class CheckpointError(LedgerError):
    pass


class IncompatibleCheckpoint(CheckpointError):
    pass


class LeaseHeld(CheckpointError):
    pass


def compatibility_hash(snapshot_id: str, manifest_sha256: str, config: dict) -> str:
    return hashlib.sha256(canonical([snapshot_id, manifest_sha256, config]).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Cursor:
    job_id: str
    snapshot_id: str
    compatibility_sha256: str
    partition_index: int
    record_offset: int
    records_committed: int
    status: str


def _row(row):
    return Cursor(row["job_id"], row["snapshot_id"], row["compatibility_sha256"], row["partition_index"],
                  row["record_offset"], row["records_committed"], row["status"])


def open_cursor(ledger, job_id: str, snapshot_id: str, compatibility: str) -> Cursor:
    ledger.snapshot(snapshot_id)
    with ledger.transaction() as conn:
        row = conn.execute("SELECT * FROM consumer_cursors WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            conn.execute("INSERT INTO consumer_cursors VALUES (?,?,?,?,?,?,?,?)",
                         (job_id, snapshot_id, compatibility, 0, 0, 0, "running", ledger.clock()))
            row = conn.execute("SELECT * FROM consumer_cursors WHERE job_id=?", (job_id,)).fetchone()
        elif row["snapshot_id"] != snapshot_id or row["compatibility_sha256"] != compatibility:
            raise IncompatibleCheckpoint("checkpoint belongs to a different snapshot or configuration; "
                                         "start a new job instead of reusing this cursor")
        return _row(row)


def get_cursor(ledger, job_id):
    row = ledger.conn.execute("SELECT * FROM consumer_cursors WHERE job_id=?", (job_id,)).fetchone()
    return None if row is None else _row(row)


def advance(ledger, job_id: str, partition_index: int, record_offset: int, committed_delta: int,
            status: str = "running") -> None:
    """Move the cursor forward. Must run inside the transaction that stored the work."""
    if not ledger.conn.in_transaction:
        raise CheckpointError("advance() must share the transaction that commits the acknowledged work")
    row = ledger.conn.execute("SELECT * FROM consumer_cursors WHERE job_id=?", (job_id,)).fetchone()
    if row is None:
        raise CheckpointError("unknown job")
    if (partition_index, record_offset) < (row["partition_index"], row["record_offset"]):
        raise CheckpointError("cursor cannot move backwards")
    if committed_delta < 0:
        raise CheckpointError("committed count cannot decrease")
    ledger.conn.execute(
        "UPDATE consumer_cursors SET partition_index=?, record_offset=?, records_committed=records_committed+?, "
        "status=?, updated_at=? WHERE job_id=?",
        (partition_index, record_offset, committed_delta, status, ledger.clock(), job_id))


def verify_and_repair(ledger, job_id: str, partitions: list) -> Cursor:
    """Check a cursor against the manifest and the durable records it acknowledges.

    ``partitions`` is the manifest's ordered ``[(partition_id, count), ...]``.
    Corrupt cursors (beyond the manifest, or disagreeing with committed records)
    are rebuilt from the records actually stored, which are the source of truth.
    """
    cursor = get_cursor(ledger, job_id)
    if cursor is None:
        raise CheckpointError("unknown job")
    counts = [n for _, n in partitions]
    stored = ledger.conn.execute("SELECT COUNT(*) FROM records WHERE snapshot_id=?",
                                 (cursor.snapshot_id,)).fetchone()[0]
    if cursor.partition_index == len(counts):
        within, expected = cursor.record_offset == 0, sum(counts)
    elif 0 <= cursor.partition_index < len(counts):
        within = 0 <= cursor.record_offset <= counts[cursor.partition_index]
        expected = sum(counts[:cursor.partition_index]) + cursor.record_offset
    else:
        within, expected = False, -1
    if within and cursor.records_committed == expected == stored:
        return cursor
    have = {pid: n for pid, n in ledger.conn.execute(
        "SELECT partition_id, COUNT(*) FROM records WHERE snapshot_id=? GROUP BY partition_id",
        (cursor.snapshot_id,))}
    index, offset = len(counts), 0
    for i, (pid, expected_count) in enumerate(partitions):
        if have.get(pid, 0) < expected_count:
            index, offset = i, have.get(pid, 0)
            break
    with ledger.transaction() as conn:
        conn.execute("UPDATE consumer_cursors SET partition_index=?, record_offset=?, records_committed=?, "
                     "status='repaired', updated_at=? WHERE job_id=?",
                     (index, offset, stored, ledger.clock(), job_id))
    return get_cursor(ledger, job_id)


def _parse(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def acquire_lease(ledger, job_id: str, owner: str, now: str, ttl_seconds: int = 300) -> str:
    expires = (_parse(now) + timedelta(seconds=ttl_seconds)).astimezone(timezone.utc)
    expires_text = expires.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    with ledger.transaction() as conn:
        row = conn.execute("SELECT owner, expires_at FROM job_leases WHERE job_id=?", (job_id,)).fetchone()
        if row is not None and row["owner"] != owner and _parse(row["expires_at"]) > _parse(now):
            raise LeaseHeld(f"job {job_id} is leased by {row['owner']} until {row['expires_at']}")
        conn.execute("INSERT INTO job_leases VALUES (?,?,?) ON CONFLICT(job_id) DO UPDATE SET owner=excluded.owner, "
                     "expires_at=excluded.expires_at", (job_id, owner, expires_text))
    return expires_text


def check_lease(ledger, job_id: str, owner: str, now: str) -> None:
    row = ledger.conn.execute("SELECT owner, expires_at FROM job_leases WHERE job_id=?", (job_id,)).fetchone()
    if row is None or row["owner"] != owner or _parse(row["expires_at"]) <= _parse(now):
        raise LeaseHeld("lease lost; stop without committing")


def release_lease(ledger, job_id: str, owner: str) -> None:
    with ledger.transaction() as conn:
        conn.execute("DELETE FROM job_leases WHERE job_id=? AND owner=?", (job_id, owner))


def reconcile_legacy_checkpoint(ledger, path) -> dict:
    """Record a legacy (e.g. MuckRock page-offset) checkpoint as migration evidence.

    Returns a summary with ``resumable: False``: source-specific offsets must
    never seed a corpus-consumer cursor (plan FBA-03).
    """
    location = Path(path)
    raw = location.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    try:
        data = json.loads(raw.decode("utf-8"))
        parsed = isinstance(data, dict)
    except (UnicodeDecodeError, json.JSONDecodeError):
        data, parsed = None, False
    detail = {
        "parsed": parsed,
        "keys": sorted(data)[:50] if parsed else [],
        "resumable": False,
        "reason": "legacy source-specific checkpoint; corpus consumer resumes only from snapshot cursors",
    }
    with ledger.transaction() as conn:
        conn.execute("INSERT OR IGNORE INTO legacy_artifacts VALUES (?,?,?,?,?)",
                     (str(location), digest, "legacy_checkpoint", canonical(detail), ledger.clock()))
    return dict(detail, sha256=digest)
