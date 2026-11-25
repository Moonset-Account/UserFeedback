package gptrepair

import (
	"encoding/binary"
	"errors"
	"fmt"
)

// Plan describes a requested repair/relocation run.
//
// TargetSectors is the desired capacity in logical sectors after the run. A
// zero value means "keep the current capacity". During migration the disk is
// addressed at max(old, target) sectors: a grow is performed before any GPT
// metadata is moved, while a shrink is performed only after both fresh copies
// have been written and verified.
//
// The partition entry size/count and all entry bytes are always preserved
// from the trusted source; callers cannot reformat the entry array.
type Plan struct {
	TargetSectors uint64
}

// Issue records why a candidate GPT location could not be trusted.
type Issue struct {
	HeaderLBA uint64
	Reason    string
}

// SourceInfo describes one candidate GPT copy found on the disk.
type SourceInfo struct {
	HeaderLBA  uint64
	EntriesLBA uint64
	Role       string // "primary", "backup" or "unknown"
	Valid      bool
	DiskGUID   [16]byte
	EntryCount uint32
	EntrySize  uint32
}

// Inspection is the read-only result of Inspect.
type Inspection struct {
	SectorSize   int
	TotalSectors uint64
	Sources      []SourceInfo
	Issues       []Issue
	// Trustworthy lists the fully validated copies.
	Trustworthy []SourceInfo
	// Contradiction is true when two validated copies disagree.
	Contradiction bool
}

// Report summarizes a completed Repair run.
type Report struct {
	OldSectors   uint64
	WorkSectors  uint64 // addressable range used during migration
	FinalSectors uint64
	PrimaryLBA   uint64
	BackupLBA    uint64
	Sources      []SourceInfo
	Issues       []Issue
	SectorWrites uint64
	Grew         bool
	Shrank       bool
	MBRWritten   bool
	StaleRemoved []uint64
}

// canonical is the immutable content carried from the trusted source into both
// rebuilt copies.
type canonical struct {
	diskGUID   [16]byte
	entryCount uint32
	entrySize  uint32
	entries    []byte // exact on-disk entry-array bytes
}

// probe is the result of examining one candidate header location.
type probe struct {
	headerLBA uint64
	role      string
	valid     bool
	zero      bool
	header    *Header
	entries   []byte
	issue     string
}

func (p *probe) sourceInfo() SourceInfo {
	si := SourceInfo{HeaderLBA: p.headerLBA, Role: p.role, Valid: p.valid}
	if p.header != nil {
		si.EntriesLBA = p.header.EntriesLBA
		si.DiskGUID = p.header.DiskGUID
		si.EntryCount = p.header.EntryCount
		si.EntrySize = p.header.EntrySize
	}
	return si
}

// readSectors allocates and returns count sectors starting at lba.
func readSectors(d SectorDisk, lba, count uint64) ([]byte, error) {
	buf := make([]byte, count*uint64(d.SectorSize()))
	if err := d.ReadSectors(lba, count, buf); err != nil {
		return nil, err
	}
	return buf, nil
}

// probeAt fully validates one potential GPT header location. Nothing it reads
// is trusted until header CRC, entry-array CRC, bounds and cross references
// have all been checked.
func probeAt(d SectorDisk, headerLBA, totalBlocks uint64) *probe {
	return probeAtDepth(d, headerLBA, totalBlocks, 0)
}

