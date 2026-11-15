"""FBA-11 versioned analytical cohorts and deterministic derived variables.

Unit of analysis: one released document (``record_type == release``). Request
logs, annual reports and request descriptions are context and never enter
content regressions. Every input record ends in exactly one place: a cohort
row or an exclusion with a stated reason, so the census always reconciles.

Deterministic: output rows are sorted by record ID and do not depend on input
order. Party-target ties are ``mixed`` (never broken by set iteration order).
Thresholds come from the versioned configuration and change outputs.
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass

from foia_bias.ledger import canonical
from foia_bias.processing.admin_mapping import TERMS, analysis_date, assign_period

COHORT_VERSION = "fba11-v1"


@dataclass(frozen=True)
class CohortConfig:
    wrongdoing_threshold: float = 0.5
    transition_months: int = 6
    jurisdictions: tuple = ("us-federal",)
    duplicate_policy: str = "cluster_representative"  # or "all"
    require_relevance: tuple = ("low", "high")

    def __post_init__(self):
        if not 0 < self.wrongdoing_threshold < 1:
            raise ValueError("wrongdoing_threshold must be in (0, 1)")
        if self.duplicate_policy not in ("cluster_representative", "all"):
            raise ValueError("unknown duplicate policy")

    def digest(self):
        return hashlib.sha256(canonical([COHORT_VERSION, asdict(self)]).encode()).hexdigest()


def _clusters(records):
    """Union-find over duplicate_of relations and identical binaries (redaction variants stay distinct)."""
    parent = {r["record_id"]: r["record_id"] for r in records}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    first_by_binary = {}
    for r in records:
        # Identical preserved binaries (same SHA-256) found at different URLs or
        # sources are one disclosure for analytical weighting.
        if r.get("original_sha256"):
            other = first_by_binary.setdefault(r["original_sha256"], r["record_id"])
            if other != r["record_id"]:
                a, b = find(r["record_id"]), find(other)
                if a != b:
                    parent[max(a, b)] = min(a, b)
        for rel in r["relations"]:
            if rel["kind"] == "duplicate_of" and rel["record_id"] in parent:
                a, b = find(r["record_id"]), find(rel["record_id"])
                if a != b:
                    parent[max(a, b)] = min(a, b)
    return {rid: find(rid) for rid in parent}


def _date_for(record):
    dates = {k: v["value"] for k, v in record["dates"].items() if v["value"]}
    field_name, value = analysis_date(dates)
    if value is None:
        return None, None, "unknown"
    return field_name, value, record["dates"][field_name]["precision"]


def _assign(value, precision, months):
    if precision in ("exact", "day"):
        return assign_period(value, months)
    if precision == "month":
        year, month = int(value[:4]), int(value[5:7])
        first = assign_period(f"{year:04d}-{month:02d}-01", months)
        last = assign_period(f"{year:04d}-{month:02d}-28", months)
        if first["term_id"] and first["term_id"] == last["term_id"] and not (month == 1 and any(
                t.start_year == year for t in TERMS)):
            return first
        return dict(first, status="ambiguous_imprecise_date", cohort_eligible=False,
                    exclusion_reason="ambiguous_imprecise_date", admin_party=None, term_id=None)
    return {"cohort_eligible": False, "exclusion_reason": "imprecise_date"}


def party_target(targets):
    counts = {"D": 0, "R": 0}
    for t in targets:
        if t["party"] in counts:
            counts[t["party"]] += 1
    if counts["D"] == counts["R"] == 0:
        return "none"
    if counts["D"] == counts["R"]:
        return "mixed"
    return "D" if counts["D"] > counts["R"] else "R"


def build_cohort(records, labels, config: CohortConfig = CohortConfig()):
    """``records``: contract dicts; ``labels``: {record_id: label dict or {"state": ...}}."""
    records = sorted(records, key=lambda r: r["record_id"])
    cluster_of = _clusters(records)
    candidates, excluded = [], []

    def exclude(record_id, reason):
        excluded.append({"record_id": record_id, "reason": reason})

    for record in records:
        rid = record["record_id"]
        if record["record_type"] != "release":
            exclude(rid, f"context_record:{record['record_type']}")
            continue
        if record["jurisdiction"] not in config.jurisdictions:
            exclude(rid, "outside_jurisdiction:" + record["jurisdiction"])
            continue
        label = labels.get(rid)
        if not label or label.get("state", "labeled") != "labeled":
            exclude(rid, "unlabeled:" + (label.get("state") if label else "no_label"))
            continue
        field_name, value, precision = _date_for(record)
        if value is None:
            exclude(rid, "unknown_date")
            continue
        period = _assign(value, precision, config.transition_months)
        if not period.get("cohort_eligible"):
            exclude(rid, period.get("exclusion_reason") or "not_cohort_eligible")
            continue
        candidates.append((record, label, field_name, value, period))
    if config.duplicate_policy == "cluster_representative":
        representative = {}
        for record, *_ in candidates:
            cluster = cluster_of[record["record_id"]]
            representative.setdefault(cluster, record["record_id"])  # records sorted: smallest ID wins
        kept = []
        for item in candidates:
            rid = item[0]["record_id"]
            rep = representative[cluster_of[rid]]
            if rep != rid:
                exclude(rid, "duplicate_of:" + rep)
            else:
                kept.append(item)
        candidates = kept
    rows = []
    for record, label, field_name, value, period in candidates:
        if label["political_relevance"] not in config.require_relevance:
            exclude(record["record_id"], "not_political")
            continue
        target = party_target(label["main_partisan_targets"])
        if target not in ("D", "R"):
            exclude(record["record_id"], f"party_target_{target}")
            continue
        incumbent = period["admin_party"]
        opposition = "R" if incumbent == "D" else "D"
        wrong = label["wrongdoing_by_party"]
        fav = label["favorability_by_party"]
        rows.append({
            "record_id": record["record_id"], "source_family": record["source_family"],
            "agency": record["agency"], "jurisdiction": record["jurisdiction"],
            "analysis_date_field": field_name, "analysis_date": value,
            "year": int(value[:4]), "term_id": period["term_id"], "admin_name": period["admin_name"],
            "admin_party": incumbent, "is_transition": period["is_transition"],
            "party_target": target, "same_party": int(target == incumbent),
            "wrongdoing_target": wrong[target],
            "wrongdoing_any": int(wrong[target] >= config.wrongdoing_threshold),
            "fav_target": fav[target],
            "fav_incumbent_minus_opposition": round(fav[incumbent] - fav[opposition], 10),
            "cluster_id": cluster_of[record["record_id"]],
        })
    reasons = {}
    for item in excluded:
        reasons[item["reason"]] = reasons.get(item["reason"], 0) + 1
    excluded.sort(key=lambda e: e["record_id"])
    if len(rows) + len(excluded) != len(records):
        raise AssertionError("cohort census does not reconcile")
    return {"cohort_version": COHORT_VERSION, "config": asdict(config), "config_sha256": config.digest(),
            "input_records": len(records), "row_count": len(rows), "rows": rows,
            "exclusions": dict(sorted(reasons.items())), "excluded": excluded}


def cohort_digest(cohort) -> str:
    return hashlib.sha256(canonical(cohort).encode()).hexdigest()


def term_exposure_years():
    """Completed-term exposure (years) for exposure-normalized pooled comparisons."""
    return {t.term_id: t.end_year - t.start_year for t in TERMS if t.in_scope and t.completed}
