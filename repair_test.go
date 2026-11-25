package gptrepair

import (
	"bytes"
	"encoding/binary"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"testing"
)

// ---- synthetic GPT builders -------------------------------------------------

// buildGPT writes a fully valid GPT pair (plus protective MBR) onto a fresh
// in-memory disk and returns it together with the raw canonical content.
func buildGPT(t *testing.T, totalSectors uint64, blockSize, entrySize int, entryCount uint32) (*MemoryDisk, canonical) {
	t.Helper()
	d, err := NewMemoryDisk(totalSectors, blockSize, true)
	if err != nil {
		t.Fatalf("NewMemoryDisk: %v", err)
	}
	g1, err := primaryGeometry(totalSectors, blockSize, entryCount, uint32(entrySize))
	if err != nil {
		t.Fatalf("primaryGeometry: %v", err)
	}
	g2, err := backupGeometry(totalSectors, blockSize, entryCount, uint32(entrySize))
	if err != nil {
		t.Fatalf("backupGeometry: %v", err)
	}
	diskGUID := guid(0x11, 0x22, 0x33)
	c := canonical{diskGUID: diskGUID, entryCount: entryCount, entrySize: uint32(entrySize)}
	c.entries = make([]byte, uint64(entryCount)*uint64(entrySize))
	mbr := buildProtectiveMBR(totalSectors)
	if blockSize > len(mbr) {
		padded := make([]byte, blockSize)
		copy(padded, mbr)
		mbr = padded
	}
	if err := d.WriteSectors(0, mbr); err != nil {
		t.Fatal(err)
	}
	writeSet(t, d, g1, g2, c, blockSize)
	writeSet(t, d, g2, g1, c, blockSize)
	return d, c
}

func writeSet(t *testing.T, d *MemoryDisk, self, peer geometry, c canonical, blockSize int) {
	t.Helper()
	if err := d.WriteSectors(self.entriesLBA, c.entries); err != nil {
		t.Fatal(err)
	}
	if err := d.WriteSectors(self.headerLBA, buildHeader(c, self, peer, blockSize)); err != nil {
		t.Fatal(err)
	}
}

func guid(seed ...byte) [16]byte {
	var g [16]byte
	for i := range g {
		g[i] = byte(i*7 + 1)
	}
	for i, v := range seed {
		g[i] = v
	}
	return g
}

// addPartition writes one non-empty entry at index idx with an explicit range.
func addPartition(c *canonical, idx uint32, typeGUID, partGUID [16]byte, first, last uint64, name string) {
	e := c.entries[idx*c.entrySize : (idx+1)*c.entrySize]
	copy(e[0:16], typeGUID[:])
	copy(e[16:32], partGUID[:])
	binary.LittleEndian.PutUint64(e[32:40], first)
	binary.LittleEndian.PutUint64(e[40:48], last)
	binary.LittleEndian.PutUint64(e[48:56], 1) // attributes
	// UTF-16LE name, best effort (ASCII).
	for i := 0; i < len(name) && i*2+1 < int(c.entrySize)-56; i++ {
		binary.LittleEndian.PutUint16(e[56+i*2:58+i*2], uint16(name[i]))
	}
}

// rebuild rewrites both copies of an existing canonical content onto a disk.
func rebuild(t *testing.T, d *MemoryDisk, c canonical) {
	t.Helper()
	total := d.TotalSectors()
	bs := d.SectorSize()
	g1, err := primaryGeometry(total, bs, c.entryCount, c.entrySize)
	if err != nil {
		t.Fatal(err)
	}
	g2, err := backupGeometry(total, bs, c.entryCount, c.entrySize)
	if err != nil {
		t.Fatal(err)
	}
	writeSet(t, d, g1, g2, c, bs)
	writeSet(t, d, g2, g1, c, bs)
}

func validatePair(t *testing.T, d SectorDisk) {
	t.Helper()
	insp, err := Inspect(d)
	if err != nil {
		t.Fatalf("Inspect after repair: %v (issues=%v)", err, insp.Issues)
	}
	if len(insp.Trustworthy) < 2 {
		t.Fatalf("want >=2 trustworthy copies, got %d (%+v)", len(insp.Trustworthy), insp.Issues)
	}
	for _, s := range insp.Trustworthy {
		if !s.Valid {
			t.Fatalf("source %d marked invalid", s.HeaderLBA)
		}
	}
}

// ---- crash-injecting wrapper ------------------------------------------------

type haltDisk struct {
	SectorDisk
	haltAfter int // <=0 means never halt
	writes    int
}

func (h *haltDisk) WriteSectors(lba uint64, src []byte) error {
	if err := h.SectorDisk.WriteSectors(lba, src); err != nil {
		return err
	}
	h.writes += len(src) / h.SectorSize()
	if h.haltAfter > 0 && h.writes >= h.haltAfter {
		return errHalted
	}
	return nil
}

func (h *haltDisk) ResizeSectors(n uint64) error {
	return h.SectorDisk.(ResizableDisk).ResizeSectors(n)
}

var errHalted = errors.New("simulated power loss after a full sector write")

