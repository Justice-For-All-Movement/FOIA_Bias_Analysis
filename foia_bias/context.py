"""FBA-06 request-log and annual-context integration (evidence-only joins).

FC-13/1 exports released documents only. FOIA_Aggregator stores FC-03
request-log rows (``request_log_rows``) but does not export them yet, so this
module defines the Bias-side *proposed* context row, modelled field-for-field
on FC-03 minus personal fields, and validates fixtures against it. When
FOIA_Aggregator exports log rows, align ``LOG_ROW_FIELDS`` here.

Rules
* Context never enters release-content analysis: rows are a separate type and
  carry no document text.
* A log row joins a release only on evidence: its ``released_record_reference``
  equals a snapshot ``record_id`` or a URL whose FC-13 record ID is in the
  snapshot. No title/fuzzy matching. Everything else stays explicitly unjoined.
* ``received_date`` (request side) never fills a release or decision date.
  ``closed_date`` is the request's closure, used only for request-outcome tables.
* Personal fields (``requester_name``, ``requester_organization``) are rejected.
"""
from __future__ import annotations

import hashlib
import re
from datetime import date

from foia_bias import aggregator_contract as contract
from foia_bias.ledger import canonical
from foia_bias.processing.admin_mapping import assign_period

PROPOSAL_VERSION = "fba-context-proposal-1"
LOG_ROW_FIELDS = {"context_id", "kind", "log_id", "source_url", "log_raw_sha256", "agency", "component",
                  "tracking_number", "request_description", "received_date", "closed_date", "disposition",
                  "exemptions", "released_record_reference", "warnings", "synthetic"}
ANNUAL_FIELDS = {"context_id", "kind", "agency", "fiscal_year", "metrics", "source_url", "synthetic"}
PERSONAL_FIELDS = {"requester_name", "requester_organization"}


class ContextError(ValueError):
    pass


def _date_or_none(value, name):
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ContextError(f"{name} must be YYYY-MM-DD or null (FC-03 nulls unparseable dates)")
    date.fromisoformat(value)
    return value


def validate_context_row(row: dict, fixture: bool) -> dict:
    if not isinstance(row, dict):
        raise ContextError("context row must be an object")
    if PERSONAL_FIELDS & set(row):
        raise ContextError("personal requester fields are never accepted")
    if row.get("synthetic") is not fixture:
        raise ContextError("synthetic context cannot cross fixture/live boundary")
    if row.get("kind") == "request_log_row":
        if set(row) != LOG_ROW_FIELDS:
            raise ContextError("log row fields do not match " + PROPOSAL_VERSION)
        if not re.fullmatch(r"[0-9a-f]{64}", str(row["log_raw_sha256"])):
            raise ContextError("log_raw_sha256 required (FC-03 raw log hash)")
        received = _date_or_none(row["received_date"], "received_date")
        closed = _date_or_none(row["closed_date"], "closed_date")
        if received and closed and closed < received and "closed_before_received" not in row["warnings"]:
            raise ContextError("closed_before_received must be flagged, as FC-03 does")
        if not (row["tracking_number"] or row["request_description"]):
            raise ContextError("log rows need a tracking number or description (FC-03 failed-row rule)")
    elif row.get("kind") == "annual_report":
        if set(row) != ANNUAL_FIELDS or type(row["fiscal_year"]) is not int or not isinstance(row["metrics"], dict):
            raise ContextError("annual context fields do not match " + PROPOSAL_VERSION)
    else:
        raise ContextError("unknown context kind")
    return row


def join_context(rows, snapshot_records, fixture=True):
    """Return a deterministic join report between context rows and snapshot releases."""
    ids = {r["record_id"] for r in snapshot_records}
    joined, unjoined, annual, outcomes = [], [], [], []
    for row in sorted(rows, key=lambda r: r["context_id"]):
        validate_context_row(row, fixture)
        if row["kind"] == "annual_report":
            annual.append({"context_id": row["context_id"], "agency": row["agency"], "fiscal_year": row["fiscal_year"],
                           "use": "context_only_never_document_observation"})
            continue
        reference = row["released_record_reference"]
        target = None
        if reference:
            candidate = reference if reference.startswith("rec_") else contract.record_id_for(reference)
            target = candidate if candidate in ids else None
        status = "joined" if target else ("reference_not_in_snapshot" if reference else "no_release_reference")
        (joined if target else unjoined).append({"context_id": row["context_id"], "record_id": target,
                                                 "status": status, "agency": row["agency"]})
        closure = assign_period(row["closed_date"]) if row["closed_date"] else None
        outcomes.append({
            "context_id": row["context_id"], "agency": row["agency"], "disposition": row["disposition"],
            "received_date": row["received_date"], "closed_date": row["closed_date"],
            "closure_term": closure["term_id"] if closure else None,
            "closure_cohort_eligible": bool(closure and closure["cohort_eligible"]),
            "outcome_known": row["disposition"] is not None and row["closed_date"] is not None,
        })
    body = {
        "proposal_version": PROPOSAL_VERSION, "rows": len(rows),
        "counts": {"joined": len(joined), "unjoined": len(unjoined), "annual": len(annual)},
        "joined": joined, "unjoined": unjoined, "annual": annual, "request_outcomes": outcomes,
        "request_outcome_estimand": ("available only for logs with known disposition and closure; "
                                     "release-content models never use these rows"),
        "contract_gap": "FC-13/1 does not export FC-03 request-log rows or annual statistics",
    }
    if len(joined) + len(unjoined) + len(annual) != len(rows):
        raise ContextError("context census does not reconcile")
    return dict(body, report_sha256=hashlib.sha256(canonical(body).encode()).hexdigest())
