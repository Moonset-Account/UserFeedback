package scanscope

import (
	"testing"
	"time"
)

// ---- synthetic snapshot builders ------------------------------------------------

func dir(p, id string, children ...*SnapshotNode) *SnapshotNode {
	return &SnapshotNode{Path: p, ObjectID: id, IsDir: true, Children: children}
}

func file(p, id string) *SnapshotNode {
	return &SnapshotNode{Path: p, ObjectID: id, IsDir: false}
}

func baseTime() time.Time {
	return time.Date(2026, 11, 19, 10, 0, 0, 0, time.UTC)
}

func assertSet(t *testing.T, got, want []string, label string) {
	t.Helper()
	g := map[string]bool{}
	for _, v := range got {
		g[v] = true
	}
	w := map[string]bool{}
	for _, v := range want {
		w[v] = true
	}
	for k := range w {
		if !g[k] {
			t.Errorf("%s: missing expected entry %q; got %v", label, k, got)
		}
	}
	for k := range g {
		if !w[k] {
			t.Errorf("%s: unexpected entry %q; got %v", label, k, got)
		}
	}
}

func restorationIndex(rep *Report) map[string]Restoration {
	idx := map[string]Restoration{}
	for _, r := range rep.Restorations {
		idx[r.Path+"|"+r.ExclusionID] = r
	}
	return idx
}

func assertRestoration(t *testing.T, rep *Report, p, exID string, reason Reason) {
	t.Helper()
	r, ok := restorationIndex(rep)[p+"|"+exID]
	if !ok {
		t.Fatalf("expected restoration for path %q exclusion %q, got %+v", p, exID, rep.Restorations)
	}
	if r.Reason != reason {
		t.Errorf("restoration %q/%q reason = %q, want %q", p, exID, r.Reason, reason)
	}
	if r.ObjectID == "" {
		t.Errorf("restoration %q/%q carries empty object id", p, exID)
	}
}

func assertNoRestoration(t *testing.T, rep *Report, exID string) {
	t.Helper()
	for _, r := range rep.Restorations {
		if r.ExclusionID == exID {
			t.Fatalf("exclusion %q unexpectedly produced restoration: %+v", exID, r)
		}
	}
}

// ---- scenario 1: acceptable result — a valid exclusion is honored ----------------

func TestValidExclusionIsHonored(t *testing.T) {
	now := baseTime()
	snap := dir("/", "obj-root",
		dir("/tmp", "obj-tmp",
			dir("/tmp/build", "obj-build",
				file("/tmp/build/a.o", "obj-a"),
				dir("/tmp/build/sub", "obj-sub",
					file("/tmp/build/sub/b.o", "obj-b"),
				),
			),
			file("/tmp/keep.go", "obj-keep"),
		),
		dir("/src", "obj-src",
			file("/src/main.go", "obj-main"),
		),
	)
	req := &Request{
		Snapshot: snap,
		Exclusions: []Exclusion{
			{ID: "EX-1", ObjectID: "obj-build", ApprovedPath: "/tmp/build", ExpiresAt: now.Add(time.Hour)},
		},
		ProtectedRoots: []string{"/src"},
		Now:            now,
	}

	rep, err := Evaluate(req)
	if err != nil {
		t.Fatalf("Evaluate returned error: %v", err)
	}

	// The anchor and everything below it are out of scan scope.
	assertSet(t, rep.ExcludedPaths, []string{"/tmp/build"}, "excluded paths")
	assertSet(t, rep.ScanPaths, []string{
		"/", "/tmp", "/tmp/keep.go", "/src", "/src/main.go",
	}, "scan paths")
	if len(rep.Restorations) != 0 {
		t.Fatalf("expected no restorations, got %+v", rep.Restorations)
	}
}

// ---- scenario 2: blocked result — unrecorded move revokes only affected branch ----