// runWithCrashInjection runs Repair repeatedly on clones, halting after each
// possible sector-write count, then resumes on the same disk until success.
func runWithCrashInjection(t *testing.T, base *MemoryDisk, plan Plan) {
	t.Helper()
	// First measure the total write count of an uninterrupted run.
	probe := base.Clone()
	rep, err := Repair(&haltDisk{SectorDisk: probe}, plan)
	if err != nil {
		t.Fatalf("baseline repair failed: %v", err)
	}
	totalWrites := int(rep.SectorWrites)
	if totalWrites == 0 {
		// A no-op run has no crash points; still assert a repeat run works.
		if _, err := Repair(base.Clone(), plan); err != nil {
			t.Fatalf("idempotent repair: %v", err)
		}
		return
	}
	for halt := 1; halt <= totalWrites; halt++ {
		d := base.Clone()
		hd := &haltDisk{SectorDisk: d, haltAfter: halt}
		_, err := Repair(hd, plan)
		if err == nil {
			t.Fatalf("halt point %d: expected simulated failure", halt)
		}
		if !errors.Is(err, errHalted) {
			t.Fatalf("halt point %d: expected errHalted, got %v", halt, err)
		}
		// At every intermediate point at least one complete, self-consistent
		// old or new table must remain discoverable.
		insp, ierr := Inspect(d)
		if ierr != nil || len(insp.Trustworthy) == 0 {
			t.Fatalf("halt point %d: no trustworthy table after crash: inspectErr=%v issues=%v",
				halt, ierr, insp.Issues)
		}
		// Resume without the halt wrapper; it must converge to the target.
		resumeRep, err := Repair(d, plan)
		if err != nil {
			t.Fatalf("halt point %d: resume failed: %v", halt, err)
		}
		if resumeRep.FinalSectors != effectiveTarget(base.TotalSectors(), plan) {
			t.Fatalf("halt point %d: final sectors=%d want %d",
				halt, resumeRep.FinalSectors, effectiveTarget(base.TotalSectors(), plan))
		}
		validatePair(t, d)
	}
}

func effectiveTarget(old uint64, p Plan) uint64 {
	if p.TargetSectors == 0 {
		return old
	}
	return p.TargetSectors
}

// ---- tests ------------------------------------------------------------------

func TestRepairHealthyDiskIsNoOp(t *testing.T) {
	d, c := buildGPT(t, 2048, 512, DefaultEntrySize, DefaultEntryCount)
	addPartition(&c, 0, guid(1), guid(2), 34, 100, "root")
	addPartition(&c, 1, guid(3), guid(4), 101, 200, "data")
	rebuild(t, d, c)
	before := append([]byte(nil), d.Bytes()...)

	rep, err := Repair(d, Plan{})
	if err != nil {
		t.Fatalf("Repair: %v", err)
	}
	if rep.SectorWrites != 0 || rep.MBRWritten {
		t.Fatalf("healthy disk should need no writes, got writes=%d mbr=%v", rep.SectorWrites, rep.MBRWritten)
	}
	if !bytes.Equal(before, d.Bytes()) {
		t.Fatal("healthy disk bytes were modified")
	}
	validatePair(t, d)
}

func TestRepairCorruptPrimaryRebuildsFromBackup(t *testing.T) {
	d, c := buildGPT(t, 2048, 512, DefaultEntrySize, DefaultEntryCount)
	addPartition(&c, 0, guid(1), guid(2), 34, 1500, "root")
	rebuild(t, d, c)

	// Corrupt primary header CRC and primary entry array.
	raw := d.Bytes()
	raw[1*512+16] ^= 0xFF
	for i := 2 * 512; i < 34*512; i++ {
		raw[i] ^= 0x5A
	}
	insp, err := Inspect(d)
	if !errors.Is(err, ErrNoTrustworthySource) && insp != nil && len(insp.Trustworthy) != 1 {
		// exactly one trustworthy (backup) is also acceptable for repair
	}
	rep, err := Repair(d, Plan{})
	if err != nil {
		t.Fatalf("Repair with bad primary: %v", err)
	}
	if rep.SectorWrites == 0 {
		t.Fatal("expected writes to rebuild the primary copy")
	}
	validatePair(t, d)
	// Canonical content preserved.
	_, got := discoverMust(t, d)
	if !bytesEqual(got.entries, c.entries) || !equalGUID(got.diskGUID, c.diskGUID) {
		t.Fatal("rebuilt content differs from trusted source")
	}
}

func TestRepairCorruptBackupRebuildsFromPrimary(t *testing.T) {
	d, c := buildGPT(t, 2048, 512, DefaultEntrySize, DefaultEntryCount)
	addPartition(&c, 0, guid(9), guid(8), 34, 1234, "p")
	rebuild(t, d, c)

	last := d.TotalSectors() - 1
	raw := d.Bytes()
	raw[int(last)*512+20] ^= 0x01 // header CRC break
	if _, err := Repair(d, Plan{}); err != nil {
		t.Fatalf("Repair: %v", err)
	}
	validatePair(t, d)
	_, got := discoverMust(t, d)
	if !bytesEqual(got.entries, c.entries) {
		t.Fatal("entries changed")
	}
}

