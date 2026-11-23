//! Copy-on-write storage block reclamation module.
//!
//! A fixed-capacity block store where blocks may reference other blocks,
//! forming trees rooted at named snapshots. In-flight writes stage new
//! blocks (reserve -> write content -> publish root). Readers pin a
//! snapshot root so that deleting the snapshot name does not interrupt
//! them. Reclamation only reuses blocks that are no longer reachable from
//! any published root, reader pin, or staging write, and reports why the
//! remaining blocks are held. State can be checkpointed and recovered;
//! recovery distinguishes published roots, continuable staging writes and
//! orphaned reservations. Cancelling a write is idempotent and only ever
//! releases that write's own reservations.

use std::collections::{BTreeMap, BTreeSet};
use std::fmt;

/// Identifier of a physical block within the store.
pub type BlockId = u32;
/// Identifier of a write transaction.
pub type WriteId = u64;
/// Identifier of an open reader.
pub type ReaderId = u64;

/// Content of a block: opaque payload plus references to child blocks.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Block {
    pub data: Vec<u8>,
    pub refs: Vec<BlockId>,
}

/// Errors returned by store operations.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Error {
    /// No free block is available for reservation.
    OutOfSpace,
    /// The named snapshot does not exist.
    NoSuchSnapshot(String),
    /// The write transaction id is unknown.
    NoSuchTxn(WriteId),
    /// The reader id is unknown (never opened or already closed).
    NoSuchReader(ReaderId),
    /// The block id is outside the store capacity.
    NoSuchBlock(BlockId),
    /// The block is not reserved by the given write transaction.
    BlockNotReservedBy(BlockId, WriteId),
    /// A reference points at a block that cannot be referenced
    /// (free, out of range, or owned by another write).
    InvalidRef(BlockId),
    /// The write transaction is not active (committed or cancelled).
    TxnNotActive(WriteId),
    /// The write transaction was already committed and cannot be cancelled.
    TxnCommitted(WriteId),
    /// A publish was attempted without a written root block.
    EmptyWrite,
    /// A snapshot name is empty or contains whitespace.
    BadName,
    /// Checkpoint bytes are malformed or inconsistent.
    BadCheckpoint(String),
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::OutOfSpace => write!(f, "no free block available"),
            Error::NoSuchSnapshot(n) => write!(f, "no such snapshot: {n}"),
            Error::NoSuchTxn(t) => write!(f, "no such write transaction: {t}"),
            Error::NoSuchReader(r) => write!(f, "no such reader: {r}"),
            Error::NoSuchBlock(b) => write!(f, "no such block: {b}"),
            Error::BlockNotReservedBy(b, t) => {
                write!(f, "block {b} is not reserved by write {t}")
            }
            Error::InvalidRef(b) => write!(f, "invalid block reference: {b}"),
            Error::TxnNotActive(t) => write!(f, "write transaction {t} is not active"),
            Error::TxnCommitted(t) => write!(f, "write transaction {t} already committed"),
            Error::EmptyWrite => write!(f, "write has no written root block"),
            Error::BadName => write!(f, "snapshot name is empty or contains whitespace"),
            Error::BadCheckpoint(m) => write!(f, "bad checkpoint: {m}"),
        }
    }
}

impl std::error::Error for Error {}

/// Physical state of one slot in the store.
#[derive(Debug, Clone, PartialEq, Eq)]
enum Slot {
    Free,
    /// Reserved by an in-flight write; `content` appears once written.
    Reserved { txn: WriteId, content: Option<Block> },
    /// Published and potentially reachable from roots/pins.
    Used { content: Block },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum TxnState {
    Active,
    Committed,
    Cancelled,
}

#[derive(Debug, Clone)]
struct WriteTxn {
    state: TxnState,
    /// Blocks reserved by this write, in reservation order.
    reserved: Vec<BlockId>,
    /// Staging root chosen once content is written; set at publish time
    /// from the recorded candidate.
    root: Option<BlockId>,
}

#[derive(Debug, Clone)]
struct Reader {
    root: BlockId,
}

/// Why a block cannot be reclaimed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Reason {
    /// Block is the root of a published snapshot.
    SnapshotRoot(String),
    /// Block is reachable from a published snapshot's root.
    ReachableFromSnapshot(String),
    /// Block is the pinned root of an open reader.
    ReaderPin(ReaderId),
    /// Block is reachable from an open reader's pinned root.
    ReachableFromReader(ReaderId),
    /// Block is reserved by an in-flight write.
    ReservedByWrite(WriteId),
    /// Block is the staging root candidate of an in-flight write.
    StagingRoot(WriteId),
}

