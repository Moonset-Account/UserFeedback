package slotcheck

import (
	"fmt"
	"sort"
)

type slotState struct {
	ref               SlotRef
	report            SlotReport
	record            *SlotRecord
	policy            DirectionPolicy
	generated         bool
	auxiliaryEvidence bool
}

type checker struct {
	in       Input
	policies map[string]DirectionPolicy
	epochs   map[int64]Epoch
	states   map[SlotRef]*slotState
	global   []Finding

	receiptsByID    map[string]Receipt
	receiptsByChunk map[string][]Receipt
	delaysByChunk   map[string][]DelayedChunk
	reissueClaims   map[string][]SlotRef
	usedReceiptIDs  map[string]bool
}

// Check validates the request and evaluates every template slot/direction.
//
// A non-nil error is reserved for an invalid request that makes checking
// impossible.  Policy failures and missing evidence are returned in Report;
// they are not Go errors.
func Check(in Input) (*Report, error) {
	c := &checker{
		in:              in,
		policies:        map[string]DirectionPolicy{},
		epochs:          map[int64]Epoch{},
		states:          map[SlotRef]*slotState{},
		receiptsByID:    map[string]Receipt{},
		receiptsByChunk: map[string][]Receipt{},
		delaysByChunk:   map[string][]DelayedChunk{},
		reissueClaims:   map[string][]SlotRef{},
		usedReceiptIDs:  map[string]bool{},
	}

	if err := c.normalizePolicies(); err != nil {
		return nil, err
	}
	c.normalizeEpochs()
	if err := c.validateEvidence(); err != nil {
		return nil, err
	}
	c.buildSlotUniverse()
	c.indexEvidence()
	c.evaluateLengthsAndPadding()
	c.reconcileReissues()
	c.reconcileReceipts()
	c.reconcileBacklogReceipts()
	c.reconcileOrphanReceipts()
	c.finalizeStatuses()

	return c.buildReport(), nil
}

func (c *checker) normalizePolicies() error {
	if len(c.in.Policies) == 0 {
		return fmt.Errorf("slotcheck: at least one direction policy is required")
	}
	for _, p := range c.in.Policies {
		if p.Direction == "" {
			return fmt.Errorf("slotcheck: direction policy has empty direction")
		}
		if _, exists := c.policies[p.Direction]; exists {
			return fmt.Errorf("slotcheck: duplicate direction policy %q", p.Direction)
		}
		if p.EmptyPolicy == "" {
			p.EmptyPolicy = EmptyAsZero
		}
		if p.EmptyPolicy != EmptyAsZero && p.EmptyPolicy != EmptyRequireRecord {
			return fmt.Errorf("slotcheck: direction %q has invalid empty_policy %q", p.Direction, p.EmptyPolicy)
		}
		lengths := p.effectiveAllowedLengths()
		if len(lengths) == 0 && p.MaxVisibleBytes == nil {
			return fmt.Errorf("slotcheck: direction %q requires allowed length tiers or max_visible_bytes", p.Direction)
		}
		for _, n := range lengths {
			if n < 0 {
				return fmt.Errorf("slotcheck: direction %q has negative allowed length %d", p.Direction, n)
			}
		}
		if p.MaxVisibleBytes != nil && *p.MaxVisibleBytes < 0 {
			return fmt.Errorf("slotcheck: direction %q has negative max_visible_bytes: %d", p.Direction, *p.MaxVisibleBytes)
		}
		if p.MaxPaddingBytes != nil && *p.MaxPaddingBytes < 0 {
			return fmt.Errorf("slotcheck: direction %q has negative max_padding_bytes: %d", p.Direction, *p.MaxPaddingBytes)
		}
		if p.EpochPaddingBudgetBytes != nil && *p.EpochPaddingBudgetBytes < 0 {
			return fmt.Errorf("slotcheck: direction %q has negative epoch padding budget: %d", p.Direction, *p.EpochPaddingBudgetBytes)
		}
		c.policies[p.Direction] = p
	}
	return nil
}

