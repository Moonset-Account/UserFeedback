// Package scanscope computes which entries of a directory snapshot must be
// scanned by a protection scanner, taking into account time-limited exclusion
// declarations that were approved for temporary compile/cache directories.
//
// Exclusion rules are intentionally conservative: a declaration is honored
// only while every one of its preconditions holds (still within its time
// window, anchored at the originally approved path, and still bound to the
// same directory object). When the excluded object is renamed/moved, the
// declaration may follow it only when a matching rename or sync record is
// presented; without such a record only the affected branch loses its
// exclusion eligibility. A moved exclusion is never carried into a protected
// root. When a path is reused by a new directory object, the new object is
// evaluated from scratch and cannot inherit an exclusion granted to the old
// object. A protected root, including one nested inside an excluded subtree,
// always resumes scanning.
package scanscope

import (
	"errors"
	"fmt"
	"path"
	"sort"
	"strings"
	"time"
)

// SnapshotNode is one object in a point-in-time directory snapshot.
// ObjectID is the stable identity of the directory/file object (for example an
// inode-like identifier); two distinct objects never share an ID, while the
// same object keeps its ID across renames.
type SnapshotNode struct {
	Path     string          // clean, root-anchored path such as "/workspace/src"
	ObjectID string          // stable object identity, unique within the snapshot
	IsDir    bool            // whether the node is a directory
	Children []*SnapshotNode // children of a directory node
}

// Exclusion is a time-limited exclusion statement approved for a temporary
// directory. It matches only the exact object (ObjectID) located at the exact
// approved path (ApprovedPath); it never globs or prefix-matches other paths.
type Exclusion struct {
	ID           string    // identifier of the exclusion statement itself
	ObjectID     string    // directory object the exclusion was granted to
	ApprovedPath string    // path at which the exclusion was approved
	ExpiresAt    time.Time // expiry instant; the exclusion is active strictly before it
}

// RenameRecord attests that OldObjectID at OldPath moved to NewObjectID at
// NewPath. When object identities are preserved across renames the two object
// IDs are equal.
type RenameRecord struct {
	OldObjectID string
	OldPath     string
	NewObjectID string
	NewPath     string
}

// SyncRecord attests that an object observed at SrcPath was synchronized to
// DstPath (for example a synced workspace mirror). Either side may be keyed by
// object identity or by path; the evaluator treats both as provenance evidence
// for the same directory lineage.
type SyncRecord struct {
	SrcObjectID string
	SrcPath     string
	DstObjectID string
	DstPath     string
}

// Request bundles the inputs of a scan-scope evaluation.
type Request struct {
	Snapshot       *SnapshotNode
	Exclusions     []Exclusion
	ProtectedRoots []string
	RenameRecords  []RenameRecord
	SyncRecords    []SyncRecord
	Now            time.Time // evaluation instant for time-window checks
}

// Reason explains why scanning was resumed at a node that an exclusion would
// otherwise have skipped.
type Reason string

const (
	// ReasonProtectedRoot means the node is inside (or is) a protected root;
	// protected roots always resume scanning and override every exclusion.
	ReasonProtectedRoot Reason = "protected_root"
	// ReasonExpired means the exclusion's time window has elapsed.
	ReasonExpired Reason = "exclusion_expired"
	// ReasonMovedWithoutRecord means the excluded object was renamed/moved and
	// no matching rename or sync record attests to the move.
	ReasonMovedWithoutRecord Reason = "moved_without_record"
	// ReasonPathReused means the approved path is now occupied by a different
	// object with no lineage record, so the old exclusion cannot be inherited.
	ReasonPathReused Reason = "path_reused_by_new_object"
)

// Restoration describes one place where scanning was resumed together with the
// exclusion statement that lost effect there and the triggering reason.
type Restoration struct {
	Path        string
	ObjectID    string
	ExclusionID string
	Reason      Reason
}

// Report is the evaluation result.
type Report struct {
	// ScanPaths lists every node that the scanner must visit, in lexical order.
	ScanPaths []string
	// ExcludedPaths lists the anchor directories whose exclusion is in force,
	// in lexical order. Everything below an anchor is skipped unless a nested
	// protected root resumes scanning.
	ExcludedPaths []string
	// Restorations lists the events that resumed scanning, in lexical order.
	Restorations []Restoration
}

type candidateFailure struct {
	exclusionID string
	reason      Reason
}

// evaluator holds the working state of one evaluation run.
type evaluator struct {
	req           *Request
	protected     map[string]bool
	exclByPath    map[string][]Exclusion
	exclByObject  map[string][]Exclusion
	seenObjects   map[string]bool
	scanPaths     map[string]bool
	excludedPaths map[string]bool
	restorations  []Restoration
	restoredKeys  map[string]bool
}

