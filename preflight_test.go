package dnssecpreflight_test

import (
	"errors"
	"testing"

	dp "dnssecpreflight"
)

func coreFixture(t *testing.T) (dp.Request, map[string]int) {
	t.Helper()

	rrset := dp.RRSet{ID: "www.example.", Canonical: []byte("canonical-rrset-bytes")}
	keys := []dp.DNSKEY{
		{ID: "old", Tag: 100, Algorithm: dp.AlgRSASHA1, PublicKey: []byte("old-public")},
		// Put the successful duplicate-tag key first to prove the other same-tag
		// candidate is still attempted after a successful verification.
		{ID: "dup-b", Tag: 300, Algorithm: dp.AlgECDSAP256SHA256, PublicKey: []byte("dup-b-public")},
		{ID: "dup-a", Tag: 300, Algorithm: dp.AlgECDSAP256SHA256, PublicKey: []byte("dup-a-public")},
		{ID: "new", Tag: 200, Algorithm: dp.AlgECDSAP256SHA256, PublicKey: []byte("new-public")},
	}
	signatures := []dp.RRSIG{
		{
			ID: "sig-old", RRSetID: rrset.ID, Algorithm: dp.AlgRSASHA1, KeyTag: 100,
			Inception: 100, Expiration: 200, Signature: []byte("old-signature"),
		},
		{
			ID: "sig-dup", RRSetID: rrset.ID, Algorithm: dp.AlgECDSAP256SHA256, KeyTag: 300,
			Inception: 120, Expiration: 220, Signature: []byte("dup-signature"),
		},
		{
			ID: "sig-new", RRSetID: rrset.ID, Algorithm: dp.AlgECDSAP256SHA256, KeyTag: 200,
			Inception: 150, Expiration: 250, Signature: []byte("new-signature"),
		},
	}

	calls := map[string]int{}
	verifier := func(in dp.VerifyInput) error {
		calls[in.Signature.ID+"/"+in.Key.ID]++
		switch in.Signature.ID + "/" + in.Key.ID {
		case "sig-old/old", "sig-dup/dup-b", "sig-new/new":
			if len(in.SignedData) == 0 {
				t.Errorf("empty signed data for %s/%s", in.Signature.ID, in.Key.ID)
			}
			return nil
		default:
			return errors.New("synthetic signature mismatch")
		}
	}

	req := dp.Request{
		RRsets:     []dp.RRSet{rrset},
		Keys:       keys,
		Signatures: signatures,
		Verifier:   verifier,
	}
	return req, calls
}

func TestDuplicateTagTriesEveryEligibleKey(t *testing.T) {
	req, calls := coreFixture(t)
	req.Resolvers = []dp.Resolver{{
		Name: "any",
		Stages: []dp.Stage{{
			Name:        "default",
			Trust:       []dp.TrustGrant{{KeyID: "old", Algorithm: dp.AlgRSASHA1}, {KeyID: "dup-b", Algorithm: dp.AlgECDSAP256SHA256}, {KeyID: "new", Algorithm: dp.AlgECDSAP256SHA256}},
			Requirement: dp.AnyAlgorithmRequirement(),
		}},
	}}

	report, err := dp.Preflight(req)
	if err != nil {
		t.Fatalf("Preflight returned error: %v", err)
	}

	for _, keyID := range []string{"dup-a", "dup-b"} {
		if got := calls["sig-dup/"+keyID]; got != 1 {
			t.Fatalf("sig-dup/%s verifier calls = %d, want 1; all same-tag candidates must be tried", keyID, got)
		}
	}

	var dupCheck *dp.SignatureCheck
	for i := range report.Checks {
		if report.Checks[i].RRSIGID == "sig-dup" {
			dupCheck = &report.Checks[i]
		}
	}
	if dupCheck == nil {
		t.Fatal("sig-dup check missing")
	}
	if len(dupCheck.Candidates) != 2 {
		t.Fatalf("dup candidates = %d, want 2", len(dupCheck.Candidates))
	}
	verified := map[string]bool{}
	for _, candidate := range dupCheck.Candidates {
		verified[candidate.KeyID] = candidate.Verified
	}
	if !verified["dup-b"] || verified["dup-a"] {
		t.Fatalf("candidate verification = %v, want dup-b verified and dup-a rejected", verified)
	}
}

