"""Loaders for the internal model-config tables (trust, aliases, countries).

These are the tuning tables the pipeline itself reads at runtime so that no
trust weight, alias, or country code is hard-coded in Python. They are
distinct from the *user* output config handled by ``config_loader.py``.

Tables are cached per absolute path so repeated lookups during a run do not
re-read disk, while different config dirs (e.g. in tests) stay isolated.
"""

from __future__ import annotations

import functools
import pathlib
from typing import Any

import yaml


def default_config_dir() -> pathlib.Path:
    """Return the repo's ``config/`` directory (sibling of this package)."""
    return pathlib.Path(__file__).resolve().parent.parent / "config"


@functools.lru_cache(maxsize=None)
def _load_yaml(path_str: str) -> Any:
    """Read and parse a YAML file, caching by absolute path string.

    The argument is a string (not a ``Path``) because ``lru_cache`` keys on
    hashable arguments and we want stable cache identity per file.
    """
    with open(path_str, "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def load_trust(config_dir: pathlib.Path | None = None) -> dict[str, Any]:
    """Load the confidence-model knobs from ``trust.yaml``."""
    config_dir = config_dir or default_config_dir()
    return _load_yaml(str(config_dir / "trust.yaml"))


def load_skill_aliases(
    config_dir: pathlib.Path | None = None,
) -> dict[str, str]:
    """Load ``skill_aliases.yaml`` and invert it into alias -> canonical.

    The file is authored as canonical -> [aliases] for readability; callers
    want the reverse lookup, and the canonical name maps to itself so an
    already-canonical input resolves cleanly.
    """
    config_dir = config_dir or default_config_dir()
    table = _load_yaml(str(config_dir / "skill_aliases.yaml")) or {}
    alias_to_canonical: dict[str, str] = {}
    for canonical, aliases in table.items():
        alias_to_canonical[canonical.lower()] = canonical
        for alias in aliases or ():
            alias_to_canonical[str(alias).lower()] = canonical
    return alias_to_canonical


def load_countries(
    config_dir: pathlib.Path | None = None,
) -> dict[str, str]:
    """Load the country-name -> ISO alpha-2 table (keys lower-cased)."""
    config_dir = config_dir or default_config_dir()
    table = _load_yaml(str(config_dir / "countries.yaml")) or {}
    return {str(name).lower(): code for name, code in table.items()}