func (p DirectionPolicy) effectiveAllowedLengths() []int {
	seen := map[int]bool{}
	var out []int
	add := func(n int) {
		if !seen[n] {
			seen[n] = true
			out = append(out, n)
		}
	}
	for _, n := range p.AllowedLengths {
		add(n)
	}
	for _, t := range p.AllowedTiers {
		add(t.Length)
	}
	sort.Ints(out)
	return out
}

func (p DirectionPolicy) allowsLength(n int) (bool, *LengthTier) {
	for _, t := range p.AllowedTiers {
		if t.Length == n {
			tier := t
			return true, &tier
		}
	}
	for _, allowed := range p.AllowedLengths {
		if allowed == n {
			return true, &LengthTier{Name: fmt.Sprintf("length-%d", n), Length: n}
		}
	}
	if p.MaxVisibleBytes != nil && n <= *p.MaxVisibleBytes {
		return true, &LengthTier{Name: fmt.Sprintf("at-or-below-%d", *p.MaxVisibleBytes), Length: n}
	}
	return false, nil
}

func (p DirectionPolicy) requirePaddingIDsAtResync() bool {
	return p.RequirePaddingIDsAtResync == nil || *p.RequirePaddingIDsAtResync
}

func (p DirectionPolicy) requireReceipts() bool {
	return p.RequireMakeupReceipts == nil || *p.RequireMakeupReceipts
}

func (c *checker) normalizeEpochs() {
	for _, e := range c.in.Generations {
		c.epochs[e.Generation] = e
	}
	if len(c.epochs) > 0 {
		return
	}
	// The clock-generation block is recommended, but evidence coordinates are
	// still usable if a caller omits redundant epoch metadata.
	add := func(g int64) {
		if _, ok := c.epochs[g]; !ok {
			c.epochs[g] = Epoch{Generation: g}
		}
	}
	for _, r := range c.in.Records {
		add(r.Generation)
	}
	for _, d := range c.in.DelayedChunks {
		add(d.Origin.Generation)
		if d.Reissue != nil {
			add(d.Reissue.Generation)
		}
	}
	for _, r := range c.in.Receipts {
		add(r.Origin.Generation)
		if r.Reissue != nil {
			add(r.Reissue.Generation)
		}
	}
}