func TestArbitrationRequiredWhenCopiesContradict(t *testing.T) {
	d, c := buildGPT(t, 2048, 512, DefaultEntrySize, DefaultEntryCount)
	addPartition(&c, 0, guid(1), guid(2), 34, 500, "a")
	rebuild(t, d, c)

	// Build a contradictory but internally valid backup with a different disk
	// GUID (same geometry, so both CRCs remain valid).
	total := d.TotalSectors()
	bs := d.SectorSize()
	g1, _ := primaryGeometry(total, bs, c.entryCount, c.entrySize)
	g2, _ := backupGeometry(total, bs, c.entryCount, c.entrySize)
	conflict := c
	conflict.diskGUID = guid(0xEE)
	d.WriteSectors(g2.entriesLBA, conflict.entries)
	d.WriteSectors(g2.headerLBA, buildHeader(conflict, g2, g1, bs))

	before := append([]byte(nil), d.Bytes()...)
	_, err := Repair(d, Plan{TargetSectors: total})
	if !errors.Is(err, ErrArbitrationRequired) {
		t.Fatalf("want ErrArbitrationRequired, got %v", err)
	}
	if !bytes.Equal(before, d.Bytes()) {
		t.Fatal("arbitration case must perform no writes")
	}
	insp, _ := Inspect(d)
	if !insp.Contradiction || len(insp.Trustworthy) != 2 {
		t.Fatalf("inspection mismatch: contradiction=%v trusted=%d", insp.Contradiction, len(insp.Trustworthy))
	}
}

func TestArbitrationOnContradictoryEntries(t *testing.T) {
	d, c := buildGPT(t, 4096, 512, DefaultEntrySize, DefaultEntryCount)
	addPartition(&c, 0, guid(1), guid(2), 34, 500, "a")
	rebuild(t, d, c)

	g1, _ := primaryGeometry(d.TotalSectors(), 512, c.entryCount, c.entrySize)
	g2, _ := backupGeometry(d.TotalSectors(), 512, c.entryCount, c.entrySize)
	conflict := c
	var tg [16]byte = guid(0x77)
	addPartition(&conflict, 0, tg, guid(2), 34, 900, "b")
	d.WriteSectors(g2.entriesLBA, conflict.entries)
	d.WriteSectors(g2.headerLBA, buildHeader(conflict, g2, g1, 512))

	_, err := Repair(d, Plan{})
	if !errors.Is(err, ErrArbitrationRequired) {
		t.Fatalf("want arbitration, got %v", err)
	}
}

func TestNoTrustworthySourceWhenBothBrokenAndNoMixing(t *testing.T) {
	d, c := buildGPT(t, 2048, 512, DefaultEntrySize, DefaultEntryCount)
	addPartition(&c, 0, guid(1), guid(2), 34, 500, "a")
	rebuild(t, d, c)

	// Break the primary header but leave its (now unreferenced) entries
	// intact; break the backup entries but leave the backup header's stored
	// CRC stale. The repair must NOT combine the two partial copies.
	raw := d.Bytes()
	raw[1*512+16] ^= 0xFF // primary header CRC invalid
	g2, _ := backupGeometry(d.TotalSectors(), 512, c.entryCount, c.entrySize)
	for i := g2.entriesLBA * 512; i < (g2.entriesLBA+32)*512; i++ {
		raw[i] ^= 0x0F // backup entries no longer match EntriesCRC
	}
	before := append([]byte(nil), d.Bytes()...)
	_, err := Repair(d, Plan{})
	if !errors.Is(err, ErrNoTrustworthySource) {
		t.Fatalf("want ErrNoTrustworthySource, got %v", err)
	}
	if !bytes.Equal(before, d.Bytes()) {
		t.Fatal("no-source case must not write")
	}
}

func TestGrowRelocationKeepsDataAndGUIDs(t *testing.T) {
	const oldSize, newSize uint64 = 1024, 2048
	d, c := buildGPT(t, oldSize, 512, DefaultEntrySize, DefaultEntryCount)
	g1, _ := primaryGeometry(oldSize, 512, c.entryCount, c.entrySize)
	addPartition(&c, 0, guid(1), guid(2), g1.firstUsable, 400, "keep")
	// Stamp user bytes inside the partition and outside metadata areas.
	mark := []byte("USER-DATA-MARKER-0123456789")
	copy(d.Bytes()[400*512:], mark)
	copy(d.Bytes()[500*512:500*512+len(mark)], mark)
	rebuild(t, d, c)

	oldBackupLBA := oldSize - 1
	rep, err := Repair(d, Plan{TargetSectors: newSize})
	if err != nil {
		t.Fatalf("grow repair: %v", err)
	}
	if !rep.Grew || rep.FinalSectors != newSize {
		t.Fatalf("grow report wrong: %+v", rep)
	}
	// The old backup at the former end must have been removed once the new
	// pair was complete.
	oldHdr, _ := readSectors(d, oldBackupLBA, 1)
	if !isZero(oldHdr) {
		t.Fatal("stale backup header at former end was not removed")
	}
	validatePair(t, d)
	// User bytes untouched.
	if !bytes.Equal(d.Bytes()[400*512:400*512+len(mark)], mark) ||
		!bytes.Equal(d.Bytes()[500*512:500*512+len(mark)], mark) {
		t.Fatal("user data bytes changed during grow")
	}
	// Partition entry bytes and GUIDs preserved.
	_, got := discoverMust(t, d)
	if !bytesEqual(got.entries, c.entries) || !equalGUID(got.diskGUID, c.diskGUID) {
		t.Fatal("partition content/GUID changed during grow")
	}
}