func TestResolverPoliciesAndEarliestIntervals(t *testing.T) {
	req, _ := coreFixture(t)
	req.Resolvers = []dp.Resolver{
		{
			Name: "legacy-rsa-only",
			Stages: []dp.Stage{{
				Name:        "default",
				Trust:       []dp.TrustGrant{{KeyID: "old", Algorithm: dp.AlgRSASHA1}},
				Requirement: dp.AnyAlgorithmRequirement(dp.AlgRSASHA1),
			}},
		},
		{
			Name: "any-old-or-new",
			Stages: []dp.Stage{{
				Name: "default",
				Trust: []dp.TrustGrant{
					{KeyID: "old", Algorithm: dp.AlgRSASHA1},
					{KeyID: "new", Algorithm: dp.AlgECDSAP256SHA256},
				},
				Requirement: dp.AnyAlgorithmRequirement(),
			}},
		},
		{
			Name: "strict-both-algorithms",
			Stages: []dp.Stage{{
				Name: "default",
				Trust: []dp.TrustGrant{
					{KeyID: "old", Algorithm: dp.AlgRSASHA1},
					{KeyID: "new", Algorithm: dp.AlgECDSAP256SHA256},
					// dup-b is cryptographically valid but intentionally not trusted.
				},
				Requirement: dp.AllAlgorithmsRequirement(dp.AlgRSASHA1, dp.AlgECDSAP256SHA256),
			}},
		},
	}

	report, err := dp.Preflight(req)
	if err != nil {
		t.Fatalf("Preflight: %v", err)
	}

	earliest := report.EarliestIntervals()
	if len(earliest) != 2 {
		t.Fatalf("EarliestIntervals len = %d, want legacy and strict only: %+v", len(earliest), earliest)
	}
	if earliest[0].ResolverName != "legacy-rsa-only" || earliest[0].Start != 201 {
		t.Fatalf("first earliest = %+v", earliest[0])
	}
	if earliest[1].ResolverName != "strict-both-algorithms" || earliest[1].Start != 100 {
		t.Fatalf("second earliest = %+v", earliest[1])
	}

	legacy, ok := report.Pair("legacy-rsa-only", "www.example.")
	if !ok {
		t.Fatal("legacy pair missing")
	}
	legacyStage, ok := legacy.Stage("default")
	if !ok {
		t.Fatal("legacy stage missing")
	}
	if len(legacyStage.Segments) != 5 {
		t.Fatalf("legacy segments = %d, want 5", len(legacyStage.Segments))
	}
	if legacyStage.FirstInvalid == nil {
		t.Fatal("legacy FirstInvalid = nil, want post-RSA expiration gap")
	}
	if legacyStage.FirstInvalid.Start != 201 || legacyStage.FirstInvalid.End != 251 {
		t.Fatalf("legacy first invalid = [%d,%d), want [201,251)", legacyStage.FirstInvalid.Start, legacyStage.FirstInvalid.End)
	}
	if legacyStage.FirstInvalid.StartTime != 201 || legacyStage.FirstInvalid.EndInclusive != 250 {
		t.Fatalf("legacy circular boundaries = %d..%d, want 201..250", legacyStage.FirstInvalid.StartTime, legacyStage.FirstInvalid.EndInclusive)
	}
	if len(legacyStage.FirstInvalid.MissingAlgorithms) != 1 || legacyStage.FirstInvalid.MissingAlgorithms[0] != dp.AlgRSASHA1 {
		t.Fatalf("legacy missing algorithms = %v, want only 5", legacyStage.FirstInvalid.MissingAlgorithms)
	}
	lastSegment := legacyStage.Segments[4]
	var newState *dp.SignatureState
	for i := range lastSegment.Signatures {
		if lastSegment.Signatures[i].RRSIGID == "sig-new" {
			newState = &lastSegment.Signatures[i]
		}
	}
	if newState == nil || !newState.Verified || newState.Usable {
		t.Fatalf("new-key state = %+v, want verified but unusable by legacy resolver", newState)
	}

	anyPair, ok := report.Pair("any-old-or-new", "www.example.")
	if !ok {
		t.Fatal("any pair missing")
	}
	anyStage, _ := anyPair.Stage("default")
	if anyStage.FirstInvalid != nil || len(anyStage.InvalidIntervals) != 0 {
		t.Fatalf("any resolver invalid intervals = %+v, want none", anyStage.InvalidIntervals)
	}
	for _, segment := range anyStage.Segments {
		if !segment.Valid {
			t.Fatalf("any resolver segment %d unexpectedly invalid: %+v", segment.Index, segment.Missing)
		}
	}

	strictPair, ok := report.Pair("strict-both-algorithms", "www.example.")
	if !ok {
		t.Fatal("strict pair missing")
	}
	strictStage, _ := strictPair.Stage("default")
	if len(strictStage.InvalidIntervals) != 2 {
		t.Fatalf("strict invalid intervals = %d, want early missing-13 and late missing-5: %+v",
			len(strictStage.InvalidIntervals), strictStage.InvalidIntervals)
	}
	first := strictStage.InvalidIntervals[0]
	if first.Start != 100 || first.End != 150 {
		t.Fatalf("strict first invalid = [%d,%d), want [100,150)", first.Start, first.End)
	}
	if len(first.MissingAlgorithms) != 1 || first.MissingAlgorithms[0] != dp.AlgECDSAP256SHA256 {
		t.Fatalf("strict early missing = %v, want algorithm 13", first.MissingAlgorithms)
	}
	if first.SegmentIndices[1] != 1 {
		t.Fatalf("first run indices = %v, want [0 1]", first.SegmentIndices)
	}
	var untrustedKind string
	for _, missing := range first.Missing {
		if missing.Algorithm == dp.AlgECDSAP256SHA256 && missing.Kind == "untrusted-capability" {
			untrustedKind = missing.Kind
		}
	}
	if untrustedKind == "" {
		t.Fatalf("early missing details = %+v, want untrusted-capability for verified dup key", first.Missing)
	}
	second := strictStage.InvalidIntervals[1]
	if second.Start != 201 || second.End != 251 {
		t.Fatalf("strict second invalid = [%d,%d), want [201,251)", second.Start, second.End)
	}
	if len(second.MissingAlgorithms) != 1 || second.MissingAlgorithms[0] != dp.AlgRSASHA1 {
		t.Fatalf("strict late missing = %v, want algorithm 5", second.MissingAlgorithms)
	}
}

