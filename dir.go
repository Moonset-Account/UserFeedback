package heap

import "sync"

// FreeSpaceDir is an approximate free-space directory. For each known page
// it stores the page's free space rounded down to a whole capacity tier, so
// a stored tier always guarantees tier*tierSize free bytes at the moment it
// was recorded. Hints can go stale as concurrent writers consume space;
// writers revalidate under the page lock and correct the hint on failure.
type FreeSpaceDir struct {
	mu       sync.Mutex
	tierSize int
	tiers    map[PageID]int // page -> capacity tier (freeBytes / tierSize)
	order    []PageID       // pages in first-seen order, for deterministic scans
}

func newFreeSpaceDir(tierSize int) *FreeSpaceDir {
	return &FreeSpaceDir{
		tierSize: tierSize,
		tiers:    make(map[PageID]int),
	}
}

// Update records freeBytes for page id, rounded down to a capacity tier.
func (d *FreeSpaceDir) Update(id PageID, freeBytes int) {
	d.mu.Lock()
	defer d.mu.Unlock()
	if _, ok := d.tiers[id]; !ok {
		d.order = append(d.order, id)
	}
	d.tiers[id] = freeBytes / d.tierSize
}

// Search returns the first page whose recorded tier can hold needBytes.
func (d *FreeSpaceDir) Search(needBytes int) (PageID, bool) {
	return d.search(needBytes, nil)
}

// search is Search plus an exclusion set, so a writer that already failed
// on a page never loops back onto it because of a stale hint.
func (d *FreeSpaceDir) search(needBytes int, skip map[PageID]bool) (PageID, bool) {
	d.mu.Lock()
	defer d.mu.Unlock()
	needTier := (needBytes + d.tierSize - 1) / d.tierSize
	for _, id := range d.order {
		if skip[id] {
			continue
		}
		if d.tiers[id] >= needTier {
			return id, true
		}
	}
	return 0, false
}

// TierOf reports the capacity tier currently recorded for page id.
func (d *FreeSpaceDir) TierOf(id PageID) (tier int, ok bool) {
	d.mu.Lock()
	defer d.mu.Unlock()
	tier, ok = d.tiers[id]
	return tier, ok
}

// TierBytes converts a capacity tier to the free bytes it guarantees.
func (d *FreeSpaceDir) TierBytes(tier int) int {
	return tier * d.tierSize
}
