// Package auditlog extracts checksum-valid audit records from binary
// forensic image shards.
//
// Records use a caller-provided binary layout:
//
//	Magic | Length | Seq | Payload | Checksum
//
// Records may span shard boundaries. When a length field (or any other
// part of a record) is damaged, extraction recovers at the next record
// whose checksum verifies, and the damaged byte range is preserved as
// gap evidence. Valid records that share a sequence number but differ
// in content (e.g. from overlapping shards) are all kept and explicitly
// marked as conflicting.
package auditlog

import (
	"bytes"
	"encoding/binary"
	"errors"
	"fmt"
	"hash/crc32"
)

// Format describes the caller-provided binary record layout.
//
//	Magic | Length | Seq | Payload | Checksum
//
//   - Magic: boundary marker starting every record.
//   - Length: payload length in bytes, big-endian, LengthSize bytes wide.
//   - Seq: record sequence number, big-endian, SeqSize bytes wide.
//   - Checksum: low ChecksumSize bytes (big-endian) of the uint64 returned
//     by Checksum, computed over Length||Seq||Payload, i.e. everything
//     between the magic marker and the checksum field.
//   - MaxPayload: sanity bound for the length field; a record claiming a
//     larger payload is treated as damaged.
type Format struct {
	Magic        []byte
	LengthSize   int // 1, 2, 4 or 8
	SeqSize      int // 1, 2, 4 or 8
	ChecksumSize int // 1, 2, 4 or 8
	MaxPayload   uint64
	Checksum     func(data []byte) uint64
}

// CRC32Checksum is a ready-to-use Format.Checksum based on CRC-32 (IEEE).
func CRC32Checksum(data []byte) uint64 {
	return uint64(crc32.ChecksumIEEE(data))
}

func (f Format) validate() error {
	if len(f.Magic) == 0 {
		return errors.New("auditlog: format requires a non-empty magic boundary marker")
	}
	for name, sz := range map[string]int{
		"LengthSize": f.LengthSize, "SeqSize": f.SeqSize, "ChecksumSize": f.ChecksumSize,
	} {
		switch sz {
		case 1, 2, 4, 8:
		default:
			return fmt.Errorf("auditlog: %s must be 1, 2, 4 or 8, got %d", name, sz)
		}
	}
	if f.Checksum == nil {
		return errors.New("auditlog: format requires a checksum function")
	}
	if f.MaxPayload == 0 {
		return errors.New("auditlog: format requires a positive MaxPayload")
	}
	return nil
}

// Shard is one fragment of the forensic image. Shards are read in the
// order supplied by the caller; a record may span consecutive shards.
type Shard struct {
	ID   string
	Data []byte
}

// Segment is a raw byte interval inside one shard. A record or gap that
// spans shards is described by several segments, in stream order.
type Segment struct {
	ShardID string
	Offset  int // byte offset within the shard
	Length  int
}

// Record is a checksum-valid record reassembled from the shards.
type Record struct {
	Seq     uint64
	Payload []byte
	// Offset and Length locate the whole record (magic through checksum)
	// in the logical stream formed by concatenating the shards.
	Offset int
	Length int
	// Segments are the raw byte intervals of the record, per shard.
	Segments []Segment
	// Conflict is true when another valid record shares the same Seq but
	// has different content. Conflicting records are all kept.
	Conflict bool
	// ConflictGroup identifies the conflict set; -1 when not in conflict.
	ConflictGroup int
}

// Gap is evidence of a damaged or unparseable byte range.
type Gap struct {
	Offset   int // offset in the logical stream
	Length   int
	Segments []Segment
	Reason   string
}

// Result is the outcome of Extract.
type Result struct {
	Records []Record
	Gaps    []Gap
}

type parsedRecord struct {
	seq     uint64
	payload []byte
	start   int
	end     int
}

type shardSpan struct {
	id    string
	start int // global offset, inclusive
	end   int // global offset, exclusive
}

