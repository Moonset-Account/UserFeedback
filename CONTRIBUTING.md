# Contributing

Thank you for helping improve CodeRiskTools.

## Before opening a feature PR

Open an issue first for changes that add a detector, policy rule, external integration or public output field. This keeps the project driven by observed user demand instead of an imagined roadmap.

## Development setup

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install --no-deps -e .
python -m pytest -q
```

## Pull-request requirements

- add or update tests;
- use synthetic, non-production fixtures only;
- never commit a live credential, private finding or customer artifact;
- keep public output redacted by default;
- do not add telemetry or network fallback;
- document any new claim boundary;
- preserve deterministic output where the contract promises it.

## Detection rules

A proposed rule needs:

- a named provider or format with a public source;
- positive and independent negative controls;
- a bounded pattern or parser;
- a clear severity rationale;
- tests for redaction and pathological input.

## Proprietary product boundary

Contributions to this MIT Scanner repository remain MIT. MCPwatch Scanner and AI Change Firewall are separately maintained paid proprietary products; do not submit their code, schemas, private rules, buyer artifacts or evidence here.

By contributing, you certify that you have the right to submit the contribution under the repository’s MIT License.
