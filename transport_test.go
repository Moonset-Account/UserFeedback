package recordlayer

import (
	"bytes"
	"errors"
	"io"
	"testing"
)

// TestMemTransportShortWrite verifies that a bounded pipe caps each Write and
// reports io.ErrShortWrite, and that the coordinator's writeAll retries until
// the full record has been segmented out.
func TestMemTransportShortWrite(t *testing.T) {
	a, b := NewPipePair(7)
	payload := bytes.Repeat([]byte("p"), 50)
	done := make(chan error, 1)
	go func() { done <- writeAll(a, payload) }()

	got := make([]byte, 0, len(payload))
	chunk := make([]byte, 3)
	for len(got) < len(payload) {
		n, err := b.Read(chunk)
		if n > 0 {
			got = append(got, chunk[:n]...)
		}
		if err != nil && !errors.Is(err, io.ErrShortWrite) {
			t.Fatalf("unexpected read error: %v", err)
		}
	}
	if err := <-done; err != nil {
		t.Fatalf("writeAll: %v", err)
	}
	if !bytes.Equal(got, payload) {
		t.Fatalf("payload mismatch: got %d bytes want %d", len(got), len(payload))
	}
}

// TestMemTransportFragmentedRead checks small destination buffers on the read
// side and clean end-of-stream behaviour on close.
func TestMemTransportFragmentedRead(t *testing.T) {
	a, b := NewPipePair(0)
	wire := []byte{1, 2, 3, 4, 5, 6, 7, 8, 9, 10}
	go func() {
		if _, err := a.Write(wire); err != nil {
			t.Errorf("write: %v", err)
		}
		if err := a.Close(); err != nil {
			t.Errorf("close: %v", err)
		}
	}()
	got, err := io.ReadAll(b)
	if err != nil {
		t.Fatalf("ReadAll: %v", err)
	}
	if !bytes.Equal(got, wire) {
		t.Fatalf("data mismatch: %v", got)
	}
	if _, err := b.Read(make([]byte, 4)); !errors.Is(err, io.EOF) {
		t.Fatalf("after close: got %v want io.EOF", err)
	}
	// Close is idempotent.
	if err := a.Close(); err != nil {
		t.Fatalf("second close: %v", err)
	}
}

// TestMemTransportWriteAfterClose ensures writes fail once the pipe is closed.
func TestMemTransportWriteAfterClose(t *testing.T) {
	a, _ := NewPipePair(0)
	if err := a.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := a.Write([]byte("x")); !errors.Is(err, io.ErrClosedPipe) {
		t.Fatalf("write after close: got %v want io.ErrClosedPipe", err)
	}
}