/// Outcome of a garbage-collection pass.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GcReport {
    /// Blocks that were used, unreachable, and have been freed.
    pub reclaimed: Vec<BlockId>,
    /// All blocks allocatable after the pass (previously free + reclaimed).
    pub free: Vec<BlockId>,
    /// Blocks that remain held, each with the reasons why.
    pub held: Vec<(BlockId, Vec<Reason>)>,
}

/// Classification produced by recovery.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RecoveryReport {
    /// Names of published snapshot roots found in the checkpoint.
    pub published: Vec<String>,
    /// In-flight writes that may be continued (staging roots/reservations).
    pub staging_writes: Vec<WriteId>,
    /// Reserved blocks whose owning write is gone or no longer active;
    /// they have been returned to the free pool during recovery.
    pub orphaned_reservations: Vec<BlockId>,
}

/// Copy-on-write block store with a fixed number of physical blocks.
#[derive(Debug, Clone)]
pub struct Store {
    slots: Vec<Slot>,
    snapshots: BTreeMap<String, BlockId>,
    txns: BTreeMap<WriteId, WriteTxn>,
    readers: BTreeMap<ReaderId, Reader>,
    next_txn: WriteId,
    next_reader: ReaderId,
}

impl Store {
    /// Create an empty store with `capacity` physical blocks, all free.
    pub fn new(capacity: usize) -> Self {
        Store {
            slots: vec![Slot::Free; capacity],
            snapshots: BTreeMap::new(),
            txns: BTreeMap::new(),
            readers: BTreeMap::new(),
            next_txn: 1,
            next_reader: 1,
        }
    }

    /// Total number of physical blocks.
    pub fn capacity(&self) -> usize {
        self.slots.len()
    }

    // ---- writes -------------------------------------------------------

    /// Begin a new write transaction and return its id.
    pub fn begin_write(&mut self) -> WriteId {
        let id = self.next_txn;
        self.next_txn += 1;
        self.txns.insert(
            id,
            WriteTxn { state: TxnState::Active, reserved: Vec::new(), root: None },
        );
        id
    }

    fn active_txn(&self, txn: WriteId) -> Result<&WriteTxn, Error> {
        let t = self.txns.get(&txn).ok_or(Error::NoSuchTxn(txn))?;
        if t.state != TxnState::Active {
            return Err(Error::TxnNotActive(txn));
        }
        Ok(t)
    }

    /// Reserve the lowest-numbered free physical block for `txn`.
    pub fn reserve(&mut self, txn: WriteId) -> Result<BlockId, Error> {
        self.active_txn(txn)?;
        let pos = self
            .slots
            .iter()
            .position(|s| matches!(s, Slot::Free))
            .ok_or(Error::OutOfSpace)?;
        let id = pos as BlockId;
        self.slots[pos] = Slot::Reserved { txn, content: None };
        self.txns
            .get_mut(&txn)
            .expect("txn checked above")
            .reserved
            .push(id);
        Ok(id)
    }

