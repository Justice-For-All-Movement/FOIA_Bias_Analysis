"""Consumer-side FOIA_Aggregator export contract (single source of truth).

FOIA_Aggregator's FC-13 export contract is being defined concurrently. Until a
producer commit and schema are published, Bias pins this *consumer draft*
(``fc13-consumer-draft-1``). Every field name, enum and invariant that Bias
relies on lives in this module so alignment with the real FC-13 schema is a
one-module change plus fixture regeneration. Nothing here contacts the
Aggregator; it validates local export files only.

Manifest (``manifest.json``)::

    contract            "foia-aggregator-export"
    contract_version    CONTRACT_VERSION
    snapshot_id         stable export/snapshot identifier
    producer            {"name": "FOIA_Aggregator" | "synthetic-fixture", "commit": 40-hex | "synthetic"}
    observed_at         UTC RFC3339 "...Z"
    fixture_mode        bool (must equal producer == synthetic-fixture)
    partitions          [{"partition_id", "path", "sha256", "count"}]  (ordered, unique ids)
    totals              {"records": int}  == sum(partition counts)
    coverage            {"source_families": {family: count}, "record_types": {type: count}}
    unresolved_failures [{"record_id"|null, "reason"}]

Record (one JSON object per JSONL line)::

    record_id, revision, record_type, source_family, jurisdiction, agency, component,
    provenance [{"source_id", "source_url", "retrieved_at"}], original_sha256,
    dates {name: {"value", "precision"}}, archive_state, text {...}, relations [...], synthetic
"""
from __future__ import annotations

import re
from datetime import datetime

CONTRACT_NAME = "foia-aggregator-export"
CONTRACT_VERSION = "fc13-consumer-draft-1"
SUPPORTED_CONTRACT_VERSIONS = (CONTRACT_VERSION,)
LIVE_PRODUCER = "FOIA_Aggregator"
FIXTURE_PRODUCER = "synthetic-fixture"

RECORD_TYPES = ("release", "request_log_row", "annual_report", "request_description")
# Only release content can enter document labeling/regressions. Logs, annual
# totals and request descriptions are context (BR-07).
CONTENT_RECORD_TYPES = ("release",)
SOURCE_FAMILIES = ("muckrock", "agency_reading_room", "agency_log", "foia_gov_annual",
                   "other_release_collection")
ARCHIVE_STATES = ("preserved", "remote_only")
TEXT_STATUSES = ("extracted", "partial", "unsupported", "failed", "waiting", "not_applicable")
DATE_FIELDS = ("decision_date", "release_date", "closed_date", "submitted_date", "received_date",
               "period_start", "period_end")
DATE_PRECISIONS = ("exact", "day", "month", "year", "unknown")
RELATION_KINDS = ("duplicate_of", "redaction_variant_of", "release_of_request", "log_row_of_request",
                  "revision_of")
RECORD_FIELDS = {"record_id", "revision", "record_type", "source_family", "jurisdiction", "agency",
                 "component", "provenance", "original_sha256", "dates", "archive_state", "text",
                 "relations", "synthetic"}
TEXT_FIELDS = {"status", "text_sha256", "text_revision", "extractor", "char_count", "content"}
MANIFEST_FIELDS = {"contract", "contract_version", "snapshot_id", "producer", "observed_at", "fixture_mode",
                   "partitions", "totals", "coverage", "unresolved_failures"}
MAX_PARTITIONS = 100000

_HEX64 = re.compile(r"[0-9a-f]{64}")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._/-]{0,199}")


class ContractError(ValueError):
    pass


def _require(condition, message):
    if not condition:
        raise ContractError(message)


def _utc(value, name):
    _require(isinstance(value, str) and value.endswith("Z"), f"{name} must be UTC ...Z")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{name} must be RFC3339 UTC") from exc


def _hex_or_null(value, name):
    _require(value is None or (isinstance(value, str) and _HEX64.fullmatch(value)),
             f"{name} must be lowercase SHA-256 or null")


