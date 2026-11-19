// Package trustlist evaluates a local, line-oriented trust list before a
// jump-host client is allowed to trust the key material a peer presents.
//
// # Inputs
//
// A caller supplies a connection name (the host string the operator actually
// typed), a port, and exactly one credential: a plain host key, or a host
// certificate whose signature the transport has already verified. The
// certificate also carries an issuer key, a validity window and a list of
// principals, and Evaluate itself re-checks the time window and the name
// coverage because only the caller knows the connection name.
//
// # Trust-list grammar
//
// One entry per line. Blank lines and lines whose first non-space byte is '#'
// are ignored, but they still consume a line number so that reported line
// numbers match the physical file.
//
//	<name-patterns> [port=<range>] <action> <key>
//
//	<name-patterns> := comma separated patterns; a pattern beginning with '!'
//	                   is an exclusion pattern
//	<range>         := '*' | <port> | <lo>-<hi>   (inclusive, 1..65535)
//	<action>        := pin | cert-authority | revoke
//	<key>           := <type>:<blob>  or  <type> <blob>
//
// Example:
//
//	# physical line 1
//	db.example.com,!db.internal.example.com port=22-2222 pin ssh-ed25519:AAAAB3Nza
//	*.example.com cert-authority ssh-ed25519:CAKEY
//	*.example.com revoke ssh-ed25519:OLDKEY
//
// # Matching rules
//
// A line is in scope when at least one positive pattern matches the connection
// name and the port falls inside the line's range. Patterns are globs with '*'
// and '?', CIDR blocks for IP-literal names, and case-insensitive literals;
// names are normalised by dropping one trailing dot and surrounding brackets.
// Patterns are matched against the connection name only. The client never
// resolves the name, so two aliases of one address are judged separately, and
// the port is part of the scope so one name on two ports is judged separately.
//
// If any exclusion pattern of a line matches the name, the whole line is
// dropped: it grants nothing, revokes nothing, and cannot influence any other
// line. It is reported in Decision.ExcludedLines for audit.
//
// # Resolution order
//
//  1. Revocation. Any in-scope revoke line whose key equals the presented key
//     (or, for a certificate, either the subject key or the issuer key) denies
//     the connection. This pass runs before pinning and issuer trust and does
//     not depend on line order, so a valid revocation always wins.
//  2. Plain host key: in-scope pin lines. A matching pinned key allows; if pin
//     lines are in scope but none matches, the connection is denied rather
//     than left undecided.
//  3. Certificate: in-scope cert-authority lines. A trusted issuer allows only
//     if the signature is marked verified, the current time is inside the
//     inclusive validity window, and a principal covers the connection name.
//
// With no in-scope line at all the verdict is deny ("no-trust-anchor"): the
// list is an allow list. Every decision carries the 1-based physical line
// number that produced it, or 0 when no line was responsible.
package trustlist
