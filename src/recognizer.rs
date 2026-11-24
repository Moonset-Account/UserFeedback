//! Streaming row pattern recognition operator.
//!
//! The operator consumes a partition-ordered SQL input stream row by row and
//! emits [`Event`]s. Matching follows the SQL `MATCH_RECOGNIZE` convention:
//!
//! * starts are tried at the earliest row first;
//! * within one start, ordered alternation and repetition greediness decide
//!   the winner by the lexicographic thread rank, never by "longest match";
//! * predicates see the current row and the variable's previous capture, with
//!   references to empty captures evaluating to unknown
//!   ([`crate::tribool::Tribool::Unknown`]);
//! * after a match, processing skips past the whole span or to the last row
//!   captured by a configured variable; an uncaptured target or a target that
//!   cannot move forward advances the start by one row;
//! * cross-batch state keeps every still-winning branch. A bounded pattern
//!   becomes decided once its maximum length has been seen or every branch
//!   has died; an open-ended pattern only becomes decided at partition end;
//! * retained row history is pruned to rows an undecided branch may still
//!   read. [`Recognizer::snapshot`] / [`Recognizer::restore`] save and
//!   restore all pending branches, including undecided greedy threads.

use std::collections::BTreeMap;

use crate::nfa::{rank_cmp, EdgeKind, Nfa};
use crate::pattern::Pattern;
use crate::tribool::Tribool;

/// Predicate for one pattern variable.
///
/// `current` is the candidate row. `last_capture` is the most recent row
/// captured by this same variable within the active match, or `None` when the
/// variable has not captured anything yet; references through `None` must
/// yield [`Tribool::Unknown`]. Only a `True` result lets the consuming NFA
/// edge be taken.
pub trait RowSource<R> {
    /// Evaluates the variable predicate for `current`.
    fn predicate(&self, variable: usize, current: &R, last_capture: Option<&R>) -> Tribool;
}

/// One row inside a recognized match and the variable that owns it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RowCapture {
    /// Local zero-based row index inside the partition.
    pub row_index: u64,
    /// Variable that captured the row.
    pub variable: usize,
}

/// A fully decided pattern match.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Match {
    /// Local zero-based index of the match's first row.
    pub start_row: u64,
    /// Local zero-based index of the match's last row. For an empty-span match
    /// this equals `start_row`.
    pub end_row: u64,
    /// Rows belonging to the match in row order, each tagged with its
    /// variable. Empty for an empty-span match.
    pub rows: Vec<RowCapture>,
    /// Number of captures per variable, indexed by variable id.
    pub capture_counts: Vec<u64>,
    /// Whether the match consumes no rows (an empty-span match).
    pub is_empty: bool,
}

/// Where to continue searching after a match.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SkipMode {
    /// Continue after the whole matched span.
    PastRow,
    /// Continue at the last row captured by the given variable.
    ToVariable(usize),
}

/// Output event produced by the operator.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Event {
    /// A new partition began.
    PartitionStart,
    /// A pattern match was decided and can be emitted.
    Matched(Match),
    /// A row can never belong to any later match and is emitted as unmatched.
    UnmatchedRow {
        /// Local zero-based row index inside the partition.
        row_index: u64,
    },
    /// The partition ended; carries its aggregate statistics.
    PartitionEnd(PartitionStats),
}

/// Aggregate statistics for one partition.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct PartitionStats {
    /// Number of non-empty matches emitted.
    pub matches: u64,
    /// Number of rows covered by non-empty matches.
    pub matched_rows: u64,
    /// Number of rows explicitly emitted as unmatched.
    pub unmatched_rows: u64,
    /// Number of empty-span matches emitted.
    pub empty_matches: u64,
    /// Aggregate captures per variable, indexed by variable id.
    pub total_capture_counts: Vec<u64>,
}