    /// Write content into a block previously reserved by `txn`.
    ///
    /// Every entry of `refs` must denote a published (`Used`) block or a
    /// block reserved by the same transaction, so a staged tree can never
    /// point at a free block or at another write's private blocks.
    pub fn write_block(
        &mut self,
        txn: WriteId,
        block: BlockId,
        data: Vec<u8>,
        refs: Vec<BlockId>,
    ) -> Result<(), Error> {
        self.active_txn(txn)?;
        let idx = block as usize;
        if idx >= self.slots.len() {
            return Err(Error::NoSuchBlock(block));
        }
        match &self.slots[idx] {
            Slot::Reserved { txn: owner, .. } if *owner == txn => {}
            _ => return Err(Error::BlockNotReservedBy(block, txn)),
        }
        for &r in &refs {
            let ri = r as usize;
            if ri >= self.slots.len() {
                return Err(Error::InvalidRef(r));
            }
            match &self.slots[ri] {
                Slot::Used { .. } => {}
                Slot::Reserved { txn: owner, .. } if *owner == txn => {}
                _ => return Err(Error::InvalidRef(r)),
            }
        }
        self.slots[idx] = Slot::Reserved { txn, content: Some(Block { data, refs }) };
        Ok(())
    }

    /// Publish the staged tree rooted at `root` under snapshot `name`.
    ///
    /// All of the write's reserved blocks become used atomically; the
    /// transaction is then committed and cannot be cancelled any more.
    pub fn publish(&mut self, txn: WriteId, name: &str, root: BlockId) -> Result<(), Error> {
        if name.is_empty() || name.chars().any(char::is_whitespace) {
            return Err(Error::BadName);
        }
        let reserved = self.active_txn(txn)?.reserved.clone();
        if !reserved.contains(&root) {
            return Err(Error::BlockNotReservedBy(root, txn));
        }
        // Every reserved block must have written content before publish.
        for &b in &reserved {
            match &self.slots[b as usize] {
                Slot::Reserved { content: Some(_), .. } => {}
                _ => return Err(Error::EmptyWrite),
            }
        }
        for &b in &reserved {
            let slot = &mut self.slots[b as usize];
            if let Slot::Reserved { content, .. } = slot {
                let content = content.take().expect("reserved block must be written");
                *slot = Slot::Used { content };
            }
        }
        self.snapshots.insert(name.to_string(), root);
        let t = self.txns.get_mut(&txn).expect("txn checked above");
        t.state = TxnState::Committed;
        t.root = Some(root);
        Ok(())
    }

    /// Cancel a write, releasing exactly the blocks it reserved.
    ///
    /// Cancelling is idempotent: a repeated cancel of an already-cancelled
    /// write is a no-op and never frees anything twice. Cancelling a
    /// committed write is an error.
    pub fn cancel(&mut self, txn: WriteId) -> Result<(), Error> {
        let t = self.txns.get(&txn).ok_or(Error::NoSuchTxn(txn))?;
        match t.state {
            TxnState::Cancelled => return Ok(()),
            TxnState::Committed => return Err(Error::TxnCommitted(txn)),
            TxnState::Active => {}
        }
        let reserved = t.reserved.clone();
        for b in reserved {
            if let Slot::Reserved { txn: owner, .. } = self.slots[b as usize] {
                if owner == txn {
                    self.slots[b as usize] = Slot::Free;
                }
            }
        }
        let t = self.txns.get_mut(&txn).expect("txn checked above");
        t.state = TxnState::Cancelled;
        t.reserved.clear();
        t.root = None;
        Ok(())
    }

    // ---- snapshots and readers ---------------------------------------

    /// Open a reader on `snapshot`, pinning its current root. The pin
    /// survives deletion of the snapshot name.
    pub fn open_reader(&mut self, snapshot: &str) -> Result<ReaderId, Error> {
        let root = *self
            .snapshots
            .get(snapshot)
            .ok_or_else(|| Error::NoSuchSnapshot(snapshot.to_string()))?;
        let id = self.next_reader;
        self.next_reader += 1;
        self.readers.insert(id, Reader { root });
        Ok(id)
    }

    /// Root block pinned by `reader`.
    pub fn reader_root(&self, reader: ReaderId) -> Result<BlockId, Error> {
        self.readers
            .get(&reader)
            .map(|r| r.root)
            .ok_or(Error::NoSuchReader(reader))
    }

