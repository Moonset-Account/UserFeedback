package zoned

import (
	"errors"
	"sort"
	"sync"
	"testing"
)

func blocks(n int) []byte {
	return make([]byte, n*BlockSize)
}

func mustNew(t *testing.T, cfg Config) *Device {
	t.Helper()
	d, err := New(cfg)
	if err != nil {
		t.Fatalf("New(%+v) returned error: %v", cfg, err)
	}
	return d
}

func mustAppend(t *testing.T, d *Device, zone, n int) Result {
	t.Helper()
	r, err := d.Append(zone, blocks(n))
	if err != nil {
		t.Fatalf("Append(zone=%d, blocks=%d) returned error: %v", zone, n, err)
	}
	if r.Zone != zone || r.Blocks != n {
		t.Fatalf("Append(zone=%d, blocks=%d) = %+v, wrong zone/blocks", zone, n, r)
	}
	return r
}

func wantErr(t *testing.T, err, target error) {
	t.Helper()
	if !errors.Is(err, target) {
		t.Fatalf("error = %v, want errors.Is(_, %v)", err, target)
	}
}

func mustState(t *testing.T, d *Device, zone int) ZoneState {
	t.Helper()
	s, err := d.State(zone)
	if err != nil {
		t.Fatalf("State(%d) returned error: %v", zone, err)
	}
	return s
}

// --- construction -----------------------------------------------------------

func TestNewValidation(t *testing.T) {
	cases := []struct {
		name string
		cfg  Config
	}{
		{"zero zones", Config{ZoneCount: 0, ZoneBlocks: 4, MaxOpenZones: 1, MaxActiveZones: 1}},
		{"zero zone blocks", Config{ZoneCount: 2, ZoneBlocks: 0, MaxOpenZones: 1, MaxActiveZones: 1}},
		{"zero max open", Config{ZoneCount: 2, ZoneBlocks: 4, MaxOpenZones: 0, MaxActiveZones: 1}},
		{"max open above zone count", Config{ZoneCount: 2, ZoneBlocks: 4, MaxOpenZones: 3, MaxActiveZones: 1}},
		{"zero max active", Config{ZoneCount: 2, ZoneBlocks: 4, MaxOpenZones: 1, MaxActiveZones: 0}},
		{"max active above zone count", Config{ZoneCount: 2, ZoneBlocks: 4, MaxOpenZones: 1, MaxActiveZones: 3}},
		{"capacities length mismatch", Config{ZoneCount: 2, ZoneBlocks: 4, Capacities: []int{4}, MaxOpenZones: 1, MaxActiveZones: 1}},
		{"capacity zero", Config{ZoneCount: 2, ZoneBlocks: 4, Capacities: []int{4, 0}, MaxOpenZones: 1, MaxActiveZones: 1}},
		{"capacity above zone blocks", Config{ZoneCount: 2, ZoneBlocks: 4, Capacities: []int{4, 5}, MaxOpenZones: 1, MaxActiveZones: 1}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if _, err := New(tc.cfg); !errors.Is(err, ErrInvalidConfig) {
				t.Fatalf("New(%+v) error = %v, want ErrInvalidConfig", tc.cfg, err)
			}
		})
	}
}

// --- normal path ------------------------------------------------------------

func TestSequentialAppendsReportStartBlocks(t *testing.T) {
	d := mustNew(t, Config{
		ZoneCount:      3,
		ZoneBlocks:     8,
		MaxOpenZones:   2,
		MaxActiveZones: 2,
	})

	r := mustAppend(t, d, 0, 3)
	if r.StartBlock != 0 {
		t.Fatalf("first append start = %d, want 0", r.StartBlock)
	}
	r = mustAppend(t, d, 0, 2)
	if r.StartBlock != 3 {
		t.Fatalf("second append start = %d, want 3", r.StartBlock)
	}

	// Zone 2 begins at 2*8 = 16 device blocks.
	r = mustAppend(t, d, 2, 1)
	if r.StartBlock != 16 {
		t.Fatalf("zone 2 first append start = %d, want 16", r.StartBlock)
	}
	r = mustAppend(t, d, 2, 1)
	if r.StartBlock != 17 {
		t.Fatalf("zone 2 second append start = %d, want 17", r.StartBlock)
	}

	s := mustState(t, d, 0)
	if s.WritePointer != 5 || s.RemainingBlocks != 3 || !s.Open || s.Full {
		t.Fatalf("zone 0 state = %+v, want pointer 5, remaining 3, open", s)
	}
	if got := d.Stats(); got != (Stats{TotalZones: 3, OpenZones: 2, ActiveZones: 2}) {
		t.Fatalf("stats = %+v", got)
	}
}

