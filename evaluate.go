package trustlist

import (
	"fmt"
	"time"
)

// Reason is a stable, machine-readable explanation for a decision.
type Reason string

const (
	// ReasonPinned: an in-scope pin line matches the presented host key.
	ReasonPinned Reason = "pinned"
	// ReasonCertTrusted: an in-scope cert-authority line trusts the issuer and
	// the certificate is currently valid for the connection name.
	ReasonCertTrusted Reason = "certificate-trusted"
	// ReasonRevoked: a valid revocation covers the presented key or its issuer.
	ReasonRevoked Reason = "revoked"
	// ReasonNoTrustAnchor: no line is in scope for this name and port.
	ReasonNoTrustAnchor Reason = "no-trust-anchor"
	// ReasonPortOutOfScope: the name matches but every matching line is scoped
	// to other ports.
	ReasonPortOutOfScope Reason = "port-out-of-scope"
	// ReasonKeyMismatch: pin lines are in scope but none matches the key.
	ReasonKeyMismatch Reason = "key-mismatch"
	// ReasonIssuerNotTrusted: no in-scope cert-authority line trusts the issuer.
	ReasonIssuerNotTrusted Reason = "issuer-not-trusted"
	// ReasonSignatureUnverified: the caller did not verify the certificate
	// signature.
	ReasonSignatureUnverified Reason = "signature-unverified"
	// ReasonCertificateExpired: the evaluation time is outside the validity
	// window.
	ReasonCertificateExpired Reason = "certificate-outside-validity"
	// ReasonNameNotCovered: no principal of the certificate covers the
	// connection name.
	ReasonNameNotCovered Reason = "name-not-covered"
	// ReasonInvalidRequest: the request itself was malformed (no name, no key,
	// bad port, no clock).
	ReasonInvalidRequest Reason = "invalid-request"
)

// Request describes one connection that is about to be trusted.
//
// Exactly one of HostKey and Certificate must be set, the way a real handshake
// offers either a plain host key or a host certificate. Give Evaluate a
// non-zero Now to pin the clock; the zero time falls back to the list's default
// clock, and if neither exists the request is refused rather than evaluated
// against a time nobody chose.
type Request struct {
	// Name is the connection name as the operator typed it. It is matched
	// against patterns and certificate principals verbatim; it is never
	// resolved, so two aliases of one address stay independent.
	Name string
	// Port is the connection port.
	Port int
	// HostKey is a plain host key offered by the peer. Leave it zero when
	// Certificate is set.
	HostKey PublicKey
	// Certificate is a verified-signed host certificate offered by the peer.
	Certificate *HostCertificate
	// Now is the evaluation time.
	Now time.Time
}

// Decision is the outcome for one connection. The zero value is not used: a
// decision about anything other than an empty list always comes back with an
// explicit Allowed value.
type Decision struct {
	// Allowed is the verdict: true only for pinned or certificate-trusted.
	Allowed bool
	// Reason is the stable code for the verdict.
	Reason Reason
	// Detail is a human-readable explanation.
	Detail string
	// Line is the 1-based line that produced the verdict, or 0 when no line is
	// responsible. For a certificate guarded by several issuer lines it is the
	// first such line. For a revocation it is the first revocation that
	// matches, since revocation overrides everything.
	Line int
	// ExcludedLines lists in-scope-but-excluded line numbers, in order. They
	// did not participate in the verdict.
	ExcludedLines []int
}

// Evaluation is a decision plus audit context.
type Evaluation struct {
	Decision
	// Issues are the list's parse issues plus any evaluation-time issue.
	Issues []Issue
	// Inspected counts the entries whose name scope admitted the connection
	// before exclusions were applied.
	Inspected int
}

// List.Allowed reports whether the connection is trusted. It is the short form
// of Evaluate for callers that do not want the audit trail.
func (l *List) Allowed(req Request) bool {
	return l.Evaluate(req).Allowed
}