func (c *checker) validateEvidence() error {
	seenRecords := map[SlotRef]bool{}
	seenReceiptIDs := map[string]bool{}
	seenChunks := map[string]bool{}

	for _, r := range c.in.Records {
		if err := validateRef(r.SlotRef, "record"); err != nil {
			return err
		}
		if _, ok := c.policies[r.Direction]; !ok {
			return fmt.Errorf("slotcheck: record %s uses unknown direction %q", r.RecordID, r.Direction)
		}
		if seenRecords[r.SlotRef] {
			return fmt.Errorf("slotcheck: duplicate slot record for %s", r.SlotRef)
		}
		seenRecords[r.SlotRef] = true
		if r.RealBytes < 0 || r.PaddingBytes < 0 || r.PriorBacklogBytes < 0 {
			return fmt.Errorf("slotcheck: negative byte count in record for %s", r.SlotRef)
		}
		if r.ObservedVisibleBytes != nil && *r.ObservedVisibleBytes < 0 {
			return fmt.Errorf("slotcheck: negative observed length for %s", r.SlotRef)
		}
		if r.NewRealBytes != nil && *r.NewRealBytes < 0 {
			return fmt.Errorf("slotcheck: negative new_real_bytes for %s", r.SlotRef)
		}
		if r.NextBacklogBytes != nil && *r.NextBacklogBytes < 0 {
			return fmt.Errorf("slotcheck: negative next_backlog_bytes for %s", r.SlotRef)
		}
		for _, a := range r.PaddingAllocations {
			if a.ID == "" {
				return fmt.Errorf("slotcheck: empty padding allocation id for %s", r.SlotRef)
			}
			if a.Bytes != nil && *a.Bytes < 0 {
				return fmt.Errorf("slotcheck: negative padding allocation bytes for %q", a.ID)
			}
		}
	}

	for _, d := range c.in.DelayedChunks {
		if d.ChunkID == "" {
			return fmt.Errorf("slotcheck: delayed chunk has empty chunk_id")
		}
		if d.Bytes < 0 {
			return fmt.Errorf("slotcheck: delayed chunk %q has negative bytes", d.ChunkID)
		}
		if err := validateRef(d.Origin, "delayed chunk origin"); err != nil {
			return err
		}
		if _, ok := c.policies[d.Origin.Direction]; !ok {
			return fmt.Errorf("slotcheck: delayed chunk %q uses unknown direction %q", d.ChunkID, d.Origin.Direction)
		}
		if d.Reissue != nil {
			if err := validateRef(*d.Reissue, "delayed chunk reissue"); err != nil {
				return err
			}
		}
		if seenChunks[d.ChunkID] {
			return fmt.Errorf("slotcheck: duplicate delayed chunk %q", d.ChunkID)
		}
		seenChunks[d.ChunkID] = true
	}

	for _, r := range c.in.Receipts {
		if r.ID == "" {
			return fmt.Errorf("slotcheck: receipt has empty id")
		}
		if seenReceiptIDs[r.ID] {
			return fmt.Errorf("slotcheck: duplicate receipt id %q", r.ID)
		}
		seenReceiptIDs[r.ID] = true
		if r.ChunkID == "" {
			return fmt.Errorf("slotcheck: receipt %q has empty chunk_id", r.ID)
		}
		if r.Bytes < 0 {
			return fmt.Errorf("slotcheck: receipt %q has negative bytes", r.ID)
		}
		if err := validateRef(r.Origin, "receipt origin"); err != nil {
			return err
		}
		if r.Reissue != nil {
			if err := validateRef(*r.Reissue, "receipt reissue"); err != nil {
				return err
			}
		}
	}

	for _, idx := range c.in.Template.indexes() {
		if idx < 0 {
			return fmt.Errorf("slotcheck: template has negative slot index %d", idx)
		}
	}
	return nil
}

func validateRef(r SlotRef, what string) error {
	if r.Direction == "" {
		return fmt.Errorf("slotcheck: %s coordinate has empty direction", what)
	}
	if r.Slot < 0 {
		return fmt.Errorf("slotcheck: %s coordinate has negative slot index %d", what, r.Slot)
	}
	return nil
}

func (c *checker) buildSlotUniverse() {
	indexes := c.in.Template.indexes()

	// Every generation gets every fixed template slot for every direction.
	for gen := range c.epochs {
		for _, idx := range indexes {
			for dir, policy := range c.policies {
				ref := SlotRef{Generation: gen, Slot: idx, Direction: dir}
				st := c.ensure(ref)
				st.policy = policy
				st.generated = true
				st.report.Empty = true
			}
		}
	}

	// Evidence coordinates are checked even if omitted from the template.
	for i := range c.in.Records {
		r := c.in.Records[i]
		st := c.ensure(r.SlotRef)
		rcopy := r
		st.record = &rcopy
		st.policy = c.policies[r.Direction]
		st.report.Empty = false
	}
	for _, d := range c.in.DelayedChunks {
		st := c.ensure(d.Origin)
		st.policy = c.policies[d.Origin.Direction]
		st.auxiliaryEvidence = true
		if d.Reissue != nil {
			rs := c.ensure(*d.Reissue)
			rs.policy = c.policies[d.Reissue.Direction]
			rs.auxiliaryEvidence = true
		}
	}
	for _, r := range c.in.Receipts {
		st := c.ensure(r.Origin)
		st.auxiliaryEvidence = true
		if p, ok := c.policies[r.Origin.Direction]; ok {
			st.policy = p
		}
		if r.Reissue != nil {
			rs := c.ensure(*r.Reissue)
			rs.auxiliaryEvidence = true
			if p, ok := c.policies[r.Reissue.Direction]; ok {
				rs.policy = p
			}
		}
		if r.ObservedAt != nil {
			os := c.ensure(*r.ObservedAt)
			os.auxiliaryEvidence = true
			if p, ok := c.policies[r.ObservedAt.Direction]; ok {
				os.policy = p
			}
		}
	}

	// Fill policy for any state whose direction is known.
	for ref, st := range c.states {
		if st.policy.Direction == "" {
			st.policy = c.policies[ref.Direction]
		}
	}
}

