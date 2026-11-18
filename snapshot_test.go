package hibersnapshot_test

import (
	"bytes"
	"errors"
	"testing"

	hibersnapshot "hibersnapshot"
)

const (
	pidA = 1001
	pidB = 1002
	va1  = uint64(0x400000)
	va2  = uint64(0x500000)
)

func pageContent(name string) []byte {
	return bytes.Repeat([]byte(name), 16)
}

func mustBuild(t *testing.T, s *hibersnapshot.SampleStore, opts ...hibersnapshot.BuildOption) *hibersnapshot.Image {
	t.Helper()
	im, err := s.Build(opts...)
	if err != nil {
		t.Fatalf("Build returned unexpected error: %v", err)
	}
	return im
}

func findEntry(t *testing.T, im *hibersnapshot.Image, pfn uint64) hibersnapshot.Entry {
	t.Helper()
	for _, e := range im.Entries {
		if e.PFN == pfn {
			return e
		}
	}
	t.Fatalf("image has no entry for pfn=%d", pfn)
	return hibersnapshot.Entry{}
}

func recoveryRefs(im *hibersnapshot.Image, pfn uint64) []hibersnapshot.RecoveryEntry {
	var out []hibersnapshot.RecoveryEntry
	for _, r := range im.Recovery {
		if r.PFN == pfn {
			out = append(out, r)
		}
	}
	return out
}

// TestBuildAcceptableSharedExport is the acceptable-result case: a normal
// shared page is exported with its real content and needs no rebuild, while a
// second private page is exported independently.
func TestBuildAcceptableSharedExport(t *testing.T) {
	s := hibersnapshot.NewSampleStore()
	shared := pageContent("SHARED")
	privateA := pageContent("PRIVATE-A")
	s.AddPage(hibersnapshot.Page{PFN: 11, Label: hibersnapshot.LabelNormal, Bytes: shared})
	s.AddPage(hibersnapshot.Page{PFN: 22, Label: hibersnapshot.LabelNormal, Bytes: privateA})
	if err := s.AddMapping(hibersnapshot.Mapping{PID: pidA, VA: va1, PFN: 11}); err != nil {
		t.Fatalf("AddMapping: %v", err)
	}
	if err := s.AddMapping(hibersnapshot.Mapping{PID: pidB, VA: va2, PFN: 11}); err != nil {
		t.Fatalf("AddMapping: %v", err)
	}
	if err := s.AddMapping(hibersnapshot.Mapping{PID: pidA, VA: va2, PFN: 22}); err != nil {
		t.Fatalf("AddMapping: %v", err)
	}

	im := mustBuild(t, s)

	if im.Attempts != 1 {
		t.Fatalf("expected single attempt, got %d", im.Attempts)
	}
	if len(im.Recovery) != 0 {
		t.Fatalf("expected no recovery entries for fully exportable pages, got %d", len(im.Recovery))
	}

	e11 := findEntry(t, im, 11)
	if e11.Redacted {
		t.Fatal("shared normal page must not be redacted")
	}
	if !bytes.Equal(e11.Content, shared) {
		t.Fatalf("shared page content mismatch: got %q want %q", e11.Content, shared)
	}
	if e11.ReinitKey != "" {
		t.Fatalf("exported page must not carry a reinit marker, got %q", e11.ReinitKey)
	}

	e22 := findEntry(t, im, 22)
	if !bytes.Equal(e22.Content, privateA) {
		t.Fatalf("private page content mismatch: got %q want %q", e22.Content, privateA)
	}

	// The image must own a defensive copy: mutating store bytes after the
	// snapshot cannot leak into a previously built image.
	im2 := mustBuild(t, s)
	s.AddPage(hibersnapshot.Page{PFN: 11, Label: hibersnapshot.LabelNormal, Bytes: []byte("TAMPERED-TAMPERED")})
	if !bytes.Equal(im2.Entries[0].Content, shared) {
		t.Fatal("image content aliases live store memory instead of being copied")
	}
}

