# trustlist

A local trust-list matcher for a jump-host client. Before a connection is
allowed, the client hands the library the name it is connecting to, the port,
and the key material the peer presented, and gets back an allow/deny verdict
with the physical line number that produced it.

This is a defensive matching library. It does not probe anything, does not
resolve names, does not perform cryptography, and never talks to a network. It
judges only the data the caller already collected.

## Entry point

The library is a single Go package at the repository root:

```
module example.com/trustlist
```

- `LoadFile(path) (*List, error)` — read and compile a trust list from disk.
- `Parse(io.Reader) (*List, error)` — compile from any reader.
- `(*List).Evaluate(Request) Evaluation` — full verdict plus audit trail.
- `(*List).Allowed(Request) bool` — the verdict alone.
- `(*List).SetNow(time.Time)` — default clock for requests without `Now`.

The acceptance suite is the `*_test.go` files at the root. It uses only
`testing` from the standard library and is the authoritative statement of the
required behaviour.

```sh
go test ./...
```

Requires Go 1.20.2 (the version this was built and verified against). No
third-party dependencies; `go.mod` has no `require` block.

## Request

```go
type Request struct {
    Name        string           // connection name as typed, never resolved
    Port        int              // 1..65535
    HostKey     PublicKey        // set for a plain host key
    Certificate *HostCertificate // set for a host certificate
    Now         time.Time        // evaluation clock; wins over List.SetNow
}
```

Exactly one of `HostKey` and `Certificate` is expected. `HostCertificate`
carries the subject key, the issuer key, the principals, an inclusive
validity window, and a `SignatureVerified` flag that records the caller's
already-completed signature check. The library re-checks the window and the
principal coverage itself, because only the caller knows the connection name
and the evaluation time.

If neither `Request.Now` nor a list default clock is set, the request is
refused. Nothing is judged against a time nobody chose.

## Trust-list grammar

One entry per physical line:

```
<name-patterns> [port=<range>] <action> <key>
```

- `<name-patterns>` — comma-separated. A pattern starting with `!` is an
  exclusion. At least one positive pattern is required.
- `port=<range>` — `*`, a port, or an inclusive `lo-hi` range. Omitted means
  all ports.
- `<action>` — `pin`, `cert-authority`, or `revoke`.
- `<key>` — `<type>:<blob>` or `<type> <blob>`.

Blank lines and lines whose first non-space byte is `#` are ignored. Comments
and blank lines still consume line numbers, so reported provenance matches the
physical file (verified against a temp file with CRLF and comment fixtures).

Malformed lines are reported in `List.Issues()` and skipped. They never become
entries, so they can never be interpreted as a wildcard. Trailing junk after a
key is a syntax error rather than being glued onto the key blob.

Patterns are globs (`*`, `?`), CIDR blocks for IP-literal names, and otherwise
case-insensitive literals. Names are normalised: surrounding `[]` removed, one
trailing `.` removed, ASCII lower-cased. Patterns are matched against the
connection name only.

## Resolution

1. **Revocation** — any in-scope `revoke` whose key equals the presented key,
   or (for a certificate) its subject key or issuer key, denies. This pass runs
   first over the whole list and does not depend on line order.
2. **Pin** — in-scope `pin` lines. A matching key allows; if pin lines are in
   scope and none matches, the connection is denied (`key-mismatch`) rather
   than left undecided.
3. **Issuer trust** — in-scope `cert-authority` lines. The issuer must be
   trusted *and* the signature verified *and* the time inside the inclusive
   window *and* a principal must cover the connection name. Each is an
   independent gate; a list pattern that matches the name does not substitute
   for the certificate's own coverage.

No in-scope line at all denies (`no-trust-anchor`). The list is an allow list.

An excluded line grants and revokes nothing and does not influence other
lines; excluded lines are still reported in `Decision.ExcludedLines` for audit.
Two aliases of one address are judged independently, because the name is never
resolved. One name on two ports is judged independently, because the port is
part of a line's scope.

## Verdict fields

`Decision` carries `Allowed`, a stable `Reason` code, a human `Detail`, the
1-based `Line` responsible (0 when no line is), and `ExcludedLines`.
`Evaluation` adds `Issues` and `Inspected`. `Reason` values:
`pinned`, `certificate-trusted`, `revoked`, `no-trust-anchor`,
`port-out-of-scope`, `key-mismatch`, `issuer-not-trusted`,
`signature-unverified`, `certificate-outside-validity`, `name-not-covered`,
`invalid-request`.

## Verification scope

`go test ./...` is green with 52 test functions (61 cases including subtests),
covering both accepted and rejected outcomes plus the boundaries between them:

| Requirement | Accepted | Blocked | Boundary |
| --- | --- | --- | --- |
| Pin a host key | `TestPinAllowsExactHostKey` | `TestPinRejectsDifferentHostKey` | `TestKeyBlobIsCaseSensitive` |
| Trust an issuer | `TestCertAuthorityAllowsVerifiedCertificate` | `TestUntrustedIssuerRejected` | `TestUnverifiedSignatureRejected` |
| Certificate covers name | `TestCertificatePrincipalMayBeGlob` | `TestCertificateMustCoverConnectionName` | `TestListPatternDoesNotSubstituteForCertificateCoverage` |
| Validity window | middle of window | before `NotBefore`, at `NotAfter` | `TestCertificateValidityWindowIsInclusive` (±1ns) |
| Exclusion is per-line | `TestExclusionDropsOnlyItsOwnLine` | `TestExclusionDropsRevocationAndPinAlike` | `TestExclusionCannotContaminateAnotherLinesScope` |
| Revocation outranks trust | `TestRevocationIsScopedByNameAndPort` | `TestRevocationOverridesPinWhenListedFirst/Last` | `TestExcludedRevocationDoesNotOverrideValidIssuerTrust` |
| Aliases independent | `TestTwoAliasesOfOneAddressAreIndependent` | `TestAliasSharingRevokedKeyIsStillRevoked` | same-address, different names |
| Ports independent | `TestSameNameDifferentPortsAreIndependent` | `TestPortOutOfScopeIsDistinctFromNoAnchor` | `TestPortRangeBoundaries` (21/22/23) |
| Line provenance | `TestLineNumbersMatchPhysicalFile` | `TestCombinedScenarioKeepsProvenance` | `TestCRLFLineNumbersArePreserved` |
| Fail closed | `TestBlankLinesAndWhitespaceIgnored` | `TestMalformedLinesAreReportedAndSkipped`, `TestBadPatternNeverWidensScope` | `TestZeroPortRangeMatchesNothing` |

All fixtures are synthetic: literal key blobs, fake CAs, and a fixed
`2026-11-20T12:00:00Z` clock. No network, no real keys, no real hosts.
