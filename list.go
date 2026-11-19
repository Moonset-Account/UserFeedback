package trustlist

import (
	"bufio"
	"fmt"
	"io"
	"os"
	"strings"
	"time"
)

// List is a compiled trust list: the accepted entries plus everything that was
// wrong with the source, in physical line order.
type List struct {
	entries []Entry
	issues  []Issue
	// now supplies the evaluation clock when a Request carries no Now. It is
	// unset by default so that nothing is decided against an implicit time.
	now func() time.Time
}

// SetNow fixes the list's default evaluation clock. Requests without an
// explicit Now are judged against t.
func (l *List) SetNow(t time.Time) *List {
	if l == nil {
		return nil
	}
	l.now = func() time.Time { return t }
	return l
}

// SetNowFunc installs a default evaluation clock. The function is consulted
// only when a Request leaves Now zero.
func (l *List) SetNowFunc(f func() time.Time) *List {
	if l == nil {
		return nil
	}
	l.now = f
	return l
}

// Entries returns a copy of the accepted entries.
func (l *List) Entries() []Entry {
	if l == nil {
		return nil
	}
	out := make([]Entry, len(l.entries))
	copy(out, l.entries)
	return out
}

// Issues returns a copy of the parse issues found while reading the list.
func (l *List) Issues() []Issue {
	if l == nil {
		return nil
	}
	out := make([]Issue, len(l.issues))
	copy(out, l.issues)
	return out
}

// Len returns the number of accepted entries.
func (l *List) Len() int {
	if l == nil {
		return 0
	}
	return len(l.entries)
}

// LoadFile reads and compiles a trust list from disk.
func LoadFile(path string) (*List, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, fmt.Errorf("trustlist: open %s: %w", path, err)
	}
	defer f.Close()
	return Parse(f)
}

// Parse compiles a trust list. Unreadable input is an error; individual bad
// lines are not, because dropping one line is the safe response and the caller
// can inspect Issues to report them.
func Parse(r io.Reader) (*List, error) {
	if r == nil {
		return nil, fmt.Errorf("trustlist: nil reader")
	}

	list := &List{}
	scanner := bufio.NewScanner(r)
	// Trust-list lines are short; the default 64 KiB limit is generous, but a
	// line that overruns it must fail loudly rather than be truncated.
	scanner.Buffer(make([]byte, 0, 4096), 1<<20)

	lineNo := 0
	for scanner.Scan() {
		lineNo++
		entry, issue, ok := parseLine(scanner.Text(), lineNo)
		switch {
		case ok:
			list.entries = append(list.entries, entry)
		case issue != nil:
			list.issues = append(list.issues, *issue)
		}
	}
	if err := scanner.Err(); err != nil {
		return nil, fmt.Errorf("trustlist: read: %w", err)
	}
	return list, nil
}

// parseLine turns one physical line into an Entry. It returns ok=false for
// lines that are blank, comments, or invalid; a non-nil issue is returned only
// for invalid content, so comments stay silent.
func parseLine(raw string, lineNo int) (Entry, *Issue, bool) {
	trimmed := strings.TrimSpace(raw)
	if trimmed == "" {
		return Entry{}, nil, false
	}
	if strings.HasPrefix(trimmed, "#") {
		return Entry{}, nil, false
	}

	fields := strings.Fields(trimmed)
	if len(fields) != 3 && len(fields) != 4 {
		return Entry{}, syntaxIssue(lineNo, raw, "expected \"<names> [port=<range>] <action> <key>\""), false
	}

	entry := Entry{Line: lineNo, Raw: trimmed}

	positives, negatives, err := parseNamePatterns(fields[0])
	if err != nil {
		return Entry{}, syntaxIssue(lineNo, raw, err.Error()), false
	}
	entry.Positives, entry.Negatives = positives, negatives

	rest := fields[1:]
	if strings.HasPrefix(strings.ToLower(rest[0]), "port=") {
		ports, err := ParsePortRange(rest[0][len("port="):])
		if err != nil {
			return Entry{}, syntaxIssue(lineNo, raw, err.Error()), false
		}
		entry.Ports = ports
		rest = rest[1:]
	} else {
		entry.Ports = AnyPort()
	}
	if len(rest) < 2 {
		return Entry{}, syntaxIssue(lineNo, raw, "missing action or key"), false
	}

	action, ok := ParseAction(rest[0])
	if !ok {
		return Entry{}, syntaxIssue(lineNo, raw, fmt.Sprintf("unknown action %q", rest[0])), false
	}
	entry.Action = action

	key, err := parseKeyFields(rest[1:])
	if err != nil {
		return Entry{}, syntaxIssue(lineNo, raw, err.Error()), false
	}
	entry.Key = key

	return entry, nil, true
}

// parseKeyFields rebuilds the key from the fields after the action. The
// "<type>:<blob>" form must be exactly one field: silently gluing a stray token
// onto the blob would widen a pin or miss a revocation, so trailing junk is an
// error. The "<type> <blob>" form is exactly two fields, and its first field
// must not itself look like a colon form.
func parseKeyFields(fields []string) (PublicKey, error) {
	switch len(fields) {
	case 1:
		if !strings.Contains(fields[0], ":") {
			return PublicKey{}, fmt.Errorf("key %q is not \"<type>:<blob>\"", fields[0])
		}
	case 2:
		if strings.Contains(fields[0], ":") {
			return PublicKey{}, fmt.Errorf("unexpected token %q after key %q", fields[1], fields[0])
		}
	default:
		return PublicKey{}, fmt.Errorf("key has %d fields, want 1 or 2", len(fields))
	}
	return parseKey(strings.Join(fields, " "))
}

// parseNamePatterns splits the comma-separated pattern field. A pattern that
// starts with '!' is an exclusion; everything else is positive. A line with no
// positive pattern is rejected because it could never be in scope, which would
// make its action dead weight and its intent ambiguous.
func parseNamePatterns(field string) (positives, negatives []string, err error) {
	for _, part := range strings.Split(field, ",") {
		pattern := strings.TrimSpace(part)
		if pattern == "" {
			return nil, nil, fmt.Errorf("empty name pattern in %q", field)
		}
		if strings.HasPrefix(pattern, "!") {
			exclusion := strings.TrimSpace(pattern[1:])
			if exclusion == "" {
				return nil, nil, fmt.Errorf("empty exclusion pattern in %q", field)
			}
			negatives = append(negatives, exclusion)
			continue
		}
		positives = append(positives, pattern)
	}
	if len(positives) == 0 {
		return nil, nil, fmt.Errorf("no positive name pattern in %q", field)
	}
	return positives, negatives, nil
}

// parseKey accepts "<type>:<blob>" or "<type> <blob>".
func parseKey(field string) (PublicKey, error) {
	if i := strings.IndexByte(field, ':'); i > 0 {
		return NewPublicKey(field[:i], field[i+1:]), nil
	}
	if i := strings.IndexAny(field, " \t"); i > 0 {
		return NewPublicKey(field[:i], field[i+1:]), nil
	}
	return PublicKey{}, fmt.Errorf("key %q is not \"<type>:<blob>\"", field)
}

func syntaxIssue(lineNo int, raw, msg string) *Issue {
	return &Issue{Kind: IssueSyntax, Line: lineNo, Msg: fmt.Sprintf("line %d: %s: %q", lineNo, msg, strings.TrimSpace(raw))}
}
