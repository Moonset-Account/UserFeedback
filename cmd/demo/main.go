package main

import (
	"errors"
	"fmt"
	"strings"

	causalcodec "causalcodec"
)

func main() {
	report := buildReport()

	fmt.Println("=== causal context codec demo ===")
	fmt.Printf("event=%s deliveries=%d paths=%d catalog_causes=%d\n\n",
		report.Delivery.EventID, 1, len(report.Paths), len(report.Causes))

	// Encode under a hard budget. Causes below normal retention priority are
	// disconnected before byte-budget pruning starts.
	codec := &causalcodec.Codec{MaxBytes: 1500, MinPriority: causalcodec.PriorityNormal}
	packet, err := codec.Encode(report)
	if err != nil {
		panic(err)
	}
	fmt.Println("=== compression / duplicate sharing ===")
	fmt.Printf("budget=%d encoded=%d full_unpruned=%d\n",
		packet.Plan.MaxBytes, packet.Plan.SizeBytes, packet.Plan.FullSizeBytes)
	fmt.Printf("cause_references=%d unique_catalog_entries=%d repeated_references=%d duplicate_savings~%dB\n",
		packet.Plan.CauseReferences, packet.Plan.UniqueCauses,
		packet.Plan.RepeatedReferences, packet.Plan.DuplicateSavingsBytes)
	fmt.Printf("retained=%s\n", strings.Join(packet.Plan.RetainedCauseIDs, ", "))
	for _, d := range packet.Plan.DroppedCauses {
		fmt.Printf("dropped id=%s priority=%s reason=%s\n", d.ID, d.Priority, d.Reason)
	}
	fmt.Printf("gap_ids=%s\n\n", strings.Join(packet.Plan.GapIDs, ", "))

	// Receiver has an old same-name anchor and one valid unrelated anchor, but
	// not the exact requested warehouse-db@v2 fingerprint.
	store := causalcodec.NewAnchorStore()
	oldDB, err := causalcodec.NewAnchor("warehouse-db", "v1", causalcodec.Context{
		Causes: []causalcodec.Cause{{
			ID: "old-local-db", Stage: "db.old", Kind: "OldLocalSnapshot",
			Message: "old local same-name context; must never fill v2 chain",
		}},
		Segments: []causalcodec.Segment{{Stage: "db.old", CauseIDs: []string{"old-local-db"}}},
	})
	if err != nil {
		panic(err)
	}
	cacheAnchor, err := causalcodec.NewAnchor("cache-sidecar", "v3", causalcodec.Context{
		Causes: []causalcodec.Cause{{
			ID: "cache-backlog", Stage: "cache.invalidate", Kind: "InvalidationBacklog",
			Message: "invalidation stream lagged by 18s", Priority: causalcodec.PriorityHigh,
		}},
		Segments: []causalcodec.Segment{{
			Stage: "cache.invalidate", CauseIDs: []string{"cache-backlog"},
		}},
	})
	if err != nil {
		panic(err)
	}
	store.MustPut(oldDB).MustPut(cacheAnchor)
	codec.Anchors = store

	decoded, decodeErr := codec.Decode(packet.Bytes)
	fmt.Println("=== receiver display after bounded decode ===")
	fmt.Print(causalcodec.Display(decoded))
	if decodeErr != nil {
		fmt.Printf("\ndecode returned partial result plus: %v\n", decodeErr)
		var missing *causalcodec.MissingAnchorError
		if errors.As(decodeErr, &missing) {
			fmt.Printf("exact anchor still missing: %s\n", missing.Ref)
		}
	}

	// Later diagnostics return the detached low-priority storage tail. Attach
	// it to the priority gap without touching the missing-anchor gap.
	priorityGap := findPriorityGap(decoded)
	if priorityGap == nil {
		panic("expected a priority gap for extension demo")
	}
	root := causalcodec.Cause{
		ID: "disk-controller-throttle", Stage: "storage.controller",
		Kind:    "ControllerQueueThrottled",
		Message: "nvme controller depth exceeded and throttled fsync completion",
		Source: causalcodec.Source{
			Bus: "node-agent", Topic: "node.disk.events", Consumer: "storage-diagnoser",
		},
		Priority: causalcodec.PriorityHigh,
	}
	dbCause, ok := report.Cause("cause-db-timeout")
	if !ok {
		panic("missing synthetic db cause")
	}
	extended, err := decoded.Extend(causalcodec.Extension{
		GapID:  priorityGap.ID,
		Note:   "late storage diagnostics reconnected this tail",
		Causes: []causalcodec.Cause{root, dbCause},
		Segments: []causalcodec.Segment{
			{Stage: "storage.controller", CauseIDs: []string{root.ID}},
			{Stage: "storage.fsync", CauseIDs: []string{"cause-db-timeout"}},
		},
	})
	if err != nil {
		panic(err)
	}

	fmt.Println("\n=== display after extension ===")
	fmt.Print(causalcodec.Display(extended))
	fmt.Printf("\nopen gaps after extension: %d (missing exact anchor remains explicit)\n",
		len(extended.OpenGaps()))
}

