import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from foia_bias.consumer import consume
from foia_bias.ledger import AnalysisLedger
from foia_bias.llm.jobs import run_labeling
from foia_bias.llm.labeler import ChunkConfig
from foia_bias.llm.provider import FakeProvider, ProviderError
from tests.fixture_helpers import EXPORT, ROOT, SNAP, rid, texts

CONFIG = ChunkConfig(max_chars=8000, overlap=400)


class Flaky(FakeProvider):
    def __init__(self, failures):
        super().__init__()
        self.failures = failures

    def complete(self, system, user):
        if self.failures > 0:
            self.failures -= 1
            self.calls += 1
            raise ProviderError("transient")
        return super().complete(system, user)


class LabelingJobTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "s" / "analysis.sqlite"
        self.ledger = AnalysisLedger(self.path)
        consume(self.ledger, EXPORT, batch_size=50)
        self.texts = texts()

    def tearDown(self):
        self.ledger.close()
        self.folder.cleanup()

    def test_full_census_and_no_rebilling(self):
        provider = FakeProvider()
        result = run_labeling(self.ledger, SNAP, provider, texts=self.texts, config=CONFIG, workers=4)
        self.assertEqual(result["records"], 13)
        self.assertEqual(result["census"], {"labeled": 9, "not_labeled:partial_text_not_labeled": 1,
                                            "not_labeled:remote_only_waiting_upstream": 1,
                                            "not_labeled:upstream_extraction_failed": 1,
                                            "not_labeled:waiting_upstream_text": 1})
        calls = provider.calls
        again = run_labeling(self.ledger, SNAP, provider, texts=self.texts, config=CONFIG)
        self.assertEqual((provider.calls, again["census"]), (calls, result["census"]))

    def test_no_provider_pauses_with_census(self):
        result = run_labeling(self.ledger, SNAP, None, texts=self.texts)
        self.assertEqual(result["status"], "paused_no_provider")
        self.assertEqual(sum(result["census"].values()), 13)
        self.assertEqual(result["census"]["paused_no_provider"], 9)

    def test_budget_ceiling_then_resume(self):
        first = run_labeling(self.ledger, SNAP, FakeProvider(), texts=self.texts, config=CONFIG, max_calls=4)
        self.assertEqual(first["status"], "paused_budget")
        self.assertEqual(sum(first["census"].values()), 13)
        second = run_labeling(self.ledger, SNAP, FakeProvider(), texts=self.texts, config=CONFIG)
        self.assertEqual(second["census"]["labeled"], 9)

    def test_bounded_retries(self):
        ok = run_labeling(self.ledger, SNAP, Flaky(1), texts=self.texts, config=CONFIG, workers=1, max_retries=2)
        self.assertEqual(ok["census"]["labeled"], 9)
        with tempfile.TemporaryDirectory() as other:
            ledger = AnalysisLedger(Path(other) / "a.sqlite")
            try:
                consume(ledger, EXPORT, batch_size=50)
                bad = run_labeling(ledger, SNAP, Flaky(10 ** 6), texts=self.texts, config=CONFIG, max_retries=1)
                self.assertEqual(bad["census"]["failed"], 9)
            finally:
                ledger.close()

    def test_crash_after_provider_response_is_billing_ambiguous_not_retried(self):
        code = ("import os, sys\n"
                "from foia_bias.ledger import AnalysisLedger\n"
                "from foia_bias.llm.jobs import run_labeling\n"
                "from foia_bias.llm.labeler import ChunkConfig\n"
                "from foia_bias.llm.provider import FakeProvider\n"
                "from tests.fixture_helpers import SNAP, texts\n"
                "ledger = AnalysisLedger(sys.argv[1])\n"
                "def die(r):\n"
                "    os._exit(9)\n"
                "run_labeling(ledger, SNAP, FakeProvider(), texts=texts(), workers=1,"
                " config=ChunkConfig(max_chars=8000, overlap=400), hooks={'after_provider': die})\n")
        self.ledger.close()
        result = subprocess.run([sys.executable, "-c", code, str(self.path)], cwd=ROOT, capture_output=True,
                                text=True, timeout=60)
        self.assertEqual(result.returncode, 9, result.stderr)
        self.ledger = AnalysisLedger(self.path)
        provider = FakeProvider()
        resumed = run_labeling(self.ledger, SNAP, provider, texts=self.texts, config=CONFIG)
        self.assertEqual(resumed["census"]["billing_ambiguous"], 9)
        self.assertEqual(provider.calls, 0)
        explicit = run_labeling(self.ledger, SNAP, provider, texts=self.texts, config=CONFIG, retry_ambiguous=True)
        self.assertEqual(explicit["census"]["labeled"], 9)

    def test_text_must_match_stored_hash(self):
        bad = dict(self.texts)
        bad[rid("rr-001")] = bad[rid("rr-002")]
        with self.assertRaises(Exception):
            run_labeling(self.ledger, SNAP, FakeProvider(), texts=bad, config=CONFIG)


if __name__ == "__main__":
    unittest.main()
