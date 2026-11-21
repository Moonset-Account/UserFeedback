# chunkstore — incremental content-defined chunking writer

A small, self-contained Go 1.20 library for a local, in-memory object store.
It implements an **incremental chunking writer**: it accepts an object's bytes
in fragments of *any* size, splits the stream into content-defined chunks, and
records an ordered **manifest** of chunk lengths and content digests. Chunks
are content-addressed by SHA-256, so a chunk that appears in more than one
object (or more than once in one object) is **stored exactly once**.

The point of content-defined chunking is that a boundary depends on the bytes
around it, not on absolute offsets. Inserting a few bytes at the head of an
object therefore perturbs only the first chunks; the trailing chunks keep their
old digests and are shared with earlier versions.

## Entry points

| Symbol | What it does |
| --- | --- |
| `chunkstore.NewStore()` | Creates the in-memory content-addressed store. |
| `(*Store).NewWriter(Config)` | Validates the cut rules and returns a `Writer`. This is the chunking entry point. |
| `(*Writer).Write([]byte)` | Feeds a fragment of any size (including zero bytes). |
| `(*Writer).Close()` | Finalizes the object; emits a short final chunk if needed. Idempotent. |
| `(*Writer).Manifest()` | Returns a copy of the ordered manifest. Fails before `Close`. |
| `(*Store).ReadObject(Manifest)` | Rebuilds an object from the store, verifying every length and digest. |
| `(*Store).ReadObjectCrossCheck(Manifest)` | Rebuilds into a fresh store from the manifest alone. |
| `(*Store).Len()` | Number of **distinct** stored chunks. |
| `Manifest.Validate()` | Structural check: positive chunk lengths, hex SHA-256 digests, total == sum of chunk lengths. |

## Boundary rules

`Config` is supplied by the caller:

- `MinChunkLen` — a chunk is never cut before this many bytes (`>= 1`).
- `MaxChunkLen` — a chunk is cut unconditionally at this length (`>= MinChunkLen`).
- `WindowSize` — number of trailing bytes in the rolling window (`>= 1`).
- `Mask` — a content cut is accepted when `value & Mask == 0`.

The rolling value is the **unsigned 32-bit polynomial** of the most recent
`WindowSize` bytes in stream order, base **257**:

```
value = (((b0*257 + b1)*257 + b2) ... )*257 + b_{n-1}   (mod 2^32)
```

After appending a byte, a cut is made when **all** of these hold:

1. the current chunk has reached `MinChunkLen`,
2. the window has filled (`WindowSize` bytes have been seen in the stream),
3. `value & Mask == 0`.

If `MaxChunkLen` is reached first, the chunk is cut regardless of the value.

The window is **not** reset at a chunk boundary — it keeps sliding across the
whole stream. Only the current chunk length restarts at zero after a cut. This
is what makes the boundaries depend on content alone, and it is why feeding the
same bytes one at a time or all at once produces the **same manifest**.

By construction the window is full whenever `WindowSize` bytes have been seen, so
in practice condition 2 only delays the very first possible cut. If
`WindowSize > MaxChunkLen` the window cannot fill before a forced cut, and the
first cut is always forced.

The final remainder after the last cut is emitted as the last chunk even if it
is shorter than `MinChunkLen`. An empty object yields an empty manifest and
zero chunks.

## Reading objects back

`Manifest` is ordered and self-sufficient: it records each chunk's length and
SHA-256 digest plus the total object length. `ReadObject` verifies that every
referenced chunk exists, matches its recorded length, and hashes to its recorded
digest before concatenating. Empty objects and short tails round-trip.

## Verification

Run from this directory:

```
go test ./...
```

The acceptance tests live in `chunkstore_test.go` and `example_test.go`. They
assert real behaviour — no test passes by inspecting itself. Coverage:

**Accepted results**

- `TestSegmentSizeIndependence` — one write, one byte at a time, and several
  mixed/zero-length segmentations all produce byte-identical manifests.
- `TestHeadInsertionSharesTrailingChunks` — a 3-byte head insertion leaves all
  but the first chunk shared; the store holds exactly the number of distinct
  digests, both objects round-trip, and a fresh store rebuilds them from the
  manifest alone.
- `TestRepeatedContentStoredOnce` — four identical chunks are stored once.
- `TestEmptyObjectYieldsEmptyManifest` — empty input gives an empty manifest,
  zero chunks, and reads back as zero bytes.
- `TestShortTailIsReadable` — inputs shorter than `MinChunkLen` round-trip.
- `TestMultiChunkWithShortTailRoundTrips` — a multi-chunk object with a short
  final chunk round-trips.

**Rejected / blocked results**

- `TestConfigValidationRejectsBadRules` — zero/negative min, max below min, and
  zero/negative window are rejected with `ErrInvalidConfig`.
- `TestManifestBeforeCloseRejected` — `Manifest()` before `Close()` returns
  `ErrNotFinalized`.
- `TestWriteAfterCloseRejected` — `Write()` after `Close()` returns
  `ErrWriterClosed` and reports zero bytes written.
- `TestReadObjectRejectsTamperedManifest` — a manifest with a missing digest, a
  wrong chunk length, or a total that disagrees with the chunk sum is rejected.

**Boundary / interleaving scenarios**

- `TestRollingHashMatchesPolynomialDefinition` and
  `TestRollingHashMatchesReferenceAtEveryByte` — the rolling hash is checked
  byte by byte against an independent polynomial recomputation for window sizes
  1, 2, 3, 5, 16, and 64.
- `TestBoundariesMatchReferenceAcrossConfigsAndInputs` — a table over 7 configs,
  19 input sizes, and 3 input flavours (random, all-zero, counting-with-zeros),
  each fed in 3 segmentations, is compared to a naive full-recomputation oracle.
- `TestCutRequiresMinimumAndFullWindow` /
  `TestCutRequiresMinimumEvenWhenWindowFull` — the minimum-length gate holds even
  when the window is already full.
- `TestWindowLargerThanMaxDefersContentCuts`, `TestMinEqualsMaxProducesFixedChunks`,
  `TestMaxChunkLenForcesCut`, `TestSingleByteMaskCutsEveryByte` — forced-cut and
  degenerate-config edges.
- `TestCutHappensExactlyAtZeroHashWithAllOnesMask` — a cut fires exactly where the
  polynomial is zero.
- `TestWindowContinuesAcrossChunkBoundary` — the rolling window carries across
  boundaries, matching the reference.
- `Example` — an executable example that builds two objects in fragments, shows
  the distinct-chunk count below the total reference count, and verifies the
  rebuilt bytes.

`Manifest.Validate`, `ReadObject`, `ReadObjectCrossCheck`, and `Store.Len` are
exercised by these tests; `go vet` and `gofmt` are clean.
