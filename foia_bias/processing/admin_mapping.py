"""FBA-08 administration, term-window and transition mapping (standard library only).

Rules (plan section 1 "Unresolved historical detail", section 2 "Comparable
time periods" and FBA-08 acceptance):

* Administration assignment uses **exact** intervals. Power transfers at
  12:00 America/New_York on January 20 (17:00 UTC for every boundary here).
* The transition flag is independent and **never** changes the incumbent.
  ``transition_months=6`` marks records near a boundary; 2021-01-21 is Biden.
* A date-only record on an inauguration day is ambiguous: no party is guessed.
  Callers report include/exclude sensitivity instead.
* Records in the ongoing second Trump term are assigned to it but excluded from
  the completed-term cohort with an explicit reason; the archive keeps them.
* Primary comparison windows are complete four-year terms.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Optional

TRANSFER_UTC = time(17, 0, tzinfo=timezone.utc)  # noon EST on January 20


@dataclass(frozen=True)
class Term:
    term_id: str
    admin_name: str
    party: str
    start_year: int
    end_year: Optional[int]  # None = ongoing
    in_scope: bool = True

    @property
    def start(self) -> datetime:
        return datetime.combine(date(self.start_year, 1, 20), TRANSFER_UTC)

    @property
    def end(self) -> Optional[datetime]:
        if self.end_year is None:
            return None
        return datetime.combine(date(self.end_year, 1, 20), TRANSFER_UTC)

    @property
    def completed(self) -> bool:
        return self.end_year is not None


TERMS = (
    Term("bush41", "G.H.W. Bush", "R", 1989, 1993, in_scope=False),
    Term("clinton-1", "Clinton", "D", 1993, 1997),
    Term("clinton-2", "Clinton", "D", 1997, 2001),
    Term("bush-1", "Bush", "R", 2001, 2005),
    Term("bush-2", "Bush", "R", 2005, 2009),
    Term("obama-1", "Obama", "D", 2009, 2013),
    Term("obama-2", "Obama", "D", 2013, 2017),
    Term("trump-1", "Trump", "R", 2017, 2021),
    Term("biden-1", "Biden", "D", 2021, 2025),
    Term("trump-2", "Trump", "R", 2025, None),
)
# Administration-level intervals (for legacy callers / pooled comparisons).
ADMIN_PERIODS = [
    ("Clinton", "D", date(1993, 1, 20), date(2001, 1, 20)),
    ("Bush", "R", date(2001, 1, 20), date(2009, 1, 20)),
    ("Obama", "D", date(2009, 1, 20), date(2017, 1, 20)),
    ("Trump", "R", date(2017, 1, 20), date(2021, 1, 20)),
    ("Biden", "D", date(2021, 1, 20), date(2025, 1, 20)),
]
# Years in which a different administration took office (boundaries that
# matter for the transition flag). Term-to-term same-person boundaries are not
# transfers of power but are still term-window boundaries.
TRANSFER_YEARS = (1993, 2001, 2009, 2017, 2021, 2025)
COHORT_START = TERMS[1].start
COHORT_END = TERMS[-2].end  # exclusive: 2025-01-20 17:00 UTC
ASSIGNMENT_VERSION = "fba08-v1"


def _add_months(value: datetime, months: int) -> datetime:
    total = value.year * 12 + (value.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    # January 20 shifted by whole months always exists (day <= 28 safe).
    return value.replace(year=year, month=month)


def parse_temporal(value):
    """Return (instant_or_date, precision) where precision is exact/date/naive_time/unknown."""
    if value is None or value == "":
        return None, "unknown"
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value, "naive_time"
        return value.astimezone(timezone.utc), "exact"
    if isinstance(value, date):
        return value, "date"
    if not isinstance(value, str):
        raise ValueError("temporal value must be ISO text, date or datetime")
    text = value.strip()
    if len(text) == 10:
        return date.fromisoformat(text), "date"
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed, "naive_time"
    return parsed.astimezone(timezone.utc), "exact"


def _term_for_instant(instant: datetime) -> Optional[Term]:
    for term in TERMS:
        if term.start <= instant and (term.end is None or instant < term.end):
            return term
    return None


def _term_for_day(day: date) -> Optional[Term]:
    for term in TERMS:
        start = term.start.date()
        end = term.end.date() if term.end else None
        if start < day and (end is None or day < end):
            return term
    return None


def _transition_flag(reference: datetime, months: int) -> bool:
    if months <= 0:
        return False
    for year in TRANSFER_YEARS:
        boundary = datetime.combine(date(year, 1, 20), TRANSFER_UTC)
        if _add_months(boundary, -months) <= reference < _add_months(boundary, months):
            return True
    return False


def assign_period(value, transition_months: int = 0) -> dict:
    """Assign a record date to an administration/term with explicit status."""
    if type(transition_months) is not int or not 0 <= transition_months <= 24:
        raise ValueError("transition_months must be an integer 0..24")
    parsed, precision = parse_temporal(value)
    result = {
        "admin_name": None, "admin_party": None, "term_id": None, "completed_term": None,
        "is_transition": False, "precision": precision, "status": "assigned",
        "candidates": [], "cohort_eligible": False, "exclusion_reason": None,
        "assignment_version": ASSIGNMENT_VERSION,
    }
    if parsed is None:
        result.update(status="unknown_date", exclusion_reason="unknown_date")
        return result
    if precision == "exact":
        instant = parsed
        term = _term_for_instant(instant)
        reference = instant
    else:
        day = parsed.date() if isinstance(parsed, datetime) else parsed
        reference = datetime.combine(day, time(12, 0, tzinfo=timezone.utc))
        if day.month == 1 and day.day == 20 and any(t.start_year == day.year for t in TERMS):
            # Date-only (or timezone-less) inauguration day: never guess.
            outgoing = _term_for_day(date(day.year, 1, 19))
            incoming = next(t for t in TERMS if t.start_year == day.year)
            result.update(
                status="ambiguous_transition_day",
                candidates=[t.term_id for t in (outgoing, incoming) if t is not None],
                is_transition=transition_months > 0,
                exclusion_reason="ambiguous_transition_day",
            )
            return result
        term = _term_for_day(day)
    result["is_transition"] = _transition_flag(reference, transition_months)
    if term is None:
        result.update(status="before_scope", exclusion_reason="before_scope")
        return result
    result.update(admin_name=term.admin_name, admin_party=term.party, term_id=term.term_id,
                  completed_term=term.completed)
    if not term.in_scope:
        result["exclusion_reason"] = "before_scope"
    elif not term.completed:
        result["exclusion_reason"] = "ongoing_incomplete_term"
    else:
        result["cohort_eligible"] = True
    return result


def get_admin_for_date(value, transition_months: int = 0) -> dict:
    """Legacy-compatible wrapper: incumbent + independent transition flag.

    Ambiguous, unknown and out-of-scope dates return ``admin_name=None``.
    """
    info = assign_period(value, transition_months)
    in_scope = info["status"] == "assigned" and info["exclusion_reason"] != "before_scope"
    return {
        "admin_name": info["admin_name"] if in_scope else None,
        "admin_party": info["admin_party"] if in_scope else None,
        "is_transition": info["is_transition"],
        "term_id": info["term_id"] if in_scope else None,
        "status": info["status"],
    }


def parse_date(value: str | None) -> Optional[date]:
    """Parse ISO8601 date strings while tolerating missing values."""
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).date()


# Explicit analysis-date policy. Submission/receipt dates describe a request,
# not the government's disclosure decision; they never stand in for one.
DECISION_DATE_FIELDS = ("decision_date", "release_date", "closed_date")
NON_DECISION_DATE_FIELDS = ("submitted_date", "received_date", "requested_date", "created_date")


def analysis_date(dates: dict, policy=DECISION_DATE_FIELDS):
    """Return ``(field, value)`` for the first available decision/release date.

    Returns ``(None, None)`` when only request-side dates exist.
    """
    for field in policy:
        if field in NON_DECISION_DATE_FIELDS:
            raise ValueError(f"{field} is not a decision/release date")
        value = dates.get(field)
        if value:
            return field, value
    return None, None


def term_windows(include_ongoing: bool = False):
    """Equal-duration comparison windows (completed four-year terms by default)."""
    return [
        {"term_id": t.term_id, "admin_name": t.admin_name, "party": t.party,
         "start": t.start.isoformat().replace("+00:00", "Z"),
         "end": t.end.isoformat().replace("+00:00", "Z") if t.end else None,
         "completed": t.completed, "years": (t.end_year - t.start_year) if t.end_year else None}
        for t in TERMS if t.in_scope and (t.completed or include_ongoing)
    ]
