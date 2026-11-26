package dnssecpreflight

import (
	"encoding/binary"
	"errors"
	"strings"
)

// EncodeNameWire encodes a conventional DNS name as canonical, lowercase
// uncompressed wire format. A trailing dot is optional; "@" is not supported.
func EncodeNameWire(name string) ([]byte, error) {
	if name == "" || name == "." {
		return []byte{0}, nil
	}
	name = strings.TrimSuffix(name, ".")
	labels := strings.Split(name, ".")
	out := make([]byte, 0, len(name)+len(labels)+1)
	total := 1 // root zero byte
	for _, label := range labels {
		if label == "" {
			return nil, errors.New("dnssecpreflight: empty DNS label")
		}
		if len(label) > 63 {
			return nil, errors.New("dnssecpreflight: DNS label longer than 63 bytes")
		}
		total += 1 + len(label)
		if total > 255 {
			return nil, errors.New("dnssecpreflight: DNS name longer than 255 bytes")
		}
		out = append(out, byte(len(label)))
		out = append(out, []byte(strings.ToLower(label))...)
	}
	out = append(out, 0)
	return out, nil
}

// BuildSignedData builds the RFC 4034 signed bytes: RRSIG RDATA except the
// Signature field (including canonical signer name), followed by the canonical
// RRset bytes.
func BuildSignedData(sig RRSIG, canonicalRRSet []byte) ([]byte, error) {
	signer := sig.SignerNameWire
	if signer == nil {
		var err error
		signer, err = EncodeNameWire(sig.SignerName)
		if err != nil {
			return nil, err
		}
	}

	out := make([]byte, 0, 18+len(signer)+len(canonicalRRSet))
	fixed := make([]byte, 18)
	binary.BigEndian.PutUint16(fixed[0:2], sig.TypeCovered)
	fixed[2] = byte(sig.Algorithm)
	fixed[3] = sig.Labels
	binary.BigEndian.PutUint32(fixed[4:8], sig.OriginalTTL)
	binary.BigEndian.PutUint32(fixed[8:12], uint32(sig.Expiration))
	binary.BigEndian.PutUint32(fixed[12:16], uint32(sig.Inception))
	binary.BigEndian.PutUint16(fixed[16:18], uint16(sig.KeyTag))
	out = append(out, fixed...)
	out = append(out, signer...)
	out = append(out, canonicalRRSet...)
	return out, nil
}
