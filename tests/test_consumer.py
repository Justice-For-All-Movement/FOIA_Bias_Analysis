import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from foia_bias import aggregator_contract as contract
from foia_bias.aggregator_api import ApiError, SnapshotChanged, iter_api_records
from foia_bias.checkpoints import IncompatibleCheckpoint, get_cursor
from foia_bias.consumer import ConsumerError, consume
from foia_bias.ledger import AnalysisLedger, LedgerError
from tests.fixture_helpers import EXPORT, PRODUCER, ROOT, SNAP, catalog_records


class ConsumerTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.tmp = Path(self.folder.name)
        self.ledger_path = self.tmp / "state" / "analysis.sqlite"
        self.ledger = AnalysisLedger(self.ledger_path, clock=lambda: "2026-11-15T00:00:00Z")

    def tearDown(self):
        self.ledger.close()
        self.folder.cleanup()

    def copy_export(self, source=EXPORT):
        target = self.tmp / "export"
        shutil.copytree(source, target)
        return target

    def rewrite(self, export, name, data):
        (export / name).write_bytes(data)
        manifest = json.loads((export / "manifest.json").read_text())
        for entry in manifest["files"]:
            if entry["path"] == name:
                entry["sha256"] = hashlib.sha256(data).hexdigest()
        manifest["snapshot_id"] = "snap_" + "2" * 20
        (export / "manifest.json").write_text(json.dumps(manifest))

    def test_full_census_and_pinned_contract(self):
        summary = consume(self.ledger, EXPORT, batch_size=2)
        self.assertEqual(summary["status"], "complete")
        self.assertEqual((summary["records_total"], summary["records_committed"], summary["text_supplied"]),
                         (13, 13, 10))
        self.assertEqual(summary["contract_commit"], contract.AGGREGATOR_CONTRACT_COMMIT)
        self.assertFalse(summary["research_eligible"])
        families = self.ledger.census(SNAP)["source_family"]
        self.assertEqual(set(families), {"agency_reading_room", "third_party_release", "mixed_official_third_party"})
        again = consume(self.ledger, EXPORT, batch_size=7)
        self.assertEqual((again["status"], again["batches_this_run"]), ("complete", 0))

    def test_producer_generated_snapshot_consumes(self):
        summary = consume(self.ledger, PRODUCER, batch_size=2)
        self.assertEqual((summary["status"], summary["records_committed"]), ("complete", 3))
        self.assertEqual(summary["snapshot_id"], "snap_936bc978fe5fc655b664")

    def test_batch_cap_pauses_without_shrinking_census_and_resumes(self):
        statuses = []
        for _ in range(20):
            summary = consume(self.ledger, EXPORT, batch_size=2, max_batches=1)
            statuses.append(summary["status"])
            self.assertEqual(summary["records_total"], 13)
            if summary["status"] == "complete":
                break
        self.assertEqual((statuses[0], statuses[-1]), ("paused_batch_cap", "complete"))
        self.assertEqual(self.ledger.record_count(SNAP), 13)

    def test_crash_mid_batch_then_resume_matches_snapshot_exactly(self):
        code = ("import os, sys\n"
                "from foia_bias.ledger import AnalysisLedger\n"
                "from foia_bias.consumer import consume\n"
                "ledger = AnalysisLedger(sys.argv[1])\n"
                "def die(n):\n"
                "    if n == 3:\n"
                "        os._exit(9)\n"
                "consume(ledger, sys.argv[2], batch_size=2, hooks={'before_commit': die})\n")
        result = subprocess.run([sys.executable, "-c", code, str(self.ledger_path), str(EXPORT)], cwd=ROOT,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 9, result.stderr)
        self.ledger.close()
        self.ledger = AnalysisLedger(self.ledger_path)
        self.assertEqual(self.ledger.record_count(SNAP), 5)
        self.assertEqual(get_cursor(self.ledger, "consume").records_committed, 5)
        summary = consume(self.ledger, EXPORT, batch_size=2, now="2026-11-15T01:00:00Z")
        self.assertEqual(summary["status"], "complete")
        self.assertEqual(sorted(r["record_id"] for r in self.ledger.records(SNAP)),
                         sorted(r["record_id"] for r in catalog_records()))

    def test_tampered_truncated_duplicate_and_miscounted_snapshots_fail_before_ingest(self):
        export = self.copy_export()
        part = export / "records-00002.jsonl"
        original = part.read_bytes()
        part.write_bytes(b"".join(original.splitlines(keepends=True)[:4]))
        with self.assertRaisesRegex(ConsumerError, "SHA-256|count"):
            consume(self.ledger, export, batch_size=2)
        first = (export / "records-00001.jsonl").read_bytes().splitlines(keepends=True)[0]
        self.rewrite(export, "records-00002.jsonl", b"".join(original.splitlines(keepends=True)[:4]) + first)
        with self.assertRaisesRegex(ConsumerError, "duplicate record_id"):
            consume(self.ledger, export, batch_size=2, job_id="dup")
        self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM records").fetchone()[0], 0)
        self.rewrite(export, "records-00002.jsonl", original)
        manifest = json.loads((export / "manifest.json").read_text())
        manifest["counts"]["text_indexed"] -= 1
        (export / "manifest.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ConsumerError, "text_indexed"):
            consume(self.ledger, export, batch_size=2, job_id="counts")

    def test_changed_snapshot_token_or_config_fails(self):
        consume(self.ledger, EXPORT, batch_size=5, max_batches=1)
        export = self.copy_export()
        manifest = json.loads((export / "manifest.json").read_text())
        manifest["generated_at"] = "2026-11-16T00:00:00+00:00"  # same snapshot_id, different manifest
        (export / "manifest.json").write_text(json.dumps(manifest))
        with self.assertRaises(LedgerError):
            consume(self.ledger, export, batch_size=5)
        with self.assertRaises(IncompatibleCheckpoint):
            consume(self.ledger, EXPORT, batch_size=5, analysis_config={"cohort": "different"})

    def test_fixture_and_live_boundaries(self):
        with self.assertRaises(contract.ContractError):
            consume(self.ledger, EXPORT, expected_fixture=False)
        export = self.copy_export()
        (export / contract.FIXTURE_MARKER).unlink()
        with self.assertRaisesRegex(ConsumerError, "fixture-only"):
            consume(self.ledger, export, expected_fixture=False)
        with self.assertRaises(contract.ContractError):
            consume(self.ledger, export, expected_fixture=True)

    def test_cli_consume(self):
        result = subprocess.run([sys.executable, "main.py", "consume", "--export", str(EXPORT),
                                 "--ledger", str(self.tmp / "cli" / "analysis.sqlite"), "--batch-size", "4"],
                                cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["records_committed"], 13)
        bad = subprocess.run([sys.executable, "main.py", "consume", "--export", str(EXPORT), "--live",
                              "--ledger", str(self.tmp / "cli2" / "analysis.sqlite")],
                             cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual((bad.returncode, bad.stdout), (2, ""))


class ApiReaderTests(unittest.TestCase):
    def fetcher(self, pages, manifest=None, script=None):
        manifest = manifest or {"schema_version": "foia-catalog-export/1", "snapshot_id": "snap_" + "a" * 20,
                                "counts": {"records": sum(len(p["records"]) for p in pages)}}
        script = list(script or [])
        calls = []

        def fetch(path):
            calls.append(path)
            if path == "/api/v1/export/manifest":
                return 200, {}, json.dumps(manifest).encode()
            if script:
                status = script.pop(0)
                if status != 200:
                    return status, {"Retry-After": "7"}, b"{}"
            index = int(path.split("cursor=c")[1]) if "cursor=" in path else 0
            return 200, {}, json.dumps(pages[index]).encode()
        return fetch, calls

    def pages(self, snapshot="snap_" + "a" * 20):
        records = catalog_records()
        return [{"schema_version": "foia-catalog-export/1", "snapshot_id": snapshot, "records": records[:7],
                 "next_cursor": "c1"},
                {"schema_version": "foia-catalog-export/1", "snapshot_id": snapshot, "records": records[7:],
                 "next_cursor": None}]

    def test_pages_validated_and_counted(self):
        fetch, calls = self.fetcher(self.pages())
        got = list(iter_api_records(fetch, limit=7))
        self.assertEqual(len(got), 13)
        self.assertEqual(calls[1:], ["/api/v1/records?limit=7", "/api/v1/records?limit=7&cursor=c1"])

    def test_rate_limit_conflict_and_mixed_snapshots(self):
        waits = []
        fetch, _ = self.fetcher(self.pages(), script=[429, 200])
        self.assertEqual(len(list(iter_api_records(fetch, sleep=waits.append))), 13)
        self.assertEqual(waits, [7])
        fetch, _ = self.fetcher(self.pages(), script=[429])
        with self.assertRaises(ApiError):
            list(iter_api_records(fetch))
        fetch, _ = self.fetcher(self.pages(), script=[200, 409])
        with self.assertRaises(SnapshotChanged):
            list(iter_api_records(fetch))
        pages = self.pages()
        pages[1]["snapshot_id"] = "snap_" + "b" * 20
        fetch, _ = self.fetcher(pages)
        with self.assertRaises(SnapshotChanged):
            list(iter_api_records(fetch))
        pages = self.pages()
        pages[1]["records"] = pages[0]["records"][:1]
        fetch, _ = self.fetcher(pages, manifest={"schema_version": "foia-catalog-export/1",
                                                  "snapshot_id": "snap_" + "a" * 20, "counts": {"records": 8}})
        with self.assertRaisesRegex(ApiError, "duplicate"):
            list(iter_api_records(fetch))


if __name__ == "__main__":
    unittest.main()
