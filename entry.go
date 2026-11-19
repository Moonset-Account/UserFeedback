package trustlist

import (
	"strings"
	"time"
)

// Action is the verb of a trust-list line.
type Action int

const (
	// ActionNone is the zero value and is never produced by a valid line.
	ActionNone Action = iota
	// ActionPin fixes one host key for the matching name and port.
	ActionPin
	// ActionCertAuthority makes one issuer key trusted to sign host
	// certificates for the matching name and port.
	ActionCertAuthority
	// ActionRevoke withdraws trust from one key outright.
	ActionRevoke
)

var actionNames = map[Action]string{
	ActionNone:          "none",
	ActionPin:           "pin",
	ActionCertAuthority: "cert-authority",
	ActionRevoke:        "revoke",
}

// String returns the trust-list spelling of the action.
func (a Action) String() string {
	if s, ok := actionNames[a]; ok {
		return s
	}
	return "invalid"
}

// ParseAction maps a trust-list verb to an Action. Matching is
// case-insensitive, but every synonym maps to exactly one action.
func ParseAction(token string) (Action, bool) {
	switch normalizeWord(token) {
	case "pin":
		return ActionPin, true
	case "cert-authority", "cert-authority-trust", "ca":
		return ActionCertAuthority, true
	case "revoke":
		return ActionRevoke, true
	default:
		return ActionNone, false
	}
}

// Entry is one parsed, valid trust-list line. Invalid lines never become an
// Entry; they are reported as Issues so a single bad line cannot silently
// change the list's meaning.
type Entry struct {
	// Line is the 1-based physical line number in the source list.
	Line int
	// Positives are the name patterns that bring the line into scope.
	Positives []string
	// Negatives are the exclusion patterns; any hit drops the whole line.
	Negatives []string
	// Ports scopes the line to a port range.
	Ports PortRange
	// Action is the line's verb.
	Action Action
	// Key is the pinned key, trusted issuer, or revoked key.
	Key PublicKey
	// Raw is the trimmed source line, kept for diagnostics.
	Raw string
}

// inScope reports whether the entry's name patterns and port range both admit
// the connection, before exclusions are considered.
func (e Entry) inScope(host string, port int) bool {
	return matchNameAny(e.Positives, host) && e.Ports.Contains(port)
}

// excluded reports whether any exclusion pattern matches host. A matching
// exclusion drops the whole line, whatever its action.
func (e Entry) excluded(host string) bool {
	return matchNameAny(e.Negatives, host)
}

// appliesTo reports whether the entry is in scope, not excluded, and therefore
// able to influence the decision for this connection.
func (e Entry) appliesTo(host string, port int) bool {
	return e.inScope(host, port) && !e.excluded(host)
}

// coversKey reports whether the entry's key names the presented key. For a
// certificate the subject key and the issuer key are both tested: revoking a
// subject kills that certificate, and revoking an issuer kills every
// certificate it signed. Callers pass nil for a plain host key.
func (e Entry) coversKey(presented PublicKey, cert *HostCertificate) bool {
	if keyEqual(e.Key, presented) {
		return true
	}
	if cert != nil && keyEqual(e.Key, cert.Issuer) {
		return true
	}
	return false
}

// IssueKind classifies a problem found while reading or evaluating the list.
type IssueKind string

const (
	// IssueSyntax marks a line that could not be parsed. It is skipped.
	IssueSyntax IssueKind = "syntax"
	// IssueInvalidTime marks an evaluation time that was not supplied.
	IssueInvalidTime IssueKind = "invalid-time"
	// IssueInvalidRequest marks a request that could not be judged safely.
	IssueInvalidRequest IssueKind = "invalid-request"
)

// normalizeWord lower-cases and trims a trust-list keyword.
func normalizeWord(s string) string {
	return strings.ToLower(strings.TrimSpace(s))
}

// Issue records a line that was skipped or a condition that prevented a sound
// decision.
type Issue struct {
	Kind IssueKind
	Line int
	Msg  string
}

// decisionTime uses the caller's clock, or the configured default clock when
// the caller supplied none and a default clock exists.
func decisionTime(now time.Time, fallback func() time.Time) (time.Time, bool) {
	if !now.IsZero() {
		return now, true
	}
	if fallback != nil {
		if t := fallback(); !t.IsZero() {
			return t, true
		}
	}
	return time.Time{}, false
}
