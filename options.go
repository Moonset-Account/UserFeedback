package pppframe

// DefaultMaxPayloadSize bounds the unstuffed payload delivered upstairs.
// Two additional FCS bytes are allowed before the receiver declares a
// frame oversized. The limit prevents an unbounded allocation from a peer
// that never sends a closing flag.
const DefaultMaxPayloadSize = 1 << 20

// BareControlPolicy selects handling of an unescaped byte whose bit is set
// in the active receive control map.
type BareControlPolicy uint8

const (
	// BareControlDiscardByte implements the stated behavior: the bare
	// mapped control byte is omitted from frame data. With agreeing maps
	// this never occurs; with mismatched maps the later FCS check usually
	// drops the frame as DropFCS.
	BareControlDiscardByte BareControlPolicy = iota

	// BareControlRejectFrame immediately drops the current frame with
	// DropBareControl and waits for the next flag before resynchronizing.
	BareControlRejectFrame

	// BareControlAcceptByte accepts the byte despite the map. This is
	// provided for interoperability testing, not the default.
	BareControlAcceptByte
)

type settings struct {
	maps       Maps
	maxPayload int
	bare       BareControlPolicy
}

func defaultSettings() settings {
	return settings{
		maps: Maps{
			Send:    DefaultControlMap,
			Receive: DefaultControlMap,
		},
		maxPayload: DefaultMaxPayloadSize,
		bare:       BareControlDiscardByte,
	}
}

// Option configures an Adapter or Receiver.
type Option func(*settings)

// WithMaps sets both confirmed directional maps.
func WithMaps(m Maps) Option {
	return func(s *settings) { s.maps = m }
}

// WithSendMap sets the confirmed send control map.
func WithSendMap(m ControlMap) Option {
	return func(s *settings) { s.maps.Send = m }
}

// WithReceiveMap sets the confirmed receive control map.
func WithReceiveMap(m ControlMap) Option {
	return func(s *settings) { s.maps.Receive = m }
}

// WithMaxPayload limits delivered payload size. Values <= 0 select
// unlimited payload size.
func WithMaxPayload(n int) Option {
	return func(s *settings) {
		if n <= 0 {
			s.maxPayload = -1
		} else {
			s.maxPayload = n
		}
	}
}

// WithBareControlPolicy changes unescaped mapped-control handling.
func WithBareControlPolicy(p BareControlPolicy) Option {
	return func(s *settings) { s.bare = p }
}