/// Construction-time errors.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BuildError {
    /// A repetition had `max < min`.
    InvalidRepeatBounds {
        /// Offending minimum.
        min: u32,
        /// Offending maximum.
        max: u32,
    },
    /// A predicate or skip target references a variable outside `0..num_vars`.
    UnknownVariable(usize),
}

#[derive(Debug, Clone)]
struct Thread {
    state: usize,
    rank: Vec<u32>,
    /// `(row index, variable)` for every captured row, in row order.
    captures: Vec<(u64, usize)>,
    /// Last row index captured per variable (for predicate lookups).
    last_by_var: Vec<Option<u64>>,
}

#[derive(Debug, Clone)]
struct Candidate {
    end_row: u64,
    rank: Vec<u32>,
    thread: Thread,
}

#[derive(Debug, Clone)]
struct Run {
    /// Start row of this run.
    start: u64,
    /// Number of rows already consumed by this run.
    consumed: u64,
    /// Live NFA threads after epsilon expansion.
    threads: Vec<Thread>,
    /// Best complete match found so far (smallest rank wins).
    candidate: Option<Candidate>,
}

#[derive(Debug, Clone)]
struct PartitionState<R> {
    /// Retained rows indexed by their local partition index. Rows that no
    /// undecided branch can read are pruned.
    rows: BTreeMap<u64, R>,
    next_row_index: u64,
    /// Everything below this index has already appeared in a match or an
    /// unmatched event.
    reported_until: u64,
    run: Option<Run>,
    stats: PartitionStats,
}

impl<R> Default for PartitionState<R> {
    fn default() -> Self {
        PartitionState {
            rows: BTreeMap::new(),
            next_row_index: 0,
            reported_until: 0,
            run: None,
            stats: PartitionStats::default(),
        }
    }
}

/// The streaming row pattern recognition operator.
#[derive(Clone)]
pub struct Recognizer<P, R, S> {
    nfa: Nfa,
    num_vars: usize,
    source: S,
    skip: SkipMode,
    nullable: bool,
    current_partition: Option<P>,
    part: PartitionState<R>,
}

/// Immutable snapshot of every pending branch and retained row, suitable for
/// restoring later with [`Recognizer::restore`].
#[derive(Clone)]
pub struct RecognizerSnapshot<P, R, S> {
    current_partition: Option<P>,
    part: PartitionState<R>,
    source: S,
}

