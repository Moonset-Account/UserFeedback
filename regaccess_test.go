package regaccess_test

import (
	"errors"
	"fmt"
	"reflect"
	"sync"
	"testing"

	"regaccess"
)

func mustOK(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
}

func mustErrIs(t *testing.T, err error, target error) {
	t.Helper()
	if err == nil {
		t.Fatalf("expected error wrapping %v, got nil", target)
	}
	if !errors.Is(err, target) {
		t.Fatalf("error %v does not wrap %v", err, target)
	}
}

func TestReadToClear_CommitClearsObservedEvent(t *testing.T) {
	dev := regaccess.NewDevice()
	session := regaccess.NewSession(dev)
	dev.Raise(regaccess.EventCommandComplete)

	attempt, err := session.Begin("clear-on-read")
	mustOK(t, err)

	read, err := attempt.ReadEvents()
	mustOK(t, err)
	if !read.Has(regaccess.EventCommandComplete) {
		t.Fatalf("first read = 0x%x, want command-complete", read.Mask)
	}
	gen, ok := read.Generation(regaccess.EventCommandComplete)
	if !ok || gen != 1 {
		t.Fatalf("generation = %d,%v; want 1,true", gen, ok)
	}

	// The clear is staged, not externally applied before commit.
	if visible := dev.PeekEvents(); !visible.Has(regaccess.EventCommandComplete) {
		t.Fatal("event disappeared externally before commit")
	}

	receipt, err := attempt.Commit()
	mustOK(t, err)
	if receipt.ID() != "clear-on-read" || len(receipt.EventAcks()) != 1 {
		t.Fatalf("unexpected receipt: id=%q acks=%+v", receipt.ID(), receipt.EventAcks())
	}

	if visible := dev.PeekEvents(); visible.Has(regaccess.EventCommandComplete) {
		t.Fatalf("event mask 0x%x still visible after commit", visible.Mask)
	}
	acked, latest, ok := dev.EventProgress(regaccess.EventCommandComplete)
	if !ok || acked != 1 || latest != 1 {
		t.Fatalf("progress = acked %d latest %d ok %v; want 1/1/true", acked, latest, ok)
	}
}

func TestReadEvents_StagedClearHiddenFromAttemptButVisibleExternally(t *testing.T) {
	dev := regaccess.NewDevice()
	session := regaccess.NewSession(dev)
	dev.Raise(regaccess.EventDataReady)

	attempt, err := session.Begin("staged-event-view")
	mustOK(t, err)

	first, err := attempt.ReadEvents()
	mustOK(t, err)
	if !first.Has(regaccess.EventDataReady) {
		t.Fatalf("first read = 0x%x", first.Mask)
	}

	// Clear-on-read is reflected in the attempt's staged view.
	second, err := attempt.ReadEvents()
	mustOK(t, err)
	if !second.Mask.IsEmpty() {
		t.Fatalf("second attempt-local read = 0x%x, want staged clear", second.Mask)
	}

	// The external device has not committed the acknowledgement yet.
	external := dev.PeekEvents()
	if !external.Has(regaccess.EventDataReady) {
		t.Fatal("external event should remain visible before commit")
	}

	acks := attempt.PendingEventAcks()
	if len(acks) != 1 || acks[0] != (regaccess.EventAck{Bit: regaccess.EventDataReady, Through: 1}) {
		t.Fatalf("pending acks = %+v, want data-ready through 1", acks)
	}

	_, err = attempt.Commit()
	mustOK(t, err)
	if dev.PeekEvents().Has(regaccess.EventDataReady) {
		t.Fatal("event remained visible after staged clear committed")
	}
}

