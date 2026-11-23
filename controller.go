// Package podresize implements a cluster-controller coordination loop for
// in-place vertical Pod resource adjustments (for example moving a Pod from
// 3 CPU / 5 memory to 5 CPU / 3 memory without recreating it).
//
// The protocol is a two-phase, generation-tagged hand-off between the
// controller and a node:
//
//  1. The controller accepts a versioned ResizeRequest and creates a new
//     generation. Each resource (CPU and memory) is prepared independently by
//     the node. While a generation is outstanding it reserves the larger of
//     the old and the new value for *every* resource, including a resource
//     that is already shrinking: that reservation can only be released after
//     the node confirms the switch (apply) for the same generation.
//  2. The Pod's externally visible spec is switched exactly once, and only
//     after both resources of the same generation report Ready. The node then
//     confirms the switch with an apply receipt.
//  3. A newer request may supersede a generation that has not completed yet.
//     The superseded generation is revoked; its reservations remain counted
//     against node capacity until the node confirms the revocation, so other
//     Pods on the same node cannot steal that capacity.
//  4. Every mutating transition is persisted through a Store. A restarted
//     controller reconstructs reservations and pending actions from the last
//     snapshot.
//  5. Replayed receipts carrying an old generation never advance a newer
//     request: they are reported as stale and change no state.
package podresize

import (
	"errors"
	"sync"
)

// Resource states inside a generation lifecycle.
const (
	// PhasePrepare means the node has not yet reported the resource ready.
	PhasePrepare = "Prepare"
	// PhaseReady means the node reported the resource prepared.
	PhaseReady = "Ready"
	// PhaseApplying means both resources are ready, the visible spec was
	// switched, and the node apply receipt is still outstanding.
	PhaseApplying = "Applying"
	// PhaseRevoking means the generation was superseded before completion and
	// the node revocation receipt is still outstanding.
	PhaseRevoking = "Revoking"
	// PhaseDone means the generation completed (apply confirmed).
	PhaseDone = "Done"
	// PhaseRevoked means a superseded generation was fully revoked.
	PhaseRevoked = "Revoked"
)

// Resource names used by receipts.
const (
	CPU    = "cpu"
	Memory = "memory"
)

// Action kinds returned in a View, telling the caller what may proceed next.
const (
	ActionPrepare = "Prepare" // send/await a node prepare for a resource
	ActionApply   = "Apply"   // await the node apply receipt for the switch
	ActionRevoke  = "Revoke"  // send/await a node revocation
	ActionNone    = "None"    // no outstanding work for the Pod
)

// Sentinel errors returned for caller mistakes. Replayed/old-generation
// receipts are not errors: they are reported through View.Stale instead.
var (
	ErrNodeNotFound      = errors.New("podresize: node not found")
	ErrPodNotFound       = errors.New("podresize: pod not found")
	ErrAlreadyExists     = errors.New("podresize: object already exists")
	ErrStaleVersion      = errors.New("podresize: request version is not newer than the last accepted version")
	ErrNegativeResource  = errors.New("podresize: resource quantities must be non-negative")
	ErrInsufficient      = errors.New("podresize: node has insufficient capacity for the max(old,new) reservation")
	ErrUnknownResource   = errors.New("podresize: unknown resource name")
	ErrUnknownGeneration = errors.New("podresize: receipt refers to an unknown generation")
)

// Resources is a quantity pair for CPU and memory. Quantities are treated as
// opaque integer units; only ordering and addition are required.
type Resources struct {
	CPU    int `json:"cpu"`
	Memory int `json:"memory"`
}

// ResizeRequest is a versioned adjustment request.
type ResizeRequest struct {
	PodUID  string    `json:"pod_uid"`
	Version int64     `json:"version"`
	Target  Resources `json:"target"`
}

// PrepareReceipt is a node receipt confirming one resource of a generation is
// prepared at the requested new value.
type PrepareReceipt struct {
	PodUID   string `json:"pod_uid"`
	Gen      int64  `json:"gen"`
	Resource string `json:"resource"`
}

// ApplyReceipt confirms the node switched the Pod to the generation's spec.
type ApplyReceipt struct {
	PodUID string `json:"pod_uid"`
	Gen    int64  `json:"gen"`
}