impl<P, R, S> Recognizer<P, R, S>
where
    P: Eq + Clone,
    R: Clone,
    S: RowSource<R> + Clone,
{
    /// Builds an operator for `pattern` over `num_vars` variables.
    ///
    /// `skip` selects the after-match continuation rule. `source` supplies
    /// all variable predicates.
    pub fn new(
        pattern: &Pattern,
        num_vars: usize,
        skip: SkipMode,
        source: S,
    ) -> Result<Recognizer<P, R, S>, BuildError> {
        validate_pattern(pattern, num_vars)?;
        if let SkipMode::ToVariable(var) = skip {
            if var >= num_vars {
                return Err(BuildError::UnknownVariable(var));
            }
        }
        Ok(Recognizer {
            nfa: Nfa::compile(pattern),
            num_vars,
            source,
            skip,
            nullable: pattern.min_len() == 0,
            current_partition: None,
            part: PartitionState::default(),
        })
    }

    /// Number of row payloads currently retained for undecided branches.
    #[must_use]
    pub fn retained_row_count(&self) -> usize {
        self.part.rows.len()
    }

    /// Local index that the next row of the current partition will receive.
    #[must_use]
    pub fn next_row_index(&self) -> u64 {
        self.part.next_row_index
    }

    /// Captures all pending branches (including undecided greedy threads) and
    /// retained history.
    #[must_use]
    pub fn snapshot(&self) -> RecognizerSnapshot<P, R, S> {
        RecognizerSnapshot {
            current_partition: self.current_partition.clone(),
            part: self.part.clone(),
            source: self.source.clone(),
        }
    }

    /// Restores a previously taken snapshot.
    pub fn restore(&mut self, snapshot: RecognizerSnapshot<P, R, S>) {
        self.current_partition = snapshot.current_partition;
        self.part = snapshot.part;
        self.source = snapshot.source;
    }

    fn open_partition(&mut self, partition: P) -> Vec<Event> {
        self.current_partition = Some(partition);
        self.part = PartitionState {
            stats: PartitionStats {
                total_capture_counts: vec![0; self.num_vars],
                ..PartitionStats::default()
            },
            ..PartitionState::default()
        };
        vec![Event::PartitionStart]
    }

    /// Feeds one row belonging to `partition`. A partition key change first
    /// closes the previous partition and opens the new one.
    pub fn push_row(&mut self, partition: P, row: R) -> Vec<Event> {
        let mut events = Vec::new();
        if let Some(current) = &self.current_partition {
            if current != &partition {
                events.extend(self.end_current_partition());
                // end_current_partition leaves the key in place; clear it so
                // the branch below opens a fresh, fully initialized partition.
                self.current_partition = None;
            }
        }
        if self.current_partition.is_none() {
            events.extend(self.open_partition(partition));
        }

        let row_index = self.part.next_row_index;
        self.part.next_row_index += 1;
        self.part.rows.insert(row_index, row);

        if self.part.run.is_none() {
            self.part.run = Some(self.launch_run(row_index));
        }

        events.extend(self.pump_until(row_index + 1));
        events
    }

    /// Closes the current partition, forcing every pending branch to settle.
    pub fn end_partition(&mut self) -> Vec<Event> {
        if self.current_partition.is_none() {
            return Vec::new();
        }
        let events = self.end_current_partition();
        // end_current_partition already reset the per-partition state.
        self.current_partition = None;
        events
    }

    fn end_current_partition(&mut self) -> Vec<Event> {
        let mut events = Vec::new();
        let upper = self.part.next_row_index;
        // Settle every pending run anchored on a real row. For a nullable
        // pattern the trailing run one row past the end still emits its empty
        // match; a non-nullable run past the end cannot match and stops here.
        while let Some(run) = self.part.run.as_ref() {
            if !self.nullable && run.start >= upper {
                break;
            }
            // Let the run consume every retained row it can before forcing
            // the decision; an overlap-restarted run otherwise never gets to
            // re-consume its anchor row through the normal streaming path.
            let pending = self.part.run.as_ref().map(|r| r.start + r.consumed);
            if pending.is_some_and(|next| next < upper) {
                let driven = self.pump_until(upper);
                events.extend(driven);
            }
            events.extend(self.settle_active_run());
        }
        // Anything not covered by a match is reported as unmatched.
        events.extend(self.flush_unmatched(upper));
        let stats = std::mem::take(&mut self.part.stats);
        events.push(Event::PartitionEnd(stats));
        // Reset all per-partition state so the next partition starts clean.
        self.part = PartitionState::default();
        events
    }

    /// Forces the current run to settle immediately, preserving any candidate
    /// it already holds (used at partition end; bounded/dead runs settle
    /// themselves inside [`Self::pump_until`]).
    fn settle_active_run(&mut self) -> Vec<Event> {
        let run = self
            .part
            .run
            .take()
            .expect("settle_active_run called without an active run");
        self.settle_run(run)
    }

    fn launch_run(&mut self, start: u64) -> Run {
        let closure = self.nfa.epsilon_closure(self.nfa.start, Vec::new());
        let mut threads = Vec::new();
        let mut candidate = None;

        for (state, rank) in closure {
            let thread = Thread {
                state,
                rank: rank.clone(),
                captures: Vec::new(),
                last_by_var: vec![None; self.num_vars],
            };
            if state == self.nfa.accept {
                // A nullable pattern has an empty-span complete match before
                // any row is consumed.
                candidate = Some(Candidate {
                    end_row: start,
                    rank,
                    thread,
                });
            } else {
                threads.push(thread);
            }
        }

        Run {
            start,
            consumed: 0,
            threads,
            candidate,
        }
    }

    /// Drives the active run up to but not including row index `upper`,
    /// settling runs as they become decided and relaunching successors.
    fn pump_until(&mut self, upper: u64) -> Vec<Event> {
        let mut events = Vec::new();

        while let Some(run) = self.part.run.as_mut() {
            // Bounded runs become settled once their maximum length has been
            // observed; open-ended runs must wait for partition end. This is
            // checked before the row-availability test so that a run settles
            // immediately on the row that reaches the bound.
            if let Some(max_len) = self.nfa.max_len {
                if run.consumed >= max_len {
                    let run = self.part.run.take().expect("run present");
                    events.extend(self.settle_run(run));
                    continue;
                }
            }

            let next_index = run.start + run.consumed;
            if next_index >= upper {
                break;
            }

            // All branches died: the run cannot produce any more matches, but
            // a candidate already found stays valid and must be emitted.
            if run.threads.is_empty() {
                let run = self.part.run.take().expect("run present");
                events.extend(self.settle_run(run));
                continue;
            }

            self.consume_step(next_index);
        }

        events
    }

    /// Moves every live thread across the given row using its consuming edges.
    fn consume_step(&mut self, row_index: u64) {
        let row = self
            .part
            .rows
            .get(&row_index)
            .expect("retained row exists")
            .clone();
        let run = self
            .part
            .run
            .as_mut()
            .expect("active run while consuming");
        run.consumed += 1;
        let incoming = std::mem::take(&mut run.threads);
        let mut next_threads: Vec<Thread> = Vec::new();

        for thread in incoming {
            for (edge_idx, edge) in self.nfa.states[thread.state].edges.iter().enumerate() {
                let var = match edge.kind {
                    EdgeKind::Epsilon => continue,
                    EdgeKind::Consume(var) => var,
                };
                let last_row = thread.last_by_var[var].and_then(|idx| self.part.rows.get(&idx));
                let result = self.source.predicate(var, &row, last_row);
                if !result.is_true() {
                    continue;
                }

                let mut rank = thread.rank.clone();
                rank.push(edge_idx as u32);
                let mut captures = thread.captures.clone();
                captures.push((row_index, var));
                let mut last_by_var = thread.last_by_var.clone();
                last_by_var[var] = Some(row_index);

                let reached = Thread {
                    state: edge.to,
                    rank,
                    captures,
                    last_by_var,
                };

                // Expand epsilon edges immediately; accept states become
                // candidate matches.
                let closure = self
                    .nfa
                    .epsilon_closure(reached.state, reached.rank.clone());
                for (state, closure_rank) in closure {
                    if state == self.nfa.accept {
                        let accepted = Thread {
                            state,
                            rank: closure_rank.clone(),
                            captures: reached.captures.clone(),
                            last_by_var: reached.last_by_var.clone(),
                        };
                        let candidate = Candidate {
                            end_row: row_index,
                            rank: closure_rank,
                            thread: accepted,
                        };
                        run.candidate = Some(match run.candidate.take() {
                            Some(existing)
                                if rank_cmp(&existing.rank, &candidate.rank)
                                    != std::cmp::Ordering::Greater =>
                            {
                                existing
                            }
                            _ => candidate,
                        });
                        // Accept states are terminal.
                        continue;
                    }
                    next_threads.push(Thread {
                        state,
                        rank: closure_rank,
                        captures: reached.captures.clone(),
                        last_by_var: reached.last_by_var.clone(),
                    });
                }
            }
        }

        // Priority order: the smallest rank is explored first and decides
        // lexicographic tie breaks.
        next_threads.sort_by(|a, b| rank_cmp(&a.rank, &b.rank));
        let run = self.part.run.as_mut().expect("active run");
        run.threads = next_threads;
    }

    /// Decides the winner of a run, emits the match or its unmatched start
    /// row, then launches the successor run at the appropriate start.
    fn settle_run(&mut self, run: Run) -> Vec<Event> {
        let mut events = Vec::new();
        let mut next_start = run.start + 1;
        let mut emitted_match = false;

        // An empty-span candidate is only acceptable once no real row remains
        // at the run's start that a consuming thread could still try. While
        // rows are available the run must keep competing for a non-empty
        // match instead of vacuously succeeding on every failing row.
        let upper = self.part.next_row_index;
        let candidate = run.candidate.filter(|c| {
            !(c.thread.captures.is_empty() && run.start < upper)
        });

        if let Some(candidate) = candidate {
            let end_row = candidate.end_row;
            let mut counts = vec![0u64; self.num_vars];
            for (_, var) in &candidate.thread.captures {
                counts[*var] += 1;
            }
            let is_empty = candidate.thread.captures.is_empty();
            let rows = candidate
                .thread
                .captures
                .iter()
                .map(|(row_index, variable)| RowCapture {
                    row_index: *row_index,
                    variable: *variable,
                })
                .collect();

            match self.skip {
                SkipMode::PastRow => {
                    if is_empty {
                        // An empty match cannot move past its start row.
                        next_start = run.start + 1;
                    } else {
                        next_start = end_row + 1;
                    }
                }
                SkipMode::ToVariable(var) => {
                    let target = candidate
                        .thread
                        .captures
                        .iter()
                        .rev()
                        .find(|(_, v)| *v == var)
                        .map(|(idx, _)| *idx);
                    match target {
                        // Uncaptured target: advance one row.
                        None => next_start = run.start + 1,
                        // A target strictly before the run start cannot move
                        // the search forward; fall back to one-row advance.
                        Some(idx) if idx < run.start => next_start = run.start + 1,
                        // idx == run.start is a valid overlapping restart that
                        // re-consumes the anchor row.
                        Some(idx) => next_start = idx,
                    }
                }
            }

            // Rows between the last reported position and this match's start
            // are unmatched. Rows already covered by an earlier overlapping
            // match are never re-reported.
            events.extend(self.flush_unmatched(run.start));
            let advance_to = match self.skip {
                SkipMode::PastRow => {
                    if is_empty {
                        run.start + 1
                    } else {
                        end_row + 1
                    }
                }
                // An overlapping restart reuses a row already reported by the
                // previous match, so the cursor stays put until that run
                // settles; only a forward-moving target advances it.
                SkipMode::ToVariable(_) => next_start,
            };
            // The cursor never moves backwards: overlap restarts reuse rows
            // that an earlier match already covered.
            self.part.reported_until = self.part.reported_until.max(advance_to);

            for (idx, value) in counts.iter().enumerate() {
                self.part.stats.total_capture_counts[idx] += value;
            }
            if is_empty {
                self.part.stats.empty_matches += 1;
            } else {
                self.part.stats.matches += 1;
                self.part.stats.matched_rows += candidate.thread.captures.len() as u64;
            }
            events.push(Event::Matched(Match {
                start_row: run.start,
                end_row,
                rows,
                capture_counts: counts,
                is_empty,
            }));
            emitted_match = true;
        }

        if !emitted_match {
            next_start = run.start + 1;
            // A run restarted by an overlapping skip may anchor on a row the
            // previous match already reported; flush only genuinely new rows.
            let flush_to = next_start.max(self.part.reported_until);
            events.extend(self.flush_unmatched(flush_to));
        }

        // History pruning: rows strictly below the new start cannot be read
        // by any future predicate. The start row itself is retained because
        // an overlapping skip-to successor re-consumes it, and rows the new
        // run already consumed stay until that run settles.
        let retain_from = next_start;
        self.part.rows.retain(|idx, _| *idx >= retain_from);

        if next_start < self.part.next_row_index {
            // The successor start points at a retained row; launch it and
            // replay the already-available rows.
            self.part.run = Some(self.launch_run(next_start));
            let upper = self.part.next_row_index;
            let more = self.pump_until(upper);
            events.extend(more);
        } else if next_start == self.part.next_row_index {
            // The successor anchors on the last retained row (or, for a
            // nullable pattern, one row past the end for its trailing empty
            // match). It cannot consume anything during normal streaming, so
            // leave it pending; the next push_row or partition-close loop
            // drives and settles it.
            self.part.run = Some(self.launch_run(next_start));
        } else {
            self.part.run = None;
        }
        events
    }

    fn flush_unmatched(&mut self, until: u64) -> Vec<Event> {
        let mut events = Vec::new();
        while self.part.reported_until < until {
            let idx = self.part.reported_until;
            events.push(Event::UnmatchedRow { row_index: idx });
            self.part.stats.unmatched_rows += 1;
            self.part.reported_until += 1;
        }
        events
    }
}

