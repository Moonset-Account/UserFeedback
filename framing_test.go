package pppframe_test

import (
	"bytes"
	"errors"
	"io"
	"math/rand"
	"testing"

	ppp "pppframe"
)

func aggregate(t *testing.T, r *ppp.Receiver, chunks ...[]byte) ppp.Report {
	t.Helper()
	var total ppp.Report
	for _, chunk := range chunks {
		rep := r.Feed(chunk)
		total.Frames = append(total.Frames, rep.Frames...)
		total.Drops = append(total.Drops, rep.Drops...)
		total.EmptyDelimiters += rep.EmptyDelimiters
		total.DiscardedControls = append(total.DiscardedControls, rep.DiscardedControls...)
		total.Resyncs += rep.Resyncs
	}
	return total
}

func feedByteByByte(r *ppp.Receiver, wire []byte) ppp.Report {
	var total ppp.Report
	for _, b := range wire {
		rep := r.Feed([]byte{b})
		total.Frames = append(total.Frames, rep.Frames...)
		total.Drops = append(total.Drops, rep.Drops...)
		total.EmptyDelimiters += rep.EmptyDelimiters
		total.DiscardedControls = append(total.DiscardedControls, rep.DiscardedControls...)
		total.Resyncs += rep.Resyncs
	}
	return total
}

func TestFCS16KnownVectorAndIndependentImplementation(t *testing.T) {
	got := ppp.FCS16([]byte("123456789"))
	if got != 0x906e {
		t.Fatalf("PPP FCS check value = 0x%04x, want 0x906e", got)
	}

	rng := rand.New(rand.NewSource(17234))
	for i := 0; i < 200; i++ {
		p := make([]byte, rng.Intn(97))
		_, _ = rng.Read(p)

		want := bitwisePPPFCS(p)
		if got := ppp.FCS16(p); got != want {
			t.Fatalf("FCS16(% x) = 0x%04x, independent = 0x%04x", p, got, want)
		}

		fcs := ppp.FCS16(p)
		withFCS := append(append([]byte(nil), p...), byte(fcs), byte(fcs>>8))
		if !ppp.VerifyFCS(withFCS) {
			t.Fatalf("VerifyFCS rejected valid frame % x", withFCS)
		}
	}
}

func bitwisePPPFCS(p []byte) uint16 {
	fcs := uint16(0xffff)
	for _, b := range p {
		fcs ^= uint16(b)
		for i := 0; i < 8; i++ {
			if fcs&1 != 0 {
				fcs = (fcs >> 1) ^ 0x8408
			} else {
				fcs >>= 1
			}
		}
	}
	return fcs ^ 0xffff
}

func TestEncodeEscapesMappedAndFramingBytes(t *testing.T) {
	m := ppp.MapFor(0x00, 0x01, 0x1f)
	payload := []byte{0x00, 0x01, 'A', ppp.Flag, ppp.Escape, 0x1f, 'Z'}
	wire := ppp.EncodeFrame(payload, m)

	wantSubsequences := [][]byte{
		{ppp.Escape, 0x20}, // 0x00
		{ppp.Escape, 0x21}, // 0x01
		{ppp.Escape, 0x5e}, // Flag
		{ppp.Escape, 0x5d}, // Escape
		{ppp.Escape, 0x3f}, // 0x1f
	}
	for _, want := range wantSubsequences {
		if !bytes.Contains(wire, want) {
			t.Fatalf("encoded wire % x does not contain escaped sequence % x", wire, want)
		}
	}
	if wire[0] != ppp.Flag || wire[len(wire)-1] != ppp.Flag {
		t.Fatalf("frame must start/end with flag: % x", wire)
	}

	rep := ppp.NewReceiver(m).Feed(wire)
	if len(rep.Frames) != 1 || len(rep.Drops) != 0 {
		t.Fatalf("decode report = %+v, want one frame and no drops", rep)
	}
	if !bytes.Equal(rep.Frames[0], payload) {
		t.Fatalf("payload = % x, want % x", rep.Frames[0], payload)
	}
}

