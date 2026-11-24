//! Streaming semantics: deferred emission, bounded history retention,
//! snapshot/restore of undecided greedy branches, after-match skip rules,
//! partition boundaries and empty-span matches.

mod common;

use common::{
    ab_predicate, build, down_up_predicate, feed, matches_of, only_a_predicate, Row,
};
use row_pattern_recognizer::{Event, Pattern, SkipMode, Tribool};

fn kinds(events: &[Event]) -> Vec<&'static str> {
    events
        .iter()
        .map(|e| match e {
            Event::PartitionStart => "start",
            Event::Matched(_) => "match",
            Event::UnmatchedRow { .. } => "unmatched",
            Event::PartitionEnd(_) => "end",
        })
        .collect()
}

#[test]
fn bounded_run_emits_only_after_the_winner_cannot_change() {
    // A B? is bounded to two rows: the first-row decision must stay pending
    // until either the second row arrives or the run cannot extend further.
    let pattern = Pattern::concat(vec![
        Pattern::var(0),
        Pattern::optional(Pattern::var(1)),
    ]);
    let mut rec = build(&pattern, 2, SkipMode::PastRow, ab_predicate);

    let e1 = rec.push_row(1, Row { kind: "A", value: 1 });
    assert!(
        e1.iter().all(|e| matches!(e, Event::PartitionStart)),
        "nothing is decidable after one row: {e1:?}"
    );
    assert_eq!(rec.retained_row_count(), 1);

    let e2 = rec.push_row(1, Row { kind: "B", value: 2 });
    // Bound reached: the match covering both rows is emitted now.
    let m = matches_of(&e2);
    assert_eq!(m.len(), 1);
    assert_eq!(m[0].end_row, 1);
    assert_eq!(m[0].capture_counts, vec![1, 1]);

    let e3 = rec.push_row(1, Row { kind: "A", value: 3 });
    assert!(e3.iter().all(|e| !matches!(e, Event::Matched(_))));
    let closed = rec.end_partition();
    let tail = matches_of(&closed);
    assert_eq!(tail.len(), 1);
    assert_eq!(tail[0].start_row, 2);
    assert_eq!(tail[0].end_row, 2, "optional B is skipped without a B row");
}

#[test]
fn history_is_pruned_to_rows_undecided_branches_can_still_read() {
    // Pattern length 2 means at most two retained rows per undecided run.
    let pattern = Pattern::concat(vec![Pattern::var(0), Pattern::var(0)]);
    let mut rec = build(&pattern, 1, SkipMode::PastRow, only_a_predicate);
    rec.push_row(1, Row { kind: "A", value: 1 });
    assert!(rec.retained_row_count() <= 2);
    rec.push_row(1, Row { kind: "A", value: 2 });
    // The match covering rows 0..=1 settles; row 0 is pruned, the successor
    // run starts at row 2.
    assert!(rec.retained_row_count() <= 1);
    rec.push_row(1, Row { kind: "A", value: 3 });
    assert!(rec.retained_row_count() <= 2);
    rec.end_partition();
}

#[test]
fn open_ended_pattern_keeps_history_until_partition_end() {
    let pattern = Pattern::plus(Pattern::var(0));
    let mut rec = build(&pattern, 1, SkipMode::PastRow, only_a_predicate);
    for i in 0..5 {
        rec.push_row(1, Row { kind: "A", value: i });
        assert_eq!(
            rec.retained_row_count(),
            (i + 1) as usize,
            "an open-ended run cannot be settled mid-partition"
        );
    }
    let events = rec.end_partition();
    let m = matches_of(&events);
    assert_eq!(m.len(), 1);
    assert_eq!(m[0].rows.len(), 5);
    assert_eq!(rec.retained_row_count(), 0);
}

#[test]
fn snapshot_and_restore_replay_pending_greedy_branches() {
    let pattern = Pattern::concat(vec![
        Pattern::var(0),
        Pattern::star(Pattern::var(1)),
    ]);
    let rows = vec![
        Row { kind: "D", value: 10 },
        Row { kind: "U", value: 20 },
        Row { kind: "U", value: 30 },
        Row { kind: "Z", value: 1 },
    ];

    // Reference run fed all rows straight through.
    let reference = feed(
        &mut build(&pattern, 2, SkipMode::PastRow, down_up_predicate),
        5,
        &rows,
        1,
    );

    // Restored run: snapshot after two rows, feed the rest on a clone, then
    // roll back and feed again - both continuations must match the reference.
    let mut rec = build(&pattern, 2, SkipMode::PastRow, down_up_predicate);
    rec.push_row(5, rows[0].clone());
    rec.push_row(5, rows[1].clone());
    let snapshot = rec.snapshot();
    assert!(rec.retained_row_count() >= 2);

    let mut probe = rec.clone();
    probe.push_row(5, rows[2].clone());
    let probe_tail = probe.push_row(5, rows[3].clone());
    let probe_end = probe.end_partition();
    let probe_events = [probe_tail, probe_end].concat();
    assert_eq!(
        strip_frames(&reference),
        strip_frames(&probe_events),
        "a clone of pending branches must reach the same winner"
    );

    rec.restore(snapshot);
    let tail = rec.push_row(5, rows[2].clone());
    let more = rec.push_row(5, rows[3].clone());
    let end = rec.end_partition();
    let restored_events = [tail, more, end].concat();
    assert_eq!(strip_frames(&reference), strip_frames(&restored_events));
}

