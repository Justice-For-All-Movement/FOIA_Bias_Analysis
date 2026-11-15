"""FBA-02 Aggregator record identity and analytical provenance ledger.

SQLite, standard library only. Keys are Aggregator identities, never Bias
inventions: ``(snapshot_id, record_id)`` for catalog rows and
``(record_id, text_sha256, labeler_key)`` for labels, so revised text or a
changed model/prompt can never silently reuse an old label. Snapshots are
immutable: re-registering an ID with different content is an error.

The ledger stores identities, hashes, typed dates, provenance and relations;
full text stays in the Aggregator export (bounded caches only).
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from foia_bias import aggregator_contract as contract

SCHEMA_VERSION = 1
_SCHEMA = (
    "CREATE TABLE schema_meta (version INTEGER NOT NULL)",
    """CREATE TABLE snapshots (
        snapshot_id TEXT PRIMARY KEY, contract_version TEXT NOT NULL, producer_name TEXT NOT NULL,
        producer_commit TEXT NOT NULL, manifest_sha256 TEXT NOT NULL, observed_at TEXT NOT NULL,
        fixture_mode INTEGER NOT NULL, total_records INTEGER NOT NULL, registered_at TEXT NOT NULL)""",
    """CREATE TABLE records (
        snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id), record_id TEXT NOT NULL,
        revision INTEGER NOT NULL, record_type TEXT NOT NULL, source_family TEXT NOT NULL,
        jurisdiction TEXT NOT NULL, agency TEXT NOT NULL, component TEXT, original_sha256 TEXT,
        archive_state TEXT NOT NULL, text_status TEXT NOT NULL, text_sha256 TEXT, text_revision INTEGER,
        extractor TEXT, char_count INTEGER, dates_json TEXT NOT NULL, provenance_json TEXT NOT NULL,
        synthetic INTEGER NOT NULL, partition_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
        row_sha256 TEXT NOT NULL,
        PRIMARY KEY (snapshot_id, record_id))""",
    """CREATE TABLE relations (
        snapshot_id TEXT NOT NULL, record_id TEXT NOT NULL, kind TEXT NOT NULL, other_record_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL,
        PRIMARY KEY (snapshot_id, record_id, kind, other_record_id))""",
    """CREATE TABLE runs (
        run_id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
        provenance_json TEXT NOT NULL, created_at TEXT NOT NULL)""",
    """CREATE TABLE labels (
        record_id TEXT NOT NULL, text_sha256 TEXT NOT NULL, labeler_key TEXT NOT NULL,
        status TEXT NOT NULL, result_json TEXT, run_id TEXT, created_at TEXT NOT NULL,
        PRIMARY KEY (record_id, text_sha256, labeler_key))""",
    """CREATE TABLE label_invalidations (
        record_id TEXT NOT NULL, old_text_sha256 TEXT NOT NULL, new_text_sha256 TEXT,
        labeler_key TEXT NOT NULL, snapshot_id TEXT NOT NULL, reason TEXT NOT NULL, at TEXT NOT NULL,
        PRIMARY KEY (record_id, old_text_sha256, labeler_key, snapshot_id))""",
    "CREATE TRIGGER records_immutable BEFORE UPDATE ON records BEGIN SELECT RAISE(ABORT, 'snapshot records are immutable'); END",
    "CREATE TRIGGER invalidations_append_only BEFORE DELETE ON label_invalidations BEGIN SELECT RAISE(ABORT, 'append-only'); END",
)


class LedgerError(RuntimeError):
    pass


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _private_file(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise LedgerError("ledger path must not be a symlink")
    if not path.exists():
        os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600))
    if path.stat().st_mode & 0o077:
        raise LedgerError("ledger file must be private (0600)")
    return path


def record_row_digest(record: dict) -> str:
    """Hash of everything Bias keeps for a record (text content excluded)."""
    kept = dict(record)
    kept["text"] = {k: v for k, v in record["text"].items() if k != "content"}
    return hashlib.sha256(canonical(kept).encode("utf-8")).hexdigest()


class AnalysisLedger:
    def __init__(self, path, clock=utc_now):
        self.path = _private_file(Path(path))
        self.clock = clock
        self.conn = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=30000")
        with self.transaction() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                for statement in _SCHEMA:
                    conn.execute(statement)
                conn.execute("INSERT INTO schema_meta VALUES (?)", (SCHEMA_VERSION,))
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            elif version != SCHEMA_VERSION:
                raise LedgerError("unsupported analysis ledger schema")

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @contextmanager
    def transaction(self):
        if self.conn.in_transaction:
            yield self.conn
            return
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    # -- snapshots and records ---------------------------------------------
    def register_snapshot(self, manifest: dict, manifest_sha256: str):
        contract.validate_manifest(manifest)
        with self.transaction() as conn:
            row = conn.execute("SELECT manifest_sha256 FROM snapshots WHERE snapshot_id=?",
                               (manifest["snapshot_id"],)).fetchone()
            if row is not None:
                if row[0] != manifest_sha256:
                    raise LedgerError("snapshot_id reused with different manifest content")
                return False
            conn.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?)", (
                manifest["snapshot_id"], manifest["contract_version"], manifest["producer"]["name"],
                manifest["producer"]["commit"], manifest_sha256, manifest["observed_at"],
                int(manifest["fixture_mode"]), manifest["totals"]["records"], self.clock()))
            return True

    def snapshot(self, snapshot_id):
        row = self.conn.execute("SELECT * FROM snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()
        if row is None:
            raise LedgerError("unknown snapshot")
        return dict(row)

    def ingest_record(self, snapshot_id: str, record: dict, partition_id: str, ordinal: int):
        """Store one validated record. Identical re-ingest is a no-op."""
        snap = self.snapshot(snapshot_id)
        contract.validate_record(record, bool(snap["fixture_mode"]))
        digest = record_row_digest(record)
        text = record["text"]
        with self.transaction() as conn:
            existing = conn.execute("SELECT row_sha256 FROM records WHERE snapshot_id=? AND record_id=?",
                                    (snapshot_id, record["record_id"])).fetchone()
            if existing is not None:
                if existing[0] != digest:
                    raise LedgerError(f"duplicate record_id {record['record_id']} with different content")
                return False
            conn.execute("INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                snapshot_id, record["record_id"], record["revision"], record["record_type"],
                record["source_family"], record["jurisdiction"], record["agency"], record["component"],
                record["original_sha256"], record["archive_state"], text["status"], text["text_sha256"],
                text["text_revision"], text["extractor"], text["char_count"], canonical(record["dates"]),
                canonical(record["provenance"]), int(record["synthetic"]), partition_id, ordinal, digest))
            for index, rel in enumerate(record["relations"]):
                conn.execute("INSERT INTO relations VALUES (?,?,?,?,?)",
                             (snapshot_id, record["record_id"], rel["kind"], rel["record_id"], index))
            return True

    def export_record(self, snapshot_id: str, record_id: str) -> dict:
        """Reconstruct the contract record (text content omitted: it stays upstream)."""
        row = self.conn.execute("SELECT * FROM records WHERE snapshot_id=? AND record_id=?",
                                (snapshot_id, record_id)).fetchone()
        if row is None:
            raise LedgerError("unknown record")
        relations = [{"kind": r["kind"], "record_id": r["other_record_id"]} for r in self.conn.execute(
            "SELECT kind, other_record_id FROM relations WHERE snapshot_id=? AND record_id=? ORDER BY ordinal",
            (snapshot_id, record_id))]
        return {
            "record_id": row["record_id"], "revision": row["revision"], "record_type": row["record_type"],
            "source_family": row["source_family"], "jurisdiction": row["jurisdiction"], "agency": row["agency"],
            "component": row["component"], "provenance": json.loads(row["provenance_json"]),
            "original_sha256": row["original_sha256"], "dates": json.loads(row["dates_json"]),
            "archive_state": row["archive_state"],
            "text": {"status": row["text_status"], "text_sha256": row["text_sha256"],
                     "text_revision": row["text_revision"], "extractor": row["extractor"],
                     "char_count": row["char_count"], "content": None},
            "relations": relations, "synthetic": bool(row["synthetic"]),
        }

    def records(self, snapshot_id):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM records WHERE snapshot_id=? ORDER BY partition_id, ordinal", (snapshot_id,))]

    def record_count(self, snapshot_id):
        return self.conn.execute("SELECT COUNT(*) FROM records WHERE snapshot_id=?", (snapshot_id,)).fetchone()[0]

    def relations(self, snapshot_id, record_id=None):
        query = "SELECT record_id, kind, other_record_id FROM relations WHERE snapshot_id=?"
        params = [snapshot_id]
        if record_id is not None:
            query += " AND (record_id=? OR other_record_id=?)"
            params += [record_id, record_id]
        return [tuple(r) for r in self.conn.execute(query + " ORDER BY record_id, ordinal", params)]

    def census(self, snapshot_id):
        out = {}
        for column in ("record_type", "source_family", "text_status", "archive_state", "jurisdiction"):
            out[column] = {r[0]: r[1] for r in self.conn.execute(
                f"SELECT {column}, COUNT(*) FROM records WHERE snapshot_id=? GROUP BY {column} ORDER BY {column}",
                (snapshot_id,))}
        out["total"] = self.record_count(snapshot_id)
        return out

    # -- runs and labels ------------------------------------------------------
    def start_run(self, snapshot_id, provenance: dict):
        """Record analytical run provenance: cohort/config/code/roster/model/prompt versions."""
        self.snapshot(snapshot_id)
        required = {"cohort_version", "config_sha256", "code_version", "roster_sha256", "labeler_key"}
        if not isinstance(provenance, dict) or not required <= set(provenance):
            raise LedgerError("run provenance requires " + ", ".join(sorted(required)))
        run_id = hashlib.sha256(canonical([snapshot_id, provenance]).encode()).hexdigest()[:32]
        with self.transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO runs VALUES (?,?,?,?)",
                         (run_id, snapshot_id, canonical(provenance), self.clock()))
        return run_id

    def put_label(self, record_id, text_sha256, labeler_key, status, result=None, run_id=None):
        with self.transaction() as conn:
            existing = conn.execute("SELECT status, result_json FROM labels WHERE record_id=? AND text_sha256=? "
                                    "AND labeler_key=?", (record_id, text_sha256, labeler_key)).fetchone()
            payload = None if result is None else canonical(result)
            if existing is not None:
                if existing["status"] == "labeled":
                    return False  # A committed label is never overwritten.
                conn.execute("UPDATE labels SET status=?, result_json=?, run_id=?, created_at=? WHERE record_id=? "
                             "AND text_sha256=? AND labeler_key=?",
                             (status, payload, run_id, self.clock(), record_id, text_sha256, labeler_key))
                return True
            conn.execute("INSERT INTO labels VALUES (?,?,?,?,?,?,?)",
                         (record_id, text_sha256, labeler_key, status, payload, run_id, self.clock()))
            return True

    def get_label(self, record_id, text_sha256, labeler_key):
        row = self.conn.execute("SELECT * FROM labels WHERE record_id=? AND text_sha256=? AND labeler_key=?",
                                (record_id, text_sha256, labeler_key)).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["result"] = None if row["result_json"] is None else json.loads(row["result_json"])
        return out

    def invalidate_for_snapshot(self, snapshot_id):
        """Record auditable invalidations where a record's text changed (or vanished)."""
        now = self.clock()
        created = []
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT l.record_id, l.text_sha256, l.labeler_key, r.text_sha256 AS new_sha "
                "FROM labels l JOIN records r ON r.record_id=l.record_id AND r.snapshot_id=? "
                "WHERE r.text_sha256 IS NULL OR r.text_sha256 <> l.text_sha256 "
                "ORDER BY l.record_id, l.text_sha256, l.labeler_key", (snapshot_id,)).fetchall()
            for row in rows:
                reason = "text_revised" if row["new_sha"] else "text_unavailable"
                inserted = conn.execute("INSERT OR IGNORE INTO label_invalidations VALUES (?,?,?,?,?,?,?)", (
                    row["record_id"], row["text_sha256"], row["new_sha"], row["labeler_key"], snapshot_id,
                    reason, now)).rowcount
                if inserted:
                    created.append((row["record_id"], row["text_sha256"], row["new_sha"], reason))
        return created

    def invalidations(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM label_invalidations ORDER BY record_id, old_text_sha256")]
