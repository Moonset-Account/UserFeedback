//! Pattern abstract syntax tree.
//!
//! A [`Pattern`] is built with the constructor functions in this module rather
//! than parsed from SQL text. It supports exactly the operators required by
//! the task:
//!
//! * [`Pattern::var`] - a pattern variable whose predicate decides on a row;
//! * [`Pattern::concat`] - ordered concatenation;
//! * [`Pattern::alt`] - alternation whose branches are tried in written order;
//! * [`Pattern::repeat`] - finite or open-ended repetition, greedy or lazy.

use std::sync::Arc;

/// A row pattern expression.
#[derive(Debug, Clone)]
pub enum Pattern {
    /// A single pattern variable, identified by an index assigned by the
    /// recognizer builder.
    Var(usize),
    /// Concatenation in written order.
    Concat(Vec<Pattern>),
    /// Ordered alternation: the first branch has the highest priority.
    Alt(Vec<Pattern>),
    /// Bounded or open-ended repetition.
    Repeat(Repeat),
}

/// Greediness of a repeated sub-pattern.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Greed {
    /// Prefer taking another iteration while a complete match stays possible.
    Greedy,
    /// Prefer stopping as soon as a complete match is possible.
    Lazy,
}

/// A repetition expression `child{min, max}`.
#[derive(Debug, Clone)]
pub struct Repeat {
    /// Minimum number of iterations (must be satisfied).
    pub min: u32,
    /// Maximum number of iterations, or `None` for an open-ended repetition.
    pub max: Option<u32>,
    /// Greedy or lazy preference.
    pub greed: Greed,
    /// The repeated sub-pattern.
    pub child: Arc<Pattern>,
}

impl Pattern {
    /// A pattern variable reference.
    #[must_use]
    pub fn var(id: usize) -> Pattern {
        Pattern::Var(id)
    }

    /// Concatenation of the given sub-patterns, in written order.
    ///
    /// An empty list is an empty pattern that matches the empty span.
    #[must_use]
    pub fn concat(parts: Vec<Pattern>) -> Pattern {
        Pattern::Concat(parts)
    }

    /// Ordered alternation: branches are tried in the order given.
    ///
    /// An empty list is a pattern that never matches.
    #[must_use]
    pub fn alt(branches: Vec<Pattern>) -> Pattern {
        Pattern::Alt(branches)
    }

    /// A repetition `child{min, max}` with explicit greediness.
    #[must_use]
    pub fn repeat(child: Pattern, min: u32, max: Option<u32>, greed: Greed) -> Pattern {
        Pattern::Repeat(Repeat {
            min,
            max,
            greed,
            child: Arc::new(child),
        })
    }

    /// `child?` - zero or one occurrence, greedy.
    #[must_use]
    pub fn optional(child: Pattern) -> Pattern {
        Pattern::repeat(child, 0, Some(1), Greed::Greedy)
    }

    /// `child*` - zero or more occurrences, greedy and open-ended.
    #[must_use]
    pub fn star(child: Pattern) -> Pattern {
        Pattern::repeat(child, 0, None, Greed::Greedy)
    }

    /// `child+` - one or more occurrences, greedy and open-ended.
    #[must_use]
    pub fn plus(child: Pattern) -> Pattern {
        Pattern::repeat(child, 1, None, Greed::Greedy)
    }

    /// Minimum number of rows the pattern can consume.
    #[must_use]
    pub fn min_len(&self) -> u64 {
        match self {
            Pattern::Var(_) => 1,
            Pattern::Concat(parts) => parts.iter().map(Pattern::min_len).sum(),
            Pattern::Alt(branches) => branches.iter().map(Pattern::min_len).min().unwrap_or(0),
            Pattern::Repeat(r) => u64::from(r.min).saturating_mul(r.child.min_len()),
        }
    }

    /// Maximum number of rows the pattern can consume, or `None` when the
    /// pattern contains an open-ended repetition.
    #[must_use]
    pub fn max_len(&self) -> Option<u64> {
        match self {
            Pattern::Var(_) => Some(1),
            Pattern::Concat(parts) => parts
                .iter()
                .map(Pattern::max_len)
                .try_fold(0u64, |acc, item| item.map(|v| acc.saturating_add(v))),
            Pattern::Alt(branches) => branches
                .iter()
                .map(Pattern::max_len)
                .try_fold(0u64, |acc, item| item.map(|v| acc.max(v))),
            Pattern::Repeat(r) => {
                let child_max = r.child.max_len()?;
                r.max.map(|m| u64::from(m).saturating_mul(child_max))
            }
        }
    }

    /// Whether the pattern can match the empty span.
    #[must_use]
    pub fn nullable(&self) -> bool {
        self.min_len() == 0
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn length_bounds_account_for_repetition_and_alternation() {
        let p = Pattern::concat(vec![
            Pattern::var(0),
            Pattern::repeat(
                Pattern::var(1),
                2,
                Some(4),
                Greed::Lazy,
            ),
            Pattern::alt(vec![Pattern::var(2), Pattern::concat(vec![])]),
        ]);
        assert_eq!(p.min_len(), 3);
        assert_eq!(p.max_len(), Some(6));
        assert!(Pattern::star(Pattern::var(0)).nullable());
        assert_eq!(Pattern::plus(Pattern::var(0)).max_len(), None);
        assert!(!Pattern::plus(Pattern::var(0)).nullable());
    }
}