fn strip_frames(events: &[Event]) -> Vec<Event> {
    events
        .iter()
        .filter(|e| !matches!(e, Event::PartitionStart | Event::PartitionEnd(_)))
        .cloned()
        .collect()
}

#[test]
fn skip_past_row_continues_after_the_whole_span() {
    // A A followed by more A rows: matches are non-overlapping spans.
    let pattern = Pattern::concat(vec![Pattern::var(0), Pattern::var(0)]);
    let rows = (0..5)
        .map(|i| Row { kind: "A", value: i })
        .collect::<Vec<_>>();
    let events = feed(
        &mut build(&pattern, 1, SkipMode::PastRow, only_a_predicate),
        1,
        &rows,
        2,
    );
    let m = matches_of(&events);
    assert_eq!(m.len(), 2);
    assert_eq!((m[0].start_row, m[0].end_row), (0, 1));
    assert_eq!((m[1].start_row, m[1].end_row), (2, 3));
    assert!(events
        .iter()
        .any(|e| matches!(e, Event::UnmatchedRow { row_index: 4 })));
}

fn down_then_up_predicate(
    variable: usize,
    row: &Row,
    last: Option<&Row>,
) -> Tribool {
    // Variable 0 (X) accepts X- or U-tagged rows as the match anchor but
    // rejects Q rows. Variable 1 (U) accepts U-tagged rows with a strictly
    // increasing value relative to the previous U capture in the same match.
    match variable {
        0 => match row.kind {
            "X" | "U" => Tribool::True,
            _ => Tribool::False,
        },
        1 => match (row.kind, last) {
            ("U", None) => Tribool::True,
            ("U", Some(prev)) => (row.value > prev.value).into(),
            _ => Tribool::False,
        },
        _ => Tribool::False,
    }
}

#[test]
fn skip_to_variable_continues_at_its_last_capture() {
    // X U* with AFTER MATCH SKIP TO U. The first match spans rows 0..=3 and
    // the next search starts at row 3 (the last U). Predicate references only
    // see captures of the *current* match, so the restarted run begins with an
    // empty capture again and re-captures row 3 as its X anchor. The Q row is
    // rejected by both variables and ends the second run.
    let pattern = Pattern::concat(vec![
        Pattern::var(0),
        Pattern::star(Pattern::var(1)),
    ]);
    let rows = vec![
        Row { kind: "X", value: 0 },
        Row { kind: "U", value: 1 },
        Row { kind: "U", value: 2 },
        Row { kind: "U", value: 3 },
        Row { kind: "Q", value: 9 },
    ];
    let events = feed(
        &mut build(
            &pattern,
            2,
            SkipMode::ToVariable(1),
            down_then_up_predicate,
        ),
        1,
        &rows,
        1,
    );
    let m = matches_of(&events);
    assert_eq!(m.len(), 2, "the restart at the last U begins a new match");
    assert_eq!((m[0].start_row, m[0].end_row), (0, 3));
    assert_eq!(m[0].capture_counts, vec![1, 3]);
    assert_eq!(m[1].start_row, 3, "restart exactly at the last U row");
    assert_eq!(m[1].end_row, 3, "the Q row cannot extend the restarted run");
    assert_eq!(m[1].capture_counts, vec![1, 0]);
    assert!(events
        .iter()
        .any(|e| matches!(e, Event::UnmatchedRow { row_index: 4 })));

    // A stream that keeps growing lets the restarted run extend into a
    // second, overlapping match anchored at the last U capture.
    let overlapping_rows = vec![
        Row { kind: "X", value: 0 },
        Row { kind: "U", value: 1 },
        Row { kind: "U", value: 2 },
        Row { kind: "U", value: 3 },
        Row { kind: "U", value: 4 },
    ];
    let events2 = feed(
        &mut build(
            &pattern,
            2,
            SkipMode::ToVariable(1),
            down_then_up_predicate,
        ),
        1,
        &overlapping_rows,
        2,
    );
    let m2 = matches_of(&events2);
    assert!(m2.len() >= 2, "skip-to must allow overlapping restarts");
    assert_eq!((m2[0].start_row, m2[0].end_row), (0, 4));
    assert_eq!(m2[1].start_row, 4, "restart at the last captured U row");
    // The restarted run re-captures row 4 as its X anchor (one U capture is
    // not possible there because its first edge requires an X-tagged row).
    assert_eq!(m2[1].capture_counts, vec![1, 0]);
}

