# V7c — SSVC normalization and policy boundary

V7c normalizes SSVC decision-point content carried by CISA Vulnrichment.

Supported canonical points:

- `exploitation`: `none`, `poc`, `active`;
- `automatable`: `yes`, `no`;
- `technical_impact`: `partial`, `total`.

The projection deliberately returns:

- `decision: not_evaluable`;
- `missing_decision_points` for the organization-specific SSVC tree inputs not present in the supplied enrichment;
- `follow_up_signal: true` when exploitation is PoC/active, automation is possible, or technical impact is total.

This is not a fabricated remediation priority. The CISA SSVC calculator/tree requires additional organization/context decision points, so the implementation does not infer a final decision from three values alone.

The projection is additive and does not affect advisory, component, vulnerability-match, baseline, or snapshot identity. No schema change is required because the normalized projection is generated from the already stored Vulnrichment revision.

Source reference: [CISA SSVC resources](https://www.cisa.gov/resources-tools/resources/stakeholder-specific-vulnerability-categorization-ssvc).
