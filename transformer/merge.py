"""Entity resolution, conflict resolution, and the confidence model.

This module turns many per-source ``SourceRecord`` objects into a handful of
canonical ``CandidateProfile`` objects. Three ideas drive it:

1. **Strong-signal-only matching.** Records are clustered only when they
   share an email or phone. Names never merge anything (design: name is a
   weak corroborator). A record with no strong key is *quarantined*, not
   merged, so a "ghost" candidate can never be glued onto someone else.

2. **Field-level conflict resolution with corroboration override.** Within a
   cluster each field is resolved independently. The default winner follows
   the source-trust hierarchy (ATS > CSV > GitHub > Notes), but agreement is
   evidence: a value seen in two sources can beat a lone higher-trust value.
   Losing values are written to the provenance log, never dropped silently.

3. **Dynamic confidence.** Each field's confidence is
   ``source_trust x corroboration_bonus x normalization_success`` and the
   overall score is a weighted average that prioritizes identity fields.

GitHub is folded in as an *enrichment* layer: a GitHub record is attached to
a cluster only when the cluster already resolved a ``links.github`` pointing
at that username -- no entity matching on GitHub data itself.
"""

from __future__ import annotations

import hashlib
import pathlib
from dataclasses import dataclass
from typing import Any, Optional

from transformer.config_files import load_trust
from transformer.models import (
    CandidateProfile,
    Education,
    Experience,
    Links,
    Location,
    Observation,
    ProvenanceEntry,
    QuarantinedRecord,
    Skill,
    SourceRecord,
)
from transformer.parsers import extract_github_username

# Field taxonomy. Each group is resolved by a different rule, so the field
# names live here in exactly one place rather than scattered as literals.
_SCALAR_FIELDS = (
    "full_name",
    "location.city",
    "location.region",
    "location.country",
    "links.linkedin",
    "links.github",
    "links.portfolio",
    "headline",
    "years_experience",
)
_ARRAY_IDENTITY_FIELDS = ("emails", "phones")
_ENTRY_ARRAY_FIELDS = ("experience", "education")


@dataclass(frozen=True)
class MergeResult:
    """Everything the merger produces: resolved profiles and what was set
    aside (quarantined ghosts), so nothing silently disappears.
    """

    profiles: tuple[CandidateProfile, ...]
    quarantined: tuple[QuarantinedRecord, ...]


# --------------------------------------------------------------------------
# Confidence model
# --------------------------------------------------------------------------


class ConfidenceModel:
    """Computes field and overall confidence from the runtime trust config.

    Holding the config on an instance keeps the arithmetic in one place and
    makes the model trivially swappable in tests (pass a different config).
    """

    def __init__(self, trust_config: dict[str, Any]):
        self._trust = trust_config["source_trust"]
        self._bonus = trust_config["corroboration_bonus"]
        self._norm = trust_config["normalization_success"]
        self._weights = trust_config["overall_confidence_weights"]
        self._required = set(
            trust_config.get("overall_confidence_required_fields", ())
        )

    def has_weight(self, field_name: str) -> bool:
        """True if ``field_name`` contributes to overall confidence."""
        return field_name in self._weights

    def source_trust(self, source: str) -> float:
        """Base trust for a source (0 if the source is unknown to config)."""
        return float(self._trust.get(source, 0.0))

    def corroboration_bonus(self, source_count: int) -> float:
        """Bonus multiplier for a value seen in ``source_count`` sources."""
        index = min(source_count, len(self._bonus) - 1)
        return float(self._bonus[index])

    def normalization_factor(self, normalized: bool) -> float:
        """Multiplier rewarding values that survived normalization."""
        return float(self._norm["normalized" if normalized else "raw"])

    def field_confidence(
        self, best_trust: float, source_count: int, normalized: bool
    ) -> float:
        """Combine the three multipliers and clamp to ``[0, 1]``.

        Corroboration can push the product above 1.0; we cap it so a
        confidence is always a probability-like number.
        """
        raw = (
            best_trust
            * self.corroboration_bonus(source_count)
            * self.normalization_factor(normalized)
        )
        return round(min(raw, 1.0), 4)

    def overall_confidence(self, field_confidences: dict[str, float]) -> float:
        """Weighted average of field confidences, biased toward identity.

        Required identity fields stay in the denominator even when missing
        (contributing zero), so an unidentifiable profile cannot score high.
        Optional fields are simply excluded when absent.
        """
        numerator = 0.0
        denominator = 0.0
        for field_name, weight in self._weights.items():
            present = field_name in field_confidences
            if not present and field_name not in self._required:
                continue
            numerator += weight * field_confidences.get(field_name, 0.0)
            denominator += weight
        if denominator == 0:
            return 0.0
        return round(numerator / denominator, 4)


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------


