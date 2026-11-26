package dnssecpreflight

import "errors"

// ErrSignatureMismatch is returned by BoolVerifier when its boolean callback
// reports that signature bytes do not verify.
var ErrSignatureMismatch = errors.New("dnssecpreflight: signature verification failed")

// RRSet is one canonical DNSSEC RRset input.
type RRSet struct {
	// ID is the preflight-local identifier referenced by RRSIG.RRSetID.
	ID string `json:"id"`
	// Canonical contains the RFC 4034 canonical RRset bytes.
	Canonical []byte `json:"-"`
}

// DNSKEY is the public-key candidate input. Cryptographic parsing is left to
// the Verifier callback.
type DNSKEY struct {
	// ID is a stable policy identifier. If empty, Preflight assigns key-N by
	// input order; setting IDs explicitly is recommended.
	ID string `json:"id,omitempty"`

	Tag       KeyTag    `json:"tag"`
	Algorithm Algorithm `json:"algorithm"`
	PublicKey []byte    `json:"-"`

	// Optional DNSKEY RDATA metadata. The engine does not use Flags for trust
	// because TrustGrant is the explicit resolver policy.
	Flags    uint16 `json:"flags,omitempty"`
	Protocol uint8  `json:"protocol,omitempty"`
}

// RRSIG is one signature input.
type RRSIG struct {
	ID      string `json:"id,omitempty"`
	RRSetID string `json:"rrset_id"`

	Algorithm  Algorithm `json:"algorithm"`
	KeyTag     KeyTag    `json:"key_tag"`
	Inception  Timestamp `json:"inception"`
	Expiration Timestamp `json:"expiration"`
	Signature  []byte    `json:"-"`

	// SignedData optionally supplies the exact bytes passed to Verifier.
	// When nil, the engine uses SignedDataBuilder, BuildSignedData when signer
	// metadata is present, or RRSet.Canonical for simple synthetic signers.
	SignedData []byte `json:"-"`

	// Optional RFC 4034 RRSIG metadata used by BuildSignedData.
	TypeCovered    uint16 `json:"type_covered,omitempty"`
	Labels         uint8  `json:"labels,omitempty"`
	OriginalTTL    uint32 `json:"original_ttl,omitempty"`
	SignerName     string `json:"signer_name,omitempty"`
	SignerNameWire []byte `json:"-"`
}

// VerifyInput is supplied to a caller-controlled signature verification
// callback. The callback must verify Signature over SignedData using Key.
type VerifyInput struct {
	RRSet      RRSet
	Signature  RRSIG
	Key        DNSKEY
	SignedData []byte
}

// Verifier checks signature bytes against one candidate DNSKEY. A non-nil
// result means this candidate did not verify.
type Verifier func(input VerifyInput) error

// BoolVerifier adapts a boolean verification callback.
func BoolVerifier(fn func(key DNSKEY, signedData, signature []byte) bool) Verifier {
	return func(input VerifyInput) error {
		if fn(input.Key, input.SignedData, input.Signature.Signature) {
			return nil
		}
		return ErrSignatureMismatch
	}
}

// AnyKeyID can be used in TrustGrant.KeyID to trust every supplied key that
// matches the grant algorithm. Prefer an explicit key ID or PublicKey.
const AnyKeyID = "*"

// TrustGrant is one resolver-stage trusted key/algorithm combination.
type TrustGrant struct {
	// KeyID matches DNSKEY.ID. Use AnyKeyID to match any candidate key.
	KeyID string `json:"key_id,omitempty"`

	// PublicKey optionally requires an exact DNSKEY.PublicKey match. It is
	// useful when policy embeds key material instead of assigned IDs.
	PublicKey []byte `json:"-"`

	Algorithm Algorithm `json:"algorithm"`
}

// TrustKey creates an exact key/algorithm grant. If key has an ID, the grant
// uses it; PublicKey is also included when present.
func TrustKey(key DNSKEY, algorithm Algorithm) TrustGrant {
	return TrustGrant{KeyID: key.ID, PublicKey: key.PublicKey, Algorithm: algorithm}
}

