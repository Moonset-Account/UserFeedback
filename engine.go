package dnssecpreflight

import (
	"fmt"
	"sort"
)

// Preflight verifies all candidate signatures and evaluates every resolver
// stage for every RRset on an atomic 32-bit timestamp timeline.
func Preflight(request Request) (*Report, error) {
	if len(request.Signatures) > 0 && request.Verifier == nil {
		return nil, fmt.Errorf("dnssecpreflight: Verifier callback is required")
	}
	if request.Horizon != nil && request.Horizon.End <= request.Horizon.Start {
		return nil, fmt.Errorf("dnssecpreflight: Horizon end must be after start")
	}

	rrByID, err := normalizeRRSets(request.RRsets)
	if err != nil {
		return nil, err
	}
	keys, keyByID, err := normalizeKeys(request.Keys)
	if err != nil {
		return nil, err
	}
	signatures, checkByID, checks, err := buildChecks(request, rrByID, keys, keyByID)
	if err != nil {
		return nil, err
	}
	resolvers, err := normalizeResolvers(request.Resolvers, keyByID)
	if err != nil {
		return nil, err
	}

	report := &Report{
		ReferenceTime: request.ReferenceTime,
		Checks:        checks,
		Pairs:         make([]PairReport, 0, len(resolvers)*len(request.RRsets)),
	}

	for _, resolver := range resolvers {
		for _, rrset := range request.RRsets {
			pair := PairReport{
				ResolverName: resolver.Name,
				RRSetID:      rrset.ID,
			}

			rrSigs := signaturesForRRSet(signatures, rrset.ID)
			pair.Timeline = timelineFor(rrSigs, checkByID, request.Horizon, int64(request.ReferenceTime))

			for _, stage := range resolver.Stages {
				stageReport := evaluateStage(stage, pair.Timeline, rrSigs, checkByID, keyByID)
				pair.Stages = append(pair.Stages, stageReport)
			}
			report.Pairs = append(report.Pairs, pair)
		}
	}

	return report, nil
}

func normalizeRRSets(input []RRSet) (map[string]RRSet, error) {
	out := make(map[string]RRSet, len(input))
	for i, rr := range input {
		if rr.ID == "" {
			return nil, fmt.Errorf("dnssecpreflight: RRset at index %d has empty ID", i)
		}
		if _, exists := out[rr.ID]; exists {
			return nil, fmt.Errorf("dnssecpreflight: duplicate RRset ID %q", rr.ID)
		}
		out[rr.ID] = rr
	}
	return out, nil
}

func normalizeKeys(input []DNSKEY) ([]DNSKEY, map[string]DNSKEY, error) {
	keys := append([]DNSKEY(nil), input...)
	byID := make(map[string]DNSKEY, len(keys))
	for i := range keys {
		if keys[i].ID == "" {
			keys[i].ID = fmt.Sprintf("key-%d", i)
		}
		if _, exists := byID[keys[i].ID]; exists {
			return nil, nil, fmt.Errorf("dnssecpreflight: duplicate DNSKEY ID %q", keys[i].ID)
		}
		byID[keys[i].ID] = keys[i]
	}
	return keys, byID, nil
}