    /// Read a block through `reader`. The block must be published and
    /// reachable from the reader's pinned root.
    pub fn read_block(&self, reader: ReaderId, block: BlockId) -> Result<&Block, Error> {
        let r = self.readers.get(&reader).ok_or(Error::NoSuchReader(reader))?;
        if block as usize >= self.slots.len() {
            return Err(Error::NoSuchBlock(block));
        }
        if !self.reachable_from(r.root).contains(&block) {
            return Err(Error::InvalidRef(block));
        }
        match &self.slots[block as usize] {
            Slot::Used { content } => Ok(content),
            _ => Err(Error::InvalidRef(block)),
        }
    }

    /// Close a reader, dropping its pin.
    pub fn close_reader(&mut self, reader: ReaderId) -> Result<(), Error> {
        self.readers
            .remove(&reader)
            .map(|_| ())
            .ok_or(Error::NoSuchReader(reader))
    }

    /// Delete a snapshot name. Open readers keep their pinned root and
    /// can finish reading.
    pub fn delete_snapshot(&mut self, name: &str) -> Result<(), Error> {
        self.snapshots
            .remove(name)
            .map(|_| ())
            .ok_or_else(|| Error::NoSuchSnapshot(name.to_string()))
    }

    /// Current published snapshot roots.
    pub fn snapshots(&self) -> &BTreeMap<String, BlockId> {
        &self.snapshots
    }

    // ---- reachability, reclamation ------------------------------------

    fn reachable_from(&self, root: BlockId) -> BTreeSet<BlockId> {
        let mut seen = BTreeSet::new();
        let mut stack = vec![root];
        while let Some(b) = stack.pop() {
            if b as usize >= self.slots.len() || !seen.insert(b) {
                continue;
            }
            let refs = match &self.slots[b as usize] {
                Slot::Used { content } => &content.refs,
                Slot::Reserved { content: Some(c), .. } => &c.refs,
                _ => continue,
            };
            stack.extend(refs.iter().copied());
        }
        seen
    }

    /// Compute, for every non-free block, the reasons it is held.
    fn held_reasons(&self) -> BTreeMap<BlockId, Vec<Reason>> {
        let mut held: BTreeMap<BlockId, Vec<Reason>> = BTreeMap::new();
        let add = |b: BlockId, r: Reason, held: &mut BTreeMap<BlockId, Vec<Reason>>| {
            let reasons = held.entry(b).or_default();
            if !reasons.contains(&r) {
                reasons.push(r);
            }
        };
        for (name, &root) in &self.snapshots {
            for b in self.reachable_from(root) {
                let reason = if b == root {
                    Reason::SnapshotRoot(name.clone())
                } else {
                    Reason::ReachableFromSnapshot(name.clone())
                };
                add(b, reason, &mut held);
            }
        }
        for (&rid, reader) in &self.readers {
            for b in self.reachable_from(reader.root) {
                let reason = if b == reader.root {
                    Reason::ReaderPin(rid)
                } else {
                    Reason::ReachableFromReader(rid)
                };
                add(b, reason, &mut held);
            }
        }
        for (&tid, txn) in &self.txns {
            if txn.state != TxnState::Active {
                continue;
            }
            for &b in &txn.reserved {
                add(b, Reason::ReservedByWrite(tid), &mut held);
                if txn.root == Some(b) {
                    add(b, Reason::StagingRoot(tid), &mut held);
                }
            }
        }
        held
    }

    /// Inspect allocatable state without mutating the store: the free
    /// blocks and, for every other block, why it cannot be reclaimed.
    pub fn allocatable(&self) -> GcReport {
        let held = self.held_reasons();
        let mut free = Vec::new();
        let mut held_list = Vec::new();
        for (i, slot) in self.slots.iter().enumerate() {
            let id = i as BlockId;
            match slot {
                Slot::Free => free.push(id),
                _ => held_list.push((
                    id,
                    held.get(&id).cloned().unwrap_or_default(),
                )),
            }
        }
        GcReport { reclaimed: Vec::new(), free, held: held_list }
    }

