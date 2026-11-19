# IPv4/UDP endpoint anonymizer

A small Go library that rewrites the endpoint addresses of IPv4/UDP packet
samples so the result can be shared with a support team without exposing
internal addresses. The payload is assumed to be address-free already; only the
two IP addresses are touched.

## Entry point

Package `anonymize` (module `example.com/ipv4udp-anonymizer`), Go 1.20.

| API | Purpose |
| --- | --- |
| `Translate(pkt []byte, captureLen int, table Table, link LinkLayer) ([]byte, error)` | Core entry point. `link` is `LinkEthernet` or `LinkRawIPv4`. |
| `TranslateEthernet(frame []byte, captureLen int, table Table) ([]byte, error)` | Convenience wrapper for Ethernet II frames (with optional stacked VLAN tags). |
| `TranslateRawIPv4(packet []byte, captureLen int, table Table) ([]byte, error)` | Convenience wrapper for samples starting at the IPv4 header. |
| `Table map[Addr]Addr` | One-to-one original → replacement address map. |
| `ValidateTable(Table) error` | Rejects empty and non-injective tables. |
| `ParseAddr(string) (Addr, error)` | Dotted-quad helper. |
| Sentinel errors | `ErrTruncatedHeader`, `ErrNotIPv4`, `ErrNotUDP`, `ErrFragmented`, `ErrInvalidLengths`, `ErrInvalidHeader`, `ErrBadChecksum`, `ErrMissingMapping`, `ErrInvalidTable`, `ErrInvalidCaptureLen`, `ErrUnsupportedLink` — match with `errors.Is`. |

### Semantics

* **Input preconditions.** The sample must be an unfragmented IPv4/UDP packet
  (DF or no fragment bits) whose IPv4 header checksum, and whose UDP checksum
  when the whole datagram is present, verify. `captureLen == 0` means the whole
  slice was captured; bytes beyond `captureLen` are never inspected.
* **Only two fields change.** The IPv4 source/destination addresses are
  replaced; the IPv4 header checksum and, when the original UDP checksum was
  enabled, the UDP checksum are recomputed. IPv4 total length, UDP length, the
  captured payload and all link-layer framing are byte-identical.
* **Partial payload captures.** A sample may contain the complete UDP header
  and only part of the payload. Because only the pseudo-header changes, the
  UDP checksum is updated incrementally
  ([RFC 1624](https://www.rfc-editor.org/rfc/rfc1624)) without reading payload
  bytes. The result is therefore identical to translating the full datagram and
  then truncating it to `captureLen` — order of operations does not matter.
* **Checksum-disabled datagrams.** A stored UDP checksum of `0x0000` means
  "disabled" in IPv4 and is copied through unchanged (all-zero stays all-zero).
* **Zero-result rule.** If the computed UDP checksum is zero, `0xffff` is
  stored instead, per RFC 768, for both full and truncated captures.
* **Fail closed.** Incomplete headers (sample or capture ends inside the
  Ethernet/VLAN, IPv4 or UDP header), missing endpoint mappings, invalid
  tables, fragmented or non-UDP/non-IPv4 packets, inconsistent lengths and bad
  original checksums all return `nil` bytes plus a wrapped sentinel error.
  A caller can never receive a fragment that still holds an original address.

## Verification

Run from the repository root:

```
go test ./...
```

`translate_test.go` builds every sample locally with synthetic fixtures (no
external captures, no network access) and asserts:

**Accepted results**
* `TestTranslateFullCaptureReplacesOnlyEndpointsAndChecksums` — byte-for-byte
  check that only the two addresses and the two checksums differ; original
  addresses absent from the output; input not mutated and output not aliased.
* `TestLinkVariants`, `TestIPv4OptionsHandled`, `TestSharedTableInterleavedFlows`
  — raw IPv4, 0/1/2 stacked VLAN tags, IPv4 options, and several interleaved
  flows sharing one table.
* `TestTranslateDoesNotMutateInput` — the input sample is never written to and
  the returned slice does not alias it.

**Rejected results**
* `TestRejectIncompleteHeadersWithoutLeakingOriginal` — capture or sample cut
  inside the IP/UDP header, empty sample, negative/oversized capture length;
  asserts nil bytes and no address leak.
* `TestRejectMissingMappings`, `TestValidateTableRejectsNonInjectiveTables` —
  missing source/destination mappings, empty table, two originals mapped to one
  replacement.
* `TestRejectBadOriginalChecksums`, `TestRejectFragmentedAndNonUDPAndWrongLengths`
  — corrupted IPv4/UDP checksums, MF/offset fragments, non-UDP protocol,
  non-IPv4 (ARP) EtherType, truncated Ethernet/VLAN headers, too-short IP
  header, mismatched IP/UDP lengths.

**Boundary / interleaved scenarios**
* `TestTranslateTruncatedHeaderMatchesFullThenTruncate` — full header with 0, 1
  and half payload bytes, one byte short, and full again: each result equals the
  full translation truncated to the same length.
* `TestTranslateTruncatedSampleSlice` — sample slice itself ends mid-payload.
* `TestBytesBeyondCaptureLengthAreIgnored` — trailing garbage past `captureLen`
  cannot change validation or output.
* `TestEthernetPaddingPreserved` — Ethernet minimum-frame padding beyond the IP
  total length is copied verbatim and excluded from the UDP checksum.
* `TestTranslateDisabledUDPChecksumStaysZero` — `0x0000` preserved for full and
  truncated captures.
* `TestTranslateZeroComputedUDPChecksumIsAllOnes` — zero result stored as
  `0xffff`, verified for full and truncated paths.
* `TestTransmittedAllOnesChecksumRoundTrips` — an input already carrying
  `0xffff` verifies, stays valid after rewriting, and an inconsistent `0xffff`
  is rejected.
* `TestIncrementalUpdateMatchesFullRecomputation` — sweep of endpoint pairs,
  replacement pairs and payload lengths (including odd lengths, `0.0.0.0` and
  `255.255.255.255`) cross-checking incremental updates against full
  recomputation.

Last verified locally: `go test ./...` and `go test -race ./...` pass on Go
1.20.2 (darwin/arm64), statement coverage 96.4%.
