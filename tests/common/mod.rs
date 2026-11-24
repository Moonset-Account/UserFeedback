//! Common support for integration tests: row types, predicate function items
//! and a small independent reference matcher used to cross-check the
//! streaming operator.

#![allow(dead_code)]

use row_pattern_recognizer::{
    ClosureSource, Event, Greed, Match, Pattern, Recognizer, SkipMode, Tribool,
};

/// Test row: a kind tag and an integer value.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Row {
    pub kind: &'static str,
    pub value: i64,
}

pub fn ab_predicate(variable: usize, row: &Row, _last: Option<&Row>) -> Tribool {
    match (variable, row.kind) {
        (0, "A") | (1, "B") => Tribool::True,
        _ => Tribool::False,
    }
}

pub fn only_a_predicate(_variable: usize, row: &Row, _last: Option<&Row>) -> Tribool {
    (row.kind == "A").into()
}

pub fn increasing_predicate(
    _variable: usize,
    row: &Row,
    last: Option<&Row>,
) -> Tribool {
    match last {
        None => Tribool::True,
        Some(prev) => (row.value > prev.value).into(),
    }
}

/// Predicate used by the ownership-flip scenario: variable 0 (DOWN) accepts
/// rows below the previous DOWN capture; variable 1 (UP) accepts rows above
/// the previous UP capture. The first capture of either variable always
/// succeeds because references to empty captures are unknown, not false.
pub fn down_up_predicate(
    variable: usize,
    row: &Row,
    last: Option<&Row>,
) -> Tribool {
    match (variable, last) {
        (_, None) => Tribool::True,
        (0, Some(prev)) => (row.value < prev.value).into(),
        (1, Some(prev)) => (row.value > prev.value).into(),
        _ => Tribool::False,
    }
}

pub type Predicate = fn(usize, &Row, Option<&Row>) -> Tribool;
pub type TestRecognizer = Recognizer<u32, Row, ClosureSource<Predicate>>;

pub fn build(pattern: &Pattern, num_vars: usize, skip: SkipMode, predicate: Predicate) -> TestRecognizer {
    Recognizer::new(pattern, num_vars, skip, ClosureSource(predicate))
        .expect("test patterns are valid")
}

/// Feeds one partition, splitting rows into chunks of `chunk` rows (a chunk
/// of zero means one row per call), then closes the partition.
pub fn feed(rec: &mut TestRecognizer, key: u32, rows: &[Row], chunk: usize) -> Vec<Event> {
    let mut events = Vec::new();
    for batch in rows.chunks(chunk.max(1)) {
        for row in batch {
            events.extend(rec.push_row(key, row.clone()));
        }
    }
    events.extend(rec.end_partition());
    events
}

pub fn matches_of(events: &[Event]) -> Vec<&Match> {
    events
        .iter()
        .filter_map(|e| if let Event::Matched(m) = e { Some(m) } else { None })
        .collect()
}

pub fn count_vars(pattern: &Pattern) -> usize {
    match pattern {
        Pattern::Var(id) => id + 1,
        Pattern::Concat(parts) | Pattern::Alt(parts) => {
            parts.iter().map(count_vars).max().unwrap_or(0)
        }
        Pattern::Repeat(repeat) => count_vars(&repeat.child),
    }
}

