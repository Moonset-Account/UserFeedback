package pppframe

// PPP FCS-16 uses reflected polynomial x^16+x^12+x^5+1 (0x8408),
// initial value 0xffff and final complementing.
const (
	fcsInit uint16 = 0xffff
	fcsGood uint16 = 0xf0b8
)

var fcsTable = func() [256]uint16 {
	var table [256]uint16
	for b := 0; b < 256; b++ {
		v := uint16(b)
		for i := 0; i < 8; i++ {
			if v&1 != 0 {
				v = (v >> 1) ^ 0x8408
			} else {
				v >>= 1
			}
		}
		table[b] = v
	}
	return table
}()

func fcsUpdate(fcs uint16, p []byte) uint16 {
	for _, b := range p {
		fcs = (fcs >> 8) ^ fcsTable[(fcs^uint16(b))&0xff]
	}
	return fcs
}

func fcsUpdateByte(fcs uint16, b byte) uint16 {
	return (fcs >> 8) ^ fcsTable[(fcs^uint16(b))&0xff]
}

// FCS16 returns the transmitted PPP FCS value: the complemented CRC
// register. Its low byte is transmitted first.
func FCS16(p []byte) uint16 {
	return fcsUpdate(fcsInit, p) ^ 0xffff
}

// VerifyFCS checks unstuffed bytes consisting of payload followed by the
// two little-endian FCS bytes. Processing them from the PPP initial state
// yields the fixed good residual 0xf0b8 exactly when the FCS is valid.
func VerifyFCS(payloadAndFCS []byte) bool {
	return fcsUpdate(fcsInit, payloadAndFCS) == fcsGood
}