#[test]
fn skip_to_uncaptured_variable_advances_one_row() {
    // Pattern where variable 1 never captures; SKIP TO 1 must fall back to
    // advancing the start by one row.
    let pattern = Pattern::concat(vec![
        Pattern::var(0),
        Pattern::star(Pattern::var(1)),
    ]);
    let rows = vec![
        Row { kind: "A", value: 1 },
        Row { kind: "A", value: 2 },
        Row { kind: "A", value: 3 },
    ];
    let events = feed(
        &mut build(
            &pattern,
            2,
            SkipMode::ToVariable(1),
            ab_predicate, // variable 1 (B) never matches
        ),
        1,
        &rows,
        1,
    );
    let m = matches_of(&events);
    assert_eq!(m.len(), 3);
    assert_eq!(m[0].start_row, 0);
    assert_eq!(m[1].start_row, 1);
    assert_eq!(m[2].start_row, 2);
    assert!(m.iter().all(|m| m.capture_counts[1] == 0));
}

#[test]
fn partition_boundaries_open_close_and_reset_statistics() {
    let pattern = Pattern::plus(Pattern::var(0));
    let mut rec = build(&pattern, 1, SkipMode::PastRow, only_a_predicate);

    // First partition: one A match and one unmatched X.
    let mut events = Vec::new();
    events.extend(rec.push_row(1u32, Row { kind: "A", value: 1 }));
    events.extend(rec.push_row(1u32, Row { kind: "X", value: 2 }));
    events.extend(rec.end_partition());

    // Second partition opens with a fresh local index space and statistics.
    events.extend(rec.push_row(2u32, Row { kind: "A", value: 3 }));
    events.extend(rec.push_row(2u32, Row { kind: "A", value: 4 }));
    events.extend(rec.push_row(2u32, Row { kind: "X", value: 5 }));
    events.extend(rec.end_partition());

    let sequence = kinds(&events);
    assert_eq!(sequence.first(), Some(&"start"));
    let starts = sequence.iter().filter(|k| **k == "start").count();
    let ends = sequence.iter().filter(|k| **k == "end").count();
    assert_eq!((starts, ends), (2, 2));

    let end_stats: Vec<_> = events
        .iter()
        .filter_map(|e| match e {
            Event::PartitionEnd(stats) => Some(stats.clone()),
            _ => None,
        })
        .collect();
    assert_eq!(end_stats[0].matches, 1);
    assert_eq!(end_stats[0].matched_rows, 1);
    assert_eq!(end_stats[0].unmatched_rows, 1);
    assert_eq!(end_stats[1].matches, 1);
    assert_eq!(end_stats[1].matched_rows, 2);
    assert_eq!(end_stats[1].unmatched_rows, 1);
    assert_eq!(rec.retained_row_count(), 0);

    // A partition key change mid-stream also closes and reopens cleanly.
    let mut switched = Vec::new();
    switched.extend(rec.push_row(7u32, Row { kind: "A", value: 9 }));
    switched.extend(rec.push_row(8u32, Row { kind: "A", value: 10 }));
    switched.extend(rec.end_partition());
    assert!(switched.iter().any(|e| matches!(e, Event::PartitionStart)));
    let last_stats = switched
        .iter()
        .rev()
        .find_map(|e| match e {
            Event::PartitionEnd(stats) => Some(stats),
            _ => None,
        })
        .unwrap();
    assert_eq!((last_stats.matches, last_stats.matched_rows), (1, 1));
}

#[test]
fn empty_span_pattern_emits_empty_matches_and_advances() {
    // A* can match the empty span. At each row the greedy empty candidate is
    // overtaken once the A-consuming thread completes; after the last row the
    // trailing empty match is emitted at partition end.
    let pattern = Pattern::star(Pattern::var(0));
    let rows = vec![
        Row { kind: "A", value: 1 },
        Row { kind: "A", value: 2 },
        Row { kind: "X", value: 3 },
    ];
    let events = feed(
        &mut build(&pattern, 1, SkipMode::PastRow, only_a_predicate),
        1,
        &rows,
        1,
    );
    let m = matches_of(&events);
    // First match greedily covers both A rows; row 2 fails, then an empty
    // match is emitted at index 3 (past the last row).
    assert!(m.iter().any(|m| !m.is_empty && m.end_row == 1));
    assert!(
        m.iter().any(|m| m.is_empty),
        "nullable pattern must emit a trailing empty-span match: {m:?}"
    );
    let stats = match events.last().unwrap() {
        Event::PartitionEnd(stats) => stats,
        other => panic!("expected partition end, got {other:?}"),
    };
    assert_eq!(stats.empty_matches, 1);
    assert_eq!(stats.matches, 1);
    assert_eq!(stats.unmatched_rows, 1, "the X row stays unmatched");
}