func TestAppendFillingZoneReleasesBothCounters(t *testing.T) {
	d := mustNew(t, Config{
		ZoneCount:      2,
		ZoneBlocks:     4,
		MaxOpenZones:   1,
		MaxActiveZones: 1,
	})

	// Filling an empty zone still has to acquire the single opening credit,
	// but on completion the zone is full and leaves both counters.
	r := mustAppend(t, d, 0, 4)
	if r.StartBlock != 0 {
		t.Fatalf("start = %d, want 0", r.StartBlock)
	}
	if got := d.Stats(); got != (Stats{TotalZones: 2, FullZones: 1}) {
		t.Fatalf("stats after fill = %+v, want full=1 with no open/active", got)
	}
	s := mustState(t, d, 0)
	if !s.Full || s.Open || s.Active || s.WritePointer != 4 || s.RemainingBlocks != 0 {
		t.Fatalf("filled zone state = %+v", s)
	}

	// The released credit lets the next empty zone open and fill in one append.
	r = mustAppend(t, d, 1, 4)
	if r.StartBlock != 4 {
		t.Fatalf("zone 1 start = %d, want 4", r.StartBlock)
	}
	if got := d.Stats(); got != (Stats{TotalZones: 2, FullZones: 2}) {
		t.Fatalf("stats = %+v", got)
	}
}

// --- capacity smaller than address length -----------------------------------

func TestCapacitySmallerThanZoneBlocksBoundary(t *testing.T) {
	d := mustNew(t, Config{
		ZoneCount:      2,
		ZoneBlocks:     8,
		Capacities:     []int{4, 8},
		MaxOpenZones:   2,
		MaxActiveZones: 2,
	})

	r := mustAppend(t, d, 0, 1)
	if r.StartBlock != 0 {
		t.Fatalf("start = %d, want 0", r.StartBlock)
	}

	// Four blocks do not fit in the three remaining blocks: whole append fails,
	// and the write pointer must not move.
	_, err := d.Append(0, blocks(4))
	wantErr(t, err, ErrNoSpace)
	s := mustState(t, d, 0)
	if s.WritePointer != 1 || s.RemainingBlocks != 3 {
		t.Fatalf("state after failed oversized append = %+v, want pointer 1 remaining 3", s)
	}

	// Exactly the remaining capacity fits and fills the zone.
	r = mustAppend(t, d, 0, 3)
	if r.StartBlock != 1 {
		t.Fatalf("start = %d, want 1", r.StartBlock)
	}
	s = mustState(t, d, 0)
	if !s.Full || s.WritePointer != 4 || s.RemainingBlocks != 0 {
		t.Fatalf("state after exact fill = %+v", s)
	}

	// Any further append fails; the zone is full even though its address
	// length (8) is larger than its writable capacity (4).
	_, err = d.Append(0, blocks(1))
	wantErr(t, err, ErrZoneFull)
}

func TestCapacityZeroIsRejected(t *testing.T) {
	if _, err := New(Config{
		ZoneCount: 1, ZoneBlocks: 8, Capacities: []int{0},
		MaxOpenZones: 1, MaxActiveZones: 1,
	}); !errors.Is(err, ErrInvalidConfig) {
		t.Fatalf("error = %v, want ErrInvalidConfig", err)
	}
}

// --- opening quota on empty / closed zones ----------------------------------