func TestAllByteValuesRoundTripOneByteAtATime(t *testing.T) {
	payload := make([]byte, 256)
	for i := range payload {
		payload[i] = byte(i)
	}
	maps := []ppp.ControlMap{ppp.ControlMapNone, ppp.ControlMapAll, 0xdeadbeef}

	for _, m := range maps {
		wire := ppp.EncodeFrame(payload, m)
		r := ppp.NewReceiver(m)
		rep := feedByteByByte(r, wire)

		if len(rep.Frames) != 1 {
			t.Fatalf("map %08x: got %d frames (%+v), want 1", m, len(rep.Frames), rep)
		}
		if len(rep.Drops) != 0 {
			t.Fatalf("map %08x: unexpected drops %+v", m, rep.Drops)
		}
		if !bytes.Equal(rep.Frames[0], payload) {
			t.Fatalf("map %08x: payload mismatch", m)
		}
	}
}

func TestEverySplitKeepsEscapeSequenceAcrossReads(t *testing.T) {
	payload := []byte{ppp.Escape, ppp.Flag, 0x01, 0x02, 'h', 'a', 'l', 'f'}
	m := ppp.MapFor(0x01, 0x02)
	wire := ppp.EncodeFrame(payload, m)

	if !bytes.Contains(wire, []byte{ppp.Escape}) {
		t.Fatalf("test frame has no escape prefix: % x", wire)
	}

	for cut := 1; cut < len(wire); cut++ {
		r := ppp.NewReceiver(m)
		first := r.Feed(wire[:cut])
		if len(first.Frames) != 0 || len(first.Drops) != 0 {
			t.Fatalf("cut %d: partial feed completed frame/drop: %+v", cut, first)
		}

		second := r.Feed(wire[cut:])
		if len(second.Frames) != 1 || len(second.Drops) != 0 {
			t.Fatalf("cut %d: report = %+v, want one frame/no drops", cut, second)
		}
		if !bytes.Equal(second.Frames[0], payload) {
			t.Fatalf("cut %d: payload = % x", cut, second.Frames[0])
		}
	}
}

func TestReceiveMapDiscardsBareByteButAcceptsEscapedValue(t *testing.T) {
	m := ppp.MapFor(0x01)
	payload := []byte{0x01, 'x'}

	// Agreeing sender escapes 0x01 as 7d 21; receiver must recover 0x01.
	escapedWire := ppp.EncodeFrame(payload, m)
	rep := ppp.NewReceiver(m).Feed(escapedWire)
	if len(rep.Frames) != 1 || len(rep.Drops) != 0 {
		t.Fatalf("escaped report = %+v", rep)
	}
	if rep.Frames[0][0] != 0x01 {
		t.Fatalf("escaped 0x01 decoded as % x", rep.Frames[0])
	}

	// A sender using no map leaves 0x01 bare. The current receiver map says
	// that byte is a control byte, so it is omitted and FCS must fail.
	bareWire := ppp.EncodeFrame(payload, ppp.ControlMapNone)
	r := ppp.NewReceiver(m)
	bad := r.Feed(bareWire)
	if len(bad.Frames) != 0 || len(bad.Drops) != 1 {
		t.Fatalf("bare report = %+v, want one FCS drop", bad)
	}
	if bad.Drops[0].Reason != ppp.DropFCS {
		t.Fatalf("drop reason = %s, want %s", bad.Drops[0].Reason, ppp.DropFCS)
	}
	if !bytes.Equal(bad.DiscardedControls, []byte{0x01}) {
		t.Fatalf("discarded = % x, want 01", bad.DiscardedControls)
	}
	if r.Stats().ControlsDiscarded != 1 || r.Stats().DropCount(ppp.DropFCS) != 1 {
		t.Fatalf("stats = %+v", r.Stats())
	}

	// Strict policy can reject at the offending byte and resync at next flag.
	strict := ppp.NewReceiver(m, ppp.WithBareControlPolicy(ppp.BareControlRejectFrame))
	strictRep := strict.Feed(bareWire)
	if len(strictRep.Drops) != 1 || strictRep.Drops[0].Reason != ppp.DropBareControl {
		t.Fatalf("strict report = %+v", strictRep)
	}
	if strictRep.Resyncs != 1 {
		t.Fatalf("strict report resyncs = %d, want recovery at closing flag", strictRep.Resyncs)
	}
	okPayload := []byte("ok")
	after := strict.Feed(ppp.EncodeFrame(okPayload, ppp.ControlMapNone))
	if len(after.Frames) != 1 || !bytes.Equal(after.Frames[0], okPayload) {
		t.Fatalf("frame after strict reject = %+v", after)
	}
}