    /// Reclaim every used block that is no longer reachable from any
    /// published root, reader pin, or staging write. Reserved blocks of
    /// active writes are never touched.
    pub fn gc(&mut self) -> GcReport {
        let held = self.held_reasons();
        let mut reclaimed = Vec::new();
        for i in 0..self.slots.len() {
            let id = i as BlockId;
            if matches!(self.slots[i], Slot::Used { .. }) && !held.contains_key(&id) {
                self.slots[i] = Slot::Free;
                reclaimed.push(id);
            }
        }
        let mut report = self.allocatable();
        report.reclaimed = reclaimed;
        report
    }

    // ---- persistence ---------------------------------------------------

    /// Serialize the full store state (blocks, roots, write transactions,
    /// reader pins) into a checkpoint byte string.
    pub fn checkpoint(&self) -> Vec<u8> {
        let mut out = String::from("COWGC/1\n");
        out.push_str(&format!("capacity {}\n", self.slots.len()));
        out.push_str(&format!("next_txn {}\n", self.next_txn));
        out.push_str(&format!("next_reader {}\n", self.next_reader));
        for (i, slot) in self.slots.iter().enumerate() {
            match slot {
                Slot::Free => out.push_str(&format!("block {i} free\n")),
                Slot::Reserved { txn, content } => {
                    out.push_str(&format!("block {i} reserved {txn}"));
                    push_content(&mut out, content);
                    out.push('\n');
                }
                Slot::Used { content } => {
                    out.push_str(&format!("block {i} used"));
                    push_content(&mut out, &Some(content.clone()));
                    out.push('\n');
                }
            }
        }
        for (name, root) in &self.snapshots {
            out.push_str(&format!("snapshot {name} {root}\n"));
        }
        for (id, txn) in &self.txns {
            let state = match txn.state {
                TxnState::Active => "active",
                TxnState::Committed => "committed",
                TxnState::Cancelled => "cancelled",
            };
            let reserved = if txn.reserved.is_empty() {
                "-".to_string()
            } else {
                txn.reserved.iter().map(u32::to_string).collect::<Vec<_>>().join(",")
            };
            let root = txn.root.map_or_else(|| "-".to_string(), |r| r.to_string());
            out.push_str(&format!("txn {id} {state} reserved {reserved} root {root}\n"));
        }
        for (id, reader) in &self.readers {
            out.push_str(&format!("reader {id} {}\n", reader.root));
        }
        out.push_str("END\n");
        out.into_bytes()
    }

