package negotiation

import (
	"fmt"
	"math/big"
	"net/netip"
	"strings"
)

// AddressRange is an inclusive start/end IP address range.
// IPv4 and IPv6 ranges are kept in separate families.
type AddressRange struct {
	Start netip.Addr `json:"start"`
	End   netip.Addr `json:"end"`
}

// IPRange is a concise alias for AddressRange.
type IPRange = AddressRange

// NewAddressRange parses two inclusive endpoints.
func NewAddressRange(start, end string) (AddressRange, error) {
	s, err := parsePolicyAddr(start)
	if err != nil {
		return AddressRange{}, err
	}
	e, err := parsePolicyAddr(end)
	if err != nil {
		return AddressRange{}, err
	}
	if s.BitLen() != e.BitLen() {
		return AddressRange{}, fmt.Errorf("address range %s-%s mixes IPv4 and IPv6", start, end)
	}
	if s.Compare(e) > 0 {
		return AddressRange{}, fmt.Errorf("address range start %s is after end %s", start, end)
	}
	return AddressRange{Start: s, End: e}, nil
}

// MustNewAddressRange is intended for tests and compile-time constant setup.
// It panics on invalid input.
func MustNewAddressRange(start, end string) AddressRange {
	r, err := NewAddressRange(start, end)
	if err != nil {
		panic(err)
	}
	return r
}

// ParseAddressRange accepts a single IP, a CIDR, or "start-end".
func ParseAddressRange(spec string) (AddressRange, error) {
	spec = strings.TrimSpace(spec)
	if spec == "" {
		return AddressRange{}, fmt.Errorf("address range is empty")
	}
	if strings.Contains(spec, "/") {
		return CIDRAddressRange(spec)
	}
	if dash := strings.Index(spec, "-"); dash >= 0 {
		return NewAddressRange(spec[:dash], spec[dash+1:])
	}
	addr, err := parsePolicyAddr(spec)
	if err != nil {
		return AddressRange{}, err
	}
	return AddressRange{Start: addr, End: addr}, nil
}

// MustParseAddressRange parses a range or panics.
func MustParseAddressRange(spec string) AddressRange {
	r, err := ParseAddressRange(spec)
	if err != nil {
		panic(err)
	}
	return r
}

// CIDRAddressRange returns the inclusive range covered by prefix.
func CIDRAddressRange(cidr string) (AddressRange, error) {
	prefix, err := netip.ParsePrefix(strings.TrimSpace(cidr))
	if err != nil {
		return AddressRange{}, fmt.Errorf("invalid CIDR %q: %w", cidr, err)
	}
	if prefix.Addr().Zone() != "" {
		return AddressRange{}, fmt.Errorf("zoned CIDR %q is not supported", cidr)
	}
	prefix = prefix.Masked()
	start := prefix.Addr().Unmap()
	bits := start.BitLen()
	if bits == 0 {
		return AddressRange{}, fmt.Errorf("invalid CIDR %q", cidr)
	}

	hostBits := bits - prefix.Bits()
	startInt := new(big.Int).SetBytes(start.AsSlice())
	blockSize := new(big.Int).Lsh(big.NewInt(1), uint(hostBits))
	lastInt := new(big.Int).Add(startInt, new(big.Int).Sub(blockSize, big.NewInt(1)))

	endBytes := make([]byte, bits/8)
	lastInt.FillBytes(endBytes)
	end, ok := netip.AddrFromSlice(endBytes)
	if !ok {
		return AddressRange{}, fmt.Errorf("could not compute end of CIDR %q", cidr)
	}
	end = end.Unmap()
	return AddressRange{Start: start, End: end}, nil
}

// Valid reports whether the range is complete, within one IP family, and ordered.
func (r AddressRange) Valid() bool {
	if !r.Start.IsValid() || !r.End.IsValid() {
		return false
	}
	if r.Start.Zone() != "" || r.End.Zone() != "" {
		return false
	}
	if r.Start.BitLen() == 0 || r.Start.BitLen() != r.End.BitLen() {
		return false
	}
	return r.Start.Compare(r.End) <= 0
}

// Family returns 4 for IPv4 and 6 for IPv6; an invalid range returns 0.
func (r AddressRange) Family() int {
	if !r.Valid() {
		return 0
	}
	if r.Start.Is4() {
		return 4
	}
	return 6
}

// Contains reports whether addr is within the inclusive range.
func (r AddressRange) Contains(addr netip.Addr) bool {
	if !r.Valid() || !addr.IsValid() {
		return false
	}
	addr = addr.Unmap()
	if addr.BitLen() != r.Start.BitLen() {
		return false
	}
	return r.Start.Compare(addr) <= 0 && r.End.Compare(addr) >= 0
}

// String renders a single-address range as an address and otherwise as
// "start-end".
func (r AddressRange) String() string {
	if !r.Valid() {
		return "<invalid-address-range>"
	}
	if r.Start == r.End {
		return r.Start.String()
	}
	return r.Start.String() + "-" + r.End.String()
}