func TestEmptyZoneAppendNeedsOpenCreditEvenWhenFilling(t *testing.T) {
	d := mustNew(t, Config{
		ZoneCount:      2,
		ZoneBlocks:     4,
		Capacities:     []int{4, 1},
		MaxOpenZones:   1,
		MaxActiveZones: 2,
	})

	// Occupy the only open credit with a non-full zone.
	mustAppend(t, d, 0, 1)
	if got := d.Stats(); got != (Stats{TotalZones: 2, OpenZones: 1, ActiveZones: 1}) {
		t.Fatalf("stats = %+v", got)
	}

	// Zone 1 has a one-block capacity and the append would exactly fill it,
	// but no open credit is available, so the append fails atomically.
	_, err := d.Append(1, blocks(1))
	wantErr(t, err, ErrNoOpenQuota)
	s := mustState(t, d, 1)
	if s.WritePointer != 0 || s.RemainingBlocks != 1 || s.Open || s.Active {
		t.Fatalf("zone 1 state after failed first append = %+v, want untouched empty zone", s)
	}

	// Closing the non-full zone releases the open credit but keeps it active.
	if err := d.Close(0); err != nil {
		t.Fatalf("Close(0) error: %v", err)
	}
	if got := d.Stats(); got != (Stats{TotalZones: 2, ActiveZones: 1}) {
		t.Fatalf("stats after close = %+v", got)
	}

	// Now the filling append can open zone 1 and finishes it in one step.
	r := mustAppend(t, d, 1, 1)
	if r.StartBlock != 4 {
		t.Fatalf("zone 1 start = %d, want 4", r.StartBlock)
	}
	if got := d.Stats(); got != (Stats{TotalZones: 2, ActiveZones: 1, FullZones: 1}) {
		t.Fatalf("stats after filling append = %+v", got)
	}
}

func TestClosedZoneAppendNeedsOpenCredit(t *testing.T) {
	d := mustNew(t, Config{
		ZoneCount:      3,
		ZoneBlocks:     4,
		MaxOpenZones:   1,
		MaxActiveZones: 3,
	})

	mustAppend(t, d, 0, 1)
	if err := d.Close(0); err != nil {
		t.Fatalf("Close(0): %v", err)
	}
	// Zone 1 is now the sole open zone.
	if err := d.Open(1); err != nil {
		t.Fatalf("Open(1): %v", err)
	}
	// Zone 0 is closed and active, but reopening it needs the open credit.
	_, err := d.Append(0, blocks(1))
	wantErr(t, err, ErrNoOpenQuota)
	if s := mustState(t, d, 0); s.WritePointer != 1 {
		t.Fatalf("zone 0 pointer = %d, want 1", s.WritePointer)
	}

	if err := d.Close(1); err != nil {
		t.Fatalf("Close(1): %v", err)
	}
	r := mustAppend(t, d, 0, 1)
	if r.StartBlock != 1 {
		t.Fatalf("resumed append start = %d, want 1", r.StartBlock)
	}
}

// --- the combined open/active scenario from the task ------------------------

