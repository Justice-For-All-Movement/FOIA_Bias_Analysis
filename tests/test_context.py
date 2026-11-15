import copy
import unittest

from foia_bias import aggregator_contract as contract
from foia_bias.analysis.cohort import build_cohort
from foia_bias.context import ContextError, join_context
from foia_bias.extensions import ExtensionError, load_log_rows
from tests.fixture_helpers import LOG_ROWS, PRODUCER_ROOT, catalog_records, fixture_records, rid


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.records = list(fixture_records().values())
        self.rows = load_log_rows(LOG_ROWS)["records"]

    def test_evidence_only_joins_and_census(self):
        report = join_context(self.rows, self.records)
        self.assertEqual(report["counts"], {"joined": 2, "unjoined": 3})
        self.assertEqual(sorted(j["record_id"] for j in report["joined"]), sorted([rid("rr-002"), rid("oc-001")]))
        self.assertEqual(sorted(u["status"] for u in report["unjoined"]),
                         ["no_release_reference", "no_release_reference", "reference_not_in_snapshot"])
        self.assertEqual(report, join_context(list(reversed(self.rows)), self.records))

    def test_producer_generated_log_rows_conform_and_join(self):
        loaded = load_log_rows(PRODUCER_ROOT / "request-log-rows")
        self.assertEqual(loaded["producer_commit"], contract.AGGREGATOR_CONTRACT_COMMIT)
        catalog = [contract.to_analysis_record(r, fixture=True) for r in catalog_records(PRODUCER_ROOT / "catalog")]
        report = join_context(loaded["records"], catalog)
        self.assertEqual(report["rows"], len(loaded["records"]))
        self.assertTrue(all("requester_name" not in r for r in loaded["records"]))

    def test_outcomes_use_closure_not_receipt(self):
        outcomes = {o["row_id"]: o for o in join_context(self.rows, self.records)["request_outcomes"]}
        by_tracking = {r["tracking_number"]: r["row_id"] for r in self.rows}
        self.assertEqual(outcomes[by_tracking["DOJ-2018-0002"]]["closure_term"], "obama-2")
        self.assertFalse(outcomes[by_tracking["DOJ-2018-0004"]]["outcome_known"])

    def test_validation_rules(self):
        row = self.rows[0]
        for mutate in (lambda r: r.update(requester_name="Jane Doe"), lambda r: r.update(received_date="01/05/2018"),
                       lambda r: r.update(row_id="row-1"), lambda r: r["log"].update(format="pdf"),
                       lambda r: r.update(received_date="2018-02-30")):
            bad = copy.deepcopy(row)
            mutate(bad)
            with self.subTest(mutation=mutate), self.assertRaises(contract.ContractError):
                contract.validate_log_row(bad)
        bad = copy.deepcopy(row)
        bad.update(received_date="2018-05-01", closed_date="2018-01-01")
        with self.assertRaises(ContextError):
            join_context([bad], self.records)

    def test_live_log_export_requires_pinned_producer(self):
        with self.assertRaises(contract.ContractError):
            load_log_rows(LOG_ROWS, expected_fixture=False)

    def test_context_never_enters_release_content_cohort(self):
        as_records = [dict(r, record_type="request_log_row") for r in self.records]
        cohort = build_cohort(as_records, {})
        self.assertEqual(cohort["row_count"], 0)


if __name__ == "__main__":
    unittest.main()
