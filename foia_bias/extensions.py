"""Loaders for FOIA_Aggregator extension exports (pinned in ``aggregator_contract``).

``load_dataset`` verifies an ``export-dataset`` snapshot directory: manifest
(version, dataset, producer commit for live data, file list), per-file
SHA-256 and counts, every record against its vendored schema, and unique IDs.
Fixture directories carry FIXTURE.json; live ones must not.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from foia_bias import aggregator_contract as contract
from foia_bias.corpus import MAX_MANIFEST_BYTES, MAX_RECORD_BYTES, local_path, strict_json


class ExtensionError(ValueError):
    pass


def _rows(path):
    with path.open("rb") as stream:
        while True:
            line = stream.readline(MAX_RECORD_BYTES + 1)
            if not line:
                return
            if len(line) > MAX_RECORD_BYTES:
                raise ExtensionError("record exceeds 16 MiB boundary")
            yield line, strict_json(line.decode("utf-8"))


def load_dataset(directory, version, *, expected_fixture=True):
    root = Path(directory).resolve(strict=True)
    with local_path(root, "manifest.json").open("rb") as stream:
        raw = stream.read(MAX_MANIFEST_BYTES + 1)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ExtensionError("manifest exceeds 1 MiB boundary")
    manifest = contract.validate_dataset_manifest(strict_json(raw.decode("utf-8")), version)
    fixture = (root / contract.FIXTURE_MARKER).is_file()
    if fixture != expected_fixture:
        raise contract.ContractError("fixture mode mismatch for extension dataset")
    commit = contract.producer_commit(manifest, fixture)
    validate = contract.validate_text_record if version == contract.TEXT_VERSION else contract.validate_log_row
    id_field = contract.EXTENSION_SCHEMAS[version][3]
    records, seen = [], set()
    for entry in manifest["files"]:
        path = local_path(root, entry["path"])
        digest, count = hashlib.sha256(), 0
        for line, row in _rows(path):
            digest.update(line)
            validate(row)
            if row[id_field] in seen:
                raise ExtensionError(f"duplicate {id_field} {row[id_field]}")
            seen.add(row[id_field])
            records.append(row)
            count += 1
        if digest.hexdigest() != entry["sha256"] or count != entry["records"]:
            raise ExtensionError(f"{entry['path']} hash or count mismatch")
    return {"manifest": manifest, "manifest_sha256": hashlib.sha256(raw).hexdigest(), "producer_commit": commit,
            "fixture": fixture, "records": records}


def load_text_export(directory, *, expected_fixture=True):
    loaded = load_dataset(directory, contract.TEXT_VERSION, expected_fixture=expected_fixture)
    loaded["texts"] = {r["record_id"]: r for r in loaded["records"]}
    return loaded


def load_log_rows(directory, *, expected_fixture=True):
    return load_dataset(directory, contract.LOG_ROW_VERSION, expected_fixture=expected_fixture)