func probeAtDepth(d SectorDisk, headerLBA, totalBlocks uint64, depth int) *probe {
	p := &probe{headerLBA: headerLBA, role: "unknown"}
	if depth > 2 {
		p.issue = "header pointer chain too deep"
		return p
	}
	bs := d.SectorSize()

	raw, err := readSectors(d, headerLBA, 1)
	if err != nil {
		p.issue = "header unreadable: " + err.Error()
		return p
	}
	if isZero(raw) {
		p.zero = true
		p.issue = "no header (zero sector)"
		return p
	}
	h, err := parseHeader(raw)
	if err != nil {
		p.issue = err.Error()
		return p
	}
	p.header = h

	fail := func(format string, args ...interface{}) *probe {
		p.issue = fmt.Sprintf(format, args...)
		return p
	}

	if h.Revision != gptRevision {
		return fail("unsupported GPT revision 0x%08x", h.Revision)
	}
	if h.HeaderSize > uint32(bs) {
		return fail("header size %d exceeds sector size %d", h.HeaderSize, bs)
	}
	// Header CRC is computed over HeaderSize bytes with the CRC field zeroed.
	if h.HeaderCRC != headerCRC(raw[:h.HeaderSize]) {
		return fail("header CRC mismatch")
	}
	if h.CurrentLBA != headerLBA {
		return fail("current LBA %d does not match location %d", h.CurrentLBA, headerLBA)
	}
	if h.CurrentLBA == h.BackupLBA {
		return fail("primary and backup header share LBA %d", h.CurrentLBA)
	}
	if h.CurrentLBA >= totalBlocks || h.BackupLBA >= totalBlocks {
		return fail("header LBAs exceed disk end")
	}
	if h.EntrySize < 128 || h.EntrySize%128 != 0 {
		return fail("invalid partition entry size %d", h.EntrySize)
	}
	es, err := entriesSectors(h.EntryCount, h.EntrySize, bs)
	if err != nil {
		return fail("%v", err)
	}
	entryEnd, ok := addOverflows(h.EntriesLBA, es)
	if !ok || entryEnd > totalBlocks {
		return fail("partition entry array exceeds disk end")
	}
	// Header sector must not overlap its own entry array.
	if h.EntriesLBA <= h.CurrentLBA && h.CurrentLBA < entryEnd {
		return fail("entry array overlaps header sector")
	}

	entryBytes, err := readSectors(d, h.EntriesLBA, es)
	if err != nil {
		return fail("entry array unreadable: %v", err)
	}
	if h.EntriesCRC != crc(entryBytes) {
		return fail("partition entry array CRC mismatch")
	}

	// Role and structural placement. A copy is accepted when its own layout
	// is internally coherent and sector-bounded; it is NOT required to sit at
	// the current disk's last LBA. During a grow the old backup may remain at
	// the former end; during a shrink the new backup already lives at the
	// target end while the disk is still at its migration size.
	if h.FirstUsableLBA > h.LastUsableLBA || h.FirstUsableLBA < 2 {
		return fail("usable LBA range is empty or overlaps primary metadata")
	}
	switch h.CurrentLBA {
	case 1:
		// Primary: entries directly follow the header at LBA 2.
		p.role = "primary"
		if h.EntriesLBA != 2 {
			return fail("primary entry array must start at LBA 2, got %d", h.EntriesLBA)
		}
		if h.FirstUsableLBA != 2+es {
			return fail("primary first usable LBA does not follow its entry array")
		}
		if h.BackupLBA <= h.LastUsableLBA {
			return fail("backup header LBA %d is inside usable area", h.BackupLBA)
		}
	default:
		// Backup: it must point back at LBA 1 and its entries must end in the
		// sector immediately before the backup header.
		p.role = "backup"
		if h.BackupLBA != 1 {
			return fail("backup header does not reference primary LBA 1, got %d", h.BackupLBA)
		}
		if h.EntriesLBA+es != h.CurrentLBA {
			return fail("backup entry array does not immediately precede backup header")
		}
		if h.LastUsableLBA+1 != h.EntriesLBA {
			return fail("usable area does not end before backup entry array")
		}
	}
	if h.LastUsableLBA >= totalBlocks {
		return fail("usable area extends beyond disk end")
	}

	// Mutual references: when the peer copy is also present and self-consistent
	// the two headers must point at each other and share one usable range and
	// one entry-array geometry. Each copy keeps its own entry-array location.
	// A lone surviving copy is still accepted.
	peer := probeAtDepth(d, h.BackupLBA, totalBlocks, depth+1)
	if peer.valid && peer.header != nil {
		ph := peer.header
		if ph.BackupLBA != h.CurrentLBA ||
			ph.EntriesLBA == h.EntriesLBA ||
			ph.FirstUsableLBA != h.FirstUsableLBA || ph.LastUsableLBA != h.LastUsableLBA ||
			ph.EntryCount != h.EntryCount || ph.EntrySize != h.EntrySize {
			return fail("peer header at LBA %d does not cross-reference this copy consistently", h.BackupLBA)
		}
	}

	// Partition bounds: every non-empty entry must sit inside the usable area
	// advertised by its own header.
	for i := uint32(0); i < h.EntryCount; i++ {
		e := entryBytes[i*h.EntrySize : (i+1)*h.EntrySize]
		if isZero(e[:16]) { // empty entry: partition type GUID is zero
			continue
		}
		first := binary.LittleEndian.Uint64(e[32:40])
		last := binary.LittleEndian.Uint64(e[40:48])
		if first > last {
			return fail("partition entry %d has first LBA > last LBA", i)
		}
		if first < h.FirstUsableLBA || last > h.LastUsableLBA {
			return fail("partition entry %d crosses usable-area boundary", i)
		}
	}

	p.valid = true
	p.entries = entryBytes
	return p
}

