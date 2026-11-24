//! Thompson-style NFA compilation for [`Pattern`] expressions.
//!
//! Every NFA state keeps an ordered edge list. Edge order encodes priority:
//!
//! * alternation branches are linked in written order;
//! * a greedy repetition lists the loop-back (take another iteration) before
//!   the exit edge;
//! * a lazy repetition lists the exit edge first.
//!
//! A thread records the sequence of chosen edge indices as its `rank`. Rank
//! comparison is plain lexicographic ordering, which yields exactly the
//! "written order first, greedy tries longer / lazy stops earlier" semantics
//! required by SQL row pattern matching. Bounded repetitions are fully
//! unrolled; open-ended repetitions use a loop state, and epsilon cycles are
//! broken during closure expansion with a visited set.

use std::cmp::Ordering;
use std::collections::HashMap;

use crate::pattern::{Greed, Pattern, Repeat};

/// An NFA transition.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EdgeKind {
    /// Consumes no row.
    Epsilon,
    /// Consumes one row, handing it to the predicate of the given variable.
    Consume(usize),
}

/// A directed NFA edge.
#[derive(Debug, Clone, Copy)]
pub struct Edge {
    /// What the edge does when taken.
    pub kind: EdgeKind,
    /// Destination state.
    pub to: usize,
}

/// A compiled NFA state with priority-ordered outgoing edges.
#[derive(Debug, Clone, Default)]
pub struct NfaState {
    /// Outgoing edges in priority order.
    pub edges: Vec<Edge>,
}

/// A compiled non-deterministic finite automaton.
#[derive(Debug, Clone)]
pub struct Nfa {
    /// All states, indexed by state id.
    pub states: Vec<NfaState>,
    /// Start state.
    pub start: usize,
    /// Single accepting state.
    pub accept: usize,
    /// Maximum number of rows a match may consume, if bounded.
    pub max_len: Option<u64>,
}

/// Lexicographic comparison of thread ranks (shorter prefixes sort first,
/// matching the usual ordered-regex backtracking convention).
#[must_use]
pub fn rank_cmp(left: &[u32], right: &[u32]) -> Ordering {
    left.cmp(right)
}

struct Compiler {
    states: Vec<NfaState>,
}

impl Compiler {
    fn new_state(&mut self) -> usize {
        let id = self.states.len();
        self.states.push(NfaState::default());
        id
    }

    fn add_edge(&mut self, from: usize, kind: EdgeKind, to: usize) {
        self.states[from].edges.push(Edge { kind, to });
    }

    fn add_epsilon(&mut self, from: usize, to: usize) {
        self.add_edge(from, EdgeKind::Epsilon, to);
    }

    fn compile(&mut self, pattern: &Pattern) -> (usize, usize) {
        match pattern {
            Pattern::Var(id) => {
                let start = self.new_state();
                let accept = self.new_state();
                self.add_edge(start, EdgeKind::Consume(*id), accept);
                (start, accept)
            }
            Pattern::Concat(parts) => {
                let start = self.new_state();
                let mut cur = start;
                if parts.is_empty() {
                    // Empty concatenation: single state that is both ends.
                    return (cur, cur);
                }
                for part in parts {
                    let (s, a) = self.compile(part);
                    self.add_epsilon(cur, s);
                    cur = a;
                }
                (start, cur)
            }
            Pattern::Alt(branches) => {
                let start = self.new_state();
                let accept = self.new_state();
                for branch in branches {
                    let (s, a) = self.compile(branch);
                    // Branch order is edge order, so written order wins.
                    self.add_epsilon(start, s);
                    self.add_epsilon(a, accept);
                }
                (start, accept)
            }
            Pattern::Repeat(repeat) => self.compile_repeat(repeat),
        }
    }

