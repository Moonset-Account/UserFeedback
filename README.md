# zonedappend — simulated zoned append device

A small Go (1.20.2) simulation of a device split into equal-length address
zones with whole-block append writes, per-zone writable capacity, an opening
quota, and an active-zone limit.

## Concepts

- **Zone**: `ZoneBlocks` blocks of address space. Each zone also has a writable
  `capacity` in blocks, which may be smaller than `ZoneBlocks`.
- **Append**: writes `len(data)/BlockSize` whole blocks (`BlockSize = 512`
  bytes; the bytes are validated but only block counts are simulated). The
  return value reports the actual device-wide `StartBlock`, which is
  `zone*ZoneBlocks + zone write pointer`.
- **Atomic append**: an append either succeeds completely or changes nothing.
  A failed append never moves the write pointer.
- **Contiguity**: a device-wide mutex makes every append atomic, so concurrent
  appends to the same zone each receive one contiguous, non-overlapping range.
- **Opening credit**: an append to an empty or closed-but-not-full zone must
  first acquire an opening credit (`MaxOpenZones`), even when that single
  append would fill the zone. Otherwise the append fails as a whole.
- **Active zone**: open zones and closed-but-not-full zones are both active
  (`MaxActiveZones`). A zone that becomes full leaves both the open and active
  counts.
- **Close**: closing a non-full zone preserves the write pointer. Closing frees
  an opening credit but the zone stays active.
- **Finish**: marks an active (open or closed) zone full directly, even if its
  writable capacity was not exhausted. It releases both counters.
- **Reset**: returns a zone to empty with write pointer 0; the only way to write
  a full zone from the start again.
- **Queries after failure**: `State(zone)` reports the write pointer and the
  remaining writable range, so the caller can inspect the unchanged state after
  a rejected append. `Stats()` reports device-wide counters.

## Public API

```go
const BlockSize = 512

type Config struct {
    ZoneCount      int
    ZoneBlocks     int   // address length of each zone, in blocks
    Capacities     []int // optional writable capacity per zone, each in [1, ZoneBlocks]
    MaxOpenZones   int   // in [1, ZoneCount]
    MaxActiveZones int   // in [1, ZoneCount]
}

func New(cfg Config) (*Device, error)

func (d *Device) ZoneCount() int
func (d *Device) ZoneBlocks() int

func (d *Device) Open(zone int) error
func (d *Device) Close(zone int) error
func (d *Device) Finish(zone int) error
func (d *Device) Reset(zone int) error

func (d *Device) Append(zone int, data []byte) (Result, error)
func (d *Device) State(zone int) (ZoneState, error)
func (d *Device) Stats() Stats
```

`Result` carries `{Zone, StartBlock, Blocks}`. `ZoneState` carries
`{Zone, ZoneBlocks, CapacityBlocks, WritePointer, RemainingBlocks, Open,
Closed, Full, Active}`. `Stats` carries
`{TotalZones, OpenZones, ActiveZones, FullZones}`.

Sentinel errors (match with `errors.Is`): `ErrInvalidConfig`,
`ErrZoneOutOfRange`, `ErrInvalidAppendSize`, `ErrZoneFull`, `ErrNoSpace`,
`ErrNoOpenQuota`, `ErrActiveLimit`, `ErrAlreadyOpen`, `ErrNotOpen`,
`ErrNotActive`, `ErrAlreadyFull`.

`Append` requires `len(data)` to be a positive multiple of `BlockSize`.
`Device` is safe for concurrent use.

## Reproduce

From this directory, with Go 1.20.2:

```sh
go test ./... -count=1 -v
go test ./... -count=1 -race
```

## What the tests actually cover

All of the following were executed and passed (see `zoned_test.go`):

**Normal**
- Sequential appends report correct start blocks (`TestSequentialAppendsReportStartBlocks`).
- Filling an empty zone in one append works and releases both counters
  (`TestAppendFillingZoneReleasesBothCounters`).
- Close preserves the write pointer and reopen resumes from it
  (`TestClosePreservesWritePointer`).
- Finish marks a partial zone full; Reset returns it to the start
  (`TestFinishMarksPartialZoneFull`).

**Boundary**
- Writable capacity smaller than the address length: oversized append fails
  with `ErrNoSpace` and leaves the pointer unchanged; exact-fit append fills the
  zone; later appends report `ErrZoneFull`
  (`TestCapacitySmallerThanZoneBlocksBoundary`).
- An append to an empty zone that would exactly fill it still needs an opening
  credit (`TestEmptyZoneAppendNeedsOpenCreditEvenWhenFilling`).
- An append to a closed zone needs an opening credit
  (`TestClosedZoneAppendNeedsOpenCredit`).
- Config validation for all invalid ranges and capacity lists
  (`TestNewValidation`, `TestCapacityZeroIsRejected`).
- Invalid append sizes and out-of-range zones (`TestAppendInvalidSize`,
  `TestZoneOutOfRange`).
- Close/Finish/Reset error paths (`TestFinishAndResetErrors`,
  `TestResetOpenZoneReleasesCounters`).

**Interleaved / concurrent**
- Concurrent appends to one zone produce unique contiguous start blocks and
  exactly one full zone from many writers
  (`TestConcurrentAppendsSameZoneGetContiguousRanges`).
- Concurrent appends to two zones do not interleave ranges
  (`TestConcurrentAppendsTwoZonesDoNotInterleave`).
- Concurrent first appends to many zones admit exactly one writer under a
  single opening credit (`TestConcurrentFirstAppendsRespectOpenQuota`).
- Mixed concurrent appends keep `Stats` consistent with per-zone `State`
  (`TestConcurrentMixedOperationsKeepCountersConsistent`).

**The scenario in the task** — closing a half-full zone and then trying to
open another zone depends on *both* limits — is covered by
`TestFirstOpenAfterCloseDependsOnBothLimits`: it exercises the open quota
failing first, then the active limit failing after one credit is freed, then
success after `Finish` releases an active slot.

## Verification scope / not done

- Executed: `go test ./... -count=1 -v` (all tests PASS) and
  `go test ./... -count=1 -race` (PASS).
- `gofmt -l .` reported no files.
- `go vet ./...` was **not** run (the command required approval in this
  environment).
- No real device, network, account, credential, or external dependency is
  used. Only synthetic in-process data is exercised. No dependencies were
  installed.
