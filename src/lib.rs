//! Paged KV-cache manager for an inference serving stack.
//!
//! # Model
//!
//! * The cache is a fixed pool of physical pages (`CacheConfig::total_pages`),
//!   each holding up to `CacheConfig::page_size` token slots.
//! * Every session is bound to a [`ModelKey`] (model version + positional
//!   encoding config). Confirmed token prefixes are published into a
//!   per-model prefix index, so another session with the *same* model version
//!   *and* positional config can reuse the longest confirmed prefix of its
//!   token sequence instead of recomputing it.
//! * Reused pages are shared by reference. A physical page may be only
//!   partially filled; when two sessions hold the same partial page and one
//!   of them appends a diverging token, the writer gets a private copy
//!   (copy-on-write) so each session sees its own tail while the page held
//!   by the other sessions keeps its original content.
//! * Only pages that are evictable (published in the cache index) **and**
//!   referenced by no session may be reclaimed. Unreferenced pages that were
//!   never published are freed immediately.
//! * Fragment confirmations may arrive late. Confirming a fragment of a
//!   cancelled session (or any otherwise stale handle) is a no-op and never
//!   attributes reclaimed pages to a dead session. Page and session ids are
//!   never reused.
//! * When the pool cannot satisfy an append, the call fails with
//!   [`AllocFailure`] reporting the reusable confirmed prefix length and the
//!   actual number of missing pages; existing sessions stay readable.
//!
//! # Example
//!
//! ```
//! use paged_kv_cache::{CacheConfig, PagedCache};
//!
//! let mut cache = PagedCache::new(CacheConfig { page_size: 2, total_pages: 4 }).unwrap();
//! let a = cache.create_session("model-v1", "rope-v1");
//! let frags = cache.append_tokens(a, &[1, 2, 3]).unwrap();
//! for f in frags {
//!     cache.confirm(a, f);
//! }
//! // A second session with the same model + positional config reuses the prefix.
//! assert_eq!(cache.reusable_prefix("model-v1", "rope-v1", &[1, 2, 3, 9]), 3);
//! // A different positional encoding config does not match.
//! assert_eq!(cache.reusable_prefix("model-v1", "rope-v2", &[1, 2, 3, 9]), 0);
//! ```

#![forbid(unsafe_code)]

use std::collections::HashMap;
use std::fmt;

/// A token id from the model vocabulary.
pub type Token = u32;

/// Cache namespace: identical model version **and** positional encoding
/// config are required for prefix reuse.
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct ModelKey {
    pub model_version: String,
    pub pos_config: String,
}

impl ModelKey {
    pub fn new(model_version: impl Into<String>, pos_config: impl Into<String>) -> Self {
        ModelKey {
            model_version: model_version.into(),
            pos_config: pos_config.into(),
        }
    }
}

/// Opaque session handle. Ids are never reused.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct SessionId(pub u64);

/// Opaque physical page handle. Ids are never reused, so a stale handle can
/// never accidentally name a recycled page.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct PageId(pub u64);

/// Opaque per-token fragment handle returned by `append*`.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct FragmentId(pub u64);

/// Pool geometry.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct CacheConfig {
    /// Number of token slots per physical page (must be >= 1).
    pub page_size: usize,
    /// Total number of physical pages in the pool (must be >= 1).
    pub total_pages: usize,
}

/// Reported when the page pool cannot satisfy an append. The session is left
/// untouched and every other session remains readable.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct AllocFailure {
    /// Length of the longest confirmed prefix of the attempted token sequence
    /// that is currently reusable from the cache index.
    pub reusable_len: usize,
    /// Number of additional physical pages that would be needed.
    pub missing_pages: usize,
}

/// Errors returned by [`PagedCache`] operations.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum CacheError {
    /// `page_size` or `total_pages` was zero.
    InvalidConfig,
    /// The session does not exist (unknown or already cancelled).
    UnknownSession(SessionId),
    /// The page does not exist (unknown or already reclaimed).
    UnknownPage(PageId),
    /// Requested read length exceeds the session's token count.
    InvalidLength { requested: usize, available: usize },
    /// Not enough free pages; see [`AllocFailure`].
    Allocation(AllocFailure),
}