// candidateHeaderLBAs builds the de-duplicated set of header locations to
// examine. Pointers are only followed from fully validated headers, so a
// corrupt header can never redirect the scan. Both primary and backup
// pointers are followed because during migration a valid copy can live away
// from the current disk end.
func candidateHeaderLBAs(d SectorDisk, totalBlocks uint64) []uint64 {
	seen := map[uint64]bool{}
	var out []uint64
	add := func(lba uint64) {
		if lba < totalBlocks && !seen[lba] {
			seen[lba] = true
			out = append(out, lba)
		}
	}
	add(1)
	add(totalBlocks - 1)
	// Re-probe the two standard locations first; their validated pointers
	// extend the search to relocated/stale copies.
	for _, lba := range []uint64{1, totalBlocks - 1} {
		p := probeAt(d, lba, totalBlocks)
		if p.valid && p.header != nil {
			add(p.header.BackupLBA)
		}
	}
	return out
}

// discover runs the full read-only validation pass and returns all probes,
// valid ones first-class, plus derived inspection metadata.
func discover(d SectorDisk) ([]*probe, []SourceInfo, []Issue, error) {
	total := d.TotalSectors()
	var probes []*probe
	var sources []SourceInfo
	var issues []Issue
	for _, lba := range candidateHeaderLBAs(d, total) {
		p := probeAt(d, lba, total)
		probes = append(probes, p)
		sources = append(sources, p.sourceInfo())
		if !p.valid {
			issues = append(issues, Issue{HeaderLBA: lba, Reason: p.issue})
		}
	}
	var trusted []*probe
	for _, p := range probes {
		if p.valid {
			trusted = append(trusted, p)
		}
	}
	if len(trusted) == 0 {
		return probes, sources, issues, ErrNoTrustworthySource
	}
	first := canonicalOf(trusted[0])
	for _, p := range trusted[1:] {
		if !canonicalsEqual(first, canonicalOf(p)) {
			return probes, sources, issues, fmt.Errorf("%w: headers at LBA %d and %d disagree",
				ErrArbitrationRequired, trusted[0].headerLBA, p.headerLBA)
		}
	}
	return probes, sources, issues, nil
}

func canonicalOf(p *probe) canonical {
	return canonical{
		diskGUID:   p.header.DiskGUID,
		entryCount: p.header.EntryCount,
		entrySize:  p.header.EntrySize,
		entries:    append([]byte(nil), p.entries...),
	}
}

func canonicalsEqual(a, b canonical) bool {
	return a.entryCount == b.entryCount &&
		a.entrySize == b.entrySize &&
		equalGUID(a.diskGUID, b.diskGUID) &&
		bytesEqual(a.entries, b.entries)
}

func bytesEqual(a, b []byte) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

