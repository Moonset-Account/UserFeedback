package authstack

import "strconv"

// Control decides how the result of one stack item affects its layer.
//
// The model mirrors the classic PAM control vocabulary:
//
//	Required    a failure is remembered and evaluation continues
//	Requisite   a failure ends this layer immediately with failure
//	Sufficient  a success ends this layer immediately with success, but
//	            only while the layer has not remembered a failure
//	Optional    neither result is remembered; a success still counts
type Control int

const (
	Required Control = iota
	Requisite
	Sufficient
	Optional
)

// String returns the canonical name of the control flag.
func (c Control) String() string {
	switch c {
	case Required:
		return "required"
	case Requisite:
		return "requisite"
	case Sufficient:
		return "sufficient"
	case Optional:
		return "optional"
	default:
		return "Control(" + strconv.Itoa(int(c)) + ")"
	}
}

func (c Control) valid() bool {
	return c >= Required && c <= Optional
}