// Evaluate reaches a verdict for one connection.
//
// The order is fixed and does not depend on line order:
//  1. rejection of malformed requests;
//  2. revocation, which beats pinning and issuer trust everywhere;
//  3. pinning for a plain host key, or issuer trust plus certificate checks
//     for a host certificate.
//
// A list that is in scope but somehow has no usable line fails closed.
func (l *List) Evaluate(req Request) Evaluation {
	ev := Evaluation{Issues: l.Issues()}
	deny := func(reason Reason, line int, detail string) Evaluation {
		ev.Allowed = false
		ev.Reason = reason
		ev.Line = line
		ev.Detail = detail
		return ev
	}

	if l == nil {
		return deny(ReasonNoTrustAnchor, 0, "no trust list")
	}
	if err := validateRequest(req); err != nil {
		ev.Issues = append(ev.Issues, Issue{Kind: IssueInvalidRequest, Msg: err.Error()})
		return deny(ReasonInvalidRequest, 0, err.Error())
	}

	now, ok := decisionTime(req.Now, l.now)
	if !ok {
		ev.Issues = append(ev.Issues, Issue{Kind: IssueInvalidTime, Msg: "no evaluation time supplied"})
		return deny(ReasonInvalidRequest, 0, "no evaluation time: set Request.Now or List.SetNow")
	}

	var certificate *HostCertificate
	presented := req.HostKey
	if req.Certificate != nil {
		certificate = req.Certificate
		presented = certificate.Subject
	}

	// Bucket every entry that the name and port admit. Exclusions are applied
	// here, once, so an excluded line can never leak into any later pass.
	var pins, issuers, revocations []Entry
	for _, entry := range l.entries {
		if !entry.inScope(req.Name, req.Port) {
			continue
		}
		ev.Inspected++
		if entry.excluded(req.Name) {
			ev.ExcludedLines = append(ev.ExcludedLines, entry.Line)
			continue
		}
		switch entry.Action {
		case ActionPin:
			pins = append(pins, entry)
		case ActionCertAuthority:
			issuers = append(issuers, entry)
		case ActionRevoke:
			revocations = append(revocations, entry)
		}
	}

	// Pass 1: revocation. It is evaluated first and independently of line
	// order, so neither a pin nor an issuer line, wherever it sits, can
	// outrank a valid revocation.
	for _, entry := range revocations {
		if entry.coversKey(presented, certificate) {
			return deny(ReasonRevoked, entry.Line, fmt.Sprintf(
				"key %s is revoked by line %d", maskedKey(entry.Key), entry.Line))
		}
	}

	// Pass 2: plain host key.
	if certificate == nil {
		for _, entry := range pins {
			if keyEqual(entry.Key, presented) {
				ev.Allowed = true
				ev.Reason = ReasonPinned
				ev.Line = entry.Line
				ev.Detail = fmt.Sprintf("host key matches pin on line %d", entry.Line)
				return ev
			}
		}
		if len(pins) > 0 {
			return deny(ReasonKeyMismatch, pins[0].Line, fmt.Sprintf(
				"host key does not match any of %d pin line(s) in scope", len(pins)))
		}
		return denyPortOrAnchor(l, req, deny)
	}

	// Pass 3: host certificate.
	var trusted []Entry
	for _, entry := range issuers {
		if keyEqual(entry.Key, certificate.Issuer) {
			trusted = append(trusted, entry)
		}
	}
	if len(trusted) == 0 {
		if len(issuers) == 0 {
			return denyPortOrAnchor(l, req, deny)
		}
		return deny(ReasonIssuerNotTrusted, issuers[0].Line, fmt.Sprintf(
			"issuer %s is not trusted by any of %d cert-authority line(s) in scope",
			maskedKey(certificate.Issuer), len(issuers)))
	}
	anchor := trusted[0].Line
	if !certificate.SignatureVerified {
		return deny(ReasonSignatureUnverified, anchor,
			"certificate signature was not verified by the caller")
	}
	if now.Before(certificate.NotBefore) {
		return deny(ReasonCertificateExpired, anchor, fmt.Sprintf(
			"certificate is not valid before %s; evaluated at %s",
			certificate.NotBefore.Format(time.RFC3339), now.Format(time.RFC3339)))
	}
	if !now.Before(certificate.NotAfter) {
		return deny(ReasonCertificateExpired, anchor, fmt.Sprintf(
			"certificate expired at %s; evaluated at %s",
			certificate.NotAfter.Format(time.RFC3339), now.Format(time.RFC3339)))
	}
	if !certificate.coversName(req.Name) {
		return deny(ReasonNameNotCovered, anchor, fmt.Sprintf(
			"certificate principals do not cover connection name %q", req.Name))
	}

	ev.Allowed = true
	ev.Reason = ReasonCertTrusted
	ev.Line = anchor
	ev.Detail = fmt.Sprintf("issuer trusted by line %d and certificate covers %q", anchor, req.Name)
	return ev
}

// denyPortOrAnchor distinguishes "the name matched but only on other ports"
// from "nothing matched at all", because the two call for different operator
// responses even though both deny. Only positive name matches count: a line
// that merely excluded this name never granted anything, so it must not make
// the name look covered.
func denyPortOrAnchor(l *List, req Request, deny func(Reason, int, string) Evaluation) Evaluation {
	for _, entry := range l.entries {
		if !matchNameAny(entry.Positives, req.Name) || entry.Ports.Contains(req.Port) {
			continue
		}
		// A line that excludes this name never granted it anything, so it
		// cannot make the name look covered on other ports either.
		if entry.excluded(req.Name) {
			continue
		}
		return deny(ReasonPortOutOfScope, entry.Line, fmt.Sprintf(
			"name %q is covered only on other ports; connection port is %d",
			req.Name, req.Port))
	}
	return deny(ReasonNoTrustAnchor, 0, fmt.Sprintf(
		"no trust-list line covers %q on port %d", req.Name, req.Port))
}

// validateRequest rejects requests that cannot be judged safely.
func validateRequest(req Request) error {
	switch {
	case canonicalName(req.Name) == "":
		return fmt.Errorf("request has no connection name")
	case req.Port < 1 || req.Port > 65535:
		return fmt.Errorf("request port %d is outside 1..65535", req.Port)
	case req.Certificate != nil && req.HostKey.valid():
		return fmt.Errorf("request offers both a host key and a certificate")
	case req.Certificate == nil && !req.HostKey.valid():
		return fmt.Errorf("request offers neither a host key nor a certificate")
	}
	return nil
}

// maskedKey shortens a key blob for log lines. Blobs are public, but full
// blobs make detail strings unreadable; the type and a short prefix identify
// the key well enough to find the offending line.
func maskedKey(k PublicKey) string {
	blob := k.Blob
	if len(blob) > 12 {
		blob = blob[:12] + "..."
	}
	return normalizeKeyType(k.Type) + ":" + blob
}
