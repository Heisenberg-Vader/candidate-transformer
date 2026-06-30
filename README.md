# Eightfold Candidate Transformer

A deterministic, rule-based pipeline that fuses candidate data from several
heterogeneous sources — ATS JSON, recruiter CSV, free-text notes, and GitHub
— into a single standardized profile per candidate, with **per-field
confidence** and **full provenance**. No LLMs, no persistence: everything
runs in memory and is reproducible from the same inputs.

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Full canonical output (every field + provenance):
.venv/bin/python -m transformer.cli \
    --ats sample_inputs/ats.json \
    --csv sample_inputs/recruiter.csv \
    --notes sample_inputs/notes.txt \
    --notes sample_inputs/ghost_note.txt \
    --github sample_inputs/github.json \
    --region GB

# Custom projected output (subset + renames), written to a file:
.venv/bin/python -m transformer.cli \
    --ats sample_inputs/ats.json \
    --csv sample_inputs/recruiter.csv \
    --config config/output.example.yaml \
    --out outputs/result.json
```

Run the tests:

```bash
.venv/bin/python -m pytest -q
```

## Pipeline

```
ingest → normalize → resolve entities → merge → score confidence
       → (optional) project → validate
```

| Stage | Module | Responsibility |
|-------|--------|----------------|
| ingest + normalize | `parsers.py` | Parse each source in isolation; normalize values on the way in. A malformed record is logged as a warning and skipped — the run continues. |
| normalize (rules) | `normalize.py` | Phones → E.164 (invalid rejected, never coerced); countries → ISO-3166 alpha-2; dates → `YYYY-MM` (bare year → `-01`, "Present" → null); skills → canonical names (unknowns kept, never dropped). |
| resolve + merge + score | `merge.py` | Strong-key entity resolution, field-level conflict resolution, confidence model, GitHub enrichment. |
| project + validate | `projection.py`, `config_loader.py` | Apply the runtime output config; validate the result against the schema the config implies. |
| orchestration | `pipeline.py`, `cli.py` | Wire the stages together; command-line entry point. |

The canonical schema and all intermediate types live in `models.py` as
immutable (frozen) dataclasses — no pydantic, so every invariant is explicit.

## Key design decisions

**Strong-signal-only matching.** Records merge only when they share an email
or phone. Names never trigger a merge (a name is a weak corroborator). A
record with no strong key — a "ghost" candidate — is **quarantined**, not
merged, so it can never be glued onto someone else.

**Conflict resolution with corroboration override.** Within a cluster each
field is resolved independently. The default winner follows the source-trust
hierarchy (**ATS > CSV > GitHub > Notes**), but agreement is evidence: a value
seen in two sources can outrank a lone higher-trust value. Array fields
(emails, phones, skills) are combined, not contested. **Losing values are
never dropped silently** — they are written to the provenance log.

**Dynamic confidence.** Each field's confidence is
`source_trust × corroboration_bonus × normalization_success`. The
`overall_confidence` is a weighted average that heavily weights identity
fields (name, email, phone), so baseline identity outranks peripheral skills.

**GitHub is enrichment only.** A GitHub profile contributes links and skills
to an already-resolved candidate, matched by a `links.github` username the
candidate's own sources provided. No entity matching is performed on GitHub
data, and it never carries an email/phone, so it can neither form nor break a
match.

**Config-driven, validated output.** A JSON/YAML config selects a subset of
fields, renames each from its canonical path (`emails[0]` → `primary_email`,
`skills[].name` → a flat `string[]`), and picks a missing-value policy
(`null | omit | error`). The config is validated **before** the run, so an
unknown field or malformed path fails fast and explainably; each produced
record is then validated against the schema the config implies.

Everything tunable — source-trust weights, corroboration bonuses, skill
aliases, country codes — lives in `config/*.yaml` and is read at runtime;
none of it is hard-coded in Python.

## Configuration files

| File | Purpose |
|------|---------|
| `config/trust.yaml` | Source-trust weights, corroboration bonuses, normalization factors, overall-confidence weights. |
| `config/skill_aliases.yaml` | Canonical skill name → aliases. |
| `config/countries.yaml` | Country name/code → ISO-3166 alpha-2. |
| `config/output.example.yaml` | Example runtime output contract. |

## Edge cases handled

1. **Ghost candidate** (name, no email/phone) → quarantined, surfaced with a
   reason rather than risking a false merge.
2. **Format divergence** — `(415) 555-0100` and `+1 415-555-0100` normalize
   to one E.164 key; structurally invalid numbers are rejected.
3. **Unknown config field** → caught by config validation before the run, with
   a clear message.
4. **Malformed source** → isolated inside its own parser, logged as a warning;
   the run continues on the remaining sources.

## Deliberately out of scope

LLM-based extraction (deterministic rules instead), live OAuth (GitHub via
public API payloads only), persistent storage (in-memory), and fuzzy/ML name
dedupe (name stays a weak signal).