func (c *checker) ensure(ref SlotRef) *slotState {
	if st, ok := c.states[ref]; ok {
		return st
	}
	st := &slotState{ref: ref, report: SlotReport{Slot: ref}}
	c.states[ref] = st
	return st
}

func (c *checker) indexEvidence() {
	for _, r := range c.in.Receipts {
		rcopy := r
		c.receiptsByID[r.ID] = rcopy
		c.receiptsByChunk[r.ChunkID] = append(c.receiptsByChunk[r.ChunkID], rcopy)
	}
	for _, d := range c.in.DelayedChunks {
		dcopy := d
		c.delaysByChunk[d.ChunkID] = append(c.delaysByChunk[d.ChunkID], dcopy)
	}
}

func (c *checker) evaluateLengthsAndPadding() {
	// Evaluate each known state. Generated empty slots have record == nil.
	for _, st := range c.states {
		_, knownGen := c.epochs[st.ref.Generation]
		if !knownGen {
			st.addEvidence(Finding{
				Code:    CodeUnknownGeneration,
				Kind:    "evidence",
				Message: "record references a clock generation absent from the generation manifest",
			})
		}

		if st.record == nil {
			c.evaluateEmpty(st)
		} else {
			c.evaluateRecord(st)
		}

		// Template membership is a definitive timing check only when the
		// clock generation itself is known. An unknown generation is already
		// an evidence gap and should not be dressed up as a length verdict.
		if knownGen && !st.generated && st.record != nil {
			st.addLength(Finding{
				Code:    CodeSlotOutsideTemplate,
				Kind:    "length",
				Message: "recorded slot coordinate is not one of the configured fixed template slots",
			})
		}
	}

	c.checkPaddingIdentityAndBudgets()
}

func (c *checker) evaluateEmpty(st *slotState) {
	policy := st.policy

	// A coordinate named by a delay/receipt is not an independently observed
	// empty slot. Assuming zero there would fabricate a length result.
	if st.auxiliaryEvidence {
		st.addEvidence(Finding{
			Code:    CodeMissingSlotRecord,
			Kind:    "evidence",
			Message: "slot is referenced by delayed-chunk or receipt evidence but has no transmission record",
		})
		return
	}

	if policy.EmptyPolicy == EmptyRequireRecord {
		st.addEvidence(Finding{
			Code:    CodeMissingSlotRecord,
			Kind:    "evidence",
			Message: "direction policy requires explicit evidence for this slot, but no record was supplied",
		})
		return
	}

	// Absence in the complete offline evidence set is treated as observed
	// zero bytes. Independent direction policies decide whether zero is an
	// allowed exposure tier.
	st.report.Empty = true
	st.report.AccountedVisibleBytes = 0
	st.report.EffectiveVisibleBytes = 0
	allowed, tier := policy.allowsLength(0)
	if allowed {
		st.report.SelectedTier = tier
		return
	}
	st.addLength(Finding{
		Code:     CodeEmptyLengthNotAllowed,
		Kind:     "length",
		Message:  "empty slot exposes length 0, which is not allowed by this direction's tier policy",
		Expected: firstAllowedOrMax(policy),
		Actual:   0,
	})
}