func TestMovedWithoutRecordRevokesOnlyAffectedBranch(t *testing.T) {
	now := baseTime()
	// obj-build moved from /tmp/build to /var/build with no rename record;
	// a brand-new object (obj-newbuild) now occupies /tmp/build.
	snap := dir("/", "obj-root",
		dir("/tmp", "obj-tmp",
			dir("/tmp/build", "obj-newbuild",
				file("/tmp/build/fresh.o", "obj-fresh"),
			),
			dir("/tmp/other", "obj-other",
				dir("/tmp/other/cache", "obj-cache",
					file("/tmp/other/cache/x.tmp", "obj-x"),
				),
			),
		),
		dir("/var", "obj-var",
			dir("/var/build", "obj-build",
				file("/var/build/a.o", "obj-a"),
			),
		),
	)
	req := &Request{
		Snapshot: snap,
		Exclusions: []Exclusion{
			{ID: "EX-MOVED", ObjectID: "obj-build", ApprovedPath: "/tmp/build", ExpiresAt: now.Add(time.Hour)},
			{ID: "EX-OTHER", ObjectID: "obj-cache", ApprovedPath: "/tmp/other/cache", ExpiresAt: now.Add(time.Hour)},
		},
		Now: now,
	}

	rep, err := Evaluate(req)
	if err != nil {
		t.Fatalf("Evaluate returned error: %v", err)
	}

	// Both affected branches are scanned, including their contents.
	assertSet(t, rep.ScanPaths, []string{
		"/", "/tmp", "/tmp/build", "/tmp/build/fresh.o",
		"/tmp/other",
		"/var", "/var/build", "/var/build/a.o",
	}, "scan paths")
	// The unrelated exclusion on the other branch stays in force.
	assertSet(t, rep.ExcludedPaths, []string{"/tmp/other/cache"}, "excluded paths")

	assertRestoration(t, rep, "/tmp/build", "EX-MOVED", ReasonPathReused)
	assertRestoration(t, rep, "/var/build", "EX-MOVED", ReasonMovedWithoutRecord)
	assertNoRestoration(t, rep, "EX-OTHER")
}

// ---- scenario 3a: rename record lets the exclusion follow the object ----------------

func TestRenameRecordLetsExclusionFollowObject(t *testing.T) {
	now := baseTime()
	snap := dir("/", "obj-root",
		dir("/tmp", "obj-tmp",
			dir("/tmp/build", "obj-newbuild",
				file("/tmp/build/fresh.o", "obj-fresh"),
			),
		),
		dir("/var", "obj-var",
			dir("/var/build", "obj-build",
				file("/var/build/a.o", "obj-a"),
			),
		),
	)
	req := &Request{
		Snapshot: snap,
		Exclusions: []Exclusion{
			{ID: "EX-1", ObjectID: "obj-build", ApprovedPath: "/tmp/build", ExpiresAt: now.Add(time.Hour)},
		},
		RenameRecords: []RenameRecord{
			{OldObjectID: "obj-build", OldPath: "/tmp/build", NewObjectID: "obj-build", NewPath: "/var/build"},
		},
		Now: now,
	}

	rep, err := Evaluate(req)
	if err != nil {
		t.Fatalf("Evaluate returned error: %v", err)
	}

	// The recorded move carries the exclusion to the new location ...
	assertSet(t, rep.ExcludedPaths, []string{"/var/build"}, "excluded paths")
	assertSet(t, rep.ScanPaths, []string{
		"/", "/tmp", "/tmp/build", "/tmp/build/fresh.o", "/var",
	}, "scan paths")
	// ... while the new object reusing the old path is evaluated fresh and scanned.
	assertRestoration(t, rep, "/tmp/build", "EX-1", ReasonPathReused)
}

// ---- scenario 3b: a moved exclusion must never enter a protected root ----------------

func TestMovedExclusionCannotEnterProtectedRoot(t *testing.T) {
	now := baseTime()
	snap := dir("/", "obj-root",
		dir("/protected", "obj-prot",
			dir("/protected/build", "obj-build",
				file("/protected/build/a.o", "obj-a"),
			),
		),
	)
	req := &Request{
		Snapshot: snap,
		Exclusions: []Exclusion{
			{ID: "EX-1", ObjectID: "obj-build", ApprovedPath: "/tmp/build", ExpiresAt: now.Add(time.Hour)},
		},
		RenameRecords: []RenameRecord{
			{OldObjectID: "obj-build", OldPath: "/tmp/build", NewObjectID: "obj-build", NewPath: "/protected/build"},
		},
		ProtectedRoots: []string{"/protected"},
		Now:            now,
	}

	rep, err := Evaluate(req)
	if err != nil {
		t.Fatalf("Evaluate returned error: %v", err)
	}

	assertSet(t, rep.ScanPaths, []string{
		"/", "/protected", "/protected/build", "/protected/build/a.o",
	}, "scan paths")
	if len(rep.ExcludedPaths) != 0 {
		t.Fatalf("no exclusion may survive inside a protected root, got %v", rep.ExcludedPaths)
	}
	assertRestoration(t, rep, "/protected/build", "EX-1", ReasonProtectedRoot)
}

