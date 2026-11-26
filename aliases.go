package barrier

// BarrierManager is the domain-named alias for Manager.
type BarrierManager = Manager

// Coordinator is an alternate name for callers modeling the coordination
// service that owns barriers.
type Coordinator = Manager

// NewCoordinator creates a barrier Coordinator.
func NewCoordinator(opts ...Option) *Coordinator { return NewManager(opts...) }

// Handoff aliases cover the common one-word spelling.
type HandoffRequest = HandOverRequest
type HandoffRecord = HandOverRecord

// Handoff is the one-word spelling of HandOver.
func (m *Manager) Handoff(req HandoffRequest) (*HandoffRecord, error) {
	return m.HandOver(req)
}

// HandOffRequest and HandOffRecord cover the phrasal spelling.
type HandOffRequest = HandOverRequest
type HandOffRecord = HandOverRecord

// HandOff is the phrasal-spelling alias for HandOver.
func (m *Manager) HandOff(req HandOffRequest) (*HandOffRecord, error) {
	return m.HandOver(req)
}
