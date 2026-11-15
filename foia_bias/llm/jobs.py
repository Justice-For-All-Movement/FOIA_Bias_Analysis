"""FBA-10 resumable labeling jobs, usage controls and full-census accounting.

Jobs are the durable ``labels`` rows keyed by ``(record_id, text_sha256,
labeler_key)``, where the key covers provider, model, prompt, schema and
chunking. Per record:

1. an ``in_flight`` marker is committed *before* any provider call;
2. the outcome (labeled / failed state) is committed after the call.

On resume, an ``in_flight`` row means the process died after a provider call
may have been billed. It becomes ``billing_ambiguous`` and is **not** retried
unless the operator explicitly passes ``retry_ambiguous=True``, so a crash never
silently double-bills. Transient provider errors retry up to ``max_retries``.
Provider calls run on a bounded thread pool; all ledger writes happen on the
calling thread. ``max_calls`` is a per-run usage ceiling (remaining records
are ``budget_blocked``). With no provider configured, labeling pauses
(``paused_no_provider``) and the census is still reported. Every snapshot
record lands in exactly one census bucket.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from foia_bias.llm.labeler import (ChunkConfig, check_capabilities, chunk_spans, label_document, labeler_key,
                                   not_labelable_reason)
from foia_bias.text_completeness import verify_text

TERMINAL = ("labeled", "invalid_output", "out_of_range", "conflicting_parties", "refused", "failed",
            "text_hash_mismatch")


def _analysis_record(ledger, snapshot_id, row, texts):
    record = ledger.export_record(snapshot_id, row["record_id"])
    text = texts.get(row["record_id"])
    if text is not None and row["text_sha256"]:
        verify_text(row, text)
        record["text"]["content"] = text
    return record


def run_labeling(ledger, snapshot_id, provider, *, texts, config: ChunkConfig = ChunkConfig(), max_calls=None,
                 max_retries=2, workers=2, retry_ambiguous=False, run_id=None, hooks=None):
    hooks = hooks or {}
    rows = ledger.records(snapshot_id)
    census = {}

    def count(bucket):
        census[bucket] = census.get(bucket, 0) + 1

    if provider is None:
        for row in rows:
            reason = _row_reason(row)
            count("not_labeled:" + reason if reason else "paused_no_provider")
        return {"status": "paused_no_provider", "census": dict(sorted(census.items())), "calls": 0,
                "records": len(rows)}
    if not 1 <= workers <= 16:
        raise ValueError("workers must be 1..16")
    check_capabilities(provider, config)
    key = labeler_key(provider, config)
    pending, calls = [], 0
    for row in rows:
        reason = _row_reason(row)
        if reason:
            count("not_labeled:" + reason)
            continue
        if row["text_sha256"] is None or row["record_id"] not in texts:
            count("not_labeled:waiting_upstream_text")
            continue
        existing = ledger.get_label(row["record_id"], row["text_sha256"], key)
        status = existing["status"] if existing else None
        if status in TERMINAL:
            count(status)
            continue
        if status == "in_flight":
            ledger.put_label(row["record_id"], row["text_sha256"], key, "billing_ambiguous",
                             {"reason": "process stopped after a provider call may have been billed"}, run_id)
            status = "billing_ambiguous"
        if status == "billing_ambiguous" and not retry_ambiguous:
            count("billing_ambiguous")
            continue
        pending.append(row)
    batch = []
    for row in pending:
        estimate = len(chunk_spans(row["char_count"] or 0, config))
        if max_calls is not None and calls + estimate > max_calls:
            ledger.put_label(row["record_id"], row["text_sha256"], key, "budget_blocked", None, run_id)
            count("budget_blocked")
            continue
        calls += estimate
        ledger.put_label(row["record_id"], row["text_sha256"], key, "in_flight", None, run_id)  # committed first
        batch.append(row)

    records = {row["record_id"]: _analysis_record(ledger, snapshot_id, row, texts) for row in batch}

    def call(row):
        attempts = 0
        while True:
            attempts += 1
            outcome = label_document(records[row["record_id"]], provider, config)
            if outcome["state"] != "provider_error" or attempts > max_retries:
                return row, outcome, attempts

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for row, outcome, attempts in pool.map(call, batch):
            if "after_provider" in hooks:
                hooks["after_provider"](row["record_id"])
            state = "failed" if outcome["state"] == "provider_error" else outcome["state"]
            result = outcome.get("label") or {"reason": outcome.get("reason")}
            result.update(usage=outcome.get("usage"), attempts=attempts)
            ledger.put_label(row["record_id"], row["text_sha256"], key, state, result, run_id)
            count(state)
    total = sum(census.values())
    if total != len(rows):
        raise AssertionError("labeling census does not reconcile with the snapshot")
    return {"status": "complete" if "budget_blocked" not in census else "paused_budget",
            "labeler_key": key, "census": dict(sorted(census.items())), "calls_estimated": calls,
            "records": len(rows)}


def _row_reason(row):
    shaped = {"record_type": row["record_type"], "archive_state": row["archive_state"],
              "text": {"status": row["text_status"]}}
    return not_labelable_reason(shaped)
