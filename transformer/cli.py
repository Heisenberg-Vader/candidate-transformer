"""Command-line entry point for the candidate transformer.

Usage example::

    python -m transformer.cli \
        --ats sample_inputs/ats.json \
        --csv sample_inputs/recruiter.csv \
        --notes sample_inputs/notes.txt \
        --github sample_inputs/github.json \
        --config config/output.example.yaml \
        --region US \
        --out outputs/result.json

With no ``--config`` the full canonical profiles (with provenance) are
emitted; with one, the projected/renamed records are emitted instead.
"""

from __future__ import annotations

import argparse
import json
import logging
import pathlib
import sys
from typing import Optional

from transformer.config_loader import ConfigError, load_output_config
from transformer.pipeline import (
    PipelineInputs,
    result_to_dict,
    run,
    write_json,
)
from transformer.projection import ProjectionError


def build_parser() -> argparse.ArgumentParser:
    """Define the CLI surface."""
    parser = argparse.ArgumentParser(
        prog="candidate-transformer",
        description="Fuse multi-source candidate data into one profile.",
    )
    parser.add_argument("--ats", type=pathlib.Path, help="ATS JSON file")
    parser.add_argument("--csv", type=pathlib.Path, help="Recruiter CSV file")
    parser.add_argument(
        "--notes", type=pathlib.Path, action="append", default=[],
        help="Free-text notes file (repeatable)",
    )
    parser.add_argument(
        "--github", type=pathlib.Path,
        help="JSON file with a list of GitHub user payloads",
    )
    parser.add_argument(
        "--config", type=pathlib.Path,
        help="Output config (JSON/YAML); omit for full canonical output",
    )
    parser.add_argument(
        "--region", help="Default ISO alpha-2 region for parsing note phones"
    )
    parser.add_argument(
        "--config-dir", type=pathlib.Path,
        help="Directory of model-config tables (defaults to repo config/)",
    )
    parser.add_argument(
        "--out", type=pathlib.Path, help="Write JSON here (default: stdout)"
    )
    parser.add_argument(
        "--compact", action="store_true", help="Emit compact (non-indented) JSON"
    )
    return parser


def _read_text(path: Optional[pathlib.Path]) -> Optional[str]:
    """Read a text file if given, else return None."""
    return path.read_text(encoding="utf-8") if path else None


def _read_github(path: Optional[pathlib.Path]) -> tuple[dict, ...]:
    """Read the GitHub payload file into a tuple of user dicts."""
    if not path:
        return ()
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = [data]
    return tuple(data)


def main(argv: Optional[list[str]] = None) -> int:
    """Parse args, run the pipeline, and emit JSON. Returns an exit code.

    Configuration errors fail fast (before any processing) with a clear
    message and a non-zero exit, per the design's fail-fast contract.
    """
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    args = build_parser().parse_args(argv)

    # Validate the output config up front so misconfiguration fails fast.
    output_config = None
    if args.config:
        try:
            output_config = load_output_config(args.config)
        except ConfigError as exc:
            print(f"config error: {exc}", file=sys.stderr)
            return 2

    inputs = PipelineInputs(
        ats_json=_read_text(args.ats),
        csv_text=_read_text(args.csv),
        notes_texts=tuple(_read_text(p) or "" for p in args.notes),
        github_profiles=_read_github(args.github),
        default_region=args.region,
    )

    try:
        result = run(inputs, output_config, config_dir=args.config_dir)
    except ProjectionError as exc:
        # e.g. a required field was missing under the 'error' policy.
        print(f"projection error: {exc}", file=sys.stderr)
        return 3

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        write_json(result, args.out, pretty=not args.compact)
        print(
            f"wrote {len(result.profiles)} candidate(s) to {args.out}; "
            f"{len(result.quarantined)} quarantined, "
            f"{len(result.warnings)} warning(s)",
            file=sys.stderr,
        )
    else:
        payload = result_to_dict(result)
        print(json.dumps(payload, indent=None if args.compact else 2,
                         ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
