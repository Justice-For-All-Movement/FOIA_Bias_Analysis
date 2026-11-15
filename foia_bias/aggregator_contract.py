"""FOIA_Aggregator export contracts, consumer side (single source of truth).

Pinned upstream
  repository  Justice-For-All-Movement/FOIA_Aggregator
  commit      7e75ab90ded8713fe47b59a92a4b3b87d2181c8c (main after PR #75 export
              extensions; FC-13 record schema byte-identical since ca822fa, PR #72)
  contracts   foia-catalog-export/1        (catalog records; contract_revision 1.1 manifests)
              foia-document-text-export/1  (document text, joined by record_id)
              foia-request-log-export/1    (FC-03 request-log rows)
  schemas     foia_bias/schemas/*.schema.json, vendored verbatim, SHA-256 pinned below
  snapshots   ``export-catalog <dir>``; ``export-dataset document-text|request-log-rows <dir>``

Manifests name the producer commit (``producer.commit``). Live exports with a
null/unpinned producer commit are refused; fixtures are marked by FIXTURE.json.

Layers: (1) FC-13 catalog validation; (2) extension dataset validation
(document text, request-log rows); (3) ``to_analysis_record`` adapter into
Bias's internal analysis record; (4) internal record validation.

Remaining FC-13 gaps: no jurisdiction field (``us-federal`` assumed per the
federal-release note), no page offsets in text, no annual statistics export.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

AGGREGATOR_REPOSITORY = "Justice-For-All-Movement/FOIA_Aggregator"
AGGREGATOR_CONTRACT_COMMIT = "7e75ab90ded8713fe47b59a92a4b3b87d2181c8c"
CATALOG_SCHEMA_INTRODUCED = "ca822fad6bc089a48ede6d89d94cdad47967b11f"
CONTRACT_VERSION = "foia-catalog-export/1"
TEXT_VERSION = "foia-document-text-export/1"
LOG_ROW_VERSION = "foia-request-log-export/1"
EXTENSION_SCHEMAS = {
    TEXT_VERSION: ("document-text.v1.schema.json", "777e048245a11b01ee43a5e00e912374e9dff694218bb8381a89feabbc4a1218",
                   "document-text", "record_id"),
    LOG_ROW_VERSION: ("request-log-row.v1.schema.json",
                      "1e939ce1d023c0fa9aa06e98b62ec9d41086d0acb395f9aa7bb5b9b267c416ce", "request-log-rows", "row_id"),
}
SUPPORTED_CONTRACT_VERSIONS = (CONTRACT_VERSION,)
SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "catalog-record.v1.schema.json"
SCHEMA_SHA256 = "1e8abc956b31db20d4855b2c73fb785696f6f0c28c437b91c1fc83c9bd039f34"
LIVE_PRODUCER = "FOIA_Aggregator"
FIXTURE_PRODUCER = "synthetic-fixture"
FIXTURE_MARKER = "FIXTURE.json"
ASSUMED_JURISDICTION = "us-federal"
INDEXED_STATUSES = ("indexed", "indexed_truncated")
MANIFEST_REQUIRED = {"schema_version", "snapshot_id", "generated_at", "counts", "excluded_fields", "files"}
MANIFEST_OPTIONAL = {"schema", "pagination", "license", "contract_revision", "producer"}
COUNT_FIELDS = {"records", "archived", "metadata_only", "text_indexed", "agencies", "sources"}
REQUIRED_EXCLUSIONS = {"requester_name", "storage_key", "local_path"}
MAX_FILES = 100000

# Internal analysis-record vocabulary (Bias-owned).
RECORD_TYPES = ("release", "request_log_row", "annual_report", "request_description")
CONTENT_RECORD_TYPES = ("release",)
SOURCE_FAMILIES = ("agency_reading_room", "third_party_release", "mixed_official_third_party", "muckrock",
                   "agency_log", "foia_gov_annual", "other_release_collection")
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

_HEX64 = re.compile(r"[0-9a-f]{64}")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._/-]{0,199}")


class ContractError(ValueError):
    pass


def _require(condition, message):
    if not condition:
        raise ContractError(message)


# -- FC-13 layer -------------------------------------------------------------
def load_schema():
    raw = SCHEMA_PATH.read_bytes()
    _require(hashlib.sha256(raw).hexdigest() == SCHEMA_SHA256,
             "vendored FC-13 schema does not match the pinned SHA-256")
    return json.loads(raw.decode("utf-8"))


_SCHEMA_CACHE = {}


def _schema():
    if "s" not in _SCHEMA_CACHE:
        _SCHEMA_CACHE["s"] = load_schema()
    return _SCHEMA_CACHE["s"]


_TYPES = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool, "null": type(None),
          "number": (int, float)}


def schema_validate(instance, schema, path="$"):
    """JSON Schema subset used by FC-13/1: type, required, properties,
    additionalProperties=false, items, enum, pattern."""
    types = schema.get("type")
    if types is not None:
        allowed = types if isinstance(types, list) else [types]
        _require(any(isinstance(instance, _TYPES[t]) and not (t in ("integer", "number") and isinstance(instance, bool))
                     for t in allowed), f"{path}: expected {allowed}")
    if isinstance(instance, dict):
        for key in schema.get("required", []):
            _require(key in instance, f"{path}: missing {key}")
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extra = set(instance) - set(props)
            _require(not extra, f"{path}: unexpected fields {sorted(extra)}")
        for key, value in instance.items():
            if key in props:
                schema_validate(value, props[key], f"{path}.{key}")
    if isinstance(instance, list) and "items" in schema:
        for i, item in enumerate(instance):
            schema_validate(item, schema["items"], f"{path}[{i}]")
    if isinstance(instance, str):
        if "enum" in schema:
            _require(instance in schema["enum"], f"{path}: {instance!r} not allowed")
        if "pattern" in schema:
            _require(re.search(schema["pattern"], instance) is not None, f"{path}: pattern mismatch")


def normalize_url(url: str) -> str:
    """Same normalization as FOIA_Aggregator ``source_inventory.normalize_url``."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower() or "https"
    host = (parts.hostname or "").lower()
    if parts.port and (scheme, parts.port) not in (("https", 443), ("http", 80)):
        host = f"{host}:{parts.port}"
    return urlunsplit((scheme, host, parts.path or "/", parts.query, ""))


