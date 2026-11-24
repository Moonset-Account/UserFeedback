// Package negotiation implements bidirectional policy negotiation between
// two branches that each submit allowed-send and allowed-receive scopes.
//
// The primary entry point is Negotiate. It computes the exact
// protocol/source/destination/destination-port intersection for each traffic
// direction and also returns each side's applied-but-not-agreed remainder.
package negotiation
