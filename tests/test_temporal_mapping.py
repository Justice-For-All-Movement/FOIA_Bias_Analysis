import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from foia_bias.processing.actor_roster import load_roster
from foia_bias.processing.admin_mapping import (analysis_date, assign_period, get_admin_for_date,
                                                term_windows)

ROOT = Path(__file__).resolve().parents[1]
BOUNDARIES = {1993: ("bush41", "clinton-1"), 2001: ("clinton-2", "bush-1"), 2009: ("bush-2", "obama-1"),
              2017: ("obama-2", "trump-1"), 2021: ("trump-1", "biden-1"), 2025: ("biden-1", "trump-2")}


class AdministrationMappingTests(unittest.TestCase):
    def test_fba08_regression_transition_flag_never_changes_incumbent(self):
        # Previously returned Trump/R with transition_months=6.
        for months in (0, 1, 6, 12):
            with self.subTest(months=months):
                info = get_admin_for_date("2021-01-21", months)
                self.assertEqual((info["admin_name"], info["admin_party"]), ("Biden", "D"))
                self.assertEqual(info["is_transition"], months > 0)

    def test_every_boundary_day_before_and_after(self):
        for year, (outgoing, incoming) in BOUNDARIES.items():
            with self.subTest(year=year):
                before = assign_period(f"{year}-01-19", 6)
                after = assign_period(f"{year}-01-21", 6)
                self.assertEqual(before["term_id"], outgoing)
                self.assertEqual(after["term_id"], incoming)
                self.assertTrue(before["is_transition"] and after["is_transition"])
                self.assertFalse(assign_period(f"{year}-01-19", 0)["is_transition"])

    def test_date_only_inauguration_day_is_ambiguous_not_guessed(self):
        for year, (outgoing, incoming) in BOUNDARIES.items():
            with self.subTest(year=year):
                info = assign_period(f"{year}-01-20")
                self.assertEqual(info["status"], "ambiguous_transition_day")
                self.assertIsNone(info["admin_party"])
                self.assertEqual(info["candidates"], [outgoing, incoming])
                self.assertFalse(info["cohort_eligible"])
                self.assertIsNone(get_admin_for_date(f"{year}-01-20", 6)["admin_name"])

    def test_exact_timestamps_resolve_inauguration_day_at_noon_eastern(self):
        self.assertEqual(assign_period("2021-01-20T16:59:59Z")["term_id"], "trump-1")
        self.assertEqual(assign_period("2021-01-20T17:00:00Z")["term_id"], "biden-1")
        self.assertEqual(assign_period("2021-01-20T11:59:00-05:00")["term_id"], "trump-1")
        self.assertEqual(assign_period(datetime(2009, 1, 20, 18, tzinfo=timezone.utc))["term_id"], "obama-1")
        # A timezone-less time on inauguration day is still ambiguous.
        self.assertEqual(assign_period("2017-01-20T12:30:00")["status"], "ambiguous_transition_day")

    def test_lower_bound_unknown_and_ongoing_term(self):
        self.assertEqual(assign_period("1993-01-19")["exclusion_reason"], "before_scope")
        self.assertEqual(assign_period("1985-06-01")["status"], "before_scope")
        self.assertTrue(assign_period("1993-01-21")["cohort_eligible"])
        unknown = assign_period(None)
        self.assertEqual((unknown["status"], unknown["exclusion_reason"]), ("unknown_date", "unknown_date"))
        ongoing = assign_period("2025-03-01")
        self.assertEqual((ongoing["admin_name"], ongoing["term_id"]), ("Trump", "trump-2"))
        self.assertFalse(ongoing["completed_term"])
        self.assertEqual(ongoing["exclusion_reason"], "ongoing_incomplete_term")
        self.assertFalse(ongoing["cohort_eligible"])
        self.assertTrue(assign_period("2025-01-19")["cohort_eligible"])

    def test_completed_term_windows_are_equal_duration(self):
        windows = term_windows()
        self.assertEqual([w["term_id"] for w in windows],
                         ["clinton-1", "clinton-2", "bush-1", "bush-2", "obama-1", "obama-2", "trump-1", "biden-1"])
        self.assertTrue(all(w["years"] == 4 and w["completed"] for w in windows))
        self.assertEqual(term_windows(include_ongoing=True)[-1]["end"], None)

    def test_invalid_transition_setting(self):
        for bad in (-1, 25, True, 1.5):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                assign_period("2010-01-01", bad)

    def test_submitted_dates_never_fill_decision_date(self):
        self.assertEqual(analysis_date({"submitted_date": "2020-01-01"}), (None, None))
        self.assertEqual(analysis_date({"submitted_date": "2020-01-01", "release_date": "2021-02-01"}),
                         ("release_date", "2021-02-01"))
        with self.assertRaises(ValueError):
            analysis_date({}, policy=("submitted_date",))


class RosterTests(unittest.TestCase):
    def test_executive_roster_is_degraded_but_dated(self):
        roster = load_roster(ROOT / "fixtures" / "executive_roster.json")
        self.assertFalse(roster.certifies_full_coverage)
        self.assertEqual(roster.party_on("exec:biden-j", "2012-05-01"), "D")
        self.assertEqual(roster.party_on("exec:biden-j", "2019-05-01"), "unknown")
        self.assertEqual(roster.lookup("president trump"), ["exec:trump-d"])
        self.assertEqual(len(roster.sha256), 64)

    def test_party_switch_and_mixed_roles(self):
        roster = load_roster(ROOT / "fixtures" / "synthetic_roster.json")
        self.assertTrue(roster.certifies_full_coverage)
        self.assertEqual(roster.party_on("syn:switcher", date(2009, 4, 29)), "R")
        self.assertEqual(roster.party_on("syn:switcher", date(2009, 4, 30)), "D")
        self.assertEqual(roster.party_on("syn:dual", "2010-01-01"), "mixed")
        self.assertEqual(roster.party_on("syn:nobody", "2010-01-01"), "unknown")

    def test_malformed_roster_rejected(self):
        import json
        import tempfile
        base = json.loads((ROOT / "fixtures" / "synthetic_roster.json").read_text())
        bad_variants = []
        v = json.loads(json.dumps(base)); v["coverage"] = "full"; bad_variants.append(v)
        v = json.loads(json.dumps(base)); v["actors"][0]["roles"][0]["party"] = "Whig"; bad_variants.append(v)
        v = json.loads(json.dumps(base)); v["actors"][0]["roles"][0]["end"] = "2001-01-01"; bad_variants.append(v)
        v = json.loads(json.dumps(base)); v["actors"].append(v["actors"][0]); bad_variants.append(v)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "roster.json"
            for variant in bad_variants:
                path.write_text(json.dumps(variant))
                with self.assertRaises(ValueError):
                    load_roster(path)


if __name__ == "__main__":
    unittest.main()
