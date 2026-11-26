package dnssecpreflight_test

import (
	"bytes"
	"testing"

	dp "dnssecpreflight"
)

func TestExplicitHorizonIncludesPostExpirationFailureTail(t *testing.T) {
	req := dp.Request{
		RRsets:     []dp.RRSet{{ID: "rr", Canonical: []byte("x")}},
		Keys:       []dp.DNSKEY{{ID: "k", Tag: 1, Algorithm: dp.AlgED25519}},
		Signatures: []dp.RRSIG{{ID: "s", RRSetID: "rr", Algorithm: dp.AlgED25519, KeyTag: 1, Inception: 100, Expiration: 200}},
		Verifier:   func(dp.VerifyInput) error { return nil },
		Horizon:    &dp.Interval{Start: 150, End: 300},
		Resolvers: []dp.Resolver{{Name: "r", Stages: []dp.Stage{{
			Name: "default", Trust: []dp.TrustGrant{{KeyID: "k", Algorithm: dp.AlgED25519}},
			Requirement: dp.AnyAlgorithmRequirement(),
		}}}},
	}

	report, err := dp.Preflight(req)
	if err != nil {
		t.Fatal(err)
	}
	pair, _ := report.Pair("r", "rr")
	stage, _ := pair.Stage("default")
	if stage.Timeline != *req.Horizon {
		t.Fatalf("timeline=%v want horizon %v", stage.Timeline, *req.Horizon)
	}
	if len(stage.Segments) != 2 {
		t.Fatalf("segments=%d want valid [150,201) and invalid [201,300)", len(stage.Segments))
	}
	if !stage.Segments[0].Valid || stage.Segments[0].Interval.Start != 150 || stage.Segments[0].Interval.End != 201 {
		t.Fatalf("first segment=%+v", stage.Segments[0])
	}
	if stage.FirstInvalid == nil || stage.FirstInvalid.Start != 201 || stage.FirstInvalid.End != 300 {
		t.Fatalf("first invalid=%+v want [201,300)", stage.FirstInvalid)
	}
}

func TestEngineBuildsSignedDataFromRRSIGMetadata(t *testing.T) {
	canonical := []byte("canonical rrset")
	sig := dp.RRSIG{
		ID: "s", RRSetID: "rr", Algorithm: dp.AlgRSASHA256, KeyTag: 42,
		Inception: 10, Expiration: 20, Signature: []byte("sig"),
		TypeCovered: 1, Labels: 3, OriginalTTL: 300, SignerName: "example.",
	}
	wantData, err := dp.BuildSignedData(sig, canonical)
	if err != nil {
		t.Fatal(err)
	}

	var gotData []byte
	req := dp.Request{
		RRsets:     []dp.RRSet{{ID: "rr", Canonical: canonical}},
		Keys:       []dp.DNSKEY{{ID: "k", Tag: 42, Algorithm: dp.AlgRSASHA256}},
		Signatures: []dp.RRSIG{sig},
		Verifier: func(in dp.VerifyInput) error {
			gotData = append([]byte(nil), in.SignedData...)
			return nil
		},
		Resolvers: []dp.Resolver{{Name: "r", Stages: []dp.Stage{{
			Name: "default", Trust: []dp.TrustGrant{{KeyID: "k", Algorithm: dp.AlgRSASHA256}},
		}}}},
	}
	if _, err := dp.Preflight(req); err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(gotData, wantData) {
		t.Fatalf("callback signed data mismatch\n got: %x\nwant: %x", gotData, wantData)
	}
}
