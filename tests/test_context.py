import copy
import json
import unittest

from foia_bias.analysis.cohort import build_cohort
from foia_bias.context import ContextError, join_context, validate_context_row
from tests.fixture_helpers import ROOT, fixture_records, rid

ROWS = [json.loads(line) for line in (ROOT / "fixtures/context_v1/context.jsonl").read_text().splitlines()]


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.records = list(fixture_records().values())

    def test_evidence_only_joins_and_census(self):
        report = join_context(ROWS, self.records)
        self.assertEqual(report["counts"], {"joined": 2, "unjoined": 3, "annual": 1})
        joined = {j["context_id"]: j["record_id"] for j in report["joined"]}
        self.assertEqual(joined, {"log-001:Sheet1:1": rid("rr-002"), "log-001:Sheet1:2": rid("oc-001")})
        statuses = sorted(u["status"] for u in report["unjoined"])
        self.assertEqual(statuses, ["no_release_reference", "no_release_reference", "reference_not_in_snapshot"])
        self.assertEqual(report, join_context(list(reversed(ROWS)), self.records))

    def test_outcomes_use_closure_not_receipt_and_unknowns_stay_explicit(self):
        outcomes = {o["context_id"]: o for o in join_context(ROWS, self.records)["request_outcomes"]}
        self.assertEqual(outcomes["log-001:Sheet1:2"]["closure_term"], "obama-2")
        self.assertFalse(outcomes["log-001:Sheet1:4"]["outcome_known"])
        self.assertIsNone(outcomes["log-001:Sheet1:4"]["closure_term"])

    def test_validation_rules(self):
        log = ROWS[0]
        for mutate in (lambda r: r.update(requester_name="Jane Doe"), lambda r: r.update(synthetic=False),
                       lambda r: r.update(received_date="01/05/2018"),
                       lambda r: r.update(received_date="2018-05-01", closed_date="2018-01-01"),
                       lambda r: r.update(tracking_number=None, request_description=None),
                       lambda r: r.update(log_raw_sha256="x"), lambda r: r.update(kind="tweet")):
            bad = copy.deepcopy(log)
            mutate(bad)
            with self.subTest(mutation=mutate), self.assertRaises(ContextError):
                validate_context_row(bad, True)

    def test_context_never_enters_release_content_cohort(self):
        as_records = [dict(r, record_type="request_log_row") for r in self.records]
        cohort = build_cohort(as_records, {})
        self.assertEqual(cohort["row_count"], 0)
        self.assertTrue(all(e["reason"].startswith("context_record") for e in cohort["excluded"]))


if __name__ == "__main__":
    unittest.main()
