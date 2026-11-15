import tempfile
import unittest
from pathlib import Path

from foia_bias.consumer import consume
from foia_bias.ledger import AnalysisLedger
from foia_bias.text_completeness import TextIntegrityError, gap_report, verify_text
from tests.fixture_helpers import EXPORT, PRODUCER, SNAP, rid, texts


class TextCompletenessTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.ledger = AnalysisLedger(Path(self.folder.name) / "s" / "analysis.sqlite")
        consume(self.ledger, EXPORT, batch_size=50)

    def tearDown(self):
        self.ledger.close()
        self.folder.cleanup()

    def test_gap_report_covers_census_with_upstream_record_ids(self):
        report = gap_report(self.ledger, SNAP)
        self.assertEqual(report["records"], 13)
        self.assertEqual(report["counts"], {"not_preserved_upstream": 1, "partial_text": 1, "text_ready": 9,
                                            "upstream_extraction_failed": 1, "waiting_upstream_text": 1})
        issues = {i["record_id"]: i["issue"] for i in report["repair_items"]}
        self.assertEqual(issues, {rid("mr-003"): "not_preserved_upstream", rid("rr-005"): "partial_text",
                                  rid("rr-004"): "upstream_extraction_failed",
                                  rid("rr-009"): "waiting_upstream_text"})
        self.assertEqual(report, gap_report(self.ledger, SNAP))

    def test_producer_snapshot_reports_contract_gap(self):
        consume(self.ledger, PRODUCER, job_id="producer")
        report = gap_report(self.ledger, "snap_936bc978fe5fc655b664")
        self.assertEqual(report["counts"]["text_ready"], 0)
        self.assertIn("FC-13/1 exports no document text", report["contract_gaps"][0])

    def test_text_reuse_requires_matching_hash(self):
        rows = {r["record_id"]: r for r in self.ledger.records(SNAP)}
        stored = rows[rid("oc-001")]
        text = texts()[rid("oc-001")]
        self.assertEqual(verify_text(stored, text), stored["text_sha256"])
        self.assertIn("Late decisive passage", text[20000:])
        with self.assertRaises(TextIntegrityError):
            verify_text(stored, texts()[rid("rr-001")])
        with self.assertRaises(TextIntegrityError):
            verify_text(rows[rid("rr-004")], "anything")


if __name__ == "__main__":
    unittest.main()