// partitionFits verifies that every non-empty partition entry fits, unchanged,
// inside the target usable area.
func partitionFits(c canonical, g geometry) error {
	for i := uint32(0); i < c.entryCount; i++ {
		e := c.entries[i*c.entrySize : (i+1)*c.entrySize]
		if isZero(e[:16]) {
			continue
		}
		first := binary.LittleEndian.Uint64(e[32:40])
		last := binary.LittleEndian.Uint64(e[40:48])
		if first < g.firstUsable || last > g.lastUsable {
			return fmt.Errorf("%w: entry %d spans LBA %d-%d, target usable area is %d-%d",
				ErrPartitionOverflow, i, first, last, g.firstUsable, g.lastUsable)
		}
	}
	return nil
}

// buildHeader constructs a fresh header for one role pointing at the target
// geometry. Entry bytes and disk GUID are preserved verbatim.
func buildHeader(c canonical, self, peer geometry, blockSize int) []byte {
	h := &Header{
		Revision:       gptRevision,
		HeaderSize:     headerSize,
		CurrentLBA:     self.headerLBA,
		BackupLBA:      peer.headerLBA,
		FirstUsableLBA: self.firstUsable,
		LastUsableLBA:  self.lastUsable,
		DiskGUID:       c.diskGUID,
		EntriesLBA:     self.entriesLBA,
		EntryCount:     c.entryCount,
		EntrySize:      c.entrySize,
		EntriesCRC:     crc(c.entries),
	}
	return encodeHeader(h, blockSize)
}

// probeMatches reports whether an existing validated probe already contains
// exactly the target copy (so rewriting it can be skipped).
func probeMatches(p *probe, self, peer geometry, c canonical) bool {
	if p == nil || !p.valid || p.header == nil {
		return false
	}
	h := p.header
	return h.CurrentLBA == self.headerLBA &&
		h.BackupLBA == peer.headerLBA &&
		h.EntriesLBA == self.entriesLBA &&
		h.FirstUsableLBA == self.firstUsable &&
		h.LastUsableLBA == self.lastUsable &&
		h.EntryCount == c.entryCount &&
		h.EntrySize == c.entrySize &&
		h.EntriesCRC == crc(c.entries) &&
		bytesEqual(p.entries, c.entries)
}

// Inspect performs the read-only validation pass without touching the disk.
// The returned error (if any) is ErrNoTrustworthySource or
// ErrArbitrationRequired; the Inspection still carries every candidate and
// issue for diagnostics.
func Inspect(d SectorDisk) (*Inspection, error) {
	if d == nil {
		return nil, fmt.Errorf("%w: nil disk", ErrArgument)
	}
	probes, sources, issues, err := discover(d)
	insp := &Inspection{
		SectorSize:   d.SectorSize(),
		TotalSectors: d.TotalSectors(),
		Sources:      sources,
		Issues:       issues,
	}
	for _, p := range probes {
		if p.valid {
			insp.Trustworthy = append(insp.Trustworthy, p.sourceInfo())
		}
	}
	if errors.Is(err, ErrArbitrationRequired) {
		insp.Contradiction = true
	}
	return insp, err
}