func TestFCSCorruptionShortAndInvalidEscapeReasons(t *testing.T) {
	wire := ppp.EncodeFrame([]byte("hello"), ppp.ControlMapNone)
	idx := bytes.IndexByte(wire, 'h')
	if idx < 0 {
		t.Fatal("h not found")
	}
	wire[idx] = 'H'

	r := ppp.NewReceiver(ppp.ControlMapNone)
	rep := r.Feed(wire)
	if len(rep.Drops) != 1 || rep.Drops[0].Reason != ppp.DropFCS {
		t.Fatalf("corrupt report = %+v", rep)
	}

	short := ppp.NewReceiver(ppp.ControlMapNone).Feed([]byte{ppp.Flag, 0x01, ppp.Flag})
	if len(short.Drops) != 1 || short.Drops[0].Reason != ppp.DropShortFrame {
		t.Fatalf("short report = %+v", short)
	}

	badEsc := ppp.NewReceiver(ppp.ControlMapNone).Feed([]byte{ppp.Flag, ppp.Escape, ppp.Flag})
	if len(badEsc.Drops) != 1 || badEsc.Drops[0].Reason != ppp.DropInvalidEscape {
		t.Fatalf("invalid escape report = %+v", badEsc)
	}
}

func TestResetBeforeFrameEndResyncsAtNextFlag(t *testing.T) {
	m := ppp.MapFor(0x07)
	goodPayload := []byte("after-reset\x07")
	good := ppp.EncodeFrame(goodPayload, m)

	r := ppp.NewReceiver(m)
	partialCut := bytes.Index(good, []byte("after")) + 3
	if partialCut <= 0 || partialCut >= len(good) {
		partialCut = len(good) / 2
	}
	r.Feed(good[:partialCut])

	d, ok := r.Reset()
	if !ok || d.Reason != ppp.DropReset {
		t.Fatalf("Reset = (%+v,%v), want reset drop", d, ok)
	}

	garbage := []byte{0x01, 0x02, 0x03, ppp.Escape, 0x99}
	rep := r.Feed(append(garbage, good...))
	if len(rep.Frames) != 1 {
		t.Fatalf("report frames = %+v, want resynchronized frame", rep)
	}
	if len(rep.Drops) != 0 {
		t.Fatalf("unexpected feed drops: %+v", rep.Drops)
	}
	if !bytes.Equal(rep.Frames[0], goodPayload) {
		t.Fatalf("payload = %q, want %q", rep.Frames[0], goodPayload)
	}
	if rep.Resyncs != 1 || r.Stats().Resyncs != 1 {
		t.Fatalf("resync count report=%d stats=%d", rep.Resyncs, r.Stats().Resyncs)
	}
	if r.Stats().DropCount(ppp.DropReset) != 1 {
		t.Fatalf("reset drop not counted: %+v", r.Stats())
	}
}

func TestConsecutiveEmptyDelimitersAreIgnored(t *testing.T) {
	payload := []byte("one")
	m := ppp.ControlMapNone
	var wire []byte
	wire = append(wire, ppp.Flag, ppp.Flag, ppp.Flag)
	wire = ppp.AppendFrame(wire, payload, m)
	wire = append(wire, ppp.Flag, ppp.Flag, ppp.Flag)

	r := ppp.NewReceiver(m)
	rep := feedByteByByte(r, wire)
	if len(rep.Frames) != 1 || !bytes.Equal(rep.Frames[0], payload) {
		t.Fatalf("frames = %+v, want %q", rep.Frames, payload)
	}
	if len(rep.Drops) != 0 {
		t.Fatalf("drops = %+v", rep.Drops)
	}
	if rep.EmptyDelimiters != 6 {
		t.Fatalf("empty delimiters = %d, want 6", rep.EmptyDelimiters)
	}
}