// Evaluate computes the scan scope for the given request.
func Evaluate(req *Request) (*Report, error) {
	if req == nil {
		return nil, errors.New("scanscope: nil request")
	}
	if req.Snapshot == nil {
		return nil, errors.New("scanscope: nil snapshot")
	}
	if req.Now.IsZero() {
		return nil, errors.New("scanscope: evaluation time must be set")
	}

	protected := make(map[string]bool)
	for _, p := range req.ProtectedRoots {
		if p == "" {
			return nil, errors.New("scanscope: empty protected root")
		}
		cp := path.Clean(p)
		if !path.IsAbs(cp) {
			return nil, fmt.Errorf("scanscope: protected root must be absolute: %q", p)
		}
		protected[cp] = true
	}

	exclByPath := make(map[string][]Exclusion)
	exclByObject := make(map[string][]Exclusion)
	for _, ex := range req.Exclusions {
		if ex.ID == "" {
			return nil, errors.New("scanscope: exclusion without id")
		}
		if ex.ObjectID == "" {
			return nil, fmt.Errorf("scanscope: exclusion %q has empty object id", ex.ID)
		}
		if ex.ApprovedPath == "" || !path.IsAbs(path.Clean(ex.ApprovedPath)) {
			return nil, fmt.Errorf("scanscope: exclusion %q has invalid approved path", ex.ID)
		}
		if ex.ExpiresAt.IsZero() {
			return nil, fmt.Errorf("scanscope: exclusion %q has zero expiry", ex.ID)
		}
		cp := path.Clean(ex.ApprovedPath)
		ex.ApprovedPath = cp
		exclByPath[cp] = append(exclByPath[cp], ex)
		exclByObject[ex.ObjectID] = append(exclByObject[ex.ObjectID], ex)
	}

	ev := &evaluator{
		req:           req,
		protected:     protected,
		exclByPath:    exclByPath,
		exclByObject:  exclByObject,
		seenObjects:   make(map[string]bool),
		scanPaths:     make(map[string]bool),
		excludedPaths: make(map[string]bool),
		restoredKeys:  make(map[string]bool),
	}

	if err := ev.walk(req.Snapshot, ""); err != nil {
		return nil, err
	}

	report := &Report{
		ScanPaths:     sortedKeys(ev.scanPaths),
		ExcludedPaths: sortedKeys(ev.excludedPaths),
		Restorations:  ev.restorations,
	}
	sort.SliceStable(report.Restorations, func(i, j int) bool {
		if report.Restorations[i].Path != report.Restorations[j].Path {
			return report.Restorations[i].Path < report.Restorations[j].Path
		}
		return report.Restorations[i].ExclusionID < report.Restorations[j].ExclusionID
	})
	return report, nil
}

// walk traverses a snapshot node. activeExclusion is the exclusion currently in
// force for the subtree (empty when the subtree is fully in scan scope).
func (ev *evaluator) walk(node *SnapshotNode, activeExclusion string) error {
	if node == nil {
		return errors.New("scanscope: snapshot contains nil node")
	}
	if node.Path == "" || !path.IsAbs(path.Clean(node.Path)) {
		return fmt.Errorf("scanscope: node has invalid path: %q", node.Path)
	}
	if node.ObjectID == "" {
		return fmt.Errorf("scanscope: node at %q has empty object id", node.Path)
	}
	if ev.seenObjects[node.ObjectID] {
		return fmt.Errorf("scanscope: duplicated object id %q in snapshot", node.ObjectID)
	}
	ev.seenObjects[node.ObjectID] = true
	nodePath := path.Clean(node.Path)

	activeID, failures := ev.evaluateNode(nodePath, node.ObjectID)

	if ev.isProtected(nodePath) {
		// Protected roots always resume scanning and override every exclusion,
		// including one carried over from an enclosing excluded subtree or one
		// that would have followed a moved object into the protected area.
		effectiveActive := activeID
		if effectiveActive == "" {
			effectiveActive = activeExclusion
		}
		if effectiveActive != "" {
			ev.recordRestoration(Restoration{
				Path:        nodePath,
				ObjectID:    node.ObjectID,
				ExclusionID: effectiveActive,
				Reason:      ReasonProtectedRoot,
			})
		}
		for _, f := range failures {
			ev.recordRestoration(Restoration{
				Path:        nodePath,
				ObjectID:    node.ObjectID,
				ExclusionID: f.exclusionID,
				Reason:      f.reason,
			})
		}
		ev.scanPaths[nodePath] = true
		for _, child := range node.Children {
			if err := ev.walk(child, ""); err != nil {
				return err
			}
		}
		return nil
	}

	if activeID != "" {
		// All preconditions hold: skip the whole subtree.
		ev.excludedPaths[nodePath] = true
		for _, child := range node.Children {
			if err := ev.walk(child, activeID); err != nil {
				return err
			}
		}
		return nil
	}

	// An exclusion carried over from an enclosing anchor stays in force for the
	// whole subtree until a nested protected root breaks it (handled above).
	if activeExclusion != "" {
		for _, child := range node.Children {
			if err := ev.walk(child, activeExclusion); err != nil {
				return err
			}
		}
		return nil
	}

	for _, f := range failures {
		ev.recordRestoration(Restoration{
			Path:        nodePath,
			ObjectID:    node.ObjectID,
			ExclusionID: f.exclusionID,
			Reason:      f.reason,
		})
	}

	// No declaration applies at this node: normal scan eligibility, regardless
	// of whether an enclosing subtree was excluded (a nested protected root
	// already reset the carried exclusion above).
	ev.scanPaths[nodePath] = true
	for _, child := range node.Children {
		if err := ev.walk(child, ""); err != nil {
			return err
		}
	}
	return nil
}

