//! Cross-batching invariance: the same rows must produce the same matches
//! no matter how they are split into batches. Results are also cross-checked
//! against the independent depth-first reference matcher in `common`.

mod common;

use common::{
    ab_predicate, build, down_up_predicate, feed, increasing_predicate, matches_of, only_a_predicate,
    reference_match, Row,
};
use row_pattern_recognizer::{Event, Greed, Pattern, SkipMode};

fn strip_partition_frames(events: Vec<Event>) -> Vec<Event> {
    events
        .into_iter()
        .filter(|e| !matches!(e, Event::PartitionStart | Event::PartitionEnd(_)))
        .collect()
}

/// Runs the same partition through every chunk size from 1..=n+1 and asserts
/// the emitted decisions are byte-for-byte identical.
fn assert_chunk_invariant(
    pattern: &Pattern,
    num_vars: usize,
    rows: &[Row],
    predicate: common::Predicate,
) {
    let baseline = strip_partition_frames(feed(
        &mut build(pattern, num_vars, SkipMode::PastRow, predicate),
        1,
        rows,
        1,
    ));
    assert!(!baseline.is_empty() || rows.is_empty());
    for chunk in 1..=rows.len() + 1 {
        let actual = strip_partition_frames(feed(
            &mut build(pattern, num_vars, SkipMode::PastRow, predicate),
            1,
            rows,
            chunk,
        ));
        assert_eq!(
            baseline, actual,
            "chunk size {chunk} changed the decisions for {rows:?}"
        );
    }
}

#[test]
fn greedy_star_is_chunk_invariant() {
    let pattern = Pattern::concat(vec![Pattern::var(0), Pattern::star(Pattern::var(1))]);
    let rows = vec![
        Row { kind: "A", value: 1 },
        Row { kind: "B", value: 2 },
        Row { kind: "B", value: 3 },
        Row { kind: "X", value: 9 },
        Row { kind: "A", value: 4 },
        Row { kind: "B", value: 5 },
    ];
    assert_chunk_invariant(&pattern, 2, &rows, ab_predicate);
}

#[test]
fn bounded_lazy_and_greed_repeat_are_chunk_invariant() {
    let lazy = Pattern::repeat(Pattern::var(0), 1, Some(3), Greed::Lazy);
    let greedy = Pattern::repeat(Pattern::var(0), 1, Some(3), Greed::Greedy);
    let rows = vec![
        Row { kind: "A", value: 1 },
        Row { kind: "A", value: 2 },
        Row { kind: "A", value: 3 },
        Row { kind: "A", value: 4 },
        Row { kind: "X", value: 5 },
    ];
    assert_chunk_invariant(&lazy, 1, &rows, only_a_predicate);
    assert_chunk_invariant(&greedy, 1, &rows, only_a_predicate);
}

#[test]
fn ordered_alternation_is_chunk_invariant_and_matches_reference() {
    let pattern = Pattern::alt(vec![
        Pattern::var(0),
        Pattern::concat(vec![Pattern::var(0), Pattern::var(0)]),
    ]);
    let rows = vec![
        Row { kind: "A", value: 1 },
        Row { kind: "A", value: 2 },
        Row { kind: "A", value: 3 },
    ];
    assert_chunk_invariant(&pattern, 1, &rows, only_a_predicate);

    // The reference matcher must make the same per-start decisions.
    let rec_events = feed(
        &mut build(&pattern, 1, SkipMode::PastRow, only_a_predicate),
        1,
        &rows,
        2,
    );
    let rec_matches = matches_of(&rec_events);
    let mut start = 0usize;
    for m in rec_matches {
        let (end, captures) =
            reference_match(&pattern, &rows, start, only_a_predicate).expect("reference matches");
        assert_eq!(m.start_row as usize, start);
        assert_eq!(m.end_row as usize, end);
        assert_eq!(
            m.rows
                .iter()
                .map(|c| (c.row_index as usize, c.variable))
                .collect::<Vec<_>>(),
            captures
        );
        start = end + 1;
    }
}

#[test]
fn increasing_plus_is_chunk_invariant_with_last_capture_predicate() {
    let pattern = Pattern::plus(Pattern::var(0));
    let rows = vec![
        Row { kind: "A", value: 1 },
        Row { kind: "A", value: 4 },
        Row { kind: "A", value: 2 },
        Row { kind: "A", value: 8 },
        Row { kind: "A", value: 3 },
    ];
    assert_chunk_invariant(&pattern, 1, &rows, increasing_predicate);
}