func TestShrinkWhenAllPartitionsFit(t *testing.T) {
	const oldSize, newSize uint64 = 2048, 1200
	d, c := buildGPT(t, oldSize, 512, DefaultEntrySize, DefaultEntryCount)
	g1, _ := primaryGeometry(oldSize, 512, c.entryCount, c.entrySize)
	tpg, _ := primaryGeometry(newSize, 512, c.entryCount, c.entrySize)
	addPartition(&c, 0, guid(1), guid(2), g1.firstUsable, tpg.lastUsable-10, "small")
	mark := []byte("SHRINK-KEEP")
	markLBA := tpg.lastUsable - 10
	copy(d.Bytes()[markLBA*512:], mark)
	rebuild(t, d, c)

	rep, err := Repair(d, Plan{TargetSectors: newSize})
	if err != nil {
		t.Fatalf("shrink repair: %v", err)
	}
	if !rep.Shrank || rep.FinalSectors != newSize || d.TotalSectors() != newSize {
		t.Fatalf("shrink report wrong: %+v total=%d", rep, d.TotalSectors())
	}
	validatePair(t, d)
	if !bytes.Equal(d.Bytes()[markLBA*512:markLBA*512+uint64(len(mark))], mark) {
		t.Fatal("kept user bytes changed during shrink")
	}
	_, got := discoverMust(t, d)
	if !bytesEqual(got.entries, c.entries) || !equalGUID(got.diskGUID, c.diskGUID) {
		t.Fatal("partition content/GUID changed during shrink")
	}
}

func TestShrinkRejectedWhenPartitionCrossesNewEnd(t *testing.T) {
	const oldSize, newSize uint64 = 2048, 1024
	d, c := buildGPT(t, oldSize, 512, DefaultEntrySize, DefaultEntryCount)
	g1, _ := primaryGeometry(oldSize, 512, c.entryCount, c.entrySize)
	addPartition(&c, 0, guid(1), guid(2), g1.firstUsable, 1900, "too-big")
	rebuild(t, d, c)

	before := append([]byte(nil), d.Bytes()...)
	_, err := Repair(d, Plan{TargetSectors: newSize})
	if !errors.Is(err, ErrPartitionOverflow) {
		t.Fatalf("want ErrPartitionOverflow, got %v", err)
	}
	if d.TotalSectors() != oldSize || !bytes.Equal(before, d.Bytes()) {
		t.Fatal("overflow rejection must leave the disk untouched")
	}
}

func TestTargetTooSmallForMetadata(t *testing.T) {
	d, c := buildGPT(t, 2048, 512, DefaultEntrySize, DefaultEntryCount)
	rebuild(t, d, c)
	// 32 entry sectors + header at each end need at least 2*32+3 = 67 sectors.
	_, err := Repair(d, Plan{TargetSectors: 40})
	if !errors.Is(err, ErrGeometry) {
		t.Fatalf("want ErrGeometry, got %v", err)
	}
}

func TestCrashInjectionSameSize(t *testing.T) {
	d, c := buildGPT(t, 2048, 512, DefaultEntrySize, DefaultEntryCount)
	addPartition(&c, 0, guid(1), guid(2), 34, 1500, "p")
	rebuild(t, d, c)
	// Wipe the primary so the run has real work to do.
	for i := 512; i < 34*512; i++ {
		d.Bytes()[i] = 0
	}
	runWithCrashInjection(t, d, Plan{TargetSectors: 2048})
}

func TestCrashInjectionGrow(t *testing.T) {
	d, c := buildGPT(t, 1024, 512, DefaultEntrySize, DefaultEntryCount)
	g1, _ := primaryGeometry(1024, 512, c.entryCount, c.entrySize)
	addPartition(&c, 0, guid(1), guid(2), g1.firstUsable, 500, "p")
	mark := []byte("CRASH-GROW-DATA")
	copy(d.Bytes()[500*512:], mark)
	rebuild(t, d, c)
	runWithCrashInjection(t, d, Plan{TargetSectors: 1800})
	// Converged disk retains user data.
	if !bytes.Equal(d.Bytes()[500*512:500*512+len(mark)], mark) {
		t.Fatal("user data lost after grow crash recovery")
	}
}