// RequirementMode selects OR versus AND algorithm acceptance semantics.
type RequirementMode string

const (
	// RequireAnyTrustedSignature accepts one usable trusted signature. If
	// Requirement.Algorithms is nonempty, the usable signature must use one of
	// those algorithms; otherwise any trusted grant algorithm is accepted.
	RequireAnyTrustedSignature RequirementMode = "any"

	// RequireAllAlgorithms requires at least one usable signature for every
	// algorithm listed in Requirement.Algorithms.
	RequireAllAlgorithms RequirementMode = "all-algorithms"
)

// Requirement describes how accepted signature capabilities combine.
type Requirement struct {
	Mode       RequirementMode `json:"mode"`
	Algorithms []Algorithm     `json:"algorithms,omitempty"`
}

// AnyAlgorithmRequirement returns an OR policy, optionally restricted to algs.
func AnyAlgorithmRequirement(algorithms ...Algorithm) Requirement {
	return Requirement{Mode: RequireAnyTrustedSignature, Algorithms: algorithms}
}

// AllAlgorithmsRequirement returns an AND policy requiring every listed alg.
func AllAlgorithmsRequirement(algorithms ...Algorithm) Requirement {
	return Requirement{Mode: RequireAllAlgorithms, Algorithms: algorithms}
}

// Stage is one rollout or policy stage for a resolver type.
type Stage struct {
	Name        string       `json:"name"`
	Trust       []TrustGrant `json:"trust"`
	Requirement Requirement  `json:"requirement"`
}

// Resolver groups named trust stages for one resolver implementation/type.
type Resolver struct {
	Name   string  `json:"name"`
	Stages []Stage `json:"stages"`
}

// SignedDataBuilder can override construction of bytes passed to Verifier.
type SignedDataBuilder func(rrset RRSet, signature RRSIG) []byte

// Request is the complete offline preflight input.
type Request struct {
	RRsets     []RRSet
	Keys       []DNSKEY
	Signatures []RRSIG
	Resolvers  []Resolver

	// Verifier is invoked for every DNSKEY matching an RRSIG by tag and alg.
	Verifier Verifier

	// ReferenceTime resolves placement on the unwrapped 32-bit timeline. It is
	// usually the validation instant. Zero is valid for synthetic timestamps.
	ReferenceTime Timestamp

	// Horizon optionally fixes the analyzed unwrapped interval. When nil, each
	// RRset is analyzed from its earliest RRSIG inception through its latest
	// inclusive expiration.
	Horizon *Interval

	// SignedDataBuilder optionally overrides signed-byte construction.
	SignedDataBuilder SignedDataBuilder
}

// CandidateCheck records one same-tag/algorithm public-key attempt.
type CandidateCheck struct {
	KeyID     string    `json:"key_id"`
	Tag       KeyTag    `json:"tag"`
	Algorithm Algorithm `json:"algorithm"`
	Verified  bool      `json:"verified"`
	Error     string    `json:"error,omitempty"`
}

// SignatureCheck records all candidate-key verification attempts for an RRSIG.
type SignatureCheck struct {
	RRSIGID    string           `json:"rrsig_id"`
	RRSetID    string           `json:"rrset_id"`
	Algorithm  Algorithm        `json:"algorithm"`
	KeyTag     KeyTag           `json:"key_tag"`
	Window     Interval         `json:"window"`
	Candidates []CandidateCheck `json:"candidates"`
}

// SignatureState is an RRSIG's evaluated state during one timeline segment.
type SignatureState struct {
	RRSIGID        string    `json:"rrsig_id"`
	Algorithm      Algorithm `json:"algorithm"`
	KeyTag         KeyTag    `json:"key_tag"`
	Verified       bool      `json:"verified"`
	Usable         bool      `json:"usable"`
	VerifiedKeyIDs []string  `json:"verified_key_ids,omitempty"`
	UsableKeyIDs   []string  `json:"usable_key_ids,omitempty"`
}