// Repair validates the on-disk GPT copies and, when exactly one consistent
// trusted content exists, rebuilds both primary and backup tables for the
// planned capacity. User partition bytes and GUIDs are never modified.
func Repair(d SectorDisk, plan Plan) (*Report, error) {
	if d == nil {
		return nil, fmt.Errorf("%w: nil disk", ErrArgument)
	}
	bs := d.SectorSize()
	if bs < blockSizeMin || bs > blockSizeMax || bs&(bs-1) != 0 {
		return nil, fmt.Errorf("%w: sector size %d", ErrArgument, bs)
	}
	oldSize := d.TotalSectors()
	target := plan.TargetSectors
	if target == 0 {
		target = oldSize
	}

	// ---- Phase 1: read-only discovery on the disk as opened. No writes may
	// happen before the trust/arbitration decision is final. ----
	probes, sources, issues, err := discover(d)
	if err != nil {
		return &Report{OldSectors: oldSize, FinalSectors: oldSize, Sources: sources, Issues: issues}, err
	}
	c := canonicalOf(firstValid(probes))

	// Target geometry is computed from the planned capacity up front so that
	// a too-small target or an overflowing partition aborts before any write.
	tpg, err := primaryGeometry(target, bs, c.entryCount, c.entrySize)
	if err != nil {
		return nil, err
	}
	tbg, err := backupGeometry(target, bs, c.entryCount, c.entrySize)
	if err != nil {
		return nil, err
	}
	if err := partitionFits(c, tpg); err != nil {
		return nil, err
	}

	// ---- Phase 2: establish the migration addressable range. ----
	workSize := oldSize
	if target > workSize {
		workSize = target
	}
	rd, ok := d.(ResizableDisk)
	if workSize != oldSize {
		if !ok {
			return nil, fmt.Errorf("%w: backend cannot change capacity from %d to %d sectors",
				ErrArgument, oldSize, workSize)
		}
		if err := rd.ResizeSectors(workSize); err != nil {
			return nil, fmt.Errorf("growing disk for migration: %w", err)
		}
	}

	rep := &Report{
		OldSectors:   oldSize,
		WorkSectors:  workSize,
		FinalSectors: workSize,
		PrimaryLBA:   tpg.headerLBA,
		BackupLBA:    tbg.headerLBA,
		Sources:      sources,
		Issues:       issues,
		Grew:         workSize > oldSize,
	}

	// Stale copy locations are captured while the old layout is still known.
	// A grow can leave the old backup at the former last LBA; a shrink can
	// leave the old backup beyond the new end (it is erased before truncate).
	var stale []uint64
	for _, p := range probes {
		if p.headerLBA == 1 || p.headerLBA == tbg.headerLBA {
			continue
		}
		stale = append(stale, p.headerLBA)
	}

	writer := &countingDisk{SectorDisk: d}

	// ---- Phase 3: write the new backup first. Until both new copies exist,
	// every previously trusted sector is left untouched, so a crash always
	// leaves at least one complete old table. ----
	if !existingMatches(writer, tbg.headerLBA, workSize, tbg, tpg, c) {
		if err := writer.WriteSectors(tbg.entriesLBA, c.entries); err != nil {
			return rep, err
		}
		if err := writer.WriteSectors(tbg.headerLBA, buildHeader(c, tbg, tpg, bs)); err != nil {
			return rep, err
		}
	}

	// Stale copies can now be removed: the new backup is already complete.
	// Entries are zeroed before the header so a half-erased site never looks
	// like a valid table.
	for _, lba := range stale {
		if lba >= workSize {
			continue
		}
		sp := probeAt(writer, lba, workSize)
		if sp.zero {
			continue
		}
		if sp.header != nil {
			es, err := entriesSectors(sp.header.EntryCount, sp.header.EntrySize, bs)
			if err == nil && sp.header.EntriesLBA+es <= workSize &&
				!(sp.header.EntriesLBA <= lba && lba < sp.header.EntriesLBA+es) {
				zeros := make([]byte, es*uint64(bs))
				if err := writer.WriteSectors(sp.header.EntriesLBA, zeros); err != nil {
					return rep, err
				}
			}
		}
		if err := writer.WriteSectors(lba, make([]byte, bs)); err != nil {
			return rep, err
		}
		rep.StaleRemoved = append(rep.StaleRemoved, lba)
	}

	// ---- Phase 4: replace the primary copy (entries first, header last so a
	// torn write never presents a header pointing at half-written entries). ----
	if !existingMatches(writer, tpg.headerLBA, workSize, tpg, tbg, c) {
		if err := writer.WriteSectors(tpg.entriesLBA, c.entries); err != nil {
			return rep, err
		}
		if err := writer.WriteSectors(tpg.headerLBA, buildHeader(c, tpg, tbg, bs)); err != nil {
			return rep, err
		}
	}

	// ---- Phase 5: shrink only after both new tables are durable. ----
	if target < workSize {
		if !ok {
			return rep, fmt.Errorf("%w: backend cannot shrink to %d sectors", ErrArgument, target)
		}
		if err := rd.ResizeSectors(target); err != nil {
			return rep, fmt.Errorf("shrinking disk after repair: %w", err)
		}
		rep.Shrank = true
		rep.FinalSectors = target
	}

	// ---- Phase 6: protective MBR, written last so LBA 0 never participates
	// in a crash window of the GPT copies. ----
	mbrNeeded, err := needsProtectiveMBR(writer, rep.FinalSectors)
	if err != nil {
		return rep, err
	}
	if mbrNeeded {
		mbr := buildProtectiveMBR(rep.FinalSectors)
		// Pad up to the logical sector size while keeping the boot signature at
		// byte 510..511 (all larger sector sizes remain a multiple of 512).
		if bs := writer.SectorSize(); bs > len(mbr) {
			padded := make([]byte, bs)
			copy(padded, mbr)
			mbr = padded
		}
		if err := writer.WriteSectors(mbrSector, mbr); err != nil {
			return rep, err
		}
		rep.MBRWritten = true
	}
	rep.SectorWrites = writer.writes

	// ---- Phase 7: re-validate the result from disk, exactly as a fresh
	// reopen would see it. ----
	finalProbes, finalSources, finalIssues, ferr := discover(writer)
	if ferr != nil {
		return rep, fmt.Errorf("post-repair validation failed: %w", ferr)
	}
	if len(finalProbes) < 2 {
		return rep, fmt.Errorf("post-repair validation failed: %w", ErrNoTrustworthySource)
	}
	rep.Sources = finalSources
	rep.Issues = finalIssues
	return rep, nil
}