func TestRolloverStages(t *testing.T) {
	req, _ := coreFixture(t)
	req.Resolvers = []dp.Resolver{{
		Name: "rolling",
		Stages: []dp.Stage{
			{
				Name:        "old-only",
				Trust:       []dp.TrustGrant{{KeyID: "old", Algorithm: dp.AlgRSASHA1}},
				Requirement: dp.AnyAlgorithmRequirement(dp.AlgRSASHA1),
			},
			{
				Name: "dual",
				Trust: []dp.TrustGrant{
					{KeyID: "old", Algorithm: dp.AlgRSASHA1},
					{KeyID: "new", Algorithm: dp.AlgECDSAP256SHA256},
				},
				Requirement: dp.AllAlgorithmsRequirement(dp.AlgRSASHA1, dp.AlgECDSAP256SHA256),
			},
			{
				Name:        "new-only",
				Trust:       []dp.TrustGrant{{KeyID: "new", Algorithm: dp.AlgECDSAP256SHA256}},
				Requirement: dp.AnyAlgorithmRequirement(dp.AlgECDSAP256SHA256),
			},
		},
	}}

	report, err := dp.Preflight(req)
	if err != nil {
		t.Fatal(err)
	}
	pair, ok := report.Pair("rolling", "www.example.")
	if !ok {
		t.Fatal("rolling pair missing")
	}

	oldOnly, _ := pair.Stage("old-only")
	if oldOnly.FirstInvalid == nil || oldOnly.FirstInvalid.Start != 201 {
		t.Fatalf("old-only first invalid = %+v, want start 201", oldOnly.FirstInvalid)
	}
	dual, _ := pair.Stage("dual")
	if dual.FirstInvalid == nil || dual.FirstInvalid.Start != 100 || dual.FirstInvalid.End != 150 {
		t.Fatalf("dual first invalid = %+v, want [100,150)", dual.FirstInvalid)
	}
	newOnly, _ := pair.Stage("new-only")
	if newOnly.FirstInvalid == nil || newOnly.FirstInvalid.Start != 100 || newOnly.FirstInvalid.End != 150 {
		t.Fatalf("new-only first invalid = %+v, want [100,150)", newOnly.FirstInvalid)
	}
	if len(newOnly.InvalidIntervals) != 1 {
		t.Fatalf("new-only intervals = %d, want one early interval; ECDSA then valid through 250", len(newOnly.InvalidIntervals))
	}
}

