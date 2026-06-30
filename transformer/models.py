"""Canonical data model for the candidate transformer.

Two families of types live here:

* The *canonical record* (`CandidateProfile` and its nested value objects)
  -- the immutable, normalized profile that the pipeline produces. Once a
  profile is built it is never mutated; projection only ever reads it.
* The *intermediate* types (`Observation`, `SourceRecord`) -- the mutable-
  by-construction units that flow ingest -> normalize -> resolve -> merge
  before the canonical record is assembled.

We use plain frozen dataclasses (no pydantic) so that every invariant is
visible and defensible in `validate()` rather than hidden in a library.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class Source(str, Enum):
    """The four ingest channels, ordered by default trust (ATS highest).

    Subclassing ``str`` keeps values JSON-serializable and lets them be
    used directly as config-table keys (e.g. ``trust["ats"]``).
    """

    ATS = "ats"
    CSV = "csv"
    GITHUB = "github"
    NOTES = "notes"


# --------------------------------------------------------------------------
# Intermediate types: what parsers emit and what the merger consumes.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Observation:
    """One atomic field value seen in one source.

    This is the unit of evidence the merger reasons over. A scalar field
    (e.g. ``full_name``) yields one observation per source; an array field
    (e.g. ``emails``) yields one observation per element.
    """

    # Canonical field path, e.g. "full_name", "emails", "location.country".
    field: str
    # The value after normalization (or the raw value if normalization was
    # not applicable / did not succeed -- see ``normalized``).
    value: Any
    # Which channel produced it; must be a ``Source`` value.
    source: str
    # Human-readable derivation, e.g. "ats.json:email" or "notes:regex".
    method: str
    # True iff the value matched a canonical format (drives the
    # normalization_success multiplier in the confidence model).
    normalized: bool = True


@dataclass(frozen=True)
class SourceRecord:
    """All observations belonging to one candidate-shaped record in one
    source, before cross-source entity resolution.

    A single CSV row, one ATS JSON object, or one parsed notes block each
    become one ``SourceRecord``.
    """

    source: str
    observations: tuple[Observation, ...]

    def values_for(self, field_name: str) -> tuple[Any, ...]:
        """Return every observed value for ``field_name`` in this record."""
        return tuple(
            obs.value for obs in self.observations if obs.field == field_name
        )

    def strong_keys(self) -> frozenset[tuple[str, str]]:
        """Return this record's identity keys for entity resolution.

        Only strong signals (email, phone) are returned. Names are
        deliberately excluded so they can never trigger a merge -- they are
        weak corroborators at best.
        """
        keys: set[tuple[str, str]] = set()
        for obs in self.observations:
            if obs.field in ("emails", "phones") and obs.value:
                keys.add((obs.field, str(obs.value)))
        return frozenset(keys)


# --------------------------------------------------------------------------
# Canonical value objects: the immutable building blocks of a profile.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Location:
    """A geographic location with ISO-3166 alpha-2 ``country``."""

    city: Optional[str] = None
    region: Optional[str] = None
    country: Optional[str] = None


@dataclass(frozen=True)
class Links:
    """Resolved profile links. ``other`` collects anything uncategorized."""

    linkedin: Optional[str] = None
    github: Optional[str] = None
    portfolio: Optional[str] = None
    other: tuple[str, ...] = ()


@dataclass(frozen=True)
class Skill:
    """A skill with the confidence we assign it and its supporting sources."""

    name: str
    confidence: float
    sources: tuple[str, ...]


@dataclass(frozen=True)
class Experience:
    """One employment entry. ``end`` is None when the role is ongoing."""

    company: Optional[str] = None
    title: Optional[str] = None
    start: Optional[str] = None  # YYYY-MM
    end: Optional[str] = None    # YYYY-MM, or None for "Present"
    summary: Optional[str] = None


@dataclass(frozen=True)
class Education:
    """One education entry."""

    institution: Optional[str] = None
    degree: Optional[str] = None
    field: Optional[str] = None
    end_year: Optional[int] = None


@dataclass(frozen=True)
class ProvenanceEntry:
    """One line in the audit log: which value for which field came from
    where, how it was derived, how confident we are, and whether it won.

    Losing values are recorded here too (``won=False``) so no information is
    ever dropped silently.
    """

    field: str
    value: Any
    source: str
    method: str
    confidence: float
    won: bool = True


@dataclass(frozen=True)
class CandidateProfile:
    """The canonical, immutable candidate record.

    Built once by the merger and thereafter read-only. ``provenance`` and
    ``overall_confidence`` make every value in the record auditable.
    """

    candidate_id: str
    full_name: Optional[str] = None
    emails: tuple[str, ...] = ()
    phones: tuple[str, ...] = ()
    location: Location = field(default_factory=Location)
    links: Links = field(default_factory=Links)
    headline: Optional[str] = None
    years_experience: Optional[float] = None
    skills: tuple[Skill, ...] = ()
    experience: tuple[Experience, ...] = ()
    education: tuple[Education, ...] = ()
    provenance: tuple[ProvenanceEntry, ...] = ()
    overall_confidence: float = 0.0


@dataclass(frozen=True)
class QuarantinedRecord:
    """A record that lacked any strong key (e.g. name only -- a "ghost"
    candidate). Held aside rather than risking a false merge, but surfaced
    so the operator can see what was set apart and why.
    """

    record: SourceRecord
    reason: str
