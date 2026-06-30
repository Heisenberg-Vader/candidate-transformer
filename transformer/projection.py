"""Project a canonical profile onto the user's output contract.

Projection is strictly a *read* over the immutable canonical record: it
selects the configured paths, renames them, applies the missing-value policy,
and checks each produced record against the schema implied by the config
(``kind`` on each ``FieldSpec``). The output's contract is whatever the
config asked for -- not the canonical schema.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from transformer.config_loader import FieldSpec, OutputConfig, Segment
from transformer.models import CandidateProfile

# Sentinel for "this path resolved to nothing", kept distinct from a real
# ``None`` value (e.g. an ongoing job's ``end`` is a legitimate null).
_MISSING = object()


class ProjectionError(ValueError):
    """Raised when a required field is missing under the ``error`` policy, or
    when a produced record violates the config-derived output schema.
    """


def project_profile(
    profile: CandidateProfile, config: OutputConfig
) -> dict[str, Any]:
    """Project one profile into an output record per ``config``."""
    canonical = _to_plain(dataclasses.asdict(profile))
    record: dict[str, Any] = {}
    for spec in config.fields:
        value = _resolve(canonical, spec.segments)
        if _is_absent(value):
            _apply_missing_policy(record, spec)
            continue
        record[spec.alias] = value
    _validate_record(record, config)
    return record


def project_profiles(
    profiles, config: OutputConfig
) -> list[dict[str, Any]]:
    """Project many profiles, preserving their order."""
    return [project_profile(p, config) for p in profiles]


def _to_plain(value: Any) -> Any:
    """Recursively turn tuples into lists so the canonical view is JSON-clean.

    ``dataclasses.asdict`` preserves tuples (our immutable arrays); resolving
    and serializing both want plain lists, so normalize once up front.
    """
    if isinstance(value, dict):
        return {key: _to_plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_plain(item) for item in value]
    return value


def _resolve(node: Any, segments: tuple[Segment, ...]) -> Any:
    """Resolve a parsed path against the canonical dict.

    Returns the value, ``_MISSING`` when a step has nothing to read, or a
    (possibly empty) list for an ``[]`` projection. ``None`` may be returned
    when the underlying canonical value is genuinely null.
    """
    if not segments:
        return node
    seg, rest = segments[0], segments[1:]
    if not isinstance(node, dict):
        return _MISSING
    child = node.get(seg.name, _MISSING)

    if seg.op is None:
        return _resolve(child, rest)

    # Array operations: the child must be a list to index or project.
    if child is _MISSING or child is None:
        return [] if seg.op == "all" else _MISSING
    if not isinstance(child, list):
        return _MISSING

    if seg.op == "index":
        if seg.index is not None and seg.index < len(child):
            return _resolve(child[seg.index], rest)
        return _MISSING

    # "all": map the remaining path over each element, dropping empties so a
    # projection like skills[].name yields a clean flat list of names.
    projected = []
    for element in child:
        resolved = _resolve(element, rest)
        if resolved is not _MISSING and resolved is not None:
            projected.append(resolved)
    return projected


def _is_absent(value: Any) -> bool:
    """Treat ``_MISSING`` and a bare ``None`` as absent; ``[]`` is present.

    An empty projection list is a real (if empty) answer, so it is not
    subject to the missing-value policy.
    """
    return value is _MISSING or value is None


def _apply_missing_policy(record: dict[str, Any], spec: FieldSpec) -> None:
    """Honor the field's missing policy: null writes None, omit drops the
    key, error fails loudly naming the field.
    """
    if spec.missing == "null":
        record[spec.alias] = None
    elif spec.missing == "omit":
        return
    elif spec.missing == "error":
        raise ProjectionError(
            f"required field '{spec.alias}' (path '{spec.path}') is missing"
        )


def _validate_record(record: dict[str, Any], config: OutputConfig) -> None:
    """Check each present value against the config-derived output schema.

    A path declared as a list must yield a list; a scalar path must not yield
    a list/dict. This is the "output validated against a schema derived from
    the config" guarantee, enforced per record.
    """
    for spec in config.fields:
        if spec.alias not in record:
            continue
        value = record[spec.alias]
        if value is None:
            continue  # an explicit null satisfies any declared kind
        if spec.kind == "list" and not isinstance(value, list):
            raise ProjectionError(
                f"field '{spec.alias}' must be a list but got {type(value).__name__}"
            )
        if spec.kind == "scalar" and isinstance(value, (list, dict)):
            raise ProjectionError(
                f"field '{spec.alias}' must be a scalar but got {type(value).__name__}"
            )