func TestFirstOpenAfterCloseDependsOnBothLimits(t *testing.T) {
	d := mustNew(t, Config{
		ZoneCount:      4,
		ZoneBlocks:     8,
		MaxOpenZones:   2,
		MaxActiveZones: 2,
	})

	mustAppend(t, d, 0, 4) // zone 0: open, active, half full
	mustAppend(t, d, 1, 4) // zone 1: open, active, half full
	if got := d.Stats(); got != (Stats{TotalZones: 4, OpenZones: 2, ActiveZones: 2}) {
		t.Fatalf("stats = %+v", got)
	}

	// Closing a half-full zone frees an opening credit but not an active slot.
	if err := d.Close(1); err != nil {
		t.Fatalf("Close(1): %v", err)
	}
	if got := d.Stats(); got != (Stats{TotalZones: 4, OpenZones: 1, ActiveZones: 2}) {
		t.Fatalf("stats after close = %+v", got)
	}

	// Zone 2 can get an open credit but not an active one, so first open fails.
	if err := d.Open(2); !errors.Is(err, ErrActiveLimit) {
		t.Fatalf("Open(2) error = %v, want ErrActiveLimit", err)
	}
	_, err := d.Append(2, blocks(1))
	wantErr(t, err, ErrActiveLimit)
	if s := mustState(t, d, 2); s.WritePointer != 0 || s.RemainingBlocks != 8 || s.Active {
		t.Fatalf("zone 2 state = %+v, want untouched empty zone", s)
	}

	// Finishing the still-open zone 0 makes it full: it leaves both counters.
	if err := d.Finish(0); err != nil {
		t.Fatalf("Finish(0): %v", err)
	}
	if got := d.Stats(); got != (Stats{TotalZones: 4, OpenZones: 0, ActiveZones: 1, FullZones: 1}) {
		t.Fatalf("stats after finish = %+v", got)
	}

	// Now zone 2 opens (one open credit, one active slot), but that uses both
	// remaining capacities: open=1/2, active=2/2.
	if err := d.Open(2); err != nil {
		t.Fatalf("Open(2) after finish: %v", err)
	}
	// Reopening the closed zone 1 takes the second opening credit.
	if err := d.Open(1); err != nil {
		t.Fatalf("Open(1) closed resume: %v", err)
	}
	if got := d.Stats(); got != (Stats{TotalZones: 4, OpenZones: 2, ActiveZones: 2, FullZones: 1}) {
		t.Fatalf("stats = %+v", got)
	}

	// Zone 3 fails on the open quota first (active is full too).
	if err := d.Open(3); !errors.Is(err, ErrNoOpenQuota) {
		t.Fatalf("Open(3) error = %v, want ErrNoOpenQuota", err)
	}

	// Freeing one opening credit is not enough; the active limit still blocks.
	if err := d.Close(1); err != nil {
		t.Fatalf("Close(1): %v", err)
	}
	if err := d.Open(3); !errors.Is(err, ErrActiveLimit) {
		t.Fatalf("Open(3) error = %v, want ErrActiveLimit", err)
	}

	// Finishing the closed zone 1 frees its active slot, so zone 3 finally opens.
	if err := d.Finish(1); err != nil {
		t.Fatalf("Finish(1): %v", err)
	}
	if err := d.Open(3); err != nil {
		t.Fatalf("Open(3) after second finish: %v", err)
	}
	if got := d.Stats(); got != (Stats{TotalZones: 4, OpenZones: 2, ActiveZones: 2, FullZones: 2}) {
		t.Fatalf("final stats = %+v", got)
	}
}

// --- close / finish / reset -------------------------------------------------

func TestClosePreservesWritePointer(t *testing.T) {
	d := mustNew(t, Config{
		ZoneCount: 1, ZoneBlocks: 8, MaxOpenZones: 1, MaxActiveZones: 1,
	})
	mustAppend(t, d, 0, 2)
	mustAppend(t, d, 0, 3)
	if err := d.Close(0); err != nil {
		t.Fatalf("Close: %v", err)
	}
	s := mustState(t, d, 0)
	if s.WritePointer != 5 || s.RemainingBlocks != 3 || s.Open || !s.Closed || !s.Active {
		t.Fatalf("state after close = %+v", s)
	}
	if got := d.Stats(); got != (Stats{TotalZones: 1, ActiveZones: 1}) {
		t.Fatalf("stats = %+v", got)
	}

	// Reopen and continue from the preserved pointer.
	if err := d.Open(0); err != nil {
		t.Fatalf("Open: %v", err)
	}
	s = mustState(t, d, 0)
	if s.WritePointer != 5 || !s.Open {
		t.Fatalf("state after reopen = %+v", s)
	}
	r := mustAppend(t, d, 0, 1)
	if r.StartBlock != 5 {
		t.Fatalf("resumed start = %d, want 5", r.StartBlock)
	}
}