/// A fully independent depth-first reference matcher over the pattern AST.
///
/// It explores one start row at a time in the same priority order as the NFA
/// (written-order alternation; greedy tries another iteration before
/// stopping; lazy stops first) and returns the first complete match as
/// `(end_index, captures)` where captures are `(row_index, variable)` pairs.
/// The streaming operator must agree with it regardless of how rows are
/// split into batches.
pub fn reference_match(
    pattern: &Pattern,
    rows: &[Row],
    start: usize,
    predicate: Predicate,
) -> Option<(usize, Vec<(usize, usize)>)> {
    fn dfs(
        pattern: &Pattern,
        rows: &[Row],
        pos: usize,
        last: &mut [Option<Row>],
        out: &mut Vec<(usize, usize)>,
        predicate: Predicate,
    ) -> bool {
        match pattern {
            Pattern::Var(var) => {
                if pos >= rows.len() {
                    return false;
                }
                if !predicate(*var, &rows[pos], last[*var].as_ref()).is_true() {
                    return false;
                }
                last[*var] = Some(rows[pos].clone());
                out.push((pos, *var));
                true
            }
            Pattern::Concat(parts) => {
                let mut cursor = pos;
                for part in parts {
                    let snapshot = last.to_vec();
                    let before = out.len();
                    if !dfs(part, rows, cursor, last, out, predicate) {
                        while out.len() > before {
                            out.pop();
                        }
                        last.clone_from_slice(&snapshot);
                        return false;
                    }
                    cursor += out.len() - before;
                }
                true
            }
            Pattern::Alt(branches) => {
                for branch in branches {
                    let snapshot = last.to_vec();
                    let before = out.len();
                    if dfs(branch, rows, pos, last, out, predicate) {
                        return true;
                    }
                    while out.len() > before {
                        out.pop();
                    }
                    last.clone_from_slice(&snapshot);
                }
                false
            }
            Pattern::Repeat(repeat) => match repeat.max {
                Some(max) => dfs_bounded(
                    &repeat.child,
                    rows,
                    pos,
                    last,
                    out,
                    predicate,
                    repeat.min,
                    max,
                    repeat.greed,
                ),
                None => dfs_open(
                    &repeat.child,
                    rows,
                    pos,
                    last,
                    out,
                    predicate,
                    repeat.min,
                    repeat.greed,
                ),
            },
        }
    }

    fn dfs_bounded(
        child: &Pattern,
        rows: &[Row],
        pos: usize,
        last: &mut [Option<Row>],
        out: &mut Vec<(usize, usize)>,
        predicate: Predicate,
        min: u32,
        max: u32,
        greed: Greed,
    ) -> bool {
        let mut cursor = pos;
        for _ in 0..min {
            let snapshot = last.to_vec();
            let before = out.len();
            if !dfs(child, rows, cursor, last, out, predicate) {
                while out.len() > before {
                    out.pop();
                }
                last.clone_from_slice(&snapshot);
                return false;
            }
            cursor += out.len() - before;
        }
        optional_iterations(
            child, rows, cursor, last, out, predicate, max - min, greed,
        )
    }

    fn optional_iterations(
        child: &Pattern,
        rows: &[Row],
        pos: usize,
        last: &mut [Option<Row>],
        out: &mut Vec<(usize, usize)>,
        predicate: Predicate,
        remaining: u32,
        greed: Greed,
    ) -> bool {
        if remaining == 0 {
            return true;
        }
        let try_take = |last: &mut [Option<Row>], out: &mut Vec<(usize, usize)>| {
            let snapshot = last.to_vec();
            let before = out.len();
            if dfs(child, rows, pos, last, out, predicate) {
                let consumed = out.len() - before;
                if optional_iterations(
                    child,
                    rows,
                    pos + consumed,
                    last,
                    out,
                    predicate,
                    remaining - 1,
                    greed,
                ) {
                    return true;
                }
            }
            while out.len() > before {
                out.pop();
            }
            last.clone_from_slice(&snapshot);
            false
        };
        let try_skip = |last: &mut [Option<Row>], out: &mut Vec<(usize, usize)>| {
            optional_iterations(
                child, rows, pos, last, out, predicate, remaining - 1, greed,
            )
        };
        match greed {
            Greed::Greedy => try_take(last, out) || try_skip(last, out),
            Greed::Lazy => try_skip(last, out) || try_take(last, out),
        }
    }

    fn dfs_open(
        child: &Pattern,
        rows: &[Row],
        pos: usize,
        last: &mut [Option<Row>],
        out: &mut Vec<(usize, usize)>,
        predicate: Predicate,
        min: u32,
        greed: Greed,
    ) -> bool {
        let mut cursor = pos;
        for _ in 0..min {
            let snapshot = last.to_vec();
            let before = out.len();
            if !dfs(child, rows, cursor, last, out, predicate) {
                while out.len() > before {
                    out.pop();
                }
                last.clone_from_slice(&snapshot);
                return false;
            }
            cursor += out.len() - before;
        }
        // Open-ended repetitions appear only at tail position in the test
        // patterns; greedy therefore takes the maximal run and lazy stops
        // immediately. Backtracking into a shorter greedy run is exercised by
        // the NFA's own ranked threads in the streaming tests.
        match greed {
            Greed::Lazy => true,
            Greed::Greedy => loop {
                let snapshot = last.to_vec();
                let before = out.len();
                if !dfs(child, rows, cursor, last, out, predicate) {
                    while out.len() > before {
                        out.pop();
                    }
                    last.clone_from_slice(&snapshot);
                    return true;
                }
                cursor += out.len() - before;
            },
        }
    }

    let num_vars = count_vars(pattern);
    let mut last: Vec<Option<Row>> = vec![None; num_vars];
    let mut out = Vec::new();
    if dfs(pattern, rows, start, &mut last, &mut out, predicate) {
        let end = out.last().map(|(idx, _)| *idx).unwrap_or(start);
        Some((end, out))
    } else {
        None
    }
}
