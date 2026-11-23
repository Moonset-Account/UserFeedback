use crate::store::ColumnId;

/// A boolean expression over column predicates, evaluated with three-valued
/// logic (true / unknown / false).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Expr {
    /// Predicate of one input column.
    Column(ColumnId),
    And(Box<Expr>, Box<Expr>),
    Or(Box<Expr>, Box<Expr>),
    Not(Box<Expr>),
}

impl Expr {
    pub fn column(id: ColumnId) -> Expr {
        Expr::Column(id)
    }

    pub fn and(a: Expr, b: Expr) -> Expr {
        Expr::And(Box::new(a), Box::new(b))
    }

    pub fn or(a: Expr, b: Expr) -> Expr {
        Expr::Or(Box::new(a), Box::new(b))
    }

    #[allow(clippy::should_implement_trait)]
    pub fn not(e: Expr) -> Expr {
        Expr::Not(Box::new(e))
    }
}