func TestEventRace_ReRaiseSameBitBeforeCommitLeavesNewEvent(t *testing.T) {
	dev := regaccess.NewDevice()
	session := regaccess.NewSession(dev)
	dev.Raise(regaccess.EventDataReady)

	attempt, err := session.Begin("race-same-bit")
	mustOK(t, err)

	observed, err := attempt.ReadEvents()
	mustOK(t, err)
	oldGen, ok := observed.Generation(regaccess.EventDataReady)
	if !ok || oldGen != 1 {
		t.Fatalf("observed generation = %d,%v; want 1,true", oldGen, ok)
	}

	// The device creates a new event generation on the same bit before the
	// instruction commits its delayed clear.
	dev.Raise(regaccess.EventDataReady)

	external := dev.PeekEvents()
	newGen, ok := external.Generation(regaccess.EventDataReady)
	if !external.Has(regaccess.EventDataReady) || !ok || newGen != 2 {
		t.Fatalf("external after re-raise = mask 0x%x gen %d,%v", external.Mask, newGen, ok)
	}
	if acks := attempt.PendingEventAcks(); len(acks) != 1 || acks[0].Through != 1 {
		t.Fatalf("pending ack changed to %+v; want only observed generation 1", acks)
	}

	_, err = attempt.Commit()
	mustOK(t, err)

	after := dev.PeekEvents()
	if !after.Has(regaccess.EventDataReady) {
		t.Fatal("new same-bit event was cleared together with the old observed event")
	}
	visibleGen, ok := after.Generation(regaccess.EventDataReady)
	if !ok || visibleGen != 2 {
		t.Fatalf("visible generation after commit = %d,%v; want 2,true", visibleGen, ok)
	}
	acked, latest, ok := dev.EventProgress(regaccess.EventDataReady)
	if !ok || acked != 1 || latest != 2 {
		t.Fatalf("progress = %d/%d/%v; want acked=1 latest=2", acked, latest, ok)
	}

	// The new event is independently observable and can then be consumed.
	retry, err := session.Begin("consume-new-event")
	mustOK(t, err)
	retryRead, err := retry.ReadEvents()
	mustOK(t, err)
	retryGen, ok := retryRead.Generation(regaccess.EventDataReady)
	if !retryRead.Has(regaccess.EventDataReady) || !ok || retryGen != 2 {
		t.Fatalf("retry read = mask 0x%x gen %d,%v; want generation 2", retryRead.Mask, retryGen, ok)
	}
	_, err = retry.Commit()
	mustOK(t, err)
	if dev.PeekEvents().Has(regaccess.EventDataReady) {
		t.Fatal("generation-2 event was not cleared by its own observed read")
	}
	acked, latest, _ = dev.EventProgress(regaccess.EventDataReady)
	if acked != 2 || latest != 2 {
		t.Fatalf("progress after consuming new event = %d/%d; want 2/2", acked, latest)
	}
}

func TestEventRace_RaiseOtherBitLeavesOnlyOtherBit(t *testing.T) {
	dev := regaccess.NewDevice()
	session := regaccess.NewSession(dev)
	dev.Raise(regaccess.EventCommandComplete)

	attempt, err := session.Begin("race-other-bit")
	mustOK(t, err)
	read, err := attempt.ReadEvents()
	mustOK(t, err)
	if !read.Has(regaccess.EventCommandComplete) {
		t.Fatalf("read = 0x%x", read.Mask)
	}

	dev.Raise(regaccess.EventPeripheralError)
	_, err = attempt.Commit()
	mustOK(t, err)

	after := dev.PeekEvents()
	if after.Has(regaccess.EventCommandComplete) {
		t.Error("command-complete should have been acknowledged")
	}
	if !after.Has(regaccess.EventPeripheralError) {
		t.Error("peripheral-error raised after the read should remain visible")
	}
}

func TestReadEmptyEvents_ThenRaiseBeforeCommitDoesNotAck(t *testing.T) {
	dev := regaccess.NewDevice()
	session := regaccess.NewSession(dev)

	attempt, err := session.Begin("empty-event-read")
	mustOK(t, err)
	read, err := attempt.ReadEvents()
	mustOK(t, err)
	if !read.Mask.IsEmpty() {
		t.Fatalf("initial event read = 0x%x, want zero", read.Mask)
	}

	dev.Raise(regaccess.EventPeripheralError)
	_, err = attempt.Commit()
	mustOK(t, err)

	after := dev.PeekEvents()
	if !after.Has(regaccess.EventPeripheralError) {
		t.Fatal("event raised after an empty read was incorrectly acknowledged")
	}
	acked, latest, ok := dev.EventProgress(regaccess.EventPeripheralError)
	if !ok || acked != 0 || latest != 1 {
		t.Fatalf("progress = %d/%d/%v; want 0/1/true", acked, latest, ok)
	}
}