func TestCrashInjectionShrink(t *testing.T) {
	const oldSize, newSize uint64 = 2048, 1100
	d, c := buildGPT(t, oldSize, 512, DefaultEntrySize, DefaultEntryCount)
	g1, _ := primaryGeometry(oldSize, 512, c.entryCount, c.entrySize)
	tpg, _ := primaryGeometry(newSize, 512, c.entryCount, c.entrySize)
	addPartition(&c, 0, guid(1), guid(2), g1.firstUsable, tpg.lastUsable, "p")
	rebuild(t, d, c)
	runWithCrashInjection(t, d, Plan{TargetSectors: newSize})
}

func TestFourKilobyteSectors(t *testing.T) {
	const total uint64 = 300
	d, c := buildGPT(t, total, 4096, DefaultEntrySize, DefaultEntryCount)
	g1, _ := primaryGeometry(total, 4096, c.entryCount, c.entrySize)
	addPartition(&c, 0, guid(1), guid(2), g1.firstUsable, 200, "big-sector")
	rebuild(t, d, c)
	validatePair(t, d)

	rep, err := Repair(d, Plan{TargetSectors: 420})
	if err != nil {
		t.Fatalf("4K grow: %v", err)
	}
	if rep.FinalSectors != 420 {
		t.Fatalf("unexpected final size %d", rep.FinalSectors)
	}
	validatePair(t, d)
	runWithCrashInjection(t, func() *MemoryDisk {
		dd, cc := buildGPT(t, total, 4096, DefaultEntrySize, DefaultEntryCount)
		gg, _ := primaryGeometry(total, 4096, cc.entryCount, cc.entrySize)
		addPartition(&cc, 0, guid(1), guid(2), gg.firstUsable, 200, "big-sector")
		rebuild(t, dd, cc)
		return dd
	}(), Plan{TargetSectors: 420})
}

func TestNonResizableBackendRejectsCapacityChange(t *testing.T) {
	d, c := buildGPT(t, 1024, 512, DefaultEntrySize, DefaultEntryCount)
	rebuild(t, d, c)
	fixed := &frozenDisk{MemoryDisk: d}
	if _, err := Repair(fixed, Plan{TargetSectors: 2048}); err == nil {
		t.Fatal("expected error when backend cannot resize")
	}
	// Same-size repair still works on a fixed backend.
	raw := d.Bytes()
	raw[512+16] ^= 0xFF
	if _, err := Repair(fixed, Plan{}); err != nil {
		t.Fatalf("same-size repair on fixed backend: %v", err)
	}
	validatePair(t, fixed)
}

type frozenDisk struct{ *MemoryDisk }

func (f *frozenDisk) ResizeSectors(uint64) error { return errors.New("resize not supported") }

func TestFileDiskReopenAcrossCrash(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "img.bin")
	const total, target uint64 = 1024, 1600

	// Materialize a valid GPT image via the memory backend, then copy bytes.
	d, c := buildGPT(t, total, 512, DefaultEntrySize, DefaultEntryCount)
	g1, _ := primaryGeometry(total, 512, c.entryCount, c.entrySize)
	addPartition(&c, 0, guid(1), guid(2), g1.firstUsable, 400, "file")
	rebuild(t, d, c)
	if err := os.WriteFile(path, d.Bytes(), 0o600); err != nil {
		t.Fatal(err)
	}

	// First run halts after two writes.
	fd, err := OpenFileDisk(path, 512, total)
	if err != nil {
		t.Fatal(err)
	}
	hd := &haltDisk{SectorDisk: fd, haltAfter: 2}
	if _, err := Repair(hd, Plan{TargetSectors: target}); !errors.Is(err, errHalted) {
		t.Fatalf("want halt, got %v", err)
	}
	if err := fd.Close(); err != nil {
		t.Fatal(err)
	}

	// Reopen at whatever size the file now has and resume.
	st, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	reopenSize := uint64(st.Size() / 512)
	fd2, err := OpenFileDisk(path, 512, reopenSize)
	if err != nil {
		t.Fatal(err)
	}
	rep, err := Repair(fd2, Plan{TargetSectors: target})
	if err != nil {
		t.Fatalf("resume after reopen: %v", err)
	}
	if err := fd2.Close(); err != nil {
		t.Fatal(err)
	}
	if rep.FinalSectors != target {
		t.Fatalf("final sectors=%d want %d", rep.FinalSectors, target)
	}
	fd3, err := OpenFileDisk(path, 512, target)
	if err != nil {
		t.Fatal(err)
	}
	defer fd3.Close()
	validatePair(t, fd3)
}

func TestInspectReportsIssues(t *testing.T) {
	d, c := buildGPT(t, 2048, 512, DefaultEntrySize, DefaultEntryCount)
	rebuild(t, d, c)
	d.Bytes()[int(d.TotalSectors()-1)*512+16] ^= 0xFF
	insp, err := Inspect(d)
	if err != nil {
		t.Fatalf("one good copy should still inspect: %v", err)
	}
	if len(insp.Trustworthy) != 1 || len(insp.Issues) == 0 {
		t.Fatalf("want 1 trusted + issues, got trusted=%d issues=%v", len(insp.Trustworthy), insp.Issues)
	}
}