func buildChecks(
	request Request,
	rrByID map[string]RRSet,
	keys []DNSKEY,
	keyByID map[string]DNSKEY,
) ([]RRSIG, map[string]SignatureCheck, []SignatureCheck, error) {
	signatures := append([]RRSIG(nil), request.Signatures...)
	checkByID := make(map[string]SignatureCheck, len(signatures))
	checks := make([]SignatureCheck, 0, len(signatures))

	near := int64(request.ReferenceTime)
	if request.Horizon != nil {
		near = request.Horizon.Start
	}

	for i := range signatures {
		sig := &signatures[i]
		if sig.ID == "" {
			sig.ID = fmt.Sprintf("rrsig-%d", i)
		}
		if _, exists := checkByID[sig.ID]; exists {
			return nil, nil, nil, fmt.Errorf("dnssecpreflight: duplicate RRSIG ID %q", sig.ID)
		}
		rr, ok := rrByID[sig.RRSetID]
		if !ok {
			return nil, nil, nil, fmt.Errorf("dnssecpreflight: RRSIG %q references unknown RRset %q", sig.ID, sig.RRSetID)
		}

		window, err := UnwrapWindow(sig.Inception, sig.Expiration, near)
		if err != nil {
			return nil, nil, nil, fmt.Errorf("RRSIG %q: %w", sig.ID, err)
		}
		if request.Horizon != nil {
			window = alignWindow(window, *request.Horizon)
		}

		signedData := sig.SignedData
		if signedData == nil {
			switch {
			case request.SignedDataBuilder != nil:
				signedData = request.SignedDataBuilder(rr, *sig)
			case len(sig.SignerNameWire) > 0 || sig.SignerName != "":
				signedData, err = BuildSignedData(*sig, rr.Canonical)
				if err != nil {
					return nil, nil, nil, fmt.Errorf("RRSIG %q: %w", sig.ID, err)
				}
			default:
				// Synthetic callers commonly sign the canonical RRset bytes
				// directly. Real DNSSEC callers should provide signer metadata
				// or RRSIG.SignedData.
				signedData = rr.Canonical
			}
		}

		check := SignatureCheck{
			RRSIGID:    sig.ID,
			RRSetID:    sig.RRSetID,
			Algorithm:  sig.Algorithm,
			KeyTag:     sig.KeyTag,
			Window:     window,
			Candidates: []CandidateCheck{},
		}

		for _, key := range keys {
			if key.Tag != sig.KeyTag || key.Algorithm != sig.Algorithm {
				continue
			}
			candidate := CandidateCheck{
				KeyID:     key.ID,
				Tag:       key.Tag,
				Algorithm: key.Algorithm,
			}
			verifyErr := request.Verifier(VerifyInput{
				RRSet:      rr,
				Signature:  *sig,
				Key:        key,
				SignedData: signedData,
			})
			if verifyErr == nil {
				candidate.Verified = true
			} else {
				candidate.Error = verifyErr.Error()
			}
			check.Candidates = append(check.Candidates, candidate)
		}

		checks = append(checks, check)
		checkByID[sig.ID] = check
	}

	// keyByID is accepted for API symmetry and future validation; ensure it is
	// referenced even if build logic changes.
	_ = keyByID
	return signatures, checkByID, checks, nil
}

func normalizeResolvers(input []Resolver, keyByID map[string]DNSKEY) ([]Resolver, error) {
	resolvers := make([]Resolver, len(input))
	copy(resolvers, input)
	seenResolver := make(map[string]struct{}, len(resolvers))

	for i := range resolvers {
		if resolvers[i].Name == "" {
			resolvers[i].Name = fmt.Sprintf("resolver-%d", i)
		}
		if _, exists := seenResolver[resolvers[i].Name]; exists {
			return nil, fmt.Errorf("dnssecpreflight: duplicate resolver name %q", resolvers[i].Name)
		}
		seenResolver[resolvers[i].Name] = struct{}{}

		if len(resolvers[i].Stages) == 0 {
			return nil, fmt.Errorf("dnssecpreflight: resolver %q has no stages", resolvers[i].Name)
		}
		resolvers[i].Stages = append([]Stage(nil), resolvers[i].Stages...)
		seenStage := make(map[string]struct{}, len(resolvers[i].Stages))
		for j := range resolvers[i].Stages {
			stage := &resolvers[i].Stages[j]
			if stage.Name == "" {
				stage.Name = fmt.Sprintf("stage-%d", j)
			}
			if _, exists := seenStage[stage.Name]; exists {
				return nil, fmt.Errorf("dnssecpreflight: resolver %q has duplicate stage %q", resolvers[i].Name, stage.Name)
			}
			seenStage[stage.Name] = struct{}{}

			if stage.Requirement.Mode == "" {
				stage.Requirement.Mode = RequireAnyTrustedSignature
			}
			switch stage.Requirement.Mode {
			case RequireAnyTrustedSignature:
			case RequireAllAlgorithms:
				if len(stage.Requirement.Algorithms) == 0 {
					return nil, fmt.Errorf("dnssecpreflight: stage %q requires all algorithms but none were specified", stage.Name)
				}
			default:
				return nil, fmt.Errorf("dnssecpreflight: stage %q has unknown requirement mode %q", stage.Name, stage.Requirement.Mode)
			}
			stage.Requirement.Algorithms = uniqueAlgorithms(stage.Requirement.Algorithms)

			for _, grant := range stage.Trust {
				if grant.KeyID != AnyKeyID && grant.KeyID == "" && len(grant.PublicKey) == 0 {
					return nil, fmt.Errorf("dnssecpreflight: stage %q has trust grant without KeyID/PublicKey/AnyKeyID", stage.Name)
				}
				if grant.KeyID != "" && grant.KeyID != AnyKeyID {
					key, ok := keyByID[grant.KeyID]
					if !ok {
						return nil, fmt.Errorf("dnssecpreflight: stage %q trusts unknown DNSKEY ID %q", stage.Name, grant.KeyID)
					}
					if len(grant.PublicKey) > 0 && !bytesEqual(key.PublicKey, grant.PublicKey) {
						return nil, fmt.Errorf("dnssecpreflight: trust grant KeyID %q PublicKey does not match DNSKEY", grant.KeyID)
					}
				}
			}
		}
	}
	return resolvers, nil
}