func TestOversizedFrameDropsAndResyncs(t *testing.T) {
	r := ppp.NewReceiver(ppp.ControlMapNone, ppp.WithMaxPayload(2))
	big := ppp.EncodeFrame([]byte("abc"), ppp.ControlMapNone)
	rep := r.Feed(big)
	if len(rep.Drops) != 1 || rep.Drops[0].Reason != ppp.DropOversized {
		t.Fatalf("oversized report = %+v", rep)
	}

	okPayload := []byte("ok")
	after := r.Feed(ppp.EncodeFrame(okPayload, ppp.ControlMapNone))
	if len(after.Frames) != 1 || !bytes.Equal(after.Frames[0], okPayload) {
		t.Fatalf("after oversized = %+v", after)
	}
}

func TestReceiveMapConfirmationAppliesOnlyAtFrameBoundary(t *testing.T) {
	oldMap := ppp.ControlMapNone
	newMap := ppp.MapFor(0x05)
	payload := []byte{'B', 0x05, 'A'}
	wire := ppp.EncodeFrame(payload, oldMap)

	cut := bytes.IndexByte(wire, 'B') + 1
	r := ppp.NewReceiver(oldMap)
	r.Feed(wire[:cut])

	if applied := r.UpdateReceiveMap(newMap); applied {
		t.Fatal("mid-frame map update reported immediate application")
	}
	if got := r.ReceiveMap(); got != oldMap {
		t.Fatalf("active map changed mid-frame to %v", got)
	}
	pending, ok := r.PendingReceiveMap()
	if !ok || pending != newMap {
		t.Fatalf("pending = (%v,%v), want %v", pending, ok, newMap)
	}

	rep := r.Feed(wire[cut:])
	if len(rep.Frames) != 1 || len(rep.Drops) != 0 {
		t.Fatalf("old-map frame report = %+v", rep)
	}
	if !bytes.Equal(rep.Frames[0], payload) {
		t.Fatalf("payload = % x", rep.Frames[0])
	}
	if got := r.ReceiveMap(); got != newMap {
		t.Fatalf("map after boundary = %v, want %v", got, newMap)
	}

	agreed := ppp.EncodeFrame([]byte{0x05, 'C'}, newMap)
	okRep := r.Feed(agreed)
	if len(okRep.Frames) != 1 || len(okRep.Drops) != 0 {
		t.Fatalf("agreed new-map frame = %+v", okRep)
	}

	mismatched := ppp.EncodeFrame([]byte{0x05, 'D'}, oldMap)
	badRep := r.Feed(mismatched)
	if len(badRep.Drops) != 1 || badRep.Drops[0].Reason != ppp.DropFCS {
		t.Fatalf("mismatched map report = %+v", badRep)
	}
}

func TestAdapterDirectionalMapsAndHalfFramePeerExchange(t *testing.T) {
	var aToB, bToA bytes.Buffer
	initial := ppp.Maps{Send: ppp.MapFor(0x11), Receive: ppp.MapFor(0x13)}
	a := ppp.NewAdapter(&aToB, ppp.WithMaps(initial))
	b := ppp.NewAdapter(&bToA, ppp.WithMaps(ppp.Maps{Send: ppp.MapFor(0x13), Receive: ppp.MapFor(0x11)}))

	payload := []byte{0x11, 'h', 'a', 'l', 'f', ppp.Flag}
	n, err := a.SendFrame(payload)
	if err != nil {
		t.Fatal(err)
	}
	if n != aToB.Len() {
		t.Fatalf("SendFrame wrote %d, buffer has %d", n, aToB.Len())
	}

	wire := aToB.Bytes()
	cut := len(wire) / 2
	first := b.FeedSerial(wire[:cut])
	if len(first.Frames) != 0 {
		t.Fatalf("half frame unexpectedly produced %d frames", len(first.Frames))
	}
	second := b.FeedSerial(wire[cut:])
	if len(second.Frames) != 1 || len(second.Drops) != 0 {
		t.Fatalf("peer decode = %+v", second)
	}
	if !bytes.Equal(second.Frames[0], payload) {
		t.Fatalf("peer payload = % x", second.Frames[0])
	}

	reply := []byte("ack\x13")
	if _, err := b.Write(reply); err != nil {
		t.Fatal(err)
	}
	replyWire := bToA.Bytes()
	replyCut := bytes.IndexByte(replyWire, 'a') + 2
	rep1 := a.FeedSerial(replyWire[:replyCut])
	rep2 := a.FeedSerial(replyWire[replyCut:])
	rep := rep1
	rep.Frames = append(rep.Frames, rep2.Frames...)
	rep.Drops = append(rep.Drops, rep2.Drops...)
	if len(rep.Frames) != 1 || !bytes.Equal(rep.Frames[0], reply) {
		t.Fatalf("A reply decode = %+v", rep)
	}

	// Negotiate only send direction. Receive map must remain unchanged.
	newSend := ppp.MapFor(0x7f)
	applied, err := a.ConfirmDirectionMap(ppp.DirectionSend, newSend)
	if err != nil || !applied {
		t.Fatalf("send confirmation = (%v,%v)", applied, err)
	}
	maps := a.ConfirmedMaps()
	if maps.Send != newSend || maps.Receive != initial.Receive {
		t.Fatalf("directional update changed wrong map: %+v", maps)
	}
	if _, err := a.ConfirmDirectionMap(ppp.Direction(0), 0); !errors.Is(err, ppp.ErrInvalidDirection) {
		t.Fatalf("invalid direction err = %v", err)
	}
}

