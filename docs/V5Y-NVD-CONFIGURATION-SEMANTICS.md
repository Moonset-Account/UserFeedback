# V5y — NVD configuration logic preservation

The offline NVD parser preserves configuration semantics instead of flattening them away.

Each parsed configuration retains its nodes. Each node contains:

- `operator`: `AND` or `OR`; omitted NVD operators default to `OR`;
- `negate`: boolean; omitted values default to `false`;
- `cpe_matches`: the normalized CPE matches belonging to that node;
- `children`: recursively preserved child nodes.

The legacy top-level `cpe_matches` list remains available for compatibility, while `nvd_normalized_report()` now includes the structured `configurations` field for consumers that need correct Boolean evaluation.

Malformed operators, non-boolean `negate`, non-list `children`, and malformed CPE matches fail closed with `ValueError`.