/// The headline acceptance scenario: a match that is temporarily complete
/// must not be emitted while a later row can still extend a higher-priority
/// thread and change which variable owns the already-seen rows.
#[test]
fn temporary_match_reassigns_row_ownership_before_emission() {
    // DOWN UP{0,3} is bounded to four rows. After the first UP row a short
    // match is already a complete candidate, but the run stays undecided
    // until either three UP rows have been seen or every thread dies. The
    // greedy thread keeps extending and the later UP rows join variable 1
    // instead of starting new matches.
    let pattern = Pattern::concat(vec![
        Pattern::var(0),
        Pattern::repeat(Pattern::var(1), 0, Some(3), Greed::Greedy),
    ]);
    let rows = vec![
        Row { kind: "D", value: 10 },
        Row { kind: "U", value: 20 }, // complete short candidate, not emitted
        Row { kind: "U", value: 30 }, // greedy thread extends; still pending
        Row { kind: "U", value: 40 }, // bound reached: full span decided
        Row { kind: "Z", value: 1 },  // starts a fresh, failing run
    ];

    // One row per batch: nothing is emitted while the run is undecided.
    let mut one_by_one = build(&pattern, 2, SkipMode::PastRow, down_up_predicate);
    let mut early = Vec::new();
    for (idx, row) in rows.iter().enumerate().take(3) {
        let events = one_by_one.push_row(9, row.clone());
        assert!(
            events.iter().all(|e| !matches!(e, Event::Matched(_))),
            "row {idx} emitted a match too early: {events:?}"
        );
        early.extend(events.into_iter().filter(|e| !matches!(e, Event::PartitionStart)));
    }
    assert!(early.is_empty(), "undecided run emitted events early");
    // Row 3 reaches the four-row bound and settles the winner.
    let deciding = one_by_one.push_row(9, rows[3].clone());
    let m = matches_of(&deciding);
    assert_eq!(m.len(), 1);
    assert_eq!(m[0].start_row, 0);
    assert_eq!(m[0].end_row, 3);
    assert_eq!(m[0].capture_counts, vec![1, 3]);
    assert_eq!(
        m[0].rows.iter().map(|c| c.variable).collect::<Vec<_>>(),
        vec![0, 1, 1, 1]
    );
    one_by_one.push_row(9, rows[4].clone());
    one_by_one.end_partition();

    // Every other chunking must yield the exact same decision sequence.
    let reference = common::feed(
        &mut build(&pattern, 2, SkipMode::PastRow, down_up_predicate),
        9,
        &rows,
        1,
    );
    for chunk in 2..=rows.len() {
        let actual = common::feed(
            &mut build(&pattern, 2, SkipMode::PastRow, down_up_predicate),
            9,
            &rows,
            chunk,
        );
        assert_eq!(reference, actual, "chunk size {chunk} changed the result");
    }

    // Independent reference matcher agrees.
    let (end, captures) =
        reference_match(&pattern, &rows, 0, down_up_predicate).expect("reference finds a match");
    assert_eq!(end, 3);
    assert_eq!(captures, vec![(0, 0), (1, 1), (2, 1), (3, 1)]);
}

/// Greedy repetitions must backtrack to a shorter iteration count when the
/// longest path cannot complete: the winner is decided by ranked threads,
/// not by blindly keeping the longest partial span.
#[test]
fn greedy_bounded_repeat_backtracks_to_a_shorter_complete_match() {
    // A{1,3} B : three A rows followed by a B yields the full three-A match;
    // only two A rows followed by a B force the greedy thread to settle on
    // two iterations because the third A position is occupied by B.
    let pattern = Pattern::concat(vec![
        Pattern::repeat(Pattern::var(0), 1, Some(3), Greed::Greedy),
        Pattern::var(1),
    ]);

    let full = vec![
        Row { kind: "A", value: 1 },
        Row { kind: "A", value: 2 },
        Row { kind: "A", value: 3 },
        Row { kind: "B", value: 4 },
    ];
    let full_events = common::feed(
        &mut build(&pattern, 2, SkipMode::PastRow, ab_predicate),
        1,
        &full,
        1,
    );
    let full_matches = matches_of(&full_events);
    assert_eq!(full_matches.len(), 1);
    assert_eq!(full_matches[0].capture_counts, vec![3, 1]);

    let short = vec![
        Row { kind: "A", value: 1 },
        Row { kind: "A", value: 2 },
        Row { kind: "B", value: 3 },
    ];
    let short_events = common::feed(
        &mut build(&pattern, 2, SkipMode::PastRow, ab_predicate),
        1,
        &short,
        2,
    );
    let short_matches = matches_of(&short_events);
    assert_eq!(short_matches.len(), 1, "greedy backtracks to two iterations");
    assert_eq!(short_matches[0].start_row, 0);
    assert_eq!(short_matches[0].end_row, 2);
    assert_eq!(short_matches[0].capture_counts, vec![2, 1]);

    // The independent reference matcher reaches the same decision.
    let (end, captures) =
        reference_match(&pattern, &short, 0, ab_predicate).expect("reference matches");
    assert_eq!(end, 2);
    assert_eq!(
        short_matches[0]
            .rows
            .iter()
            .map(|c| (c.row_index as usize, c.variable))
            .collect::<Vec<_>>(),
        captures
    );
    assert_chunk_invariant(&pattern, 2, &short, ab_predicate);
    assert_chunk_invariant(&pattern, 2, &full, ab_predicate);
}

