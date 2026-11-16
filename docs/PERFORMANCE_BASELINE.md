# Performance Baseline — Stage 6 Audit

The benchmark uses the real checkout evaluator:

```text
SecretScanner.scan_diff_text
```

It does not execute target-project code. The benchmark harness forces imports
from the canonical checkout and bounds the large diff below the scanner's
4 MiB input limit.

## Reference run

- Python: 3.11.15;
- iterations per case: 3;
- native rules: 192 line rules plus 6 context rules;
- projected rules: 1,500 by duplicating existing immutable rule objects only
  for workload measurement;
- resource metric: `ru_maxrss` delta;
- candidate checks upper bound: lines × active rules.

## Results

| Case | Rules | Lines | Input bytes | Median s | P95 s | Max s |
|---|---:|---:|---:|---:|---:|---:|
| clean_192 | 192 | 1,000 | 50,949 | 0.2834 | 0.2834 | 0.3158 |
| mixed_192 | 192 | 1,000 | 29,309 | 0.2934 | 0.2934 | 0.2960 |
| worst_regex_192 | 192 | 1,000 | 4,098,059 | 8.9152 | 8.9152 | 9.2413 |
| projected_1500_clean | 1,500 | 1,000 | 50,949 | 2.3738 | 2.3738 | 2.4810 |
| projected_1500_mixed | 1,500 | 1,000 | 29,309 | 2.2473 | 2.2473 | 2.3507 |
| diff_4mib_192 | 192 | 1,041 | 4,190,084 | 9.2822 | 9.2822 | 10.0058 |

Observed maximum RSS delta was **7,936 KiB** in the worst-regex case.

## Interpretation

The current real evaluator is acceptable as an immutable baseline, but it does
not meet the eventual strict-profile target of p95 below 2 seconds for a
projected 1500-rule workload or for a near-4 MiB worst-case input. This is a
measured Stage 2 optimization requirement, not a claim that the current
implementation is production-ready at 1500 rules.

The benchmark is reproducible with:

```bash
python3 tools/perf_baseline.py --output /tmp/perf-baseline.json --iterations 3
```
