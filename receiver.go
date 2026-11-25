package zonetransfer

import "fmt"

// Receiver is the secondary-side zone holder. It keeps serving the previous
// snapshot for the whole duration of an incoming transfer and swaps to the
// new snapshot only after the final version verifies successfully.
type Receiver struct {
	current Snapshot
}

// NewReceiver creates a receiver whose initial served zone is initial.
func NewReceiver(initial Snapshot) *Receiver {
	initial.Records = CloneRecords(initial.Records)
	return &Receiver{current: initial}
}

// Current returns a copy of the snapshot currently being served. The copy
// shares no slice storage with the receiver, so callers cannot mutate its
// state. During Apply, Current keeps returning the pre-transfer snapshot.
func (r *Receiver) Current() Snapshot {
	s := r.current
	s.Records = CloneRecords(s.Records)
	return s
}

// StepObserver, when non-nil, is invoked after every successfully applied
// delta in an IXFR. It exists so tests can prove the receiver still serves
// the old zone while the update is in flight.
type StepObserver func(step int, served Snapshot, assembledSoFar []Record)

// Apply consumes a planned update stream. The receiver's served snapshot is
// not touched until the whole stream has been replayed and the result matches
// plan.Target exactly (version id, serial and record set). Any failure leaves
// the previously served zone intact and returns an error; no partial state is
// ever published.
func (r *Receiver) Apply(plan Plan, observe StepObserver) error {
	old := r.Current()

	switch plan.Kind {
	case KindAXFR:
		// AXFR replaces the whole zone. The only gate is that the advertised
		// target is internally complete before it becomes the served zone.
		if plan.Target.Version == "" {
			return fmt.Errorf("%w: AXFR target has no version id", ErrVerification)
		}
		verified := Snapshot{
			Version: plan.Target.Version,
			Serial:  plan.Target.Serial,
			Records: CloneRecords(plan.Target.Records),
		}
		if observe != nil {
			observe(0, r.Current(), CloneRecords(verified.Records))
		}
		// Swapping a fully built value is the single commit point.
		r.current = verified
		return nil

	case KindIXFR:
		if len(plan.Chain) == 0 {
			// No-op: the target must be exactly what is already served.
			if !snapshotEqual(old, plan.Target) {
				return fmt.Errorf("%w: empty IXFR chain does not lead to target %q", ErrVerification, plan.Target.Version)
			}
			return nil
		}

		assembled := CloneRecords(old.Records)
		expectedVersion := old.Version
		expectedSerial := old.Serial
		for i, d := range plan.Chain {
			// The chain must start at the served zone and stay contiguous.
			if d.FromVersion != expectedVersion {
				return fmt.Errorf("%w: chain step %d expects version %q, have %q",
					ErrVerification, i, d.FromVersion, expectedVersion)
			}
			if d.FromSerial != expectedSerial {
				return fmt.Errorf("%w: chain step %d expects serial %d, have %d",
					ErrVerification, i, d.FromSerial, expectedSerial)
			}
			// Serial ordering across the step must be defined and forward.
			switch CompareSerial(d.FromSerial, d.ToSerial) {
			case SerialOrderIncomparable:
				return fmt.Errorf("%w: chain step %d crosses an incomparable (half-circle) serial boundary", ErrVerification, i)
			case SerialOrderGreater:
				return fmt.Errorf("%w: chain step %d moves serial backwards", ErrVerification, i)
			case SerialOrderEqual:
				// A pinned serial may only connect identical content. Check
				// this explicitly rather than relying on the final compare.
				next, applyErr := ApplyDelta(assembled, d)
				if applyErr != nil {
					return fmt.Errorf("%w: step %d: %v", ErrVerification, i, applyErr)
				}
				if !sameRecords(next, assembled) {
					return fmt.Errorf("%w: chain step %d keeps serial %d but changes content",
						ErrVerification, i, d.FromSerial)
				}
				assembled = next
				expectedVersion = d.ToVersion
				expectedSerial = d.ToSerial
				if observe != nil {
					served := r.Current()
					if !snapshotEqual(served, old) {
						panic("receiver published partial transfer state")
					}
					observe(i+1, served, CloneRecords(assembled))
				}
				continue
			}

			next, err := ApplyDelta(assembled, d)
			if err != nil {
				return fmt.Errorf("%w: step %d: %v", ErrVerification, i, err)
			}
			assembled = next
			expectedVersion = d.ToVersion
			expectedSerial = d.ToSerial

			// While in flight the served zone is provably still the old one.
			if observe != nil {
				served := r.Current()
				if !snapshotEqual(served, old) {
					panic("receiver published partial transfer state")
				}
				observe(i+1, served, CloneRecords(assembled))
			}
		}

		final := Snapshot{
			Version: expectedVersion,
			Serial:  expectedSerial,
			Records: assembled,
		}
		if !snapshotEqual(final, plan.Target) {
			return fmt.Errorf("%w: replayed chain yields %q serial %d, target is %q serial %d",
				ErrVerification,
				final.Version, final.Serial,
				plan.Target.Version, plan.Target.Serial)
		}
		r.current = Snapshot{
			Version: final.Version,
			Serial:  final.Serial,
			Records: CloneRecords(final.Records),
		}
		return nil

	default:
		return fmt.Errorf("%w: unknown transfer kind %d", ErrVerification, plan.Kind)
	}
}

func snapshotEqual(a, b Snapshot) bool {
	if a.Version != b.Version || a.Serial != b.Serial {
		return false
	}
	return sameRecords(a.Records, b.Records)
}