impl fmt::Display for CacheError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            CacheError::InvalidConfig => write!(f, "page_size and total_pages must be >= 1"),
            CacheError::UnknownSession(id) => write!(f, "unknown session {:?}", id),
            CacheError::UnknownPage(id) => write!(f, "unknown page {:?}", id),
            CacheError::InvalidLength { requested, available } => write!(
                f,
                "invalid read length {requested}, session holds {available} tokens"
            ),
            CacheError::Allocation(fail) => write!(
                f,
                "page pool exhausted: reusable_len={}, missing_pages={}",
                fail.reusable_len, fail.missing_pages
            ),
        }
    }
}

impl std::error::Error for CacheError {}

/// Outcome of a fragment confirmation.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ConfirmOutcome {
    /// The fragment was still live and is now confirmed.
    Confirmed,
    /// The fragment was stale (cancelled session, unknown or already
    /// confirmed handle). Nothing was changed and no page was attributed.
    Ignored,
}

/// One entry of a session's page map (its "page table" view).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PageMapEntry {
    pub page_id: PageId,
    /// Token position of the first slot of this page within the session.
    pub start: usize,
    /// Number of tokens of this session covered by this page.
    pub len: usize,
    /// Confirmed slots inside the page (contiguous from slot 0).
    pub confirmed: usize,
    /// Whether another session also references this page.
    pub shared: bool,
    /// Number of sessions currently referencing this page.
    pub ref_count: usize,
}

/// Inspection view of one in-page fragment (one token slot).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FragmentView {
    /// Slot offset inside the page.
    pub offset: usize,
    pub token: Token,
    pub confirmed: bool,
    /// The per-token cache fragment payload supplied at append time.
    pub data: Vec<u8>,
}

/// Global pool occupancy snapshot.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Occupancy {
    pub total_pages: usize,
    pub used_pages: usize,
    pub free_pages: usize,
    /// Pages published in the cache index that no session references; these
    /// are the only pages [`PagedCache::evict_unreferenced`] may reclaim.
    pub evictable_pages: usize,
    pub active_sessions: usize,
}

/// One token slot inside a physical page.
#[derive(Clone)]
struct Slot {
    token: Token,
    data: Vec<u8>,
    confirmed: bool,
}

/// A physical page.
struct Page {
    slots: Vec<Slot>,
    /// Confirmed slots, contiguous from slot 0.
    confirmed: usize,
    /// Number of sessions referencing this page.
    ref_count: usize,
    /// Whether the page is published in the cache index (i.e. evictable once
    /// `ref_count` drops to 0).
    in_index: bool,
}

impl Page {
    fn fresh() -> Self {
        Page {
            slots: Vec::new(),
            confirmed: 0,
            ref_count: 1,
            in_index: false,
        }
    }
}

/// Per-session state.
struct Session {
    model_key: ModelKey,
    /// Full token sequence of the session.
    tokens: Vec<Token>,
    /// Page table: one page id per `page_size` block of `tokens`.
    page_table: Vec<PageId>,
    /// Longest contiguous confirmed prefix length (in tokens).
    confirmed_prefix: usize,
    /// Live fragment handles -> token position.
    pending: HashMap<FragmentId, usize>,
}

/// Prefix index node for one model key. `page` is the physical page holding
/// the token at this depth; the entry is only valid while the page exists.
#[derive(Default)]
struct TrieNode {
    children: HashMap<Token, TrieNode>,
    page: Option<PageId>,
}

/// The paged KV cache.
pub struct PagedCache {
    page_size: usize,
    total_pages: usize,
    free_pages: usize,
    pages: HashMap<PageId, Page>,
    index: HashMap<ModelKey, TrieNode>,
    sessions: HashMap<SessionId, Session>,
    next_page: u64,
    next_session: u64,
    next_fragment: u64,
}

/// Reserve a physical page id from the free pool. Caller must insert the page.
fn alloc_page_id(free_pages: &mut usize, next_page: &mut u64) -> PageId {
    debug_assert!(*free_pages > 0, "allocation must be pre-reserved");
    *free_pages -= 1;
    let id = PageId(*next_page);
    *next_page += 1;
    id
}

/// Drop one session reference. Unreferenced pages that were never published
/// in the index are reclaimed immediately; published ones become evictable.
fn release_page(pages: &mut HashMap<PageId, Page>, free_pages: &mut usize, pid: PageId) {
    if let Some(p) = pages.get_mut(&pid) {
        p.ref_count -= 1;
        if p.ref_count == 0 && !p.in_index {
            pages.remove(&pid);
            *free_pages += 1;
        }
    }
}