func TestFinishMarksPartialZoneFull(t *testing.T) {
	d := mustNew(t, Config{
		ZoneCount: 2, ZoneBlocks: 8, MaxOpenZones: 1, MaxActiveZones: 1,
	})
	mustAppend(t, d, 0, 2)
	if err := d.Finish(0); err != nil {
		t.Fatalf("Finish(0): %v", err)
	}
	s := mustState(t, d, 0)
	if !s.Full || s.Active || s.Open || s.WritePointer != 2 || s.RemainingBlocks != 0 {
		t.Fatalf("state after finish = %+v", s)
	}
	if got := d.Stats(); got != (Stats{TotalZones: 2, FullZones: 1}) {
		t.Fatalf("stats = %+v", got)
	}

	// A partially written but finished zone cannot be appended to.
	_, err := d.Append(0, blocks(1))
	wantErr(t, err, ErrZoneFull)
	// It also cannot be opened before a reset.
	wantErr(t, d.Open(0), ErrZoneFull)

	// Reset returns it to empty with a zeroed pointer, and writing restarts at
	// the beginning of the zone.
	if err := d.Reset(0); err != nil {
		t.Fatalf("Reset(0): %v", err)
	}
	s = mustState(t, d, 0)
	if s.WritePointer != 0 || s.RemainingBlocks != 8 || s.Full || s.Active || s.Open {
		t.Fatalf("state after reset = %+v", s)
	}
	if got := d.Stats(); got != (Stats{TotalZones: 2}) {
		t.Fatalf("stats after reset = %+v", got)
	}
	r := mustAppend(t, d, 0, 1)
	if r.StartBlock != 0 {
		t.Fatalf("start after reset = %d, want 0", r.StartBlock)
	}
}

func TestFinishAndResetErrors(t *testing.T) {
	d := mustNew(t, Config{
		ZoneCount: 2, ZoneBlocks: 4, MaxOpenZones: 2, MaxActiveZones: 2,
	})
	wantErr(t, d.Finish(0), ErrNotActive)
	wantErr(t, d.Reset(0), ErrNotActive)
	wantErr(t, d.Close(0), ErrNotOpen)

	if err := d.Open(0); err != nil {
		t.Fatalf("Open(0): %v", err)
	}
	wantErr(t, d.Open(0), ErrAlreadyOpen)

	// Finish an open zone, then Finish again.
	if err := d.Finish(0); err != nil {
		t.Fatalf("Finish(0): %v", err)
	}
	wantErr(t, d.Finish(0), ErrAlreadyFull)

	// Finish a closed zone is allowed.
	mustAppend(t, d, 1, 1)
	if err := d.Close(1); err != nil {
		t.Fatalf("Close(1): %v", err)
	}
	if err := d.Finish(1); err != nil {
		t.Fatalf("Finish(closed 1): %v", err)
	}
	if got := d.Stats(); got != (Stats{TotalZones: 2, FullZones: 2}) {
		t.Fatalf("stats = %+v", got)
	}
}

func TestResetOpenZoneReleasesCounters(t *testing.T) {
	d := mustNew(t, Config{
		ZoneCount: 1, ZoneBlocks: 4, MaxOpenZones: 1, MaxActiveZones: 1,
	})
	mustAppend(t, d, 0, 1)
	if err := d.Reset(0); err != nil {
		t.Fatalf("Reset(open zone): %v", err)
	}
	if got := d.Stats(); got != (Stats{TotalZones: 1}) {
		t.Fatalf("stats after reset = %+v", got)
	}
	r := mustAppend(t, d, 0, 1)
	if r.StartBlock != 0 {
		t.Fatalf("start after reset = %d, want 0", r.StartBlock)
	}
}

// --- invalid input ----------------------------------------------------------

