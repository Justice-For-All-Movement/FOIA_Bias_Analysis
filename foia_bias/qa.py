"""FBA-13 automated annotation quality and robustness checks.

No human annotation. Pass/fail thresholds are fixed here *before* any results
are seen (``QA_THRESHOLDS``) and failures are reported, never tuned away. QA
samples test measurement quality; they never replace all-document labeling.

Checks
* party-swap symmetry: swapping D/R in a document must swap the labels;
  a deliberately asymmetric labeler is detected.
* repeat agreement: labeling the same text twice must agree.
* full-text vs truncation: how many decisions change when only the first
  20,000 characters are seen (the legacy behaviour).
* prefilter-loss audit: how many politically relevant documents the legacy
  keyword prefilter would have discarded with a neutral label.
* sensitivity grid: cohort/model results across thresholds, transition
  windows, duplicate policy and source strata, with diagnostics kept.
"""
from __future__ import annotations

import copy
import hashlib
import random
import re

from foia_bias.analysis.cohort import CohortConfig, build_cohort
from foia_bias.analysis.regression import fit, wrongdoing_spec
from foia_bias.ledger import canonical
from foia_bias.llm.labeler import ChunkConfig, label_document

QA_THRESHOLDS = {"max_mean_swap_asymmetry": 0.05, "min_repeat_agreement": 0.95}
LEGACY_KEYWORDS = ('democrat', 'democrats', 'democratic party', 'republican', 'republicans', 'republican party',
                   'gop', 'dnc', 'rnc', 'campaign', 'election', 'senator', 'congressional', 'president')
LEGACY_TRUNCATION = 20000
_SWAP = {"D": "R", "R": "D"}


def _with_text(record, text):
    out = copy.deepcopy(record)
    out["text"].update(content=text, text_sha256=hashlib.sha256(text.encode()).hexdigest(), char_count=len(text),
                       status="extracted", text_revision=1)
    return out


def swap_parties(text: str) -> str:
    return re.sub(r"party=(D|R)", lambda m: "party=" + _SWAP[m.group(1)], text)


def synthetic_documents(template_record, n=40, seed=0, late=False):
    """Synthetic documents with known signals (some placed after 20k chars)."""
    rng = random.Random(seed)
    docs = []
    for i in range(n):
        party = rng.choice("DR")
        wrong = round(rng.random(), 2)
        marker = f"[[signal party={party} wrongdoing={wrong} favorability={round(rng.uniform(-1, 1), 2)}]]"
        filler = "Synthetic routine text. " * (1000 if late and i % 2 else 5)
        text = (filler + marker) if late and i % 2 else (marker + filler)
        record = _with_text(template_record, f"SYNTHETIC QA doc {i}. " + text)
        record["record_id"] = f"qa-{seed}-{i:04d}"
        docs.append(record)
    return docs


def _label(record, provider, config):
    outcome = label_document(record, provider, config)
    return outcome.get("label") if outcome["state"] == "labeled" else None


def symmetry_check(docs, provider, config=ChunkConfig()):
    diffs = []
    for record in docs:
        original = _label(record, provider, config)
        swapped = _label(_with_text(record, swap_parties(record["text"]["content"])), provider, config)
        if original is None or swapped is None:
            continue
        for party in ("D", "R"):
            diffs.append(abs(original["wrongdoing_by_party"][party] - swapped["wrongdoing_by_party"][_SWAP[party]]))
    mean = sum(diffs) / len(diffs) if diffs else None
    return {"check": "party_swap_symmetry", "pairs": len(diffs) // 2, "mean_asymmetry": mean,
            "passed": mean is not None and mean <= QA_THRESHOLDS["max_mean_swap_asymmetry"]}


def repeat_agreement(docs, provider, config=ChunkConfig(), threshold=0.5):
    agree = total = 0
    for record in docs:
        a, b = _label(record, provider, config), _label(record, provider, config)
        if a is None or b is None:
            continue
        total += 1
        agree += all((a["wrongdoing_by_party"][p] >= threshold) == (b["wrongdoing_by_party"][p] >= threshold)
                     for p in ("D", "R"))
    rate = agree / total if total else None
    return {"check": "repeat_agreement", "documents": total, "agreement": rate,
            "passed": rate is not None and rate >= QA_THRESHOLDS["min_repeat_agreement"]}


def truncation_comparison(docs, provider, config=ChunkConfig(), threshold=0.5):
    changed = []
    for record in docs:
        full = _label(record, provider, config)
        cut = _label(_with_text(record, record["text"]["content"][:LEGACY_TRUNCATION]), provider, config)
        if full is None or cut is None:
            continue
        if any((full["wrongdoing_by_party"][p] >= threshold) != (cut["wrongdoing_by_party"][p] >= threshold)
               for p in ("D", "R")):
            changed.append(record["record_id"])
    return {"check": "full_text_vs_truncation", "documents": len(docs), "decisions_changed": len(changed),
            "changed_record_ids": sorted(changed)}


def prefilter_loss_audit(docs, provider, config=ChunkConfig()):
    lost = []
    for record in docs:
        label = _label(record, provider, config)
        if label is None or label["political_relevance"] == "none":
            continue
        head = record["text"]["content"][:5000].lower()
        if not any(keyword in head for keyword in LEGACY_KEYWORDS):
            lost.append(record["record_id"])
    return {"check": "legacy_prefilter_loss", "relevant_documents_lost": len(lost), "record_ids": sorted(lost),
            "note": "documents the legacy keyword prefilter would have given neutral labels"}


def sensitivity_grid(records, labels, *, thresholds=(0.3, 0.5, 0.7), transition_months=(0, 6),
                     duplicate_policies=("cluster_representative", "all"), spec=None):
    spec = spec or wrongdoing_spec()
    families = sorted({r["source_family"] for r in records})
    strata = [("all", records)] + [(f, [r for r in records if r["source_family"] == f]) for f in families]
    rows = []
    for name, subset in strata:
        for threshold in thresholds:
            for months in transition_months:
                for policy in duplicate_policies:
                    config = CohortConfig(wrongdoing_threshold=threshold, transition_months=months,
                                          duplicate_policy=policy)
                    cohort = build_cohort(subset, labels, config)
                    result = fit(cohort["rows"], spec)
                    rows.append({"stratum": name, "threshold": threshold, "transition_months": months,
                                 "duplicate_policy": policy, "rows": cohort["row_count"], "status": result["status"],
                                 "estimate": result.get("estimate"), "reason": result.get("reason")})
    return {"check": "sensitivity_grid", "cells": rows,
            "grid_sha256": hashlib.sha256(canonical(rows).encode()).hexdigest()}


def qa_report(docs, provider, config=ChunkConfig()):
    checks = [symmetry_check(docs, provider, config), repeat_agreement(docs, provider, config),
              truncation_comparison(docs, provider, config), prefilter_loss_audit(docs, provider, config)]
    gated = [c for c in checks if "passed" in c]
    return {"thresholds": QA_THRESHOLDS, "checks": checks, "passed": all(c["passed"] for c in gated),
            "failures": [c["check"] for c in gated if not c["passed"]],
            "scope": "QA of measurement quality on synthetic/sampled documents; not a substitute for full labeling"}