func signaturesForRRSet(signatures []RRSIG, rrsetID string) []RRSIG {
	var out []RRSIG
	for _, sig := range signatures {
		if sig.RRSetID == rrsetID {
			out = append(out, sig)
		}
	}
	return out
}

func timelineFor(signatures []RRSIG, checks map[string]SignatureCheck, horizon *Interval, reference int64) Interval {
	if horizon != nil {
		return *horizon
	}
	if len(signatures) == 0 {
		return Interval{Start: reference, End: reference}
	}
	minStart := checks[signatures[0].ID].Window.Start
	maxEnd := checks[signatures[0].ID].Window.End
	for _, sig := range signatures[1:] {
		w := checks[sig.ID].Window
		minStart = minInt64(minStart, w.Start)
		maxEnd = maxInt64(maxEnd, w.End)
	}
	return Interval{Start: minStart, End: maxEnd}
}

func evaluateStage(
	stage Stage,
	timeline Interval,
	signatures []RRSIG,
	checks map[string]SignatureCheck,
	keys map[string]DNSKEY,
) StageReport {
	report := StageReport{Name: stage.Name, Timeline: timeline, Segments: []Segment{}}
	if timeline.End <= timeline.Start {
		return report
	}

	points := []int64{timeline.Start, timeline.End}
	for _, sig := range signatures {
		w := checks[sig.ID].Window
		if clipped, ok := w.Clip(timeline); ok {
			points = append(points, clipped.Start, clipped.End)
		}
	}
	breakpoints := uniqueSortedInt64(points)

	for i := 0; i+1 < len(breakpoints); i++ {
		segmentStart := breakpoints[i]
		segmentEnd := breakpoints[i+1]
		if segmentEnd <= segmentStart {
			continue
		}
		segment := Segment{
			Index:        len(report.Segments),
			Interval:     Interval{Start: segmentStart, End: segmentEnd},
			StartTime:    Timestamp(uint32(segmentStart)),
			EndInclusive: Timestamp(uint32(segmentEnd - 1)),
		}

		states := make([]SignatureState, 0, len(signatures))
		for _, sig := range signatures {
			check := checks[sig.ID]
			if !check.Window.Contains(segmentStart) {
				continue
			}
			state := SignatureState{
				RRSIGID:   sig.ID,
				Algorithm: sig.Algorithm,
				KeyTag:    sig.KeyTag,
			}
			for _, candidate := range check.Candidates {
				if !candidate.Verified {
					continue
				}
				state.Verified = true
				state.VerifiedKeyIDs = appendUniqueString(state.VerifiedKeyIDs, candidate.KeyID)
				key := keys[candidate.KeyID]
				if grantMatchesAny(stage.Trust, sig.Algorithm, key) {
					state.Usable = true
					state.UsableKeyIDs = appendUniqueString(state.UsableKeyIDs, candidate.KeyID)
				}
			}
			states = append(states, state)
			segment.ActiveSignatureIDs = append(segment.ActiveSignatureIDs, sig.ID)
			if state.Usable {
				segment.UsableSignatureIDs = append(segment.UsableSignatureIDs, sig.ID)
			}
		}
		segment.Signatures = states

		relevant := relevantAlgorithms(stage, states)
		segment.AlgorithmStatuses = algorithmStatuses(relevant, states)
		segment.Valid, segment.Missing = evaluateRequirement(stage.Requirement, relevant, segment.AlgorithmStatuses, states, checks)

		report.Segments = append(report.Segments, segment)
	}

	report.InvalidIntervals = collectInvalidIntervals(report.Segments)
	if len(report.InvalidIntervals) > 0 {
		report.FirstInvalid = &report.InvalidIntervals[0]
	}
	return report
}

