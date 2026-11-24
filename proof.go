package graphwalk

import "sort"

// GuardBound is the conservative key bound of one pending prefix/page pair.
type GuardBound struct {
	ParentIDs  []EdgeID `json:"parent_ids"`
	LowerBound Key      `json:"lower_bound"`
}

// PageBound records either the observed or directory lower-cost basis for a
// page, together with all pending guard bounds that use it.
type PageBound struct {
	Ref               PageRef      `json:"ref"`
	From              Vertex       `json:"from"`
	MinLabel          string       `json:"min_label"`
	MaxLabel          string       `json:"max_label"`
	DirectoryMinCost  int64        `json:"directory_min_cost"`
	Fetched           bool         `json:"fetched"`
	ObservedMinCost   int64        `json:"observed_min_cost"`
	HasObservedMin    bool         `json:"has_observed_min"`
	Guards            []GuardBound `json:"guards"`
	MinimumGuardBound *Key         `json:"minimum_guard_bound,omitempty"`
}

// RankCertificate is the proof basis immediately after emitting one rank and
// expanding that result's prefix.
type RankCertificate struct {
	Rank               int         `json:"rank"`
	Result             Walk        `json:"result"`
	QueueItems         int         `json:"queue_items"`
	UnreadPageGuards   int         `json:"unread_page_guards"`
	HasFrontierBound   bool        `json:"has_frontier_bound"`
	FrontierLowerBound *Key        `json:"frontier_lower_bound,omitempty"`
	Pages              []PageBound `json:"pages"`
}

// Proof is the current rank-segment basis ledger.
type Proof struct {
	CatalogVersion     CatalogVersion    `json:"catalog_version"`
	K                  int               `json:"k"`
	EmittedRank        int               `json:"emitted_rank"`
	LimitReached       bool              `json:"limit_reached"`
	QueueItems         int               `json:"queue_items"`
	FrontierLowerBound *Key              `json:"frontier_lower_bound,omitempty"`
	Certificates       []RankCertificate `json:"certificates"`
	Pages              []PageBound       `json:"pages"`
}

// Proof returns fetched/unfetched page bounds and per-rank certificates.
func (c *Cursor) Proof() Proof {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.proofLocked()
}

func (c *Cursor) proofLocked() Proof {
	certs := make([]RankCertificate, len(c.certs))
	copy(certs, c.certs)
	pages := c.pageBoundsLocked()
	p := Proof{
		CatalogVersion: c.catalogVersion,
		K:              c.k,
		EmittedRank:    len(c.emitted),
		LimitReached:   c.limitReached,
		QueueItems:     c.pq.Len(),
		Certificates:   certs,
		Pages:          pages,
	}
	if item := c.pq.peek(); item != nil {
		key := item.key
		key.IDs = append([]EdgeID(nil), key.IDs...)
		p.FrontierLowerBound = &key
	}
	return p
}

func (c *Cursor) pageBoundsLocked() []PageBound {
	out := make([]PageBound, 0, len(c.pages))
	for _, info := range c.pages {
		pb := PageBound{
			Ref:              info.Ref(),
			From:             info.From,
			MinLabel:         info.MinLabel,
			MaxLabel:         info.MaxLabel,
			DirectoryMinCost: info.MinCost,
			Guards:           []GuardBound{},
		}
		if fp, ok := c.fetched[info.ID]; ok && fp.Info.Version == info.Version {
			pb.Fetched = true
			pb.ObservedMinCost = fp.ObservedMinCost
			pb.HasObservedMin = true
		}
		var minKey *Key
		unreadGuards := 0
		for _, item := range c.pq {
			if item.kind != kindGuard || item.page == nil || item.page.ID != info.ID {
				continue
			}
			if !pb.Fetched {
				unreadGuards++
			}
			ids := append([]EdgeID(nil), item.path.walk.IDs...)
			gb := GuardBound{ParentIDs: ids, LowerBound: cloneKey(item.key)}
			pb.Guards = append(pb.Guards, gb)
			k := item.key
			if minKey == nil || CompareKey(k, *minKey) < 0 {
				kk := cloneKey(k)
				minKey = &kk
			}
		}
		sort.Slice(pb.Guards, func(i, j int) bool {
			return CompareKey(pb.Guards[i].LowerBound, pb.Guards[j].LowerBound) < 0
		})
		pb.MinimumGuardBound = minKey
		out = append(out, pb)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Ref.ID < out[j].Ref.ID })
	return out
}

func cloneKey(k Key) Key {
	k.IDs = append([]EdgeID(nil), k.IDs...)
	return k
}

func (c *Cursor) makeCertificateLocked(w Walk) RankCertificate {
	pages := c.pageBoundsLocked()
	unreadGuards := 0
	for _, pb := range pages {
		if !pb.Fetched {
			unreadGuards += len(pb.Guards)
		}
	}
	cert := RankCertificate{
		Rank:             len(c.emitted),
		Result:           cloneWalk(w),
		QueueItems:       c.pq.Len(),
		UnreadPageGuards: unreadGuards,
		Pages:            pages,
	}
	if item := c.pq.peek(); item != nil {
		k := cloneKey(item.key)
		cert.HasFrontierBound = true
		cert.FrontierLowerBound = &k
	}
	return cert
}

// IsOpenBeforeKey reports whether an unfetched page has a guard whose lower
// bound can compete with key. It is useful in tests and scheduling code.
func (p PageBound) IsOpenBeforeKey(key Key) bool {
	if p.Fetched || p.MinimumGuardBound == nil {
		return false
	}
	return CompareKey(*p.MinimumGuardBound, key) <= 0
}