/// Copy-on-write: give the caller a private copy of `old_pid` (all slots,
/// including unconfirmed ones, so the writer's own pending tail survives)
/// and drop one reference from the original, which keeps its content for the
/// remaining holders.
fn cow_page(
    pages: &mut HashMap<PageId, Page>,
    free_pages: &mut usize,
    next_page: &mut u64,
    old_pid: PageId,
) -> PageId {
    let new_pid = alloc_page_id(free_pages, next_page);
    let src = pages.get(&old_pid).expect("cow source page must exist");
    let cloned = Page {
        slots: src.slots.clone(),
        confirmed: src.confirmed,
        ref_count: 1,
        in_index: false,
    };
    pages.insert(new_pid, cloned);
    release_page(pages, free_pages, old_pid);
    new_pid
}

/// Write tokens (and their fragment payloads) into `page` starting at slot
/// offset `off`, overwriting stale slots or extending the page as needed.
fn write_slots(
    page: &mut Page,
    off: usize,
    parts: &[(Token, Vec<u8>)],
    frag_ids: &[FragmentId],
    base_pos: usize,
    pending: &mut HashMap<FragmentId, usize>,
) {
    for (j, (tok, data)) in parts.iter().enumerate() {
        let slot = Slot {
            token: *tok,
            data: data.clone(),
            confirmed: false,
        };
        if off + j < page.slots.len() {
            // Writes only ever touch slots past the confirmed region.
            debug_assert!(!page.slots[off + j].confirmed);
            page.slots[off + j] = slot;
        } else {
            page.slots.push(slot);
        }
        pending.insert(frag_ids[j], base_pos + j);
    }
}

impl PagedCache {
    /// Create a cache with a bounded physical page pool.
    pub fn new(config: CacheConfig) -> Result<Self, CacheError> {
        if config.page_size == 0 || config.total_pages == 0 {
            return Err(CacheError::InvalidConfig);
        }
        Ok(PagedCache {
            page_size: config.page_size,
            total_pages: config.total_pages,
            free_pages: config.total_pages,
            pages: HashMap::new(),
            index: HashMap::new(),
            sessions: HashMap::new(),
            next_page: 0,
            next_session: 0,
            next_fragment: 0,
        })
    }

    /// Open a session bound to a model version + positional encoding config.
    pub fn create_session(&mut self, model_version: &str, pos_config: &str) -> SessionId {
        let id = SessionId(self.next_session);
        self.next_session += 1;
        self.sessions.insert(
            id,
            Session {
                model_key: ModelKey::new(model_version, pos_config),
                tokens: Vec::new(),
                page_table: Vec::new(),
                confirmed_prefix: 0,
                pending: HashMap::new(),
            },
        );
        id
    }

    /// Longest confirmed prefix of `tokens` currently reusable under the
    /// given model version + positional config.
    pub fn reusable_prefix(&self, model_version: &str, pos_config: &str, tokens: &[Token]) -> usize {
        let key = ModelKey::new(model_version, pos_config);
        self.longest_match(&key, tokens).0
    }

    /// Number of tokens currently held by a session (`None` if unknown).
    pub fn session_len(&self, sid: SessionId) -> Option<usize> {
        self.sessions.get(&sid).map(|s| s.tokens.len())
    }

    /// Append tokens with empty fragment payloads. See [`PagedCache::append`].
    pub fn append_tokens(
        &mut self,
        sid: SessionId,
        tokens: &[Token],
    ) -> Result<Vec<FragmentId>, CacheError> {
        let parts: Vec<(Token, Vec<u8>)> = tokens.iter().map(|t| (*t, Vec::new())).collect();
        self.append(sid, &parts)
    }