func findPriorityGap(r *causalcodec.Report) *causalcodec.Gap {
	for _, g := range r.Gaps() {
		if g.Kind == causalcodec.GapPriority && !g.Extended {
			g := g
			return &g
		}
	}
	return nil
}

func buildReport() causalcodec.Report {
	source := causalcodec.Source{
		Bus: "event-bus", Topic: "orders.delivery", Partition: 7, Offset: 98213,
		Consumer: "order-worker", Attempt: 2,
	}

	// warehouse-db@v2 is referenced below but intentionally not inserted into
	// the receiver store in main; see the anchor tests for the full exchange.
	warehouseV2, err := causalcodec.NewAnchor("warehouse-db", "v2", causalcodec.Context{
		Causes: []causalcodec.Cause{{
			ID: "warehouse-v2-wal", Stage: "warehouse.wal", Kind: "WALDelay",
			Message: "warehouse WAL archive fell behind", Priority: causalcodec.PriorityNormal,
		}},
	})
	if err != nil {
		panic(err)
	}
	cacheRef := causalcodec.AnchorRef{
		Name: "cache-sidecar", Version: "v3",
		Digest: mustDigest(causalcodec.Context{
			Causes: []causalcodec.Cause{{
				ID: "cache-backlog", Stage: "cache.invalidate", Kind: "InvalidationBacklog",
				Message: "invalidation stream lagged by 18s", Priority: causalcodec.PriorityHigh,
			}},
			Segments: []causalcodec.Segment{{
				Stage: "cache.invalidate", CauseIDs: []string{"cache-backlog"},
			}},
		}),
	}

	shared := causalcodec.Cause{
		ID: "shared-publish-timeout", Stage: "publish", Kind: "PublishTimeout",
		Message: "broker publish timed out after 2s", Source: source,
		Priority: causalcodec.PriorityHigh, Retryable: true,
	}
	return causalcodec.Report{
		Delivery: causalcodec.Delivery{ID: "dlv-demo-001", EventID: "evt-order-42", Source: source},
		Causes: []causalcodec.Cause{
			{
				ID: "dispatch-saturated", Stage: "dispatch.accept", Kind: "WorkerPoolSaturated",
				Message: "all 32 worker slots were busy", Source: source,
				Priority: causalcodec.PriorityCritical, Retryable: true,
			},
			shared,
			{
				ID: "broker-leader-alt", Stage: "publish", Kind: "BrokerLeaderChanged",
				Message: "alternative: leader election started during publish", Source: source,
				Priority: causalcodec.PriorityNormal,
			},
			{
				ID: "quorum-ack-failed", Stage: "commit", Kind: "QuorumWriteFailed",
				Message: "only one of three replicas acknowledged", Source: source,
				Priority: causalcodec.PriorityHigh, Retryable: true,
			},
			{
				ID: "cause-db-timeout", Stage: "storage.deep", Kind: "DatabaseFsyncTimeout",
				Message:  "low-priority verbose trace: fsync waited five seconds on a noisy disk",
				Source:   source,
				Priority: causalcodec.PriorityLow,
			},
			{
				ID: "cause-db-lock", Stage: "storage.deep", Kind: "LockWaitTimeout",
				Message:  "debug trace: row lock was held by reconciliation job 8842",
				Source:   source,
				Priority: causalcodec.PriorityDebug,
			},
			{
				ID: "cache-backlog", Stage: "cache.invalidate", Kind: "InvalidationBacklog",
				Message: "invalidation stream lagged by 18s", Priority: causalcodec.PriorityHigh,
			},
		},
		Paths: []causalcodec.Path{
			{
				ID: "path-east", Label: "us-east primary",
				Segments: []causalcodec.Segment{
					{Stage: "dispatch.accept", CauseIDs: []string{"dispatch-saturated"}},
					{Stage: "publish", CauseIDs: []string{"shared-publish-timeout", "broker-leader-alt"}},
					{Stage: "commit", CauseIDs: []string{"quorum-ack-failed"}},
					{Stage: "storage.deep", CauseIDs: []string{"cause-db-timeout", "cause-db-lock"}},
					{Stage: "warehouse.context", Anchor: &warehouseV2.Ref},
				},
			},
			{
				ID: "path-west-retry", Label: "us-west retry; duplicates publish cause",
				Segments: []causalcodec.Segment{
					{Stage: "dispatch.accept", CauseIDs: []string{"dispatch-saturated"}},
					{Stage: "publish.retry", CauseIDs: []string{"shared-publish-timeout", "broker-leader-alt"}},
					{Stage: "cache.read", Anchor: &cacheRef},
				},
			},
			{
				ID: "path-async", Label: "async delivery path; third shared use",
				Segments: []causalcodec.Segment{
					{Stage: "publish.async", CauseIDs: []string{"shared-publish-timeout"}},
				},
			},
		},
	}
}

func mustDigest(ctx causalcodec.Context) string {
	d, err := causalcodec.DigestContext(ctx)
	if err != nil {
		panic(err)
	}
	return d
}
