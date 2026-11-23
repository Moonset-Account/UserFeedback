//! Integration tests for the paged KV-cache manager.
//!
//! Scenarios are derived from the problem statement:
//! * prefix reuse under the same model version + positional config;
//! * two sessions sharing one half-filled page, then diverging (COW);
//! * bounded pool: eviction only of unreferenced published pages;
//! * cancel + late fragment confirmations must not resurrect reclaimed pages;
//! * allocation shortage reports reusable length + missing pages and keeps
//!   existing sessions readable;
//! * inspection APIs (page map, in-page fragments, occupancy) stay coherent
//!   while other sessions are cancelled or switch model.

use paged_kv_cache::{
    AllocFailure, CacheConfig, CacheError, ConfirmOutcome, PagedCache, PageId, SessionId, Token,
};

fn cache(page_size: usize, total_pages: usize) -> PagedCache {
    PagedCache::new(CacheConfig {
        page_size,
        total_pages,
    })
    .unwrap()
}

/// Append tokens carrying a distinct payload per token, return fragment ids.
fn append_with_data(cache: &mut PagedCache, sid: SessionId, tokens: &[Token]) -> Vec<paged_kv_cache::FragmentId> {
    let parts: Vec<(Token, Vec<u8>)> = tokens.iter().map(|t| (*t, vec![*t as u8, 0xAB])).collect();
    cache.append(sid, &parts).unwrap()
}

fn confirm_all(cache: &mut PagedCache, sid: SessionId, frags: &[paged_kv_cache::FragmentId]) {
    for &f in frags {
        assert_eq!(cache.confirm(sid, f), ConfirmOutcome::Confirmed);
    }
}

// ---------------------------------------------------------------------------
// Normal path: confirmed prefix reuse, namespaced by model version + pos config
// ---------------------------------------------------------------------------

#[test]
fn confirmed_prefix_reuse_is_scoped_by_model_and_pos_config() {
    let mut c = cache(4, 16);
    let a = c.create_session("m1", "rope-a");
    let frags = c.append_tokens(a, &[10, 11, 12, 13, 14, 15, 16, 17]).unwrap();
    confirm_all(&mut c, a, &frags);

    // Same model + same pos config: full prefix reusable.
    assert_eq!(c.reusable_prefix("m1", "rope-a", &[10, 11, 12, 13, 14, 15, 16, 17, 99]), 8);
    // Different model version or different pos config: nothing reusable.
    assert_eq!(c.reusable_prefix("m2", "rope-a", &[10, 11, 12, 13]), 0);
    assert_eq!(c.reusable_prefix("m1", "rope-b", &[10, 11, 12, 13]), 0);

    // A second session reuses the confirmed prefix instead of allocating.
    let b = c.create_session("m1", "rope-a");
    c.append_tokens(b, &[10, 11, 12, 13, 14, 15, 16, 17, 18, 19]).unwrap();

    let map_a = c.page_map(a).unwrap();
    let map_b = c.page_map(b).unwrap();
    assert_eq!(map_a.len(), 2);
    assert_eq!(map_b.len(), 3);
    // The two full pages are shared by reference.
    assert_eq!(map_a[0].page_id, map_b[0].page_id);
    assert_eq!(map_a[1].page_id, map_b[1].page_id);
    assert!(map_b[0].shared && map_b[1].shared);
    assert!(!map_b[2].shared);
    // Only one fresh page was allocated for B's two extra tokens.
    let occ = c.occupancy();
    assert_eq!(occ.used_pages, 3);
    assert_eq!(occ.free_pages, 13);

    assert_eq!(c.read_prefix(b, 10).unwrap(), vec![10, 11, 12, 13, 14, 15, 16, 17, 18, 19]);
    assert_eq!(c.read_prefix(a, 8).unwrap(), vec![10, 11, 12, 13, 14, 15, 16, 17]);
}

// ---------------------------------------------------------------------------
// Headline scenario: two sessions share one half-filled page, then diverge
// ---------------------------------------------------------------------------