def record_id_for(url: str) -> str:
    return "rec_" + hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()[:24]


def validate_catalog_record(record: dict) -> dict:
    schema_validate(record, _schema())
    _require(record["record_id"] == record_id_for(record["url"]), "record_id does not derive from url")
    pres = record["preservation"]
    archived = pres["status"] == "archived"
    _require(archived == (pres["sha256"] is not None), "sha256 present iff archived")
    if not archived:
        _require(pres["file_size"] is None and pres["archived_at"] is None, "metadata-only record has file data")
    text = record["text"]
    _require(text["indexed"] == (text["extraction_status"] in INDEXED_STATUSES), "text.indexed inconsistent")
    if record["published_year"] is not None:
        _require(isinstance(record["published_date"], str) and record["published_date"][:4]
                 == str(record["published_year"]), "published_year inconsistent with published_date")
    return record


def validate_catalog_manifest(manifest: dict) -> dict:
    _require(isinstance(manifest, dict) and MANIFEST_REQUIRED <= set(manifest), "FC-13 manifest fields missing")
    _require(manifest["schema_version"] in SUPPORTED_CONTRACT_VERSIONS, "unsupported FC-13 schema_version")
    _require(isinstance(manifest["snapshot_id"], str) and re.fullmatch(r"snap_[0-9a-f]{20}", manifest["snapshot_id"]),
             "snapshot_id invalid")
    try:
        datetime.fromisoformat(str(manifest["generated_at"]).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError("generated_at invalid") from exc
    counts = manifest["counts"]
    _require(isinstance(counts, dict) and COUNT_FIELDS <= set(counts)
             and all(type(counts[k]) is int and counts[k] >= 0 for k in COUNT_FIELDS), "counts invalid")
    _require(counts["archived"] + counts["metadata_only"] == counts["records"], "counts do not reconcile")
    _require(isinstance(manifest["excluded_fields"], list)
             and REQUIRED_EXCLUSIONS <= set(manifest["excluded_fields"]), "privacy exclusions missing")
    files = manifest["files"]
    _require(isinstance(files, list) and len(files) <= MAX_FILES, "files must be a list")
    seen, total = set(), 0
    for entry in files:
        _require(isinstance(entry, dict) and set(entry) == {"path", "records", "sha256"}, "file entry invalid")
        _require(isinstance(entry["path"], str) and re.fullmatch(r"records-\d{5}\.jsonl", entry["path"])
                 and entry["path"] not in seen, "file path must be records-NNNNN.jsonl, unique")
        seen.add(entry["path"])
        _require(isinstance(entry["sha256"], str) and _HEX64.fullmatch(entry["sha256"]), "file sha256 invalid")
        _require(type(entry["records"]) is int and entry["records"] >= 0, "file record count invalid")
        total += entry["records"]
    _require(total == counts["records"], "file record counts do not sum to counts.records")
    return manifest


def producer_commit(manifest: dict, fixture: bool):
    """The manifest's 40-hex producer commit; live exports without one are refused."""
    producer = manifest.get("producer")
    commit = producer.get("commit") if isinstance(producer, dict) else None
    if isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit):
        return commit
    _require(fixture, "live export manifest does not name a pinned producer commit")
    return "synthetic"