// TestBuildConfidentialSharedPageRedacted is the must-be-blocked case: when one
// reference of a shared page requires confidentiality, real page content must
// not be exported, and every shared mapping must remain recoverable under one
// shared re-initialization marker.
func TestBuildConfidentialSharedPageRedacted(t *testing.T) {
	s := hibersnapshot.NewSampleStore()
	secret := pageContent("SECRET")
	s.AddPage(hibersnapshot.Page{PFN: 33, Label: hibersnapshot.LabelNormal, Bytes: secret})
	if err := s.AddMapping(hibersnapshot.Mapping{PID: pidA, VA: va1, PFN: 33, Confidential: false}); err != nil {
		t.Fatalf("AddMapping: %v", err)
	}
	if err := s.AddMapping(hibersnapshot.Mapping{PID: pidB, VA: va2, PFN: 33, Confidential: true}); err != nil {
		t.Fatalf("AddMapping: %v", err)
	}

	im := mustBuild(t, s)
	e := findEntry(t, im, 33)

	if !e.Redacted {
		t.Fatal("page with any confidential reference must be redacted")
	}
	if e.Content != nil {
		t.Fatalf("redacted entry must not export real content, got %q", e.Content)
	}
	for _, ent := range im.Entries {
		if bytes.Contains(ent.Content, secret) {
			t.Fatalf("secret bytes leaked into entry pfn=%d", ent.PFN)
		}
	}

	rec := recoveryRefs(im, 33)
	if len(rec) != 2 {
		t.Fatalf("expected both shared mappings to remain recoverable, got %d", len(rec))
	}
	marker := e.ReinitKey
	if marker == "" {
		t.Fatal("redacted page must carry a re-initialization marker")
	}
	wantRefs := map[hibersnapshot.Ref]bool{
		{PID: pidA, VA: va1}: false,
		{PID: pidB, VA: va2}: false,
	}
	for _, r := range rec {
		if r.ReinitKey != marker {
			t.Fatalf("recoverable mapping %+v has marker %q, want shared marker %q", r.Ref, r.ReinitKey, marker)
		}
		wantRefs[r.Ref] = true
	}
	for ref, seen := range wantRefs {
		if !seen {
			t.Fatalf("recovery plan missing mapping %+v", ref)
		}
	}

	// RecoveryPlan is the resume entry point: it must hand back exactly the
	// mappings that need rebuilding.
	plan := im.RecoveryPlan()
	if len(plan) != 2 {
		t.Fatalf("RecoveryPlan length = %d, want 2", len(plan))
	}
	for _, r := range plan {
		if r.ReinitKey != marker {
			t.Fatalf("RecoveryPlan entry %+v lost the shared marker", r.Ref)
		}
	}

	// The marker is deterministic per physical page across independent builds,
	// so retries and re-runs converge on the same re-initialization identity.
	imAgain := mustBuild(t, s)
	if findEntry(t, imAgain, 33).ReinitKey != marker {
		t.Fatal("reinit marker must be stable across builds")
	}
}

// TestBuildNoImageLabelRedacted covers the label-based cross constraint: a page
// tagged as forbidden from entering the image is redacted even when no
// reference marks itself confidential.
func TestBuildNoImageLabelRedacted(t *testing.T) {
	s := hibersnapshot.NewSampleStore()
	forbidden := pageContent("FORBIDDEN")
	s.AddPage(hibersnapshot.Page{PFN: 44, Label: hibersnapshot.LabelNoImage, Bytes: forbidden})
	if err := s.AddMapping(hibersnapshot.Mapping{PID: pidA, VA: va1, PFN: 44}); err != nil {
		t.Fatalf("AddMapping: %v", err)
	}

	im := mustBuild(t, s)
	e := findEntry(t, im, 44)
	if !e.Redacted || e.Content != nil {
		t.Fatalf("LabelNoImage page must be redacted without content, got %+v", e)
	}
	rec := recoveryRefs(im, 44)
	if len(rec) != 1 || rec[0].ReinitKey != e.ReinitKey || e.ReinitKey == "" {
		t.Fatalf("no-image mapping must remain recoverable under its marker, got %+v", rec)
	}
}

