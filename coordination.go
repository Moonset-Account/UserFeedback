package podresize

import "sort"

// NewController creates a controller and restores any previously persisted
// reservations and pending actions from store. A nil snapshot (fresh store)
// yields an empty controller.
func NewController(store Store) (*Controller, error) {
	c := &Controller{
		store:   store,
		nodes:   map[string]*nodeState{},
		pods:    map[string]*podState{},
		podNode: map[string]string{},
	}
	snap, err := store.Load()
	if err != nil {
		return nil, err
	}
	if snap != nil {
		c.nodes = snap.Nodes
		c.pods = snap.Pods
		c.podNode = snap.PodNode
		if c.nodes == nil {
			c.nodes = map[string]*nodeState{}
		}
		if c.pods == nil {
			c.pods = map[string]*podState{}
		}
		if c.podNode == nil {
			c.podNode = map[string]string{}
		}
		for _, p := range c.pods {
			if p.Gens == nil {
				p.Gens = map[int64]*genState{}
			}
		}
	}
	return c, nil
}

// AddNode registers a node with its capacity.
func (c *Controller) AddNode(name string, capacity Resources) error {
	if !validResources(capacity) {
		return ErrNegativeResource
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if _, ok := c.nodes[name]; ok {
		return ErrAlreadyExists
	}
	c.nodes[name] = &nodeState{Name: name, Capacity: capacity}
	return c.persist()
}

// AddPod registers an existing Pod on a node with its current visible spec.
func (c *Controller) AddPod(uid, nodeName string, spec Resources) error {
	if !validResources(spec) {
		return ErrNegativeResource
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if _, ok := c.pods[uid]; ok {
		return ErrAlreadyExists
	}
	node, ok := c.nodes[nodeName]
	if !ok {
		return ErrNodeNotFound
	}
	used := c.nodeUsedLocked(nodeName)
	free := node.Capacity.sub(used)
	if spec.CPU > free.CPU || spec.Memory > free.Memory {
		return ErrInsufficient
	}
	c.pods[uid] = &podState{
		UID:      uid,
		NodeName: nodeName,
		Visible:  spec,
		NextGen:  1,
		Gens:     map[int64]*genState{},
	}
	c.podNode[uid] = nodeName
	return c.persist()
}

// Submit accepts a versioned resize request. The request version must be
// strictly greater than the last accepted version for the Pod.
//
// Acceptance reserves max(visible, target) on both resources for the new
// generation against node capacity. If an unfinished generation exists it is
// superseded: its node-side work is revoked, but its reservations stay on the
// node's books until a revocation receipt arrives.
func (c *Controller) Submit(req ResizeRequest) (View, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.stale = false
	c.message = ""

	pod, ok := c.pods[req.PodUID]
	if !ok {
		return View{}, ErrPodNotFound
	}
	node, ok := c.nodes[pod.NodeName]
	if !ok {
		return View{}, ErrNodeNotFound
	}
	if !validResources(req.Target) {
		return View{}, ErrNegativeResource
	}
	if req.Version <= pod.LastVersion {
		return View{}, ErrStaleVersion
	}

	reserve := maxRes(pod.Visible, req.Target)
	// Node-wide headroom must cover the new reservation in addition to every
	// reservation already on the books (including generations being revoked).
	projected := c.nodeUsedLocked(node.Name).add(reserve).sub(podOccupancyLocked(pod))
	if projected.CPU > node.Capacity.CPU || projected.Memory > node.Capacity.Memory {
		return View{}, ErrInsufficient
	}

	// Supersede the unfinished generation, if any. Its reservations remain
	// until the node confirms the revocation. A generation that already passed
	// the switch barrier (Applying) cannot be revoked: its visible spec is in
	// effect and only the outstanding apply receipt can close it out.
	if pod.ActiveGen != 0 {
		if old, ok := pod.Gens[pod.ActiveGen]; ok && !old.Applied {
			if old.CPUPhase != PhaseApplying && old.MemPhase != PhaseApplying {
				old.Revoked = true
				if old.CPUPhase != PhaseReady {
					old.CPUPhase = PhaseRevoking
				}
				if old.MemPhase != PhaseReady {
					old.MemPhase = PhaseRevoking
				}
				pod.ActiveGen = 0
			}
		} else {
			pod.ActiveGen = 0
		}
	}

	gen := pod.NextGen
	pod.NextGen++
	pod.LastVersion = req.Version
	pod.ActiveGen = gen
	pod.Gens[gen] = &genState{
		Gen:      gen,
		Version:  req.Version,
		Base:     pod.Visible,
		Target:   req.Target,
		Reserve:  reserve,
		CPUPhase: PhasePrepare,
		MemPhase: PhasePrepare,
	}
	if err := c.persist(); err != nil {
		return View{}, err
	}
	return c.viewLocked(), nil
}

// PrepareAck handles a node receipt confirming one resource of a generation
// is prepared. A receipt for an old/replaced generation is stale and never
// advances the current request. When both resources of the same generation are
// ready, the Pod's externally visible spec switches exactly once and the
// generation moves to the applying state.
func (c *Controller) PrepareAck(rcpt PrepareReceipt) (View, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.stale = false
	c.message = ""

	pod, ok := c.pods[rcpt.PodUID]
	if !ok {
		return View{}, ErrPodNotFound
	}
	if !validResource(rcpt.Resource) {
		return View{}, ErrUnknownResource
	}
	g, ok := pod.Gens[rcpt.Gen]
	if !ok {
		c.markStale("prepare receipt for unknown generation %d", rcpt.Gen)
		return c.viewLocked(), nil
	}
	if g.Applied || g.CPUPhase == PhaseRevoked || g.MemPhase == PhaseRevoked {
		c.markStale("prepare receipt for finished/revoked generation %d", rcpt.Gen)
		return c.viewLocked(), nil
	}
	if g.Revoked {
		// The node may still answer a prepare for a generation we already
		// revoked: ignore it, the revocation is the authoritative path.
		c.markStale("prepare receipt for superseded generation %d", rcpt.Gen)
		return c.viewLocked(), nil
	}
	if rcpt.Gen != pod.ActiveGen {
		c.markStale("prepare receipt for non-active generation %d", rcpt.Gen)
		return c.viewLocked(), nil
	}

	switch rcpt.Resource {
	case CPU:
		if g.CPUPhase == PhaseReady {
			c.markStale("duplicate prepare receipt for cpu, generation %d", rcpt.Gen)
			return c.viewLocked(), nil
		}
		g.CPUPhase = PhaseReady
	case Memory:
		if g.MemPhase == PhaseReady {
			c.markStale("duplicate prepare receipt for memory, generation %d", rcpt.Gen)
			return c.viewLocked(), nil
		}
		g.MemPhase = PhaseReady
	}

	// Same-generation barrier: the visible spec switches only once both
	// resources are ready, in a single atomic step.
	if g.CPUPhase == PhaseReady && g.MemPhase == PhaseReady {
		pod.Visible = g.Target
		g.CPUPhase = PhaseApplying
		g.MemPhase = PhaseApplying
	}
	if err := c.persist(); err != nil {
		return View{}, err
	}
	return c.viewLocked(), nil
}

// ApplyAck handles the node confirmation that the Pod now runs the switched
// spec. The completed generation is pruned and its reservation is released;
// reservations of any revoked generations are untouched.
func (c *Controller) ApplyAck(rcpt ApplyReceipt) (View, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.stale = false
	c.message = ""

	pod, ok := c.pods[rcpt.PodUID]
	if !ok {
		return View{}, ErrPodNotFound
	}
	g, ok := pod.Gens[rcpt.Gen]
	if !ok {
		c.markStale("apply receipt for unknown generation %d", rcpt.Gen)
		return c.viewLocked(), nil
	}
	if g.Applied {
		c.markStale("replayed apply receipt for generation %d", rcpt.Gen)
		return c.viewLocked(), nil
	}
	if g.Revoked || g.CPUPhase != PhaseApplying || g.MemPhase != PhaseApplying {
		// An apply for a generation that never reached the switch barrier
		// cannot advance anything.
		c.markStale("apply receipt for generation %d that never switched", rcpt.Gen)
		return c.viewLocked(), nil
	}
	// Note: an Applying generation may have been demoted by a newer request, so
	// it need not be the active generation. Its spec is already visible, hence
	// its apply receipt remains valid and must still release its reservation.

	g.Applied = true
	g.CPUPhase = PhaseDone
	g.MemPhase = PhaseDone
	if pod.ActiveGen == g.Gen {
		pod.ActiveGen = 0
	}
	delete(pod.Gens, g.Gen)
	if err := c.persist(); err != nil {
		return View{}, err
	}
	return c.viewLocked(), nil
}

// RevokeAck handles the node confirmation that a superseded generation was
// torn down. Only then are its reservations released back to node capacity.
func (c *Controller) RevokeAck(rcpt RevokeReceipt) (View, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.stale = false
	c.message = ""

	pod, ok := c.pods[rcpt.PodUID]
	if !ok {
		return View{}, ErrPodNotFound
	}
	g, ok := pod.Gens[rcpt.Gen]
	if !ok {
		c.markStale("revoke receipt for unknown/already-revoked generation %d", rcpt.Gen)
		return c.viewLocked(), nil
	}
	if !g.Revoked {
		c.markStale("revoke receipt for generation %d that was not superseded", rcpt.Gen)
		return c.viewLocked(), nil
	}

	g.Revoked = false
	g.CPUPhase = PhaseRevoked
	g.MemPhase = PhaseRevoked
	delete(pod.Gens, g.Gen)
	if err := c.persist(); err != nil {
		return View{}, err
	}
	return c.viewLocked(), nil
}

// View returns the caller-visible state: node usage, each Pod's visible spec
// and effective usage, and every action that can still be driven.
func (c *Controller) View() View {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.stale = false
	c.message = ""
	return c.viewLocked()
}

func (c *Controller) markStale(format string, args ...interface{}) {
	c.stale = true
	c.message = sprintf(format, args...)
}

func (c *Controller) persist() error {
	snap := &Snapshot{
		Nodes:   c.nodes,
		Pods:    c.pods,
		PodNode: c.podNode,
	}
	return c.store.Save(snap)
}

// podOccupancyLocked is the capacity this Pod effectively occupies on the
// node at any moment: its visible spec, raised to the reservation of every
// outstanding generation (active or awaiting revocation). A single Pod never
// double-counts its own generations; the maximum, not the sum, is occupied.
func podOccupancyLocked(pod *podState) Resources {
	occ := pod.Visible
	for _, g := range pod.Gens {
		occ = maxRes(occ, g.Reserve)
	}
	return occ
}

func (c *Controller) nodeUsedLocked(nodeName string) Resources {
	var used Resources
	for _, p := range c.pods {
		if p.NodeName != nodeName {
			continue
		}
		used = used.add(podOccupancyLocked(p))
	}
	return used
}

func (c *Controller) viewLocked() View {
	nodeNames := make([]string, 0, len(c.nodes))
	for name := range c.nodes {
		nodeNames = append(nodeNames, name)
	}
	sort.Strings(nodeNames)

	view := View{Stale: c.stale, Message: c.message}
	for _, name := range nodeNames {
		node := c.nodes[name]
		used := c.nodeUsedLocked(name)
		nv := NodeView{
			Name:      name,
			Capacity:  node.Capacity,
			Used:      used,
			Available: node.Capacity.sub(used),
		}

		podUIDs := make([]string, 0)
		for uid, p := range c.pods {
			if p.NodeName == name {
				podUIDs = append(podUIDs, uid)
			}
		}
		sort.Strings(podUIDs)
		for _, uid := range podUIDs {
			nv.Pods = append(nv.Pods, c.podViewLocked(c.pods[uid]))
		}
		view.Nodes = append(view.Nodes, nv)
	}
	return view
}

func (c *Controller) podViewLocked(pod *podState) PodView {
	pv := PodView{
		UID:         pod.UID,
		NodeName:    pod.NodeName,
		Visible:     pod.Visible,
		Used:        podOccupancyLocked(pod),
		ActiveGen:   pod.ActiveGen,
		LastVersion: pod.LastVersion,
	}
	gens := make([]int64, 0, len(pod.Gens))
	for gen := range pod.Gens {
		gens = append(gens, gen)
	}
	sort.Slice(gens, func(i, j int) bool { return gens[i] < gens[j] })
	for _, gen := range gens {
		g := pod.Gens[gen]
		gv := GenView{
			Gen:         g.Gen,
			Version:     g.Version,
			Target:      g.Target,
			Reservation: g.Reserve,
			CPUPhase:    g.CPUPhase,
			MemoryPhase: g.MemPhase,
			Actions:     genActionsLocked(pod, g),
		}
		pv.Generations = append(pv.Generations, gv)
		pv.NextActions = append(pv.NextActions, gv.Actions...)
	}
	if len(pv.NextActions) == 0 {
		pv.NextActions = []Action{{PodUID: pod.UID, Kind: ActionNone}}
	}
	return pv
}

// genActionsLocked tells the caller which node-side actions remain for a
// generation. Revocation work is reported even for superseded generations so
// the caller knows which receipts must still be collected.
func genActionsLocked(pod *podState, g *genState) []Action {
	var actions []Action
	if g.Revoked {
		actions = append(actions, Action{PodUID: pod.UID, Gen: g.Gen, Kind: ActionRevoke})
		return actions
	}
	if g.CPUPhase == PhasePrepare {
		actions = append(actions, Action{PodUID: pod.UID, Gen: g.Gen, Kind: ActionPrepare, Resource: CPU})
	}
	if g.MemPhase == PhasePrepare {
		actions = append(actions, Action{PodUID: pod.UID, Gen: g.Gen, Kind: ActionPrepare, Resource: Memory})
	}
	if g.CPUPhase == PhaseApplying && g.MemPhase == PhaseApplying {
		actions = append(actions, Action{PodUID: pod.UID, Gen: g.Gen, Kind: ActionApply})
	}
	if len(actions) == 0 {
		actions = append(actions, Action{PodUID: pod.UID, Gen: g.Gen, Kind: ActionNone})
	}
	return actions
}
