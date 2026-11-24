# tlsresume

Edge-entry adjudicator for TLS session resumption. The handshake layer has
already authenticated the ticket's origin; this package applies entry-point
policy and returns one of three actions for each connection:

| Outcome | Meaning |
| --- | --- |
| `OutcomeFullHandshake` | Discard the ticket, authenticate from scratch. |
| `OutcomeWaitForHandshake` | Resume the session, but hold the request until the resumption handshake completes. |
| `OutcomeAcceptEarlyData` | Process the request immediately as 0-RTT early data (read-only classes only). |

Every `Decision` carries a stable `ReasonCode` and a non-empty human-readable
`Reason`, so callers can log exactly why a request waited or re-handshook.

## Inputs

`Adjudicate(Ticket, Request)` receives:

- `Ticket.Source` — authenticated ticket issuer (`SourceID`).
- `Ticket.Entry` — service entry name claimed by the ticket (may be an old
  name registered as an alias during a rename).
- `Ticket.IsolationDomain` — isolation domain claimed by the ticket.
- `Ticket.Profile` — protection profile the ticket was issued under.
- `Ticket.NotAfter` — end of ticket validity (`now < NotAfter`). Zero is
  rejected.
- `Request.Class` — request class.
- `Config` — the entry's current policy: allow-listed sources, isolation
  domain, per-profile state, per-class registration, and the early-data
  confirmation ceiling, plus alias registrations and profile compatibility
  windows.

## Public API

```go
type Engine struct{ /* ... */ }

func New(cfg Config) *Engine
func (e *Engine) Adjudicate(t Ticket, r Request) Decision

type Outcome int
const (
    OutcomeFullHandshake Outcome = iota
    OutcomeWaitForHandshake
    OutcomeAcceptEarlyData
)
func (o Outcome) String() string

type Decision struct {
    Outcome Outcome
    Code    ReasonCode
    Reason  string
    Entry   EntryName // canonical entry after alias resolution
}
```

Types: `SourceID`, `EntryName`, `ProfileID`, `RequestClass`,
`ConfirmationLevel`, `Ticket`, `Request`, `ClassPolicy`, `ProfilePolicy`,
`EntryPolicy`, `Alias`, `Config`, `ReasonCode`.

## Decision rules

1. A ticket with no `NotAfter` is invalid → full handshake.
2. Entry resolution:
   - a configured entry is used as-is;
   - otherwise one alias hop is followed, and only while `now < alias.NotAfter`
     (zero `NotAfter` is invalid, self-targets and chains are invalid);
   - unresolved, invalid, or expired aliases → full handshake.
3. Source must be in the entry's allow-list (an empty/nil list denies all);
   the entry must name an isolation domain and the ticket must match it;
   the ticket must not be expired. Any failure → full handshake.
4. The profile must be configured for the entry:
   - active (`RetiredAt` zero or in the future) → eligible for early data if
     the profile supports it;
   - retired but inside its compatibility window (length from
     `ProfileCompatibilityWindows[profile]`, else `DefaultCompatibilityWindow`;
     a non-positive window disables compatibility) → resumption allowed, early
     data refused;
   - retired and past the window → full handshake.
5. The request class must be registered or the result is a full handshake.
6. **Active profile.** Read-only class and `ConfirmationLevel <=
   EarlyDataMaxConfirmation` and `SupportsEarlyData` → accept early data.
   Otherwise wait for the handshake, with a code-specific reason
   (`class_not_read_only`, `confirmation_above_early_data_max`,
   `profile_early_data_unsupported`).
7. **Compatibility window.** Ordinary resumption stays available: wait for the
   handshake. Requests above the entry's early-data confirmation ceiling get
   `profile_compatibility_high_confirmation`; ordinary requests get
   `profile_compatibility`. Neither is accepted as early data.

The verification order is fixed and each failure short-circuits, so the
returned `Reason` names exactly the first rule that failed.

## Running

```sh
go test ./...
```

Requires Go 1.20.2. No external dependencies.

## Verification scope

Tests use fully synthetic inputs and a fixed clock (`Config.Now`) and cover:

- early-data acceptance for a read-only class at the confirmation limit;
- waiting for a non-read-only class, a class above the early-data ceiling, and
  a profile that opts out of 0-RTT;
- compatibility-window behaviour: at retirement (resumable), at the exact end
  of the window (full handshake), per-profile window overriding the default,
  and a zero window disabling compatibility;
- full-handshake rejections for unknown entry, denied/empty source list, unset
  entry domain, domain mismatch, expired and zero-expiry tickets, unknown
  profile, and unregistered class;
- alias resolution to the canonical entry, plus expired, zero-expiry,
  self-target, missing-target, chained, and absent aliases;
- rule ordering: entry resolution wins over source, source wins over profile,
  profile wins over class, and ticket expiry is reported before profile state;
- a canonical entry-name match taking priority over a same-named alias entry;
- a nil `*Engine` forcing a full handshake, the clock being called exactly
  once per adjudication, and `Outcome.String` rendering.

Not covered: concurrency. `Engine` copies the `Config` value at `New`, but its
maps are shared; callers that mutate policy while adjudicating must synchronize
externally. `go test ./... -race` passes, but there is no concurrent test
driving the engine.