    /// Append `(token, fragment)` pairs to a session.
    ///
    /// The session first reuses the longest confirmed prefix of its full
    /// token sequence from the cache index (same model version + positional
    /// config), linking the published pages; the remaining tokens are written
    /// into session-owned pages, copying shared partial pages on write.
    ///
    /// Returns one fragment handle per appended token, in order. Handles for
    /// cache-hit tokens are inert: confirming them yields
    /// [`ConfirmOutcome::Ignored`] because those tokens are already confirmed.
    ///
    /// On pool exhaustion the session is left untouched and
    /// [`CacheError::Allocation`] reports the reusable prefix length and the
    /// actual number of missing pages.
    pub fn append(
        &mut self,
        sid: SessionId,
        parts: &[(Token, Vec<u8>)],
    ) -> Result<Vec<FragmentId>, CacheError> {
        let session = self
            .sessions
            .get(&sid)
            .ok_or(CacheError::UnknownSession(sid))?;
        if parts.is_empty() {
            return Ok(Vec::new());
        }
        let cur = session.tokens.len();
        let new_len = cur + parts.len();
        let mut new_seq = session.tokens.clone();
        new_seq.extend(parts.iter().map(|(t, _)| *t));
        let key = session.model_key.clone();

        // Planning phase: figure out how many fresh physical pages we need.
        let (mut matched, mut chain) = self.longest_match(&key, &new_seq);
        let mut needed = self.pages_needed(session, matched, &chain, new_len);
        if needed > self.free_pages {
            // Reclaim evictable pages, then re-plan: the reusable prefix may
            // have shrunk, which in turn may require more fresh pages.
            self.evict_unreferenced();
            let session = self.sessions.get(&sid).expect("session checked above");
            let (m2, c2) = self.longest_match(&key, &new_seq);
            matched = m2;
            chain = c2;
            needed = self.pages_needed(session, matched, &chain, new_len);
        }
        if needed > self.free_pages {
            return Err(CacheError::Allocation(AllocFailure {
                reusable_len: matched,
                missing_pages: needed - self.free_pages,
            }));
        }

        // Mutation phase: every allocation was pre-reserved, cannot fail.
        let PagedCache {
            pages,
            sessions,
            free_pages,
            next_page,
            next_fragment,
            page_size,
            ..
        } = self;
        let ps = *page_size;
        let session = sessions.get_mut(&sid).expect("session checked above");

        let mut frag_ids = Vec::with_capacity(parts.len());
        for _ in parts {
            frag_ids.push(FragmentId(*next_fragment));
            *next_fragment += 1;
        }
        session.tokens = new_seq;

        let mut pos = cur;

        // Step 1: top up the session's current partial page, if any.
        if pos < new_len && pos % ps != 0 {
            let slot_idx = pos / ps;
            let old_pid = session.page_table[slot_idx];
            let pid = if pages[&old_pid].ref_count > 1 {
                let new_pid = cow_page(pages, free_pages, next_page, old_pid);
                session.page_table[slot_idx] = new_pid;
                new_pid
            } else {
                old_pid
            };
            let end = ((pos / ps) + 1) * ps;
            let end = end.min(new_len);
            let page = pages.get_mut(&pid).expect("page must exist");
            write_slots(
                page,
                pos % ps,
                &parts[(pos - cur)..(end - cur)],
                &frag_ids[(pos - cur)..(end - cur)],
                pos,
                &mut session.pending,
            );
            pos = end;
        }

        // Step 2: link published pages for the reusable confirmed prefix.
        let link_upto = matched.min(new_len);
        if pos < link_upto {
            debug_assert_eq!(pos % ps, 0);
            let first_slot = pos / ps;
            let last_slot = link_upto.div_ceil(ps);
            for slot in first_slot..last_slot {
                let pid = chain[slot];
                pages.get_mut(&pid).expect("index page must exist").ref_count += 1;
                session.page_table.push(pid);
            }
            pos = link_upto;
        }

        // Step 3: write the remaining tokens into session-owned pages.
        while pos < new_len {
            let slot_idx = pos / ps;
            let pid = if slot_idx < session.page_table.len() {
                // A just-linked partial page; copy on write if still shared.
                let old_pid = session.page_table[slot_idx];
                if pages[&old_pid].ref_count > 1 {
                    let new_pid = cow_page(pages, free_pages, next_page, old_pid);
                    session.page_table[slot_idx] = new_pid;
                    new_pid
                } else {
                    old_pid
                }
            } else {
                let new_pid = alloc_page_id(free_pages, next_page);
                pages.insert(new_pid, Page::fresh());
                session.page_table.push(new_pid);
                new_pid
            };
            let end = ((slot_idx + 1) * ps).min(new_len);
            let page = pages.get_mut(&pid).expect("page must exist");
            write_slots(
                page,
                pos % ps,
                &parts[(pos - cur)..(end - cur)],
                &frag_ids[(pos - cur)..(end - cur)],
                pos,
                &mut session.pending,
            );
            pos = end;
        }

        Ok(frag_ids)
    }

