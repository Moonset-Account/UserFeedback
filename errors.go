package connpool

import "errors"

var (
	// ErrEmptyDomain is returned when a request is evaluated without a host.
	ErrEmptyDomain = errors.New("connpool: empty domain")

	// ErrInvalidDomain is returned when a domain is not a valid ASCII DNS name.
	ErrInvalidDomain = errors.New("connpool: invalid domain")

	// ErrInvalidAddress is returned when an address is not an IP literal.
	ErrInvalidAddress = errors.New("connpool: invalid address")

	// ErrUnknownZone is returned when policy does not assign the domain to an
	// isolation zone. Reuse and dialing both refuse to proceed without a zone.
	ErrUnknownZone = errors.New("connpool: domain has no isolation zone")

	// ErrUnknownConnection is returned when an operation names a connection
	// that is not registered in the pool.
	ErrUnknownConnection = errors.New("connpool: unknown connection")

	// ErrDuplicateConnection is returned when a handshake record reuses an
	// existing connection id.
	ErrDuplicateConnection = errors.New("connpool: duplicate connection id")

	// ErrInvalidRecord is returned for malformed or unusable handshake data.
	ErrInvalidRecord = errors.New("connpool: invalid handshake record")

	// ErrNoZonePolicy is returned when the pool has no isolation-zone policy.
	ErrNoZonePolicy = errors.New("connpool: no isolation zone policy")

	// ErrNoResolver is returned when the pool has no address resolver.
	ErrNoResolver = errors.New("connpool: no address resolver")

	// ErrNoDialer is returned when a new connection is needed but the pool was
	// constructed without a Dialer.
	ErrNoDialer = errors.New("connpool: no dialer configured")

	// ErrNoAddresses is returned when a new connection is needed but the
	// resolver returned no usable address.
	ErrNoAddresses = errors.New("connpool: no resolved addresses")

	// ErrProtocolUnsupported is returned when a dialed connection did not
	// negotiate HTTP/2 and therefore cannot be multiplexed by this pool.
	ErrProtocolUnsupported = errors.New("connpool: protocol is not HTTP/2")

	// ErrCertificateMismatch is returned when a dialed connection's verified
	// domains do not cover the requested domain.
	ErrCertificateMismatch = errors.New("connpool: certificate does not cover domain")

	// ErrAddressMismatch is returned when a dialed connection landed on an
	// address outside the current resolution set.
	ErrAddressMismatch = errors.New("connpool: remote address not in resolved set")

	// ErrZoneMismatch is returned when a dialed connection belongs to a
	// different isolation zone than the requested domain.
	ErrZoneMismatch = errors.New("connpool: isolation zone mismatch")

	// ErrStreamEnded is returned when a stream is ended more than once.
	ErrStreamEnded = errors.New("connpool: stream already ended")
)
