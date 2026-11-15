"""FBA-06 request-log context integration (evidence-only joins).

Consumes FOIA_Aggregator ``foia-request-log-export/1`` rows (FC-03 request logs,
pinned in ``aggregator_contract``; loaded and verified by
``extensions.load_log_rows``). The export never contains requester names,
organizations or raw cells, and its schema forbids extra fields.

Rules
* Context never enters release-content analysis: log rows are request records,
  not documents, and carry no document text.
* A log row joins a release only on evidence: ``released_record_reference``
  equals a snapshot ``record_id`` or a URL whose FC-13 record ID is in the
  snapshot. No title/fuzzy matching; everything else stays explicitly
  unjoined. FOIA_Aggregator itself calls the reference "pointer only", so a
  join links records and does not prove the released binary matches.
* ``received_date`` (request side) never fills a release or decision date;
  ``closed_date`` is used only for request-outcome tables.
* Annual FOIA statistics are not exported by FOIA_Aggregator yet (gap).
"""
from __future__ import annotations

import hashlib

from foia_bias import aggregator_contract as contract
from foia_bias.ledger import canonical
from foia_bias.processing.admin_mapping import assign_period


class ContextError(ValueError):
    pass


def join_context(rows, snapshot_records):
    """Deterministic join report between request-log rows and snapshot releases."""
    ids = {r["record_id"] for r in snapshot_records}
    joined, unjoined, outcomes = [], [], []
    for row in sorted(rows, key=lambda r: r["row_id"]):
        contract.validate_log_row(row)
        if row["closed_date"] and row["received_date"] and row["closed_date"] < row["received_date"] \
                and "closed_before_received" not in row["warnings"]:
            raise ContextError("closed-before-received rows must carry the FC-03 warning")
        reference = row["released_record_reference"]
        target = None
        if reference:
            candidate = reference if reference.startswith("rec_") else contract.record_id_for(reference)
            target = candidate if candidate in ids else None
        status = "joined" if target else ("reference_not_in_snapshot" if reference else "no_release_reference")
        (joined if target else unjoined).append({"row_id": row["row_id"], "log_id": row["log"]["log_id"],
                                                 "record_id": target, "status": status,
                                                 "agency": row["log"]["agency"]})
        closure = assign_period(row["closed_date"]) if row["closed_date"] else None
        outcomes.append({
            "row_id": row["row_id"], "agency": row["log"]["agency"], "component": row["component"],
            "disposition": row["disposition"], "received_date": row["received_date"],
            "closed_date": row["closed_date"], "closure_term": closure["term_id"] if closure else None,
            "closure_cohort_eligible": bool(closure and closure["cohort_eligible"]),
            "outcome_known": row["disposition"] is not None and row["closed_date"] is not None,
            "log_sha256": row["log"]["sha256"],
        })
    body = {
        "contract_version": contract.LOG_ROW_VERSION, "rows": len(rows),
        "counts": {"joined": len(joined), "unjoined": len(unjoined)},
        "joined": joined, "unjoined": unjoined, "request_outcomes": outcomes,
        "request_outcome_estimand": ("available only for logs with known disposition and closure; "
                                     "release-content models never use these rows"),
        "contract_gaps": ["annual FOIA statistics are not exported by FOIA_Aggregator"],
    }
    if len(joined) + len(unjoined) != len(rows):
        raise ContextError("context census does not reconcile")
    return dict(body, report_sha256=hashlib.sha256(canonical(body).encode()).hexdigest())