    /// Rebuild a store from checkpoint bytes, classifying what was found:
    /// published roots, continuable staging writes, and orphaned
    /// reservations (reserved blocks whose write is gone or no longer
    /// active), which are returned to the free pool.
    pub fn recover(bytes: &[u8]) -> Result<(Store, RecoveryReport), Error> {
        let text = std::str::from_utf8(bytes)
            .map_err(|e| Error::BadCheckpoint(format!("not utf-8: {e}")))?;
        let bad = |m: &str| Error::BadCheckpoint(m.to_string());
        let mut lines = text.lines();
        if lines.next() != Some("COWGC/1") {
            return Err(bad("missing COWGC/1 header"));
        }

        let mut capacity = None;
        let mut next_txn = None;
        let mut next_reader = None;
        let mut slots: Vec<Option<Slot>> = Vec::new();
        let mut snapshots = BTreeMap::new();
        let mut txns: BTreeMap<WriteId, WriteTxn> = BTreeMap::new();
        let mut readers = BTreeMap::new();
        let mut saw_end = false;

        for line in lines {
            let tok: Vec<&str> = line.split_whitespace().collect();
            match tok.as_slice() {
                ["END"] => {
                    saw_end = true;
                    break;
                }
                ["capacity", n] => {
                    let n: usize = n.parse().map_err(|_| bad("bad capacity"))?;
                    capacity = Some(n);
                    slots = vec![None; n];
                }
                ["next_txn", n] => {
                    next_txn = Some(n.parse().map_err(|_| bad("bad next_txn"))?);
                }
                ["next_reader", n] => {
                    next_reader = Some(n.parse().map_err(|_| bad("bad next_reader"))?);
                }
                _ => {
                    let Some(&kind) = tok.first() else { return Err(bad("empty line")) };
                    match kind {
                        "block" => parse_block_line(&tok, &mut slots)?,
                        "snapshot" => {
                            let ["snapshot", name, root] = tok.as_slice() else {
                                return Err(bad("bad snapshot line"));
                            };
                            let root: BlockId =
                                root.parse().map_err(|_| bad("bad snapshot root"))?;
                            snapshots.insert(name.to_string(), root);
                        }
                        "txn" => {
                            let (id, txn) = parse_txn_line(&tok)?;
                            txns.insert(id, txn);
                        }
                        "reader" => {
                            let ["reader", id, root] = tok.as_slice() else {
                                return Err(bad("bad reader line"));
                            };
                            let id: ReaderId =
                                id.parse().map_err(|_| bad("bad reader id"))?;
                            let root: BlockId =
                                root.parse().map_err(|_| bad("bad reader root"))?;
                            readers.insert(id, Reader { root });
                        }
                        other => return Err(bad(&format!("unknown record: {other}"))),
                    }
                }
            }
        }
        if !saw_end {
            return Err(bad("missing END marker"));
        }
        let capacity = capacity.ok_or_else(|| bad("missing capacity"))?;
        let mut store = Store {
            slots: {
                let mut v = Vec::with_capacity(capacity);
                for (i, s) in slots.into_iter().enumerate() {
                    v.push(s.ok_or_else(|| bad(&format!("missing block record {i}")))?);
                }
                v
            },
            snapshots,
            txns,
            readers,
            next_txn: next_txn.ok_or_else(|| bad("missing next_txn"))?,
            next_reader: next_reader.ok_or_else(|| bad("missing next_reader"))?,
        };
        store.validate()?;

        // Classify and clean up orphaned reservations.
        let mut report = RecoveryReport {
            published: store.snapshots.keys().cloned().collect(),
            staging_writes: store
                .txns
                .iter()
                .filter(|(_, t)| t.state == TxnState::Active)
                .map(|(id, _)| *id)
                .collect(),
            orphaned_reservations: Vec::new(),
        };
        for i in 0..store.slots.len() {
            let orphan = match store.slots[i] {
                Slot::Reserved { txn, .. } => match store.txns.get(&txn) {
                    Some(t) if t.state == TxnState::Active => false,
                    _ => true,
                },
                _ => false,
            };
            if orphan {
                store.slots[i] = Slot::Free;
                report.orphaned_reservations.push(i as BlockId);
            }
        }
        Ok((store, report))
    }

    /// Structural consistency checks applied after recovery.
    fn validate(&self) -> Result<(), Error> {
        let bad = |m: String| Error::BadCheckpoint(m);
        let in_range = |b: BlockId| (b as usize) < self.slots.len();
        for (i, slot) in self.slots.iter().enumerate() {
            let content = match slot {
                Slot::Free => continue,
                Slot::Reserved { content: Some(c), .. } => c,
                Slot::Used { content } => content,
                Slot::Reserved { content: None, .. } => continue,
            };
            for &r in &content.refs {
                if !in_range(r) {
                    return Err(bad(format!("block {i} references out-of-range block {r}")));
                }
                if matches!(self.slots[r as usize], Slot::Free) {
                    return Err(bad(format!("block {i} references free block {r}")));
                }
            }
        }
        for (name, &root) in &self.snapshots {
            if !in_range(root) || !matches!(self.slots[root as usize], Slot::Used { .. }) {
                return Err(bad(format!("snapshot {name} root {root} is not a used block")));
            }
        }
        for (&rid, reader) in &self.readers {
            if !in_range(reader.root)
                || !matches!(self.slots[reader.root as usize], Slot::Used { .. })
            {
                return Err(bad(format!("reader {rid} pins non-used block {}", reader.root)));
            }
        }
        for (&tid, txn) in &self.txns {
            for &b in &txn.reserved {
                if !in_range(b) {
                    return Err(bad(format!("txn {tid} reserves out-of-range block {b}")));
                }
            }
        }
        Ok(())
    }
}

