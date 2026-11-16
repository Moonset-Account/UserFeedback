# V7d — ransomware signal projection

V7d adds a canonical ransomware-campaign signal projection over the existing CISA KEV field `knownRansomwareCampaignUse`.

Canonical statuses:

- `known` — KEV explicitly reports `Known`, `Yes`, or boolean `true`;
- `not-known` — KEV explicitly reports `No` or boolean `false`;
- `unknown` — KEV explicitly reports `Unknown`;
- `not-listed` — no KEV ransomware field is available.

The projection distinguishes *not listed* from *not known*. It exposes the source and a bounded `action_signal`; it does not claim that every KEV item is actively exploited by ransomware, and it does not replace the underlying KEV record.

The signal is additive in `exploitation_intelligence_report(cve_id)` and does not affect advisory, component, vulnerability-match, baseline, or snapshot identity. No schema change is required.
