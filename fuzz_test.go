package mirror

import (
	"bytes"
	"encoding/binary"
	"testing"
)

// FuzzDistribute checks that arbitrary capture metadata never panics and that
// every outcome respects the invariants the distributor promises.
//
// Bounds are packed as big-endian int32 triples (start, end, depth) because the
// fuzz harness accepts []byte arguments.
func FuzzDistribute(f *testing.F) {
	pack := func(values ...int32) []byte {
		out := make([]byte, 4*len(values))
		for i, v := range values {
			binary.BigEndian.PutUint32(out[i*4:], uint32(v))
		}
		return out
	}

	f.Add(make([]byte, 60), pack(0, 14, 1, 14, 18, 1, 18, 38, 2, 38, 60, 3), int16(4))
	f.Add(make([]byte, 60), pack(0, 14, 1, 14, -1, 1, 18, 38, 2), int16(3))
	f.Add(make([]byte, 40), pack(0, 14, 1, 16, 36, 2), int16(4))
	f.Add(make([]byte, 40), pack(0, 14, 1, 12, 32, 2), int16(4))
	f.Add(make([]byte, 40), pack(0, 14, 1, 14, 400, 2), int16(4))
	f.Add([]byte{}, []byte{}, int16(1))

	f.Fuzz(func(t *testing.T, frame []byte, packed []byte, maxDepth int16) {
		var fields []FieldRange
		for i := 0; i+12 <= len(packed); i += 12 {
			fields = append(fields, FieldRange{
				Name:  "f",
				Depth: Depth(int32(binary.BigEndian.Uint32(packed[i+8:]))),
				Start: int(int32(binary.BigEndian.Uint32(packed[i:]))),
				End:   int(int32(binary.BigEndian.Uint32(packed[i+4:]))),
			})
		}

		n := int(maxDepth)
		if n < 0 {
			n = -n
		}
		depth := Depth(n%4) + DepthL2 // clamp to the defined L2..L7 range
		if depth > DepthL7 {
			depth = DepthL7
		}
		cfg := Config{Receivers: []Receiver{
			{Name: "a", Ingresses: []string{"*"}, VLANs: []int{1, 2}, AllowUntagged: true, MaxDepth: depth},
			{Name: "b", Ingresses: []string{"*"}, AllowUntagged: true, MaxDepth: DepthL7},
		}}
		capture := Capture{ID: "fuzz", Ingress: "i", VLANs: []int{1}, Frame: frame, Fields: fields}

		got := Distribute(cfg, capture)
		if len(got.Copies) != 2 {
			t.Fatalf("got %d copies, want 2", len(got.Copies))
		}

		for _, c := range got.Copies {
			if !c.Allowed {
				if c.Data != nil {
					t.Fatalf("denied copy carries bytes")
				}
				continue
			}
			if len(c.Data) > len(frame) {
				t.Fatalf("retained %d bytes from a %d byte frame", len(c.Data), len(frame))
			}
			if !bytes.Equal(c.Data, frame[:len(c.Data)]) {
				t.Fatalf("retained bytes are not a prefix of the frame")
			}
			if c.Truncation != nil {
				if c.Truncation.Offset != len(c.Data) {
					t.Fatalf("truncation offset %d != retained length %d", c.Truncation.Offset, len(c.Data))
				}
				if c.Truncation.Reason == "" {
					t.Fatalf("truncation has no reason")
				}
				if c.Truncation.Detail == "" {
					t.Fatalf("truncation has no detail")
				}
			} else if len(c.Data) != len(frame) {
				t.Fatalf("no truncation recorded but only %d of %d bytes retained", len(c.Data), len(frame))
			}
		}
	})
}