fn push_content(out: &mut String, content: &Option<Block>) {
    match content {
        None => out.push_str(" -"),
        Some(b) => {
            out.push_str(&format!(" data {}", hex_encode(&b.data)));
            let refs = if b.refs.is_empty() {
                "-".to_string()
            } else {
                b.refs.iter().map(u32::to_string).collect::<Vec<_>>().join(",")
            };
            out.push_str(&format!(" refs {refs}"));
        }
    }
}

fn parse_block_line(tok: &[&str], slots: &mut [Option<Slot>]) -> Result<(), Error> {
    let bad = |m: &str| Error::BadCheckpoint(m.to_string());
    if tok.len() < 3 {
        return Err(bad("bad block line"));
    }
    let id: usize = tok[1].parse().map_err(|_| bad("bad block id"))?;
    if id >= slots.len() {
        return Err(bad("block id out of range"));
    }
    let slot = match tok[2] {
        "free" => Slot::Free,
        "reserved" => {
            let txn: WriteId = tok
                .get(3)
                .ok_or_else(|| bad("reserved block missing txn"))?
                .parse()
                .map_err(|_| bad("bad reserved txn"))?;
            Slot::Reserved { txn, content: parse_content(&tok[4..])? }
        }
        "used" => {
            let content =
                parse_content(&tok[3..])?.ok_or_else(|| bad("used block without content"))?;
            Slot::Used { content }
        }
        _ => return Err(bad("unknown block state")),
    };
    slots[id] = Some(slot);
    Ok(())
}

fn parse_content(tok: &[&str]) -> Result<Option<Block>, Error> {
    let bad = |m: &str| Error::BadCheckpoint(m.to_string());
    match tok {
        [] | ["-"] => Ok(None),
        ["data", hex, "refs", list] => {
            let data = hex_decode(hex)?;
            let refs = parse_id_list(list)?;
            Ok(Some(Block { data, refs }))
        }
        _ => Err(bad("bad block content")),
    }
}

fn parse_txn_line(tok: &[&str]) -> Result<(WriteId, WriteTxn), Error> {
    let bad = |m: &str| Error::BadCheckpoint(m.to_string());
    // txn <id> <state> reserved <a,b,c> root <r|->
    let ["txn", id, state, "reserved", list, "root", root] = tok else {
        return Err(bad("bad txn line"));
    };
    let id: WriteId = id.parse().map_err(|_| bad("bad txn id"))?;
    let state = match *state {
        "active" => TxnState::Active,
        "committed" => TxnState::Committed,
        "cancelled" => TxnState::Cancelled,
        _ => return Err(bad("bad txn state")),
    };
    let reserved = parse_id_list(list)?;
    let root = if *root == "-" {
        None
    } else {
        Some(root.parse().map_err(|_| bad("bad txn root"))?)
    };
    Ok((id, WriteTxn { state, reserved, root }))
}

fn parse_id_list(list: &str) -> Result<Vec<BlockId>, Error> {
    if list == "-" || list.is_empty() {
        return Ok(Vec::new());
    }
    list.split(',')
        .map(|s| {
            s.parse::<BlockId>()
                .map_err(|_| Error::BadCheckpoint(format!("bad id list: {list}")))
        })
        .collect()
}

fn hex_encode(data: &[u8]) -> String {
    let mut s = String::with_capacity(data.len() * 2);
    for b in data {
        s.push(char::from_digit((b >> 4) as u32, 16).unwrap());
        s.push(char::from_digit((b & 0xf) as u32, 16).unwrap());
    }
    s
}

fn hex_decode(s: &str) -> Result<Vec<u8>, Error> {
    let bad = || Error::BadCheckpoint(format!("bad hex: {s}"));
    if s.len() % 2 != 0 {
        return Err(bad());
    }
    (0..s.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&s[i..i + 2], 16).map_err(|_| bad()))
        .collect()
}