// TestBuildRetriesAcrossRemapBoundary is the interleaving/boundary case: a
// mapping changes while an attempt is in flight. The first attempt must be
// discarded and the retry must capture the new page identity only — the image
// must never mix the old reference with the new page.
func TestBuildRetriesAcrossRemapBoundary(t *testing.T) {
	s := hibersnapshot.NewSampleStore()
	oldPage := pageContent("OLDPAGE")
	newPage := pageContent("NEWPAGE")
	s.AddPage(hibersnapshot.Page{PFN: 55, Label: hibersnapshot.LabelNormal, Bytes: oldPage})
	s.AddPage(hibersnapshot.Page{PFN: 66, Label: hibersnapshot.LabelNormal, Bytes: newPage})
	if err := s.AddMapping(hibersnapshot.Mapping{PID: pidA, VA: va1, PFN: 55}); err != nil {
		t.Fatalf("AddMapping: %v", err)
	}
	boundaryBefore := s.Generation()

	hookFired := 0
	s.OnBuildAttempt = func(attempt int) {
		if attempt == 1 {
			hookFired++
			if err := s.Remap(pidA, va1, 66, false); err != nil {
				t.Errorf("Remap during attempt: %v", err)
			}
			s.OnBuildAttempt = nil
		}
	}

	im := mustBuild(t, s)

	if hookFired != 1 {
		t.Fatalf("interleaving hook fired %d times, want 1", hookFired)
	}
	if im.Attempts != 2 {
		t.Fatalf("expected one discarded attempt plus one retry, got Attempts=%d", im.Attempts)
	}
	if im.Generation <= boundaryBefore {
		t.Fatalf("image must bind to the post-remap generation, got %d (boundary before=%d)", im.Generation, boundaryBefore)
	}

	// New page identity present with real content...
	e66 := findEntry(t, im, 66)
	if e66.Redacted || !bytes.Equal(e66.Content, newPage) {
		t.Fatalf("retry must export the new page identity, got %+v", e66)
	}
	// ...and the old page identity must not appear at all: no mixed snapshot.
	for _, e := range im.Entries {
		if e.PFN == 55 {
			t.Fatalf("old page pfn=55 must not survive the remap boundary, entry=%+v", e)
		}
		if bytes.Contains(e.Content, oldPage) {
			t.Fatalf("old page content leaked into post-remap image: %+v", e)
		}
	}
	if len(im.Recovery) != 0 {
		t.Fatalf("post-remap normal mapping needs no recovery, got %+v", im.Recovery)
	}
}

// TestBuildRetryBudgetExhausted is the rejection case: if the mapping set keeps
// changing on every attempt, no consistent boundary exists and Build must fail
// rather than return a partial, mixed image.
func TestBuildRetryBudgetExhausted(t *testing.T) {
	s := hibersnapshot.NewSampleStore()
	pageA := pageContent("AAAA")
	pageB := pageContent("BBBB")
	s.AddPage(hibersnapshot.Page{PFN: 77, Label: hibersnapshot.LabelNormal, Bytes: pageA})
	s.AddPage(hibersnapshot.Page{PFN: 88, Label: hibersnapshot.LabelNormal, Bytes: pageB})
	if err := s.AddMapping(hibersnapshot.Mapping{PID: pidA, VA: va1, PFN: 77}); err != nil {
		t.Fatalf("AddMapping: %v", err)
	}

	s.OnBuildAttempt = func(attempt int) {
		// Flip the mapping to the other physical page on every attempt so
		// that no generation boundary can ever stabilize.
		target := uint64(88)
		if attempt%2 == 0 {
			target = 77
		}
		if err := s.Remap(pidA, va1, target, false); err != nil {
			t.Errorf("Remap during attempt %d: %v", attempt, err)
		}
	}

	const budget = 3
	im, err := s.Build(hibersnapshot.WithMaxAttempts(budget))
	if err == nil {
		t.Fatalf("Build must fail when no stable boundary exists, instead got image %+v", im)
	}
	var exhausted *hibersnapshot.RetryBudgetExhaustedError
	if !errors.As(err, &exhausted) {
		t.Fatalf("want RetryBudgetExhaustedError, got %T: %v", err, err)
	}
	if exhausted.Attempts != budget {
		t.Fatalf("reported attempts = %d, want %d", exhausted.Attempts, budget)
	}
	var inconsistent *hibersnapshot.SnapshotInconsistentError
	if !errors.As(err, &inconsistent) {
		t.Fatal("exhausted error must unwrap to the last boundary mismatch")
	}
	if inconsistent.Boundary == inconsistent.Observed {
		t.Fatal("boundary mismatch error must report distinct generations")
	}
}