fn validate_pattern(pattern: &Pattern, num_vars: usize) -> Result<(), BuildError> {
    match pattern {
        Pattern::Var(id) => {
            if *id >= num_vars {
                Err(BuildError::UnknownVariable(*id))
            } else {
                Ok(())
            }
        }
        Pattern::Concat(parts) | Pattern::Alt(parts) => {
            parts.iter().try_for_each(|p| validate_pattern(p, num_vars))
        }
        Pattern::Repeat(repeat) => {
            if let Some(max) = repeat.max {
                if max < repeat.min {
                    return Err(BuildError::InvalidRepeatBounds {
                        min: repeat.min,
                        max,
                    });
                }
            }
            validate_pattern(&repeat.child, num_vars)
        }
    }
}

/// Convenience [`RowSource`] backed by a predicate closure (or function item).
pub struct ClosureSource<F>(pub F);

impl<F: Clone> Clone for ClosureSource<F> {
    fn clone(&self) -> Self {
        ClosureSource(self.0.clone())
    }
}

impl<R, F> RowSource<R> for ClosureSource<F>
where
    F: Fn(usize, &R, Option<&R>) -> Tribool,
{
    fn predicate(&self, variable: usize, current: &R, last_capture: Option<&R>) -> Tribool {
        (self.0)(variable, current, last_capture)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::pattern::Greed;

    #[derive(Debug, Clone, PartialEq)]
    struct Row {
        kind: &'static str,
        value: i64,
    }

    fn ab_predicate(variable: usize, row: &Row, _last: Option<&Row>) -> Tribool {
        match (variable, row.kind) {
            (0, "A") | (1, "B") => Tribool::True,
            _ => Tribool::False,
        }
    }

    fn only_a_predicate(_variable: usize, row: &Row, _last: Option<&Row>) -> Tribool {
        (row.kind == "A").into()
    }

    fn increasing_predicate(
        _variable: usize,
        row: &Row,
        last: Option<&Row>,
    ) -> Tribool {
        match last {
            None => Tribool::True,
            Some(prev) => (row.value > prev.value).into(),
        }
    }

    fn feed_partition<S: RowSource<Row> + Clone>(
        rec: &mut Recognizer<u32, Row, S>,
        key: u32,
        rows: &[Row],
    ) -> Vec<Event> {
        let mut events = Vec::new();
        for row in rows {
            events.extend(rec.push_row(key, row.clone()));
        }
        events.extend(rec.end_partition());
        events
    }

    #[test]
    fn rejects_unknown_variables_and_bad_bounds() {
        let err = match Recognizer::<u32, Row, _>::new(
            &Pattern::var(2),
            2,
            SkipMode::PastRow,
            ClosureSource(ab_predicate),
        ) {
            Err(err) => err,
            Ok(_) => panic!("unknown variable must be rejected"),
        };
        assert_eq!(err, BuildError::UnknownVariable(2));

        let err = match Recognizer::<u32, Row, _>::new(
            &Pattern::repeat(Pattern::var(0), 3, Some(2), Greed::Greedy),
            1,
            SkipMode::PastRow,
            ClosureSource(only_a_predicate),
        ) {
            Err(err) => err,
            Ok(_) => panic!("inverted repeat bounds must be rejected"),
        };
        assert_eq!(err, BuildError::InvalidRepeatBounds { min: 3, max: 2 });
    }

    #[test]
    fn greedy_extends_while_possible_but_rank_still_decides() {
        // A B* : greedy extends B while B rows keep arriving.
        let pattern = Pattern::concat(vec![Pattern::var(0), Pattern::star(Pattern::var(1))]);
        let rows = vec![
            Row { kind: "A", value: 1 },
            Row { kind: "B", value: 2 },
            Row { kind: "B", value: 3 },
            Row { kind: "X", value: 4 },
        ];
        let mut rec = Recognizer::new(
            &pattern,
            2,
            SkipMode::PastRow,
            ClosureSource(ab_predicate),
        )
        .unwrap();

        let events = feed_partition(&mut rec, 1, &rows);
        let match_events: Vec<&Match> = events
            .iter()
            .filter_map(|e| if let Event::Matched(m) = e { Some(m) } else { None })
            .collect();
        assert_eq!(match_events.len(), 1);
        let m = match_events[0];
        assert_eq!(m.start_row, 0);
        assert_eq!(m.end_row, 2);
        assert_eq!(m.capture_counts, vec![1, 2]);
        assert!(matches!(&events[0], Event::PartitionStart));
        assert!(matches!(events.last().unwrap(), Event::PartitionEnd(_)));
    }

    #[test]
    fn ordered_alternation_prefers_written_branch() {
        // (A | A A): first branch wins on a single A even though the second
        // could extend further; the second A restarts a new match.
        let pattern = Pattern::alt(vec![
            Pattern::var(0),
            Pattern::concat(vec![Pattern::var(0), Pattern::var(0)]),
        ]);
        let rows = vec![
            Row { kind: "A", value: 1 },
            Row { kind: "A", value: 2 },
        ];
        let mut rec = Recognizer::new(
            &pattern,
            1,
            SkipMode::PastRow,
            ClosureSource(only_a_predicate),
        )
        .unwrap();
        let events = feed_partition(&mut rec, 7, &rows);
        let matches: Vec<&Match> = events
            .iter()
            .filter_map(|e| if let Event::Matched(m) = e { Some(m) } else { None })
            .collect();
        assert_eq!(matches.len(), 2);
        assert_eq!(matches[0].start_row, 0);
        assert_eq!(matches[0].end_row, 0);
        assert_eq!(matches[1].start_row, 1);
    }

    #[test]
    fn predicate_sees_last_capture_of_same_variable() {
        // A+ requiring strictly increasing values; references are unknown on
        // the first capture and must not reject it. The plus is open-ended,
        // so nothing settles until partition end.
        let pattern = Pattern::plus(Pattern::var(0));
        let rows = vec![
            Row { kind: "A", value: 1 },
            Row { kind: "A", value: 3 },
            Row { kind: "A", value: 2 },
        ];
        let mut rec = Recognizer::new(
            &pattern,
            1,
            SkipMode::PastRow,
            ClosureSource(increasing_predicate),
        )
        .unwrap();
        let events = feed_partition(&mut rec, 0, &rows);
        let matches: Vec<&Match> = events
            .iter()
            .filter_map(|e| if let Event::Matched(m) = e { Some(m) } else { None })
            .collect();
        // First run captures rows 0..=1 (1, 3); row 2 fails inside it but
        // restarts a fresh run whose first capture sees an unknown reference.
        assert_eq!(matches.len(), 2);
        assert_eq!((matches[0].start_row, matches[0].end_row), (0, 1));
        assert_eq!((matches[1].start_row, matches[1].end_row), (2, 2));
        assert!(matches[1].capture_counts == vec![1]);
    }
}
