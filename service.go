// Package coordination implements an in-process, deterministic simulation of a
// ZooKeeper-style exclusive lock.
//
// Every contender creates an ephemeral sequential node under the same lock
// parent. The smallest sequence number holds the lock; every other contender
// arms a one-shot watch on its immediate predecessor only. Session expiry
// removes all nodes owned by the session. A separate Resource enforces fencing
// so a stale holder cannot write after a newer holder has written.
//
// The Service is deliberately step-driven: queue enumeration, predecessor watch
// installation, session expiry, and resource writes are independent calls so a
// test can interleave them and reproduce the enumeration/watch race.
package coordination

import (
	"errors"
	"fmt"
	"sort"
	"strings"
	"sync"
)

var (
	// ErrSessionExists is returned when a session id is opened twice.
	ErrSessionExists = errors.New("coordination: session already exists")
	// ErrSessionUnknown is returned for a session id that was never opened.
	ErrSessionUnknown = errors.New("coordination: unknown session")
	// ErrSessionExpired is returned when an operation uses an expired session.
	ErrSessionExpired = errors.New("coordination: session expired")
	// ErrNodeNotFound is returned when a queue node has been removed.
	ErrNodeNotFound = errors.New("coordination: node not found")
	// ErrNotNodeOwner is returned when a session tries to delete another session's node.
	ErrNotNodeOwner = errors.New("coordination: session does not own node")
	// ErrEmptySession is returned when OpenSession receives an empty id.
	ErrEmptySession = errors.New("coordination: empty session id")
)

// Node is an immutable snapshot of an ephemeral sequential queue node.
type Node struct {
	Parent    string
	Path      string
	Name      string
	Sequence  uint64
	SessionID string
	Data      string
}

type session struct {
	id      string
	expired bool
	nodes   map[string]struct{}
	watches map[uint64]struct{}
}

type watcher struct {
	id        uint64
	path      string
	sessionID string
	fn        func(WatchEvent)
}

// WatchEvent is delivered once to a one-shot watcher when its node is removed.
type WatchEvent struct {
	WatcherID uint64
	NodePath  string
	SessionID string // session that armed the watch
}

// WatchStatus reports whether a one-shot watch was installed.
type WatchStatus int

const (
	// WatchInstalled means the watched node existed and the watch is armed.
	WatchInstalled WatchStatus = iota
	// WatchNodeMissing means the watched node was already gone; nothing was armed.
	WatchNodeMissing
)

// Service is the in-memory coordination service. All methods are safe for
// concurrent use. Watcher callbacks run after the service lock is released, so
// a callback may safely call back into the Service.
type Service struct {
	mu          sync.Mutex
	seqs        map[string]uint64
	sessions    map[string]*session
	nodes       map[string]*Node
	watchers    map[string]map[uint64]*watcher
	watchesByID map[uint64]*watcher
	nextWatchID uint64
}

// NewService creates an empty coordination service.
func NewService() *Service {
	return &Service{
		seqs:        make(map[string]uint64),
		sessions:    make(map[string]*session),
		nodes:       make(map[string]*Node),
		watchers:    make(map[string]map[uint64]*watcher),
		watchesByID: make(map[uint64]*watcher),
	}
}

// OpenSession registers a client session. Sessions own ephemeral nodes and
// watches until they expire.
func (s *Service) OpenSession(id string) error {
	if id == "" {
		return ErrEmptySession
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.sessions[id]; ok {
		return fmt.Errorf("%w: %s", ErrSessionExists, id)
	}
	s.sessions[id] = &session{
		id:      id,
		nodes:   make(map[string]struct{}),
		watches: make(map[uint64]struct{}),
	}
	return nil
}

// SessionExists reports whether the session is open and not expired.
func (s *Service) SessionExists(id string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	sess, ok := s.sessions[id]
	return ok && !sess.expired
}

// checkSession reports whether the session is still alive.
func (s *Service) checkSession(id string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	sess, ok := s.sessions[id]
	if !ok {
		return fmt.Errorf("%w: %s", ErrSessionUnknown, id)
	}
	if sess.expired {
		return fmt.Errorf("%w: %s", ErrSessionExpired, id)
	}
	return nil
}

// SessionNodePaths returns the sorted paths of all nodes still owned by a
// session. Unknown sessions return nil.
func (s *Service) SessionNodePaths(id string) []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	sess, ok := s.sessions[id]
	if !ok {
		return nil
	}
	paths := make([]string, 0, len(sess.nodes))
	for p := range sess.nodes {
		paths = append(paths, p)
	}
	sort.Strings(paths)
	return paths
}

