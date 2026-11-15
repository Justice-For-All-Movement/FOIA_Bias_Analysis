"""Shared fixture loaders for tests (no TestCase classes here)."""
import hashlib
import json
from pathlib import Path

from foia_bias import aggregator_contract as contract

ROOT = Path(__file__).resolve().parents[1]
EXPORT = ROOT / "fixtures" / "fc13_export_v1"
PRODUCER = ROOT / "fixtures" / "fc13_producer_snapshot"
SNAP = json.loads((EXPORT / "manifest.json").read_text())["snapshot_id"]


def rid(name):
    return contract.record_id_for(f"https://foia.agency.gov.example/docs/{name}.pdf")


def texts(directory=EXPORT):
    path = directory / contract.TEXT_FIXTURE_SIDECAR
    if not path.exists():
        return {}
    return {row["record_id"]: row["content"] for row in map(json.loads, path.read_text().splitlines())}


def load_export(directory=EXPORT):
    """Return (manifest, manifest_sha256, snapshot_info, [(partition, ordinal, analysis_record)])."""
    raw = (directory / "manifest.json").read_bytes()
    manifest = json.loads(raw)
    digest = hashlib.sha256(raw).hexdigest()
    sidecar = texts(directory)
    rows = []
    for entry in manifest["files"]:
        for ordinal, line in enumerate((directory / entry["path"]).read_text().splitlines()):
            record = json.loads(line)
            rows.append((entry["path"], ordinal,
                         contract.to_analysis_record(record, fixture=True, text=sidecar.get(record["record_id"]))))
    return manifest, digest, contract.snapshot_info(manifest, digest, True), rows


def catalog_records(directory=EXPORT):
    manifest = json.loads((directory / "manifest.json").read_text())
    return [json.loads(line) for entry in manifest["files"]
            for line in (directory / entry["path"]).read_text().splitlines()]


def fixture_records():
    return {row["record_id"]: row for _, _, row in load_export()[3]}
