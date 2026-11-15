"""Read-only client for the FC-13 export API (``/api/v1/export/manifest``,
``/api/v1/records``), with an injected fetcher.

``fetch(path) -> (status, headers, body_bytes)``. Tests inject a recorded
fixture fetcher; ``urllib_fetcher`` is provided for authorized live use and is
never called by the test suite. Pages must carry the manifest's snapshot_id;
``409`` (snapshot changed) restarts nothing silently: it raises
``SnapshotChanged`` so the caller starts a new consistent export. ``429``
honours ``Retry-After`` through an injected ``sleep`` with a bounded number of
waits. Each record is validated against the pinned FC-13 schema.

Environment for live use (none required for tests):
  FOIA_AGGREGATOR_API_BASE   e.g. https://<aggregator host>  (read-only)
  FOIA_AGGREGATOR_API_TOKEN  only if the deployment requires one
"""
from __future__ import annotations

import json
import os
from urllib.parse import quote

from foia_bias import aggregator_contract as contract


class ApiError(RuntimeError):
    pass


class SnapshotChanged(ApiError):
    pass


def _json(status, body, what):
    if status != 200:
        raise ApiError(f"{what} returned HTTP {status}")
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ApiError(f"{what} returned invalid JSON") from exc


def iter_api_records(fetch, *, limit=500, sleep=None, max_waits=5):
    """Yield ``(snapshot_id, record)`` for one consistent API snapshot."""
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("limit must be 1..500")
    status, _, body = fetch("/api/v1/export/manifest")
    manifest = _json(status, body, "manifest")
    if manifest.get("schema_version") not in contract.SUPPORTED_CONTRACT_VERSIONS:
        raise ApiError("unsupported export schema_version")
    snapshot_id = manifest.get("snapshot_id")
    expected = manifest.get("counts", {}).get("records")
    cursor, seen, waits = None, set(), 0
    while True:
        path = f"/api/v1/records?limit={limit}" + (f"&cursor={quote(cursor)}" if cursor else "")
        status, headers, body = fetch(path)
        if status == 429:
            waits += 1
            if sleep is None or waits > max_waits:
                raise ApiError("rate limited by the export API")
            sleep(int(headers.get("Retry-After", "60")))
            continue
        if status == 409:
            raise SnapshotChanged("archive snapshot changed mid-export; restart from the manifest")
        page = _json(status, body, "records page")
        if page.get("snapshot_id") != snapshot_id or page.get("schema_version") != manifest["schema_version"]:
            raise SnapshotChanged("page snapshot differs from manifest snapshot")
        for record in page.get("records", []):
            contract.validate_catalog_record(record)
            if record["record_id"] in seen:
                raise ApiError(f"duplicate record_id {record['record_id']} across pages")
            seen.add(record["record_id"])
            yield snapshot_id, record
        cursor = page.get("next_cursor")
        if not cursor:
            break
    if expected is not None and len(seen) != expected:
        raise ApiError("API records do not match manifest counts.records")


def urllib_fetcher(base_url=None, token=None, timeout=60):  # pragma: no cover - live use only
    import urllib.error
    import urllib.request
    base_url = (base_url or os.environ.get("FOIA_AGGREGATOR_API_BASE", "")).rstrip("/")
    token = token or os.environ.get("FOIA_AGGREGATOR_API_TOKEN")
    if not base_url.startswith("https://"):
        raise ApiError("FOIA_AGGREGATOR_API_BASE must be an https URL")

    def fetch(path):
        request = urllib.request.Request(base_url + path, headers={"Accept": "application/json"})
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, dict(response.headers), response.read(64 * 1024 * 1024)
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers or {}), exc.read()
    return fetch
