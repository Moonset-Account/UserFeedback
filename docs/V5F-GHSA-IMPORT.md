# V5f — Offline GitHub Advisory Database parser/import

## Scope

This bounded batch parses one GitHub Advisory Database JSON object and imports only the fields explicitly present in the local fixture.

## Preserved fields

- `ghsa_id` as native advisory ID;
- CVE and identifier aliases;
- summary and description;
- published/updated/withdrawn timestamps;
- severity and CVSS metadata;
- references;
- vulnerable package ranges;
- complete canonical source record under `github-advisory` provenance.

## Range policy

Only the bounded, lossless range forms supported by the existing matcher are converted to events. Unsupported GitHub range expressions remain metadata and do not create a guessed active package assertion.

## Non-goals

- GitHub API/network access;
- authentication or token handling;
- automatic CVE/GHSA merge;
- CVSS recalculation;
- interpreting arbitrary Ruby/npm/PyPI range syntax;
- claiming that missing vulnerable package data means no vulnerability.