func TestStageConfig_PreservesStagedAndExternalStates(t *testing.T) {
	dev := regaccess.NewDevice()
	session := regaccess.NewSession(dev)
	// Unrelated high bits must be preserved by field-level read/modify/write.
	dev.SetRegister(regaccess.RegControl, 0xF0000000)

	attempt, err := session.Begin("config-atomic")
	mustOK(t, err)

	cfg := regaccess.Config{Enabled: true, Mode: 5, Speed: 10, Tag: 0x5A}
	if err := attempt.StageConfig(cfg); err != nil {
		t.Fatalf("StageConfig: %v", err)
	}

	stagedControl, ok := attempt.StagedRegister(regaccess.RegControl)
	if !ok {
		t.Fatal("RegControl was not staged")
	}
	const wantControl = uint32(0xF00000AB) // high bits + enable + 5<<1 + 10<<4
	if stagedControl != wantControl {
		t.Fatalf("staged control = 0x%08X, want 0x%08X", stagedControl, wantControl)
	}
	stagedTag, ok := attempt.StagedRegister(regaccess.RegTag)
	if !ok || stagedTag != 0x5A {
		t.Fatalf("staged tag = 0x%x,%v; want 0x5A,true", stagedTag, ok)
	}

	mode, err := attempt.ReadField(regaccess.FieldMode)
	mustOK(t, err)
	if mode != 5 {
		t.Fatalf("staged mode = %d, want 5", mode)
	}

	// External state remains untouched before commit.
	if got := dev.ReadRegister(regaccess.RegControl); got != 0xF0000000 {
		t.Fatalf("external control changed early: 0x%08X", got)
	}
	if got := dev.ReadRegister(regaccess.RegTag); got != 0 {
		t.Fatalf("external tag changed early: 0x%08X", got)
	}

	receipt, err := attempt.Commit()
	mustOK(t, err)
	if len(receipt.Writes()) != 4 {
		t.Fatalf("writes = %d, want four canonical fields", len(receipt.Writes()))
	}
	if got := dev.ReadRegister(regaccess.RegControl); got != wantControl {
		t.Fatalf("committed control = 0x%08X, want 0x%08X", got, wantControl)
	}
	if got := dev.ReadRegister(regaccess.RegTag); got != 0x5A {
		t.Fatalf("committed tag = 0x%02X, want 0x5A", got)
	}
}

func TestStageConfig_InvalidGroupIsAtomic(t *testing.T) {
	dev := regaccess.NewDevice()
	session := regaccess.NewSession(dev)
	attempt, err := session.Begin("invalid-config")
	mustOK(t, err)

	err = attempt.StageConfig(regaccess.Config{Mode: 8}) // MODE is three bits.
	mustErrIs(t, err, regaccess.ErrInvalidValue)

	if _, staged := attempt.StagedRegister(regaccess.RegControl); staged {
		t.Fatal("invalid StageConfig left a staged control write")
	}
	if _, staged := attempt.StagedRegister(regaccess.RegTag); staged {
		t.Fatal("invalid StageConfig left a staged tag write")
	}

	err = attempt.StageField(regaccess.Field{Width: 33}, 0)
	mustErrIs(t, err, regaccess.ErrInvalidField)

	// The attempt remains usable after validation errors.
	mustOK(t, attempt.StageConfig(regaccess.Config{Mode: 1}))
	if _, err := attempt.Commit(); err != nil {
		t.Fatalf("commit after rejected config: %v", err)
	}
}