/// Greedy vs lazy repetitions make genuinely different choices; the operator
/// must not collapse everything into a longest match.
#[test]
fn greedy_and_lazy_disagree_and_both_are_chunk_invariant() {
    let rows = vec![
        Row { kind: "A", value: 1 },
        Row { kind: "A", value: 2 },
        Row { kind: "A", value: 3 },
        Row { kind: "X", value: 4 },
    ];
    // Non-nullable bounded repetitions avoid trailing empty-span matches so
    // the greediness contrast stays visible in the non-empty matches.
    let greedy = Pattern::repeat(Pattern::var(0), 1, Some(3), Greed::Greedy);
    let lazy = Pattern::repeat(Pattern::var(0), 1, Some(3), Greed::Lazy);

    let g_events = feed(
        &mut build(&greedy, 1, SkipMode::PastRow, only_a_predicate),
        1,
        &rows,
        1,
    );
    let l_events = feed(
        &mut build(&lazy, 1, SkipMode::PastRow, only_a_predicate),
        1,
        &rows,
        1,
    );
    let g = matches_of(&g_events);
    let l = matches_of(&l_events);
    assert_eq!(g[0].rows.len(), 3, "greedy takes all three A rows");
    assert_eq!(l.len(), 3, "lazy emits three single-row matches");
    assert!(l.iter().all(|m| m.rows.len() == 1));
    assert_chunk_invariant(&greedy, 1, &rows, only_a_predicate);
    assert_chunk_invariant(&lazy, 1, &rows, only_a_predicate);

    // A nullable lazy repetition additionally produces an empty-span match
    // at the trailing row it cannot consume.
    let nullable_lazy = Pattern::repeat(Pattern::var(0), 0, Some(3), Greed::Lazy);
    let n_events = feed(
        &mut build(&nullable_lazy, 1, SkipMode::PastRow, only_a_predicate),
        1,
        &rows,
        2,
    );
    let n = matches_of(&n_events);
    assert!(n.iter().any(|m| m.is_empty), "nullable lazy leaves an empty match");
    assert_chunk_invariant(&nullable_lazy, 1, &rows, only_a_predicate);
}

/// Nested alternation inside a repetition must keep written-order priority
/// and agree with the independent reference matcher under every chunking.
#[test]
fn nested_alt_inside_repeat_matches_reference_and_chunking() {
    // (A | B){1,4}: every row is consumed greedily; A and B accept disjoint
    // tags so per-row variable ownership is unambiguous.
    let pattern = Pattern::repeat(
        Pattern::alt(vec![Pattern::var(0), Pattern::var(1)]),
        1,
        Some(4),
        Greed::Greedy,
    );
    let rows = vec![
        Row { kind: "A", value: 1 },
        Row { kind: "B", value: 2 },
        Row { kind: "A", value: 3 },
    ];
    assert_chunk_invariant(&pattern, 2, &rows, ab_predicate);

    let events = common::feed(
        &mut build(&pattern, 2, SkipMode::PastRow, ab_predicate),
        1,
        &rows,
        3,
    );
    let got = matches_of(&events);
    assert_eq!(got.len(), 1);
    let (end, captures) =
        reference_match(&pattern, &rows, 0, ab_predicate).expect("reference matches");
    assert_eq!(got[0].end_row as usize, end);
    assert_eq!(
        got[0]
            .rows
            .iter()
            .map(|c| (c.row_index as usize, c.variable))
            .collect::<Vec<_>>(),
        captures
    );
    assert_eq!(captures, vec![(0, 0), (1, 1), (2, 0)]);
}
