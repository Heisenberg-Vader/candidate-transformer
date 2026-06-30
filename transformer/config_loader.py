"""Loading and *validating* the runtime output configuration.

The output config is a small contract: it picks a subset of canonical
fields, renames each from its canonical path (e.g. ``emails[0]`` ->
``primary_email``, ``skills[].name`` -> a flat ``string[]``), and chooses
what to do when a value is missing (``null`` | ``omit`` | ``error``).

The whole point of this module is **fail-fast**: the config is validated
against the canonical schema *before* the pipeline runs, so a typo'd field
name or a malformed path is a clear, up-front error -- not a surprise after
processing (design edge case 3).
"""

from __future__ import annotations

import json
import pathlib
import re
from dataclasses import dataclass
from typing import Any

import yaml

# The canonical schema, expressed as a nested template that the path
# validator walks. "scalar" marks a leaf; a dict marks an object; a one-item
# list marks an array whose single element describes its element type. This
# is the single source of truth for "what fields may a config request".
CANONICAL_SCHEMA: dict[str, Any] = {
    "candidate_id": "scalar",
    "full_name": "scalar",
    "emails": ["scalar"],
    "phones": ["scalar"],
    "location": {"city": "scalar", "region": "scalar", "country": "scalar"},
    "links": {
        "linkedin": "scalar",
        "github": "scalar",
        "portfolio": "scalar",
        "other": ["scalar"],
    },
    "headline": "scalar",
    "years_experience": "scalar",
    "skills": [{"name": "scalar", "confidence": "scalar", "sources": ["scalar"]}],
    "experience": [{
        "company": "scalar", "title": "scalar", "start": "scalar",
        "end": "scalar", "summary": "scalar",
    }],
    "education": [{
        "institution": "scalar", "degree": "scalar", "field": "scalar",
        "end_year": "scalar",
    }],
    "overall_confidence": "scalar",
    "provenance": [{
        "field": "scalar", "value": "scalar", "source": "scalar",
        "method": "scalar", "confidence": "scalar", "won": "scalar",
    }],
}

# Allowed missing-value policies.
_MISSING_POLICIES = ("null", "omit", "error")

# One path segment: a field name with an optional ``[i]`` or ``[]`` suffix.
_SEGMENT_RE = re.compile(r"^(?P<name>\w+)(?:\[(?P<index>\d*)\])?$")


class ConfigError(ValueError):
    """Raised for any invalid output configuration. Carries a clear message
    so misconfiguration fails fast and explainably.
    """


@dataclass(frozen=True)
class Segment:
    """One parsed path segment: a field plus how it indexes into an array."""

    name: str
    # None = plain field; "index" = ``[i]``; "all" = ``[]`` projection.
    op: str | None = None
    index: int | None = None


@dataclass(frozen=True)
class FieldSpec:
    """One resolved output field: where to read, what to call it, and what to
    do if it is missing. ``kind`` is the output shape derived from the path
    (scalar/list/object) -- the schema the produced record is checked against.
    """

    path: str
    segments: tuple[Segment, ...]
    alias: str
    missing: str
    kind: str


@dataclass(frozen=True)
class OutputConfig:
    """A validated output contract: an ordered set of field specs."""

    fields: tuple[FieldSpec, ...]


def load_output_config(path: str | pathlib.Path) -> OutputConfig:
    """Read a JSON or YAML config file and validate it, or raise ConfigError.

    Format is chosen by extension (``.json`` vs ``.yaml``/``.yml``); YAML's
    loader also parses JSON, so unknown extensions fall back to YAML.
    """
    path = pathlib.Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config file {path}: {exc}") from exc
    if path.suffix.lower() == ".json":
        data = json.loads(text)
    else:
        data = yaml.safe_load(text)
    return parse_output_config(data)