// TestRemapMidBuildWithConfidentiality combines both cross constraints in one
// interleaving: a remap during the attempt lands on a page whose other
// reference is confidential. The discarded attempt must leave no trace, and
// the retried image must redact the new shared page with one shared marker.
func TestRemapMidBuildWithConfidentiality(t *testing.T) {
	s := hibersnapshot.NewSampleStore()
	plain := pageContent("PLAIN")
	guarded := pageContent("GUARDED")
	s.AddPage(hibersnapshot.Page{PFN: 101, Label: hibersnapshot.LabelNormal, Bytes: plain})
	s.AddPage(hibersnapshot.Page{PFN: 202, Label: hibersnapshot.LabelNormal, Bytes: guarded})
	if err := s.AddMapping(hibersnapshot.Mapping{PID: pidA, VA: va1, PFN: 101}); err != nil {
		t.Fatalf("AddMapping: %v", err)
	}
	if err := s.AddMapping(hibersnapshot.Mapping{PID: pidB, VA: va2, PFN: 202, Confidential: true}); err != nil {
		t.Fatalf("AddMapping: %v", err)
	}

	s.OnBuildAttempt = func(attempt int) {
		if attempt == 1 {
			if err := s.Remap(pidA, va1, 202, false); err != nil {
				t.Errorf("Remap during attempt: %v", err)
			}
			s.OnBuildAttempt = nil
		}
	}

	im := mustBuild(t, s)
	if im.Attempts != 2 {
		t.Fatalf("expected retry after interleaved remap, got Attempts=%d", im.Attempts)
	}

	// Old identity gone; new identity redacted because one reference is
	// confidential.
	for _, e := range im.Entries {
		if e.PFN == 101 {
			t.Fatal("old page identity must not be present after remap")
		}
	}
	e := findEntry(t, im, 202)
	if !e.Redacted || e.Content != nil {
		t.Fatalf("newly shared confidential page must be redacted, got %+v", e)
	}
	if bytes.Contains(bytes.Join(collectContents(im), nil), guarded) {
		t.Fatal("guarded content leaked into the retried image")
	}

	rec := recoveryRefs(im, 202)
	if len(rec) != 2 {
		t.Fatalf("both references must be recoverable, got %d", len(rec))
	}
	for _, r := range rec {
		if r.ReinitKey != e.ReinitKey {
			t.Fatalf("recoverable mapping %+v must share the marker %q", r.Ref, e.ReinitKey)
		}
	}
}

func collectContents(im *hibersnapshot.Image) [][]byte {
	out := make([][]byte, 0, len(im.Entries))
	for _, e := range im.Entries {
		out = append(out, e.Content)
	}
	return out
}

// TestStoreGuardsUnknownTargets checks the local sample rejects mappings to
// pages that do not exist, so a build can never reference an untracked page.
func TestStoreGuardsUnknownTargets(t *testing.T) {
	s := hibersnapshot.NewSampleStore()
	if err := s.AddMapping(hibersnapshot.Mapping{PID: pidA, VA: va1, PFN: 999}); err == nil {
		t.Fatal("AddMapping to unknown page must be rejected")
	}
	s.AddPage(hibersnapshot.Page{PFN: 1, Label: hibersnapshot.LabelNormal, Bytes: pageContent("X")})
	if err := s.AddMapping(hibersnapshot.Mapping{PID: pidA, VA: va1, PFN: 1}); err != nil {
		t.Fatalf("AddMapping: %v", err)
	}
	if err := s.Remap(pidA, va1, 999, false); err == nil {
		t.Fatal("Remap to unknown page must be rejected")
	}
	gen := s.Generation()
	if err := s.Remap(pidA, va1, 1, false); err != nil {
		t.Fatalf("no-op Remap: %v", err)
	}
	if s.Generation() != gen {
		t.Fatal("an identity-only remap must not bump the generation boundary")
	}
}
