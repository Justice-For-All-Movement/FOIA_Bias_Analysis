import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from foia_bias.checkpoints import (CheckpointError, IncompatibleCheckpoint, LeaseHeld, acquire_lease, advance,
                                   check_lease, compatibility_hash, get_cursor, open_cursor,
                                   reconcile_legacy_checkpoint, release_lease, verify_and_repair)
from foia_bias.ledger import SCHEMA_VERSION, AnalysisLedger, LedgerError
from tests.fixture_helpers import SNAP, load_export


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "state" / "analysis.sqlite"
        self.ledger = AnalysisLedger(self.path, clock=lambda: "2026-11-15T00:00:00Z")
        self.manifest, self.digest, info, self.rows = load_export()
        self.ledger.register_snapshot(info)
        self.partitions = [(f["path"], f["records"]) for f in self.manifest["files"]]
        self.compat = compatibility_hash(SNAP, self.digest, {"batch_size": 2})

    def tearDown(self):
        self.ledger.close()
        self.folder.cleanup()

    def ingest(self, rows, partition_index, offset):
        with self.ledger.transaction():
            for partition, ordinal, row in rows:
                self.ledger.ingest_record(SNAP, row, partition, ordinal)
            advance(self.ledger, "job-1", partition_index, offset, len(rows))

    def test_cursor_and_work_commit_atomically(self):
        open_cursor(self.ledger, "job-1", SNAP, self.compat)
        self.ingest(self.rows[:2], 0, 2)
        with self.assertRaises(RuntimeError):
            with self.ledger.transaction():
                for partition, ordinal, row in self.rows[2:4]:
                    self.ledger.ingest_record(SNAP, row, partition, ordinal)
                advance(self.ledger, "job-1", 0, 4, 2)
                raise RuntimeError("simulated crash before commit")
        cursor = get_cursor(self.ledger, "job-1")
        self.assertEqual((cursor.partition_index, cursor.record_offset, cursor.records_committed), (0, 2, 2))
        self.assertEqual(self.ledger.record_count(SNAP), 2)
        self.assertEqual(verify_and_repair(self.ledger, "job-1", self.partitions), cursor)

    def test_advance_requires_shared_transaction_and_monotonic(self):
        open_cursor(self.ledger, "job-1", SNAP, self.compat)
        with self.assertRaises(CheckpointError):
            advance(self.ledger, "job-1", 0, 1, 1)
        self.ingest(self.rows[:3], 0, 3)
        with self.assertRaises(CheckpointError):
            with self.ledger.transaction():
                advance(self.ledger, "job-1", 0, 1, 0)

    def test_changed_snapshot_or_config_cannot_reuse_cursor(self):
        open_cursor(self.ledger, "job-1", SNAP, self.compat)
        with self.assertRaises(IncompatibleCheckpoint):
            open_cursor(self.ledger, "job-1", SNAP, compatibility_hash(SNAP, self.digest, {"batch_size": 3}))
        with self.assertRaises(LedgerError):
            open_cursor(self.ledger, "job-2", "other-snapshot", self.compat)

    def test_corrupt_cursor_is_rebuilt_from_durable_records(self):
        open_cursor(self.ledger, "job-1", SNAP, self.compat)
        self.ingest(self.rows[:5], 1, 0)  # whole first partition
        self.ingest(self.rows[5:7], 1, 2)
        self.ledger.conn.execute("UPDATE consumer_cursors SET partition_index=9, record_offset=99")
        repaired = verify_and_repair(self.ledger, "job-1", self.partitions)
        self.assertEqual((repaired.partition_index, repaired.record_offset, repaired.records_committed,
                          repaired.status), (1, 2, 7, "repaired"))
        self.ledger.conn.execute("UPDATE consumer_cursors SET records_committed=1")
        self.assertEqual(verify_and_repair(self.ledger, "job-1", self.partitions).records_committed, 7)

    def test_leases_are_exclusive_until_expiry(self):
        acquire_lease(self.ledger, "job-1", "worker-a", "2026-11-15T00:00:00Z", 60)
        with self.assertRaises(LeaseHeld):
            acquire_lease(self.ledger, "job-1", "worker-b", "2026-11-15T00:00:30Z", 60)
        check_lease(self.ledger, "job-1", "worker-a", "2026-11-15T00:00:30Z")
        acquire_lease(self.ledger, "job-1", "worker-b", "2026-11-15T00:01:01Z", 60)
        with self.assertRaises(LeaseHeld):
            check_lease(self.ledger, "job-1", "worker-a", "2026-11-15T00:01:02Z")
        release_lease(self.ledger, "job-1", "worker-b")
        acquire_lease(self.ledger, "job-1", "worker-a", "2026-11-15T00:01:03Z", 60)

    def test_legacy_checkpoint_is_evidence_not_a_cursor(self):
        legacy = Path(self.folder.name) / "muckrock_checkpoint.json"
        legacy.write_text(json.dumps({"page": 17, "updated_after": "2024-01-01"}))
        summary = reconcile_legacy_checkpoint(self.ledger, legacy)
        self.assertFalse(summary["resumable"])
        self.assertEqual(summary["keys"], ["page", "updated_after"])
        corrupt = Path(self.folder.name) / "broken.json"
        corrupt.write_bytes(b"{\"page\": 3")
        self.assertFalse(reconcile_legacy_checkpoint(self.ledger, corrupt)["parsed"])
        self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM legacy_artifacts").fetchone()[0], 2)
        self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM consumer_cursors").fetchone()[0], 0)

    def test_version_one_ledger_upgrades_in_place(self):
        self.ledger.close()
        path = Path(self.folder.name) / "v1" / "analysis.sqlite"
        path.parent.mkdir(mode=0o700)
        from foia_bias import ledger as ledger_module
        with sqlite3.connect(path) as conn:
            for statement in ledger_module._SCHEMA_V1:
                conn.execute(statement)
            conn.execute("INSERT INTO schema_meta VALUES (1)")
            conn.execute("PRAGMA user_version=1")
        path.chmod(0o600)
        upgraded = AnalysisLedger(path)
        try:
            self.assertEqual(upgraded.conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(upgraded.conn.execute("SELECT version FROM schema_meta").fetchone()[0], SCHEMA_VERSION)
        finally:
            upgraded.close()
        self.ledger = AnalysisLedger(self.path)


if __name__ == "__main__":
    unittest.main()
