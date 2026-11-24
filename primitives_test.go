package negotiation

import (
	"reflect"
	"testing"
)

func TestParseAddressRangesAndCIDRConversion(t *testing.T) {
	cases := []struct {
		spec string
		want AddressRange
	}{
		{"10.1.2.3", MustNewAddressRange("10.1.2.3", "10.1.2.3")},
		{"10.1.2.0/24", MustNewAddressRange("10.1.2.0", "10.1.2.255")},
		{"10.1.2.7-10.1.2.9", MustNewAddressRange("10.1.2.7", "10.1.2.9")},
		{"2001:db8::/120", MustNewAddressRange("2001:db8::", "2001:db8::ff")},
	}
	for _, tc := range cases {
		got, err := ParseAddressRange(tc.spec)
		if err != nil {
			t.Fatalf("ParseAddressRange(%q): %v", tc.spec, err)
		}
		if got != tc.want {
			t.Fatalf("ParseAddressRange(%q) = %s, want %s", tc.spec, got, tc.want)
		}
	}

	bad := []string{"", "10.0.0.2-10.0.0.1", "not-an-ip", "10.0.0.0/40"}
	for _, spec := range bad {
		if _, err := ParseAddressRange(spec); err == nil {
			t.Fatalf("expected ParseAddressRange(%q) error", spec)
		}
	}
}

func TestAddressRangePrefixes(t *testing.T) {
	cases := []struct {
		r    AddressRange
		want []string
	}{
		{MustParseAddressRange("10.0.0.0/24"), []string{"10.0.0.0/24"}},
		{MustNewAddressRange("10.0.0.128", "10.0.0.255"), []string{"10.0.0.128/25"}},
		{MustNewAddressRange("10.0.0.1", "10.0.0.2"), []string{"10.0.0.1/32", "10.0.0.2/32"}},
	}
	for _, tc := range cases {
		got, err := tc.r.CIDRStrings()
		if err != nil {
			t.Fatal(err)
		}
		if !reflect.DeepEqual(got, tc.want) {
			t.Fatalf("CIDRs for %s = %v, want %v", tc.r, got, tc.want)
		}
	}
}

func TestProtocolAndPortParsing(t *testing.T) {
	protos, err := ParseProtocolSet("TCP,6,any")
	if err != nil {
		t.Fatal(err)
	}
	// TCP and 6 collapse; any absorbs everything.
	if len(protos) != 1 || protos[0] != FullProtocolRange() {
		t.Fatalf("unexpected canonical protocols: %v", protos)
	}
	if got := FullProtocolRange().String(); got != "any" {
		t.Fatalf("full protocol string = %q", got)
	}
	if _, err := ParseProtocolSet("bogus"); err == nil {
		t.Fatal("expected unknown protocol error")
	}

	ports, err := CanonicalizePortRanges([]PortRange{
		SinglePort(443),
		{Start: 8000, End: 8001},
		SinglePort(8002),
		SinglePort(80),
	})
	if err != nil {
		t.Fatal(err)
	}
	wantPorts := []PortRange{SinglePort(80), SinglePort(443), {Start: 8000, End: 8002}}
	if !reflect.DeepEqual(ports, wantPorts) {
		t.Fatalf("ports = %v, want %v", ports, wantPorts)
	}
	if _, err := ParsePortRange("70000"); err == nil {
		t.Fatal("expected invalid port error")
	}
}
