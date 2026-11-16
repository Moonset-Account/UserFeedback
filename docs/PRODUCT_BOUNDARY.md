# Product boundary

## Public MIT flagship

This repository contains the CodeRiskTools Secret Scanner Engine and its public delivery integrations:

- local Scanner Engine;
- pre-commit hook;
- GitHub Action;
- redacted output formatters;
- baselines, allowlists, bounded history and signed rule-pack support;
- synthetic tests and public documentation.

All source distributed in this repository is governed by its MIT License.

## Paid proprietary add-on: MCPwatch Scanner

MCPwatch Scanner is a paid add-on to the Scanner flagship. Its source, private rules, buyer artifacts, findings, test corpus and delivery packages must not be committed to this repository.

The public Scanner may expose stable extension points, but the proprietary add-on must consume them from a separately maintained private package or process.

## Separate paid proprietary product: AI Change Firewall

AI Change Firewall is not part of the open-source Scanner repository. Its proprietary scope includes, among other things:

- intent manifests and authorization workflows;
- allowed-scope and change-budget enforcement;
- deterministic `ALLOW/BLOCKED` decisions;
- signed or attestable receipts;
- evidence packs and policy packs;
- team and multi-project workflows.

No Firewall source, schemas, tests, rules, buyer archives or private evidence may be committed to this repository.

## Publication rule

The public repository is built from an explicit Scanner allowlist. Unknown top-level packages, archives, private keys, buyer directories and any path containing MCPwatch or Firewall implementation code fail the publication gate.

## Claim boundary

The Scanner provides bounded automated signals. It is not a security guarantee, audit, certification, legal opinion or proof of compliance. Commercial products may add workflow and evidence capabilities but do not convert detection results into guarantees.
