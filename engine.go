package tlsresume

import (
	"fmt"
	"time"
)

// Outcome is the action the edge entry must take for one connection.
type Outcome int

const (
	// OutcomeFullHandshake discards the ticket and authenticates from scratch.
	OutcomeFullHandshake Outcome = iota
	// OutcomeWaitForHandshake resumes the session but holds the request until
	// the resumption handshake completes.
	OutcomeWaitForHandshake
	// OutcomeAcceptEarlyData processes the request immediately as 0-RTT data.
	OutcomeAcceptEarlyData
)

// String renders the outcome for logs and diagnostics.
func (o Outcome) String() string {
	switch o {
	case OutcomeFullHandshake:
		return "full_handshake"
	case OutcomeWaitForHandshake:
		return "wait_for_handshake"
	case OutcomeAcceptEarlyData:
		return "accept_early_data"
	default:
		return fmt.Sprintf("outcome(%d)", int(o))
	}
}

// ReasonCode is a stable machine-readable explanation attached to a Decision.
type ReasonCode string

const (
	// Accepted early data.
	CodeEarlyDataAccepted ReasonCode = "early_data_accepted"

	// Resume, but wait for the handshake to complete.
	CodeClassNotReadOnly         ReasonCode = "class_not_read_only"
	CodeConfirmationTooHigh      ReasonCode = "confirmation_above_early_data_max"
	CodeEarlyDataUnsupported     ReasonCode = "profile_early_data_unsupported"
	CodeProfileCompatibility     ReasonCode = "profile_compatibility_window"
	CodeProfileCompatibilityHigh ReasonCode = "profile_compatibility_high_confirmation"

	// Require a full handshake.
	CodeEntryUnknown         ReasonCode = "entry_unknown"
	CodeAliasInvalid         ReasonCode = "alias_invalid"
	CodeAliasExpired         ReasonCode = "alias_expired"
	CodeSourceDenied         ReasonCode = "source_denied"
	CodeEntryDomainUnset     ReasonCode = "entry_domain_unset"
	CodeDomainMismatch       ReasonCode = "domain_mismatch"
	CodeTicketInvalid        ReasonCode = "ticket_invalid"
	CodeTicketExpired        ReasonCode = "ticket_expired"
	CodeProfileUnknown       ReasonCode = "profile_unknown"
	CodeCompatibilityExpired ReasonCode = "profile_compatibility_expired"
	CodeClassUnregistered    ReasonCode = "class_unregistered"
	CodeEngineMissing        ReasonCode = "engine_missing"
)

// Decision is the result of one adjudication.
type Decision struct {
	// Outcome is the selected action.
	Outcome Outcome
	// Code is a stable reason identifier.
	Code ReasonCode
	// Reason is a human-readable explanation, always non-empty.
	Reason string
	// Entry is the canonical entry name after alias resolution. It is empty
	// when no configured entry was found.
	Entry EntryName
}

// Engine adjudicates tickets against the configured entry policies.
//
// The Config is held by value, but its maps are shared: replacing a map entry
// or assigning a new map on the Config that was passed to New affects
// subsequent adjudications. Assigning to a field of the Config value after New
// does not propagate. Callers that mutate configuration concurrently with
// adjudication must synchronize externally.
type Engine struct {
	cfg Config
}

// New builds an Engine from cfg. A nil Now field falls back to time.Now.
func New(cfg Config) *Engine {
	return &Engine{cfg: cfg}
}

