# V5l — Multi-source snapshot quality metrics

## Scope

This stage reports deterministic coverage metrics from the local normalized database. It does not label the database complete and does not invent unavailable NVD/KEV/EPSS/CWE coverage.

## Metrics

- advisory count by source;
- unique CVE aliases;
- advisories without CVE;
- total aliases;
- affected packages and ranges;
- affected packages with PURL;
- range assertions with a fixed event;
- advisories with severity metadata;
- alias conflict count;
- active advisory relations;
- explicit unavailable-data counters for CWE, KEV, EPSS and CPE.

The report has a deterministic digest and a quality status. Empty databases fail the minimal quality gate; non-empty databases pass with warnings for unavailable optional enrichment.