func TestAbort_DiscardsWritesAndPendingClear(t *testing.T) {
	dev := regaccess.NewDevice()
	session := regaccess.NewSession(dev)
	const originalControl = uint32(0x12345678)
	dev.SetRegister(regaccess.RegControl, originalControl)
	dev.Raise(regaccess.EventCommandComplete)

	attempt, err := session.Begin("abort-once")
	mustOK(t, err)
	read, err := attempt.ReadEvents()
	mustOK(t, err)
	if !read.Has(regaccess.EventCommandComplete) {
		t.Fatalf("read = 0x%x", read.Mask)
	}
	mustOK(t, attempt.StageConfig(regaccess.Config{Enabled: true, Mode: 2, Speed: 3, Tag: 4}))

	if err := attempt.Abort(); err != nil {
		t.Fatalf("Abort: %v", err)
	}
	if err := attempt.Abort(); err != nil {
		t.Fatalf("idempotent Abort returned error: %v", err)
	}
	_, err = attempt.Commit()
	mustErrIs(t, err, regaccess.ErrAttemptAborted)

	if got := dev.ReadRegister(regaccess.RegControl); got != originalControl {
		t.Fatalf("control after abort = 0x%08X, want 0x%08X", got, originalControl)
	}
	if !dev.PeekEvents().Has(regaccess.EventCommandComplete) {
		t.Fatal("pending event acknowledgement was not discarded by abort")
	}
	if acked, latest, _ := dev.EventProgress(regaccess.EventCommandComplete); acked != 0 || latest != 1 {
		t.Fatalf("progress after abort = %d/%d, want 0/1", acked, latest)
	}
	if _, err := session.Replay("abort-once"); !errors.Is(err, regaccess.ErrUnknownInstruction) {
		t.Fatalf("aborted instruction replay error = %v, want ErrUnknownInstruction", err)
	}

	// The same ID is a fresh attempt after withdrawal.
	retry, err := session.Begin("abort-once")
	mustOK(t, err)
	retryRead, err := retry.ReadEvents()
	mustOK(t, err)
	if !retryRead.Has(regaccess.EventCommandComplete) {
		t.Fatalf("fresh attempt after abort read = 0x%x, want event", retryRead.Mask)
	}
	_, err = retry.Commit()
	mustOK(t, err)
	if dev.PeekEvents().Has(regaccess.EventCommandComplete) {
		t.Fatal("retried read did not clear the retained event")
	}
}

func TestInterleavedAttempts_StaleAckCannotClearNewGeneration(t *testing.T) {
	dev := regaccess.NewDevice()
	session := regaccess.NewSession(dev)
	dev.Raise(regaccess.EventCommandComplete)

	first, err := session.Begin("slow-instruction")
	mustOK(t, err)
	observed, err := first.ReadEvents()
	mustOK(t, err)
	if gen, _ := observed.Generation(regaccess.EventCommandComplete); gen != 1 {
		t.Fatalf("slow instruction observed gen %v, want 1", observed)
	}

	// A second instruction reads and commits the same old generation first.
	second, err := session.Begin("fast-instruction")
	mustOK(t, err)
	secondRead, err := second.ReadEvents()
	mustOK(t, err)
	if !secondRead.Has(regaccess.EventCommandComplete) {
		t.Fatalf("fast instruction read = 0x%x", secondRead.Mask)
	}
	_, err = second.Commit()
	mustOK(t, err)
	if dev.PeekEvents().Has(regaccess.EventCommandComplete) {
		t.Fatal("fast commit did not clear generation 1")
	}

	// A new event arrives while the slow instruction is still open.
	dev.Raise(regaccess.EventCommandComplete)

	// The slow instruction's ack still names generation 1. Replaying its
	// delayed commit must not move the acknowledgement frontier past the new 2.
	_, err = first.Commit()
	mustOK(t, err)
	after := dev.PeekEvents()
	if !after.Has(regaccess.EventCommandComplete) {
		t.Fatal("interleaved stale commit cleared the new generation-2 event")
	}
	if gen, _ := after.Generation(regaccess.EventCommandComplete); gen != 2 {
		t.Fatalf("visible generation = %d, want 2", gen)
	}
}