def _extension_schema(version):
    name, digest, _, _ = EXTENSION_SCHEMAS[version]
    key = "ext:" + version
    if key not in _SCHEMA_CACHE:
        raw = (SCHEMA_PATH.parent / name).read_bytes()
        _require(hashlib.sha256(raw).hexdigest() == digest, f"vendored {name} does not match the pinned SHA-256")
        _SCHEMA_CACHE[key] = json.loads(raw.decode("utf-8"))
    return _SCHEMA_CACHE[key]


def validate_dataset_manifest(manifest: dict, version: str) -> dict:
    _require(version in EXTENSION_SCHEMAS, "unknown extension dataset")
    _, _, dataset, id_field = EXTENSION_SCHEMAS[version]
    _require(isinstance(manifest, dict) and {"schema_version", "dataset", "snapshot_id", "generated_at", "producer",
                                             "id_field", "counts", "files", "excluded_fields"} <= set(manifest),
             "dataset manifest fields missing")
    _require(manifest["schema_version"] == version and manifest["dataset"] == dataset
             and manifest["id_field"] == id_field, "dataset manifest version/dataset mismatch")
    _require(isinstance(manifest["snapshot_id"], str) and manifest["snapshot_id"], "snapshot_id required")
    files, total, seen = manifest["files"], 0, set()
    _require(isinstance(files, list), "files must be a list")
    for entry in files:
        _require(isinstance(entry, dict) and set(entry) == {"path", "records", "sha256"}
                 and re.fullmatch(re.escape(dataset) + r"-\d{5}\.jsonl", str(entry["path"])) is not None
                 and entry["path"] not in seen and _HEX64.fullmatch(str(entry["sha256"])) is not None
                 and type(entry["records"]) is int and entry["records"] >= 0, "dataset file entry invalid")
        seen.add(entry["path"])
        total += entry["records"]
    _require(manifest["counts"].get("records") == total, "dataset file counts do not sum to counts.records")
    if version == LOG_ROW_VERSION:
        _require({"requester_name", "requester_organization", "raw_cells"} <= set(manifest["excluded_fields"]),
                 "request-log export must exclude personal fields and raw cells")
    return manifest


def validate_text_record(record: dict) -> dict:
    schema_validate(record, _extension_schema(TEXT_VERSION))
    _require(hashlib.sha256(record["text"].encode("utf-8")).hexdigest() == record["text_sha256"],
             "text_sha256 does not match exported text")
    if record["character_count"] is not None:
        _require(record["character_count"] == len(record["text"]), "character_count does not match text")
    _require(record["truncated"] or record["extraction"]["status"] != "indexed_truncated",
             "truncated extraction must set truncated")
    _require(record["record_id"] == record_id_for(record["url"]), "record_id does not derive from url")
    return record


def validate_log_row(row: dict) -> dict:
    schema_validate(row, _extension_schema(LOG_ROW_VERSION))
    for name in ("received_date", "closed_date"):
        if row[name] is not None:
            try:
                datetime.strptime(row[name], "%Y-%m-%d")
            except ValueError as exc:
                raise ContractError(f"{name} is not a real date") from exc
    return row


