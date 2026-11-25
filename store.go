package pinstate

import (
	"bytes"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"sync"
)

// DualPages is the fixed number of redundant pages used by the state machine.
const DualPages = 2

const (
	pageA = 0
	pageB = 1

	pageMagic     = "pinstate.dual-page.v1"
	formatVersion = 1
)

// PageReadWriter is the persistence boundary. A production adapter can back
// these two page IDs with flash, EEPROM, a secure element, or a file.
//
// WritePage must replace a complete page atomically at the stated write
// boundary. A power loss inside the electrical write may leave a torn page;
// the checksummed envelope detects and ignores such a page.
type PageReadWriter interface {
	ReadPage(pageID int) ([]byte, error)
	WritePage(pageID int, data []byte) error
}

// PageStore is an alias for the persistence interface.
type PageStore = PageReadWriter

var (
	ErrInvalidPageID      = errors.New("pinstate: invalid page id")
	ErrBlankPage          = errors.New("pinstate: blank page")
	ErrCorruptPage        = errors.New("pinstate: corrupt page")
	ErrNoRecoverableState = errors.New("pinstate: no valid persistent state")
	ErrAmbiguousPages     = errors.New("pinstate: ambiguous equally-generational pages")
	ErrUnsupportedFormat  = errors.New("pinstate: unsupported page format")
)

// MemoryPageStore is a local, concurrency-safe page implementation. It is
// intended for unit tests and synthetic verification.
type MemoryPageStore struct {
	mu    sync.RWMutex
	pages [DualPages][]byte
}

func NewMemoryPageStore() *MemoryPageStore {
	return &MemoryPageStore{}
}

func (s *MemoryPageStore) ReadPage(pageID int) ([]byte, error) {
	if pageID < 0 || pageID >= DualPages {
		return nil, fmt.Errorf("%w: %d", ErrInvalidPageID, pageID)
	}

	s.mu.RLock()
	defer s.mu.RUnlock()
	return append([]byte(nil), s.pages[pageID]...), nil
}

func (s *MemoryPageStore) WritePage(pageID int, data []byte) error {
	if pageID < 0 || pageID >= DualPages {
		return fmt.Errorf("%w: %d", ErrInvalidPageID, pageID)
	}

	s.mu.Lock()
	defer s.mu.Unlock()
	s.pages[pageID] = append([]byte(nil), data...)
	return nil
}

type pageEnvelope struct {
	Magic         string          `json:"magic"`
	FormatVersion int             `json:"format_version"`
	PageID        int             `json:"page_id"`
	Generation    uint64          `json:"generation"`
	State         json.RawMessage `json:"state"`
	Checksum      string          `json:"checksum"`
}

func writeSized(w io.Writer, b []byte) {
	var length [8]byte
	binary.BigEndian.PutUint64(length[:], uint64(len(b)))
	_, _ = w.Write(length[:])
	_, _ = w.Write(b)
}

func envelopeChecksum(e pageEnvelope) string {
	h := sha256.New()
	writeSized(h, []byte(e.Magic))
	writeSized(h, []byte(fmt.Sprintf("%d", e.FormatVersion)))
	writeSized(h, []byte(fmt.Sprintf("%d", e.PageID)))
	writeSized(h, []byte(fmt.Sprintf("%d", e.Generation)))
	writeSized(h, e.State)

	var sum [sha256.Size]byte
	copy(sum[:], h.Sum(nil))
	return hex.EncodeToString(sum[:])
}

func encodePage(state *storedState, pageID int, generation uint64) ([]byte, error) {
	payload, err := json.Marshal(state)
	if err != nil {
		return nil, fmt.Errorf("pinstate: encode state: %w", err)
	}

	envelope := pageEnvelope{
		Magic:         pageMagic,
		FormatVersion: formatVersion,
		PageID:        pageID,
		Generation:    generation,
		State:         payload,
	}
	envelope.Checksum = envelopeChecksum(envelope)

	data, err := json.Marshal(envelope)
	if err != nil {
		return nil, fmt.Errorf("pinstate: encode page: %w", err)
	}
	return data, nil
}

func decodePage(raw []byte, expectedPageID int) (pageEnvelope, *storedState, error) {
	if len(bytes.TrimSpace(raw)) == 0 {
		return pageEnvelope{}, nil, ErrBlankPage
	}

	var envelope pageEnvelope
	if err := json.Unmarshal(raw, &envelope); err != nil {
		return pageEnvelope{}, nil, fmt.Errorf("%w: invalid JSON: %v", ErrCorruptPage, err)
	}

	if envelope.Magic != pageMagic {
		return pageEnvelope{}, nil, fmt.Errorf("%w: bad magic", ErrCorruptPage)
	}
	if envelope.FormatVersion != formatVersion {
		return pageEnvelope{}, nil, fmt.Errorf("%w: %d", ErrUnsupportedFormat, envelope.FormatVersion)
	}
	if envelope.PageID != expectedPageID {
		return pageEnvelope{}, nil, fmt.Errorf("%w: page id %d stored in page %d", ErrCorruptPage, envelope.PageID, expectedPageID)
	}
	if len(envelope.State) == 0 {
		return pageEnvelope{}, nil, fmt.Errorf("%w: empty state", ErrCorruptPage)
	}

	wantChecksum, err := hex.DecodeString(envelope.Checksum)
	if err != nil {
		return pageEnvelope{}, nil, fmt.Errorf("%w: checksum encoding: %v", ErrCorruptPage, err)
	}
	gotChecksum, err := hex.DecodeString(envelopeChecksum(envelope))
	if err != nil {
		return pageEnvelope{}, nil, fmt.Errorf("%w: internal checksum encoding: %v", ErrCorruptPage, err)
	}
	if subtle.ConstantTimeCompare(wantChecksum, gotChecksum) != 1 {
		return pageEnvelope{}, nil, fmt.Errorf("%w: sha256 mismatch", ErrCorruptPage)
	}

	var state storedState
	if err := json.Unmarshal(envelope.State, &state); err != nil {
		return pageEnvelope{}, nil, fmt.Errorf("%w: invalid state JSON: %v", ErrCorruptPage, err)
	}
	if err := state.validate(); err != nil {
		return pageEnvelope{}, nil, fmt.Errorf("%w: %v", ErrCorruptPage, err)
	}

	return envelope, &state, nil
}
