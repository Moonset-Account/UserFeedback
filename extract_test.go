package auditlog

import (
	"bytes"
	"encoding/binary"
	"strings"
	"testing"
)

// testFormat is the caller-provided record layout used by the acceptance
// tests: 2-byte magic, 2-byte length, 4-byte sequence, CRC-32 checksum.
func testFormat() Format {
	return Format{
		Magic:        []byte{0xAA, 0x55},
		LengthSize:   2,
		SeqSize:      4,
		ChecksumSize: 4,
		MaxPayload:   256,
		Checksum:     CRC32Checksum,
	}
}

// makeRecord builds a well-formed record for the given format.
func makeRecord(t *testing.T, f Format, seq uint64, payload []byte) []byte {
	t.Helper()
	var buf bytes.Buffer
	buf.Write(f.Magic)
	writeUint(t, &buf, uint64(len(payload)), f.LengthSize)
	writeUint(t, &buf, seq, f.SeqSize)
	buf.Write(payload)
	sum := f.Checksum(buf.Bytes()[len(f.Magic):])
	writeUint(t, &buf, sum, f.ChecksumSize)
	return buf.Bytes()
}

func writeUint(t *testing.T, buf *bytes.Buffer, v uint64, size int) {
	t.Helper()
	var b [8]byte
	binary.BigEndian.PutUint64(b[:], v)
	buf.Write(b[8-size:])
}

// corruptLength rewrites the length field of a record built by makeRecord.
func corruptLength(t *testing.T, f Format, rec []byte, newLen uint64) []byte {
	t.Helper()
	out := append([]byte(nil), rec...)
	var b [8]byte
	binary.BigEndian.PutUint64(b[:], newLen)
	copy(out[len(f.Magic):len(f.Magic)+f.LengthSize], b[8-f.LengthSize:])
	return out
}

// Acceptable result: valid records are reassembled even when a record
// spans a shard boundary, with exact raw byte intervals reported.
func TestExtractValidRecordsAcrossShards(t *testing.T) {
	f := testFormat()
	r1 := makeRecord(t, f, 1, []byte("first"))
	r2 := makeRecord(t, f, 2, []byte("second-record-payload"))
	r3 := makeRecord(t, f, 3, []byte("third"))

	split := 7 // r2 is split across the shard boundary
	shardA := append(append([]byte(nil), r1...), r2[:split]...)
	shardB := append(append([]byte(nil), r2[split:]...), r3...)

	res, err := Extract([]Shard{
		{ID: "shard-a", Data: shardA},
		{ID: "shard-b", Data: shardB},
	}, f)
	if err != nil {
		t.Fatalf("Extract: %v", err)
	}
	if len(res.Gaps) != 0 {
		t.Fatalf("expected no gaps, got %+v", res.Gaps)
	}
	if len(res.Records) != 3 {
		t.Fatalf("expected 3 records, got %d", len(res.Records))
	}
	wantSeqs := []uint64{1, 2, 3}
	wantPayloads := []string{"first", "second-record-payload", "third"}
	for i, rec := range res.Records {
		if rec.Seq != wantSeqs[i] {
			t.Errorf("record %d: seq = %d, want %d", i, rec.Seq, wantSeqs[i])
		}
		if string(rec.Payload) != wantPayloads[i] {
			t.Errorf("record %d: payload = %q, want %q", i, rec.Payload, wantPayloads[i])
		}
		if rec.Conflict {
			t.Errorf("record %d: unexpected conflict flag", i)
		}
	}
	// The spanning record must report raw byte intervals in both shards.
	segs := res.Records[1].Segments
	if len(segs) != 2 {
		t.Fatalf("spanning record: expected 2 segments, got %+v", segs)
	}
	if segs[0] != (Segment{ShardID: "shard-a", Offset: len(r1), Length: split}) {
		t.Errorf("segment 0 = %+v", segs[0])
	}
	if segs[1] != (Segment{ShardID: "shard-b", Offset: 0, Length: len(r2) - split}) {
		t.Errorf("segment 1 = %+v", segs[1])
	}
	if res.Records[1].Length != len(r2) {
		t.Errorf("spanning record length = %d, want %d", res.Records[1].Length, len(r2))
	}
}

