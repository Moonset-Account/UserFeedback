package fecrx

import "sort"

// set is a GF(2) coefficient/source set: membership 1 means the
// corresponding variable/source appears an odd number of times.
type set map[uint64]struct{}

func newSet(ids ...uint64) set {
	s := make(set, len(ids))
	for _, id := range ids {
		s[id] = struct{}{}
	}
	return s
}

func (s set) clone() set {
	t := make(set, len(s))
	for k := range s {
		t[k] = struct{}{}
	}
	return t
}

// xor performs symmetric difference in place.
func (s set) xor(o set) {
	for k := range o {
		if _, ok := s[k]; ok {
			delete(s, k)
		} else {
			s[k] = struct{}{}
		}
	}
}

func (s set) has(k uint64) bool {
	_, ok := s[k]
	return ok
}

// min returns the smallest element; callers must ensure len(s) > 0.
func (s set) min() uint64 {
	var m uint64
	first := true
	for k := range s {
		if first || k < m {
			m = k
			first = false
		}
	}
	return m
}

func (s set) sorted() []uint64 {
	out := make([]uint64, 0, len(s))
	for k := range s {
		out = append(out, k)
	}
	sort.Slice(out, func(i, j int) bool { return out[i] < out[j] })
	return out
}

func sortedKeys(m map[uint64][]byte) []uint64 {
	out := make([]uint64, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Slice(out, func(i, j int) bool { return out[i] < out[j] })
	return out
}

// xorBytes returns the byte-wise XOR of a and b. Shorter input is
// zero-padded, which is exactly the padding convention of the repair
// packets.
func xorBytes(a, b []byte) []byte {
	n := len(a)
	if len(b) > n {
		n = len(b)
	}
	out := make([]byte, n)
	for i := 0; i < n; i++ {
		var x, y byte
		if i < len(a) {
			x = a[i]
		}
		if i < len(b) {
			y = b[i]
		}
		out[i] = x ^ y
	}
	return out
}

func isZeroBytes(b []byte) bool {
	for _, x := range b {
		if x != 0 {
			return false
		}
	}
	return true
}

// align pads b with zero bytes or truncates it to exactly n bytes.
// n <= 0 means "keep the original encoded length".
func align(b []byte, n int) []byte {
	if n <= 0 {
		n = len(b)
	}
	if n <= len(b) {
		out := make([]byte, n)
		copy(out, b[:n])
		return out
	}
	out := make([]byte, n)
	copy(out, b)
	return out
}

// bitSet reports whether bit idx (byte idx/8, LSB-first within the
// byte) is set in b.
func bitSet(b []byte, idx uint64) bool {
	byteIdx := idx / 8
	bitIdx := idx % 8
	if byteIdx >= uint64(len(b)) {
		return false
	}
	return (b[byteIdx]>>bitIdx)&1 == 1
}

// firstSetBit returns the smallest set bit index of b; callers must
// ensure b is not all zero.
func firstSetBit(b []byte) uint64 {
	for i, x := range b {
		if x != 0 {
			for j := 0; j < 8; j++ {
				if (x>>uint(j))&1 == 1 {
					return uint64(i)*8 + uint64(j)
				}
			}
		}
	}
	return 0
}

func unionSorted(proof []uint64, extra uint64) []uint64 {
	s := newSet(proof...)
	s[extra] = struct{}{}
	return s.sorted()
}