func TestAppendInvalidSize(t *testing.T) {
	d := mustNew(t, Config{
		ZoneCount: 1, ZoneBlocks: 4, MaxOpenZones: 1, MaxActiveZones: 1,
	})
	for _, n := range []int{0, BlockSize - 1, BlockSize + 1, 3 * BlockSize} {
		// 3*BlockSize is a valid multiple; keep only the invalid sizes below.
		if n%BlockSize == 0 && n != 0 {
			continue
		}
		if _, err := d.Append(0, make([]byte, n)); !errors.Is(err, ErrInvalidAppendSize) {
			t.Fatalf("Append(%d bytes) error = %v, want ErrInvalidAppendSize", n, err)
		}
	}
	if _, err := d.Append(0, nil); !errors.Is(err, ErrInvalidAppendSize) {
		t.Fatalf("Append(nil) error = %v, want ErrInvalidAppendSize", err)
	}
	if got := d.Stats(); got != (Stats{TotalZones: 1}) {
		t.Fatalf("invalid appends changed stats: %+v", got)
	}
}

func TestZoneOutOfRange(t *testing.T) {
	d := mustNew(t, Config{
		ZoneCount: 2, ZoneBlocks: 4, MaxOpenZones: 2, MaxActiveZones: 2,
	})
	for _, idx := range []int{-1, 2, 99} {
		wantErr(t, d.Open(idx), ErrZoneOutOfRange)
		wantErr(t, d.Close(idx), ErrZoneOutOfRange)
		wantErr(t, d.Finish(idx), ErrZoneOutOfRange)
		wantErr(t, d.Reset(idx), ErrZoneOutOfRange)
		_, err := d.Append(idx, blocks(1))
		wantErr(t, err, ErrZoneOutOfRange)
		_, err = d.State(idx)
		wantErr(t, err, ErrZoneOutOfRange)
	}
}

// --- concurrency ------------------------------------------------------------

func TestConcurrentAppendsSameZoneGetContiguousRanges(t *testing.T) {
	const capacity = 64
	const writers = 120
	d := mustNew(t, Config{
		ZoneCount: 1, ZoneBlocks: capacity, MaxOpenZones: 1, MaxActiveZones: 1,
	})

	type outcome struct {
		start int
		err   error
	}
	results := make([]outcome, writers)
	var wg sync.WaitGroup
	start := make(chan struct{})
	for i := 0; i < writers; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			<-start
			r, err := d.Append(0, blocks(1))
			results[i] = outcome{start: r.StartBlock, err: err}
		}(i)
	}
	close(start)
	wg.Wait()

	var starts []int
	fullErrs := 0
	for _, res := range results {
		switch {
		case res.err == nil:
			starts = append(starts, res.start)
		case errors.Is(res.err, ErrZoneFull):
			fullErrs++
		default:
			t.Fatalf("unexpected append error: %v", res.err)
		}
	}
	if len(starts) != capacity {
		t.Fatalf("successful appends = %d, want %d", len(starts), capacity)
	}
	if fullErrs != writers-capacity {
		t.Fatalf("ErrZoneFull count = %d, want %d", fullErrs, writers-capacity)
	}
	sort.Ints(starts)
	for i, s := range starts {
		if s != i {
			t.Fatalf("starts[%d] = %d, want %d (ranges must be contiguous and unique)", i, s, i)
		}
	}
	if got := d.Stats(); got != (Stats{TotalZones: 1, FullZones: 1}) {
		t.Fatalf("stats = %+v, want exactly one full zone", got)
	}
}