func TestBoundaryPartitionAtUsableEdges(t *testing.T) {
	d, c := buildGPT(t, 2048, 512, DefaultEntrySize, DefaultEntryCount)
	g1, _ := primaryGeometry(2048, 512, c.entryCount, c.entrySize)
	addPartition(&c, 0, guid(1), guid(2), g1.firstUsable, g1.lastUsable, "edge")
	rebuild(t, d, c)
	if _, err := Repair(d, Plan{TargetSectors: 2048}); err != nil {
		t.Fatalf("edge partition repair: %v", err)
	}
	validatePair(t, d)
}

func TestRepeatedRepairsConverge(t *testing.T) {
	d, c := buildGPT(t, 1024, 512, DefaultEntrySize, DefaultEntryCount)
	g1, _ := primaryGeometry(1024, 512, c.entryCount, c.entrySize)
	addPartition(&c, 0, guid(1), guid(2), g1.firstUsable, 500, "p")
	rebuild(t, d, c)
	for i := 0; i < 3; i++ {
		if _, err := Repair(d, Plan{TargetSectors: 1500}); err != nil {
			t.Fatalf("iteration %d: %v", i, err)
		}
	}
	if d.TotalSectors() != 1500 {
		t.Fatalf("size=%d", d.TotalSectors())
	}
	validatePair(t, d)
	if _, err := Repair(d, Plan{TargetSectors: 1500}); err != nil {
		t.Fatal(err)
	}
}

func discoverMust(t *testing.T, d SectorDisk) ([]*probe, canonical) {
	t.Helper()
	probes, _, _, err := discover(d)
	if err != nil {
		t.Fatalf("discover: %v", err)
	}
	for _, p := range probes {
		if p.valid {
			return probes, canonicalOf(p)
		}
	}
	t.Fatal("no valid probe")
	return nil, canonical{}
}

func TestInvalidArguments(t *testing.T) {
	if _, err := Repair(nil, Plan{}); !errors.Is(err, ErrArgument) {
		t.Fatalf("nil disk: %v", err)
	}
	d, _ := NewMemoryDiskMust(t, 64, 512)
	if _, err := Repair(&badSectorSizeDisk{d}, Plan{}); !errors.Is(err, ErrArgument) {
		t.Fatalf("bad sector size: %v", err)
	}
}

func NewMemoryDiskMust(t *testing.T, n uint64, bs int) (*MemoryDisk, canonical) {
	t.Helper()
	d, err := NewMemoryDisk(n, bs, true)
	if err != nil {
		t.Fatal(err)
	}
	return d, canonical{}
}

type badSectorSizeDisk struct{ *MemoryDisk }

func (b *badSectorSizeDisk) SectorSize() int { return 700 }

func TestStaleBackupGrowThenResume(t *testing.T) {
	// Simulate the exact mid-migration state: disk grown, new backup written
	// at target end, old backup still at former end, primary still old.
	const oldSize, newSize uint64 = 1024, 1700
	d, c := buildGPT(t, oldSize, 512, DefaultEntrySize, DefaultEntryCount)
	g1, _ := primaryGeometry(oldSize, 512, c.entryCount, c.entrySize)
	addPartition(&c, 0, guid(1), guid(2), g1.firstUsable, 400, "p")
	rebuild(t, d, c)
	if err := d.ResizeSectors(newSize); err != nil {
		t.Fatal(err)
	}
	ntpg, _ := primaryGeometry(newSize, 512, c.entryCount, c.entrySize)
	ntbg, _ := backupGeometry(newSize, 512, c.entryCount, c.entrySize)
	if err := d.WriteSectors(ntbg.entriesLBA, c.entries); err != nil {
		t.Fatal(err)
	}
	if err := d.WriteSectors(ntbg.headerLBA, buildHeader(c, ntbg, ntpg, 512)); err != nil {
		t.Fatal(err)
	}
	// Resuming must finish: remove stale old backup, replace primary, keep data.
	rep, err := Repair(d, Plan{TargetSectors: newSize})
	if err != nil {
		t.Fatalf("resume mid-grow: %v", err)
	}
	if rep.FinalSectors != newSize {
		t.Fatalf("size=%d", rep.FinalSectors)
	}
	validatePair(t, d)
	oldHdr, _ := readSectors(d, oldSize-1, 1)
	if !isZero(oldHdr) {
		t.Fatal("former backup still present")
	}
}