func TestInclusiveExpirationBoundary(t *testing.T) {
	req := dp.Request{
		RRsets:     []dp.RRSet{{ID: "rr", Canonical: []byte("x")}},
		Keys:       []dp.DNSKEY{{ID: "k", Tag: 7, Algorithm: dp.AlgED25519}},
		Signatures: []dp.RRSIG{{ID: "s", RRSetID: "rr", Algorithm: dp.AlgED25519, KeyTag: 7, Inception: 100, Expiration: 200}},
		Verifier:   func(dp.VerifyInput) error { return nil },
		Resolvers: []dp.Resolver{{Name: "r", Stages: []dp.Stage{{
			Name: "default", Trust: []dp.TrustGrant{{KeyID: "k", Algorithm: dp.AlgED25519}},
		}}}},
	}
	report, err := dp.Preflight(req)
	if err != nil {
		t.Fatal(err)
	}
	pair, _ := report.Pair("r", "rr")
	stage, _ := pair.Stage("default")
	if len(stage.Segments) != 1 {
		t.Fatalf("segments=%d want 1", len(stage.Segments))
	}
	seg := stage.Segments[0]
	if seg.Interval.Start != 100 || seg.Interval.End != 201 {
		t.Fatalf("interval=%v want [100,201)", seg.Interval)
	}
	if seg.EndInclusive != 200 {
		t.Fatalf("EndInclusive=%d want 200", seg.EndInclusive)
	}
	if !seg.Interval.Contains(200) || seg.Interval.Contains(201) {
		t.Fatal("expiration boundary must include second 200 and exclude 201")
	}
	if !seg.Valid {
		t.Fatal("segment should be valid")
	}
}

func TestInterleavedOverlapAndOneSecondGap(t *testing.T) {
	build := func(id string, incep, exp dp.Timestamp) dp.Request {
		return dp.Request{
			RRsets:     []dp.RRSet{{ID: id, Canonical: []byte("x")}},
			Keys:       []dp.DNSKEY{{ID: "k", Tag: 9, Algorithm: dp.AlgED25519}},
			Signatures: []dp.RRSIG{{ID: "s-" + id, RRSetID: id, Algorithm: dp.AlgED25519, KeyTag: 9, Inception: incep, Expiration: exp}},
			Verifier:   func(dp.VerifyInput) error { return nil },
			Resolvers: []dp.Resolver{{Name: "r", Stages: []dp.Stage{{
				Name: "default", Trust: []dp.TrustGrant{{KeyID: dp.AnyKeyID, Algorithm: dp.AlgED25519}},
			}}}},
		}
	}

	overlap := build("overlap", 10, 40)
	overlap.Signatures = append(overlap.Signatures,
		dp.RRSIG{ID: "s2", RRSetID: "overlap", Algorithm: dp.AlgED25519, KeyTag: 9, Inception: 30, Expiration: 60},
		dp.RRSIG{ID: "s3", RRSetID: "overlap", Algorithm: dp.AlgED25519, KeyTag: 9, Inception: 50, Expiration: 80},
	)
	report, err := dp.Preflight(overlap)
	if err != nil {
		t.Fatal(err)
	}
	pair, _ := report.Pair("r", "overlap")
	stage, _ := pair.Stage("default")
	if stage.FirstInvalid != nil {
		t.Fatalf("overlapping interleaved signatures produced gap %+v", stage.FirstInvalid)
	}

	gap := build("gap", 10, 40)
	gap.Signatures = append(gap.Signatures, dp.RRSIG{
		ID: "s2", RRSetID: "gap", Algorithm: dp.AlgED25519, KeyTag: 9, Inception: 42, Expiration: 70,
	})
	gapReport, err := dp.Preflight(gap)
	if err != nil {
		t.Fatal(err)
	}
	gapPair, _ := gapReport.Pair("r", "gap")
	gapStage, _ := gapPair.Stage("default")
	if gapStage.FirstInvalid == nil {
		t.Fatal("expected one-second gap at timestamp 41")
	}
	if gapStage.FirstInvalid.Start != 41 || gapStage.FirstInvalid.End != 42 {
		t.Fatalf("gap = [%d,%d), want [41,42)", gapStage.FirstInvalid.Start, gapStage.FirstInvalid.End)
	}
	if gapStage.FirstInvalid.StartTime != 41 || gapStage.FirstInvalid.EndInclusive != 41 {
		t.Fatalf("gap circular = %d..%d, want 41..41", gapStage.FirstInvalid.StartTime, gapStage.FirstInvalid.EndInclusive)
	}
}