def snapshot_info(manifest: dict, manifest_sha256: str, fixture: bool) -> dict:
    """Normalized snapshot identity stored in the analysis ledger."""
    validate_catalog_manifest(manifest)
    return {"snapshot_id": manifest["snapshot_id"], "contract_version": manifest["schema_version"],
            "producer_name": FIXTURE_PRODUCER if fixture else LIVE_PRODUCER,
            "producer_commit": producer_commit(manifest, fixture),
            "manifest_sha256": manifest_sha256, "observed_at": manifest["generated_at"],
            "fixture_mode": fixture, "total_records": manifest["counts"]["records"]}


def _date_entry(published):
    if not isinstance(published, str) or not published:
        return {"value": None, "precision": "unknown"}
    for pattern, precision, fmt in ((r"\d{4}-\d{2}-\d{2}", "day", "%Y-%m-%d"), (r"\d{4}-\d{2}", "month", "%Y-%m"),
                                    (r"\d{4}", "year", "%Y")):
        if re.fullmatch(pattern, published):
            try:
                datetime.strptime(published, fmt)
            except ValueError:
                break
            return {"value": published, "precision": precision}
    return {"value": None, "precision": "unknown"}  # unparseable dates are never guessed


def source_family(record: dict) -> str:
    kinds = {s["provenance"] for s in record["sources"]}
    if kinds == {"third_party"}:
        return "third_party_release"
    if kinds == {"official", "third_party"}:
        return "mixed_official_third_party"
    return "agency_reading_room"


def to_analysis_record(record: dict, *, fixture: bool, text: str | None = None) -> dict:
    """Map a validated FC-13 catalog record (plus its document-text export entry, if any)."""
    validate_catalog_record(record)
    pres, tinfo = record["preservation"], record["text"]
    archived = pres["status"] == "archived"
    status = (tinfo["extraction_status"] or "").lower()
    if not archived:
        text_status = "waiting"
    elif tinfo["indexed"] and (tinfo["truncated"] or status == "indexed_truncated"):
        text_status = "partial"
    elif tinfo["indexed"]:
        text_status = "extracted" if text is not None else "waiting"
    elif status.startswith("unsupported"):
        text_status = "unsupported"
    elif status in ("failed", "error") or status.startswith("failed"):
        text_status = "failed"
    else:
        text_status = "waiting"
    content = text if text_status in ("extracted", "partial") and text is not None else None
    if text_status == "partial" and content is None:
        text_status = "waiting"
    if content is not None and tinfo["character_count"] is not None:
        _require(len(content) == tinfo["character_count"], "supplied text length disagrees with FC-13 character_count")
    sources = record["sources"] or [{"source_id": "document_url", "url": record["url"], "provenance": "official"}]
    return {
        "record_id": record["record_id"], "revision": 1, "record_type": "release",
        "source_family": source_family(record), "jurisdiction": ASSUMED_JURISDICTION,
        "agency": (record["agency"] or {}).get("slug") or "unknown",
        "component": (record["office"] or {}).get("slug"),
        "provenance": [{"source_id": s["source_id"], "source_url": s["url"], "retrieved_at": None} for s in sources],
        "original_sha256": pres["sha256"],
        "dates": {"release_date": _date_entry(record["published_date"])},
        "archive_state": "preserved" if archived else "remote_only",
        "text": {"status": text_status,
                 "text_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest() if content is not None else None,
                 "text_revision": 1 if content is not None else None,
                 "extractor": tinfo["extraction_method"] if content is not None else None,
                 "char_count": len(content) if content is not None else tinfo["character_count"],
                 "content": content},
        "relations": [], "synthetic": fixture,
    }


# -- internal analysis record layer -------------------------------------------
def _utc(value, name):
    _require(isinstance(value, str) and value.endswith("Z"), f"{name} must be UTC ...Z")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{name} must be RFC3339 UTC") from exc


def _hex_or_null(value, name):
    _require(value is None or (isinstance(value, str) and _HEX64.fullmatch(value)),
             f"{name} must be lowercase SHA-256 or null")


def validate_record(record: dict, fixture: bool) -> dict:
    """Validate a Bias-internal analysis record."""
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
    return record["record_type"] == "release" and record["text"]["status"] == "extracted"