// Adjudicate chooses the resumption action for one verified ticket and one
// request. It never modifies its inputs and calls the configured clock exactly
// once.
func (e *Engine) Adjudicate(t Ticket, r Request) Decision {
	if e == nil {
		return Decision{
			Outcome: OutcomeFullHandshake,
			Code:    CodeEngineMissing,
			Reason:  "no engine configured: full handshake required",
		}
	}

	now := e.now()

	if t.NotAfter.IsZero() {
		return Decision{
			Outcome: OutcomeFullHandshake,
			Code:    CodeTicketInvalid,
			Reason:  "ticket has no validity end: full handshake required",
		}
	}

	entry, entryName, failure := e.resolveEntry(now, t.Entry)
	if failure != nil {
		return *failure
	}

	decision := func(out Outcome, code ReasonCode, format string, args ...interface{}) Decision {
		return Decision{
			Outcome: out,
			Code:    code,
			Reason:  fmt.Sprintf(format, args...),
			Entry:   entryName,
		}
	}

	// Ticket claims: source, isolation domain, and validity.
	if !entry.AllowedSources[t.Source] {
		return decision(OutcomeFullHandshake, CodeSourceDenied,
			"ticket source %q is not allowed at entry %q: full handshake required", t.Source, entryName)
	}
	if entry.IsolationDomain == "" {
		return decision(OutcomeFullHandshake, CodeEntryDomainUnset,
			"entry %q does not name an isolation domain: full handshake required", entryName)
	}
	if t.IsolationDomain != entry.IsolationDomain {
		return decision(OutcomeFullHandshake, CodeDomainMismatch,
			"ticket isolation domain %q does not match entry %q domain %q: full handshake required",
			t.IsolationDomain, entryName, entry.IsolationDomain)
	}
	if !now.Before(t.NotAfter) {
		return decision(OutcomeFullHandshake, CodeTicketExpired,
			"ticket expired at %s: full handshake required", t.NotAfter.UTC().Format(time.RFC3339Nano))
	}

	// Protection profile state.
	profile, ok := entry.Profiles[t.Profile]
	if !ok {
		return decision(OutcomeFullHandshake, CodeProfileUnknown,
			"profile %q is not configured for entry %q: full handshake required", t.Profile, entryName)
	}
	active, compatibility := e.profileState(now, t.Profile, profile)
	if !active && !compatibility {
		return decision(OutcomeFullHandshake, CodeCompatibilityExpired,
			"profile %q retired at %s and its compatibility window (%s) has elapsed: full handshake required",
			t.Profile, profile.RetiredAt.UTC().Format(time.RFC3339Nano), e.profileWindow(t.Profile))
	}

	// Request class registration.
	class, ok := entry.Classes[r.Class]
	if !ok {
		return decision(OutcomeFullHandshake, CodeClassUnregistered,
			"request class %q is not registered at entry %q: full handshake required", r.Class, entryName)
	}

	if active {
		if !class.ReadOnly {
			return decision(OutcomeWaitForHandshake, CodeClassNotReadOnly,
				"request class %q is not read-only at entry %q: resuming but waiting for handshake", r.Class, entryName)
		}
		if class.ConfirmationLevel > entry.EarlyDataMaxConfirmation {
			return decision(OutcomeWaitForHandshake, CodeConfirmationTooHigh,
				"request class %q requires confirmation level %d above early-data limit %d at entry %q: waiting for handshake",
				r.Class, class.ConfirmationLevel, entry.EarlyDataMaxConfirmation, entryName)
		}
		if !profile.SupportsEarlyData {
			return decision(OutcomeWaitForHandshake, CodeEarlyDataUnsupported,
				"profile %q at entry %q does not support early data: resuming but waiting for handshake",
				t.Profile, entryName)
		}
		return decision(OutcomeAcceptEarlyData, CodeEarlyDataAccepted,
			"read-only request class %q within confirmation limit %d on active profile %q at entry %q: accepting early data",
			r.Class, entry.EarlyDataMaxConfirmation, t.Profile, entryName)
	}

	// Compatibility window: resumption stays available, early data never is.
	if class.ConfirmationLevel > entry.EarlyDataMaxConfirmation {
		return decision(OutcomeWaitForHandshake, CodeProfileCompatibilityHigh,
			"profile %q is in its compatibility window at entry %q and request class %q requires confirmation level %d above early-data limit %d: waiting for handshake completion",
			t.Profile, entryName, r.Class, class.ConfirmationLevel, entry.EarlyDataMaxConfirmation)
	}
	return decision(OutcomeWaitForHandshake, CodeProfileCompatibility,
		"profile %q is in its compatibility window at entry %q: resumption allowed, early data refused", t.Profile, entryName)
}

func (e *Engine) now() time.Time {
	if e.cfg.Now != nil {
		return e.cfg.Now()
	}
	return time.Now()
}

// resolveEntry maps a ticket entry name to the active policy, following at
// most one alias hop.
func (e *Engine) resolveEntry(now time.Time, name EntryName) (EntryPolicy, EntryName, *Decision) {
	if entry, ok := e.cfg.Entries[name]; ok {
		return entry, name, nil
	}

	alias, ok := e.cfg.Aliases[name]
	if !ok {
		return EntryPolicy{}, "", &Decision{
			Outcome: OutcomeFullHandshake,
			Code:    CodeEntryUnknown,
			Reason:  fmt.Sprintf("ticket entry %q is neither a configured entry nor a registered alias: full handshake required", name),
		}
	}
	if alias.NotAfter.IsZero() {
		return EntryPolicy{}, "", &Decision{
			Outcome: OutcomeFullHandshake,
			Code:    CodeAliasInvalid,
			Reason:  fmt.Sprintf("alias %q has no validity end: full handshake required", name),
		}
	}
	if !now.Before(alias.NotAfter) {
		return EntryPolicy{}, "", &Decision{
			Outcome: OutcomeFullHandshake,
			Code:    CodeAliasExpired,
			Reason: fmt.Sprintf("alias %q for entry %q expired at %s: full handshake required",
				name, alias.NewEntry, alias.NotAfter.UTC().Format(time.RFC3339Nano)),
		}
	}
	if alias.NewEntry == "" || alias.NewEntry == name {
		return EntryPolicy{}, "", &Decision{
			Outcome: OutcomeFullHandshake,
			Code:    CodeAliasInvalid,
			Reason:  fmt.Sprintf("alias %q does not name a different canonical entry: full handshake required", name),
		}
	}
	entry, ok := e.cfg.Entries[alias.NewEntry]
	if !ok {
		// Aliases resolve in a single hop; do not chase chains.
		return EntryPolicy{}, "", &Decision{
			Outcome: OutcomeFullHandshake,
			Code:    CodeAliasInvalid,
			Reason: fmt.Sprintf("alias %q points at %q, which is not a canonical entry: full handshake required",
				name, alias.NewEntry),
		}
	}
	return entry, alias.NewEntry, nil
}

// profileState reports whether the profile is active or in its compatibility
// window. If neither is true the profile must not be resumed.
func (e *Engine) profileState(now time.Time, id ProfileID, p ProfilePolicy) (active, compatibility bool) {
	if p.RetiredAt.IsZero() || now.Before(p.RetiredAt) {
		return true, false
	}
	window := e.profileWindow(id)
	if window <= 0 {
		return false, false
	}
	return false, now.Before(p.RetiredAt.Add(window))
}

func (e *Engine) profileWindow(id ProfileID) time.Duration {
	if w, ok := e.cfg.ProfileCompatibilityWindows[id]; ok {
		return w
	}
	return e.cfg.DefaultCompatibilityWindow
}