// ---- scenario 4: nested protected root inside an excluded subtree resumes scanning ----

func TestNestedProtectedRootResumesScanning(t *testing.T) {
	now := baseTime()
	snap := dir("/", "obj-root",
		dir("/ws", "obj-ws",
			dir("/ws/tmp", "obj-tmp",
				dir("/ws/tmp/cache", "obj-cache",
					file("/ws/tmp/cache/secret", "obj-secret"),
				),
			),
		),
	)
	req := &Request{
		Snapshot: snap,
		Exclusions: []Exclusion{
			{ID: "EX-1", ObjectID: "obj-cache", ApprovedPath: "/ws/tmp/cache", ExpiresAt: now.Add(time.Hour)},
		},
		ProtectedRoots: []string{"/ws/tmp/cache"},
		Now:            now,
	}

	rep, err := Evaluate(req)
	if err != nil {
		t.Fatalf("Evaluate returned error: %v", err)
	}

	assertSet(t, rep.ScanPaths, []string{
		"/", "/ws", "/ws/tmp", "/ws/tmp/cache", "/ws/tmp/cache/secret",
	}, "scan paths")
	if len(rep.ExcludedPaths) != 0 {
		t.Fatalf("nested protected root must override exclusion, got %v", rep.ExcludedPaths)
	}
	assertRestoration(t, rep, "/ws/tmp/cache", "EX-1", ReasonProtectedRoot)
}

// ---- scenario 5: boundary — expiry instant is exclusive --------------------------------

func TestExpiryBoundary(t *testing.T) {
	expiry := baseTime().Add(time.Hour)
	build := func() *Request {
		return &Request{
			Snapshot: dir("/", "obj-root",
				dir("/tmp", "obj-tmp",
					dir("/tmp/build", "obj-build",
						file("/tmp/build/a.o", "obj-a"),
					),
				),
			),
			Exclusions: []Exclusion{
				{ID: "EX-1", ObjectID: "obj-build", ApprovedPath: "/tmp/build", ExpiresAt: expiry},
			},
			Now: baseTime(),
		}
	}

	t.Run("one instant before expiry stays excluded", func(t *testing.T) {
		req := build()
		req.Now = expiry.Add(-time.Nanosecond)
		rep, err := Evaluate(req)
		if err != nil {
			t.Fatalf("Evaluate returned error: %v", err)
		}
		assertSet(t, rep.ExcludedPaths, []string{"/tmp/build"}, "excluded paths")
		assertSet(t, rep.ScanPaths, []string{"/", "/tmp"}, "scan paths")
	})

	t.Run("at expiry the exclusion is revoked", func(t *testing.T) {
		req := build()
		req.Now = expiry
		rep, err := Evaluate(req)
		if err != nil {
			t.Fatalf("Evaluate returned error: %v", err)
		}
		assertSet(t, rep.ScanPaths, []string{"/", "/tmp", "/tmp/build", "/tmp/build/a.o"}, "scan paths")
		if len(rep.ExcludedPaths) != 0 {
			t.Fatalf("expired exclusion must not exclude, got %v", rep.ExcludedPaths)
		}
		assertRestoration(t, rep, "/tmp/build", "EX-1", ReasonExpired)
	})
}

// ---- scenario 6: boundary — protected roots match path components, not prefixes ---------

func TestProtectedRootComponentBoundary(t *testing.T) {
	now := baseTime()
	snap := dir("/", "obj-root",
		dir("/protect", "obj-protect",
			file("/protect/deep.txt", "obj-deep"),
		),
		dir("/protected-x", "obj-otherroot",
			file("/protected-x/data", "obj-data"),
		),
	)
	req := &Request{
		Snapshot:       snap,
		Exclusions:     []Exclusion{{ID: "EX-1", ObjectID: "obj-otherroot", ApprovedPath: "/protected-x", ExpiresAt: now.Add(time.Hour)}},
		ProtectedRoots: []string{"/protect"},
		Now:            now,
	}

	rep, err := Evaluate(req)
	if err != nil {
		t.Fatalf("Evaluate returned error: %v", err)
	}

	// /protect and its contents are scanned; the similarly-prefixed sibling is not
	// treated as protected and its valid exclusion still applies.
	assertSet(t, rep.ScanPaths, []string{"/", "/protect", "/protect/deep.txt"}, "scan paths")
	assertSet(t, rep.ExcludedPaths, []string{"/protected-x"}, "excluded paths")
}

// ---- scenario 7: sync record provides provenance for a synced object copy ----------------