func (c *checker) evaluateRecord(st *slotState) {
	r := st.record
	policy := st.policy
	accounted := r.RealBytes + r.PaddingBytes
	st.report.RealBytes = r.RealBytes
	st.report.PaddingBytes = r.PaddingBytes
	st.report.PriorBacklogBytes = r.PriorBacklogBytes
	st.report.ClaimedPaddingBytes = r.PaddingBytes
	st.report.AccountedVisibleBytes = accounted
	st.report.ObservedVisibleBytes = r.ObservedVisibleBytes

	effective := accounted
	if r.ObservedVisibleBytes != nil {
		effective = *r.ObservedVisibleBytes
		if *r.ObservedVisibleBytes != accounted {
			st.addLength(Finding{
				Code:     CodeVisibleLengthMismatch,
				Kind:     "length",
				Message:  "externally observed visible length differs from real bytes plus padding",
				Expected: accounted,
				Actual:   *r.ObservedVisibleBytes,
			})
		}
	} else if policy.RequireExternalObservation {
		st.addEvidence(Finding{
			Code:    CodeMissingExternalObservation,
			Kind:    "evidence",
			Message: "policy requires an independent observed external length, but none was supplied",
		})
	}
	st.report.EffectiveVisibleBytes = effective

	allowed, tier := policy.allowsLength(effective)
	if allowed {
		st.report.SelectedTier = tier
	} else {
		code := CodeTierNotAllowed
		msg := "effective visible length is not one of the allowed exposure length tiers"
		if policy.MaxVisibleBytes != nil && effective > *policy.MaxVisibleBytes {
			code = CodeAboveMaximumVisible
			msg = "effective visible length exceeds the maximum allowed visible length"
		}
		st.addLength(Finding{
			Code:     code,
			Kind:     "length",
			Message:  msg,
			Expected: firstAllowedOrMax(policy),
			Actual:   effective,
		})
	}

	if policy.MaxPaddingBytes != nil && r.PaddingBytes > *policy.MaxPaddingBytes {
		st.addLength(Finding{
			Code:     CodePaddingSlotBudget,
			Kind:     "length",
			Message:  "slot padding exceeds the per-slot padding budget",
			Expected: *policy.MaxPaddingBytes,
			Actual:   r.PaddingBytes,
		})
	}

	if r.NewRealBytes != nil && r.NextBacklogBytes != nil {
		expectedReal := r.PriorBacklogBytes + *r.NewRealBytes - *r.NextBacklogBytes
		if expectedReal != r.RealBytes {
			st.addLength(Finding{
				Code:     CodeBacklogMismatch,
				Kind:     "length",
				Message:  "backlog equation prior + new - next does not equal real bytes sent",
				Expected: expectedReal,
				Actual:   r.RealBytes,
			})
		}
	}

	if c.isResyncBoundary(st.ref) && r.PaddingBytes > 0 &&
		len(r.PaddingAllocations) == 0 && policy.requirePaddingIDsAtResync() {
		st.addEvidence(Finding{
			Code:    CodePaddingIdentityMissing,
			Kind:    "evidence",
			Message: "padding at a clock-resynchronisation boundary has no allocation ID, so uniqueness across slots cannot be proved",
		})
	}
}

func firstAllowedOrMax(p DirectionPolicy) int {
	lengths := p.effectiveAllowedLengths()
	if len(lengths) > 0 {
		// The nearest/last tier is useful in reports; Expected is advisory.
		return lengths[len(lengths)-1]
	}
	if p.MaxVisibleBytes != nil {
		return *p.MaxVisibleBytes
	}
	return 0
}

