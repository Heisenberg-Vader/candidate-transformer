"""Eightfold Candidate Transformer.

A deterministic, rule-based pipeline that fuses candidate data from
several heterogeneous sources (ATS JSON, recruiter CSV, free-text notes,
GitHub) into a single standardized profile with per-field confidence and
full provenance. No LLMs, no persistence -- everything runs in memory and
is reproducible from the same inputs.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
