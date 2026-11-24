package negotiation

import "fmt"

// Channel identifies whether an unmatched application was sent as a send
// policy or a receive policy.
type Channel string

const (
	ChannelSend    Channel = "send"
	ChannelReceive Channel = "receive"
)

// Rule is a mutually non-overlapping deployable scope in one direction.
type Rule struct {
	Direction string `json:"direction"`
	Scope
}

// UnmatchedScope is an effective applied range that the counterparty did not
// accept in the relevant direction.
type UnmatchedScope struct {
	Direction string  `json:"direction"`
	PartyID   string  `json:"party_id"`
	Channel   Channel `json:"channel"`
	Scope
}

// DirectionResult contains the adjudication for one sender->receiver flow.
type DirectionResult struct {
	Direction         string  `json:"direction"`
	From              string  `json:"from"`
	To                string  `json:"to"`
	Agreed            []Rule  `json:"agreed"`
	SenderUnmatched   []Scope `json:"sender_unmatched"`
	ReceiverUnmatched []Scope `json:"receiver_unmatched"`
}

// Result contains both directional adjudications and convenient flat lists.
type Result struct {
	PartyA    string           `json:"party_a"`
	PartyB    string           `json:"party_b"`
	Forward   DirectionResult  `json:"forward"`
	Reverse   DirectionResult  `json:"reverse"`
	Rules     []Rule           `json:"rules"`
	Unmatched []UnmatchedScope `json:"unmatched"`
}

// Negotiate adjudicates A->B from A.Send intersect B.Receive, and B->A from
// B.Send intersect A.Receive.
func Negotiate(a, b Party) (*Result, error) {
	idA := defaultPartyID(a.ID, "A")
	idB := defaultPartyID(b.ID, "B")
	dirAB := idA + "->" + idB
	dirBA := idB + "->" + idA

	aSend, err := compilePartyPolicy(idA, ChannelSend, a.Send)
	if err != nil {
		return nil, err
	}
	aReceive, err := compilePartyPolicy(idA, ChannelReceive, a.Receive)
	if err != nil {
		return nil, err
	}
	bSend, err := compilePartyPolicy(idB, ChannelSend, b.Send)
	if err != nil {
		return nil, err
	}
	bReceive, err := compilePartyPolicy(idB, ChannelReceive, b.Receive)
	if err != nil {
		return nil, err
	}

	forwardAgreed := intersectBoxes(aSend, bReceive)
	forwardSenderDenied := subtractBoxes(aSend, forwardAgreed)
	forwardReceiverDenied := subtractBoxes(bReceive, forwardAgreed)

	reverseAgreed := intersectBoxes(bSend, aReceive)
	reverseSenderDenied := subtractBoxes(bSend, reverseAgreed)
	reverseReceiverDenied := subtractBoxes(aReceive, reverseAgreed)

	forward := DirectionResult{
		Direction:         dirAB,
		From:              idA,
		To:                idB,
		Agreed:            toRules(dirAB, boxesToScopes(forwardAgreed)),
		SenderUnmatched:   boxesToScopes(forwardSenderDenied),
		ReceiverUnmatched: boxesToScopes(forwardReceiverDenied),
	}
	reverse := DirectionResult{
		Direction:         dirBA,
		From:              idB,
		To:                idA,
		Agreed:            toRules(dirBA, boxesToScopes(reverseAgreed)),
		SenderUnmatched:   boxesToScopes(reverseSenderDenied),
		ReceiverUnmatched: boxesToScopes(reverseReceiverDenied),
	}

	result := &Result{
		PartyA:  idA,
		PartyB:  idB,
		Forward: forward,
		Reverse: reverse,
		Rules:   make([]Rule, 0, len(forward.Agreed)+len(reverse.Agreed)),
		Unmatched: make(
			[]UnmatchedScope,
			0,
			len(forward.SenderUnmatched)+len(forward.ReceiverUnmatched)+
				len(reverse.SenderUnmatched)+len(reverse.ReceiverUnmatched),
		),
	}
	result.Rules = append(result.Rules, forward.Agreed...)
	result.Rules = append(result.Rules, reverse.Agreed...)

	result.Unmatched = append(result.Unmatched,
		toUnmatched(dirAB, idA, ChannelSend, forward.SenderUnmatched)...)
	result.Unmatched = append(result.Unmatched,
		toUnmatched(dirAB, idB, ChannelReceive, forward.ReceiverUnmatched)...)
	result.Unmatched = append(result.Unmatched,
		toUnmatched(dirBA, idB, ChannelSend, reverse.SenderUnmatched)...)
	result.Unmatched = append(result.Unmatched,
		toUnmatched(dirBA, idA, ChannelReceive, reverse.ReceiverUnmatched)...)

	return result, nil
}