// CreateEphemeralSequential creates a new ephemeral node under parent. The
// sequence counter is per parent, so each lock queue starts at 1 and strictly
// increases for that lock.
func (s *Service) CreateEphemeralSequential(parent, sessionID, data string) (Node, error) {
	parent = normalizeParent(parent)
	s.mu.Lock()
	defer s.mu.Unlock()

	sess, ok := s.sessions[sessionID]
	if !ok {
		return Node{}, fmt.Errorf("%w: %s", ErrSessionUnknown, sessionID)
	}
	if sess.expired {
		return Node{}, fmt.Errorf("%w: %s", ErrSessionExpired, sessionID)
	}

	s.seqs[parent]++
	n := &Node{
		Parent:    parent,
		Name:      fmt.Sprintf("node-%010d", s.seqs[parent]),
		Sequence:  s.seqs[parent],
		SessionID: sessionID,
		Data:      data,
	}
	n.Path = childPrefix(parent) + n.Name
	s.nodes[n.Path] = n
	sess.nodes[n.Path] = struct{}{}
	return *n, nil
}

// Children returns a snapshot of the queue for parent, ordered by sequence
// number. Sequence order matches lexicographic order of the zero-padded names.
func (s *Service) Children(parent string) []Node {
	parent = normalizeParent(parent)
	s.mu.Lock()
	defer s.mu.Unlock()

	children := s.childrenLocked(parent)
	out := make([]Node, 0, len(children))
	for _, n := range children {
		out = append(out, *n)
	}
	return out
}

// GetNode returns a snapshot of the node at path.
func (s *Service) GetNode(path string) (Node, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	n, ok := s.nodes[path]
	if !ok {
		return Node{}, false
	}
	return *n, true
}

// WatcherCount reports how many one-shot watchers are armed on path.
func (s *Service) WatcherCount(path string) int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.watchers[path])
}

// SessionWatcherCount reports how many watches a session still has armed.
func (s *Service) SessionWatcherCount(sessionID string) int {
	s.mu.Lock()
	defer s.mu.Unlock()
	sess, ok := s.sessions[sessionID]
	if !ok {
		return 0
	}
	return len(sess.watches)
}

// WatchPredecessor atomically verifies that selfPath still exists, is owned by
// sessionID, and that predPath still exists, then arms a one-shot watch on
// predPath. If the predecessor disappeared between enumeration and this call,
// WatchNodeMissing is returned and nothing is armed; the caller must re-enumerate
// (and possibly acquire directly).
func (s *Service) WatchPredecessor(selfPath, predPath, sessionID string, fn func(WatchEvent)) (uint64, WatchStatus, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	sess, ok := s.sessions[sessionID]
	if !ok {
		return 0, WatchNodeMissing, fmt.Errorf("%w: %s", ErrSessionUnknown, sessionID)
	}
	if sess.expired {
		return 0, WatchNodeMissing, fmt.Errorf("%w: %s", ErrSessionExpired, sessionID)
	}
	self, ok := s.nodes[selfPath]
	if !ok || self.SessionID != sessionID {
		return 0, WatchNodeMissing, fmt.Errorf("%w: %s", ErrNodeNotFound, selfPath)
	}
	if _, ok := s.nodes[predPath]; !ok {
		return 0, WatchNodeMissing, nil
	}
	return s.addWatcherLocked(predPath, sessionID, fn), WatchInstalled, nil
}

// WatchNode arms a one-shot watch on an arbitrary node. It exists mainly for
// callers and tests that manage their own predecessor selection.
func (s *Service) WatchNode(path, sessionID string, fn func(WatchEvent)) (uint64, WatchStatus, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	sess, ok := s.sessions[sessionID]
	if !ok {
		return 0, WatchNodeMissing, fmt.Errorf("%w: %s", ErrSessionUnknown, sessionID)
	}
	if sess.expired {
		return 0, WatchNodeMissing, fmt.Errorf("%w: %s", ErrSessionExpired, sessionID)
	}
	if _, ok := s.nodes[path]; !ok {
		return 0, WatchNodeMissing, nil
	}
	return s.addWatcherLocked(path, sessionID, fn), WatchInstalled, nil
}

// Unwatch removes an armed watch. Removing a watch that already fired or was
// removed is a no-op.
func (s *Service) Unwatch(id uint64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if w, ok := s.watchesByID[id]; ok {
		s.removeWatcherLocked(w)
	}
}

// TryAcquire atomically reports whether selfPath is still the smallest node in
// its queue. It is the final check a contender performs before declaring itself
// the holder.
func (s *Service) TryAcquire(selfPath, sessionID string) (bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	sess, ok := s.sessions[sessionID]
	if !ok {
		return false, fmt.Errorf("%w: %s", ErrSessionUnknown, sessionID)
	}
	if sess.expired {
		return false, fmt.Errorf("%w: %s", ErrSessionExpired, sessionID)
	}
	self, ok := s.nodes[selfPath]
	if !ok || self.SessionID != sessionID {
		return false, fmt.Errorf("%w: %s", ErrNodeNotFound, selfPath)
	}
	children := s.childrenLocked(self.Parent)
	if len(children) == 0 || children[0].Path != selfPath {
		return false, nil
	}
	return true, nil
}