// CIDRStrings converts the exact interval into minimal CIDR blocks.
func (r AddressRange) CIDRStrings() ([]string, error) {
	prefixes, err := r.Prefixes()
	if err != nil {
		return nil, err
	}
	out := make([]string, 0, len(prefixes))
	for _, p := range prefixes {
		out = append(out, p.String())
	}
	return out, nil
}

// Prefixes converts the exact interval into minimal netip.Prefix blocks.
func (r AddressRange) Prefixes() ([]netip.Prefix, error) {
	if !r.Valid() {
		return nil, fmt.Errorf("invalid address range")
	}
	bits := r.Start.BitLen()
	lo := new(big.Int).SetBytes(r.Start.AsSlice())
	hi := new(big.Int).SetBytes(r.End.AsSlice())
	one := big.NewInt(1)

	var prefixes []netip.Prefix
	for lo.Cmp(hi) <= 0 {
		remaining := new(big.Int).Sub(hi, lo)
		remaining.Add(remaining, one) // count of addresses left

		alignmentBits := bits
		if trailing := countTrailingZeroBits(lo, bits); trailing < alignmentBits {
			alignmentBits = trailing
		}

		sizeBits := 0
		for sizeBits < alignmentBits {
			size := new(big.Int).Lsh(one, uint(sizeBits+1))
			if size.Cmp(remaining) > 0 {
				break
			}
			sizeBits++
		}

		addrBytes := make([]byte, bits/8)
		lo.FillBytes(addrBytes)
		addr, ok := netip.AddrFromSlice(addrBytes)
		if !ok {
			return nil, fmt.Errorf("could not reconstruct address at %s", lo.String())
		}
		prefixLength := bits - sizeBits
		prefixes = append(prefixes, netip.PrefixFrom(addr, prefixLength))

		step := new(big.Int).Lsh(one, uint(sizeBits))
		lo.Add(lo, step)
	}
	return prefixes, nil
}

func countTrailingZeroBits(v *big.Int, maxBits int) int {
	if v.Sign() == 0 {
		return maxBits
	}
	trailing := 0
	for trailing < maxBits && v.Bit(trailing) == 0 {
		trailing++
	}
	return trailing
}

func parsePolicyAddr(s string) (netip.Addr, error) {
	a, err := netip.ParseAddr(strings.TrimSpace(s))
	if err != nil {
		return netip.Addr{}, fmt.Errorf("invalid IP address %q: %w", s, err)
	}
	if a.Zone() != "" {
		return netip.Addr{}, fmt.Errorf("zoned IP address %q is not supported", s)
	}
	return a.Unmap(), nil
}

func allAddressRange(family int) AddressRange {
	if family == 4 {
		return AddressRange{
			Start: netip.AddrFrom4([4]byte{0, 0, 0, 0}),
			End:   netip.AddrFrom4([4]byte{255, 255, 255, 255}),
		}
	}
	var end [16]byte
	for i := range end {
		end[i] = 255
	}
	return AddressRange{
		Start: netip.AddrFrom16([16]byte{}),
		End:   netip.AddrFrom16(end),
	}
}

func intersectAddressRange(a, b AddressRange) (AddressRange, bool) {
	if !a.Valid() || !b.Valid() || a.Start.BitLen() != b.Start.BitLen() {
		return AddressRange{}, false
	}
	start := a.Start
	if b.Start.Compare(start) > 0 {
		start = b.Start
	}
	end := a.End
	if b.End.Compare(end) < 0 {
		end = b.End
	}
	if start.Compare(end) > 0 {
		return AddressRange{}, false
	}
	return AddressRange{Start: start, End: end}, true
}

// unionTouchingAddressRange merges overlapping or adjacent same-family ranges.
// It returns false when there is a gap or the families differ.
func unionTouchingAddressRange(a, b AddressRange) (AddressRange, bool) {
	if !a.Valid() || !b.Valid() || a.Family() != b.Family() {
		return AddressRange{}, false
	}
	if a.Start.Compare(b.Start) > 0 {
		a, b = b, a
	}
	// Sorted by start. Merge overlap and inclusive adjacency.
	nextAfterAEnd := a.End.Next()
	if b.Start.Compare(a.End) > 0 && b.Start != nextAfterAEnd {
		return AddressRange{}, false
	}
	end := a.End
	if b.End.Compare(end) > 0 {
		end = b.End
	}
	return AddressRange{Start: a.Start, End: end}, true
}

func compareAddressRange(a, b AddressRange) int {
	fa, fb := a.Family(), b.Family()
	if fa != fb {
		if fa < fb {
			return -1
		}
		return 1
	}
	if c := a.Start.Compare(b.Start); c != 0 {
		return c
	}
	return a.End.Compare(b.End)
}

func addressRangesEqual(a, b AddressRange) bool {
	return a.Start == b.Start && a.End == b.End
}
