import copy
import math
import random
import unittest

from foia_bias.analysis.cohort import CohortConfig, build_cohort, cohort_digest, party_target
from foia_bias.analysis.regression import (ModelSpec, analysis_report, exposure_normalized_rates, favorability_spec,
                                           fit, wrongdoing_spec)
from foia_bias.llm.labeler import ChunkConfig, label_document
from foia_bias.llm.provider import FakeProvider
from tests.fixture_helpers import fixture_records, rid
from tests.test_labeling import with_text

TERMS = [("clinton-1", "D", 1994), ("clinton-2", "D", 1998), ("bush-1", "R", 2002), ("bush-2", "R", 2006),
         ("obama-1", "D", 2010), ("obama-2", "D", 2014), ("trump-1", "R", 2018), ("biden-1", "D", 2022)]


def labels_for(records):
    provider = FakeProvider()
    out = {}
    for rid, record in records.items():
        outcome = label_document(record, provider, ChunkConfig(max_chars=8000, overlap=400))
        out[rid] = dict(outcome["label"], state="labeled") if outcome["state"] == "labeled" else {
            "state": outcome["state"]}
    return out


def synthetic_rows(n, effect, seed, fav_effect=0.0, agencies=20):
    rng = random.Random(seed)
    rows = []
    agency_shift = {f"A{i:02d}": rng.gauss(0, 0.3) for i in range(agencies)}
    for i in range(n):
        term, party, year = TERMS[i % len(TERMS)]
        agency = f"A{rng.randrange(agencies):02d}"
        target = rng.choice("DR")
        same = int(target == party)
        p = 1 / (1 + math.exp(-(-0.4 + effect * same + agency_shift[agency])))
        rows.append({"record_id": f"syn-{i:05d}", "agency": agency, "year": year + rng.randrange(4) % 4,
                     "term_id": term, "admin_party": party, "party_target": target, "same_party": same,
                     "wrongdoing_any": int(rng.random() < p),
                     "fav_target": max(-1.0, min(1.0, fav_effect * same + rng.gauss(0, 0.3)))})
    return rows


class CohortTests(unittest.TestCase):
    def setUp(self):
        self.records = fixture_records()
        self.labels = labels_for(self.records)

    def test_fixture_cohort_rows_and_exclusions(self):
        cohort = build_cohort(list(self.records.values()), self.labels)
        self.assertEqual(cohort["input_records"], 13)
        self.assertEqual(cohort["row_count"] + sum(cohort["exclusions"].values()), 13)
        dup_rep, dup_other = sorted([rid("rr-001"), rid("mr-001")])
        self.assertEqual(sorted(r["record_id"] for r in cohort["rows"]),
                         sorted([dup_rep, rid("mr-002"), rid("oc-001"), rid("rr-002")]))
        reasons = {e["record_id"]: e["reason"] for e in cohort["excluded"]}
        self.assertEqual(reasons[dup_other], "duplicate_of:" + dup_rep)  # identical preserved binary
        self.assertEqual(reasons[rid("rr-003")], "unknown_date")
        self.assertEqual(reasons[rid("rr-006")], "ambiguous_transition_day")
        self.assertEqual(reasons[rid("rr-007")], "ongoing_incomplete_term")
        self.assertEqual(reasons[rid("rr-008")], "imprecise_date")
        self.assertEqual(reasons[rid("rr-005")], "unlabeled:not_labeled")
        self.assertEqual(reasons[rid("rr-009")], "unlabeled:not_labeled")
        oc = next(r for r in cohort["rows"] if r["record_id"] == rid("oc-001"))
        self.assertEqual((oc["admin_party"], oc["party_target"], oc["same_party"], oc["wrongdoing_any"]),
                         ("D", "D", 1, 1))

    def test_shuffled_input_reproduces_exact_rows(self):
        records = list(self.records.values())
        first = cohort_digest(build_cohort(records, self.labels))
        for seed in range(5):
            shuffled = list(records)
            random.Random(seed).shuffle(shuffled)
            self.assertEqual(cohort_digest(build_cohort(shuffled, dict(reversed(list(self.labels.items()))))), first)

    def test_thresholds_and_duplicate_policy_change_output(self):
        base = build_cohort(list(self.records.values()), self.labels)
        strict = build_cohort(list(self.records.values()), self.labels, CohortConfig(wrongdoing_threshold=0.95))
        self.assertNotEqual(base["config_sha256"], strict["config_sha256"])
        oc = lambda c: next(r for r in c["rows"] if r["record_id"] == rid("oc-001"))
        self.assertEqual((oc(base)["wrongdoing_any"], oc(strict)["wrongdoing_any"]), (1, 0))
        all_docs = build_cohort(list(self.records.values()), self.labels, CohortConfig(duplicate_policy="all"))
        self.assertEqual(all_docs["row_count"], base["row_count"] + 1)

    def test_january_2025_completed_term_records_are_kept(self):
        record = copy.deepcopy(self.records[rid("rr-002")])
        record["record_id"] = "rec_jan2025"
        record["relations"] = []
        record["dates"]["release_date"]["value"] = "2025-01-10"
        record["original_sha256"] = "c" * 64  # a distinct disclosure, not a duplicate binary
        records = dict(self.records, **{"rec_jan2025": record})
        labels = dict(self.labels, **{"rec_jan2025": self.labels[rid("rr-002")]})
        row = next(r for r in build_cohort(list(records.values()), labels)["rows"]
                   if r["record_id"] == "rec_jan2025")
        self.assertEqual((row["term_id"], row["admin_party"], row["year"]), ("biden-1", "D", 2025))

    def test_ties_are_mixed_and_excluded_never_arbitrary(self):
        self.assertEqual(party_target([{"party": "D"}, {"party": "R"}]), "mixed")
        self.assertEqual(party_target([{"party": "unknown"}]), "none")
        record = with_text(self.records[rid("rr-003")],
                           "SYN [[signal party=D wrongdoing=0.5 favorability=0]] [[signal party=R wrongdoing=0.5 "
                           "favorability=0]]")
        record["dates"]["decision_date"] = {"value": "2010-05-05", "precision": "day"}
        label = label_document(record, FakeProvider())["label"]
        cohort = build_cohort([record], {record["record_id"]: dict(label, state="labeled")})
        self.assertEqual(cohort["excluded"][0]["reason"], "party_target_mixed")

    def test_party_label_swap_symmetry(self):
        rows = synthetic_rows(800, 0.8, seed=3)
        swap = {"D": "R", "R": "D"}
        swapped = [dict(r, admin_party=swap[r["admin_party"]], party_target=swap[r["party_target"]]) for r in rows]
        self.assertEqual([r["same_party"] for r in rows],
                         [int(r["party_target"] == r["admin_party"]) for r in swapped])
        a, b = fit(rows, wrongdoing_spec()), fit(swapped, wrongdoing_spec())
        self.assertEqual(a["estimate"], b["estimate"])