#[test]
fn shared_partial_page_diverges_with_copy_on_write() {
    let mut c = cache(4, 8);
    let a = c.create_session("m1", "rope-a");
    let frags = c.append_tokens(a, &[10, 11, 12, 13, 14, 15]).unwrap();
    confirm_all(&mut c, a, &frags);

    // B references the same half-filled page (2 of 4 slots used).
    let b = c.create_session("m1", "rope-a");
    c.append_tokens(b, &[10, 11, 12, 13, 14, 15]).unwrap();
    let shared_page = c.page_map(a).unwrap()[1].page_id;
    assert_eq!(c.page_map(b).unwrap()[1].page_id, shared_page);
    assert!(c.page_map(b).unwrap()[1].shared);
    assert_eq!(c.occupancy().used_pages, 2);

    // The two sessions now continue with *different* tokens.
    c.append_tokens(a, &[100]).unwrap();
    c.append_tokens(b, &[200]).unwrap();

    // Each session sees its own tail.
    assert_eq!(c.read_prefix(a, 7).unwrap(), vec![10, 11, 12, 13, 14, 15, 100]);
    assert_eq!(c.read_prefix(b, 7).unwrap(), vec![10, 11, 12, 13, 14, 15, 200]);

    // The page map diverged: A owns a private copy, B keeps the original page.
    let map_a = c.page_map(a).unwrap();
    let map_b = c.page_map(b).unwrap();
    assert_ne!(map_a[1].page_id, map_b[1].page_id);
    assert_eq!(map_b[1].page_id, shared_page);
    assert!(!map_a[1].shared);
    assert!(!map_b[1].shared);

    // The page held by the other session keeps its original confirmed content.
    let frags_shared = c.page_fragments(shared_page).unwrap();
    assert_eq!(frags_shared[0].token, 14);
    assert_eq!(frags_shared[1].token, 15);
    assert!(frags_shared[0].confirmed && frags_shared[1].confirmed);
    assert_eq!(frags_shared[2].token, 200); // B's own tail, written in place
    let frags_copy = c.page_fragments(map_a[1].page_id).unwrap();
    assert_eq!(frags_copy[2].token, 100);

    // The full page stays shared by both sessions.
    assert_eq!(map_a[0].page_id, map_b[0].page_id);
    assert!(map_a[0].shared);

    // A third session still reuses the original confirmed prefix.
    let d = c.create_session("m1", "rope-a");
    c.append_tokens(d, &[10, 11, 12, 13, 14, 15]).unwrap();
    assert_eq!(c.read_prefix(d, 6).unwrap(), vec![10, 11, 12, 13, 14, 15]);
    assert_eq!(c.page_map(d).unwrap()[1].page_id, shared_page);

    // And when the third session diverges, the page B holds is still untouched.
    c.append_tokens(d, &[300]).unwrap();
    let frags_shared = c.page_fragments(shared_page).unwrap();
    assert_eq!(frags_shared[2].token, 200);
    assert_eq!(c.read_prefix(d, 7).unwrap()[6], 300);
}

// ---------------------------------------------------------------------------
// Boundary: out-of-order and duplicate confirmations
// ---------------------------------------------------------------------------

#[test]
fn out_of_order_and_duplicate_confirmations() {
    let mut c = cache(2, 8);
    let a = c.create_session("m1", "rope-a");
    let frags = c.append_tokens(a, &[10, 11, 12, 13]).unwrap();

    // Confirming a later token first does not extend the reusable prefix.
    assert_eq!(c.confirm(a, frags[2]), ConfirmOutcome::Confirmed);
    assert_eq!(c.reusable_prefix("m1", "rope-a", &[10, 11, 12, 13]), 0);

    assert_eq!(c.confirm(a, frags[0]), ConfirmOutcome::Confirmed);
    assert_eq!(c.reusable_prefix("m1", "rope-a", &[10, 11, 12, 13]), 1);

    // The gap closes and the prefix extends over the earlier confirmation.
    assert_eq!(c.confirm(a, frags[1]), ConfirmOutcome::Confirmed);
    assert_eq!(c.reusable_prefix("m1", "rope-a", &[10, 11, 12, 13]), 3);

    // Duplicate confirmation is ignored.
    assert_eq!(c.confirm(a, frags[1]), ConfirmOutcome::Ignored);

    assert_eq!(c.confirm(a, frags[3]), ConfirmOutcome::Confirmed);
    assert_eq!(c.reusable_prefix("m1", "rope-a", &[10, 11, 12, 13]), 4);

    // Unknown session / unknown fragment are ignored, not errors.
    assert_eq!(c.confirm(SessionId(999), frags[0]), ConfirmOutcome::Ignored);
    assert_eq!(c.confirm(a, paged_kv_cache::FragmentId(999)), ConfirmOutcome::Ignored);
}

