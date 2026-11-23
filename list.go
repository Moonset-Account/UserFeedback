package objstore

import (
	"bytes"
	"encoding/base64"
	"encoding/gob"
	"errors"
	"fmt"
	"strings"
)

// EntryKind distinguishes a plain object from a folded common prefix.
type EntryKind int

const (
	// EntryObject is a regular object key.
	EntryObject EntryKind = iota
	// EntryCommonPrefix is a key prefix folded by the listing delimiter.
	EntryCommonPrefix
)

// Entry is one row of a listing page. For EntryObject, Name is the object
// key; for EntryCommonPrefix, Name is the folded common prefix (including the
// trailing delimiter).
type Entry struct {
	Kind EntryKind
	Name string
}

// ListRequest describes a listing: only keys with Prefix are returned, keys
// are folded into common prefixes at Delimiter, and each page holds at most
// PageSize entries. Common prefixes produced by delimiter folding and plain
// objects both count toward the page size.
type ListRequest struct {
	Prefix    string
	Delimiter string
	PageSize  int
}

// Page is one page of a listing.
type Page struct {
	Entries  []Entry
	Token    string // continuation token; empty when the listing is complete
	Snapshot uint64 // store version the whole listing is pinned to
}

// ErrInvalidToken is returned by ListNext for malformed continuation tokens.
var ErrInvalidToken = errors.New("objstore: invalid continuation token")

// token is the opaque continuation state. It deliberately carries only the
// snapshot version and the key-space position — never shard identities — so
// continuation keeps reading the pinned snapshot under any new shard layout.
type token struct {
	Version   uint64
	Prefix    string
	Delimiter string
	PageSize  int
	LastName  string    // name of the last emitted entry
	LastKind  EntryKind // kind of the last emitted entry
}

// List returns the first page of a listing, pinning the snapshot version that
// every continuation page will read.
func (s *Store) List(req ListRequest) (Page, error) {
	if req.PageSize <= 0 {
		return Page{}, fmt.Errorf("objstore: PageSize must be positive, got %d", req.PageSize)
	}
	tk := token{
		Version:   s.Version(),
		Prefix:    req.Prefix,
		Delimiter: req.Delimiter,
		PageSize:  req.PageSize,
	}
	return s.page(tk, req.Prefix)
}

// ListNext returns the page following the one the continuation token came
// from. If the pinned snapshot has expired it returns ErrSnapshotExpired; it
// never silently continues from a newer version.
func (s *Store) ListNext(tok string) (Page, error) {
	tk, err := decodeToken(tok)
	if err != nil {
		return Page{}, err
	}
	var lower string
	if tk.LastKind == EntryCommonPrefix {
		// The whole subtree under the emitted common prefix was folded into
		// it; resume past every key carrying that prefix so the subtree is
		// never emitted twice.
		lower = prefixEnd(tk.LastName)
		if lower == "" {
			// The folded subtree reaches the end of the key space; the
			// listing is done. Still report snapshot expiry explicitly.
			if err := s.checkSnapshot(tk.Version); err != nil {
				return Page{}, err
			}
			return Page{Snapshot: tk.Version}, nil
		}
	} else {
		// Resume strictly after the last emitted object key.
		lower = tk.LastName + "\x00"
	}
	return s.page(tk, lower)
}

// page emits the page starting at the inclusive lower bound lower, reading
// the snapshot pinned in tk.
func (s *Store) page(tk token, lower string) (Page, error) {
	keys, err := s.snapshotKeys(tk.Version, lower, tk.Prefix)
	if err != nil {
		return Page{}, err
	}
	entries := fold(keys, tk.Prefix, tk.Delimiter)
	page := Page{Snapshot: tk.Version}
	if len(entries) <= tk.PageSize {
		page.Entries = entries
		return page, nil
	}
	page.Entries = entries[:tk.PageSize]
	last := page.Entries[len(page.Entries)-1]
	tk.LastName, tk.LastKind = last.Name, last.Kind
	page.Token = encodeToken(tk)
	return page, nil
}

// fold collapses sorted keys into listing entries. A key whose remainder
// (after prefix) contains the delimiter folds into a common prefix, and the
// whole subtree under that prefix is skipped. A key exactly equal to a
// collapsible prefix is kept as a regular object ahead of the prefix entry:
// both are preserved, ordered by name, the object first on a tie.
func fold(keys []string, prefix, delim string) []Entry {
	var out []Entry
	i := 0
	for i < len(keys) {
		k := keys[i]
		if delim != "" {
			if idx := strings.Index(k[len(prefix):], delim); idx >= 0 {
				cp := k[:len(prefix)+idx+len(delim)]
				if k == cp {
					// Key exactly equal to a collapsible prefix: keep the
					// object and the prefix separately, object first.
					out = append(out, Entry{Kind: EntryObject, Name: k})
				}
				out = append(out, Entry{Kind: EntryCommonPrefix, Name: cp})
				i++
				for i < len(keys) && strings.HasPrefix(keys[i], cp) {
					i++
				}
				continue
			}
		}
		out = append(out, Entry{Kind: EntryObject, Name: k})
		i++
	}
	return out
}

// prefixEnd returns the smallest key strictly greater than every key carrying
// prefix p, or "" if p's subtree reaches the end of the key space.
func prefixEnd(p string) string {
	b := []byte(p)
	for i := len(b) - 1; i >= 0; i-- {
		if b[i] != 0xff {
			b[i]++
			return string(b[:i+1])
		}
	}
	return ""
}

// encodeToken serializes the continuation state. Keys and delimiters are
// arbitrary byte strings, so the encoding must be byte-safe: JSON would
// silently replace invalid UTF-8 (e.g. a 0xff byte in a key) with U+FFFD and
// corrupt the resume position, so gob is used instead.
func encodeToken(tk token) string {
	var buf bytes.Buffer
	if err := gob.NewEncoder(&buf).Encode(tk); err != nil {
		panic(fmt.Sprintf("objstore: token encode failed: %v", err))
	}
	return base64.RawURLEncoding.EncodeToString(buf.Bytes())
}

func decodeToken(s string) (token, error) {
	raw, err := base64.RawURLEncoding.DecodeString(s)
	if err != nil {
		return token{}, fmt.Errorf("%w: %v", ErrInvalidToken, err)
	}
	var tk token
	if err := gob.NewDecoder(bytes.NewReader(raw)).Decode(&tk); err != nil {
		return token{}, fmt.Errorf("%w: %v", ErrInvalidToken, err)
	}
	if tk.PageSize <= 0 {
		return token{}, fmt.Errorf("%w: non-positive page size", ErrInvalidToken)
	}
	return tk, nil
}