// AlgorithmStatus explains whether one relevant algorithm capability exists.
type AlgorithmStatus struct {
	Algorithm                 Algorithm `json:"algorithm"`
	ActiveSignatures          int       `json:"active_signatures"`
	VerifiedSignatures        int       `json:"verified_signatures"`
	TrustedVerifiedSignatures int       `json:"trusted_verified_signatures"`
	Satisfied                 bool      `json:"satisfied"`
}

// MissingCapability identifies an absent signing capability and why.
type MissingCapability struct {
	Algorithm Algorithm `json:"algorithm"`
	KeyID     string    `json:"key_id,omitempty"`
	Kind      string    `json:"kind"`
	Detail    string    `json:"detail"`
}

// Segment is an atomic interval in which active RRSIGs and policy result are
// constant.
type Segment struct {
	Index              int                 `json:"index"`
	Interval           Interval            `json:"interval"`
	StartTime          Timestamp           `json:"start_time"`
	EndInclusive       Timestamp           `json:"end_inclusive"`
	Valid              bool                `json:"valid"`
	ActiveSignatureIDs []string            `json:"active_signature_ids,omitempty"`
	UsableSignatureIDs []string            `json:"usable_signature_ids,omitempty"`
	Signatures         []SignatureState    `json:"signatures,omitempty"`
	AlgorithmStatuses  []AlgorithmStatus   `json:"algorithm_statuses,omitempty"`
	Missing            []MissingCapability `json:"missing,omitempty"`
}

// InvalidInterval is a maximal consecutive range of invalid atomic segments.
type InvalidInterval struct {
	Interval
	StartTime         Timestamp           `json:"start_time"`
	EndInclusive      Timestamp           `json:"end_inclusive"`
	SegmentIndices    []int               `json:"segment_indices"`
	MissingAlgorithms []Algorithm         `json:"missing_algorithms,omitempty"`
	Missing           []MissingCapability `json:"missing,omitempty"`
}

// StageReport contains one stage's segment-by-segment result.
type StageReport struct {
	Name             string            `json:"name"`
	Timeline         Interval          `json:"timeline"`
	Segments         []Segment         `json:"segments"`
	InvalidIntervals []InvalidInterval `json:"invalid_intervals,omitempty"`
	FirstInvalid     *InvalidInterval  `json:"first_invalid,omitempty"`
}

// PairReport is one resolver type's result for one RRset.
type PairReport struct {
	ResolverName string        `json:"resolver"`
	RRSetID      string        `json:"rrset"`
	Timeline     Interval      `json:"timeline"`
	Stages       []StageReport `json:"stages"`
}

// Stage returns the named stage report.
func (p *PairReport) Stage(name string) (*StageReport, bool) {
	for i := range p.Stages {
		if p.Stages[i].Name == name {
			return &p.Stages[i], true
		}
	}
	return nil, false
}

// Report is the complete preflight result.
type Report struct {
	ReferenceTime Timestamp        `json:"reference_time"`
	Checks        []SignatureCheck `json:"checks"`
	Pairs         []PairReport     `json:"pairs"`
}

// EarliestInterval is the first failure interval for one resolver/RRset/stage.
type EarliestInterval struct {
	ResolverName string `json:"resolver"`
	RRSetID      string `json:"rrset"`
	StageName    string `json:"stage"`
	InvalidInterval
}

// EarliestIntervals returns every stage's FirstInvalid interval in report
// order. Stages with no invalid interval are omitted.
func (r *Report) EarliestIntervals() []EarliestInterval {
	var out []EarliestInterval
	for _, pair := range r.Pairs {
		for _, stage := range pair.Stages {
			if stage.FirstInvalid == nil {
				continue
			}
			out = append(out, EarliestInterval{
				ResolverName:    pair.ResolverName,
				RRSetID:         pair.RRSetID,
				StageName:       stage.Name,
				InvalidInterval: *stage.FirstInvalid,
			})
		}
	}
	return out
}

// Pair returns the report for a resolver/RRset pair.
func (r *Report) Pair(resolverName, rrsetID string) (*PairReport, bool) {
	for i := range r.Pairs {
		if r.Pairs[i].ResolverName == resolverName && r.Pairs[i].RRSetID == rrsetID {
			return &r.Pairs[i], true
		}
	}
	return nil, false
}
