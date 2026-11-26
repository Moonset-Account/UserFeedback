package dnssecpreflight_test

import (
	"bytes"
	"testing"

	dp "dnssecpreflight"
)

func TestBuildSignedData(t *testing.T) {
	sig := dp.RRSIG{
		TypeCovered: 46,
		Algorithm:   dp.AlgECDSAP256SHA256,
		Labels:      2,
		OriginalTTL: 3600,
		Expiration:  dp.Timestamp(0x11223344),
		Inception:   dp.Timestamp(0x55667788),
		KeyTag:      0x1234,
		SignerName:  "Example.",
	}
	got, err := dp.BuildSignedData(sig, []byte("RR"))
	if err != nil {
		t.Fatal(err)
	}

	want := []byte{
		0x00, 0x2e, // type covered NS? value 46 is RRSIG? Actually 46 decimal = 0x002e (RRSIG); arbitrary in test.
		13, 2,
		0x00, 0x00, 0x0e, 0x10,
		0x11, 0x22, 0x33, 0x44,
		0x55, 0x66, 0x77, 0x88,
		0x12, 0x34,
		0x07, 'e', 'x', 'a', 'm', 'p', 'l', 'e',
		0x00,
		'R', 'R',
	}
	if !bytes.Equal(got, want) {
		t.Fatalf("BuildSignedData mismatch\n got: %x\nwant: %x", got, want)
	}
}

func TestEncodeNameWireRootAndValidation(t *testing.T) {
	root, err := dp.EncodeNameWire(".")
	if err != nil || !bytes.Equal(root, []byte{0}) {
		t.Fatalf("root=%x err=%v", root, err)
	}
	if _, err := dp.EncodeNameWire("bad..example"); err == nil {
		t.Fatal("empty label should fail")
	}
}
