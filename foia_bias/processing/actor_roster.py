"""FBA-08 versioned historical actor roster (offline, standard library only).

A roster file is JSON::

    {"roster_version": "...", "coverage": "complete" | "executive_only" | "degraded",
     "sources": [{"name": ..., "url": ..., "retrieved_at": ...}],
     "actors": [{"actor_id": ..., "name": ..., "aliases": [...],
                 "roles": [{"role": ..., "party": "D"|"R"|"I"|..., "start": "YYYY-MM-DD",
                            "end": "YYYY-MM-DD" | null}]}]}

Party is resolved per date, so party switches are preserved. The content
SHA-256 is recorded as the roster version hash. Only ``coverage: complete``
with at least one source can certify full roster coverage; fallback or
executive-only rosters are degraded and say so.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from foia_bias.corpus import strict_json

MAX_ROSTER_BYTES = 32 * 1024 * 1024
COVERAGE_LEVELS = ("complete", "executive_only", "degraded")


@dataclass(frozen=True)
class Roster:
    version: str
    sha256: str
    coverage: str
    sources: tuple
    actors: dict

    @property
    def certifies_full_coverage(self) -> bool:
        return self.coverage == "complete" and bool(self.sources)

    def party_on(self, actor_id: str, on: date | str):
        """Party for ``actor_id`` on a date; 'mixed' if overlapping roles disagree."""
        day = date.fromisoformat(on) if isinstance(on, str) else on
        actor = self.actors.get(actor_id)
        if actor is None:
            return "unknown"
        parties = {role["party"] for role in actor["roles"]
                   if role["start"] <= day and (role["end"] is None or day < role["end"])}
        if not parties:
            return "unknown"
        return parties.pop() if len(parties) == 1 else "mixed"

    def lookup(self, name: str):
        key = name.casefold().strip()
        return sorted(actor_id for actor_id, actor in self.actors.items()
                      if key == actor["name"].casefold() or key in actor["aliases"])

    def provenance(self) -> dict:
        return {"roster_version": self.version, "roster_sha256": self.sha256,
                "coverage": self.coverage, "certifies_full_coverage": self.certifies_full_coverage}


def _date(value, field):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) != 10:
        raise ValueError(f"{field} must be YYYY-MM-DD")
    return date.fromisoformat(value)


def load_roster(path) -> Roster:
    raw = Path(path).read_bytes()
    if len(raw) > MAX_ROSTER_BYTES:
        raise ValueError("roster exceeds size boundary")
    data = strict_json(raw.decode("utf-8"))
    if not isinstance(data, dict) or set(data) != {"roster_version", "coverage", "sources", "actors"}:
        raise ValueError("roster fields must be roster_version, coverage, sources, actors")
    if data["coverage"] not in COVERAGE_LEVELS:
        raise ValueError("unknown roster coverage level")
    if not isinstance(data["roster_version"], str) or not data["roster_version"].strip():
        raise ValueError("roster_version is required")
    if not isinstance(data["sources"], list) or not isinstance(data["actors"], list):
        raise ValueError("sources and actors must be lists")
    actors = {}
    for entry in data["actors"]:
        if not isinstance(entry, dict) or set(entry) != {"actor_id", "name", "aliases", "roles"}:
            raise ValueError("actor fields must be actor_id, name, aliases, roles")
        actor_id = entry["actor_id"]
        if not isinstance(actor_id, str) or not actor_id or actor_id in actors:
            raise ValueError("actor_id must be unique nonempty text")
        roles = []
        for role in entry["roles"]:
            if not isinstance(role, dict) or set(role) != {"role", "party", "start", "end"}:
                raise ValueError("role fields must be role, party, start, end")
            if role["party"] not in ("D", "R", "I", "other"):
                raise ValueError("party must be D, R, I or other")
            start, end = _date(role["start"], "start"), _date(role["end"], "end")
            if start is None or (end is not None and end <= start):
                raise ValueError("role interval invalid")
            roles.append({"role": role["role"], "party": role["party"], "start": start, "end": end})
        if not roles:
            raise ValueError("actor requires at least one dated role")
        actors[actor_id] = {"name": entry["name"],
                            "aliases": [a.casefold().strip() for a in entry["aliases"]],
                            "roles": roles}
    return Roster(data["roster_version"], hashlib.sha256(raw).hexdigest(), data["coverage"],
                  tuple(s.get("name", "") for s in data["sources"] if isinstance(s, dict)), actors)