func grantMatchesAny(grants []TrustGrant, algorithm Algorithm, key DNSKEY) bool {
	for _, grant := range grants {
		if grant.Algorithm != algorithm {
			continue
		}
		if grant.KeyID != "" && grant.KeyID != AnyKeyID && grant.KeyID != key.ID {
			continue
		}
		if len(grant.PublicKey) > 0 && !bytesEqual(grant.PublicKey, key.PublicKey) {
			continue
		}
		return true
	}
	return false
}

func relevantAlgorithms(stage Stage, activeStates []SignatureState) []Algorithm {
	if len(stage.Requirement.Algorithms) > 0 {
		return uniqueAlgorithms(stage.Requirement.Algorithms)
	}
	algs := make([]Algorithm, 0, len(stage.Trust))
	for _, grant := range stage.Trust {
		algs = append(algs, grant.Algorithm)
	}
	return uniqueAlgorithms(algs)
}

func algorithmStatuses(algorithms []Algorithm, states []SignatureState) []AlgorithmStatus {
	statuses := make([]AlgorithmStatus, 0, len(algorithms))
	for _, alg := range algorithms {
		status := AlgorithmStatus{Algorithm: alg}
		for _, state := range states {
			if state.Algorithm != alg {
				continue
			}
			status.ActiveSignatures++
			if state.Verified {
				status.VerifiedSignatures++
			}
			if state.Usable {
				status.TrustedVerifiedSignatures++
			}
		}
		status.Satisfied = status.TrustedVerifiedSignatures > 0
		statuses = append(statuses, status)
	}
	return statuses
}

func evaluateRequirement(
	requirement Requirement,
	relevant []Algorithm,
	statuses []AlgorithmStatus,
	states []SignatureState,
	checks map[string]SignatureCheck,
) (bool, []MissingCapability) {
	statusByAlg := make(map[Algorithm]AlgorithmStatus, len(statuses))
	for _, status := range statuses {
		statusByAlg[status.Algorithm] = status
	}

	if requirement.Mode == RequireAllAlgorithms {
		missing := []MissingCapability{}
		valid := true
		for _, alg := range requirement.Algorithms {
			status := statusByAlg[alg]
			if !status.Satisfied {
				valid = false
				missing = append(missing, missingAlgorithm(alg, states, checks))
			}
		}
		return valid, missing
	}

	// Any mode with explicit algorithm restriction.
	if len(requirement.Algorithms) > 0 {
		missing := []MissingCapability{}
		for _, alg := range requirement.Algorithms {
			status := statusByAlg[alg]
			if status.Satisfied {
				return true, nil
			}
			missing = append(missing, missingAlgorithm(alg, states, checks))
		}
		return false, missing
	}

	// Any trusted capability. Relevant algorithms came from trust grants.
	for _, state := range states {
		if state.Usable {
			return true, nil
		}
	}
	if len(relevant) == 0 {
		return false, []MissingCapability{{
			Kind:   "no-trusted-capability",
			Detail: "stage defines no trusted key/algorithm combinations",
		}}
	}
	missing := make([]MissingCapability, 0, len(relevant))
	for _, alg := range relevant {
		if !statusByAlg[alg].Satisfied {
			missing = append(missing, missingAlgorithm(alg, states, checks))
		}
	}
	return false, missing
}