// DeleteNode removes a node owned by sessionID and fires every one-shot watcher
// armed on that node. Watchers armed on other nodes are not touched.
func (s *Service) DeleteNode(path, sessionID string) error {
	s.mu.Lock()

	sess, ok := s.sessions[sessionID]
	if !ok {
		s.mu.Unlock()
		return fmt.Errorf("%w: %s", ErrSessionUnknown, sessionID)
	}
	if sess.expired {
		s.mu.Unlock()
		return fmt.Errorf("%w: %s", ErrSessionExpired, sessionID)
	}
	n, ok := s.nodes[path]
	if !ok {
		s.mu.Unlock()
		return fmt.Errorf("%w: %s", ErrNodeNotFound, path)
	}
	if n.SessionID != sessionID {
		s.mu.Unlock()
		return fmt.Errorf("%w: %s", ErrNotNodeOwner, path)
	}

	delete(s.nodes, path)
	delete(sess.nodes, path)
	deliveries := deliveriesFor(s.takeWatchersLocked(path))
	s.mu.Unlock()

	deliver(deliveries)
	return nil
}

// ExpireSession marks the session expired and removes every node and every watch
// it owns in one atomic step. Successors that were actually watching a removed
// node are notified exactly once; every other waiter stays asleep. The sorted
// removed node paths are returned.
func (s *Service) ExpireSession(sessionID string) ([]string, error) {
	s.mu.Lock()

	sess, ok := s.sessions[sessionID]
	if !ok {
		s.mu.Unlock()
		return nil, fmt.Errorf("%w: %s", ErrSessionUnknown, sessionID)
	}
	if sess.expired {
		s.mu.Unlock()
		return nil, fmt.Errorf("%w: %s", ErrSessionExpired, sessionID)
	}
	sess.expired = true

	// Watches die with the session that owns them.
	for id := range sess.watches {
		if w, ok := s.watchesByID[id]; ok {
			s.removeWatcherLocked(w)
		}
	}

	paths := make([]string, 0, len(sess.nodes))
	for p := range sess.nodes {
		paths = append(paths, p)
	}
	sort.Strings(paths)

	var deliveries []watchDelivery
	for _, p := range paths {
		delete(s.nodes, p)
		delete(sess.nodes, p)
		deliveries = append(deliveries, deliveriesFor(s.takeWatchersLocked(p))...)
	}
	s.mu.Unlock()

	deliver(deliveries)
	return paths, nil
}

func (s *Service) addWatcherLocked(path, sessionID string, fn func(WatchEvent)) uint64 {
	if fn == nil {
		fn = func(WatchEvent) {}
	}
	s.nextWatchID++
	w := &watcher{id: s.nextWatchID, path: path, sessionID: sessionID, fn: fn}
	if s.watchers[path] == nil {
		s.watchers[path] = make(map[uint64]*watcher)
	}
	s.watchers[path][w.id] = w
	s.watchesByID[w.id] = w
	s.sessions[sessionID].watches[w.id] = struct{}{}
	return w.id
}

// childrenLocked returns the queue under parent ordered by sequence. The caller
// must hold s.mu.
func (s *Service) childrenLocked(parent string) []*Node {
	out := make([]*Node, 0)
	for _, n := range s.nodes {
		if n.Parent == parent {
			out = append(out, n)
		}
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Sequence != out[j].Sequence {
			return out[i].Sequence < out[j].Sequence
		}
		return out[i].Path < out[j].Path
	})
	return out
}

func (s *Service) takeWatchersLocked(path string) []*watcher {
	set := s.watchers[path]
	if len(set) == 0 {
		return nil
	}
	list := make([]*watcher, 0, len(set))
	for _, w := range set {
		list = append(list, w)
	}
	sort.Slice(list, func(i, j int) bool { return list[i].id < list[j].id })
	for _, w := range list {
		s.removeWatcherLocked(w)
	}
	return list
}

func (s *Service) removeWatcherLocked(w *watcher) {
	if set := s.watchers[w.path]; set != nil {
		delete(set, w.id)
		if len(set) == 0 {
			delete(s.watchers, w.path)
		}
	}
	delete(s.watchesByID, w.id)
	if sess := s.sessions[w.sessionID]; sess != nil {
		delete(sess.watches, w.id)
	}
}

type watchDelivery struct {
	fn func(WatchEvent)
	ev WatchEvent
}

func deliveriesFor(list []*watcher) []watchDelivery {
	out := make([]watchDelivery, 0, len(list))
	for _, w := range list {
		out = append(out, watchDelivery{
			fn: w.fn,
			ev: WatchEvent{WatcherID: w.id, NodePath: w.path, SessionID: w.sessionID},
		})
	}
	return out
}

func deliver(list []watchDelivery) {
	for _, d := range list {
		d.fn(d.ev)
	}
}

func normalizeParent(parent string) string {
	if parent == "" || parent == "/" {
		return "/"
	}
	return strings.TrimRight(parent, "/")
}

func childPrefix(parent string) string {
	if parent == "/" {
		return "/"
	}
	return parent + "/"
}