func TestIntegratedHalfFramesMapNegotiationAndEmptyDelimiters(t *testing.T) {
	oldMap := ppp.MapFor(0x10)
	newMap := ppp.MapFor(0x15)
	r := ppp.NewReceiver(oldMap)

	// Leading run: first flag hunts, second completes one empty delimiter.
	lead := r.Feed([]byte{ppp.Flag, ppp.Flag})
	if lead.EmptyDelimiters != 1 || len(lead.Frames) != 0 || len(lead.Drops) != 0 {
		t.Fatalf("leading flags report = %+v", lead)
	}

	firstPayload := []byte{0x10, 'o', 'n', 'e'}
	firstWire := ppp.EncodeFrame(firstPayload, oldMap)
	cut := len(firstWire) / 2
	if partial := r.Feed(firstWire[:cut]); len(partial.Frames) != 0 || len(partial.Drops) != 0 {
		t.Fatalf("first half produced events: %+v", partial)
	}

	// Confirmation arrives while the first frame is only half written/read.
	if immediate := r.UpdateReceiveMap(newMap); immediate {
		t.Fatal("new map became active before the frame boundary")
	}

	firstDone := r.Feed(firstWire[cut:])
	if len(firstDone.Frames) != 1 || len(firstDone.Drops) != 0 {
		t.Fatalf("first completed frame = %+v", firstDone)
	}
	if !bytes.Equal(firstDone.Frames[0], firstPayload) || r.ReceiveMap() != newMap {
		t.Fatalf("payload/map = %x/%v", firstDone.Frames, r.ReceiveMap())
	}

	// Two back-to-back flags after the closing flag are two empty frames.
	empties := r.Feed([]byte{ppp.Flag, ppp.Flag})
	if empties.EmptyDelimiters != 2 || len(empties.Frames) != 0 || len(empties.Drops) != 0 {
		t.Fatalf("empty sequence = %+v", empties)
	}

	// A corrupted frame is rejected for FCS and does not disturb resync.
	badWire := ppp.EncodeFrame([]byte("badframe"), newMap)
	idx := bytes.IndexByte(badWire, 'b')
	if idx < 0 {
		t.Fatal("payload byte not found")
	}
	badWire[idx] = 'B'
	bad := r.Feed(badWire)
	if len(bad.Drops) != 1 || bad.Drops[0].Reason != ppp.DropFCS {
		t.Fatalf("bad frame report = %+v", bad)
	}

	// The peer then sends a new-map frame in two halves plus trailing flags.
	secondPayload := []byte{0x15, 't', 'w', 'o'}
	secondWire := ppp.EncodeFrame(secondPayload, newMap)
	secondCut := bytes.Index(secondWire, []byte{'t', 'w'})
	if secondCut < 0 {
		secondCut = len(secondWire) / 2
	}
	if partial := r.Feed(secondWire[:secondCut]); len(partial.Frames) != 0 {
		t.Fatalf("second first half produced frame: %+v", partial)
	}
	secondDone := r.Feed(append(secondWire[secondCut:], ppp.Flag, ppp.Flag))
	if len(secondDone.Frames) != 1 || !bytes.Equal(secondDone.Frames[0], secondPayload) {
		t.Fatalf("second frame = %+v", secondDone)
	}
	if secondDone.EmptyDelimiters != 2 {
		t.Fatalf("trailing empties = %d, want 2", secondDone.EmptyDelimiters)
	}
	if len(secondDone.Drops) != 0 {
		t.Fatalf("unexpected drops after recovery: %+v", secondDone.Drops)
	}
}

