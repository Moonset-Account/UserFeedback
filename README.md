# streaming EXCEPT ALL with spilled segments

Go 1.20.2 implementation of a streaming `EXCEPT ALL` operator for two large
inputs. It keeps only bounded row state in the operator, spills sorted runs to
sequential temporary segments, and uses repeated binary external merges.

No third-party dependencies are required.

## Semantics

Each input row is:

```go
type Row struct {
    Text   *string // nullable text key
    Num    int64   // integer equivalence column
    Source int64   // provenance; ignored by equality
}
```

- `Text` and `Num` determine set equality; `Source` is carried to output but
  never compared.
- Two `NULL` text values are equivalent for set operations. For sorting,
  `NULL` is ordered first.
- Text uses the supplied sort key/comparator. The convenience option
  `TextSortKey: strings.ToLower` enables case-insensitive/mixed-case matching;
  the default is binary lexicographic comparison.
- For every equal key group, each left copy and right copy cancels one-for-one.
  The result multiplicity is `max(leftCount-rightCount, 0)`.
- The surviving copies are the **earliest remaining left rows in original
  order**. Those rows are finally sorted back by their 1-based left line number,
  so the emitted stream is in original left-input order.
- `OutputRow.LeftLine` exposes the original left line for verification. The
  original `Source` value is also preserved.

## Public API

The main entry point is:

```go
func Run(
    ctx context.Context,
    left RowReader,
    right RowReader,
    options Options,
    yield func(OutputRow) error,
) (Report, error)
```

Important types and functions:

- `Row`, `OutputRow{Row, LeftLine}`.
- `RowReader`; built-ins `SliceReader`, `RowReaderFunc`, and
  `GeneratorReader`.
- `Options`:
  - `SegmentCapacity`: bounded batch/initial segment size. Minimum `3`; zero
    uses `1024`. Tests set it to `3`.
  - `Segments`: optional `SegmentManager`. If omitted, `Run` creates a private
    file-backed manager under `TempDir` (default `./.exceptall-tmp`) and
    removes it on return.
  - `TempDir`: base directory for the default file-backed segments.
  - `TextSortKey func(string) string`: maps text to a comparable sort key.
  - `CompareText func(a, b string) int`: full text comparator; takes
    precedence over `TextSortKey`.
  - `CompareBusinessKey func(a, b Row) int`: optional full key comparison. It
    must implement `NULL = NULL` and must not compare `Source`.
- `Report`: input/output counts, segment counts, binary merge count, and
  maximum merge rounds.
- Temporary segment API:
  - `SegmentManager.CreateSegment(ctx) (Segment, error)`
  - `Segment.Append(ctx, Record) error`
  - `Segment.OpenReader(ctx) (SegmentReader, error)`; repeated opens return
    independent sequential readers.
  - `Segment.Release() error`
  - `SegmentReader.Next(ctx) (Record, bool, error)` and `Close()`.
- Built-in managers:
  - `NewFileSegmentManager(dir)`: JSON-lines segment files; `Release` closes
    and removes a segment.
  - `NewMemorySegmentManager()`: deterministic in-memory segment double with
    `CreatedSegments`, `ReleasedSegments`, and `ActiveSegments`. This is
    useful for tests, but segment payloads remain in process memory; use the
    file manager for actual disk spill.

`Run` releases every created segment on success, consumer error, and context
cancellation. The default private run directory is also removed.

## How the bounded execution works

1. Read each input once, assigning left original line numbers and side-local
   line numbers.
2. Build sorted runs of at most `SegmentCapacity` rows and spill each run to a
   `Segment`.
3. Repeatedly merge run pairs. Binary merging needs only two current head
   records. Equal business keys are tie-broken by line number, so an
   all-identical-key input still makes forward progress and terminates.
4. Merge the sorted left and right streams to count copies per key and write
   `Keep = max(left-right, 0)`.