func TestInterleavedAttempts_DifferentFieldsMergeAtomically(t *testing.T) {
	dev := regaccess.NewDevice()
	session := regaccess.NewSession(dev)

	enableAttempt, err := session.Begin("enable")
	mustOK(t, err)
	modeAttempt, err := session.Begin("mode")
	mustOK(t, err)

	mustOK(t, enableAttempt.StageField(regaccess.FieldEnable, 1))
	mustOK(t, modeAttempt.StageField(regaccess.FieldMode, 5))

	_, err = enableAttempt.Commit()
	mustOK(t, err)
	_, err = modeAttempt.Commit()
	mustOK(t, err)

	got := dev.ReadRegister(regaccess.RegControl)
	const want = uint32(1 | (5 << 1))
	if got != want {
		t.Fatalf("interleaved field commits = 0x%02X, want 0x%02X", got, want)
	}
}

func TestCommittedInstructionReplay_ReturnsOriginalReads(t *testing.T) {
	dev := regaccess.NewDevice()
	session := regaccess.NewSession(dev)
	dev.Raise(regaccess.EventCommandComplete)

	attempt, err := session.Begin("replay-cmd")
	mustOK(t, err)
	originalEvents, err := attempt.ReadEvents()
	mustOK(t, err)
	originalControl, err := attempt.ReadRegister(regaccess.RegControl)
	mustOK(t, err)
	if originalControl != 0 {
		t.Fatalf("initial control = 0x%x, want 0", originalControl)
	}

	cfg := regaccess.Config{Enabled: true, Mode: 5, Speed: 10, Tag: 0x5A}
	mustOK(t, attempt.StageConfig(cfg))
	stagedMode, err := attempt.ReadField(regaccess.FieldMode)
	mustOK(t, err)
	stagedTag, err := attempt.ReadRegister(regaccess.RegTag)
	mustOK(t, err)

	receipt, err := attempt.Commit()
	mustOK(t, err)

	// Mutate the device and create a newer event after the commit. Replay must
	// remain a snapshot of the instruction's original observations.
	dev.Raise(regaccess.EventCommandComplete)
	dev.SetRegister(regaccess.RegControl, 0xDEADBEEF)
	dev.SetRegister(regaccess.RegTag, 0xFFFFFFFF)

	replay, err := session.Replay("replay-cmd")
	mustOK(t, err)
	if replay.Remaining() != 4 {
		t.Fatalf("remaining = %d, want four recorded reads", replay.Remaining())
	}

	replayedEvents, err := replay.ReadEvents()
	mustOK(t, err)
	if !reflect.DeepEqual(replayedEvents, originalEvents) {
		t.Fatalf("event replay = %+v, want %+v", replayedEvents, originalEvents)
	}

	replayedControl, err := replay.ReadRegister(regaccess.RegControl)
	mustOK(t, err)
	if replayedControl != originalControl {
		t.Fatalf("control replay = 0x%x, want original 0x%x", replayedControl, originalControl)
	}

	// At this point the next record is the full RegControl read made by
	// ReadField(FieldMode). Asking for the wrong register must mismatch
	// without consuming that record.
	_, err = replay.ReadRegister(regaccess.RegTag)
	mustErrIs(t, err, regaccess.ErrReplayMismatch)

	replayedMode, err := replay.ReadField(regaccess.FieldMode)
	mustOK(t, err)
	if replayedMode != stagedMode || replayedMode != 5 {
		t.Fatalf("mode replay = %d, want staged %d/5", replayedMode, stagedMode)
	}

	replayedTag, err := replay.ReadRegister(regaccess.RegTag)
	mustOK(t, err)
	if replayedTag != stagedTag || replayedTag != 0x5A {
		t.Fatalf("tag replay = 0x%x, want staged 0x5A", replayedTag)
	}

	_, err = replay.ReadRegister(regaccess.RegControl)
	mustErrIs(t, err, regaccess.ErrReplayExhausted)

	// Replay is side-effect free: the post-commit generation-2 event remains.
	acked, latest, ok := dev.EventProgress(regaccess.EventCommandComplete)
	if !ok || acked != 1 || latest != 2 {
		t.Fatalf("progress after replay = %d/%d/%v; want 1/2/true", acked, latest, ok)
	}

	// A second replay gets an independent cursor and the same result.
	replayAgain, err := session.Replay("replay-cmd")
	mustOK(t, err)
	eventsAgain, err := replayAgain.ReadEvents()
	mustOK(t, err)
	if gen, _ := eventsAgain.Generation(regaccess.EventCommandComplete); gen != 1 {
		t.Fatalf("second replay generation = %d, want original 1", gen)
	}
	if receipt.Sequence() == 0 {
		t.Fatal("commit sequence was not assigned")
	}
}

