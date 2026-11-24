package rebalancer

import (
	"errors"
	"fmt"
	"math/big"
)

// Constraints from the problem statement:
//   - at most 16 indivisible partitions
//   - a partition may only be assigned to a consumer that subscribes to it
//   - mirror partitions of one data family must land in distinct fault domains
//   - every consumer has an integer capacity
//   - every partition has an integer weight and (optionally) an original holder

const MaxPartitions = 16

// Partition is an indivisible unit of work.
type Partition struct {
	// ID is the partition number. Partition numbers define lexicographic
	// order of the ownership table, so IDs must be 0..n-1 after validation.
	ID     int
	Weight int
	Family string
	Domain string
	Origin int    // original holder consumer index; -1 if there is none
	SubsOK []bool // per consumer: whether this consumer may subscribe
}

// Consumer is a member with integer capacity.
type Consumer struct {
	ID       int
	Capacity int
	Domain   string
}

// Problem is the full coordinator input.
type Problem struct {
	Partitions []Partition
	Consumers  []Consumer
	// InFlightCap bounds the total weight of any batch that is simultaneously
	// in hand-off (revoked but not yet consumable at the destination).
	InFlightCap int
}

// Assignment maps every partition index to the consumer that owns it.
type Assignment []int

// Input validation errors.
var (
	ErrInvalidInput = errors.New("rebalancer: invalid input")
	ErrInfeasible   = errors.New("rebalancer: no feasible assignment exists")
)

// Validate checks every structural precondition of the problem.
func (p *Problem) Validate() error {
	n := len(p.Partitions)
	if n == 0 || n > MaxPartitions {
		return fmt.Errorf("%w: partition count must be in 1..%d, got %d",
			ErrInvalidInput, MaxPartitions, n)
	}
	if len(p.Consumers) == 0 {
		return fmt.Errorf("%w: at least one consumer is required", ErrInvalidInput)
	}
	seenID := make(map[int]bool, n)
	for i := range p.Partitions {
		pt := &p.Partitions[i]
		if pt.ID != i {
			return fmt.Errorf("%w: partition IDs must be dense 0..n-1 in order; "+
				"index %d has ID %d", ErrInvalidInput, i, pt.ID)
		}
		if seenID[pt.ID] {
			return fmt.Errorf("%w: duplicate partition id %d", ErrInvalidInput, pt.ID)
		}
		seenID[pt.ID] = true
		if pt.Weight <= 0 {
			return fmt.Errorf("%w: partition %d must have positive weight",
				ErrInvalidInput, pt.ID)
		}
		if pt.Family == "" {
			return fmt.Errorf("%w: partition %d has empty family", ErrInvalidInput, pt.ID)
		}
		if pt.Domain == "" {
			return fmt.Errorf("%w: partition %d has empty domain", ErrInvalidInput, pt.ID)
		}
		if len(pt.SubsOK) != len(p.Consumers) {
			return fmt.Errorf("%w: partition %d subscription set size %d != consumers %d",
				ErrInvalidInput, pt.ID, len(pt.SubsOK), len(p.Consumers))
		}
		anySub := false
		for _, ok := range pt.SubsOK {
			if ok {
				anySub = true
				break
			}
		}
		if !anySub {
			return fmt.Errorf("%w: partition %d has no subscribing consumer",
				ErrInvalidInput, pt.ID)
		}
		if pt.Origin < -1 || pt.Origin >= len(p.Consumers) {
			return fmt.Errorf("%w: partition %d origin %d out of range",
				ErrInvalidInput, pt.ID, pt.Origin)
		}
		if pt.Origin >= 0 && !pt.SubsOK[pt.Origin] {
			return fmt.Errorf("%w: partition %d origin consumer %d does not subscribe",
				ErrInvalidInput, pt.ID, pt.Origin)
		}
	}
	seenC := make(map[int]bool, len(p.Consumers))
	for i := range p.Consumers {
		c := &p.Consumers[i]
		if c.ID != i {
			return fmt.Errorf("%w: consumer IDs must be dense 0..m-1 in order; "+
				"index %d has ID %d", ErrInvalidInput, i, c.ID)
		}
		if seenC[c.ID] {
			return fmt.Errorf("%w: duplicate consumer id %d", ErrInvalidInput, c.ID)
		}
		seenC[c.ID] = true
		if c.Capacity <= 0 {
			return fmt.Errorf("%w: consumer %d must have positive capacity",
				ErrInvalidInput, c.ID)
		}
		if c.Domain == "" {
			return fmt.Errorf("%w: consumer %d has empty domain", ErrInvalidInput, c.ID)
		}
	}
	if p.InFlightCap <= 0 {
		return fmt.Errorf("%w: in-flight cap must be positive", ErrInvalidInput)
	}
	return nil
}

// loads computes the total assigned weight per consumer.
func (p *Problem) loads(a Assignment) []int {
	loads := make([]int, len(p.Consumers))
	for i, c := range a {
		if c < 0 {
			continue // partially assigned table (decision-procedure internals)
		}
		loads[c] += p.Partitions[i].Weight
	}
	return loads
}

// MaxNormalizedLoad returns max_c load[c]/capacity[c] as an exact fraction.
func (p *Problem) MaxNormalizedLoad(a Assignment) *big.Rat {
	return maxLoad(p.loads(a), p.capacities())
}

// MigrationWeight sums the weights of partitions whose holder changed.
// Partitions without an original holder (Origin == -1) never count as
// migration: there is no old ownership to revoke.
func (p *Problem) MigrationWeight(a Assignment) int {
	total := 0
	for i, c := range a {
		pt := &p.Partitions[i]
		if pt.Origin >= 0 && c != pt.Origin {
			total += pt.Weight
		}
	}
	return total
}

func (p *Problem) capacities() []int {
	caps := make([]int, len(p.Consumers))
	for i := range p.Consumers {
		caps[i] = p.Consumers[i].Capacity
	}
	return caps
}

// feasible checks subscription and family/fault-domain constraints.
func (p *Problem) feasible(a Assignment) bool {
	// family -> set of domains already used by an assigned mirror.
	famDomain := make(map[string]map[string]bool)
	for i, c := range a {
		pt := &p.Partitions[i]
		if !pt.SubsOK[c] {
			return false
		}
		dom := p.Consumers[c].Domain
		used, ok := famDomain[pt.Family]
		if !ok {
			used = make(map[string]bool)
			famDomain[pt.Family] = used
		}
		if used[dom] {
			return false
		}
		used[dom] = true
	}
	return true
}