def parse_output_config(data: Any) -> OutputConfig:
    """Validate an already-parsed config object into an ``OutputConfig``.

    Every failure mode (not a mapping, bad policy, no fields, unknown field,
    duplicate alias) raises ``ConfigError`` with a specific message.
    """
    if not isinstance(data, dict):
        raise ConfigError("config root must be a mapping")

    default_missing = _normalize_policy(data.get("missing"), "null")
    raw_fields = data.get("fields")
    if not raw_fields or not isinstance(raw_fields, list):
        raise ConfigError("config must list at least one field under 'fields'")

    specs: list[FieldSpec] = []
    seen_aliases: set[str] = set()
    for raw in raw_fields:
        spec = _parse_field(raw, default_missing)
        if spec.alias in seen_aliases:
            raise ConfigError(
                f"duplicate output name '{spec.alias}'; add an explicit 'as'"
            )
        seen_aliases.add(spec.alias)
        specs.append(spec)
    return OutputConfig(tuple(specs))


def _parse_field(raw: Any, default_missing: str) -> FieldSpec:
    """Parse and validate one field entry (string shorthand or mapping)."""
    if isinstance(raw, str):
        path, alias, missing = raw, None, default_missing
    elif isinstance(raw, dict):
        path = raw.get("path")
        alias = raw.get("as")
        missing = _normalize_policy(raw.get("missing"), default_missing)
    else:
        raise ConfigError(f"field entry must be a string or mapping: {raw!r}")

    if not isinstance(path, str) or not path:
        raise ConfigError(f"field entry is missing a 'path': {raw!r}")

    segments = _parse_path(path)
    kind = _validate_path(path, segments)
    alias = alias or _default_alias(segments)
    return FieldSpec(path, segments, alias, missing, kind)


def _normalize_policy(value: Any, default: str) -> str:
    """Coerce a missing-policy value to a valid keyword, or raise.

    YAML's unquoted ``null`` parses to Python ``None``; we treat that as the
    "null" policy so configs read naturally either way.
    """
    if value is None:
        return default
    if value in _MISSING_POLICIES:
        return value
    raise ConfigError(
        f"missing policy must be one of {_MISSING_POLICIES}, got {value!r}"
    )


def _parse_path(path: str) -> tuple[Segment, ...]:
    """Tokenize a dotted path into ``Segment`` objects, or raise ConfigError."""
    segments: list[Segment] = []
    for part in path.split("."):
        match = _SEGMENT_RE.match(part)
        if not match:
            raise ConfigError(f"malformed path segment '{part}' in '{path}'")
        index_text = match.group("index")
        if index_text is None:
            segments.append(Segment(match.group("name")))
        elif index_text == "":
            segments.append(Segment(match.group("name"), op="all"))
        else:
            segments.append(
                Segment(match.group("name"), op="index", index=int(index_text))
            )
    return tuple(segments)


def _validate_path(path: str, segments: tuple[Segment, ...]) -> str:
    """Walk segments against the canonical schema; return the output kind.

    Raises ``ConfigError`` if any segment names an unknown field, indexes a
    non-array, or descends into a scalar. The returned kind ("scalar",
    "list", or "object") is what projection validates the output against.
    """
    node: Any = CANONICAL_SCHEMA
    produces_list = False
    for seg in segments:
        if not isinstance(node, dict):
            raise ConfigError(
                f"cannot descend into non-object before '{seg.name}' in '{path}'"
            )
        if seg.name not in node:
            raise ConfigError(f"unknown field '{seg.name}' in '{path}'")
        node = node[seg.name]
        if seg.op is not None:
            if not isinstance(node, list):
                raise ConfigError(f"'{seg.name}' is not an array in '{path}'")
            if seg.op == "all":
                produces_list = True
            node = node[0]  # descend into the array's element template

    if produces_list:
        return "list"
    if node == "scalar":
        return "scalar"
    if isinstance(node, list):
        return "list"
    return "object"


def _default_alias(segments: tuple[Segment, ...]) -> str:
    """Derive a default output name from the path's last named segment."""
    return segments[-1].name
