package causalcodec

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"sync"
)

// Context is the immutable causal fragment represented by an anchor.
type Context struct {
	Causes   []Cause   `json:"causes,omitempty"`
	Segments []Segment `json:"segments,omitempty"`
}

// Anchor pairs an exact reference with the context previously exchanged.
type Anchor struct {
	Ref     AnchorRef `json:"ref"`
	Context Context   `json:"context"`
}

// AnchorResolver is implemented by AnchorStore and test/local providers.
type AnchorResolver interface {
	ResolveAnchor(ref AnchorRef) (*Anchor, error)
}

// AnchorStore is a concurrency-safe, exact-identity anchor registry. Names may
// be duplicated; lookup never falls back from version or digest.
type AnchorStore struct {
	mu      sync.RWMutex
	anchors map[string]*Anchor
}

func NewAnchorStore() *AnchorStore {
	return &AnchorStore{anchors: make(map[string]*Anchor)}
}

// DigestContext returns the stable fingerprint used in AnchorRef.Digest.
func DigestContext(ctx Context) (string, error) {
	b, err := json.Marshal(ctx)
	if err != nil {
		return "", fmt.Errorf("canonicalize anchor context: %w", err)
	}
	sum := sha256.Sum256(b)
	return "sha256:" + hex.EncodeToString(sum[:]), nil
}

// ValidateContext ensures an anchor is self-contained. Anchors represent
// already exchanged complete context and therefore must not contain nested
// anchor references or unresolved gap markers.
func ValidateContext(ctx Context) error {
	byID := make(map[string]Cause, len(ctx.Causes))
	for i, c := range ctx.Causes {
		if c.ID == "" {
			return fmt.Errorf("%w: context.causes[%d].id is required", ErrInvalidAnchorRef, i)
		}
		if old, ok := byID[c.ID]; ok && !causesEqual(old, c) {
			return fmt.Errorf("%w: conflicting context cause %q", ErrInvalidAnchorRef, c.ID)
		}
		byID[c.ID] = c
	}
	for i, s := range ctx.Segments {
		if s.Anchor != nil {
			return fmt.Errorf("%w: nested anchor in context segment %d is not supported", ErrInvalidAnchorRef, i)
		}
		if s.Gap != nil {
			return fmt.Errorf("%w: unresolved gap in context segment %d is not allowed", ErrInvalidAnchorRef, i)
		}
		if len(s.CauseIDs) == 0 {
			return fmt.Errorf("%w: context segment %d has no cause ids", ErrInvalidAnchorRef, i)
		}
		for _, id := range s.CauseIDs {
			if _, ok := byID[id]; !ok {
				return fmt.Errorf("%w: context segment %d references unknown cause %q", ErrInvalidAnchorRef, i, id)
			}
		}
	}
	return nil
}

// NewAnchor creates an exact reference for name/version and context.
func NewAnchor(name, version string, ctx Context) (*Anchor, error) {
	if name == "" || version == "" {
		return nil, fmt.Errorf("%w: anchor name and version are required", ErrInvalidAnchorRef)
	}
	if err := ValidateContext(ctx); err != nil {
		return nil, err
	}
	digest, err := DigestContext(ctx)
	if err != nil {
		return nil, err
	}
	return &Anchor{
		Ref: AnchorRef{Name: name, Version: version, Digest: digest},
		Context: Context{
			Causes:   append([]Cause(nil), ctx.Causes...),
			Segments: append([]Segment(nil), ctx.Segments...),
		},
	}, nil
}

// Put registers an anchor. If Ref.Digest is empty it is computed; otherwise
// the supplied digest is checked against Context.
func (s *AnchorStore) Put(a *Anchor) error {
	if a == nil {
		return fmt.Errorf("%w: nil anchor", ErrInvalidAnchorRef)
	}
	if a.Ref.Name == "" || a.Ref.Version == "" {
		return fmt.Errorf("%w: anchor name and version are required", ErrInvalidAnchorRef)
	}
	ctx := Context{
		Causes:   append([]Cause(nil), a.Context.Causes...),
		Segments: append([]Segment(nil), a.Context.Segments...),
	}
	if err := ValidateContext(ctx); err != nil {
		return err
	}
	digest, err := DigestContext(ctx)
	if err != nil {
		return err
	}
	if a.Ref.Digest == "" {
		a.Ref.Digest = digest
	} else if a.Ref.Digest != digest {
		return fmt.Errorf("%w: supplied %s, computed %s", ErrAnchorDigestMismatch, a.Ref.Digest, digest)
	}
	cp := &Anchor{Ref: a.Ref, Context: ctx}

	s.mu.Lock()
	defer s.mu.Unlock()
	s.anchors[a.Ref.Key()] = cp
	return nil
}

// MustPut is convenient for fixed synthetic fixtures.
func (s *AnchorStore) MustPut(a *Anchor) *AnchorStore {
	if err := s.Put(a); err != nil {
		panic(err)
	}
	return s
}

// PutContext builds and registers an anchor.
func (s *AnchorStore) PutContext(name, version string, ctx Context) (*Anchor, error) {
	a, err := NewAnchor(name, version, ctx)
	if err != nil {
		return nil, err
	}
	if err := s.Put(a); err != nil {
		return nil, err
	}
	return a, nil
}

// ResolveAnchor returns only the exact name/version/digest match. A same-name
// anchor with another version or fingerprint is returned in the error but is
// never substituted.
func (s *AnchorStore) ResolveAnchor(ref AnchorRef) (*Anchor, error) {
	if err := validateAnchorRef(ref); err != nil {
		return nil, err
	}
	if s == nil {
		return nil, &MissingAnchorError{Ref: ref}
	}

	s.mu.RLock()
	defer s.mu.RUnlock()

	if a, ok := s.anchors[ref.Key()]; ok {
		return cloneAnchor(a), nil
	}

	miss := &MissingAnchorError{Ref: ref}
	sameNameVersion := false
	for _, a := range s.anchors {
		if a.Ref.Name == ref.Name {
			miss.AvailableSameName = append(miss.AvailableSameName, a.Ref)
			if a.Ref.Version == ref.Version {
				sameNameVersion = true
			}
		}
	}
	miss.AvailableSameName = uniqueRefs(miss.AvailableSameName)
	miss.DigestMismatch = sameNameVersion
	return nil, miss
}

// HasExact reports whether the exact reference is present.
func (s *AnchorStore) HasExact(ref AnchorRef) bool {
	if s == nil {
		return false
	}
	s.mu.RLock()
	defer s.mu.RUnlock()
	_, ok := s.anchors[ref.Key()]
	return ok
}

func cloneAnchor(a *Anchor) *Anchor {
	return &Anchor{
		Ref: a.Ref,
		Context: Context{
			Causes:   append([]Cause(nil), a.Context.Causes...),
			Segments: append([]Segment(nil), a.Context.Segments...),
		},
	}
}

func uniqueRefs(refs []AnchorRef) []AnchorRef {
	seen := make(map[string]struct{}, len(refs))
	out := refs[:0:0]
	for _, r := range refs {
		if _, ok := seen[r.Key()]; ok {
			continue
		}
		seen[r.Key()] = struct{}{}
		out = append(out, r)
	}
	return out
}