func TestSyncRecordProvenance(t *testing.T) {
	now := baseTime()
	snap := dir("/", "obj-root",
		dir("/mirror", "obj-mirror",
			dir("/mirror/cache", "obj-cache",
				file("/mirror/cache/z.bin", "obj-z"),
			),
		),
	)
	req := &Request{
		Snapshot: snap,
		Exclusions: []Exclusion{
			{ID: "EX-1", ObjectID: "obj-cache", ApprovedPath: "/origin/cache", ExpiresAt: now.Add(time.Hour)},
		},
		SyncRecords: []SyncRecord{
			{SrcObjectID: "obj-cache", SrcPath: "/origin/cache", DstObjectID: "obj-cache", DstPath: "/mirror/cache"},
		},
		Now: now,
	}

	rep, err := Evaluate(req)
	if err != nil {
		t.Fatalf("Evaluate returned error: %v", err)
	}
	assertSet(t, rep.ExcludedPaths, []string{"/mirror/cache"}, "excluded paths")
	assertSet(t, rep.ScanPaths, []string{"/", "/mirror"}, "scan paths")
	if len(rep.Restorations) != 0 {
		t.Fatalf("sync provenance should keep the exclusion valid, got %+v", rep.Restorations)
	}
}

// ---- scenario 8: interleaved declarations — valid one wins over an expired one ------------

func TestMultipleDeclarationsValidWinsOverExpired(t *testing.T) {
	now := baseTime()
	snap := dir("/", "obj-root",
		dir("/tmp", "obj-tmp",
			dir("/tmp/build", "obj-build",
				file("/tmp/build/a.o", "obj-a"),
			),
		),
	)
	req := &Request{
		Snapshot: snap,
		Exclusions: []Exclusion{
			{ID: "EX-OLD", ObjectID: "obj-build", ApprovedPath: "/tmp/build", ExpiresAt: now.Add(-time.Minute)},
			{ID: "EX-NEW", ObjectID: "obj-build", ApprovedPath: "/tmp/build", ExpiresAt: now.Add(time.Hour)},
		},
		Now: now,
	}

	rep, err := Evaluate(req)
	if err != nil {
		t.Fatalf("Evaluate returned error: %v", err)
	}
	assertSet(t, rep.ExcludedPaths, []string{"/tmp/build"}, "excluded paths")
	assertNoRestoration(t, rep, "EX-NEW")
	assertNoRestoration(t, rep, "EX-OLD")
}

// ---- scenario 9: input validation rejects malformed requests ------------------------------

func TestValidationRejectsMalformedInput(t *testing.T) {
	now := baseTime()
	goodSnap := dir("/", "obj-root")
	goodEx := Exclusion{ID: "EX-1", ObjectID: "obj-x", ApprovedPath: "/tmp/x", ExpiresAt: now.Add(time.Hour)}

	cases := []struct {
		name string
		req  *Request
	}{
		{"nil request", nil},
		{"nil snapshot", &Request{Now: now, Exclusions: []Exclusion{goodEx}}},
		{"zero evaluation time", &Request{Snapshot: goodSnap, Exclusions: []Exclusion{goodEx}}},
		{"relative protected root", &Request{Snapshot: goodSnap, ProtectedRoots: []string{"rel/path"}, Now: now}},
		{"empty protected root", &Request{Snapshot: goodSnap, ProtectedRoots: []string{""}, Now: now}},
		{"exclusion without object id", &Request{
			Snapshot:   goodSnap,
			Now:        now,
			Exclusions: []Exclusion{{ID: "EX-2", ApprovedPath: "/tmp/x", ExpiresAt: now.Add(time.Hour)}},
		}},
		{"exclusion with relative path", &Request{
			Snapshot:   goodSnap,
			Now:        now,
			Exclusions: []Exclusion{{ID: "EX-3", ObjectID: "obj-x", ApprovedPath: "tmp/x", ExpiresAt: now.Add(time.Hour)}},
		}},
		{"exclusion with zero expiry", &Request{
			Snapshot:   goodSnap,
			Now:        now,
			Exclusions: []Exclusion{{ID: "EX-4", ObjectID: "obj-x", ApprovedPath: "/tmp/x"}},
		}},
		{"duplicated object id in snapshot", &Request{
			Snapshot: dir("/", "dup", dir("/a", "dup")),
			Now:      now,
		}},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if _, err := Evaluate(tc.req); err == nil {
				t.Fatalf("expected error for %s, got nil", tc.name)
			}
		})
	}
}
