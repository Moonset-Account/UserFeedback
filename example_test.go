package dnssecpreflight_test

import (
	"fmt"

	dp "dnssecpreflight"
)

func ExamplePreflight() {
	rrset := dp.RRSet{ID: "www.example.", Canonical: []byte("canonical-rrset")}
	oldKey := dp.DNSKEY{ID: "old", Tag: 100, Algorithm: dp.AlgRSASHA1}
	newKey := dp.DNSKEY{ID: "new", Tag: 200, Algorithm: dp.AlgECDSAP256SHA256}

	req := dp.Request{
		RRsets: []dp.RRSet{rrset},
		Keys:   []dp.DNSKEY{oldKey, newKey},
		Verifier: func(dp.VerifyInput) error {
			return nil // Production callers perform real cryptographic verification here.
		},
		Signatures: []dp.RRSIG{
			{ID: "old-sig", RRSetID: rrset.ID, Algorithm: dp.AlgRSASHA1, KeyTag: 100, Inception: 100, Expiration: 200},
			{ID: "new-sig", RRSetID: rrset.ID, Algorithm: dp.AlgECDSAP256SHA256, KeyTag: 200, Inception: 150, Expiration: 250},
		},
		Resolvers: []dp.Resolver{{
			Name: "strict",
			Stages: []dp.Stage{{
				Name: "dual",
				Trust: []dp.TrustGrant{
					{KeyID: "old", Algorithm: dp.AlgRSASHA1},
					{KeyID: "new", Algorithm: dp.AlgECDSAP256SHA256},
				},
				Requirement: dp.AllAlgorithmsRequirement(dp.AlgRSASHA1, dp.AlgECDSAP256SHA256),
			}},
		}},
	}

	report, err := dp.Preflight(req)
	if err != nil {
		panic(err)
	}
	for _, gap := range report.EarliestIntervals() {
		fmt.Printf("%s %s %s: earliest [%d,%d) missing=%v\n",
			gap.ResolverName, gap.RRSetID, gap.StageName,
			gap.Start, gap.End, gap.MissingAlgorithms)
	}
	// Output:
	// strict www.example. dual: earliest [100,150) missing=[13]
}
