//! Async I/O runtime module: slot/generation request lifecycle with buffer loans.
//!
//! # Lifecycle
//!
//! * A request occupies a reusable **slot**, distinguished by a **generation**
//!   counter that is bumped every time the slot is acquired.
//! * One request may produce **multiple completions**; each completion loans
//!   one buffer to the consumer (`Event::Completion`).
//! * A **cancel receipt** (`cancel`) may arrive *before* the request's final
//!   completion. After cancellation, buffers the device still returns are
//!   **reclaimed** internally (`Event::Reclaimed`) and never handed to the
//!   consumer.
//! * Buffers already handed out are released **only** when the consumer
//!   returns them (`return_buffer`). Duplicate completions or duplicate
//!   returns never add extra capacity.
//! * A slot becomes reusable only after the request's **final completion**
//!   (`finalize`) has been received, i.e. the original request no longer
//!   accepts new results. Late events carrying an old generation are ignored
//!   and never advance the new request occupying the slot.
//!
//! All capacity changes are traceable through the event log (`log`):
//! capacity decreases only on `Completion` (a loan) and increases only on
//! `Returned` (an actual consumer return).

/// Handle identifying one request: the slot it occupies plus the generation
/// of that slot at acquisition time.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct RequestHandle {
    pub slot: usize,
    pub generation: u64,
}

/// Lifecycle state of a slot.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SlotState {
    /// No request occupies the slot; it may be acquired by `submit`.
    Free,
    /// A request is in flight and accepts new completions.
    Active,
    /// A cancel receipt arrived; the final completion is still pending.
    /// Further device buffers are reclaimed instead of loaned out.
    Cancelled,
}