// ---------------------------------------------------------------------------
// Shortage: report reusable length + missing pages, keep old sessions readable
// ---------------------------------------------------------------------------

#[test]
fn allocation_shortage_reports_reusable_len_and_missing_pages() {
    let mut c = cache(2, 3);
    let a = c.create_session("m1", "rope-a");
    let frags = c.append_tokens(a, &[1, 2, 3, 4]).unwrap();
    confirm_all(&mut c, a, &frags);
    assert_eq!(c.occupancy().used_pages, 2);
    assert_eq!(c.occupancy().free_pages, 1);

    // B wants 8 tokens: 4 reusable (2 linked pages) + 4 fresh (2 pages),
    // but only 1 page is free.
    let b = c.create_session("m1", "rope-a");
    let err = c.append_tokens(b, &[1, 2, 3, 4, 5, 6, 7, 8]).unwrap_err();
    assert_eq!(
        err,
        CacheError::Allocation(AllocFailure {
            reusable_len: 4,
            missing_pages: 1,
        })
    );

    // The failed append changed nothing; the old session is still readable.
    assert_eq!(c.session_len(b), Some(0));
    assert_eq!(c.read_prefix(a, 4).unwrap(), vec![1, 2, 3, 4]);
    assert_eq!(c.occupancy().used_pages, 2);

    // Cancel A: its published pages stay in the index and remain reusable.
    c.cancel_session(a).unwrap();
    assert_eq!(c.occupancy().evictable_pages, 2);

    // Now B fits: it links the two evictable pages and allocates one more.
    c.append_tokens(b, &[1, 2, 3, 4, 5, 6]).unwrap();
    assert_eq!(c.read_prefix(b, 6).unwrap(), vec![1, 2, 3, 4, 5, 6]);
    let occ = c.occupancy();
    assert_eq!(occ.used_pages, 3);
    assert_eq!(occ.free_pages, 0);
    assert_eq!(occ.evictable_pages, 0);

    // Pool is full; one more token fails and still reports truthfully.
    let err = c.append_tokens(b, &[7]).unwrap_err();
    assert_eq!(
        err,
        CacheError::Allocation(AllocFailure {
            reusable_len: 4,
            missing_pages: 1,
        })
    );
    assert_eq!(c.read_prefix(b, 6).unwrap(), vec![1, 2, 3, 4, 5, 6]);
}

// ---------------------------------------------------------------------------
// Eviction: only unreferenced, published pages are reclaimable
// ---------------------------------------------------------------------------

#[test]
fn eviction_reclaims_only_unreferenced_published_pages() {
    let mut c = cache(2, 4);
    let a = c.create_session("m1", "rope-a");
    let frags = c.append_tokens(a, &[1, 2, 3, 4]).unwrap();
    confirm_all(&mut c, a, &frags);
    let b = c.create_session("m1", "rope-a");
    c.append_tokens(b, &[1, 2, 3, 4]).unwrap();

    // Everything is referenced: nothing to evict.
    assert_eq!(c.evict_unreferenced(), 0);
    assert_eq!(c.occupancy().evictable_pages, 0);

    // Cancelling A does not make the shared pages evictable (B holds them).
    c.cancel_session(a).unwrap();
    assert_eq!(c.evict_unreferenced(), 0);
    assert_eq!(c.read_prefix(b, 4).unwrap(), vec![1, 2, 3, 4]);

    // An unpublished page is freed immediately on cancel, without eviction.
    let x = c.create_session("m1", "rope-a");
    c.append_tokens(x, &[7, 8]).unwrap();
    assert_eq!(c.occupancy().used_pages, 3);
    c.cancel_session(x).unwrap();
    let occ = c.occupancy();
    assert_eq!(occ.used_pages, 2);
    assert_eq!(occ.evictable_pages, 0);

    // Cancelling B turns the published pages evictable; eviction reclaims them.
    c.cancel_session(b).unwrap();
    assert_eq!(c.occupancy().evictable_pages, 2);
    assert_eq!(c.evict_unreferenced(), 2);
    let occ = c.occupancy();
    assert_eq!(occ.used_pages, 0);
    assert_eq!(occ.free_pages, 4);
    // The reclaimed prefix is no longer reusable.
    assert_eq!(c.reusable_prefix("m1", "rope-a", &[1, 2, 3, 4]), 0);
}