class RegressionTests(unittest.TestCase):
    def test_known_signal_is_recovered_and_null_is_not_significant(self):
        signal = fit(synthetic_rows(1200, 1.2, seed=11), wrongdoing_spec())
        self.assertEqual(signal["status"], "estimated")
        self.assertGreater(signal["ci95"][0], 0)
        self.assertLess(abs(signal["estimate"] - 1.2), 0.4)
        null = fit(synthetic_rows(1200, 0.0, seed=12), wrongdoing_spec())
        self.assertLessEqual(null["ci95"][0], 0)
        self.assertGreaterEqual(null["ci95"][1], 0)
        ols = fit(synthetic_rows(1200, 0.0, seed=13, fav_effect=0.25), favorability_spec())
        self.assertLess(abs(ols["estimate"] - 0.25), 0.06)
        self.assertGreater(ols["ci95"][0], 0)

    def test_reproducible_and_order_independent(self):
        rows = synthetic_rows(600, 0.5, seed=5)
        shuffled = list(rows)
        random.Random(1).shuffle(shuffled)
        self.assertEqual(fit(rows, wrongdoing_spec()), fit(shuffled, wrongdoing_spec()))

    def test_each_option_changes_recorded_specification(self):
        rows = synthetic_rows(600, 0.5, seed=6)
        specs = [wrongdoing_spec(), wrongdoing_spec(fixed_effects=("agency",)), wrongdoing_spec(fixed_effects=()),
                 wrongdoing_spec(cluster=None), favorability_spec()]
        results = [fit(rows, s) for s in specs]
        self.assertEqual(len({r["spec"]["spec_sha256"] for r in results}), len(specs))
        self.assertEqual(len({r["std_error"] for r in results}), len(specs))
        self.assertEqual(results[3]["spec"]["covariance"], "classical")

    def test_unsuitable_data_returns_diagnostics(self):
        rows = synthetic_rows(400, 0.5, seed=7)
        cases = {
            "empty_cohort": [],
            "too_few_rows": rows[:5],
            "no_outcome_variation": [dict(r, wrongdoing_any=0) for r in rows],
            "no_regressor_variation": [dict(r, same_party=1) for r in rows],
            "too_few_clusters": [dict(r, agency=f"A{i % 3}") for i, r in enumerate(rows)],
            "separation_or_nonconvergence": [dict(r, wrongdoing_any=r["same_party"]) for r in rows],
        }
        for reason, data in cases.items():
            with self.subTest(reason=reason):
                result = fit(data, wrongdoing_spec())
                self.assertEqual(result["status"], "diagnostic")
                self.assertEqual(result["reason"], reason)
                self.assertNotIn("estimate", result)
        collinear = [dict(r, agency=f"A{r['same_party']}", year=r["same_party"]) for r in rows]
        result = fit(collinear, ModelSpec("ols", "fav_target", cluster=None))
        self.assertEqual((result["status"], result["reason"]), ("diagnostic", "singular_design"))

    def test_exposure_normalization_and_report(self):
        rows = synthetic_rows(800, 0.5, seed=8)
        rates = exposure_normalized_rates(rows)
        self.assertEqual(rates["pooled_by_incumbent_party"]["D"]["exposure_years"], 20)
        self.assertEqual(rates["pooled_by_incumbent_party"]["R"]["exposure_years"], 12)
        self.assertEqual(rates["per_term"]["biden-1"]["exposure_years"], 4)
        cohort = {"cohort_version": "x", "config_sha256": "y", "row_count": len(rows), "exclusions": {},
                  "rows": rows}
        report = analysis_report(cohort)
        self.assertEqual(report, analysis_report(cohort))
        self.assertFalse(report["research_eligible"])
        self.assertIn("not evidence of suppression", report["estimand"])


if __name__ == "__main__":
    unittest.main()
