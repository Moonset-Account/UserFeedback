package dnssecpreflight_test

import (
	"crypto/ed25519"
	"crypto/rand"
	"errors"
	"testing"

	dp "dnssecpreflight"
)

func TestEd25519CallbackVerifiesSyntheticSignature(t *testing.T) {
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	canonical := []byte("canonical example rrset")
	signature := ed25519.Sign(priv, canonical)

	key := dp.DNSKEY{ID: "ed-key", Tag: 12345, Algorithm: dp.AlgED25519, PublicKey: []byte(pub)}
	req := dp.Request{
		RRsets:     []dp.RRSet{{ID: "rr", Canonical: canonical}},
		Keys:       []dp.DNSKEY{key},
		Signatures: []dp.RRSIG{{ID: "rrsig", RRSetID: "rr", Algorithm: dp.AlgED25519, KeyTag: 12345, Inception: 10, Expiration: 11, Signature: signature}},
		Verifier: dp.BoolVerifier(func(k dp.DNSKEY, signedData, sig []byte) bool {
			if len(k.PublicKey) != ed25519.PublicKeySize {
				return false
			}
			return ed25519.Verify(ed25519.PublicKey(k.PublicKey), signedData, sig)
		}),
		Resolvers: []dp.Resolver{{Name: "real-callback", Stages: []dp.Stage{{
			Name:        "default",
			Trust:       []dp.TrustGrant{dp.TrustKey(key, dp.AlgED25519)},
			Requirement: dp.AnyAlgorithmRequirement(),
		}}}},
	}

	report, err := dp.Preflight(req)
	if err != nil {
		t.Fatal(err)
	}
	pair, _ := report.Pair("real-callback", "rr")
	stage, _ := pair.Stage("default")
	if stage.FirstInvalid != nil {
		t.Fatalf("expected Ed25519 signature to verify, got invalid %+v", stage.FirstInvalid)
	}
	if !report.Checks[0].Candidates[0].Verified {
		t.Fatalf("candidate error: %s", report.Checks[0].Candidates[0].Error)
	}
}

func TestFailedCallbackClassifiedAsVerificationFailure(t *testing.T) {
	req := dp.Request{
		RRsets:     []dp.RRSet{{ID: "rr", Canonical: []byte("canonical")}},
		Keys:       []dp.DNSKEY{{ID: "k", Tag: 1, Algorithm: dp.AlgED25519}},
		Signatures: []dp.RRSIG{{ID: "s", RRSetID: "rr", Algorithm: dp.AlgED25519, KeyTag: 1, Inception: 1, Expiration: 2}},
		Verifier:   func(dp.VerifyInput) error { return errors.New("bad signature bytes") },
		Resolvers: []dp.Resolver{{Name: "r", Stages: []dp.Stage{{
			Name:        "default",
			Trust:       []dp.TrustGrant{{KeyID: "k", Algorithm: dp.AlgED25519}},
			Requirement: dp.AllAlgorithmsRequirement(dp.AlgED25519),
		}}}},
	}
	report, err := dp.Preflight(req)
	if err != nil {
		t.Fatal(err)
	}
	pair, _ := report.Pair("r", "rr")
	stage, _ := pair.Stage("default")
	if stage.FirstInvalid == nil {
		t.Fatal("expected invalid result")
	}
	if got := stage.FirstInvalid.Missing[0].Kind; got != "verification-failed" {
		t.Fatalf("missing kind=%q want verification-failed; detail=%q", got, stage.FirstInvalid.Missing[0].Detail)
	}
	if got := report.Checks[0].Candidates[0].Error; got != "bad signature bytes" {
		t.Fatalf("candidate error=%q", got)
	}
}

func TestValidationErrors(t *testing.T) {
	t.Run("missing verifier", func(t *testing.T) {
		_, err := dp.Preflight(dp.Request{
			RRsets:     []dp.RRSet{{ID: "rr"}},
			Signatures: []dp.RRSIG{{ID: "s", RRSetID: "rr"}},
		})
		if err == nil {
			t.Fatal("expected missing Verifier error")
		}
	})

	t.Run("unknown rrset", func(t *testing.T) {
		_, err := dp.Preflight(dp.Request{
			RRsets:     []dp.RRSet{{ID: "rr"}},
			Verifier:   func(dp.VerifyInput) error { return nil },
			Signatures: []dp.RRSIG{{ID: "s", RRSetID: "other"}},
		})
		if err == nil {
			t.Fatal("expected unknown RRset error")
		}
	})

	t.Run("unsafe window", func(t *testing.T) {
		_, err := dp.Preflight(dp.Request{
			RRsets:     []dp.RRSet{{ID: "rr"}},
			Verifier:   func(dp.VerifyInput) error { return nil },
			Signatures: []dp.RRSIG{{ID: "s", RRSetID: "rr", Inception: 0, Expiration: 0x80000000}},
		})
		if err == nil {
			t.Fatal("expected half-circle window error")
		}
	})
}