def validate_manifest(manifest: dict, expected_fixture: bool | None = None) -> dict:
    _require(isinstance(manifest, dict) and set(manifest) == MANIFEST_FIELDS,
             "manifest fields must match " + CONTRACT_VERSION)
    _require(manifest["contract"] == CONTRACT_NAME, "not a FOIA_Aggregator export manifest")
    _require(manifest["contract_version"] in SUPPORTED_CONTRACT_VERSIONS,
             "unsupported export contract version")
    _require(isinstance(manifest["snapshot_id"], str) and _ID.fullmatch(manifest["snapshot_id"]),
             "snapshot_id invalid")
    producer = manifest["producer"]
    _require(isinstance(producer, dict) and set(producer) == {"name", "commit"}, "producer invalid")
    fixture = manifest["fixture_mode"]
    _require(type(fixture) is bool, "fixture_mode must be boolean")
    if expected_fixture is not None:
        _require(fixture == expected_fixture, "fixture mode mismatch")
    if fixture:
        _require(producer == {"name": FIXTURE_PRODUCER, "commit": "synthetic"}, "fixture producer mismatch")
    else:
        _require(producer["name"] == LIVE_PRODUCER and isinstance(producer["commit"], str)
                 and re.fullmatch(r"[0-9a-f]{40}", producer["commit"]) is not None,
                 "live export must pin a FOIA_Aggregator producer commit")
    _utc(manifest["observed_at"], "observed_at")
    partitions = manifest["partitions"]
    _require(isinstance(partitions, list) and 0 < len(partitions) <= MAX_PARTITIONS, "partitions required")
    seen = set()
    total = 0
    for part in partitions:
        _require(isinstance(part, dict) and set(part) == {"partition_id", "path", "sha256", "count"},
                 "partition fields invalid")
        _require(isinstance(part["partition_id"], str) and _ID.fullmatch(part["partition_id"])
                 and part["partition_id"] not in seen, "partition ids must be unique")
        seen.add(part["partition_id"])
        _require(isinstance(part["sha256"], str) and _HEX64.fullmatch(part["sha256"]),
                 "partition sha256 invalid")
        _require(type(part["count"]) is int and part["count"] >= 0, "partition count invalid")
        _require(isinstance(part["path"], str), "partition path invalid")
        total += part["count"]
    totals = manifest["totals"]
    _require(isinstance(totals, dict) and set(totals) == {"records"} and totals["records"] == total,
             "manifest totals disagree with partition counts")
    coverage = manifest["coverage"]
    _require(isinstance(coverage, dict) and set(coverage) == {"source_families", "record_types"},
             "coverage fields invalid")
    for key, allowed in (("source_families", SOURCE_FAMILIES), ("record_types", RECORD_TYPES)):
        counts = coverage[key]
        _require(isinstance(counts, dict) and set(counts) <= set(allowed)
                 and all(type(v) is int and v >= 0 for v in counts.values())
                 and sum(counts.values()) == total, f"coverage.{key} must reconcile to totals")
    failures = manifest["unresolved_failures"]
    _require(isinstance(failures, list) and all(
        isinstance(f, dict) and set(f) == {"record_id", "reason"} and isinstance(f["reason"], str)
        for f in failures), "unresolved_failures invalid")
    return manifest


def validate_record(record: dict, fixture: bool) -> dict:
    _require(isinstance(record, dict) and set(record) == RECORD_FIELDS, "record fields must match contract")
    _require(type(record["synthetic"]) is bool and record["synthetic"] == fixture,
             "synthetic record cannot cross fixture/live boundary")
    _require(isinstance(record["record_id"], str) and _ID.fullmatch(record["record_id"]), "record_id invalid")
    _require(type(record["revision"]) is int and record["revision"] >= 1, "revision must be >= 1")
    _require(record["record_type"] in RECORD_TYPES, "unknown record_type")
    _require(record["source_family"] in SOURCE_FAMILIES, "unknown source_family")
    _require(isinstance(record["jurisdiction"], str) and record["jurisdiction"], "jurisdiction required")
    _require(isinstance(record["agency"], str) and record["agency"], "agency required")
    _require(record["component"] is None or isinstance(record["component"], str), "component invalid")
    provenance = record["provenance"]
    _require(isinstance(provenance, list) and provenance, "at least one provenance entry required")
    for entry in provenance:
        _require(isinstance(entry, dict) and set(entry) == {"source_id", "source_url", "retrieved_at"}
                 and isinstance(entry["source_id"], str) and entry["source_id"], "provenance entry invalid")
        _require(entry["source_url"] is None or isinstance(entry["source_url"], str), "source_url invalid")
        if entry["retrieved_at"] is not None:
            _utc(entry["retrieved_at"], "retrieved_at")
    _hex_or_null(record["original_sha256"], "original_sha256")
    dates = record["dates"]
    _require(isinstance(dates, dict) and set(dates) <= set(DATE_FIELDS), "unknown date field")
    for name, value in dates.items():
        _require(isinstance(value, dict) and set(value) == {"value", "precision"}
                 and value["precision"] in DATE_PRECISIONS
                 and (value["value"] is None or isinstance(value["value"], str)), f"date {name} invalid")
        _require((value["value"] is None) == (value["precision"] == "unknown"),
                 f"date {name}: unknown precision iff null value")
    _require(record["archive_state"] in ARCHIVE_STATES, "unknown archive_state")
    text = record["text"]
    _require(isinstance(text, dict) and set(text) == TEXT_FIELDS, "text fields invalid")
    _require(text["status"] in TEXT_STATUSES, "unknown text status")
    _hex_or_null(text["text_sha256"], "text_sha256")
    has_text = text["status"] in ("extracted", "partial")
    _require(has_text == (text["text_sha256"] is not None), "text hash present iff text extracted/partial")
    _require(text["content"] is None or isinstance(text["content"], str), "text content invalid")
    _require(text["char_count"] is None or (type(text["char_count"]) is int and text["char_count"] >= 0),
             "char_count invalid")
    _require(text["text_revision"] is None or (type(text["text_revision"]) is int and text["text_revision"] >= 1),
             "text_revision invalid")
    if record["record_type"] != "release":
        _require(text["status"] == "not_applicable", "only releases carry document text")
    if record["archive_state"] == "remote_only":
        _require(not has_text, "remote-only records cannot carry extracted text")
    relations = record["relations"]
    _require(isinstance(relations, list), "relations must be a list")
    for rel in relations:
        _require(isinstance(rel, dict) and set(rel) == {"kind", "record_id"} and rel["kind"] in RELATION_KINDS
                 and isinstance(rel["record_id"], str) and rel["record_id"] != record["record_id"],
                 "relation invalid")
    return record


def is_content_record(record: dict) -> bool:
    return record["record_type"] in CONTENT_RECORD_TYPES


def text_ready(record: dict) -> bool:
    """Only fully extracted text can produce a successful label."""
    return record["record_type"] == "release" and record["text"]["status"] == "extracted"