// RevokeReceipt confirms the node tore down a superseded generation.
type RevokeReceipt struct {
	PodUID string `json:"pod_uid"`
	Gen    int64  `json:"gen"`
}

// Action is a unit of work the caller may drive next.
type Action struct {
	PodUID   string `json:"pod_uid"`
	Gen      int64  `json:"gen"`
	Kind     string `json:"kind"`
	Resource string `json:"resource,omitempty"` // for Prepare actions
}

// GenView is the caller-visible state of one generation.
type GenView struct {
	Gen         int64     `json:"gen"`
	Version     int64     `json:"version"`
	Target      Resources `json:"target"`
	Reservation Resources `json:"reservation"`
	CPUPhase    string    `json:"cpu_phase"`
	MemoryPhase string    `json:"memory_phase"`
	Actions     []Action  `json:"actions,omitempty"`
}

// PodView is the caller-visible state of one Pod on a node.
type PodView struct {
	UID         string    `json:"uid"`
	NodeName    string    `json:"node_name"`
	Visible     Resources `json:"visible"`
	Used        Resources `json:"used"`
	ActiveGen   int64     `json:"active_gen"` // 0 when no outstanding generation
	Generations []GenView `json:"generations"`
	NextActions []Action  `json:"next_actions"`
	LastVersion int64     `json:"last_version"`
}

// NodeView is the caller-visible state of one node, enough to decide whether
// another Pod's upscale can be admitted.
type NodeView struct {
	Name      string    `json:"name"`
	Capacity  Resources `json:"capacity"`
	Used      Resources `json:"used"`
	Available Resources `json:"available"`
	Pods      []PodView `json:"pods"`
}

// View is a full snapshot of controller state.
type View struct {
	Nodes []NodeView `json:"nodes"`
	// Stale is populated after a receipt call when the receipt carried an old
	// or unknown-but-harmless generation; state was not advanced.
	Stale   bool   `json:"-"`
	Message string `json:"-"`
}

// genState is the persisted state of one generation.
type genState struct {
	Gen      int64     `json:"gen"`
	Version  int64     `json:"version"`
	Base     Resources `json:"base"` // visible spec when the generation was created
	Target   Resources `json:"target"`
	Reserve  Resources `json:"reserve"`
	CPUPhase string    `json:"cpu_phase"`
	MemPhase string    `json:"mem_phase"`
	Revoked  bool      `json:"revoked"`
	Applied  bool      `json:"applied"`
}

// podState is the persisted state of one Pod.
type podState struct {
	UID         string              `json:"uid"`
	NodeName    string              `json:"node_name"`
	Visible     Resources           `json:"visible"`
	LastVersion int64               `json:"last_version"`
	NextGen     int64               `json:"next_gen"`
	ActiveGen   int64               `json:"active_gen"`
	Gens        map[int64]*genState `json:"gens"`
}

type nodeState struct {
	Name     string `json:"name"`
	Capacity Resources
}

// Snapshot is the durable record written after every mutating transition.
type Snapshot struct {
	Nodes   map[string]*nodeState `json:"nodes"`
	Pods    map[string]*podState  `json:"pods"`
	PodNode map[string]string     `json:"pod_node"`
}

func maxInt(a, b int) int {
	if a > b {
		return a
	}
	return b
}

func (r Resources) add(o Resources) Resources {
	return Resources{CPU: r.CPU + o.CPU, Memory: r.Memory + o.Memory}
}

func (r Resources) sub(o Resources) Resources {
	return Resources{CPU: r.CPU - o.CPU, Memory: r.Memory - o.Memory}
}

func maxRes(a, b Resources) Resources {
	return Resources{CPU: maxInt(a.CPU, b.CPU), Memory: maxInt(a.Memory, b.Memory)}
}

func validResource(name string) bool {
	return name == CPU || name == Memory
}

func validResources(r Resources) bool {
	return r.CPU >= 0 && r.Memory >= 0
}

// Controller is the in-place resize coordinator. It is safe for concurrent
// use: every exported operation holds the controller mutex.
type Controller struct {
	mu      sync.Mutex
	store   Store
	nodes   map[string]*nodeState
	pods    map[string]*podState
	podNode map[string]string
	stale   bool
	message string
}