func TestShrinkMidMigrationResume(t *testing.T) {
	// Disk still at migration size, both new tables already at target layout,
	// truncate not yet performed.
	const oldSize, newSize uint64 = 2048, 1300
	d, c := buildGPT(t, oldSize, 512, DefaultEntrySize, DefaultEntryCount)
	og1, _ := primaryGeometry(oldSize, 512, c.entryCount, c.entrySize)
	ntpg, _ := primaryGeometry(newSize, 512, c.entryCount, c.entrySize)
	addPartition(&c, 0, guid(1), guid(2), og1.firstUsable, ntpg.lastUsable, "p")
	rebuild(t, d, c)

	ntbg, _ := backupGeometry(newSize, 512, c.entryCount, c.entrySize)
	if err := d.WriteSectors(ntbg.entriesLBA, c.entries); err != nil {
		t.Fatal(err)
	}
	if err := d.WriteSectors(ntbg.headerLBA, buildHeader(c, ntbg, ntpg, 512)); err != nil {
		t.Fatal(err)
	}
	if err := d.WriteSectors(ntpg.entriesLBA, c.entries); err != nil {
		t.Fatal(err)
	}
	if err := d.WriteSectors(ntpg.headerLBA, buildHeader(c, ntpg, ntbg, 512)); err != nil {
		t.Fatal(err)
	}
	rep, err := Repair(d, Plan{TargetSectors: newSize})
	if err != nil {
		t.Fatalf("resume mid-shrink: %v", err)
	}
	if !rep.Shrank || d.TotalSectors() != newSize {
		t.Fatalf("shrink did not complete: %+v", rep)
	}
	validatePair(t, d)
}

func TestValidationBranches(t *testing.T) {
	t.Run("entry array CRC mismatch", func(t *testing.T) {
		d, c := buildGPT(t, 2048, 512, DefaultEntrySize, DefaultEntryCount)
		addPartition(&c, 0, guid(1), guid(2), 34, 100, "p")
		rebuild(t, d, c)
		// Tamper primary entries without recomputing EntriesCRC.
		d.Bytes()[2*512] ^= 0xFF
		insp, err := Inspect(d)
		if err != nil {
			t.Fatalf("backup still valid, Inspect err=%v", err)
		}
		found := false
		for _, is := range insp.Issues {
			if is.HeaderLBA == 1 && is.Reason == "partition entry array CRC mismatch" {
				found = true
			}
		}
		if !found {
			t.Fatalf("entry CRC issue not reported: %+v", insp.Issues)
		}
		// Repair must rebuild the primary from the good backup.
		if _, err := Repair(d, Plan{}); err != nil {
			t.Fatalf("repair: %v", err)
		}
		validatePair(t, d)
	})

	t.Run("broken cross reference rejected", func(t *testing.T) {
		d, c := buildGPT(t, 2048, 512, DefaultEntrySize, DefaultEntryCount)
		addPartition(&c, 0, guid(1), guid(2), 34, 100, "p")
		rebuild(t, d, c)
		// Point the primary header's backup pointer at LBA 100 (no header)
		// and recompute its header CRC so only the cross check fails.
		raw := d.Bytes()
		binary.LittleEndian.PutUint64(raw[512+32:512+40], 100)
		binary.LittleEndian.PutUint32(raw[512+16:512+20], 0)
		binary.LittleEndian.PutUint32(raw[512+16:512+20], headerCRC(raw[512:1024]))
		insp, err := Inspect(d)
		// Primary becomes untrusted; the standalone backup must still be trusted.
		if err != nil {
			t.Fatalf("backup should remain trusted: %v", err)
		}
		for _, s := range insp.Trustworthy {
			if s.HeaderLBA == 1 {
				t.Fatal("primary with broken cross reference was trusted")
			}
		}
		if _, err := Repair(d, Plan{}); err != nil {
			t.Fatalf("repair from backup: %v", err)
		}
		validatePair(t, d)
	})

	t.Run("partition outside usable area rejected", func(t *testing.T) {
		d, c := buildGPT(t, 2048, 512, DefaultEntrySize, DefaultEntryCount)
		g1, _ := primaryGeometry(2048, 512, c.entryCount, c.entrySize)
		addPartition(&c, 0, guid(1), guid(2), g1.firstUsable, g1.lastUsable, "ok")
		rebuild(t, d, c)
		// Forge a primary entry that runs past the usable area, then fix the
		// entry-array CRC so only the boundary check can reject it.
		raw := d.Bytes()
		binary.LittleEndian.PutUint64(raw[2*512+40:2*512+48], g1.lastUsable+1)
		newEntriesCRC := crc(raw[2*512 : 34*512])
		binary.LittleEndian.PutUint32(raw[512+88:512+92], newEntriesCRC)
		binary.LittleEndian.PutUint32(raw[512+16:512+20], 0)
		binary.LittleEndian.PutUint32(raw[512+16:512+20], headerCRC(raw[512:1024]))
		insp, err := Inspect(d)
		if err != nil {
			t.Fatalf("backup copy should still validate: %v", err)
		}
		var sawBoundary bool
		for _, is := range insp.Issues {
			if is.HeaderLBA == 1 && len(is.Reason) > 0 && bytes.Contains([]byte(is.Reason), []byte("boundary")) {
				sawBoundary = true
			}
		}
		if !sawBoundary {
			t.Fatalf("boundary issue missing: %+v", insp.Issues)
		}
	})

	t.Run("bad signature and zero sector", func(t *testing.T) {
		d, c := buildGPT(t, 1024, 512, DefaultEntrySize, DefaultEntryCount)
		rebuild(t, d, c)
		// Wipe primary header entirely.
		for i := 512; i < 1024; i++ {
			d.Bytes()[i] = 0
		}
		insp, err := Inspect(d)
		if err != nil {
			t.Fatalf("backup only: %v", err)
		}
		var sawZero bool
		for _, is := range insp.Issues {
			if is.HeaderLBA == 1 && is.Reason == "no header (zero sector)" {
				sawZero = true
			}
		}
		if !sawZero {
			t.Fatalf("zero-sector issue missing: %+v", insp.Issues)
		}
		if _, err := Repair(d, Plan{}); err != nil {
			t.Fatalf("repair: %v", err)
		}
		validatePair(t, d)
	})
}