func TestCommittedIDAndAttemptAreIdempotent(t *testing.T) {
	dev := regaccess.NewDevice()
	session := regaccess.NewSession(dev)
	attempt, err := session.Begin("idem")
	mustOK(t, err)
	mustOK(t, attempt.StageRegister(0x20, 0x1234))

	first, err := attempt.Commit()
	mustOK(t, err)
	second, err := attempt.Commit()
	mustOK(t, err)
	if first != second {
		t.Fatal("repeat Commit did not return the same receipt")
	}

	_, err = session.Begin("idem")
	mustErrIs(t, err, regaccess.ErrInstructionCommitted)

	_, err = session.Replay("missing")
	mustErrIs(t, err, regaccess.ErrUnknownInstruction)

	if err := attempt.Abort(); !errors.Is(err, regaccess.ErrAttemptCommitted) {
		t.Fatalf("Abort after commit error = %v, want ErrAttemptCommitted", err)
	}
}

func TestConcurrentCommitsProduceAtomicRegisterPairs(t *testing.T) {
	dev := regaccess.NewDevice()
	session := regaccess.NewSession(dev)
	const n = 48

	stopReader := make(chan struct{})
	var readerWG sync.WaitGroup
	readerWG.Add(1)
	go func() {
		defer readerWG.Done()
		for {
			select {
			case <-stopReader:
				return
			default:
			}
			for i := 0; i < n; i++ {
				addr0 := uint16(0x1000 + 2*i)
				addr1 := addr0 + 1
				snapshot := dev.RegisterSnapshot(addr0, addr1)
				x, y := snapshot[addr0], snapshot[addr1]
				wantX := uint32(0xA0000000) | uint32(i)
				wantY := uint32(0xB0000000) | uint32(i)
				if !((x == 0 && y == 0) || (x == wantX && y == wantY)) {
					t.Errorf("torn pair %d: (%08X,%08X), want zero or (%08X,%08X)",
						i, x, y, wantX, wantY)
					return
				}
			}
		}
	}()

	var writerWG sync.WaitGroup
	for i := 0; i < n; i++ {
		writerWG.Add(1)
		go func(i int) {
			defer writerWG.Done()
			attempt, err := session.Begin(fmt.Sprintf("pair-%02d", i))
			if err != nil {
				t.Errorf("Begin %d: %v", i, err)
				return
			}
			addr0 := uint16(0x1000 + 2*i)
			addr1 := addr0 + 1
			if err := attempt.StageRegister(addr0, uint32(0xA0000000)|uint32(i)); err != nil {
				t.Errorf("stage addr0 %d: %v", i, err)
				return
			}
			if err := attempt.StageRegister(addr1, uint32(0xB0000000)|uint32(i)); err != nil {
				t.Errorf("stage addr1 %d: %v", i, err)
				return
			}
			if _, err := attempt.Commit(); err != nil {
				t.Errorf("commit %d: %v", i, err)
			}
		}(i)
	}
	writerWG.Wait()
	close(stopReader)
	readerWG.Wait()

	for i := 0; i < n; i++ {
		addr0 := uint16(0x1000 + 2*i)
		addr1 := addr0 + 1
		snapshot := dev.RegisterSnapshot(addr0, addr1)
		if got := snapshot[addr0]; got != uint32(0xA0000000)|uint32(i) {
			t.Fatalf("pair %d addr0 = %08X", i, got)
		}
		if got := snapshot[addr1]; got != uint32(0xB0000000)|uint32(i) {
			t.Fatalf("pair %d addr1 = %08X", i, got)
		}
	}
}
