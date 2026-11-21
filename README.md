# Local Authoritative DNS Kernel

An in-memory, network-free authoritative DNS kernel for a single zone. It
answers `A`, `AAAA` and `TXT` queries, returns referrals at `NS` delegation
points, and implements the exact-name / empty-non-terminal / wildcard rules
described below. Everything runs locally against synthetic fixtures: there is
no socket, no resolver-over-the-network and no external data source.

## Entry points

| Purpose | Command |
| --- | --- |
| Run the acceptance suite | `python3 -m unittest discover -s tests -t . -v` |
| Query the sample zone file | `python3 -m authoritative_dns --zone zones/example.zone www.example. A` |
| Several queries at once | `python3 -m authoritative_dns --zone zones/example.zone b.example. A y.b.example. A child.example. A` |
| Machine-readable output | `python3 -m authoritative_dns --zone zones/example.zone --json y.example. A` |
| Scripted run without a file | `python3 -c 'from authoritative_dns.fixtures import sample_zone; print(sample_zone().lookup("www.example.","A"))'` |

`python3` is Python 3.11.6 in this workspace. The package has no third-party
dependencies; nothing is installed or fetched at test time.

## Library surface

`authoritative_dns.Zone` is the kernel:

```python
from authoritative_dns import Zone

zone = Zone("example.", default_ttl=300)
zone.add("example.", "A", "192.0.2.1")
zone.add("*.example.", "A", "192.0.2.99")
zone.add("x.b.example.", "A", "192.0.2.20")
zone.add("child.example.", "NS", "ns1.child.example.")

r = zone.lookup("y.b.example.", "A")
r.status      # "NXDOMAIN"  (b.example is a nearer empty node, no wildcard there)
r.records     # ()

zone.remove("x.b.example.", "A")
zone.lookup("y.b.example.", "A").status   # "ANSWER" from *.example.
```

`Zone.lookup(name, type)` returns a `Response` with:

* `status` — `ANSWER`, `NODATA`, `NXDOMAIN` or `REFERRAL`;
* `records` — the answer RRset, the child `NS` RRset for a referral, or empty;
* `source` — the node an answer came from (`None` for `NXDOMAIN`);
* `wildcard` — `True` when the records were synthesised from a wildcard node.

Mutation helpers (`add`, `add_record`, `remove`, `remove_record`) update the
RRset index and the existing-name index together, so empty nodes appear and
disappear with the data that justifies them. `Zone.records()`,
`Zone.rrsets()` and `Zone.existing_names()` expose the state for diagnostics.

## Resolution rules

Given `qname`/`qtype` inside the zone:

1. **Delegation.** If `qname` is at or below a non-apex name that owns `NS`
   records, return a `REFERRAL` with the closest such `NS` RRset. Nothing below
   the cut is answered or synthesised, and a wildcard never applies there.
   The apex `NS` RRset is ordinary apex data and is *not* a referral.
2. **Exact name.** If `qname` exists exactly: return its `qtype` RRset
   (`ANSWER`), or `NODATA` when the name has no such type. A name that owns no
   records at all but has descendants is an **empty non-terminal**; it exists,
   so it yields `NODATA`, never `NXDOMAIN`.
3. **Wildcard synthesis.** Otherwise take the *nearest existing ancestor* of
   `qname` — the closest one, measured in labels. Only that ancestor's direct
   child `*.<ancestor>` may answer. If it owns `qtype`, the records are
   returned under the original `qname` (`ANSWER`, `wildcard=True`); if it
   exists without `qtype`, the result is `NODATA`; if it does not exist, the
   result is `NXDOMAIN`. Wildcards higher up are never consulted, because the
   nearest existing ancestor already fixed the search.

Consequences enforced by tests:

* With both `*.example.` and `x.b.example.` present, `b.example.` is an empty
  node and `y.b.example.` has no `*.b.example.` child, so it is `NXDOMAIN` —
  the shallow `*.example.` cannot be used.
* After `x.b.example.` is deleted, `b.example.` stops existing, so the *same*
  query `y.b.example.` is then answered from `*.example.`.
* Adding/removing records moves names between `ANSWER`, `NODATA` and
  `NXDOMAIN` in lock-step with the empty-node index.

## Validation and refusals

`Record` and `Zone` reject bad input rather than storing it:

* addresses that are not valid IPv4/IPv6 for their type, and unsupported type
  mnemonics (`MX`, `CNAME`, ...) — `InvalidRecord`;
* malformed owner names (empty labels, illegal characters, over-long
  labels/names, a `*` that is not a whole label);
* duplicate records and RRsets with mismatched TTLs;
* names outside the zone origin — `OutOfZoneName`;
* adding data below a delegation point, adding non-`NS` data at a delegation
  point, and creating a delegation over existing child data — `ZoneError`.

Query types are not restricted to the three record types with data: asking for
`MX` at an existing name is a normal `NODATA` answer, while a malformed type
mnemonic is refused.

## Layout

```
authoritative_dns/
  names.py       name parsing, canonical form, ancestor/wildcard helpers
  records.py     Record model and validation (A/AAAA/TXT/NS)
  zone.py        Zone storage, empty-node index, lookup/resolution
  status.py      AnswerStatus enum and supported-type sets
  cli.py         zone-file parser and command-line entry point
  fixtures.py    sample_zone() mirroring zones/example.zone
zones/example.zone   synthetic zone fixture used by the CLI
tests/
  support.py             shared assertions over Response
  test_names.py          name parsing/canonicalisation
  test_records.py        record validation
  test_resolution.py     exact, NODATA, wildcard, delegation outcomes
  test_cross_constraints.py  nearest-ancestor / deletion / empty-node interlocks
  test_cli.py            zone-file loader and CLI output
```

## Zone-file format

One record per line; `;`/`#` start a comment, `shlex` quoting applies to TXT
character-strings, and these directives/records are understood:

```
; comment
$ORIGIN example.
$TTL 300
@            IN  SOA    ns hostmaster 1 3600 600 86400 300   ; skipped
@            IN  NS     ns1.example.
www          IN  A      192.0.2.10
*            IN  TXT    "wild"
child        IN  NS     ns1.child.example.
```

Owners are absolute with a trailing dot, or relative to `$ORIGIN`; `@` is the
origin. `SOA` and unsupported types are skipped so real-world-style fixtures
remain loadable.

## Verification scope

`tests/` contains 72 `unittest` cases with real assertions (no mocks of the
kernel, no network). They cover, per the task requirements:

* **Acceptable outcomes** — `A`/`AAAA`/`TXT` answers, case/dot-insensitive
  lookups, wildcard synthesis under the queried name, `NODATA` at an existing
  name, and referrals at/below `NS` delegation points.
* **Blocked/rejected outcomes** — invalid addresses and types, duplicates,
  TTL mismatch inside an RRset, malformed or out-of-zone names, data added
  below a cut or at a delegation point, and delegating a name that already
  holds data.
* **Boundary and interleaved cases** — empty non-terminals vs. wildcards, the
  `*.example.`/`x.b.example.` shadowing interlock, the same query changing
  outcome after a deletion, nearer wildcards winning or masking with
  `NODATA`, and empty-node add/remove transitions.

Run `python3 -m unittest discover -s tests -t . -v` to reproduce.