func TestConcurrentAppendsTwoZonesDoNotInterleave(t *testing.T) {
	const perZone = 16
	const writersPerZone = 16
	d := mustNew(t, Config{
		ZoneCount: 2, ZoneBlocks: perZone, MaxOpenZones: 2, MaxActiveZones: 2,
	})

	type outcome struct {
		zone  int
		start int
		err   error
	}
	results := make([]outcome, 2*writersPerZone)
	var wg sync.WaitGroup
	start := make(chan struct{})
	k := 0
	for zone := 0; zone < 2; zone++ {
		for w := 0; w < writersPerZone; w++ {
			i := k
			k++
			wg.Add(1)
			go func(zone, i int) {
				defer wg.Done()
				<-start
				r, err := d.Append(zone, blocks(1))
				results[i] = outcome{zone: zone, start: r.StartBlock, err: err}
			}(zone, i)
		}
	}
	close(start)
	wg.Wait()

	seen := make(map[int]map[int]bool)
	for _, res := range results {
		if res.err != nil {
			t.Fatalf("append to zone %d failed: %v", res.zone, res.err)
		}
		if seen[res.zone] == nil {
			seen[res.zone] = make(map[int]bool)
		}
		if seen[res.zone][res.start] {
			t.Fatalf("zone %d start block %d handed out twice", res.zone, res.start)
		}
		seen[res.zone][res.start] = true
	}
	for zone := 0; zone < 2; zone++ {
		if len(seen[zone]) != perZone {
			t.Fatalf("zone %d got %d starts, want %d", zone, len(seen[zone]), perZone)
		}
		base := zone * perZone
		for local := 0; local < perZone; local++ {
			if !seen[zone][base+local] {
				t.Fatalf("zone %d missing start block %d", zone, base+local)
			}
		}
	}
	if got := d.Stats(); got != (Stats{TotalZones: 2, FullZones: 2}) {
		t.Fatalf("stats = %+v", got)
	}
}

func TestConcurrentFirstAppendsRespectOpenQuota(t *testing.T) {
	const zones = 32
	d := mustNew(t, Config{
		ZoneCount: zones, ZoneBlocks: 4, MaxOpenZones: 1, MaxActiveZones: 4,
	})

	errs := make([]error, zones)
	var wg sync.WaitGroup
	start := make(chan struct{})
	for i := 0; i < zones; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			<-start
			_, errs[i] = d.Append(i, blocks(1))
		}(i)
	}
	close(start)
	wg.Wait()

	ok, quota := 0, 0
	for _, err := range errs {
		switch {
		case err == nil:
			ok++
		case errors.Is(err, ErrNoOpenQuota):
			quota++
		default:
			t.Fatalf("unexpected append error: %v", err)
		}
	}
	if ok != 1 {
		t.Fatalf("successful first appends = %d, want 1 (one open credit)", ok)
	}
	if quota != zones-1 {
		t.Fatalf("ErrNoOpenQuota count = %d, want %d", quota, zones-1)
	}
	if got := d.Stats(); got != (Stats{TotalZones: zones, OpenZones: 1, ActiveZones: 1}) {
		t.Fatalf("stats = %+v", got)
	}
}

func TestConcurrentMixedOperationsKeepCountersConsistent(t *testing.T) {
	d := mustNew(t, Config{
		ZoneCount: 8, ZoneBlocks: 4, MaxOpenZones: 3, MaxActiveZones: 5,
	})

	var wg sync.WaitGroup
	for zone := 0; zone < 8; zone++ {
		for w := 0; w < 8; w++ {
			wg.Add(1)
			go func(zone int) {
				defer wg.Done()
				_, _ = d.Append(zone, blocks(1))
				s, err := d.State(zone)
				if err != nil {
					t.Errorf("State(%d): %v", zone, err)
					return
				}
				if s.Full && s.Active {
					t.Errorf("zone %d is both full and active", zone)
				}
			}(zone)
		}
	}
	wg.Wait()

	stats := d.Stats()
	states := make([]ZoneState, stats.TotalZones)
	for i := range states {
		states[i] = mustState(t, d, i)
	}
	var open, active, full int
	for _, s := range states {
		if s.Open {
			open++
		}
		if s.Active {
			active++
		}
		if s.Full {
			full++
		}
	}
	if open != stats.OpenZones || active != stats.ActiveZones || full != stats.FullZones {
		t.Fatalf("stats %+v disagree with states open=%d active=%d full=%d", stats, open, active, full)
	}
	if stats.OpenZones > 3 || stats.ActiveZones > 5 {
		t.Fatalf("limits exceeded: %+v", stats)
	}
	if len(states) != 8 {
		t.Fatalf("zone count = %d, want 8", len(states))
	}
}
