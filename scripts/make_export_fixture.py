"""Regenerate the synthetic multi-source, multi-partition export fixture.

Deterministic: running it twice yields byte-identical files. All content is
synthetic and labelled as such. Markers like ``[[signal ...]]`` drive the
deterministic fake labeling provider in tests; they are not real findings.

Usage: python scripts/make_export_fixture.py [output_dir]
"""
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from foia_bias.aggregator_contract import (CONTRACT_NAME, CONTRACT_VERSION,  # noqa: E402
                                           FIXTURE_PRODUCER, validate_manifest, validate_record)

FILLER = "Synthetic routine administrative text with no partisan content. "


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def day(value):
    return {"value": value, "precision": "day"}


def release(record_id, family, agency, decision, text=None, status="extracted", archive="preserved",
            original=None, relations=(), jurisdiction="us-federal", precision="day"):
    content = text if status in ("extracted", "partial") else None
    dates = {"decision_date": {"value": decision, "precision": precision if decision else "unknown"}}
    return {
        "record_id": record_id, "revision": 1, "record_type": "release", "source_family": family,
        "jurisdiction": jurisdiction, "agency": agency, "component": None,
        "provenance": [{"source_id": f"{family}:{record_id}", "source_url": None,
                        "retrieved_at": "2026-11-01T00:00:00Z"}],
        "original_sha256": original or sha("original:" + record_id), "dates": dates,
        "archive_state": archive,
        "text": {"status": status, "text_sha256": sha(content) if content is not None else None,
                 "text_revision": 1 if content is not None else None,
                 "extractor": "synthetic-extractor/1" if content is not None else None,
                 "char_count": len(content) if content is not None else None, "content": content},
        "relations": [{"kind": k, "record_id": r} for k, r in relations], "synthetic": True,
    }


def context(record_id, record_type, family, agency, dates, relations=()):
    return {
        "record_id": record_id, "revision": 1, "record_type": record_type, "source_family": family,
        "jurisdiction": "us-federal", "agency": agency, "component": None,
        "provenance": [{"source_id": f"{family}:{record_id}", "source_url": None, "retrieved_at": None}],
        "original_sha256": sha("original:" + record_id), "dates": dates, "archive_state": "preserved",
        "text": {"status": "not_applicable", "text_sha256": None, "text_revision": None, "extractor": None,
                 "char_count": None, "content": None},
        "relations": [{"kind": k, "record_id": r} for k, r in relations], "synthetic": True,
    }


def records():
    shared = "SYNTHETIC FIXTURE memo. [[signal party=R wrongdoing=0.8 favorability=-0.6]] " + FILLER * 3
    long_text = "SYNTHETIC FIXTURE long release. " + FILLER * 400 + \
        " [[signal party=D wrongdoing=0.9 favorability=-0.7]] Late decisive passage."
    assert long_text.index("[[signal") > 20000
    redacted = "SYNTHETIC FIXTURE briefing. [[signal party=D wrongdoing=0.1 favorability=0.5]] " + FILLER
    return [
        release("agg:rr-001", "agency_reading_room", "DOJ", "2003-05-01", shared),
        release("agg:mr-001", "muckrock", "DOJ", "2003-05-01", shared, original=sha("original:agg:rr-001"),
                relations=[("duplicate_of", "agg:rr-001")]),
        release("agg:oc-001", "other_release_collection", "DHS", "2014-08-12", long_text),
        release("agg:rr-002", "agency_reading_room", "EPA", "2018-03-01", redacted),
        release("agg:mr-002", "muckrock", "EPA", "2018-03-03", redacted.replace("briefing", "briefing [b6]"),
                relations=[("redaction_variant_of", "agg:rr-002")]),
        release("agg:rr-003", "agency_reading_room", "DOJ", None, "SYNTHETIC FIXTURE undated. " + FILLER),
        release("agg:mr-003", "muckrock", "FBI", "2011-02-02", status="waiting", archive="remote_only"),
        release("agg:rr-004", "agency_reading_room", "DOD", "2006-07-07", status="failed"),
        release("agg:rr-005", "agency_reading_room", "DOD", "2007-07-07",
                "SYNTHETIC FIXTURE partial OCR. " + FILLER, status="partial"),
        context("agg:log-001", "request_log_row", "agency_log", "DOJ",
                {"submitted_date": day("2020-12-01"), "closed_date": day("2021-02-15")},
                relations=[("log_row_of_request", "agg:desc-001")]),
        context("agg:annual-001", "annual_report", "foia_gov_annual", "DOJ",
                {"period_start": day("2018-10-01"), "period_end": day("2019-09-30")}),
        context("agg:desc-001", "request_description", "muckrock", "DOJ", {"submitted_date": day("2020-12-01")}),
        release("agg:rr-006", "agency_reading_room", "State", "2021-01-20",
                "SYNTHETIC FIXTURE inauguration-day release. [[signal party=R wrongdoing=0.2 favorability=0.1]]"),
        release("agg:rr-007", "agency_reading_room", "State", "2025-03-15",
                "SYNTHETIC FIXTURE ongoing-term release. " + FILLER),
        release("agg:st-001", "other_release_collection", "California DOJ", "2010-06-01",
                "SYNTHETIC FIXTURE state release. " + FILLER, jurisdiction="us-state-ca"),
    ]


def build(out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = records()
    for row in rows:
        validate_record(row, True)
    partitions, families, types = [], {}, {}
    for index in range(0, len(rows), 5):
        chunk = rows[index:index + 5]
        name = f"part-{index // 5 + 1:04d}.jsonl"
        data = "".join(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n" for r in chunk)
        (out_dir / name).write_bytes(data.encode("utf-8"))
        partitions.append({"partition_id": f"p{index // 5 + 1:04d}", "path": name,
                           "sha256": hashlib.sha256(data.encode("utf-8")).hexdigest(), "count": len(chunk)})
        for r in chunk:
            families[r["source_family"]] = families.get(r["source_family"], 0) + 1
            types[r["record_type"]] = types.get(r["record_type"], 0) + 1
    manifest = {
        "contract": CONTRACT_NAME, "contract_version": CONTRACT_VERSION,
        "snapshot_id": "synthetic-export-v1", "producer": {"name": FIXTURE_PRODUCER, "commit": "synthetic"},
        "observed_at": "2026-11-15T00:00:00Z", "fixture_mode": True, "partitions": partitions,
        "totals": {"records": len(rows)},
        "coverage": {"source_families": dict(sorted(families.items())), "record_types": dict(sorted(types.items()))},
        "unresolved_failures": [{"record_id": "agg:rr-004", "reason": "synthetic extraction failure"}],
    }
    validate_manifest(manifest)
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


if __name__ == "__main__":
    build(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "fixtures" / "export_v1")
