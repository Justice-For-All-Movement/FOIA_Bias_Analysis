import copy
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from foia_bias import aggregator_contract as contract
from foia_bias.ledger import AnalysisLedger, LedgerError

ROOT = Path(__file__).resolve().parents[1]
EXPORT = ROOT / "fixtures" / "export_v1"


def load_export(directory=EXPORT):
    raw = (directory / "manifest.json").read_bytes()
    manifest = json.loads(raw)
    rows = []
    for part in manifest["partitions"]:
        for ordinal, line in enumerate((directory / part["path"]).read_text().splitlines()):
            rows.append((part["partition_id"], ordinal, json.loads(line)))
    return manifest, hashlib.sha256(raw).hexdigest(), rows


def without_content(record):
    out = copy.deepcopy(record)
    out["text"]["content"] = None
    return out


class ContractTests(unittest.TestCase):
    def test_fixture_is_valid_and_reproducible(self):
        manifest, _, rows = load_export()
        contract.validate_manifest(manifest, expected_fixture=True)
        for _, _, row in rows:
            contract.validate_record(row, True)
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as folder:
            subprocess.run([sys.executable, str(ROOT / "scripts/make_export_fixture.py"), folder], check=True)
            for name in ("manifest.json", "part-0001.jsonl", "part-0002.jsonl", "part-0003.jsonl"):
                self.assertEqual((Path(folder) / name).read_bytes(), (EXPORT / name).read_bytes(), name)

    def test_contract_rejections(self):
        manifest, _, rows = load_export()
        live = copy.deepcopy(manifest)
        live["fixture_mode"] = False
        with self.assertRaises(contract.ContractError):
            contract.validate_manifest(live)  # live export must pin producer commit
        bad_totals = copy.deepcopy(manifest)
        bad_totals["totals"]["records"] += 1
        with self.assertRaises(contract.ContractError):
            contract.validate_manifest(bad_totals)
        unknown_version = dict(manifest, contract_version="fc13-v99")
        with self.assertRaises(contract.ContractError):
            contract.validate_manifest(unknown_version)
        record = rows[0][2]
        mutations = [
            lambda r: r.update(synthetic=False),
            lambda r: r.update(record_type="tweet"),
            lambda r: r["text"].update(text_sha256=None),
            lambda r: r.update(archive_state="remote_only"),
            lambda r: r["dates"].update(decision_date={"value": None, "precision": "day"}),
            lambda r: r.update(provenance=[]),
            lambda r: r.update(extra_field=1),
        ]
        for mutate in mutations:
            candidate = copy.deepcopy(record)
            mutate(candidate)
            with self.subTest(mutation=mutate), self.assertRaises(contract.ContractError):
                contract.validate_record(candidate, True)
        log_row = next(r for _, _, r in rows if r["record_type"] == "request_log_row")
        candidate = copy.deepcopy(log_row)
        candidate["text"] = copy.deepcopy(record["text"])
        with self.assertRaises(contract.ContractError):
            contract.validate_record(candidate, True)  # logs never carry document text


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.ledger = AnalysisLedger(Path(self.folder.name) / "state" / "analysis.sqlite",
                                     clock=lambda: "2026-11-15T00:00:00Z")
        self.manifest, self.digest, self.rows = load_export()
        self.ledger.register_snapshot(self.manifest, self.digest)
        for partition, ordinal, row in self.rows:
            self.ledger.ingest_record("synthetic-export-v1", row, partition, ordinal)

    def tearDown(self):
        self.ledger.close()
        self.folder.cleanup()

    def test_private_file(self):
        self.assertEqual(self.ledger.path.stat().st_mode & 0o777, 0o600)

    def test_round_trip_preserves_ids_hashes_nulls_and_provenance(self):
        for _, _, row in self.rows:
            with self.subTest(record=row["record_id"]):
                self.assertEqual(self.ledger.export_record("synthetic-export-v1", row["record_id"]),
                                 without_content(row))

    def test_reingest_idempotent_and_conflicts_rejected(self):
        partition, ordinal, row = self.rows[0]
        self.assertFalse(self.ledger.ingest_record("synthetic-export-v1", row, partition, ordinal))
        changed = copy.deepcopy(row)
        changed["agency"] = "Other"
        with self.assertRaises(LedgerError):
            self.ledger.ingest_record("synthetic-export-v1", changed, partition, ordinal)
        self.assertFalse(self.ledger.register_snapshot(self.manifest, self.digest))
        with self.assertRaises(LedgerError):
            self.ledger.register_snapshot(self.manifest, "0" * 64)
        with self.assertRaises(sqlite3.DatabaseError):
            self.ledger.conn.execute("UPDATE records SET agency='x'")

    def test_duplicate_associations_and_record_types_stay_distinct(self):
        rels = self.ledger.relations("synthetic-export-v1", "agg:rr-001")
        self.assertEqual(rels, [("agg:mr-001", "duplicate_of", "agg:rr-001")])
        self.assertIn(("agg:mr-002", "redaction_variant_of", "agg:rr-002"),
                      self.ledger.relations("synthetic-export-v1"))
        census = self.ledger.census("synthetic-export-v1")
        self.assertEqual(census["total"], 15)
        self.assertEqual(census["record_type"], {"annual_report": 1, "release": 12,
                                                 "request_description": 1, "request_log_row": 1})
        self.assertEqual(census["text_status"]["not_applicable"], 3)
        self.assertEqual(census["archive_state"]["remote_only"], 1)
        self.assertEqual(census["jurisdiction"]["us-state-ca"], 1)

    def test_revised_text_yields_auditable_label_invalidation(self):
        record = next(r for _, _, r in self.rows if r["record_id"] == "agg:rr-002")
        old_sha = record["text"]["text_sha256"]
        self.ledger.put_label("agg:rr-002", old_sha, "fake:v1", "labeled", {"wrongdoing": 0.1})
        self.assertFalse(self.ledger.put_label("agg:rr-002", old_sha, "fake:v1", "labeled", {"wrongdoing": 0.9}))
        self.assertEqual(self.ledger.get_label("agg:rr-002", old_sha, "fake:v1")["result"], {"wrongdoing": 0.1})
        revised_manifest = copy.deepcopy(self.manifest)
        revised_manifest["snapshot_id"] = "synthetic-export-v2"
        self.ledger.register_snapshot(revised_manifest, "1" * 64)
        for partition, ordinal, row in self.rows:
            row = copy.deepcopy(row)
            if row["record_id"] == "agg:rr-002":
                row["revision"] = 2
                row["text"]["content"] += " revised"
                row["text"]["text_sha256"] = hashlib.sha256(row["text"]["content"].encode()).hexdigest()
                row["text"]["text_revision"] = 2
            self.ledger.ingest_record("synthetic-export-v2", row, partition, ordinal)
        created = self.ledger.invalidate_for_snapshot("synthetic-export-v2")
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0][0:2], ("agg:rr-002", old_sha))
        self.assertEqual(created[0][3], "text_revised")
        self.assertEqual(self.ledger.invalidate_for_snapshot("synthetic-export-v2"), [])
        self.assertEqual(len(self.ledger.invalidations()), 1)
        # The old label is retained for audit, not deleted.
        self.assertIsNotNone(self.ledger.get_label("agg:rr-002", old_sha, "fake:v1"))

    def test_run_provenance(self):
        provenance = {"cohort_version": "c1", "config_sha256": "a" * 64, "code_version": "test",
                      "roster_sha256": "b" * 64, "labeler_key": "fake:v1"}
        run = self.ledger.start_run("synthetic-export-v1", provenance)
        self.assertEqual(run, self.ledger.start_run("synthetic-export-v1", provenance))
        with self.assertRaises(LedgerError):
            self.ledger.start_run("synthetic-export-v1", {"cohort_version": "c1"})


if __name__ == "__main__":
    unittest.main()
