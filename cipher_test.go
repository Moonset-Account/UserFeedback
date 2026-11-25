package recordlayer

import (
	"bytes"
	"testing"
)

func TestDemoCipherRoundTrip(t *testing.T) {
	c, err := NewDemoCipher([]byte("base-secret"))
	if err != nil {
		t.Fatal(err)
	}
	cases := []struct {
		gen, seq uint64
		typ      RecordType
		msg      []byte
	}{
		{0, 0, TypeApplication, nil},
		{0, 1, TypeRotateRequest, []byte("")},
		{1, 0, TypeRotateResponse, []byte("first after rotation")},
		{2, 7, TypeApplication, bytes.Repeat([]byte("q"), 100)},
	}
	for i, tc := range cases {
		sealed, err := c.Seal(tc.gen, tc.seq, tc.typ, tc.msg)
		if err != nil {
			t.Fatalf("case %d seal: %v", i, err)
		}
		if len(sealed) != len(tc.msg)+demoTagLen {
			t.Fatalf("case %d sealed length: got %d want %d", i, len(sealed), len(tc.msg)+demoTagLen)
		}
		got, err := c.Open(tc.gen, tc.seq, tc.typ, sealed)
		if err != nil {
			t.Fatalf("case %d open: %v", i, err)
		}
		if !bytes.Equal(got, tc.msg) {
			t.Fatalf("case %d round trip mismatch", i)
		}
	}
}

func TestDemoCipherTamperDetection(t *testing.T) {
	c, _ := NewDemoCipher([]byte("base-secret"))
	sealed, err := c.Seal(1, 3, TypeApplication, []byte("authenticated"))
	if err != nil {
		t.Fatal(err)
	}

	t.Run("payload bit flip", func(t *testing.T) {
		bad := append([]byte(nil), sealed...)
		bad[0] ^= 0x80
		if _, err := c.Open(1, 3, TypeApplication, bad); err == nil {
			t.Fatal("tampered payload accepted")
		}
	})
	t.Run("tag bit flip", func(t *testing.T) {
		bad := append([]byte(nil), sealed...)
		bad[len(bad)-1] ^= 0x01
		if _, err := c.Open(1, 3, TypeApplication, bad); err == nil {
			t.Fatal("tampered tag accepted")
		}
	})
	t.Run("wrong sequence", func(t *testing.T) {
		if _, err := c.Open(1, 4, TypeApplication, sealed); err == nil {
			t.Fatal("record accepted under wrong sequence")
		}
	})
	t.Run("wrong generation", func(t *testing.T) {
		if _, err := c.Open(2, 3, TypeApplication, sealed); err == nil {
			t.Fatal("record accepted under wrong generation")
		}
	})
	t.Run("wrong record type", func(t *testing.T) {
		if _, err := c.Open(1, 3, TypeRotateRequest, sealed); err == nil {
			t.Fatal("record accepted under wrong type")
		}
	})
	t.Run("truncated", func(t *testing.T) {
		if _, err := c.Open(1, 3, TypeApplication, sealed[:demoTagLen-1]); err == nil {
			t.Fatal("truncated record accepted")
		}
	})
}

func TestDemoCipherDistinctGenerations(t *testing.T) {
	c, _ := NewDemoCipher([]byte("base-secret"))
	g0, err := c.Seal(0, 0, TypeApplication, []byte("same plaintext"))
	if err != nil {
		t.Fatal(err)
	}
	g1, err := c.Seal(1, 0, TypeApplication, []byte("same plaintext"))
	if err != nil {
		t.Fatal(err)
	}
	if bytes.Equal(g0, g1) {
		t.Fatal("sealed payloads of different generations are identical")
	}
	if _, err := c.Open(1, 0, TypeApplication, g0); err == nil {
		t.Fatal("gen-0 record opened under gen-1 key")
	}
}

func TestNewDemoCipherRejectsEmptyKey(t *testing.T) {
	if _, err := NewDemoCipher(nil); err == nil {
		t.Fatal("empty base key accepted")
	}
}
