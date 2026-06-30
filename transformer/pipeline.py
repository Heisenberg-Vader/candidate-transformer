"""End-to-end orchestration.

Wires the stages together in the order the design prescribes:

    ingest -> normalize -> resolve entities -> merge -> score
            -> (optional) project -> validate

Each stage already lives in its own module; this file is the thin conductor
that moves data between them and collects everything worth surfacing
(profiles, quarantined ghosts, and per-source warnings). It holds no
business rules of its own, which keeps the rules testable in isolation.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
from dataclasses import dataclass, field
from typing import Any, Optional

from transformer.config_loader import OutputConfig
from transformer.merge import merge_records
from transformer.models import CandidateProfile, QuarantinedRecord
from transformer.parsers import (
    parse_ats,
    parse_csv,
    parse_github,
    parse_notes,
)
from transformer.projection import _to_plain, project_profiles


@dataclass(frozen=True)
class PipelineInputs:
    """The raw materials for one run. Every field is optional so a run can
    use any subset of sources (a missing source is simply absent, not an
    error -- the design tolerates non-existent sources).
    """

    ats_json: Optional[str] = None
    csv_text: Optional[str] = None
    notes_texts: tuple[str, ...] = ()
    github_profiles: tuple[dict, ...] = ()
    # ISO alpha-2 region used to parse national-format phones in free-text
    # notes, which carry no country column to infer it from.
    default_region: Optional[str] = None


@dataclass(frozen=True)
class PipelineResult:
    """Everything one run produces, ready to serialize."""

    profiles: tuple[CandidateProfile, ...]
    quarantined: tuple[QuarantinedRecord, ...]
    warnings: tuple[str, ...]
    # Populated only when an output config was supplied.
    output_records: Optional[list[dict[str, Any]]] = None


def run(
    inputs: PipelineInputs,
    output_config: Optional[OutputConfig] = None,
    config_dir: Optional[pathlib.Path] = None,
) -> PipelineResult:
    """Run the full pipeline over ``inputs``.

    Parsing is fault-isolated per source, so a malformed source contributes
    warnings and whatever it could salvage, while the rest of the run
    proceeds. Projection runs only if an ``output_config`` is given.
    """
    warnings: list[str] = []
    records = []
    github_records = []

    if inputs.ats_json is not None:
        result = parse_ats(inputs.ats_json)
        records.extend(result.records)
        warnings.extend(result.warnings)

    if inputs.csv_text is not None:
        result = parse_csv(inputs.csv_text)
        records.extend(result.records)
        warnings.extend(result.warnings)

    for note in inputs.notes_texts:
        result = parse_notes(note, region=inputs.default_region)
        records.extend(result.records)
        warnings.extend(result.warnings)

    if inputs.github_profiles:
        result = parse_github(list(inputs.github_profiles))
        github_records.extend(result.records)
        warnings.extend(result.warnings)

    merged = merge_records(records, github_records, config_dir=config_dir)

    output_records = None
    if output_config is not None:
        output_records = project_profiles(merged.profiles, output_config)

    return PipelineResult(
        profiles=merged.profiles,
        quarantined=merged.quarantined,
        warnings=tuple(warnings),
        output_records=output_records,
    )


def profile_to_dict(profile: CandidateProfile) -> dict[str, Any]:
    """Serialize a canonical profile to a plain, JSON-ready dict.

    Used when no output config is supplied: the caller gets the full
    canonical record, provenance and all, for maximum auditability.
    """
    return _to_plain(dataclasses.asdict(profile))


def result_to_dict(result: PipelineResult) -> dict[str, Any]:
    """Assemble the full run output (candidates + quarantine + warnings).

    Candidates are the projected records when a config was used, else the
    full canonical profiles.
    """
    if result.output_records is not None:
        candidates: list[dict[str, Any]] = result.output_records
    else:
        candidates = [profile_to_dict(p) for p in result.profiles]

    quarantined = [
        {
            "source": q.record.source,
            "reason": q.reason,
            # Surface what little we know so an operator can act on it.
            "observed": _to_plain(
                {obs.field: obs.value for obs in q.record.observations}
            ),
        }
        for q in result.quarantined
    ]
    return {
        "candidates": candidates,
        "quarantined": quarantined,
        "warnings": list(result.warnings),
    }


def write_json(result: PipelineResult, path: pathlib.Path, pretty: bool = True):
    """Write the run output to ``path`` as JSON."""
    payload = result_to_dict(result)
    text = json.dumps(payload, indent=2 if pretty else None, ensure_ascii=False)
    path.write_text(text + "\n", encoding="utf-8")