// ---------------------------------------------------------------------------
// Cancel + late confirmations must not attribute reclaimed pages
// ---------------------------------------------------------------------------

#[test]
fn cancel_and_late_confirm_do_not_resurrect_reclaimed_pages() {
    let mut c = cache(2, 3);
    let a = c.create_session("m1", "rope-a");
    let frags = c.append_tokens(a, &[1, 2, 3, 4]).unwrap();
    assert_eq!(c.confirm(a, frags[0]), ConfirmOutcome::Confirmed);
    assert_eq!(c.confirm(a, frags[1]), ConfirmOutcome::Confirmed);

    c.cancel_session(a).unwrap();
    // Page 0 (published) is evictable; page 1 (never published) is gone.
    let occ = c.occupancy();
    assert_eq!(occ.used_pages, 1);
    assert_eq!(occ.evictable_pages, 1);
    assert_eq!(occ.free_pages, 2);

    // Late confirmations for the cancelled session are ignored.
    assert_eq!(c.confirm(a, frags[2]), ConfirmOutcome::Ignored);
    assert_eq!(c.confirm(a, frags[3]), ConfirmOutcome::Ignored);
    assert_eq!(c.occupancy().used_pages, 1);

    // Reclaim the evictable page, then confirm again: nothing comes back.
    assert_eq!(c.evict_unreferenced(), 1);
    assert_eq!(c.occupancy().used_pages, 0);
    assert_eq!(c.confirm(a, frags[0]), ConfirmOutcome::Ignored);
    assert_eq!(c.occupancy().used_pages, 0);
    assert_eq!(c.reusable_prefix("m1", "rope-a", &[1, 2, 3, 4]), 0);

    // The cancelled session is gone for good.
    assert_eq!(c.read_prefix(a, 1), Err(CacheError::UnknownSession(a)));
    assert_eq!(c.cancel_session(a), Err(CacheError::UnknownSession(a)));

    // A new session starts clean and can repopulate the pool.
    let b = c.create_session("m1", "rope-a");
    let frags_b = c.append_tokens(b, &[1, 2, 3, 4]).unwrap();
    confirm_all(&mut c, b, &frags_b);
    assert_eq!(c.reusable_prefix("m1", "rope-a", &[1, 2, 3, 4]), 4);
}

// ---------------------------------------------------------------------------
// Cancelling another session / switching model must not break own prefix
// ---------------------------------------------------------------------------

#[test]
fn cancel_of_other_session_and_model_switch_keep_own_prefix_readable() {
    let mut c = cache(2, 8);
    let a = c.create_session("m1", "rope");
    let frags = c.append_tokens(a, &[1, 2, 3, 4]).unwrap();
    confirm_all(&mut c, a, &frags);

    let b = c.create_session("m1", "rope");
    c.append_tokens(b, &[1, 2, 3, 4]).unwrap();

    let other = c.create_session("m2", "rope");
    let frags_o = c.append_tokens(other, &[1, 2, 3, 4]).unwrap();
    confirm_all(&mut c, other, &frags_o);

    // Cancel the session that originally published the prefix.
    c.cancel_session(a).unwrap();
    assert_eq!(c.read_prefix(b, 4).unwrap(), vec![1, 2, 3, 4]);
    assert_eq!(c.read_prefix(other, 4).unwrap(), vec![1, 2, 3, 4]);

    // B switches model: its cache is reset, others are unaffected.
    c.switch_model(b, "m2", "rope").unwrap();
    assert_eq!(c.session_len(b), Some(0));
    assert_eq!(c.read_prefix(b, 1), Err(CacheError::InvalidLength { requested: 1, available: 0 }));
    assert_eq!(c.read_prefix(other, 4).unwrap(), vec![1, 2, 3, 4]);

    // The m1 prefix survived B's switch (pages still published, unreferenced).
    assert_eq!(c.reusable_prefix("m1", "rope", &[1, 2, 3, 4]), 4);

    // B, now on m2, reuses the m2 prefix published by `other`.
    c.append_tokens(b, &[1, 2, 3, 4]).unwrap();
    assert_eq!(c.read_prefix(b, 4).unwrap(), vec![1, 2, 3, 4]);
    let map_b = c.page_map(b).unwrap();
    let map_o = c.page_map(other).unwrap();
    assert_eq!(map_b[0].page_id, map_o[0].page_id);
    assert!(map_b[0].shared);

    // A fresh m1 session still picks up the original m1 prefix.
    let d = c.create_session("m1", "rope");
    c.append_tokens(d, &[1, 2, 3, 4, 5, 6]).unwrap();
    assert_eq!(c.read_prefix(d, 6).unwrap(), vec![1, 2, 3, 4, 5, 6]);
}

