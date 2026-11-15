"""FBA-07 full-text and extraction completeness validation.

Bias does not rebuild OCR or re-fetch originals. It validates the text state of
every record in a snapshot and produces a deterministic gap report whose repair
items are keyed by Aggregator ``record_id`` and returned upstream.

FC-13/1 exports text-index status but no text body, page offsets or text hash.
Every indexed record therefore appears as ``waiting_upstream_text`` in a live
snapshot until FOIA_Aggregator publishes a text export. That is a contract
gap, reported as such, never a trigger for a Bias crawler.
"""
from __future__ import annotations

import hashlib

from foia_bias.ledger import canonical

ISSUES = {
    "partial": ("partial_text", "re-extract complete text (index truncated)"),
    "failed": ("upstream_extraction_failed", "repair extraction/OCR for the preserved binary"),
    "unsupported": ("upstream_unsupported_format", "add an extractor for this format"),
    "waiting": ("waiting_upstream_text", "export full text for this record (FC-13/1 has no text body)"),
}


class TextIntegrityError(ValueError):
    pass


def verify_text(stored: dict, text: str) -> str:
    """Check that supplied text belongs to the stored record; return its hash."""
    if stored["text_sha256"] is None:
        raise TextIntegrityError(f"{stored['record_id']} has no extracted text on record")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if digest != stored["text_sha256"]:
        raise TextIntegrityError(f"text does not match {stored['record_id']} (hash association failed)")
    if stored["char_count"] is not None and stored["char_count"] != len(text):
        raise TextIntegrityError(f"text length disagrees with {stored['record_id']} char_count")
    return digest


def classify(row: dict):
    """Return (issue, upstream_action) or (None, None) for text-ready records."""
    if row["record_type"] != "release":
        return "not_document_content", None
    if row["archive_state"] == "remote_only":
        return "not_preserved_upstream", "acquire and preserve the released binary"
    if row["text_status"] == "extracted" and row["text_sha256"]:
        return None, None
    return ISSUES.get(row["text_status"], ("unknown_text_state", "inspect text state"))


def gap_report(ledger, snapshot_id: str) -> dict:
    snapshot = ledger.snapshot(snapshot_id)
    counts, items = {"text_ready": 0}, []
    for row in ledger.records(snapshot_id):
        issue, action = classify(row)
        if issue is None:
            counts["text_ready"] += 1
            continue
        counts[issue] = counts.get(issue, 0) + 1
        if action:
            items.append({"record_id": row["record_id"], "issue": issue, "upstream_action": action,
                          "agency": row["agency"], "source_family": row["source_family"]})
    total = sum(counts.values())
    items.sort(key=lambda i: (i["issue"], i["record_id"]))
    body = {
        "snapshot_id": snapshot_id, "contract_version": snapshot["contract_version"],
        "fixture_mode": bool(snapshot["fixture_mode"]), "records": total,
        "counts": dict(sorted(counts.items())),
        "text_coverage": round(counts["text_ready"] / total, 6) if total else None,
        "repair_items": items,
        "contract_gaps": ["FC-13/1 exports no document text, text hash or page offsets"],
    }
    if total != snapshot["total_records"]:
        raise TextIntegrityError("gap report does not cover the whole snapshot census")
    return dict(body, report_sha256=hashlib.sha256(canonical(body).encode()).hexdigest())