// evaluateNode inspects every declaration that could possibly concern the
// object at nodePath. It returns the ID of the first declaration that is fully
// in force, and the failure of every declaration that cannot apply. A carried
// exclusion from an enclosing subtree is handled by the caller.
func (ev *evaluator) evaluateNode(nodePath, objectID string) (string, []candidateFailure) {
	candidates := ev.collectCandidates(nodePath, objectID)
	var failures []candidateFailure
	for _, ex := range candidates {
		if !ev.req.Now.Before(ex.ExpiresAt) {
			failures = append(failures, candidateFailure{ex.ID, ReasonExpired})
			continue
		}
		if ex.ApprovedPath == nodePath {
			if ex.ObjectID == objectID {
				return ex.ID, nil
			}
			// A different object occupies the approved path. The declaration
			// transfers only if provenance records link the two lineages.
			if ev.hasProvenance(ex.ObjectID, ex.ApprovedPath, objectID, nodePath) {
				return ex.ID, nil
			}
			failures = append(failures, candidateFailure{ex.ID, ReasonPathReused})
			continue
		}
		// The object moved away from its approved path: the declaration follows
		// the object only with a matching rename/sync record.
		if ev.hasProvenance(ex.ObjectID, ex.ApprovedPath, objectID, nodePath) {
			return ex.ID, nil
		}
		failures = append(failures, candidateFailure{ex.ID, ReasonMovedWithoutRecord})
	}
	return "", failures
}

// collectCandidates gathers declarations anchored at the node's path or bound
// to the node's object, de-duplicated by exclusion ID.
func (ev *evaluator) collectCandidates(nodePath, objectID string) []Exclusion {
	seen := make(map[string]bool)
	var out []Exclusion
	for _, ex := range ev.exclByPath[nodePath] {
		if !seen[ex.ID] {
			seen[ex.ID] = true
			out = append(out, ex)
		}
	}
	for _, ex := range ev.exclByObject[objectID] {
		if !seen[ex.ID] {
			seen[ex.ID] = true
			out = append(out, ex)
		}
	}
	return out
}

// hasProvenance reports whether a rename or sync record connects the originally
// approved object/path to the object/path currently observed. Records bind on
// either object identity or approved path, matching either direction.
func (ev *evaluator) hasProvenance(oldObjectID, oldPath, newObjectID, newPath string) bool {
	for _, r := range ev.req.RenameRecords {
		if r.NewObjectID == newObjectID && r.NewPath == newPath {
			if r.OldObjectID == oldObjectID || r.OldPath == oldPath {
				return true
			}
		}
		if r.OldObjectID == newObjectID && r.OldPath == newPath {
			if r.NewObjectID == oldObjectID || r.NewPath == oldPath {
				return true
			}
		}
	}
	for _, s := range ev.req.SyncRecords {
		idMatch := (s.SrcObjectID == oldObjectID && s.DstObjectID == newObjectID) ||
			(s.DstObjectID == oldObjectID && s.SrcObjectID == newObjectID)
		pathMatch := (s.SrcPath == oldPath && s.DstPath == newPath) ||
			(s.DstPath == oldPath && s.SrcPath == newPath)
		if idMatch || pathMatch {
			return true
		}
	}
	return false
}

// isProtected reports whether nodePath equals a protected root or lies inside
// one. Matching is component-wise so "/protect" never matches "/protected-x".
func (ev *evaluator) isProtected(nodePath string) bool {
	for root := range ev.protected {
		if nodePath == root || strings.HasPrefix(nodePath, root+"/") {
			return true
		}
	}
	return false
}

func (ev *evaluator) recordRestoration(r Restoration) {
	key := r.Path + "\x00" + r.ExclusionID
	if ev.restoredKeys[key] {
		return
	}
	ev.restoredKeys[key] = true
	ev.restorations = append(ev.restorations, r)
}

func sortedKeys(m map[string]bool) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}