func (c *checker) isResyncBoundary(ref SlotRef) bool {
	epoch, ok := c.epochs[ref.Generation]
	if !ok {
		return false
	}
	indexes := c.in.Template.indexes()
	first := c.in.Template.FirstSlot
	if len(indexes) > 0 {
		first = indexes[0]
	}
	last := first
	if len(indexes) > 0 {
		last = indexes[len(indexes)-1]
	}

	// First slot of a generation that itself resynced.
	if epoch.Resync && ref.Slot == first {
		return true
	}
	// Last template slot of the immediately preceding generation, if the
	// next generation is marked as resynced.
	nextGen := c.nextGeneration(ref.Generation)
	if nextGen != nil && nextGen.Resync && ref.Slot == last {
		return true
	}
	return false
}

func (c *checker) nextGeneration(gen int64) *Epoch {
	gens := make([]int64, 0, len(c.epochs))
	for g := range c.epochs {
		gens = append(gens, g)
	}
	sort.Slice(gens, func(i, j int) bool { return gens[i] < gens[j] })
	for i, g := range gens {
		if g == gen && i+1 < len(gens) {
			e := c.epochs[gens[i+1]]
			return &e
		}
	}
	return nil
}

func (c *checker) checkPaddingIdentityAndBudgets() {
	type use struct {
		ref   SlotRef
		bytes *int
	}
	usesByID := map[string][]use{}

	epochClaimed := map[string]int{} // direction + generation key
	for _, st := range c.states {
		if st.record == nil {
			continue
		}
		key := budgetKey(st.ref.Direction, st.ref.Generation)
		epochClaimed[key] += st.record.PaddingBytes

		for _, a := range st.record.PaddingAllocations {
			b := a.Bytes
			usesByID[a.ID] = append(usesByID[a.ID], use{ref: st.ref, bytes: b})
		}
	}

	// Generation padding budgets.
	for _, st := range c.states {
		p := st.policy
		if p.EpochPaddingBudgetBytes == nil || st.record == nil {
			continue
		}
		key := budgetKey(st.ref.Direction, st.ref.Generation)
		total := epochClaimed[key]
		if total > *p.EpochPaddingBudgetBytes {
			// Add once per generation/direction by checking the first state
			// encountered; map iteration is nondeterministic, so use a marker
			// and add to every record? Better add only to the current slot if
			// it is the smallest slot coordinate.
			if c.isFirstSlotForBudget(st.ref) {
				st.addLength(Finding{
					Code:     CodePaddingEpochBudget,
					Kind:     "length",
					Message:  "sum of claimed padding for this direction and clock generation exceeds budget",
					Expected: *p.EpochPaddingBudgetBytes,
					Actual:   total,
				})
			}
		}
	}

	// Duplicate allocation identity.
	ids := make([]string, 0, len(usesByID))
	for id := range usesByID {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	for _, id := range ids {
		uses := usesByID[id]
		if len(uses) <= 1 {
			continue
		}
		for i, u := range uses {
			st := c.states[u.ref]
			other := uses[0].ref
			if i == 0 && len(uses) > 1 {
				other = uses[1].ref
			}
			code := CodePaddingDuplicate
			msg := "same padding allocation is recorded in more than one slot"
			if u.ref.Generation != other.Generation && (c.epochs[u.ref.Generation].Resync || c.epochs[other.Generation].Resync) {
				code = CodePaddingResyncDuplicate
				msg = "same padding allocation is counted on both sides of a clock resynchronisation"
			}
			f := Finding{
				Code:      code,
				Kind:      "length",
				Message:   msg,
				OtherSlot: &other,
				PaddingID: id,
			}
			st.addLength(f)

			// If byte amounts disagree, locate that as an additional error.
			if u.bytes != nil {
				for _, v := range uses {
					if v.bytes != nil && *v.bytes != *u.bytes {
						st.addLength(Finding{
							Code:      CodePaddingAllocationConflict,
							Kind:      "length",
							Message:   "same padding allocation ID has conflicting byte lengths",
							OtherSlot: &v.ref,
							PaddingID: id,
							Expected:  *u.bytes,
							Actual:    *v.bytes,
						})
						break
					}
				}
			}
		}
	}
}

func (c *checker) isFirstSlotForBudget(ref SlotRef) bool {
	for _, st := range c.states {
		if st.ref.Direction != ref.Direction || st.ref.Generation != ref.Generation || st.record == nil {
			continue
		}
		if st.ref.Slot < ref.Slot {
			return false
		}
	}
	return true
}

func budgetKey(direction string, gen int64) string {
	return direction + "\x00g" + fmt.Sprint(gen)
}

func (st *slotState) addLength(f Finding) {
	if f.Kind == "" {
		f.Kind = "length"
	}
	if f.Slot == nil {
		ref := st.ref
		f.Slot = &ref
	}
	st.report.LengthFindings = append(st.report.LengthFindings, f)
}

func (st *slotState) addConfirmation(f Finding) {
	if f.Kind == "" {
		f.Kind = "confirmation"
	}
	if f.Slot == nil {
		ref := st.ref
		f.Slot = &ref
	}
	st.report.ConfirmationGaps = append(st.report.ConfirmationGaps, f)
}

func (st *slotState) addEvidence(f Finding) {
	if f.Kind == "" {
		f.Kind = "evidence"
	}
	if f.Slot == nil {
		ref := st.ref
		f.Slot = &ref
	}
	st.report.EvidenceGaps = append(st.report.EvidenceGaps, f)
}

func (c *checker) finalizeStatuses() {
	for _, st := range c.states {
		sortFindings(st.report.LengthFindings)
		sortFindings(st.report.ConfirmationGaps)
		sortFindings(st.report.EvidenceGaps)

		switch {
		case len(st.report.LengthFindings) > 0:
			st.report.Status = StatusExceededVisibilityTier
		case len(st.report.ConfirmationGaps) > 0 || len(st.report.EvidenceGaps) > 0:
			st.report.Status = StatusInsufficientEvidence
		default:
			st.report.Status = StatusProvenCompliant
		}
	}
}

func sortFindings(fs []Finding) {
	sort.SliceStable(fs, func(i, j int) bool {
		if fs[i].Code != fs[j].Code {
			return fs[i].Code < fs[j].Code
		}
		if fs[i].ChunkID != fs[j].ChunkID {
			return fs[i].ChunkID < fs[j].ChunkID
		}
		if fs[i].PaddingID != fs[j].PaddingID {
			return fs[i].PaddingID < fs[j].PaddingID
		}
		if fs[i].ReceiptID != fs[j].ReceiptID {
			return fs[i].ReceiptID < fs[j].ReceiptID
		}
		return fs[i].Message < fs[j].Message
	})
}

func (c *checker) buildReport() *Report {
	refs := make([]SlotRef, 0, len(c.states))
	for ref := range c.states {
		refs = append(refs, ref)
	}
	sort.Slice(refs, func(i, j int) bool {
		a, b := refs[i], refs[j]
		if a.Direction != b.Direction {
			return a.Direction < b.Direction
		}
		if a.Generation != b.Generation {
			return a.Generation < b.Generation
		}
		return a.Slot < b.Slot
	})

	rep := &Report{GlobalFindings: append([]Finding(nil), c.global...)}
	for _, ref := range refs {
		st := c.states[ref]
		st.report.StatusLabelZH = st.report.Status.LabelZH()
		rep.Slots = append(rep.Slots, st.report)
	}
	for _, s := range rep.Slots {
		rep.Summary.Slots++
		switch s.Status {
		case StatusProvenCompliant:
			rep.Summary.ProvenCompliant++
		case StatusExceededVisibilityTier:
			rep.Summary.ExceededVisibilityTier++
		case StatusInsufficientEvidence:
			rep.Summary.InsufficientEvidence++
		}
		rep.Summary.LengthFindings += len(s.LengthFindings)
		rep.Summary.ConfirmationGaps += len(s.ConfirmationGaps)
		rep.Summary.EvidenceGaps += len(s.EvidenceGaps)
	}
	rep.Summary.GlobalFindings = len(rep.GlobalFindings)
	return rep
}
