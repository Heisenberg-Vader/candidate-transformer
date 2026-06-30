"""Source parsers: raw input -> normalized ``SourceRecord`` objects.

Every parser obeys two rules from the design:

* **Normalize on the way in.** Values are run through ``normalize`` so the
  merger downstream sees canonical forms and knows (via ``Observation.
  normalized``) whether each value survived normalization.
* **Fault isolation.** A malformed record is caught inside its own parser,
  logged as a warning, and skipped -- the run continues on the rest of the
  source. One bad CSV row never sinks the file.

Parsers return a ``ParseResult`` carrying both the good records and the
human-readable warnings, so callers can surface what was dropped and why.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from transformer.models import Education, Experience, Observation, SourceRecord
from transformer.normalize import (
    normalize_country,
    normalize_date,
    normalize_email,
    normalize_phone,
    normalize_skill,
)

logger = logging.getLogger(__name__)

# Free-text extraction patterns for the notes parser. Deterministic, not ML.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Loose phone candidate: a run of digits/spaces/() /-/+ at least 7 long. The
# real validity decision is delegated to ``normalize_phone`` (E.164 check).
_PHONE_RE = re.compile(r"\+?[\d][\d\s().-]{6,}\d")
_GITHUB_USER_RE = re.compile(
    r"github\.com/(?P<user>[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ParseResult:
    """Output of a parser: clean records plus warnings about skipped data."""

    records: tuple[SourceRecord, ...] = ()
    warnings: tuple[str, ...] = ()


class _ObservationBuilder:
    """Accumulates ``Observation`` objects for one record under one source.

    A tiny helper so each parser reads as a list of "add this field" calls
    instead of repeating the Observation/normalize boilerplate.
    """

    def __init__(self, source: str):
        self._source = source
        self._observations: list[Observation] = []
        self.warnings: list[str] = []

    def add(self, field_name: str, value: Any, *, normalized: bool = True):
        """Add a non-empty value as an observation; ignore blanks."""
        if value is None or value == "":
            return
        self._observations.append(
            Observation(
                field=field_name,
                value=value,
                source=self._source,
                method=f"{self._source}:{field_name}",
                normalized=normalized,
            )
        )

    def add_email(self, raw: Any):
        """Normalize and add an email; warn (and drop) if it is invalid."""
        email = normalize_email(raw)
        if email:
            self.add("emails", email)
        elif raw:
            self.warnings.append(f"rejected invalid email: {raw!r}")

    def add_phone(self, raw: Any, region: Optional[str]):
        """Normalize and add a phone; warn (and drop) if it is invalid."""
        phone = normalize_phone(raw, region)
        if phone:
            self.add("phones", phone)
        elif raw:
            self.warnings.append(f"rejected invalid phone: {raw!r}")

    def add_skill(self, raw: Any):
        """Normalize and add a skill, keeping unknowns as raw (never drop)."""
        name, canonical = normalize_skill(raw)
        if name:
            self.add("skills", name, normalized=canonical)

    def build(self) -> Optional[SourceRecord]:
        """Return the record, or None if no observation was collected."""
        if not self._observations:
            return None
        return SourceRecord(self._source, tuple(self._observations))


def _region_hint(raw_country: Any) -> Optional[str]:
    """Best-effort ISO alpha-2 region to help parse national phone numbers."""
    return normalize_country(raw_country)


# --------------------------------------------------------------------------
# ATS JSON
# --------------------------------------------------------------------------


def parse_ats(raw_json: str) -> ParseResult:
    """Parse an ATS export: a JSON array of candidate objects.

    The whole file is one fault boundary for syntax errors; beyond that each
    candidate object is isolated so one bad entry does not drop the rest.
    """
    warnings: list[str] = []
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        logger.warning("ATS source is not valid JSON: %s", exc)
        return ParseResult((), (f"ats: invalid JSON ({exc})",))

    if not isinstance(payload, list):
        payload = [payload]

    records: list[SourceRecord] = []
    for index, obj in enumerate(payload):
        try:
            record = _parse_ats_object(obj, warnings)
        except Exception as exc:  # isolate a single malformed candidate
            logger.warning("ATS record %d skipped: %s", index, exc)
            warnings.append(f"ats: record {index} skipped ({exc})")
            continue
        if record:
            records.append(record)
    return ParseResult(tuple(records), tuple(warnings))


def _parse_ats_object(obj: dict, warnings: list[str]) -> Optional[SourceRecord]:
    """Build one ``SourceRecord`` from one ATS candidate object."""
    builder = _ObservationBuilder("ats")
    region = _region_hint(_dig(obj, "location", "country"))

    builder.add("full_name", _clean(obj.get("name") or obj.get("full_name")))
    for email in _as_list(obj.get("emails") or obj.get("email")):
        builder.add_email(email)
    for phone in _as_list(obj.get("phones") or obj.get("phone")):
        builder.add_phone(phone, region)

    builder.add("location.city", _clean(_dig(obj, "location", "city")))
    builder.add("location.region", _clean(_dig(obj, "location", "region")))
    builder.add(
        "location.country", normalize_country(_dig(obj, "location", "country"))
    )

    links = obj.get("links") or {}
    builder.add("links.linkedin", _clean(links.get("linkedin")))
    builder.add("links.github", _clean(links.get("github")))
    builder.add("links.portfolio", _clean(links.get("portfolio")))
    for other in _as_list(links.get("other")):
        builder.add("links.other", _clean(other))

    builder.add("headline", _clean(obj.get("headline")))
    builder.add("years_experience", _as_number(obj.get("years_experience")))

    for skill in _as_list(obj.get("skills")):
        builder.add_skill(skill)
    for exp in _as_list(obj.get("experience")):
        builder.add("experience", _build_experience(exp))
    for edu in _as_list(obj.get("education")):
        builder.add("education", _build_education(edu))

    warnings.extend(f"ats: {w}" for w in builder.warnings)
    return builder.build()


def _build_experience(exp: dict) -> Experience:
    """Build a canonical ``Experience`` from an ATS-shaped dict."""
    return Experience(
        company=_clean(exp.get("company")),
        title=_clean(exp.get("title")),
        start=normalize_date(exp.get("start")),
        end=normalize_date(exp.get("end")),
        summary=_clean(exp.get("summary")),
    )


def _build_education(edu: dict) -> Education:
    """Build a canonical ``Education`` from an ATS-shaped dict."""
    end_year = _as_int(edu.get("end_year") or edu.get("end"))
    return Education(
        institution=_clean(edu.get("institution")),
        degree=_clean(edu.get("degree")),
        field=_clean(edu.get("field")),
        end_year=end_year,
    )


# --------------------------------------------------------------------------
# Recruiter CSV
# --------------------------------------------------------------------------

# Column aliases so the parser tolerates differently-headed recruiter files.
_CSV_FIELD_ALIASES = {
    "full_name": {"name", "full_name", "candidate", "candidate_name"},
    "emails": {"email", "emails", "e-mail"},
    "phones": {"phone", "phones", "mobile", "tel"},
    "location.city": {"city", "town"},
    "location.region": {"region", "state", "province"},
    "location.country": {"country"},
    "links.linkedin": {"linkedin"},
    "links.github": {"github"},
    "links.portfolio": {"portfolio", "website"},
    "headline": {"headline", "title", "role"},
    "years_experience": {"years_experience", "experience_years", "yoe"},
    "skills": {"skills", "skill"},
}


def parse_csv(raw_csv: str) -> ParseResult:
    """Parse a recruiter CSV. Each row is its own fault boundary."""
    warnings: list[str] = []
    reader = csv.DictReader(io.StringIO(raw_csv))
    if reader.fieldnames is None:
        return ParseResult((), ("csv: empty or headerless file",))

    column_map = _map_csv_columns(reader.fieldnames)
    records: list[SourceRecord] = []
    for index, row in enumerate(reader):
        try:
            record = _parse_csv_row(row, column_map, warnings)
        except Exception as exc:  # isolate one bad row
            logger.warning("CSV row %d skipped: %s", index, exc)
            warnings.append(f"csv: row {index} skipped ({exc})")
            continue
        if record:
            records.append(record)
    return ParseResult(tuple(records), tuple(warnings))


def _value_for(
    row: dict, column_map: dict[str, str], canonical: str
) -> Any:
    """Return the raw cell whose column maps to ``canonical`` (or None)."""
    for header, mapped in column_map.items():
        if mapped == canonical:
            return row.get(header)
    return None


def _map_csv_columns(headers: list[str]) -> dict[str, str]:
    """Map each raw header to a canonical field name via the alias table."""
    mapping: dict[str, str] = {}
    for header in headers:
        if header is None:
            continue
        key = header.strip().lower()
        for canonical, aliases in _CSV_FIELD_ALIASES.items():
            if key in aliases:
                mapping[header] = canonical
                break
    return mapping


def _parse_csv_row(
    row: dict, column_map: dict[str, str], warnings: list[str]
) -> Optional[SourceRecord]:
    """Build one ``SourceRecord`` from one CSV row using the column map."""
    builder = _ObservationBuilder("csv")
    # Resolve country first so it can hint phone parsing for this row.
    raw_country = _value_for(row, column_map, "location.country")
    region = _region_hint(raw_country)

    for header, canonical in column_map.items():
        raw = row.get(header)
        if raw is None or str(raw).strip() == "":
            continue
        if canonical == "emails":
            for email in _split_multi(raw):
                builder.add_email(email)
        elif canonical == "phones":
            for phone in _split_multi(raw):
                builder.add_phone(phone, region)
        elif canonical == "skills":
            for skill in _split_multi(raw):
                builder.add_skill(skill)
        elif canonical == "location.country":
            builder.add("location.country", normalize_country(raw))
        elif canonical == "years_experience":
            builder.add("years_experience", _as_number(raw))
        else:
            builder.add(canonical, _clean(raw))

    warnings.extend(f"csv: {w}" for w in builder.warnings)
    return builder.build()


# --------------------------------------------------------------------------
# Recruiter notes (free text)
# --------------------------------------------------------------------------


def parse_notes(raw_text: str, region: Optional[str] = None) -> ParseResult:
    """Extract structured signals from a free-text recruiter note.

    Deterministic regex extraction only (no LLM). Recognizes emails, phones,
    GitHub links, a leading ``Name:`` line, and a ``Skills:`` line. A note
    with no email/phone yields a name-only "ghost" record, which the merger
    later quarantines rather than risk a false merge.
    """
    builder = _ObservationBuilder("notes")

    name = _extract_labeled(raw_text, "name")
    builder.add("full_name", _clean(name))

    for email in dict.fromkeys(_EMAIL_RE.findall(raw_text)):
        builder.add_email(email)
    for phone in dict.fromkeys(_PHONE_RE.findall(raw_text)):
        builder.add_phone(phone.strip(), region)

    for match in _GITHUB_USER_RE.finditer(raw_text):
        builder.add("links.github", f"https://github.com/{match['user']}")

    skills_line = _extract_labeled(raw_text, "skills")
    if skills_line:
        for skill in _split_multi(skills_line):
            builder.add_skill(skill)

    record = builder.build()
    warnings = tuple(f"notes: {w}" for w in builder.warnings)
    return ParseResult((record,) if record else (), warnings)


def _extract_labeled(text: str, label: str) -> Optional[str]:
    """Return the value after a ``Label:`` prefix on any line, or None."""
    pattern = re.compile(rf"^\s*{label}\s*:\s*(.+)$", re.IGNORECASE | re.M)
    match = pattern.search(text)
    return match.group(1).strip() if match else None


# --------------------------------------------------------------------------
# GitHub (enrichment only -- never an entity, never a strong key)
# --------------------------------------------------------------------------


def parse_github(profiles: list[dict]) -> ParseResult:
    """Build enrichment records from GitHub public-API user payloads.

    GitHub is an *enrichment layer*: it contributes links and skills to an
    already-resolved candidate (matched later by GitHub username), and never
    carries email/phone, so it can never form or break an entity match.

    Each profile dict is expected to look like the public API user object,
    optionally augmented with a pre-aggregated ``languages`` list:
        {"login", "html_url", "blog", "bio", "languages": [...]}.
    """
    warnings: list[str] = []
    records: list[SourceRecord] = []
    for index, profile in enumerate(profiles or []):
        try:
            record = _parse_github_profile(profile)
        except Exception as exc:  # isolate one bad profile
            logger.warning("GitHub profile %d skipped: %s", index, exc)
            warnings.append(f"github: profile {index} skipped ({exc})")
            continue
        if record:
            records.append(record)
    return ParseResult(tuple(records), tuple(warnings))


def _parse_github_profile(profile: dict) -> Optional[SourceRecord]:
    """Build one GitHub enrichment ``SourceRecord``."""
    builder = _ObservationBuilder("github")
    login = _clean(profile.get("login"))
    html_url = _clean(profile.get("html_url"))
    if html_url:
        builder.add("links.github", html_url)
    elif login:
        builder.add("links.github", f"https://github.com/{login}")

    builder.add("links.portfolio", _clean(profile.get("blog")))
    builder.add("headline", _clean(profile.get("bio")))
    for language in _as_list(profile.get("languages")):
        builder.add_skill(language)
    return builder.build()


def extract_github_username(url: Optional[str]) -> Optional[str]:
    """Pull a lower-cased GitHub username out of a profile URL, or None.

    Used by the merger to associate a candidate's resolved ``links.github``
    with a GitHub enrichment record -- the only "matching" GitHub gets.
    """
    if not url:
        return None
    match = _GITHUB_USER_RE.search(str(url))
    return match["user"].lower() if match else None


# --------------------------------------------------------------------------
# Small shared helpers
# --------------------------------------------------------------------------


def _clean(value: Any) -> Optional[str]:
    """Trim a string, returning None for blanks/non-strings."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_list(value: Any) -> list[Any]:
    """Coerce a scalar/None/list into a list (None -> [])."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _split_multi(value: Any) -> list[str]:
    """Split a delimited multi-value cell on ``;``, ``,`` or ``|``."""
    if value is None:
        return []
    parts = re.split(r"[;,|]", str(value))
    return [p.strip() for p in parts if p.strip()]


def _as_number(value: Any) -> Optional[float]:
    """Parse a float from a possibly-noisy value, or None."""
    if value is None or value == "":
        return None
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def _as_int(value: Any) -> Optional[int]:
    """Parse an int (e.g. a graduation year) from a value, or None."""
    number = _as_number(value)
    return int(number) if number is not None else None


def _dig(obj: dict, *keys: str) -> Any:
    """Safely walk nested dict keys, returning None if any link is missing."""
    current: Any = obj
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current