func missingAlgorithm(alg Algorithm, states []SignatureState, checks map[string]SignatureCheck) MissingCapability {
	active := make([]SignatureState, 0)
	for _, state := range states {
		if state.Algorithm == alg {
			active = append(active, state)
		}
	}
	if len(active) == 0 {
		return MissingCapability{
			Algorithm: alg,
			Kind:      "no-active-signature",
			Detail:    "no RRSIG for this algorithm is inside its 32-bit validity window at this segment",
		}
	}

	anyVerified := false
	for _, state := range active {
		if state.Verified {
			anyVerified = true
			break
		}
	}
	if !anyVerified {
		hasCandidate := false
		for _, state := range active {
			if check, ok := checks[state.RRSIGID]; ok && len(check.Candidates) > 0 {
				hasCandidate = true
				break
			}
		}
		if !hasCandidate {
			return MissingCapability{
				Algorithm: alg,
				Kind:      "no-eligible-dnskey",
				Detail:    "no DNSKEY matches this RRSIG by both key tag and algorithm",
			}
		}
		return MissingCapability{
			Algorithm: alg,
			Kind:      "verification-failed",
			Detail:    "all matching same-tag DNSKEY candidates were rejected by the verifier callback",
		}
	}

	return MissingCapability{
		Algorithm: alg,
		Kind:      "untrusted-capability",
		Detail:    "a same-tag DNSKEY verifies the signature, but no stage trust grant accepts that key/algorithm combination",
	}
}

func collectInvalidIntervals(segments []Segment) []InvalidInterval {
	var intervals []InvalidInterval
	var current *InvalidInterval

	flush := func() {
		if current == nil {
			return
		}
		current.StartTime = Timestamp(uint32(current.Start))
		current.EndInclusive = Timestamp(uint32(current.End - 1))
		current.MissingAlgorithms = uniqueMissingAlgorithms(current.Missing)
		intervals = append(intervals, *current)
		current = nil
	}

	for _, segment := range segments {
		if segment.Valid {
			flush()
			continue
		}
		if current == nil {
			current = &InvalidInterval{
				Interval:       segment.Interval,
				SegmentIndices: []int{segment.Index},
				Missing:        append([]MissingCapability(nil), segment.Missing...),
			}
			continue
		}
		current.End = segment.Interval.End
		current.SegmentIndices = append(current.SegmentIndices, segment.Index)
		for _, missing := range segment.Missing {
			current.Missing = appendMissingUnique(current.Missing, missing)
		}
	}
	flush()
	return intervals
}

func appendMissingUnique(items []MissingCapability, item MissingCapability) []MissingCapability {
	for _, existing := range items {
		if existing == item {
			return items
		}
	}
	return append(items, item)
}

func uniqueMissingAlgorithms(items []MissingCapability) []Algorithm {
	algs := make([]Algorithm, 0, len(items))
	for _, item := range items {
		algs = appendUniqueAlgorithm(algs, item.Algorithm)
	}
	return algs
}

func uniqueAlgorithms(input []Algorithm) []Algorithm {
	out := make([]Algorithm, 0, len(input))
	for _, alg := range input {
		out = appendUniqueAlgorithm(out, alg)
	}
	return out
}

func appendUniqueAlgorithm(items []Algorithm, alg Algorithm) []Algorithm {
	for _, existing := range items {
		if existing == alg {
			return items
		}
	}
	return append(items, alg)
}

func appendUniqueString(items []string, value string) []string {
	for _, existing := range items {
		if existing == value {
			return items
		}
	}
	return append(items, value)
}

func bytesEqual(a, b []byte) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

// SortedChecksByWindow is a small helper intended for diagnostics and future
// CLI output. It returns check IDs sorted by unwrapped window start.
func SortedChecksByWindow(checks []SignatureCheck) []SignatureCheck {
	out := append([]SignatureCheck(nil), checks...)
	sort.SliceStable(out, func(i, j int) bool {
		if out[i].Window.Start != out[j].Window.Start {
			return out[i].Window.Start < out[j].Window.Start
		}
		return out[i].RRSIGID < out[j].RRSIGID
	})
	return out
}