func TestEmptyPayloadFrameIsDistinctFromEmptyDelimiter(t *testing.T) {
	for _, m := range []ppp.ControlMap{ppp.ControlMapNone, ppp.ControlMapAll} {
		wire := ppp.EncodeFrame(nil, m)
		r := ppp.NewReceiver(m)
		rep := r.Feed(wire)
		if len(rep.Frames) != 1 || len(rep.Drops) != 0 {
			t.Fatalf("map %x empty payload report = %+v", m, rep)
		}
		if rep.Frames[0] == nil || len(rep.Frames[0]) != 0 {
			t.Fatalf("map %x frame = %#v, want non-nil empty payload", m, rep.Frames[0])
		}

		flags := r.Feed([]byte{ppp.Flag, ppp.Flag})
		if len(flags.Frames) != 0 || flags.EmptyDelimiters != 2 {
			t.Fatalf("bare flags report = %+v", flags)
		}
	}
}

func TestFrameWriterSnapshotsSendMapUntilBoundary(t *testing.T) {
	var wire bytes.Buffer
	oldMap := ppp.MapFor(0x04)
	newMap := ppp.ControlMapNone
	a := ppp.NewAdapter(&wire, ppp.WithSendMap(oldMap))

	fw := a.BeginFrame()
	if _, err := fw.Write([]byte{0x04}); err != nil {
		t.Fatal(err)
	}
	if applied, err := a.ConfirmDirectionMap(ppp.DirectionSend, newMap); err != nil || !applied {
		t.Fatalf("send map confirmation = (%v,%v)", applied, err)
	}
	if _, err := fw.Write([]byte("x")); err != nil {
		t.Fatal(err)
	}
	if _, err := fw.Finish(); err != nil {
		t.Fatal(err)
	}

	peer := ppp.NewReceiver(oldMap)
	rep := peer.Feed(wire.Bytes())
	want := []byte{0x04, 'x'}
	if len(rep.Frames) != 1 || !bytes.Equal(rep.Frames[0], want) {
		t.Fatalf("snapshot-map frame = %+v, want %x", rep, want)
	}
	if err := fw.Close(); !errors.Is(err, ppp.ErrFrameClosed) {
		t.Fatalf("second close err = %v, want ErrFrameClosed", err)
	}

	wire.Reset()
	if _, err := a.SendFrame([]byte{0x04, 'y'}); err != nil {
		t.Fatal(err)
	}
	bad := peer.Feed(wire.Bytes())
	if len(bad.Drops) != 1 || bad.Drops[0].Reason != ppp.DropFCS {
		t.Fatalf("next frame should use new send map: %+v", bad)
	}
}

func TestAdapterNilOutputAndShortWriter(t *testing.T) {
	a := ppp.NewAdapter(nil)
	if _, err := a.SendFrame([]byte("x")); !errors.Is(err, ppp.ErrNoOutput) {
		t.Fatalf("nil output err = %v", err)
	}

	sw := &shortWriter{first: 1}
	bad := ppp.NewAdapter(sw)
	if _, err := bad.SendFrame([]byte("abcdef")); !errors.Is(err, io.ErrShortWrite) {
		t.Fatalf("short writer err = %v", err)
	}
}

type shortWriter struct {
	first int
	calls int
}

func (w *shortWriter) Write(p []byte) (int, error) {
	w.calls++
	if w.calls == 1 {
		return w.first, nil
	}
	return 0, nil
}
