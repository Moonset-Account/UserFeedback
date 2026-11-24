// Package tlsresume adjudicates TLS session resumption at an edge entry point.
//
// The handshake layer authenticates the ticket origin before a Ticket is handed
// to this package; the package only applies entry-point policy. Given a
// verified ticket, the service entry, isolation domain, protection profile,
// ticket validity, the request class, and the entry's current policy, Engine
// chooses one of three outcomes:
//
//   - FullHandshake: discard the ticket and authenticate from scratch;
//   - WaitForHandshake: resume the session, but hold the request until the
//     resumption handshake completes;
//   - AcceptEarlyData: hand the request to the application immediately as
//     early data, but only for request classes registered as read-only.
//
// Every decision carries a stable ReasonCode and a human-readable Reason.
package tlsresume

import "time"

// SourceID identifies the issuer of a session ticket. The handshake layer has
// already authenticated the value before it is placed in a Ticket.
type SourceID string

// EntryName is the service entry name, either the canonical name or a name
// registered as an alias while an entry is being renamed.
type EntryName string

// ProfileID names a protection profile (cipher suite / certificate profile
// generation) that a ticket was issued under.
type ProfileID string

// RequestClass names the kind of request carried by the connection.
type RequestClass string

// ConfirmationLevel ranks how strongly the client and the request must be
// confirmed before the request may be processed. Larger values demand a
// stronger confirmation. The value 0 is the ordinary level.
type ConfirmationLevel int

// Ticket is the handshake-verified input of one adjudication.
//
// Source, Entry, IsolationDomain, and Profile are the ticket's claims; the
// handshake layer is responsible for their authenticity. NotAfter is the end
// of the ticket's validity period; the ticket is valid while now < NotAfter.
// A zero NotAfter is rejected.
type Ticket struct {
	Source          SourceID
	Entry           EntryName
	IsolationDomain string
	Profile         ProfileID
	NotAfter        time.Time
}

// Request is the request-specific input of one adjudication.
type Request struct {
	Class RequestClass
}

// ClassPolicy is an entry's registration for one request class.
//
// ReadOnly marks classes whose requests may be accepted as early data.
// ConfirmationLevel is the level that requests of this class require.
type ClassPolicy struct {
	ReadOnly          bool
	ConfirmationLevel ConfirmationLevel
}

// ProfilePolicy binds one protection profile to an entry.
//
// A zero RetiredAt means the profile is active. Once RetiredAt is reached the
// profile enters its compatibility window: ordinary session resumption stays
// available, but early data is refused. The length of the window comes from
// Config.ProfileCompatibilityWindows (or Config.DefaultCompatibilityWindow),
// configured separately from the entry so that one profile's grace period can
// be tuned without touching every entry.
//
// SupportsEarlyData lets an active profile opt out of 0-RTT entirely.
type ProfilePolicy struct {
	RetiredAt         time.Time
	SupportsEarlyData bool
}

// EntryPolicy is the current policy of one service entry.
//
// AllowedSources is an allow-list of ticket sources. An empty or nil map
// denies every source; the entry must name its issuers explicitly.
// IsolationDomain is the only domain whose tickets the entry accepts.
// EarlyDataMaxConfirmation is the highest confirmation level that early data
// may carry at this entry; levels above it wait for the handshake.
type EntryPolicy struct {
	IsolationDomain          string
	AllowedSources           map[SourceID]bool
	Profiles                 map[ProfileID]ProfilePolicy
	Classes                  map[RequestClass]ClassPolicy
	EarlyDataMaxConfirmation ConfirmationLevel
}

// Alias registers an old entry name against the entry that replaced it during
// a rename. The registration is valid while now < NotAfter; a zero NotAfter is
// invalid and the alias is refused, because a rename mapping must not become
// permanent by omission. Aliases resolve in a single hop: the target must be a
// canonical entry, not another alias.
type Alias struct {
	NewEntry EntryName
	NotAfter time.Time
}

// Config is the entry-point policy configuration.
//
// Entries and Aliases are read on every Adjudicate call, so a policy manager
// may replace them at runtime. ProfileCompatibilityWindows is keyed only by
// profile and is applied to every entry that retires that profile. A
// non-positive window disables the compatibility period: a retired profile is
// then immediately unusable, forcing a full handshake.
type Config struct {
	Entries map[EntryName]EntryPolicy
	Aliases map[EntryName]Alias

	ProfileCompatibilityWindows map[ProfileID]time.Duration
	DefaultCompatibilityWindow  time.Duration

	// Now overrides the clock; it is called once per adjudication. When nil,
	// time.Now is used.
	Now func() time.Time
}
