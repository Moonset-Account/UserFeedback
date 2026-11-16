# Detection Inventory — Stage 0 Batch 0.1

The authoritative inventory is generated from the real in-process registry,
not by scanning source text. The generator is deterministic and emits no
absolute paths or timestamps.

## Command

```bash
python3 tools/detection_inventory.py --output /tmp/detection-inventory.json
```

## Baseline reconciliation

- native detectors: **182**;
- line detectors: **176**;
- context detectors: **6**;
- secret: **134**;
- IaC/cloud plus containers/Kubernetes: **21**;
- CI/CD: **9**;
- supply chain: **9**;
- AI/MCP: **9**;
- generic configuration classifiers excluded: **5**;
- infrastructure partition: `I0=8` IaC/cloud and `C0=13` containers/Kubernetes.

The inventory records detector IDs, seeded family IDs, engine type, category,
domain, source commit and SHA-256 hashes for the source files used by the
registry (`src/patterns.py`, `src/rulepacks.py`).

## Scope note

The working tree is on branch `chore/secret-scanner-branding` at commit
`9600d488aede2ef4b819f58d88bbd6f62f98aad9`, while the immutable program
starting point `015cc64ed490d7219cfe03fb1c66c1d943f76760` is present in Git but
is not the current HEAD. This batch inventories the current canonical source
without changing or resetting it.

## Gate

`tests/test_detection_inventory.py` independently checks count reconciliation,
unique IDs, family seed mapping, absence of absolute paths/timestamps and
byte-identical regeneration.