def merge_records(
    records: list[SourceRecord],
    github_records: Optional[list[SourceRecord]] = None,
    config_dir: Optional[pathlib.Path] = None,
) -> MergeResult:
    """Resolve, enrich, and merge source records into canonical profiles.

    ``records`` are identity-bearing rows (ATS/CSV/notes). ``github_records``
    are enrichment-only and are attached by username, never clustered.
    """
    model = ConfidenceModel(load_trust(config_dir))
    github_index = _index_github(github_records or [])

    clusters, quarantined = _resolve_entities(records)

    profiles = []
    for cluster in clusters:
        enriched = _attach_github(cluster, github_index)
        profiles.append(_build_profile(enriched, model))

    # Deterministic output ordering by candidate id (no run-to-run drift).
    profiles.sort(key=lambda p: p.candidate_id)
    return MergeResult(tuple(profiles), tuple(quarantined))


# --------------------------------------------------------------------------
# Step 1: entity resolution (strong-key union-find)
# --------------------------------------------------------------------------


def _resolve_entities(
    records: list[SourceRecord],
) -> tuple[list[list[SourceRecord]], list[QuarantinedRecord]]:
    """Cluster records sharing any strong key; quarantine keyless ghosts."""
    parent: dict[int, int] = {}

    def find(node: int) -> int:
        # Path-halving union-find: keeps the forest near-flat cheaply.
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(a: int, b: int) -> None:
        parent.setdefault(a, a)
        parent.setdefault(b, b)
        parent[find(a)] = find(b)

    quarantined: list[QuarantinedRecord] = []
    key_owner: dict[tuple[str, str], int] = {}
    indexed: list[tuple[int, SourceRecord]] = []

    for record in records:
        keys = record.strong_keys()
        if not keys:
            # Ghost candidate: no email/phone -> cannot safely merge it.
            quarantined.append(
                QuarantinedRecord(record, "no strong identity key (email/phone)")
            )
            continue
        idx = len(indexed)
        indexed.append((idx, record))
        parent.setdefault(idx, idx)
        for key in keys:
            if key in key_owner:
                union(idx, key_owner[key])
            else:
                key_owner[key] = idx

    groups: dict[int, list[SourceRecord]] = {}
    for idx, record in indexed:
        groups.setdefault(find(idx), []).append(record)
    return list(groups.values()), quarantined


# --------------------------------------------------------------------------
# Step 2: GitHub enrichment (attach by link, never by identity match)
# --------------------------------------------------------------------------


def _index_github(
    github_records: list[SourceRecord],
) -> dict[str, SourceRecord]:
    """Index GitHub enrichment records by their lower-cased username."""
    index: dict[str, SourceRecord] = {}
    for record in github_records:
        for url in record.values_for("links.github"):
            username = extract_github_username(url)
            if username:
                index[username] = record
    return index


def _attach_github(
    cluster: list[SourceRecord], github_index: dict[str, SourceRecord]
) -> list[SourceRecord]:
    """Append matching GitHub records to a cluster's record list.

    The match key is the GitHub username already present in the cluster's
    resolved links -- so enrichment rides on a link the candidate's own
    sources provided, with no fuzzy entity matching.
    """
    usernames = set()
    for record in cluster:
        for url in record.values_for("links.github"):
            username = extract_github_username(url)
            if username:
                usernames.add(username)

    extra = [
        github_index[name] for name in usernames if name in github_index
    ]
    return cluster + extra


# --------------------------------------------------------------------------
# Step 3: field-level merge into a canonical profile
# --------------------------------------------------------------------------


