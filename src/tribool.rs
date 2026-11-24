//! Three-valued logic used by pattern variable predicates.
//!
//! SQL row pattern recognition uses SQL's three-valued logic: a predicate may
//! evaluate to true, false or unknown. A reference to a variable that has not
//! captured any row yet evaluates to `NULL`, and any comparison involving
//! `NULL` yields unknown. Only `True` makes an NFA edge eligible.

use std::cmp::Ordering;

/// The three values of SQL boolean logic.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Tribool {
    /// Corresponds to SQL `TRUE`. Only `True` allows a pattern edge to be taken.
    True,
    /// Corresponds to SQL `FALSE`.
    False,
    /// Corresponds to SQL `UNKNOWN` / `NULL`.
    Unknown,
}

impl Tribool {
    /// SQL `AND`: true iff both operands are true; unknown if no operand is
    /// false but at least one is unknown.
    #[must_use]
    pub fn and(self, other: Tribool) -> Tribool {
        match (self, other) {
            (Tribool::True, Tribool::True) => Tribool::True,
            (Tribool::False, _) | (_, Tribool::False) => Tribool::False,
            _ => Tribool::Unknown,
        }
    }

    /// SQL `OR`: false iff both operands are false; unknown if no operand is
    /// true but at least one is unknown.
    #[must_use]
    pub fn or(self, other: Tribool) -> Tribool {
        match (self, other) {
            (Tribool::False, Tribool::False) => Tribool::False,
            (Tribool::True, _) | (_, Tribool::True) => Tribool::True,
            _ => Tribool::Unknown,
        }
    }

    /// SQL `NOT`.
    #[must_use]
    pub fn not(self) -> Tribool {
        match self {
            Tribool::True => Tribool::False,
            Tribool::False => Tribool::True,
            Tribool::Unknown => Tribool::Unknown,
        }
    }

    /// A pattern edge may only be taken when the predicate is definitely true;
    /// false and unknown both reject the row.
    #[must_use]
    pub fn is_true(self) -> bool {
        self == Tribool::True
    }
}

impl From<bool> for Tribool {
    fn from(value: bool) -> Self {
        if value {
            Tribool::True
        } else {
            Tribool::False
        }
    }
}

/// Ordering over nullable values following SQL semantics: any comparison with
/// an unknown (`None`) operand yields [`Tribool::Unknown`].
pub fn cmp_options<T: Ord + ?Sized>(left: Option<&T>, right: Option<&T>) -> Tribool {
    match (left, right) {
        (Some(l), Some(r)) => match l.cmp(r) {
            Ordering::Less => Tribool::False,
            Ordering::Equal => Tribool::True,
            Ordering::Greater => Tribool::False,
        },
        _ => Tribool::Unknown,
    }
}

/// SQL equality for nullable values: `None` means unknown, never equal.
pub fn eq_options<T: PartialEq + ?Sized>(left: Option<&T>, right: Option<&T>) -> Tribool {
    match (left, right) {
        (Some(l), Some(r)) => (l == r).into(),
        _ => Tribool::Unknown,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn truth_tables_follow_sql_semantics() {
        use Tribool::{False, True, Unknown};

        assert_eq!(True.and(True), True);
        assert_eq!(True.and(Unknown), Unknown);
        assert_eq!(Unknown.and(False), False);
        assert_eq!(False.or(Unknown), Unknown);
        assert_eq!(Unknown.or(True), True);
        assert_eq!(False.or(False), False);
        assert_eq!(Unknown.not(), Unknown);
        assert!(!Unknown.is_true());
        assert!(!False.is_true());
        assert!(True.is_true());
    }

    #[test]
    fn nullable_comparisons_are_unknown_when_operand_missing() {
        assert_eq!(cmp_options(Some(&1), Some(&1)), Tribool::True);
        assert_eq!(cmp_options(Some(&1), Some(&2)), Tribool::False);
        assert_eq!(cmp_options::<u32>(None, Some(&2)), Tribool::Unknown);
        assert_eq!(eq_options(Some("a"), Some("a")), Tribool::True);
        assert_eq!(eq_options::<&str>(None, None), Tribool::Unknown);
    }
}