// Extract reads the shards in caller-provided order, reassembles every
// checksum-valid record and reports damaged ranges as gap evidence.
func Extract(shards []Shard, f Format) (*Result, error) {
	if err := f.validate(); err != nil {
		return nil, err
	}
	var stream []byte
	spans := make([]shardSpan, 0, len(shards))
	for _, s := range shards {
		off := len(stream)
		stream = append(stream, s.Data...)
		spans = append(spans, shardSpan{id: s.ID, start: off, end: len(stream)})
	}

	res := &Result{}
	pos := 0
	for pos < len(stream) {
		rel := bytes.Index(stream[pos:], f.Magic)
		if rel < 0 {
			addGap(res, spans, stream, pos, len(stream), "unparseable trailing data")
			break
		}
		start := pos + rel
		if start > pos {
			addGap(res, spans, stream, pos, start, "unparseable data between records")
		}
		if rec, ok := f.parseAt(stream, start); ok {
			res.Records = append(res.Records, Record{
				Seq:           rec.seq,
				Payload:       append([]byte(nil), rec.payload...),
				Offset:        start,
				Length:        rec.end - start,
				Segments:      segmentsFor(spans, start, rec.end),
				ConflictGroup: -1,
			})
			pos = rec.end
			continue
		}
		// Damaged record (corrupt length field, bad checksum, truncated
		// tail, ...): recover at the next checksum-valid record and keep
		// the damaged range as gap evidence.
		recov := -1
		search := start + 1
		for search < len(stream) {
			j := bytes.Index(stream[search:], f.Magic)
			if j < 0 {
				break
			}
			cand := search + j
			if _, ok := f.parseAt(stream, cand); ok {
				recov = cand
				break
			}
			search = cand + 1
		}
		if recov < 0 {
			addGap(res, spans, stream, start, len(stream),
				"damaged record: invalid length or checksum; no further valid record")
			break
		}
		addGap(res, spans, stream, start, recov,
			"damaged record: invalid length or checksum; recovered at next valid record")
		pos = recov
	}
	markConflicts(res)
	return res, nil
}

// parseAt attempts to parse one record at offset off. It reports success
// only when the length field is plausible and the checksum verifies.
func (f Format) parseAt(stream []byte, off int) (parsedRecord, bool) {
	var rec parsedRecord
	hs := len(f.Magic) + f.LengthSize + f.SeqSize
	if off+hs > len(stream) {
		return rec, false
	}
	if !bytes.Equal(stream[off:off+len(f.Magic)], f.Magic) {
		return rec, false
	}
	p := off + len(f.Magic)
	length := readUint(stream[p : p+f.LengthSize])
	p += f.LengthSize
	seq := readUint(stream[p : p+f.SeqSize])
	if length > f.MaxPayload {
		return rec, false
	}
	end := off + hs + int(length) + f.ChecksumSize
	if end > len(stream) {
		return rec, false
	}
	payloadStart := off + hs
	payload := stream[payloadStart : payloadStart+int(length)]
	want := readUint(stream[payloadStart+int(length) : end])
	got := f.Checksum(stream[off+len(f.Magic):payloadStart+int(length)]) & maskFor(f.ChecksumSize)
	if got != want {
		return rec, false
	}
	return parsedRecord{seq: seq, payload: payload, start: off, end: end}, true
}

// markConflicts flags records that share a sequence number but differ in
// payload content. Identical duplicates (e.g. from overlapping shards)
// are kept but not marked as conflicts.
func markConflicts(res *Result) {
	var order []uint64
	bySeq := map[uint64][]int{}
	for i, r := range res.Records {
		if _, ok := bySeq[r.Seq]; !ok {
			order = append(order, r.Seq)
		}
		bySeq[r.Seq] = append(bySeq[r.Seq], i)
	}
	gid := 0
	for _, seq := range order {
		idxs := bySeq[seq]
		if len(idxs) < 2 {
			continue
		}
		first := res.Records[idxs[0]].Payload
		differ := false
		for _, j := range idxs[1:] {
			if !bytes.Equal(first, res.Records[j].Payload) {
				differ = true
				break
			}
		}
		if !differ {
			continue
		}
		for _, j := range idxs {
			res.Records[j].Conflict = true
			res.Records[j].ConflictGroup = gid
		}
		gid++
	}
}

// addGap records gap evidence for stream[start:end], skipping all-zero
// padding which carries no evidence value.
func addGap(res *Result, spans []shardSpan, stream []byte, start, end int, reason string) {
	if start >= end {
		return
	}
	zero := true
	for _, b := range stream[start:end] {
		if b != 0 {
			zero = false
			break
		}
	}
	if zero {
		return
	}
	res.Gaps = append(res.Gaps, Gap{
		Offset:   start,
		Length:   end - start,
		Segments: segmentsFor(spans, start, end),
		Reason:   reason,
	})
}

// segmentsFor maps the global stream range [start, end) back to raw byte
// intervals inside the original shards.
func segmentsFor(spans []shardSpan, start, end int) []Segment {
	var segs []Segment
	for _, sp := range spans {
		s := maxInt(start, sp.start)
		e := minInt(end, sp.end)
		if s < e {
			segs = append(segs, Segment{ShardID: sp.id, Offset: s - sp.start, Length: e - s})
		}
	}
	return segs
}

func readUint(b []byte) uint64 {
	switch len(b) {
	case 1:
		return uint64(b[0])
	case 2:
		return uint64(binary.BigEndian.Uint16(b))
	case 4:
		return uint64(binary.BigEndian.Uint32(b))
	case 8:
		return binary.BigEndian.Uint64(b)
	}
	return 0
}

func maskFor(size int) uint64 {
	if size >= 8 {
		return ^uint64(0)
	}
	return (uint64(1) << (8 * size)) - 1
}

func minInt(a, b int) int {
	if a < b {
		return a
	}
	return b
}

func maxInt(a, b int) int {
	if a > b {
		return a
	}
	return b
}