// ---------------------------------------------------------------------------
// Inspection APIs: page map, in-page fragments, occupancy
// ---------------------------------------------------------------------------

#[test]
fn inspection_apis_expose_page_map_fragments_and_occupancy() {
    let mut c = cache(2, 4);
    let a = c.create_session("m1", "rope-a");
    let frags = append_with_data(&mut c, a, &[10, 11, 12]);

    let map = c.page_map(a).unwrap();
    assert_eq!(map.len(), 2);
    assert_eq!(map[0].start, 0);
    assert_eq!(map[0].len, 2);
    assert_eq!(map[0].confirmed, 0);
    assert!(!map[0].shared);
    assert_eq!(map[0].ref_count, 1);
    assert_eq!(map[1].start, 2);
    assert_eq!(map[1].len, 1);

    // In-page fragments carry the payload supplied at append time.
    let page0 = map[0].page_id;
    let views = c.page_fragments(page0).unwrap();
    assert_eq!(views.len(), 2);
    assert_eq!(views[0].offset, 0);
    assert_eq!(views[0].token, 10);
    assert_eq!(views[0].data, vec![10u8, 0xAB]);
    assert!(!views[0].confirmed);
    assert_eq!(views[1].token, 11);
    assert_eq!(views[1].data, vec![11u8, 0xAB]);

    // Confirmation is reflected in both the page map and the fragments.
    assert_eq!(c.confirm(a, frags[0]), ConfirmOutcome::Confirmed);
    assert_eq!(c.page_map(a).unwrap()[0].confirmed, 1);
    assert!(c.page_fragments(page0).unwrap()[0].confirmed);
    assert!(!c.page_fragments(page0).unwrap()[1].confirmed);

    // Unknown page inspects as None.
    assert_eq!(c.page_fragments(PageId(999)), None);

    let occ = c.occupancy();
    assert_eq!(occ.total_pages, 4);
    assert_eq!(occ.used_pages, 2);
    assert_eq!(occ.free_pages, 2);
    assert_eq!(occ.evictable_pages, 0);
    assert_eq!(occ.active_sessions, 1);
}

// ---------------------------------------------------------------------------
// Boundary: exact page fill, empty append, invalid config, unknown handles
// ---------------------------------------------------------------------------