    /// Confirm a fragment previously returned by `append*`.
    ///
    /// Confirmation extends the session's contiguous confirmed prefix and
    /// (re)publishes it into the cache index so other sessions with the same
    /// model key can reuse it. Late or stale confirmations (cancelled
    /// session, unknown or already-used handle) are ignored and never
    /// attribute pages — reclaimed or not — to a dead session.
    pub fn confirm(&mut self, sid: SessionId, frag: FragmentId) -> ConfirmOutcome {
        let ps = self.page_size;
        let PagedCache {
            pages,
            sessions,
            index,
            ..
        } = self;
        let Some(session) = sessions.get_mut(&sid) else {
            return ConfirmOutcome::Ignored;
        };
        let Some(pos) = session.pending.remove(&frag) else {
            return ConfirmOutcome::Ignored;
        };

        // Mark the slot in the session's *current* page for that position.
        let pid = session.page_table[pos / ps];
        if let Some(page) = pages.get_mut(&pid) {
            let off = pos % ps;
            if off < page.slots.len() {
                page.slots[off].confirmed = true;
                page.confirmed = page.slots.iter().take_while(|s| s.confirmed).count();
            }
        }

        // Extend the contiguous confirmed prefix.
        while session.confirmed_prefix < session.tokens.len() {
            let p = session.confirmed_prefix;
            let pid = session.page_table[p / ps];
            let ok = pages.get(&pid).is_some_and(|pg| {
                let off = p % ps;
                off < pg.slots.len() && pg.slots[off].confirmed
            });
            if !ok {
                break;
            }
            session.confirmed_prefix += 1;
        }

        // (Re)publish the confirmed prefix into the cache index.
        let cp = session.confirmed_prefix;
        if cp > 0 {
            let key = session.model_key.clone();
            let root = index.entry(key).or_default();
            let mut node = &mut *root;
            for i in 0..cp {
                let pid = session.page_table[i / ps];
                node = node.children.entry(session.tokens[i]).or_default();
                node.page = Some(pid);
            }
            let last_slot = cp.div_ceil(ps);
            for slot in 0..last_slot {
                let pid = session.page_table[slot];
                if let Some(pg) = pages.get_mut(&pid) {
                    pg.in_index = true;
                }
            }
        }
        ConfirmOutcome::Confirmed
    }

    /// Cancel a session and release its page references. Published pages it
    /// leaves behind stay reusable until evicted; unpublished ones are freed.
    pub fn cancel_session(&mut self, sid: SessionId) -> Result<(), CacheError> {
        let Some(session) = self.sessions.remove(&sid) else {
            return Err(CacheError::UnknownSession(sid));
        };
        for pid in session.page_table {
            release_page(&mut self.pages, &mut self.free_pages, pid);
        }
        Ok(())
    }

    /// Rebind a session to a different model version / positional config.
    /// Its cached pages are released and its token sequence is reset (KV
    /// state is not transferable across model keys); other sessions and the
    /// published index are unaffected.
    pub fn switch_model(
        &mut self,
        sid: SessionId,
        model_version: &str,
        pos_config: &str,
    ) -> Result<(), CacheError> {
        let Some(session) = self.sessions.get_mut(&sid) else {
            return Err(CacheError::UnknownSession(sid));
        };
        let table = std::mem::take(&mut session.page_table);
        for pid in table {
            release_page(&mut self.pages, &mut self.free_pages, pid);
        }
        session.tokens.clear();
        session.confirmed_prefix = 0;
        session.pending.clear();
        session.model_key = ModelKey::new(model_version, pos_config);
        Ok(())
    }

    /// Reclaim every page that is published in the cache index and referenced
    /// by no session. Returns the number of reclaimed pages. Index entries
    /// pointing at reclaimed pages are invalidated lazily on next lookup.
    pub fn evict_unreferenced(&mut self) -> usize {
        let evict: Vec<PageId> = self
            .pages
            .iter()
            .filter(|(_, p)| p.ref_count == 0)
            .map(|(&id, _)| id)
            .collect();
        let n = evict.len();
        for id in evict {
            self.pages.remove(&id);
        }
        self.free_pages += n;
        n
    }