5. Scan left rows in `(business key, original line)` order and emit only ranks
   `1..Keep`; these are exactly the earliest surviving left copies.
6. Externally sort the emitted rows by `LeftLine` and stream them to `yield`.

With `SegmentCapacity = 3`, sorted batches contain at most three rows; binary
merge state uses two heads, and the count/decision phase uses two heads plus
one group/decision record. Segment handles and external segment payloads are
not counted as resident input rows. No runtime RSS bound was measured in the
tests; the bound verified here is the algorithm's live row/batch state.

## Example

```go
package main

import (
    "context"
    "fmt"
    "strings"

    "exceptall"
)

func main() {
    hello := "Hello"
    helloLower := "hello"

    left := exceptall.NewSliceReader([]exceptall.Row{
        {Text: &hello, Num: 1, Source: 101},
        {Text: &helloLower, Num: 1, Source: 102},
    })
    right := exceptall.NewSliceReader([]exceptall.Row{
        {Text: &helloLower, Num: 1, Source: 201},
    })

    report, err := exceptall.Run(
        context.Background(),
        left,
        right,
        exceptall.Options{
            SegmentCapacity: 3,
            TextSortKey:     strings.ToLower, // "Hello" and "hello" match
        },
        func(row exceptall.OutputRow) error {
            fmt.Printf("left line %d: %+v\n", row.LeftLine, row.Row)
            return nil
        },
    )
    if err != nil {
        panic(err)
    }
    fmt.Printf("output rows: %d, merges: %d\n", report.OutputRows, report.MergeOperations)
}
```

## Run and verify

Requirements: Go 1.20.x. Development verification used
`go version go1.20.2 darwin/arm64`.

```sh
go test ./... -count=1
go test -race ./... -count=1
go test ./... -count=1 -cover
```

All tests use local synthetic data only. They do not access a database,
network, real target, or credentials.

## Verification scope

The test suite actually executes and asserts the following:

- Basic one-for-one cancellation, source-field indifference, and preservation
  of the earliest remaining left copies.
- Boundary cases: empty left, empty right, exact cancellation, right count
  greater than left, `NULL = NULL`, `NULL` versus non-`NULL`, and integer
  distinction.
- `SegmentCapacity: 3` with 65 left and 41 right synthetic rows, requiring
  multiple merge rounds. The same data is checked under binary and
  case-folding/mixed-case collations, including `NULL` values. Output line
  numbers are compared against an independent map-based reference
  implementation.
- A file-backed end-to-end run is compared with the same reference and asserts
  that all segment files are removed.
- 24 deterministic randomized/interleaved seeds with capacity 3 are compared
  against the reference for exact and folded text behavior.
- An all-identical-key case with 601 left and 239 right rows completes under a
  timeout and produces exactly left lines `1..362`, proving tie-broken merge
  termination and earliest-copy retention.
- Context cancellation after segment allocation asserts
  `errors.Is(err, context.Canceled)` and
  `CreatedSegments == ReleasedSegments`, `ActiveSegments == 0`.
- File-segment cancellation asserts the segment directory has no leftover
  files.
- Consumer callback failure also releases all segments.
- Invalid capacity below 3 returns/wraps `ErrInvalidOptions`.
- Memory and file segment implementations pass sequential write/read/round-trip
  tests, including a nullable text and decision `Keep` value.
- The default omitted `Segments` path creates and removes its private local
  file-backed run directory.

Recent actual result:

```text
$ go test ./... -count=1
ok  	exceptall	...

$ go test -race ./... -count=1
ok  	exceptall	...

$ go test ./... -count=1 -cover
ok  	exceptall	...	coverage: 78.4% of statements
```

The tests do not claim production-scale disk or latency validation, RSS
measurement, crash/fsync failure injection, or coverage of every possible
custom comparator. They verify the specified semantics, repeated merges,
mixed-case/NULL behavior, original line numbers, termination on a universal
key, and cancellation/cleanup through actual execution.
