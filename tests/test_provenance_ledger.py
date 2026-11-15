import copy
import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from foia_bias import aggregator_contract as contract
from foia_bias.ledger import AnalysisLedger, LedgerError
from tests.fixture_helpers import EXPORT, PRODUCER, ROOT, SNAP, catalog_records, load_export, rid


def without_content(record):
    out = copy.deepcopy(record)
    out["text"]["content"] = None
    return out


class FC13ContractTests(unittest.TestCase):
    def test_vendored_schema_is_pinned_and_matches_upstream_commit(self):
        self.assertEqual(hashlib.sha256(contract.SCHEMA_PATH.read_bytes()).hexdigest(), contract.SCHEMA_SHA256)
        self.assertEqual(contract.load_schema()["title"], "FOIA Aggregator catalog record (foia-catalog-export/1)")
        self.assertRegex(contract.AGGREGATOR_CONTRACT_COMMIT, r"^[0-9a-f]{40}$")

    def test_producer_generated_snapshot_conforms(self):
        manifest = contract.validate_catalog_manifest(json.loads((PRODUCER / "manifest.json").read_text()))
        records = catalog_records(PRODUCER)
        self.assertEqual(len(records), manifest["counts"]["records"])
        for record in records:
            contract.validate_catalog_record(record)  # includes record_id == f(normalized url)
        mapped = [contract.to_analysis_record(r, fixture=True) for r in records]
        self.assertEqual([m["text"]["status"] for m in mapped], ["waiting", "waiting", "waiting"])
        self.assertEqual([m["archive_state"] for m in mapped], ["preserved", "remote_only", "remote_only"])
        self.assertEqual(mapped[2]["dates"]["release_date"], {"value": "2019", "precision": "year"})
        self.assertEqual(mapped[1]["dates"]["release_date"], {"value": None, "precision": "unknown"})

    def test_fixture_is_valid_and_reproducible(self):
        contract.validate_catalog_manifest(json.loads((EXPORT / "manifest.json").read_text()))
        with tempfile.TemporaryDirectory() as folder:
            subprocess.run([sys.executable, str(ROOT / "scripts/make_export_fixture.py"), folder], check=True)
            for path in EXPORT.iterdir():
                self.assertEqual((Path(folder) / path.name).read_bytes(), path.read_bytes(), path.name)

    def test_catalog_rejections(self):
        record = catalog_records()[0]
        mutations = [
            lambda r: r.update(extra_field=1),
            lambda r: r.update(record_id="rec_" + "0" * 24),
            lambda r: r["preservation"].update(sha256=None),
            lambda r: r["preservation"].update(status="deleted"),
            lambda r: r["text"].update(indexed=False),
            lambda r: r.update(published_year=1999),
            lambda r: r["sources"][0].update(provenance="scraped"),
            lambda r: r["sources"][0].update(source_id="bad"),
            lambda r: r.pop("title"),
        ]
        for mutate in mutations:
            candidate = copy.deepcopy(record)
            mutate(candidate)
            with self.subTest(mutation=mutate), self.assertRaises(contract.ContractError):
                contract.validate_catalog_record(candidate)
        manifest = json.loads((EXPORT / "manifest.json").read_text())
        for mutate in (lambda m: m.update(schema_version="foia-catalog-export/2"),
                       lambda m: m["counts"].update(archived=0),
                       lambda m: m["files"][0].update(records=99),
                       lambda m: m["files"][0].update(path="../x.jsonl"),
                       lambda m: m.update(excluded_fields=[]),
                       lambda m: m.update(snapshot_id="snapshot-1")):
            candidate = copy.deepcopy(manifest)
            mutate(candidate)
            with self.subTest(manifest=mutate), self.assertRaises(contract.ContractError):
                contract.validate_catalog_manifest(candidate)

    def test_adapter_states_and_text_length_check(self):
        records = {r["url"].rsplit("/", 1)[-1][:-4]: r for r in catalog_records()}
        self.assertEqual(contract.to_analysis_record(records["rr-004"], fixture=True)["text"]["status"], "failed")
        self.assertEqual(contract.to_analysis_record(records["rr-009"], fixture=True)["text"]["status"], "waiting")
        self.assertEqual(contract.to_analysis_record(records["mr-001"], fixture=True)["source_family"],
                         "third_party_release")
        self.assertEqual(contract.to_analysis_record(records["oc-001"], fixture=True)["source_family"],
                         "mixed_official_third_party")
        with self.assertRaisesRegex(contract.ContractError, "character_count"):
            contract.to_analysis_record(records["rr-001"], fixture=True, text="too short")


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.ledger = AnalysisLedger(Path(self.folder.name) / "state" / "analysis.sqlite",
                                     clock=lambda: "2026-11-15T00:00:00Z")
        self.manifest, self.digest, self.info, self.rows = load_export()
        self.ledger.register_snapshot(self.info)
        for partition, ordinal, row in self.rows:
            self.ledger.ingest_record(SNAP, row, partition, ordinal)

    def tearDown(self):
        self.ledger.close()
        self.folder.cleanup()

    def test_private_file(self):
        self.assertEqual(self.ledger.path.stat().st_mode & 0o777, 0o600)

    def test_round_trip_preserves_ids_hashes_nulls_and_provenance(self):
        for _, _, row in self.rows:
            with self.subTest(record=row["record_id"]):
                self.assertEqual(self.ledger.export_record(SNAP, row["record_id"]), without_content(row))
        self.assertEqual(self.ledger.snapshot(SNAP)["producer_commit"], "synthetic")

    def test_reingest_idempotent_and_conflicts_rejected(self):
        partition, ordinal, row = self.rows[0]
        self.assertFalse(self.ledger.ingest_record(SNAP, row, partition, ordinal))
        changed = copy.deepcopy(row)
        changed["agency"] = "other"
        with self.assertRaises(LedgerError):
            self.ledger.ingest_record(SNAP, changed, partition, ordinal)
        self.assertFalse(self.ledger.register_snapshot(self.info))
        with self.assertRaises(LedgerError):
            self.ledger.register_snapshot(dict(self.info, manifest_sha256="0" * 64))
        with self.assertRaises(LedgerError):
            self.ledger.register_snapshot({"snapshot_id": "x"})
        with self.assertRaises(sqlite3.DatabaseError):
            self.ledger.conn.execute("UPDATE records SET agency='x'")

    def test_census_and_duplicate_binaries_kept_distinct_in_ledger(self):
        census = self.ledger.census(SNAP)
        self.assertEqual(census["total"], 13)
        self.assertEqual(census["record_type"], {"release": 13})
        self.assertEqual(census["archive_state"]["remote_only"], 1)
        self.assertEqual(census["text_status"], {"extracted": 9, "failed": 1, "partial": 1, "waiting": 2})
        a = self.ledger.export_record(SNAP, rid("rr-001"))
        b = self.ledger.export_record(SNAP, rid("mr-001"))
        self.assertEqual(a["original_sha256"], b["original_sha256"])
        self.assertNotEqual(a["provenance"], b["provenance"])

    def test_revised_text_yields_auditable_label_invalidation(self):
        target = rid("rr-002")
        record = next(r for _, _, r in self.rows if r["record_id"] == target)
        old_sha = record["text"]["text_sha256"]
        self.ledger.put_label(target, old_sha, "fake:v1", "labeled", {"wrongdoing": 0.1})
        self.assertFalse(self.ledger.put_label(target, old_sha, "fake:v1", "labeled", {"wrongdoing": 0.9}))
        info = dict(self.info, snapshot_id="snap_" + "1" * 20, manifest_sha256="1" * 64)
        self.ledger.register_snapshot(info)
        for partition, ordinal, row in self.rows:
            row = copy.deepcopy(row)
            if row["record_id"] == target:
                row["text"]["content"] += " revised"
                row["text"]["text_sha256"] = hashlib.sha256(row["text"]["content"].encode()).hexdigest()
                row["text"]["text_revision"] = 2
            self.ledger.ingest_record(info["snapshot_id"], row, partition, ordinal)
        created = self.ledger.invalidate_for_snapshot(info["snapshot_id"])
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0][0:2], (target, old_sha))
        self.assertEqual(created[0][3], "text_revised")
        self.assertEqual(self.ledger.invalidate_for_snapshot(info["snapshot_id"]), [])
        self.assertIsNotNone(self.ledger.get_label(target, old_sha, "fake:v1"))

    def test_run_provenance(self):
        provenance = {"cohort_version": "c1", "config_sha256": "a" * 64, "code_version": "test",
                      "roster_sha256": "b" * 64, "labeler_key": "fake:v1"}
        run = self.ledger.start_run(SNAP, provenance)
        self.assertEqual(run, self.ledger.start_run(SNAP, provenance))
        with self.assertRaises(LedgerError):
            self.ledger.start_run(SNAP, {"cohort_version": "c1"})


if __name__ == "__main__":
    unittest.main()