    /// Read `len` tokens of the session's own prefix from its pages.
    pub fn read_prefix(&self, sid: SessionId, len: usize) -> Result<Vec<Token>, CacheError> {
        let session = self
            .sessions
            .get(&sid)
            .ok_or(CacheError::UnknownSession(sid))?;
        if len > session.tokens.len() {
            return Err(CacheError::InvalidLength {
                requested: len,
                available: session.tokens.len(),
            });
        }
        let ps = self.page_size;
        let mut out = Vec::with_capacity(len);
        for i in 0..len {
            let pid = session.page_table[i / ps];
            let page = self.pages.get(&pid).ok_or(CacheError::UnknownPage(pid))?;
            out.push(page.slots[i % ps].token);
        }
        Ok(out)
    }

    /// Inspect a session's page map.
    pub fn page_map(&self, sid: SessionId) -> Result<Vec<PageMapEntry>, CacheError> {
        let session = self
            .sessions
            .get(&sid)
            .ok_or(CacheError::UnknownSession(sid))?;
        let ps = self.page_size;
        Ok(session
            .page_table
            .iter()
            .enumerate()
            .map(|(slot, &pid)| {
                let p = &self.pages[&pid];
                let start = slot * ps;
                PageMapEntry {
                    page_id: pid,
                    start,
                    len: session.tokens.len().saturating_sub(start).min(ps),
                    confirmed: p.confirmed,
                    shared: p.ref_count > 1,
                    ref_count: p.ref_count,
                }
            })
            .collect())
    }

    /// Inspect the in-page fragments of a physical page.
    pub fn page_fragments(&self, pid: PageId) -> Option<Vec<FragmentView>> {
        self.pages.get(&pid).map(|p| {
            p.slots
                .iter()
                .enumerate()
                .map(|(offset, s)| FragmentView {
                    offset,
                    token: s.token,
                    confirmed: s.confirmed,
                    data: s.data.clone(),
                })
                .collect()
        })
    }

    /// Global pool occupancy snapshot.
    pub fn occupancy(&self) -> Occupancy {
        Occupancy {
            total_pages: self.total_pages,
            used_pages: self.pages.len(),
            free_pages: self.free_pages,
            evictable_pages: self.pages.values().filter(|p| p.ref_count == 0).count(),
            active_sessions: self.sessions.len(),
        }
    }

    /// Longest confirmed prefix of `tokens` in the model's index.
    /// Returns the matched length and one page id per page slot.
    fn longest_match(&self, key: &ModelKey, tokens: &[Token]) -> (usize, Vec<PageId>) {
        let ps = self.page_size;
        let mut matched = 0;
        let mut chain: Vec<PageId> = Vec::new();
        let Some(root) = self.index.get(key) else {
            return (0, chain);
        };
        let mut node = root;
        for (i, tok) in tokens.iter().enumerate() {
            let Some(child) = node.children.get(tok) else { break };
            let Some(pid) = child.page else { break };
            // Lazily skip index entries whose page was reclaimed.
            if !self.pages.contains_key(&pid) {
                break;
            }
            let slot = i / ps;
            if chain.len() == slot {
                chain.push(pid);
            }
            matched = i + 1;
            node = child;
        }
        (matched, chain)
    }

    /// How many fresh physical pages an append would need, mirroring the
    /// mutation phase step by step (copy-on-write of shared partial pages
    /// plus brand-new pages for the tail).
    fn pages_needed(
        &self,
        session: &Session,
        matched: usize,
        chain: &[PageId],
        new_len: usize,
    ) -> usize {
        let ps = self.page_size;
        let cur = session.tokens.len();
        let mut needed = 0usize;
        let mut pos = cur;

        // Step 1: topping up the current partial page may force a COW.
        if pos < new_len && pos % ps != 0 {
            let pid = session.page_table[pos / ps];
            if self.pages[&pid].ref_count > 1 {
                needed += 1;
            }
            pos = (pos / ps + 1) * ps;
        }

        // Step 2: linked pages need no allocation.
        let link_upto = matched.min(new_len);
        if pos < link_upto {
            pos = link_upto;
        }

        // Step 3: fresh tail pages, plus a possible COW of a linked partial.
        if pos < new_len {
            if pos % ps != 0 {
                let pid = chain[pos / ps];
                // After linking, our reference makes it `old + 1`; writing in
                // place is only possible if nobody else holds the page.
                if self.pages[&pid].ref_count > 0 {
                    needed += 1;
                }
                let boundary = (pos / ps + 1) * ps;
                if boundary < new_len {
                    needed += (new_len - boundary).div_ceil(ps);
                }
            } else {
                needed += (new_len - pos).div_ceil(ps);
            }
        }
        needed
    }
}