def _build_profile(
    cluster: list[SourceRecord], model: ConfidenceModel
) -> CandidateProfile:
    """Resolve every field of a cluster into one immutable profile."""
    observations = [obs for record in cluster for obs in record.observations]
    by_field = _group_by_field(observations)
    provenance: list[ProvenanceEntry] = []
    field_confidences: dict[str, float] = {}

    scalars = _resolve_scalars(by_field, model, provenance, field_confidences)
    emails = _resolve_array(
        by_field.get("emails", []), model, "emails", provenance,
        field_confidences,
    )
    phones = _resolve_array(
        by_field.get("phones", []), model, "phones", provenance,
        field_confidences,
    )
    skills = _resolve_skills(
        by_field.get("skills", []), model, provenance, field_confidences
    )
    experience = _resolve_entries(
        by_field.get("experience", []), model, "experience", provenance
    )
    education = _resolve_entries(
        by_field.get("education", []), model, "education", provenance
    )
    other_links = _distinct(
        obs.value for obs in by_field.get("links.other", [])
    )

    location = Location(
        city=scalars.get("location.city"),
        region=scalars.get("location.region"),
        country=scalars.get("location.country"),
    )
    _record_location_confidence(scalars, field_confidences)

    links = Links(
        linkedin=scalars.get("links.linkedin"),
        github=scalars.get("links.github"),
        portfolio=scalars.get("links.portfolio"),
        other=tuple(other_links),
    )

    candidate_id = _make_candidate_id(cluster)
    overall = model.overall_confidence(field_confidences)

    return CandidateProfile(
        candidate_id=candidate_id,
        full_name=scalars.get("full_name"),
        emails=tuple(emails),
        phones=tuple(phones),
        location=location,
        links=links,
        headline=scalars.get("headline"),
        years_experience=scalars.get("years_experience"),
        skills=tuple(skills),
        experience=tuple(experience),
        education=tuple(education),
        provenance=tuple(provenance),
        overall_confidence=overall,
    )


def _group_by_field(
    observations: list[Observation],
) -> dict[str, list[Observation]]:
    """Bucket observations by their canonical field name."""
    grouped: dict[str, list[Observation]] = {}
    for obs in observations:
        grouped.setdefault(obs.field, []).append(obs)
    return grouped


def _resolve_scalars(
    by_field: dict[str, list[Observation]],
    model: ConfidenceModel,
    provenance: list[ProvenanceEntry],
    field_confidences: dict[str, float],
) -> dict[str, Optional[Any]]:
    """Resolve every scalar field to a single winning value.

    Returns a map of field -> winner. Confidence for the top-level fields is
    recorded; nested ``location.*`` confidence is aggregated separately.
    """
    winners: dict[str, Optional[Any]] = {}
    for field_name in _SCALAR_FIELDS:
        observations = by_field.get(field_name, [])
        if not observations:
            continue
        winner, confidence = _resolve_one_scalar(
            field_name, observations, model, provenance
        )
        winners[field_name] = winner
        # Only top-level scalar fields feed overall confidence directly.
        if model.has_weight(field_name):
            field_confidences[field_name] = confidence
        # Stash nested confidences keyed by subfield for location rollup.
        if field_name.startswith("location."):
            field_confidences[field_name] = confidence
    return winners


def _resolve_one_scalar(
    field_name: str,
    observations: list[Observation],
    model: ConfidenceModel,
    provenance: list[ProvenanceEntry],
) -> tuple[Any, float]:
    """Pick the winning value for one scalar field and log all candidates.

    Scoring encodes the design rule: the value's score is its best source's
    trust times its corroboration bonus, so two agreeing sources can outscore
    one higher-trust source. Ties break toward the more-corroborated value
    (more agreement wins), then deterministically by string for stability.
    """
    grouped = _group_by_value(observations)
    scored = []
    for value, obs_list in grouped.items():
        sources = _distinct(o.source for o in obs_list)
        best_trust = max(model.source_trust(s) for s in sources)
        normalized = any(o.normalized for o in obs_list)
        score = best_trust * model.corroboration_bonus(len(sources))
        confidence = model.field_confidence(best_trust, len(sources), normalized)
        scored.append((score, len(sources), value, sources, confidence))

    # Winner: highest score, then most sources, then deterministic value.
    scored.sort(key=lambda s: (s[0], s[1], str(s[2])), reverse=True)
    win_score, _, win_value, win_sources, win_conf = scored[0]

    for score, _count, value, sources, confidence in scored:
        won = value == win_value
        for source in sources:
            provenance.append(
                ProvenanceEntry(
                    field=field_name,
                    value=value,
                    source=source,
                    method=f"{source}:{field_name}",
                    confidence=confidence,
                    won=won,
                )
            )
    return win_value, win_conf


def _resolve_array(
    observations: list[Observation],
    model: ConfidenceModel,
    field_name: str,
    provenance: list[ProvenanceEntry],
    field_confidences: dict[str, float],
) -> list[Any]:
    """Combine an identity array (emails/phones): keep every distinct value.

    Array fields are unions, not contests -- two emails are both real. Each
    value still earns a confidence (trust x corroboration), and the field's
    rolled-up confidence is its strongest member.
    """
    if not observations:
        return []
    grouped = _group_by_value(observations)
    values_sorted = sorted(grouped.keys(), key=str)
    best_confidence = 0.0
    for value in values_sorted:
        obs_list = grouped[value]
        sources = _distinct(o.source for o in obs_list)
        best_trust = max(model.source_trust(s) for s in sources)
        normalized = any(o.normalized for o in obs_list)
        confidence = model.field_confidence(best_trust, len(sources), normalized)
        best_confidence = max(best_confidence, confidence)
        for source in sources:
            provenance.append(
                ProvenanceEntry(
                    field=field_name,
                    value=value,
                    source=source,
                    method=f"{source}:{field_name}",
                    confidence=confidence,
                    won=True,
                )
            )
    field_confidences[field_name] = best_confidence
    return values_sorted


