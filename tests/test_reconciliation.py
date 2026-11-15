import os
import tempfile
import unittest
from pathlib import Path

from foia_bias.consumer import consume
from foia_bias.ledger import AnalysisLedger
from foia_bias.reconciliation import analytical_weights, duplicate_clusters, reconcile_legacy_artifacts
from tests.fixture_helpers import EXPORT, SNAP, fixture_records, rid


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.records = list(fixture_records().values())

    def test_identical_binaries_cluster_and_keep_all_provenance(self):
        clusters = {c["cluster_id"]: c for c in duplicate_clusters(self.records)}
        pair = sorted([rid("rr-001"), rid("mr-001")])
        cluster = clusters[pair[0]]
        self.assertEqual(cluster["members"], pair)
        self.assertEqual(cluster["source_families"], ["agency_reading_room", "third_party_release"])
        self.assertEqual(len(cluster["provenance"]), 2)
        self.assertNotEqual(clusters.get(rid("rr-002"), {}).get("members"), sorted([rid("rr-002"), rid("mr-002")]))
        self.assertEqual(len(clusters), 12)  # 13 records, one duplicate pair

    def test_weights_never_give_duplicates_extra_weight(self):
        for policy in ("representative", "fractional"):
            weights = analytical_weights(self.records, policy)
            self.assertAlmostEqual(sum(weights.values()), 12.0)
            self.assertAlmostEqual(weights[rid("rr-001")] + weights[rid("mr-001")], 1.0)
        self.assertEqual(analytical_weights(list(reversed(self.records))), analytical_weights(self.records))

    def test_legacy_cache_reconciliation(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = AnalysisLedger(Path(folder) / "s" / "a.sqlite")
            try:
                consume(ledger, EXPORT, batch_size=50)
                legacy = Path(folder) / "legacy"
                legacy.mkdir()
                (legacy / "unknown.pdf").write_bytes(b"legacy bytes not in snapshot")
                os.symlink("/etc/hostname", legacy / "link.pdf")
                # The synthetic fixture's binary hash for rr-002 is sha256(b"binary:rr-002").
                (legacy / "known.pdf").write_bytes(b"binary:rr-002")
                report = reconcile_legacy_artifacts(ledger, SNAP, legacy)
                self.assertEqual([m["records"] for m in report["matched"]], [[rid("rr-002")]])
                self.assertEqual([c["path"] for c in report["upstream_import_candidates"]], ["unknown.pdf"])
                self.assertEqual(report["skipped"][0]["reason"], "symlink not followed")
                self.assertEqual(report, reconcile_legacy_artifacts(ledger, SNAP, legacy))
                self.assertEqual((legacy / "unknown.pdf").read_bytes(), b"legacy bytes not in snapshot")
            finally:
                ledger.close()


if __name__ == "__main__":
    unittest.main()
