// Package gptrepair repairs and relocates GUID Partition Tables (GPT) on a
// virtual block disk that exposes sector-granularity read/write access.
//
// The implementation follows the on-disk layout defined by UEFI 2.10, section
// 5.3:
//
//	LBA 0            protective MBR
//	LBA 1            primary GPT header
//	LBA 2 ..         primary partition entry array
//	...              partition data
//	.. LBA n-1       backup partition entry array
//	LBA n            backup GPT header (last LBA)
//
// All repair decisions are defensive: a table is only trusted when both its
// header CRC and its partition-entry-array CRC are valid, its bounds stay
// inside the disk and the two headers cross-reference each other correctly.
package gptrepair

import (
	"bytes"
	"encoding/binary"
	"errors"
	"fmt"
	"hash/crc32"
)

const (
	// Signature "EFI PART" at offset 0 of every GPT header.
	gptSignature = 0x5452415020494645
	gptRevision  = 0x00010000 // revision 1.0
	headerSize   = 92

	// Default partition entry layout used when building tables.
	DefaultEntrySize  = 128
	DefaultEntryCount = 128

	// Protective MBR.
	mbrSector    = 0
	mbrBootSig   = 0xAA55
	mbrTypeGPT   = 0xEE
	mbrPartition = 446

	blockSizeMin = 512
	blockSizeMax = 4096
)

// Sentinel errors. Callers can use errors.Is to distinguish the documented
// failure classes.
var (
	// ErrNoTrustworthySource means no complete, self-consistent GPT copy
	// (header CRC + entry-array CRC + bounds + cross references) was found.
	ErrNoTrustworthySource = errors.New("gptrepair: no trustworthy GPT copy found")

	// ErrArbitrationRequired means two individually valid copies disagree
	// about disk identity or partition contents and the caller must decide
	// which copy is authoritative. No sectors are written in this case.
	ErrArbitrationRequired = errors.New("gptrepair: valid GPT copies contradict each other; arbitration required")

	// ErrPartitionOverflow means a partition cannot be accommodated at the
	// requested (smaller) capacity without changing its byte range.
	ErrPartitionOverflow = errors.New("gptrepair: partition extends beyond the target disk")

	// ErrGeometry means the requested disk/table geometry cannot hold a GPT.
	ErrGeometry = errors.New("gptrepair: invalid disk or table geometry")

	// ErrArgument means a caller supplied an invalid argument.
	ErrArgument = errors.New("gptrepair: invalid argument")
)

var crcTable = crc32.MakeTable(crc32.IEEE)

func crc(b []byte) uint32 { return crc32.Checksum(b, crcTable) }

// Header is the decoded 92-byte GPT header.
type Header struct {
	Revision       uint32
	HeaderSize     uint32
	HeaderCRC      uint32
	CurrentLBA     uint64
	BackupLBA      uint64
	FirstUsableLBA uint64
	LastUsableLBA  uint64
	DiskGUID       [16]byte
	EntriesLBA     uint64
	EntryCount     uint32
	EntrySize      uint32
	EntriesCRC     uint32
}

// parseHeader decodes a header-sized buffer. It returns the structural
// information regardless of CRC validity; callers must verify HeaderCRC and
// EntriesCRC through validate before trusting any pointer.
func parseHeader(b []byte) (*Header, error) {
	if len(b) < headerSize {
		return nil, fmt.Errorf("%w: short header buffer", ErrArgument)
	}
	h := &Header{
		Revision:       binary.LittleEndian.Uint32(b[8:12]),
		HeaderSize:     binary.LittleEndian.Uint32(b[12:16]),
		HeaderCRC:      binary.LittleEndian.Uint32(b[16:20]),
		CurrentLBA:     binary.LittleEndian.Uint64(b[24:32]),
		BackupLBA:      binary.LittleEndian.Uint64(b[32:40]),
		FirstUsableLBA: binary.LittleEndian.Uint64(b[40:48]),
		LastUsableLBA:  binary.LittleEndian.Uint64(b[48:56]),
		EntriesLBA:     binary.LittleEndian.Uint64(b[72:80]),
		EntryCount:     binary.LittleEndian.Uint32(b[80:84]),
		EntrySize:      binary.LittleEndian.Uint32(b[84:88]),
		EntriesCRC:     binary.LittleEndian.Uint32(b[88:92]),
	}
	copy(h.DiskGUID[:], b[56:72])
	if binary.LittleEndian.Uint64(b[0:8]) != gptSignature {
		return h, errors.New("bad signature")
	}
	if h.HeaderSize < headerSize {
		return h, errors.New("header size below 92 bytes")
	}
	return h, nil
}

// headerCRC computes the CRC of a header with its HeaderCRC field zeroed.
// The CRC covers HeaderSize bytes (the 92-byte defined header, possibly more
// for future revisions), never the padding that fills the rest of the sector.
func headerCRC(raw []byte) uint32 {
	length := binary.LittleEndian.Uint32(raw[12:16])
	if length == 0 || int(length) > len(raw) {
		length = uint32(headerSize)
	}
	work := make([]byte, length)
	copy(work, raw[:length])
	binary.LittleEndian.PutUint32(work[16:20], 0)
	return crc(work)
}