func firstValid(probes []*probe) *probe {
	for _, p := range probes {
		if p.valid {
			return p
		}
	}
	return nil
}

// existingMatches re-probes one header location against the desired target.
func existingMatches(d SectorDisk, headerLBA, total uint64, self, peer geometry, c canonical) bool {
	p := probeAt(d, headerLBA, total)
	return probeMatches(p, self, peer, c)
}

// countingDisk wraps a SectorDisk and counts sectors written.
type countingDisk struct {
	SectorDisk
	writes uint64
}

func (c *countingDisk) WriteSectors(lba uint64, src []byte) error {
	if err := c.SectorDisk.WriteSectors(lba, src); err != nil {
		return err
	}
	c.writes += uint64(len(src) / c.SectorSize())
	return nil
}

// needsProtectiveMBR reports whether LBA 0 lacks a GPT protective MBR.
func needsProtectiveMBR(d SectorDisk, totalBlocks uint64) (bool, error) {
	raw, err := readSectors(d, 0, 1)
	if err != nil {
		return false, err
	}
	if binary.LittleEndian.Uint16(raw[510:512]) != mbrBootSig {
		return true, nil
	}
	if raw[mbrPartition+4] != mbrTypeGPT {
		return true, nil
	}
	return false, nil
}

// buildProtectiveMBR builds a standard protective MBR with one 0xEE partition
// starting at LBA 1 and covering as much of the disk as fits in 32 bits.
func buildProtectiveMBR(totalBlocks uint64) []byte {
	buf := make([]byte, 512)
	part := buf[mbrPartition : mbrPartition+16]
	// CHS start 0/0/2, CHS end 0xFE/0xFF/0xFF per UEFI example.
	part[0] = 0x00
	part[1] = 0x00
	part[2] = 0x02
	part[3] = 0x00
	part[4] = mbrTypeGPT
	part[5] = 0xFE
	part[6] = 0xFF
	part[7] = 0xFF
	binary.LittleEndian.PutUint32(part[8:12], 1)
	size := totalBlocks - 1
	if size > 0xFFFFFFFF {
		size = 0xFFFFFFFF
	}
	binary.LittleEndian.PutUint32(part[12:16], uint32(size))
	binary.LittleEndian.PutUint16(buf[510:512], mbrBootSig)
	return buf
}