func TestGrowThenShrinkRoundTrip(t *testing.T) {
	const start, grown, back uint64 = 1024, 1800, 1200
	d, c := buildGPT(t, start, 512, DefaultEntrySize, DefaultEntryCount)
	g1, _ := primaryGeometry(start, 512, c.entryCount, c.entrySize)
	backPG, _ := primaryGeometry(back, 512, c.entryCount, c.entrySize)
	// The partition must already fit inside the smallest geometry used.
	last := g1.lastUsable
	if backPG.lastUsable < last {
		last = backPG.lastUsable
	}
	addPartition(&c, 0, guid(1), guid(2), g1.firstUsable, last, "p")
	mark := []byte("ROUND-TRIP-DATA-XYZ")
	copy(d.Bytes()[600*512:], mark)
	rebuild(t, d, c)

	if _, err := Repair(d, Plan{TargetSectors: grown}); err != nil {
		t.Fatalf("grow: %v", err)
	}
	validatePair(t, d)
	if _, err := Repair(d, Plan{TargetSectors: back}); err != nil {
		t.Fatalf("shrink back: %v", err)
	}
	if d.TotalSectors() != back {
		t.Fatalf("final size=%d want %d", d.TotalSectors(), back)
	}
	validatePair(t, d)
	if !bytes.Equal(d.Bytes()[600*512:600*512+uint64(len(mark))], mark) {
		t.Fatal("user data changed across grow/shrink round trip")
	}
	_, got := discoverMust(t, d)
	if !bytesEqual(got.entries, c.entries) || !equalGUID(got.diskGUID, c.diskGUID) {
		t.Fatal("partition content/GUID changed across round trip")
	}
}

func TestFileDiskBasicAndErrors(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "fd.bin")
	d, c := buildGPT(t, 512, 512, DefaultEntrySize, DefaultEntryCount)
	g1, _ := primaryGeometry(512, 512, c.entryCount, c.entrySize)
	addPartition(&c, 0, guid(1), guid(2), g1.firstUsable, 300, "f")
	rebuild(t, d, c)
	if err := os.WriteFile(path, d.Bytes(), 0o600); err != nil {
		t.Fatal(err)
	}

	if _, err := OpenFileDisk(path, 700, 512); !errors.Is(err, ErrArgument) {
		t.Fatalf("bad sector size: %v", err)
	}
	if _, err := OpenFileDisk(path, 512, 256); !errors.Is(err, ErrArgument) {
		t.Fatalf("size mismatch: %v", err)
	}
	fd, err := OpenFileDisk(path, 512, 512)
	if err != nil {
		t.Fatal(err)
	}
	if err := fd.WriteSectors(511, make([]byte, 1024)); err == nil {
		t.Fatal("expected out-of-range write")
	}
	if err := fd.WriteSectors(0, make([]byte, 256)); err == nil {
		t.Fatal("expected partial-sector write")
	}
	if err := fd.ReadSectors(512, 1, make([]byte, 512)); err == nil {
		t.Fatal("expected out-of-range read")
	}
	rep, err := Repair(fd, Plan{TargetSectors: 700})
	if err != nil {
		t.Fatalf("file-backed grow: %v", err)
	}
	if rep.FinalSectors != 700 {
		t.Fatalf("size=%d", rep.FinalSectors)
	}
	if err := fd.Close(); err != nil {
		t.Fatal(err)
	}
	// Reopen at the grown size and validate.
	fd2, err := OpenFileDisk(path, 512, 700)
	if err != nil {
		t.Fatal(err)
	}
	defer fd2.Close()
	validatePair(t, fd2)
	// Double close is tolerated.
	if err := fd2.Close(); err != nil {
		t.Fatal(err)
	}
}

func TestReadWriteRangeChecks(t *testing.T) {
	d, _ := NewMemoryDiskMust(t, 16, 512)
	if err := d.ReadSectors(15, 2, make([]byte, 1024)); err == nil {
		t.Fatal("expected out-of-range read error")
	}
	if err := d.WriteSectors(15, make([]byte, 1024)); err == nil {
		t.Fatal("expected out-of-range write error")
	}
	if err := d.WriteSectors(0, make([]byte, 300)); err == nil {
		t.Fatal("expected partial-sector write error")
	}
	if err := d.ResizeSectors(0); err == nil {
		t.Fatal("expected zero-size resize error")
	}
}

func ExampleRepair() {
	d, _ := NewMemoryDisk(64, 512, true)
	fmt.Println(d.TotalSectors())
	// Output: 64
}
