import unittest

from foia_bias.llm.labeler import ChunkConfig
from foia_bias.llm.provider import FakeProvider
from foia_bias.qa import (QA_THRESHOLDS, prefilter_loss_audit, qa_report, sensitivity_grid, swap_parties,
                          synthetic_documents, truncation_comparison)
from tests.fixture_helpers import fixture_records, rid
from tests.test_cohort_regression import labels_for

CONFIG = ChunkConfig(max_chars=8000, overlap=400)


class QATests(unittest.TestCase):
    def setUp(self):
        self.template = fixture_records()[rid("rr-003")]
        self.docs = synthetic_documents(self.template, n=30, seed=1)

    def test_symmetric_labeler_passes_and_asymmetric_one_is_detected(self):
        good = qa_report(self.docs, FakeProvider(), CONFIG)
        self.assertTrue(good["passed"], good)
        biased = qa_report(self.docs, FakeProvider(bias={"R": 0.3}), CONFIG)
        self.assertFalse(biased["passed"])
        self.assertEqual(biased["failures"], ["party_swap_symmetry"])
        self.assertEqual(QA_THRESHOLDS["max_mean_swap_asymmetry"], 0.05)  # fixed before results

    def test_swap_is_an_involution(self):
        text = "[[signal party=D wrongdoing=0.4 favorability=0]] [[signal party=R wrongdoing=0.1 favorability=0]]"
        self.assertEqual(swap_parties(swap_parties(text)), text)

    def test_truncation_and_prefilter_losses_are_measured(self):
        late = synthetic_documents(self.template, n=10, seed=2, late=True)
        report = truncation_comparison(late, FakeProvider(), CONFIG)
        self.assertGreater(report["decisions_changed"], 0)
        loss = prefilter_loss_audit(self.docs, FakeProvider(), CONFIG)
        self.assertEqual(loss["relevant_documents_lost"], 30)  # synthetic text has no legacy keywords

    def test_sensitivity_grid_is_reproducible_and_keeps_diagnostics(self):
        records = list(fixture_records().values())
        labels = labels_for(fixture_records())
        grid = sensitivity_grid(records, labels, thresholds=(0.5, 0.95), transition_months=(0,),
                                duplicate_policies=("cluster_representative", "all"))
        self.assertEqual(grid, sensitivity_grid(records, labels, thresholds=(0.5, 0.95), transition_months=(0,),
                                                duplicate_policies=("cluster_representative", "all")))
        self.assertTrue(all(c["status"] == "diagnostic" for c in grid["cells"]))  # tiny fixture: honest refusal
        self.assertIn("third_party_release", {c["stratum"] for c in grid["cells"]})


if __name__ == "__main__":
    unittest.main()