def _resolve_skills(
    observations: list[Observation],
    model: ConfidenceModel,
    provenance: list[ProvenanceEntry],
    field_confidences: dict[str, float],
) -> list[Skill]:
    """Combine skills by canonical name, each with confidence and sources."""
    if not observations:
        return []
    grouped = _group_by_value(observations)
    skills: list[Skill] = []
    best_confidence = 0.0
    for name in sorted(grouped.keys(), key=str):
        obs_list = grouped[name]
        sources = _distinct(o.source for o in obs_list)
        best_trust = max(model.source_trust(s) for s in sources)
        normalized = any(o.normalized for o in obs_list)
        confidence = model.field_confidence(best_trust, len(sources), normalized)
        best_confidence = max(best_confidence, confidence)
        skills.append(Skill(name=name, confidence=confidence, sources=tuple(sources)))
        for source in sources:
            provenance.append(
                ProvenanceEntry(
                    field="skills",
                    value=name,
                    source=source,
                    method=f"{source}:skills",
                    confidence=confidence,
                    won=True,
                )
            )
    field_confidences["skills"] = best_confidence
    return skills


def _resolve_entries(
    observations: list[Observation],
    model: ConfidenceModel,
    field_name: str,
    provenance: list[ProvenanceEntry],
) -> list[Any]:
    """Combine structured entries (experience/education): dedupe, keep all.

    Entries are whole objects, so we treat equal objects as one and keep the
    rest. They are logged for provenance but do not drive overall confidence
    strongly (their weight in the model is small).
    """
    if not observations:
        return []
    seen: dict[Any, list[str]] = {}
    for obs in observations:
        seen.setdefault(obs.value, [])
        if obs.source not in seen[obs.value]:
            seen[obs.value].append(obs.source)

    entries = sorted(seen.keys(), key=_entry_sort_key)
    for entry in entries:
        sources = seen[entry]
        confidence = model.field_confidence(
            max(model.source_trust(s) for s in sources), len(sources), True
        )
        for source in sources:
            provenance.append(
                ProvenanceEntry(
                    field=field_name,
                    value=entry,
                    source=source,
                    method=f"{source}:{field_name}",
                    confidence=confidence,
                    won=True,
                )
            )
    return entries


def _record_location_confidence(
    scalars: dict[str, Optional[Any]], field_confidences: dict[str, float]
) -> None:
    """Roll the three location subfields up into one ``location`` score.

    The merged confidence is the mean of whichever subfields are present, so
    a fully-specified location scores no worse than a partial one.
    """
    parts = [
        field_confidences.pop(key, None)
        for key in ("location.city", "location.region", "location.country")
    ]
    present = [c for c in parts if c is not None]
    if present:
        field_confidences["location"] = round(sum(present) / len(present), 4)


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def _group_by_value(
    observations: list[Observation],
) -> dict[Any, list[Observation]]:
    """Bucket observations of one field by their (equal) value."""
    grouped: dict[Any, list[Observation]] = {}
    for obs in observations:
        grouped.setdefault(obs.value, []).append(obs)
    return grouped


def _distinct(values) -> list[Any]:
    """Return values de-duplicated while preserving first-seen order."""
    return list(dict.fromkeys(values))


def _entry_sort_key(entry: Any) -> tuple:
    """Stable sort key for experience/education entries."""
    if isinstance(entry, Experience):
        return (entry.start or "", entry.company or "", entry.title or "")
    if isinstance(entry, Education):
        return (str(entry.end_year or ""), entry.institution or "")
    return (str(entry),)


def _make_candidate_id(cluster: list[SourceRecord]) -> str:
    """Derive a stable id from the cluster's sorted strong keys.

    Hashing the identity keys (not a counter or timestamp) makes the id
    reproducible: the same inputs always yield the same id, run to run.
    """
    keys = sorted(
        f"{kind}={value}"
        for record in cluster
        for kind, value in record.strong_keys()
    )
    digest = hashlib.sha1("|".join(keys).encode("utf-8")).hexdigest()
    return f"cand_{digest[:12]}"