#[test]
fn boundary_conditions() {
    // Invalid configs are rejected.
    assert!(matches!(
        PagedCache::new(CacheConfig { page_size: 0, total_pages: 2 }),
        Err(CacheError::InvalidConfig)
    ));
    assert!(matches!(
        PagedCache::new(CacheConfig { page_size: 2, total_pages: 0 }),
        Err(CacheError::InvalidConfig)
    ));

    let mut c = cache(2, 2);
    let a = c.create_session("m1", "rope-a");

    // Empty append is a no-op.
    assert_eq!(c.append_tokens(a, &[]).unwrap(), Vec::new());
    assert_eq!(c.session_len(a), Some(0));

    // Exactly fill two pages.
    c.append_tokens(a, &[1, 2]).unwrap();
    c.append_tokens(a, &[3, 4]).unwrap();
    assert_eq!(c.occupancy().used_pages, 2);

    // One more token needs a page the pool does not have.
    let err = c.append_tokens(a, &[5]).unwrap_err();
    assert_eq!(
        err,
        CacheError::Allocation(AllocFailure {
            reusable_len: 0,
            missing_pages: 1,
        })
    );
    // Session content is intact after the failure.
    assert_eq!(c.read_prefix(a, 4).unwrap(), vec![1, 2, 3, 4]);
    assert_eq!(
        c.read_prefix(a, 5),
        Err(CacheError::InvalidLength { requested: 5, available: 4 })
    );

    // Reads and lookups on unknown handles.
    let ghost = SessionId(4242);
    assert_eq!(c.read_prefix(ghost, 1), Err(CacheError::UnknownSession(ghost)));
    assert_eq!(c.page_map(ghost), Err(CacheError::UnknownSession(ghost)));
    assert_eq!(c.cancel_session(ghost), Err(CacheError::UnknownSession(ghost)));
    assert_eq!(c.switch_model(ghost, "m", "p"), Err(CacheError::UnknownSession(ghost)));
    assert_eq!(c.session_len(ghost), None);
    assert_eq!(c.reusable_prefix("nope", "nope", &[1, 2]), 0);
}

// ---------------------------------------------------------------------------
// Interleaved sessions: appends, confirms, cancel and eviction interleaved
// ---------------------------------------------------------------------------

#[test]
fn interleaved_sessions_stay_consistent() {
    let mut c = cache(4, 10);
    let a = c.create_session("m1", "rope");
    let b = c.create_session("m1", "rope");
    let d = c.create_session("m2", "rope");

    // A publishes only its first page worth of tokens.
    let frags_a = c.append_tokens(a, &[1, 2, 3, 4, 5, 6, 7, 8]).unwrap();
    confirm_all(&mut c, a, &frags_a[0..4]);

    // B reuses the confirmed half and computes the rest itself.
    let _frags_b = c.append_tokens(b, &[1, 2, 3, 4, 5, 6, 7, 8]).unwrap();
    let map_b = c.page_map(b).unwrap();
    assert_eq!(map_b[0].page_id, c.page_map(a).unwrap()[0].page_id);
    assert_ne!(map_b[1].page_id, c.page_map(a).unwrap()[1].page_id);

    // D (other model) allocates its own pages.
    c.append_tokens(d, &[1, 2, 3, 4, 5, 6, 7, 8]).unwrap();

    // A extends and confirms the rest; B extends with its own continuation.
    let frags_a2 = c.append_tokens(a, &[9, 10]).unwrap();
    confirm_all(&mut c, a, &frags_a[4..]);
    c.append_tokens(b, &[9, 10]).unwrap();

    // Cancel A: its unpublished tail page is freed, the published ones stay.
    c.cancel_session(a).unwrap();
    assert_eq!(c.confirm(a, frags_a2[0]), ConfirmOutcome::Ignored);
    assert_eq!(c.read_prefix(b, 10).unwrap(), vec![1, 2, 3, 4, 5, 6, 7, 8, 9, 10]);
    assert_eq!(c.read_prefix(d, 8).unwrap(), vec![1, 2, 3, 4, 5, 6, 7, 8]);

    // A new m1 session reuses everything A confirmed (both pages).
    assert_eq!(c.reusable_prefix("m1", "rope", &[1, 2, 3, 4, 5, 6, 7, 8]), 8);
    let e = c.create_session("m1", "rope");
    c.append_tokens(e, &[1, 2, 3, 4, 5, 6, 7, 8]).unwrap();
    assert_eq!(c.read_prefix(e, 8).unwrap(), vec![1, 2, 3, 4, 5, 6, 7, 8]);

    // All pages are referenced now: eviction is a no-op, accounting balances.
    assert_eq!(c.evict_unreferenced(), 0);
    let occ = c.occupancy();
    assert_eq!(occ.used_pages + occ.free_pages, occ.total_pages);
    assert_eq!(occ.active_sessions, 3); // b, d, e
    assert_eq!(occ.used_pages, 6); // 2 shared m1 pages + 1 B tail + 2 D pages + 0 extra
}