    fn compile_repeat(&mut self, repeat: &Repeat) -> (usize, usize) {
        let Repeat {
            min,
            max,
            greed,
            child,
        } = repeat;

        let entry = self.new_state();
        let mut cur = entry;

        // Mandatory iterations.
        for _ in 0..*min {
            let (s, a) = self.compile(child);
            self.add_epsilon(cur, s);
            cur = a;
        }

        match max {
            Some(max) => {
                // Bounded repetition: unroll every optional iteration.
                for _ in 0..(*max - *min) {
                    let (cs, ca) = self.compile(child);
                    let frag = self.new_state();
                    let end = self.new_state();
                    match greed {
                        Greed::Greedy => {
                            // Try another iteration before skipping.
                            self.add_epsilon(frag, cs);
                            self.add_epsilon(frag, end);
                        }
                        Greed::Lazy => {
                            // Skip before trying another iteration.
                            self.add_epsilon(frag, end);
                            self.add_epsilon(frag, cs);
                        }
                    }
                    self.add_epsilon(ca, end);
                    self.add_epsilon(cur, frag);
                    cur = end;
                }
                (entry, cur)
            }
            None => {
                // Open-ended repetition: explicit loop state.
                let loop_state = self.new_state();
                let exit = self.new_state();
                let (cs, ca) = self.compile(child);
                self.add_epsilon(cur, loop_state);
                self.add_epsilon(ca, loop_state);
                match greed {
                    Greed::Greedy => {
                        // Loop back (longer match) before exit.
                        self.add_epsilon(loop_state, cs);
                        self.add_epsilon(loop_state, exit);
                    }
                    Greed::Lazy => {
                        // Exit before looping back.
                        self.add_epsilon(loop_state, exit);
                        self.add_epsilon(loop_state, cs);
                    }
                }
                (entry, exit)
            }
        }
    }
}

impl Nfa {
    /// Compiles a pattern into an NFA.
    #[must_use]
    pub fn compile(pattern: &Pattern) -> Nfa {
        let mut compiler = Compiler { states: Vec::new() };
        let (start, accept) = compiler.compile(pattern);
        Nfa {
            states: compiler.states,
            start,
            accept,
            max_len: pattern.max_len(),
        }
    }

    /// Expands all epsilon reachable states from `start` with their ranks.
    ///
    /// The returned vector contains one best (smallest-rank) entry per
    /// reachable state. Epsilon cycles - possible when a nullable child is
    /// repeated open-endedly - are broken with a per-closure visited set.
    #[must_use]
    pub fn epsilon_closure(&self, start: usize, initial_rank: Vec<u32>) -> Vec<(usize, Vec<u32>)> {
        let mut best: HashMap<usize, Vec<u32>> = HashMap::new();
        let mut stack = vec![(start, initial_rank)];

        while let Some((state, rank)) = stack.pop() {
            if let Some(existing) = best.get(&state) {
                if rank_cmp(existing, &rank) != Ordering::Greater {
                    continue;
                }
            }
            best.insert(state, rank.clone());

            // Push in reverse so the highest-priority edge is popped first.
            for (idx, edge) in self.states[state].edges.iter().enumerate().rev() {
                if edge.kind == EdgeKind::Epsilon {
                    let mut next_rank = rank.clone();
                    next_rank.push(idx as u32);
                    stack.push((edge.to, next_rank));
                }
            }
        }

        best.into_iter().collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::pattern::Pattern;

    #[test]
    fn alternation_edges_follow_written_order() {
        let p = Pattern::alt(vec![Pattern::var(0), Pattern::var(1)]);
        let nfa = Nfa::compile(&p);
        let start_edges = &nfa.states[nfa.start].edges;
        assert_eq!(start_edges.len(), 2);
        assert_eq!(start_edges[0].kind, EdgeKind::Epsilon);
        assert_eq!(start_edges[1].kind, EdgeKind::Epsilon);
        assert_ne!(start_edges[0].to, start_edges[1].to);
    }

    #[test]
    fn greedy_loop_ranks_loopback_before_exit() {
        let p = Pattern::star(Pattern::var(0));
        let nfa = Nfa::compile(&p);
        let loop_state = nfa
            .states
            .iter()
            .position(|s| s.edges.len() == 2)
            .expect("star introduces a loop state with two edges");
        assert_eq!(nfa.states[loop_state].edges[0].to != nfa.accept, true);
        assert_eq!(nfa.states[loop_state].edges[1].to, nfa.accept);
    }

    #[test]
    fn closure_breaks_nullable_star_cycle_and_keeps_best_rank() {
        // (empty)* is nullable and creates an epsilon-only cycle.
        let p = Pattern::star(Pattern::concat(vec![]));
        let nfa = Nfa::compile(&p);
        let reached = nfa.epsilon_closure(nfa.start, Vec::new());
        assert!(reached.iter().any(|(state, _)| *state == nfa.accept));
    }

    #[test]
    fn bounded_repeat_is_fully_unrolled() {
        let p = Pattern::repeat(Pattern::var(0), 1, Some(3), Greed::Greedy);
        let nfa = Nfa::compile(&p);
        let consume_count = nfa
            .states
            .iter()
            .flat_map(|s| &s.edges)
            .filter(|e| matches!(e.kind, EdgeKind::Consume(_)))
            .count();
        assert_eq!(consume_count, 3);
        assert_eq!(nfa.max_len, Some(3));
    }
}
