package exceptall

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sync"
)

// FileSegmentManager creates JSON-lines temporary segments in a directory.
// Every released segment is closed and removed.
type FileSegmentManager struct {
	mu       sync.Mutex
	dir      string
	created  int
	released int
}

// NewFileSegmentManager creates dir if necessary and returns a file-backed
// SegmentManager.
func NewFileSegmentManager(dir string) (*FileSegmentManager, error) {
	if dir == "" {
		dir = ".exceptall-segments"
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, fmt.Errorf("exceptall: create segment directory %q: %w", dir, err)
	}
	abs, err := filepath.Abs(dir)
	if err != nil {
		return nil, fmt.Errorf("exceptall: resolve segment directory %q: %w", dir, err)
	}
	return &FileSegmentManager{dir: abs}, nil
}

// Dir returns the segment directory.
func (m *FileSegmentManager) Dir() string { return m.dir }

// CreateSegment implements SegmentManager.
func (m *FileSegmentManager) CreateSegment(ctx context.Context) (Segment, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}

	f, err := os.CreateTemp(m.dir, "exceptall-seg-*.jsonl")
	if err != nil {
		return nil, fmt.Errorf("exceptall: create temporary segment: %w", err)
	}
	m.mu.Lock()
	m.created++
	id := fmt.Sprintf("file-segment-%d", m.created)
	m.mu.Unlock()

	return &FileSegment{
		id:      id,
		path:    f.Name(),
		file:    f,
		encoder: json.NewEncoder(f),
		manager: m,
	}, nil
}

// CreatedSegments implements SegmentStats.
func (m *FileSegmentManager) CreatedSegments() int {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.created
}

// ReleasedSegments implements SegmentStats.
func (m *FileSegmentManager) ReleasedSegments() int {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.released
}

// ActiveSegments implements SegmentStats.
func (m *FileSegmentManager) ActiveSegments() int {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.created - m.released
}

// RemoveAll removes the manager directory. Call it after Run has returned;
// Run itself releases every segment but does not remove the parent directory.
func (m *FileSegmentManager) RemoveAll() error {
	return os.RemoveAll(m.dir)
}

func (m *FileSegmentManager) markReleased() {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.released++
}

// FileSegment stores one JSON object per line.
type FileSegment struct {
	mu       sync.Mutex
	id       string
	path     string
	file     *os.File
	encoder  *json.Encoder
	manager  *FileSegmentManager
	released bool
}

// ID implements Segment.
func (s *FileSegment) ID() string { return s.id }

// Path returns the underlying file path.
func (s *FileSegment) Path() string { return s.path }

// Append implements Segment.
func (s *FileSegment) Append(ctx context.Context, record Record) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.released {
		return fmt.Errorf("exceptall: append to released segment %s", s.id)
	}
	if s.file == nil {
		return fmt.Errorf("exceptall: append after reader opened for segment %s", s.id)
	}
	if err := s.encoder.Encode(record); err != nil {
		return fmt.Errorf("exceptall: write segment %s: %w", s.id, err)
	}
	return nil
}

// OpenReader implements Segment.
func (s *FileSegment) OpenReader(ctx context.Context) (SegmentReader, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	s.mu.Lock()
	if s.released {
		s.mu.Unlock()
		return nil, fmt.Errorf("exceptall: open released segment %s", s.id)
	}
	if s.file != nil {
		if err := s.file.Close(); err != nil {
			s.mu.Unlock()
			return nil, fmt.Errorf("exceptall: close writer for segment %s: %w", s.id, err)
		}
		s.file = nil
		s.encoder = nil
	}
	s.mu.Unlock()

	f, err := os.Open(s.path)
	if err != nil {
		return nil, fmt.Errorf("exceptall: open segment %s: %w", s.id, err)
	}
	return &fileSegmentReader{file: f, decoder: json.NewDecoder(f)}, nil
}

// Release implements Segment.
func (s *FileSegment) Release() error {
	s.mu.Lock()
	if s.released {
		s.mu.Unlock()
		return nil
	}
	s.released = true
	var closeErr error
	if s.file != nil {
		closeErr = s.file.Close()
		s.file = nil
		s.encoder = nil
	}
	path := s.path
	s.mu.Unlock()

	removeErr := os.Remove(path)
	if s.manager != nil {
		s.manager.markReleased()
	}
	if closeErr != nil {
		return fmt.Errorf("exceptall: close released segment %s: %w", s.id, closeErr)
	}
	if removeErr != nil && !os.IsNotExist(removeErr) {
		return fmt.Errorf("exceptall: remove segment %s: %w", s.id, removeErr)
	}
	return nil
}

type fileSegmentReader struct {
	file    *os.File
	decoder *json.Decoder
	closed  bool
}

// Next implements SegmentReader.
func (r *fileSegmentReader) Next(ctx context.Context) (Record, bool, error) {
	if err := ctx.Err(); err != nil {
		return Record{}, false, err
	}
	if r.closed {
		return Record{}, false, fmt.Errorf("exceptall: read from closed file reader")
	}

	var rec Record
	if err := r.decoder.Decode(&rec); err != nil {
		if errors.Is(err, io.EOF) {
			return Record{}, false, nil
		}
		return Record{}, false, fmt.Errorf("exceptall: decode segment record: %w", err)
	}
	return rec, true, nil
}

// Close implements SegmentReader.
func (r *fileSegmentReader) Close() error {
	if r.closed {
		return nil
	}
	r.closed = true
	if r.file == nil {
		return nil
	}
	err := r.file.Close()
	r.file = nil
	return err
}
