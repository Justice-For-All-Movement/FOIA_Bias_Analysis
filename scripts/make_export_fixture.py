"""Regenerate the synthetic FC-13 (foia-catalog-export/1) snapshot fixture.

Output matches FOIA_Aggregator ``export_contract.write_snapshot`` byte layout:
``records-NNNNN.jsonl`` (``json.dumps(sort_keys=True, ensure_ascii=False)``
per line) and ``manifest.json`` (``indent=2, sort_keys=True``). Adds the
Bias-side ``FIXTURE.json`` marker and ``bias-text-fixture.jsonl`` text sidecar
(FC-13/1 exports no text). Deterministic; all content is synthetic. Markers
like ``[[signal ...]]`` drive the deterministic fake labeling provider.

Usage: python scripts/make_export_fixture.py [output_dir]
"""
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from foia_bias.aggregator_contract import (CONTRACT_VERSION, FIXTURE_MARKER, TEXT_FIXTURE_SIDECAR,  # noqa: E402
                                           normalize_url, record_id_for, validate_catalog_manifest,
                                           validate_catalog_record)

FILLER = "Synthetic routine administrative text with no partisan content. "
OFFICIAL = "https://foia.agency.gov.example/{agency}/reading-room/"
THIRD_PARTY = "https://releases.thirdparty.example/agency/{agency}/"


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def source(url, provenance):
    return {"source_id": "src_" + hashlib.sha256(normalize_url(url).encode()).hexdigest()[:16],
            "url": normalize_url(url), "provenance": provenance}


def doc_url(name):
    return f"https://foia.agency.gov.example/docs/{name}.pdf"


def record(name, agency, published, *, text=None, provenance=("official",), archived=True, binary=None,
           extraction="indexed", char_count=None):
    sources = [source((OFFICIAL if p == "official" else THIRD_PARTY).format(agency=agency.lower()), p)
               for p in provenance]
    indexed = archived and extraction in ("indexed", "indexed_truncated")
    url = doc_url(name)
    return {
        "record_id": record_id_for(url), "url": url, "title": f"SYNTHETIC {name}", "description": None,
        "filename": f"{name}.pdf", "file_type": "pdf", "mime_type": "application/pdf",
        "published_date": published, "published_year": int(published[:4]) if published else None,
        "discovered_at": "2026-11-01T00:00:00+00:00",
        "agency": {"slug": agency.lower(), "name": f"Synthetic {agency}"},
        "office": {"slug": f"{agency.lower()}-foia", "name": f"Synthetic {agency} FOIA Office"},
        "sources": sources,
        "preservation": {"status": "archived" if archived else "metadata_only",
                         "acquisition_status": "downloaded" if archived else "pending",
                         "sha256": (binary or sha("binary:" + name)) if archived else None,
                         "file_size": 1000 if archived else None,
                         "archived_at": "2026-11-02T00:00:00+00:00" if archived else None},
        "text": {"indexed": indexed, "extraction_status": extraction if archived else None,
                 "extraction_method": "native_text" if indexed else None,
                 "character_count": (char_count if char_count is not None else (len(text) if text else None))
                 if indexed else None,
                 "truncated": (extraction == "indexed_truncated") if indexed else None},
    }, (text if indexed else None)


def scenarios():
    shared = "SYNTHETIC FIXTURE memo. [[signal party=R wrongdoing=0.8 favorability=-0.6]] " + FILLER * 3
    long_text = "SYNTHETIC FIXTURE long release. " + FILLER * 400 + \
        " [[signal party=D wrongdoing=0.9 favorability=-0.7]] Late decisive passage."
    assert long_text.index("[[signal") > 20000
    redacted = "SYNTHETIC FIXTURE briefing. [[signal party=D wrongdoing=0.1 favorability=0.5]] " + FILLER
    return [
        record("rr-001", "DOJ", "2003-05-01", text=shared, binary=sha("shared-binary")),
        record("mr-001", "DOJ", "2003-05-01", text=shared, binary=sha("shared-binary"), provenance=("third_party",)),
        record("oc-001", "DHS", "2014-08-12", text=long_text, provenance=("official", "third_party")),
        record("rr-002", "EPA", "2018-03-01", text=redacted),
        record("mr-002", "EPA", "2018-03-03", text=redacted.replace("briefing", "briefing [b6]"),
               provenance=("third_party",)),
        record("rr-003", "DOJ", None, text="SYNTHETIC FIXTURE undated. " + FILLER),
        record("mr-003", "FBI", "2011-02-02", archived=False, provenance=("third_party",)),
        record("rr-004", "DOD", "2006-07-07", extraction="failed"),
        record("rr-005", "DOD", "2007-07-07", text="SYNTHETIC FIXTURE partial OCR. " + FILLER,
               extraction="indexed_truncated"),
        record("rr-006", "State", "2021-01-20",
               text="SYNTHETIC FIXTURE inauguration-day release. [[signal party=R wrongdoing=0.2 favorability=0.1]]"),
        record("rr-007", "State", "2025-03-15", text="SYNTHETIC FIXTURE ongoing-term release. " + FILLER),
        record("rr-008", "DOJ", "2012", text="SYNTHETIC FIXTURE year-only. [[signal party=D wrongdoing=0.6 favorability=0]]"),
        record("rr-009", "DHS", "2016-05-05", char_count=120),  # indexed upstream, text not supplied
    ]


def build(out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = scenarios()
    for row, _ in rows:
        validate_catalog_record(row)
    files = []
    for index in range(0, len(rows), 5):
        chunk = [r for r, _ in rows[index:index + 5]]
        name = f"records-{index // 5 + 1:05d}.jsonl"
        body = "".join(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in chunk)
        (out_dir / name).write_text(body, encoding="utf-8")
        files.append({"path": name, "records": len(chunk), "sha256": sha(body)})
    records = [r for r, _ in rows]
    manifest = {
        "counts": {"records": len(records),
                   "archived": sum(r["preservation"]["status"] == "archived" for r in records),
                   "metadata_only": sum(r["preservation"]["status"] == "metadata_only" for r in records),
                   "text_indexed": sum(r["text"]["indexed"] for r in records),
                   "agencies": len({r["agency"]["slug"] for r in records}),
                   "sources": len({s["source_id"] for r in records for s in r["sources"]})},
        "excluded_fields": ["storage_key", "local_path", "storage_backend", "download_error", "requester_name"],
        "files": files, "generated_at": "2026-11-15T00:00:00+00:00",
        "license": "SYNTHETIC FIXTURE - not FOIA_Aggregator data.",
        "pagination": {"default_page_size": 100, "max_page_size": 500,
                       "order": "stable within a snapshot; cursors expire when the snapshot changes"},
        "schema": "/api/v1/export/schema", "schema_version": CONTRACT_VERSION,
        "snapshot_id": "snap_" + sha("synthetic-fc13-fixture-v1")[:20],
    }
    validate_catalog_manifest(manifest)
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    (out_dir / FIXTURE_MARKER).write_text(json.dumps(
        {"synthetic": True, "note": "Synthetic Bias fixture in FC-13 snapshot layout; not Aggregator data."},
        sort_keys=True) + "\n", encoding="utf-8")
    sidecar = "".join(json.dumps({"record_id": r["record_id"], "content": t}, sort_keys=True) + "\n"
                      for r, t in rows if t is not None and r["text"]["indexed"])
    (out_dir / TEXT_FIXTURE_SIDECAR).write_text(sidecar, encoding="utf-8")
    return manifest


if __name__ == "__main__":
    build(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "fixtures" / "fc13_export_v1")