#[test]
fn nullable_pattern_on_all_failing_rows_reports_each_and_terminates() {
    // A* over rows that never satisfy A: every anchored run fails on its
    // start row and the rows are reported unmatched exactly once. The only
    // successful match is the single trailing empty-span match that a
    // nullable pattern owes one row past the partition end; the operator must
    // terminate rather than looping on empty-span candidates.
    let pattern = Pattern::star(Pattern::var(0));
    let rows = vec![
        Row { kind: "X", value: 1 },
        Row { kind: "X", value: 2 },
        Row { kind: "X", value: 3 },
    ];
    let events = feed(
        &mut build(&pattern, 1, SkipMode::PastRow, only_a_predicate),
        1,
        &rows,
        1,
    );
    let unmatched: Vec<u64> = events
        .iter()
        .filter_map(|e| match e {
            Event::UnmatchedRow { row_index } => Some(*row_index),
            _ => None,
        })
        .collect();
    assert_eq!(unmatched, vec![0, 1, 2]);
    let all_matches = matches_of(&events);
    let empty: Vec<_> = all_matches.iter().filter(|m| m.is_empty).collect();
    assert_eq!(empty.len(), 1, "exactly one trailing empty match");
    let stats = match events.last().unwrap() {
        Event::PartitionEnd(stats) => stats,
        other => panic!("expected partition end, got {other:?}"),
    };
    assert_eq!(stats.unmatched_rows, 3);
    assert_eq!(stats.matches, 0);
    assert_eq!(stats.empty_matches, 1);
}

#[test]
fn unknown_predicate_result_rejects_the_row_like_false() {
    // A predicate that returns Unknown on even rows must reject them; only
    // rows yielding True can be captured.
    fn unknown_on_even(_v: usize, row: &Row, _last: Option<&Row>) -> Tribool {
        if row.value % 2 == 0 {
            Tribool::Unknown
        } else {
            Tribool::True
        }
    }
    let pattern = Pattern::plus(Pattern::var(0));
    let rows = vec![
        Row { kind: "A", value: 1 },
        Row { kind: "A", value: 2 },
        Row { kind: "A", value: 3 },
    ];
    let events = feed(
        &mut build(&pattern, 1, SkipMode::PastRow, unknown_on_even),
        1,
        &rows,
        1,
    );
    let m = matches_of(&events);
    // Run at row 0 captures it, then row 1 (unknown) ends the run; row 2
    // starts a fresh single-row match.
    assert_eq!(m.len(), 2);
    assert_eq!((m[0].start_row, m[0].end_row), (0, 0));
    assert_eq!((m[1].start_row, m[1].end_row), (2, 2));
    assert!(events
        .iter()
        .any(|e| matches!(e, Event::UnmatchedRow { row_index: 1 })));
}

#[test]
fn dead_branches_settle_early_and_report_unmatched_rows() {
    // A B requires both predicates; a stream without any B settles each run
    // as soon as its thread dies, without waiting for the two-row bound.
    let pattern = Pattern::concat(vec![Pattern::var(0), Pattern::var(1)]);
    let rows = vec![
        Row { kind: "A", value: 1 },
        Row { kind: "A", value: 2 },
        Row { kind: "A", value: 3 },
    ];
    let mut rec = build(&pattern, 2, SkipMode::PastRow, ab_predicate);
    let e1 = rec.push_row(1, rows[0].clone());
    assert!(e1.iter().all(|e| matches!(e, Event::PartitionStart)));
    let e2 = rec.push_row(1, rows[1].clone());
    // The B predicate fails on an A row: the run dies and row 0 is reported
    // unmatched immediately while the successor run starts at row 1.
    assert!(e2
        .iter()
        .any(|e| matches!(e, Event::UnmatchedRow { row_index: 0 })));
    let rest = rec.push_row(1, rows[2].clone());
    let end = rec.end_partition();
    let all = [e1, e2, rest, end].concat();
    let unmatched: Vec<u64> = all
        .iter()
        .filter_map(|e| match e {
            Event::UnmatchedRow { row_index } => Some(*row_index),
            _ => None,
        })
        .collect();
    assert_eq!(unmatched, vec![0, 1, 2]);
    assert!(matches_of(&all).is_empty());
}