/// Visible events emitted by the runtime, in order.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Event {
    /// A new request acquired a slot; the generation is verifiable here.
    SlotAcquired { slot: usize, generation: u64 },
    /// A buffer was loaned to the consumer (capacity decreased by one).
    Completion { handle: RequestHandle, buffer: usize },
    /// The cancel receipt for a request was recorded.
    CancelAck { handle: RequestHandle },
    /// A buffer arrived after cancellation and was reclaimed internally
    /// (never loaned, capacity unchanged).
    Reclaimed { handle: RequestHandle, buffer: usize },
    /// The request's final completion arrived; the slot is now free.
    Finalized { slot: usize, generation: u64 },
    /// The consumer returned a loaned buffer (capacity increased by one).
    Returned { buffer: usize },
    /// An event carried a stale (old) generation and was ignored; it does not
    /// advance the current request.
    StaleIgnored { handle: RequestHandle, kind: &'static str },
    /// A duplicate completion / return / cancel / finalize — or a late event
    /// for an already-finalized request of the current generation — was
    /// ignored; balances are unchanged.
    DuplicateIgnored { kind: &'static str },
    /// An event referenced an out-of-range slot or buffer and was ignored.
    InvalidIgnored { kind: &'static str },
}

/// One step of an input script: application stages and device events.
#[derive(Debug, Clone, Copy)]
pub enum Step {
    /// Application submits a new request.
    Submit,
    /// Device delivers one completion segment carrying `buffer` for `handle`.
    Complete { handle: RequestHandle, buffer: usize },
    /// Device acknowledges cancellation of `handle`.
    Cancel { handle: RequestHandle },
    /// Device delivers the final completion of `handle`.
    Finalize { handle: RequestHandle },
    /// Consumer returns a previously loaned `buffer`.
    Return { buffer: usize },
}

struct Slot {
    generation: u64,
    state: SlotState,
}

/// The async I/O runtime: a fixed set of reusable slots plus a fixed pool of
/// buffers whose loans are tracked individually.
pub struct IoRuntime {
    slots: Vec<Slot>,
    /// `loaned[b]` is true while buffer `b` is held by the consumer.
    loaned: Vec<bool>,
    free_capacity: usize,
    log: Vec<Event>,
}

impl IoRuntime {
    /// Create a runtime with `num_slots` reusable slots and `num_buffers`
    /// buffers. All slots start free at generation 0; all buffers start
    /// available.
    pub fn new(num_slots: usize, num_buffers: usize) -> Self {
        IoRuntime {
            slots: (0..num_slots)
                .map(|_| Slot { generation: 0, state: SlotState::Free })
                .collect(),
            loaned: vec![false; num_buffers],
            free_capacity: num_buffers,
            log: Vec::new(),
        }
    }

    /// Number of buffers currently available for loan. Every change of this
    /// value corresponds to a `Completion` (down) or `Returned` (up) event.
    pub fn available_capacity(&self) -> usize {
        self.free_capacity
    }

    /// Current lifecycle state of a slot.
    pub fn slot_state(&self, slot: usize) -> Option<SlotState> {
        self.slots.get(slot).map(|s| s.state)
    }

    /// Current generation of a slot; a newly acquired request's handle must
    /// carry exactly this value.
    pub fn slot_generation(&self, slot: usize) -> Option<u64> {
        self.slots.get(slot).map(|s| s.generation)
    }

    /// Whether a buffer is currently loaned to the consumer.
    pub fn is_loaned(&self, buffer: usize) -> Option<bool> {
        self.loaned.get(buffer).copied()
    }

    /// All visible events emitted so far, in order.
    pub fn log(&self) -> &[Event] {
        &self.log
    }

    /// Application submits a new request. Returns the handle for the acquired
    /// slot (with its fresh generation), or `None` if no slot is free.
    pub fn submit(&mut self) -> Option<RequestHandle> {
        let idx = self.slots.iter().position(|s| s.state == SlotState::Free)?;
        let slot = &mut self.slots[idx];
        slot.generation += 1;
        slot.state = SlotState::Active;
        let handle = RequestHandle { slot: idx, generation: slot.generation };
        self.log.push(Event::SlotAcquired { slot: idx, generation: handle.generation });
        Some(handle)
    }

    /// Device delivers one completion segment carrying `buffer`.
    ///
    /// * Active request: the buffer is loaned to the consumer.
    /// * Cancelled request: the buffer is reclaimed internally, never loaned.
    /// * Stale generation / free slot / duplicate buffer: ignored.
    pub fn complete(&mut self, handle: RequestHandle, buffer: usize) {
        if buffer >= self.loaned.len() {
            self.log.push(Event::InvalidIgnored { kind: "complete:buffer" });
            return;
        }
        let Some(slot) = self.slots.get(handle.slot) else {
            self.log.push(Event::InvalidIgnored { kind: "complete:slot" });
            return;
        };
        if slot.generation != handle.generation {
            self.log.push(Event::StaleIgnored { handle, kind: "complete" });
            return;
        }
        if slot.state == SlotState::Free {
            // Same generation but the request already finalized: a late or
            // repeated segment for the finished request.
            self.log.push(Event::DuplicateIgnored { kind: "complete" });
            return;
        }
        if self.loaned[buffer] {
            // Same buffer completed twice (or still loaned): no double loan.
            self.log.push(Event::DuplicateIgnored { kind: "complete" });
            return;
        }
        match slot.state {
            SlotState::Active => {
                self.loaned[buffer] = true;
                self.free_capacity -= 1;
                self.log.push(Event::Completion { handle, buffer });
            }
            SlotState::Cancelled => {
                // Cancel receipt already arrived: reclaim the late buffer
                // without handing it to the consumer.
                self.log.push(Event::Reclaimed { handle, buffer });
            }
            SlotState::Free => unreachable!("checked above"),
        }
    }

    /// Device acknowledges cancellation of the request.
    pub fn cancel(&mut self, handle: RequestHandle) {
        let Some(slot) = self.slots.get_mut(handle.slot) else {
            self.log.push(Event::InvalidIgnored { kind: "cancel:slot" });
            return;
        };
        if slot.generation != handle.generation {
            self.log.push(Event::StaleIgnored { handle, kind: "cancel" });
            return;
        }
        match slot.state {
            SlotState::Active => {
                slot.state = SlotState::Cancelled;
                self.log.push(Event::CancelAck { handle });
            }
            SlotState::Cancelled | SlotState::Free => {
                self.log.push(Event::DuplicateIgnored { kind: "cancel" });
            }
        }
    }

    /// Device delivers the request's final completion. The slot becomes free
    /// and may be reused; the original request accepts no further results.
    pub fn finalize(&mut self, handle: RequestHandle) {
        let Some(slot) = self.slots.get_mut(handle.slot) else {
            self.log.push(Event::InvalidIgnored { kind: "finalize:slot" });
            return;
        };
        if slot.generation != handle.generation {
            self.log.push(Event::StaleIgnored { handle, kind: "finalize" });
            return;
        }
        if slot.state == SlotState::Free {
            self.log.push(Event::DuplicateIgnored { kind: "finalize" });
            return;
        }
        slot.state = SlotState::Free;
        self.log.push(Event::Finalized { slot: handle.slot, generation: handle.generation });
    }

    /// Consumer returns a previously loaned buffer. Only an actual return of
    /// a loaned buffer increases capacity; duplicates are ignored.
    pub fn return_buffer(&mut self, buffer: usize) {
        let Some(loaned) = self.loaned.get_mut(buffer) else {
            self.log.push(Event::InvalidIgnored { kind: "return:buffer" });
            return;
        };
        if !*loaned {
            self.log.push(Event::DuplicateIgnored { kind: "return" });
            return;
        }
        *loaned = false;
        self.free_capacity += 1;
        self.log.push(Event::Returned { buffer });
    }

    /// Run an input script of stages and device events, returning the handles
    /// produced by each `Submit` step (in order).
    pub fn run_script(&mut self, steps: &[Step]) -> Vec<Option<RequestHandle>> {
        let mut handles = Vec::new();
        for step in steps {
            match *step {
                Step::Submit => handles.push(self.submit()),
                Step::Complete { handle, buffer } => self.complete(handle, buffer),
                Step::Cancel { handle } => self.cancel(handle),
                Step::Finalize { handle } => self.finalize(handle),
                Step::Return { buffer } => self.return_buffer(buffer),
            }
        }
        handles
    }
}