// Rejected result: a record with a corrupted length field must not be
// extracted; extraction recovers at the next checksum-valid record and
// the damaged range is preserved as gap evidence.
func TestCorruptLengthRecovery(t *testing.T) {
	f := testFormat()
	r1 := makeRecord(t, f, 1, []byte("ok-1"))
	damaged := corruptLength(t, f, makeRecord(t, f, 2, []byte("broken-payload")), 0xFFFF)
	r3 := makeRecord(t, f, 3, []byte("ok-3"))

	stream := append(append(append([]byte(nil), r1...), damaged...), r3...)
	res, err := Extract([]Shard{{ID: "shard-a", Data: stream}}, f)
	if err != nil {
		t.Fatalf("Extract: %v", err)
	}
	if len(res.Records) != 2 {
		t.Fatalf("expected 2 valid records, got %d", len(res.Records))
	}
	if res.Records[0].Seq != 1 || res.Records[1].Seq != 3 {
		t.Fatalf("recovered seqs = %d, %d; want 1, 3", res.Records[0].Seq, res.Records[1].Seq)
	}
	if string(res.Records[1].Payload) != "ok-3" {
		t.Fatalf("recovered payload = %q", res.Records[1].Payload)
	}
	// The damaged record must be rejected and left as evidence.
	if len(res.Gaps) != 1 {
		t.Fatalf("expected 1 gap, got %+v", res.Gaps)
	}
	gap := res.Gaps[0]
	if gap.Offset != len(r1) || gap.Length != len(damaged) {
		t.Errorf("gap range = [%d, %d), want [%d, %d)",
			gap.Offset, gap.Offset+gap.Length, len(r1), len(r1)+len(damaged))
	}
	if !strings.Contains(gap.Reason, "damaged record") {
		t.Errorf("gap reason = %q", gap.Reason)
	}
	if len(gap.Segments) != 1 || gap.Segments[0].ShardID != "shard-a" ||
		gap.Segments[0].Offset != len(r1) || gap.Segments[0].Length != len(damaged) {
		t.Errorf("gap segments = %+v", gap.Segments)
	}
	// Extraction resumed exactly at the next valid record.
	if res.Records[1].Offset != len(r1)+len(damaged) {
		t.Errorf("recovery offset = %d, want %d", res.Records[1].Offset, len(r1)+len(damaged))
	}
}

// Overlapping shards: two checksum-valid records with the same sequence
// number but different content are both kept and marked as conflicting.
func TestConflictingSequences(t *testing.T) {
	f := testFormat()
	a := makeRecord(t, f, 7, []byte("alpha"))
	b := makeRecord(t, f, 7, []byte("beta!"))

	res, err := Extract([]Shard{
		{ID: "shard-a", Data: a},
		{ID: "shard-b", Data: b},
	}, f)
	if err != nil {
		t.Fatalf("Extract: %v", err)
	}
	if len(res.Records) != 2 {
		t.Fatalf("expected both conflicting records kept, got %d", len(res.Records))
	}
	for i, rec := range res.Records {
		if rec.Seq != 7 {
			t.Errorf("record %d: seq = %d, want 7", i, rec.Seq)
		}
		if !rec.Conflict {
			t.Errorf("record %d: expected conflict flag", i)
		}
		if rec.ConflictGroup != 0 {
			t.Errorf("record %d: conflict group = %d, want 0", i, rec.ConflictGroup)
		}
	}
	if string(res.Records[0].Payload) != "alpha" || string(res.Records[1].Payload) != "beta!" {
		t.Errorf("payloads = %q, %q", res.Records[0].Payload, res.Records[1].Payload)
	}
	if len(res.Gaps) != 0 {
		t.Errorf("expected no gaps, got %+v", res.Gaps)
	}
}

// Identical duplicates from overlapping shards are kept but must not be
// flagged as conflicts (only differing content is conflicting).
func TestIdenticalDuplicatesNotConflict(t *testing.T) {
	f := testFormat()
	rec := makeRecord(t, f, 9, []byte("same"))
	res, err := Extract([]Shard{
		{ID: "shard-a", Data: rec},
		{ID: "shard-b", Data: rec},
	}, f)
	if err != nil {
		t.Fatalf("Extract: %v", err)
	}
	if len(res.Records) != 2 {
		t.Fatalf("expected 2 records, got %d", len(res.Records))
	}
	for i, r := range res.Records {
		if r.Conflict {
			t.Errorf("record %d: identical duplicate wrongly flagged as conflict", i)
		}
	}
}