// encodeHeader builds a sector-aligned header block with a fresh CRC.
func encodeHeader(h *Header, blockSize int) []byte {
	buf := make([]byte, blockSize)
	binary.LittleEndian.PutUint64(buf[0:8], gptSignature)
	binary.LittleEndian.PutUint32(buf[8:12], h.Revision)
	binary.LittleEndian.PutUint32(buf[12:16], h.HeaderSize)
	binary.LittleEndian.PutUint32(buf[16:20], 0) // CRC placeholder
	binary.LittleEndian.PutUint32(buf[20:24], 0) // reserved
	binary.LittleEndian.PutUint64(buf[24:32], h.CurrentLBA)
	binary.LittleEndian.PutUint64(buf[32:40], h.BackupLBA)
	binary.LittleEndian.PutUint64(buf[40:48], h.FirstUsableLBA)
	binary.LittleEndian.PutUint64(buf[48:56], h.LastUsableLBA)
	copy(buf[56:72], h.DiskGUID[:])
	binary.LittleEndian.PutUint64(buf[72:80], h.EntriesLBA)
	binary.LittleEndian.PutUint32(buf[80:84], h.EntryCount)
	binary.LittleEndian.PutUint32(buf[84:88], h.EntrySize)
	binary.LittleEndian.PutUint32(buf[88:92], h.EntriesCRC)
	binary.LittleEndian.PutUint32(buf[16:20], headerCRC(buf))
	return buf
}

// geometry is the placement of one complete GPT copy (header + entries).
type geometry struct {
	headerLBA  uint64
	entriesLBA uint64
	entryCount uint32
	entrySize  uint32
	// first/last LBA of the sectors occupied by the entry array.
	entriesEndLBA uint64
	firstUsable   uint64
	lastUsable    uint64
}

// entriesSectors returns how many whole sectors the entry array occupies.
func entriesSectors(entryCount, entrySize uint32, blockSize int) (uint64, error) {
	if entryCount == 0 || entrySize == 0 {
		return 0, fmt.Errorf("%w: zero entry count/size", ErrGeometry)
	}
	total := uint64(entryCount) * uint64(entrySize)
	if total/uint64(entrySize) != uint64(entryCount) {
		return 0, fmt.Errorf("%w: entry array overflow", ErrGeometry)
	}
	if total%uint64(blockSize) != 0 {
		return 0, fmt.Errorf("%w: entry array is not sector aligned", ErrGeometry)
	}
	return total / uint64(blockSize), nil
}

// primaryGeometry places the primary copy at LBA 1/2.
func primaryGeometry(totalBlocks uint64, blockSize int, entryCount, entrySize uint32) (geometry, error) {
	es, err := entriesSectors(entryCount, entrySize, blockSize)
	if err != nil {
		return geometry{}, err
	}
	g := geometry{
		headerLBA:     1,
		entriesLBA:    2,
		entryCount:    entryCount,
		entrySize:     entrySize,
		entriesEndLBA: 2 + es - 1,
	}
	g.firstUsable = g.entriesEndLBA + 1
	// Backup header occupies the final LBA; its entries sit immediately before.
	backupEntriesEnd := totalBlocks - 2
	backupEntriesStart := backupEntriesEnd - es + 1
	g.lastUsable = backupEntriesStart - 1
	if g.firstUsable > g.lastUsable {
		return geometry{}, fmt.Errorf("%w: disk too small for GPT metadata", ErrGeometry)
	}
	return g, nil
}

// backupGeometry places the backup copy at the end of the disk.
func backupGeometry(totalBlocks uint64, blockSize int, entryCount, entrySize uint32) (geometry, error) {
	es, err := entriesSectors(entryCount, entrySize, blockSize)
	if err != nil {
		return geometry{}, err
	}
	g := geometry{
		headerLBA:     totalBlocks - 1,
		entriesEndLBA: totalBlocks - 2,
		entryCount:    entryCount,
		entrySize:     entrySize,
	}
	g.entriesLBA = g.entriesEndLBA - es + 1
	// Usable range is shared with the primary copy.
	primary, err := primaryGeometry(totalBlocks, blockSize, entryCount, entrySize)
	if err != nil {
		return geometry{}, err
	}
	g.firstUsable = primary.firstUsable
	g.lastUsable = primary.lastUsable
	return g, nil
}

// isZero reports whether a buffer contains only zero bytes.
func isZero(b []byte) bool {
	for _, v := range b {
		if v != 0 {
			return false
		}
	}
	return true
}

// equalGUID compares two 16-byte GUIDs.
func equalGUID(a, b [16]byte) bool { return bytes.Equal(a[:], b[:]) }
