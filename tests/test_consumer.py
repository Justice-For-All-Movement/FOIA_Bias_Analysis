import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from foia_bias.checkpoints import IncompatibleCheckpoint, get_cursor
from foia_bias.consumer import ConsumerError, consume
from foia_bias.ledger import AnalysisLedger, LedgerError

ROOT = Path(__file__).resolve().parents[1]
EXPORT = ROOT / "fixtures" / "export_v1"


class ConsumerTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.tmp = Path(self.folder.name)
        self.ledger_path = self.tmp / "state" / "analysis.sqlite"
        self.ledger = AnalysisLedger(self.ledger_path, clock=lambda: "2026-11-15T00:00:00Z")

    def tearDown(self):
        self.ledger.close()
        self.folder.cleanup()

    def copy_export(self):
        target = self.tmp / "export"
        shutil.copytree(EXPORT, target)
        return target

    def test_full_multi_source_multi_partition_census(self):
        summary = consume(self.ledger, EXPORT, batch_size=2)
        self.assertEqual(summary["status"], "complete")
        self.assertEqual((summary["records_total"], summary["records_committed"]), (15, 15))
        self.assertFalse(summary["research_eligible"])
        families = self.ledger.census("synthetic-export-v1")["source_family"]
        self.assertEqual(set(families), {"agency_reading_room", "muckrock", "other_release_collection",
                                         "agency_log", "foia_gov_annual"})
        again = consume(self.ledger, EXPORT, batch_size=7)
        self.assertEqual((again["status"], again["batches_this_run"]), ("complete", 0))

    def test_batch_cap_pauses_without_shrinking_census_and_resumes(self):
        statuses = []
        for _ in range(20):
            summary = consume(self.ledger, EXPORT, batch_size=2, max_batches=1)
            statuses.append(summary["status"])
            self.assertEqual(summary["records_total"], 15)
            if summary["status"] == "complete":
                break
        self.assertEqual(statuses[0], "paused_batch_cap")
        self.assertEqual(statuses[-1], "complete")
        self.assertEqual(self.ledger.record_count("synthetic-export-v1"), 15)

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
        self.assertEqual(self.ledger.record_count("synthetic-export-v1"), 5)
        cursor = get_cursor(self.ledger, "consume")
        self.assertEqual(cursor.records_committed, 5)
        summary = consume(self.ledger, EXPORT, batch_size=2, now="2026-11-15T01:00:00Z")
        self.assertEqual(summary["status"], "complete")
        ids = sorted(r["record_id"] for r in self.ledger.records("synthetic-export-v1"))
        expected = sorted(json.loads(l)["record_id"] for p in sorted(EXPORT.glob("part-*.jsonl"))
                          for l in p.read_text().splitlines())
        self.assertEqual(ids, expected)

    def test_tampered_truncated_and_duplicate_partitions_fail_visibly(self):
        export = self.copy_export()
        part = export / "part-0002.jsonl"
        original = part.read_bytes()
        part.write_bytes(original.splitlines(keepends=True)[0] * 1 + b"".join(original.splitlines(keepends=True)[1:4]))
        with self.assertRaisesRegex(ConsumerError, "SHA-256|count"):
            consume(self.ledger, export, batch_size=2)
        # Re-hash a duplicated row so only the duplicate check can catch it.
        lines = original.splitlines(keepends=True)
        dup = (export / "part-0001.jsonl").read_bytes().splitlines(keepends=True)[0]
        data = b"".join(lines[:4]) + dup
        part.write_bytes(data)
        manifest = json.loads((export / "manifest.json").read_text())
        manifest["partitions"][1]["sha256"] = hashlib.sha256(data).hexdigest()
        manifest["snapshot_id"] = "synthetic-export-dup"
        (export / "manifest.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ConsumerError, "duplicate record_id"):
            consume(self.ledger, export, batch_size=2, job_id="dup-job")
        self.assertEqual(self.ledger.record_count("synthetic-export-dup"), 0)

    def test_changed_snapshot_token_or_config_fails(self):
        consume(self.ledger, EXPORT, batch_size=5, max_batches=1)
        export = self.copy_export()
        manifest = json.loads((export / "manifest.json").read_text())
        manifest["observed_at"] = "2026-11-16T00:00:00Z"  # same snapshot_id, different manifest
        (export / "manifest.json").write_text(json.dumps(manifest))
        with self.assertRaises(LedgerError):
            consume(self.ledger, export, batch_size=5)
        with self.assertRaises(IncompatibleCheckpoint):
            consume(self.ledger, EXPORT, batch_size=5, analysis_config={"cohort": "different"})

    def test_fixture_cannot_pass_as_live(self):
        from foia_bias.aggregator_contract import ContractError
        with self.assertRaises(ContractError):
            consume(self.ledger, EXPORT, expected_fixture=False)

    def test_cli_consume(self):
        result = subprocess.run([sys.executable, "main.py", "consume", "--export", str(EXPORT),
                                 "--ledger", str(self.tmp / "cli" / "analysis.sqlite"), "--batch-size", "4"],
                                cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual((summary["status"], summary["records_committed"]), ("complete", 15))
        bad = subprocess.run([sys.executable, "main.py", "consume", "--export", str(EXPORT), "--live",
                              "--ledger", str(self.tmp / "cli2" / "analysis.sqlite")],
                             cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(bad.returncode, 2)
        self.assertEqual(bad.stdout, "")


if __name__ == "__main__":
    unittest.main()