// Interleaved boundary scenario: a damaged record sits between valid
// records, the following valid record is split across the shard boundary
// inside its checksum field, and a later overlapping shard carries a
// conflicting copy of the first record's sequence number.
func TestInterleavedDamageBoundaryAndConflict(t *testing.T) {
	f := testFormat()
	r1 := makeRecord(t, f, 1, []byte("alpha"))
	// Plausible-but-wrong length: header parses, checksum fails.
	damaged := corruptLength(t, f, makeRecord(t, f, 2, []byte("garbage")), 1)
	r2 := makeRecord(t, f, 3, bytes.Repeat([]byte("x"), 100))
	r3 := makeRecord(t, f, 1, []byte("ALPHA")) // conflicts with r1

	// Split r2 two bytes before its end, i.e. inside the checksum field.
	split := len(r2) - 2
	shardA := append(append(append([]byte(nil), r1...), damaged...), r2[:split]...)
	shardB := append(append([]byte(nil), r2[split:]...), r3...)

	res, err := Extract([]Shard{
		{ID: "shard-a", Data: shardA},
		{ID: "shard-b", Data: shardB},
	}, f)
	if err != nil {
		t.Fatalf("Extract: %v", err)
	}

	// Valid records: r1, r2 (spanning), r3 (conflicting copy).
	if len(res.Records) != 3 {
		t.Fatalf("expected 3 records, got %d", len(res.Records))
	}
	if res.Records[0].Seq != 1 || res.Records[1].Seq != 3 || res.Records[2].Seq != 1 {
		t.Fatalf("seqs = %d, %d, %d", res.Records[0].Seq, res.Records[1].Seq, res.Records[2].Seq)
	}
	if !bytes.Equal(res.Records[1].Payload, bytes.Repeat([]byte("x"), 100)) {
		t.Errorf("spanning record payload mismatch")
	}

	// The damaged record was rejected and left as evidence.
	if len(res.Gaps) != 1 {
		t.Fatalf("expected 1 gap, got %+v", res.Gaps)
	}
	gap := res.Gaps[0]
	if gap.Offset != len(r1) || gap.Length != len(damaged) {
		t.Errorf("gap range = [%d, %d), want [%d, %d)",
			gap.Offset, gap.Offset+gap.Length, len(r1), len(r1)+len(damaged))
	}

	// The spanning record's raw intervals cover both shards, with the
	// checksum tail landing at the start of shard-b.
	segs := res.Records[1].Segments
	if len(segs) != 2 {
		t.Fatalf("spanning record segments = %+v", segs)
	}
	if segs[0] != (Segment{ShardID: "shard-a", Offset: len(r1) + len(damaged), Length: split}) {
		t.Errorf("segment 0 = %+v", segs[0])
	}
	if segs[1] != (Segment{ShardID: "shard-b", Offset: 0, Length: 2}) {
		t.Errorf("segment 1 = %+v", segs[1])
	}

	// Conflict marking: r1 and r3 share seq 1 with different content;
	// the spanning record is unaffected.
	if !res.Records[0].Conflict || !res.Records[2].Conflict {
		t.Errorf("expected records 0 and 2 flagged as conflicting")
	}
	if res.Records[0].ConflictGroup != res.Records[2].ConflictGroup {
		t.Errorf("conflicting records in different groups: %d vs %d",
			res.Records[0].ConflictGroup, res.Records[2].ConflictGroup)
	}
	if res.Records[1].Conflict {
		t.Errorf("spanning record wrongly flagged as conflicting")
	}
}

// A caller-provided format that cannot describe records is rejected.
func TestInvalidFormatRejected(t *testing.T) {
	f := testFormat()
	f.Magic = nil
	if _, err := Extract([]Shard{{ID: "a", Data: []byte{1}}}, f); err == nil {
		t.Fatal("expected error for empty magic marker")
	}
	f = testFormat()
	f.LengthSize = 3
	if _, err := Extract([]Shard{{ID: "a", Data: []byte{1}}}, f); err == nil {
		t.Fatal("expected error for unsupported length size")
	}
}
