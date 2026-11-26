package dnssecpreflight_test

import (
	"testing"

	dp "dnssecpreflight"
)

func TestUnwrapTimestamp(t *testing.T) {
	const mod = 1 << 32
	cases := []struct {
		name string
		ts   dp.Timestamp
		near int64
		want int64
	}{
		{"low near zero", 10, 0, 10},
		{"high becomes negative around zero", 0xfffffff0, 5, -16},
		{"low shifts after high reference", 10, 0xfffffff0, mod + 10},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := dp.UnwrapTimestamp(tc.ts, tc.near); got != tc.want {
				t.Fatalf("UnwrapTimestamp(%d,%d)=%d want %d", tc.ts, tc.near, got, tc.want)
			}
		})
	}
}

func TestUnwrapWindowWrap(t *testing.T) {
	window, err := dp.UnwrapWindow(0xfffffff0, 0x0000000a, 5)
	if err != nil {
		t.Fatal(err)
	}
	if window.Start != -16 || window.End != 11 {
		t.Fatalf("window = [%d,%d), want [-16,11)", window.Start, window.End)
	}
	circular := window.Circular()
	if !circular.WrapsZero {
		t.Fatal("expected circular window to report zero wrap")
	}
	if circular.Start != 0xfffffff0 || circular.EndInclusive != 0x0a {
		t.Fatalf("circular = %+v, want fffffff0..0a", circular)
	}
}

func TestUnwrapWindowRejectsHalfCircleAndLonger(t *testing.T) {
	if _, err := dp.UnwrapWindow(0, 0x80000000, 0); err == nil {
		t.Fatal("exact half-circle window must be rejected")
	}
	if _, err := dp.UnwrapWindow(0, 0x80000001, 0); err == nil {
		t.Fatal("over-half-circle window must be rejected")
	}
	if _, err := dp.UnwrapWindow(0, 0x7fffffff, 0); err != nil {
		t.Fatalf("just-under-half-circle should be accepted: %v", err)
	}
}

func TestWrappedTimelineFindsGapAcrossZero(t *testing.T) {
	req := dp.Request{
		ReferenceTime: 5,
		RRsets:        []dp.RRSet{{ID: "rr", Canonical: []byte("x")}},
		Keys:          []dp.DNSKEY{{ID: "k", Tag: 1, Algorithm: dp.AlgECDSAP256SHA256}},
		Verifier:      func(dp.VerifyInput) error { return nil },
		Signatures: []dp.RRSIG{
			{ID: "a", RRSetID: "rr", Algorithm: dp.AlgECDSAP256SHA256, KeyTag: 1, Inception: 0xfffffff0, Expiration: 0x0000000a},
			{ID: "b", RRSetID: "rr", Algorithm: dp.AlgECDSAP256SHA256, KeyTag: 1, Inception: 0x0000000d, Expiration: 0x00000020},
		},
		Resolvers: []dp.Resolver{{Name: "any", Stages: []dp.Stage{{
			Name:        "default",
			Trust:       []dp.TrustGrant{{KeyID: "k", Algorithm: dp.AlgECDSAP256SHA256}},
			Requirement: dp.AnyAlgorithmRequirement(),
		}}}},
	}

	report, err := dp.Preflight(req)
	if err != nil {
		t.Fatal(err)
	}
	pair, ok := report.Pair("any", "rr")
	if !ok {
		t.Fatal("pair missing")
	}
	stage, ok := pair.Stage("default")
	if !ok {
		t.Fatal("stage missing")
	}
	if stage.FirstInvalid == nil {
		t.Fatal("expected wrapped gap between expiration 0a and inception 0d")
	}
	got := stage.FirstInvalid
	if got.Start != 11 || got.End != 13 {
		t.Fatalf("gap unwrapped = [%d,%d), want [11,13)", got.Start, got.End)
	}
	if got.StartTime != 0x0b || got.EndInclusive != 0x0c {
		t.Fatalf("gap 32-bit boundaries = %08x..%08x, want 0b..0c", got.StartTime, got.EndInclusive)
	}
}