// NegotiatePolicies is a descriptive alias for Negotiate.
func NegotiatePolicies(a, b Party) (*Result, error) {
	return Negotiate(a, b)
}

// MergeRules canonicalizes rules separately by Direction. It is useful for
// renormalizing externally constructed rule sets with the same exact union
// and correspondence-preserving merge logic used by Negotiate.
func MergeRules(rules []Rule) ([]Rule, error) {
	directions := make([]string, 0)
	groups := make(map[string][]Scope)
	for _, rule := range rules {
		if _, ok := groups[rule.Direction]; !ok {
			directions = append(directions, rule.Direction)
		}
		groups[rule.Direction] = append(groups[rule.Direction], rule.Scope)
	}

	merged := make([]Rule, 0, len(rules))
	for _, direction := range directions {
		scopes, err := MergeScopes(groups[direction])
		if err != nil {
			return nil, fmt.Errorf("direction %q: %w", direction, err)
		}
		merged = append(merged, toRules(direction, scopes)...)
	}
	return merged, nil
}

// RulesPairwiseDisjoint reports whether rules with the same Direction have no
// overlapping flow tuples. Rules in different directions are intentionally
// treated as separate enforcement directions.
func RulesPairwiseDisjoint(rules []Rule) bool {
	for i := 0; i < len(rules); i++ {
		for j := i + 1; j < len(rules); j++ {
			if RulesOverlap(rules[i], rules[j]) {
				return false
			}
		}
	}
	return true
}

// RulesOverlap reports whether two rules enforce an intersecting tuple in the
// same direction.
func RulesOverlap(a, b Rule) bool {
	if a.Direction != b.Direction {
		return false
	}
	return ScopesOverlap(a.Scope, b.Scope)
}

// CheckRulesDisjoint returns an error identifying the first overlapping pair.
func CheckRulesDisjoint(rules []Rule) error {
	for i := 0; i < len(rules); i++ {
		for j := i + 1; j < len(rules); j++ {
			if RulesOverlap(rules[i], rules[j]) {
				return fmt.Errorf("rules %d and %d overlap: %s and %s", i, j, ruleLabel(rules[i]), ruleLabel(rules[j]))
			}
		}
	}
	return nil
}

func compilePartyPolicy(partyID string, channel Channel, policy Policy) ([]box, error) {
	boxes, err := policy.compile()
	if err != nil {
		return nil, fmt.Errorf("party %q %s policy: %w", partyID, channel, err)
	}
	return boxes, nil
}

func defaultPartyID(id, fallback string) string {
	if id == "" {
		return fallback
	}
	return id
}

func toRules(direction string, scopes []Scope) []Rule {
	rules := make([]Rule, 0, len(scopes))
	for _, scope := range scopes {
		rules = append(rules, Rule{Direction: direction, Scope: scope})
	}
	return rules
}

func toUnmatched(direction, partyID string, channel Channel, scopes []Scope) []UnmatchedScope {
	out := make([]UnmatchedScope, 0, len(scopes))
	for _, scope := range scopes {
		out = append(out, UnmatchedScope{
			Direction: direction,
			PartyID:   partyID,
			Channel:   channel,
			Scope:     scope,
		})
	}
	return out
}

func ruleLabel(r Rule) string {
	return fmt.Sprintf("%s %s %s->%s ports%v", r.Direction, r.Protocol, r.Source, r.Destination, r.DestinationPorts)
}
