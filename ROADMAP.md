# Public roadmap

This roadmap covers only the public Secret Scanner Engine. MCPwatch Scanner and AI Change Firewall are maintained as separate paid proprietary products and are not implemented in this repository.

## Now — Scanner flagship launch

- [x] MIT Secret Scanner Engine 3.0.1
- [x] strict bounded diff parser
- [x] pre-commit hook
- [x] composite GitHub Action
- [x] synthetic test corpus
- [x] first public tagged release
- [x] GitHub Private Vulnerability Reporting
- [x] opt-in offline vulnerability enrichment with separate baseline and active-snapshot boundary
- [x] versioned vulnerability schema status and NVD metadata readback (V5x–V5z)
- [ ] maintainer identity and verified support links
- [ ] short public demo that does not execute target code

## Candidate improvements — demand gated

- documented stable extension API for separately distributed add-ons;
- additional source-backed detectors with negative controls;
- more SARIF and GitHub annotations interoperability tests;
- performance corpus and exact published limits;
- compatibility matrix for supported Python/Git versions.

## Demand threshold

Do not build major public integrations until issues, reproducible use cases or user interviews establish demand. VS Code or additional agent integrations require at least 20 substantive requests or equivalent validated evidence.

## Non-goals

- publishing MCPwatch Scanner source;
- publishing AI Change Firewall source;
- claiming a clean scan proves security;
- claiming legal or regulatory compliance;
- executing untrusted target-project code;
- collecting secrets in a hosted service;
- manufacturing benchmark or efficacy claims without reproducible evidence.
