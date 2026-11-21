//! 集成测试：正常场景、边界场景、交错（混合空值/多行）场景，
//! 以及与朴素参考实现的确定性随机交叉验证和查找次数断言。

use row_not_in::{naive_not_in, row_equals, NotInIndex, ProbeReport, Row, TriBool};

/// 题面给定场景：右表只有 (2,NULL,7)，左行 (1,NULL,7) 仅凭第一列即判定不等，
/// NOT IN 为真，WHERE 必须保留该行。
#[test]
fn spec_example_first_column_distinguishes_and_row_is_retained() {
    let right: Vec<Row> = vec![(Some(2), None, Some(7))];
    let idx = NotInIndex::build(right);

    let probe_left: Row = (Some(1), None, Some(7));
    let (result, report) = idx.probe(probe_left);
    assert_eq!(result, TriBool::True);
    assert!(!report.saw_unknown);
    assert!(!report.found_true_match);

    let left_rows: Vec<Row> = vec![
        (Some(1), None, Some(7)), // 0：第一列 1 != 2，保留
        (Some(2), None, Some(7)), // 1：与右行非空列全等，左行含空 → 未知，过滤
    ];
    assert_eq!(idx.retain_where(&left_rows), vec![0]);
}

#[test]
fn fully_non_null_left_true_match_yields_false() {
    let right: Vec<Row> = vec![
        (Some(1), Some(2), Some(3)),
        (Some(4), Some(5), Some(6)),
    ];
    let idx = NotInIndex::build(right);

    assert_eq!(idx.probe((Some(1), Some(2), Some(3))).0, TriBool::False);
    assert_eq!(idx.probe((Some(4), Some(5), Some(6))).0, TriBool::False);
    // 完全不同的值：无任何匹配 → 真。
    assert_eq!(idx.probe((Some(7), Some(8), Some(9))).0, TriBool::True);
    // 第一列不同即整体不等，即便后两列相同。
    assert_eq!(idx.probe((Some(9), Some(2), Some(3))).0, TriBool::True);
}

#[test]
fn null_in_right_makes_match_unknown_unless_true_match_exists() {
    let right: Vec<Row> = vec![(Some(1), None, Some(3))];
    let idx = NotInIndex::build(right);

    // 左行无空，非空列全部相等，但右行含空 → 未知。
    let (result, report) = idx.probe((Some(1), Some(2), Some(3)));
    assert_eq!(result, TriBool::Unknown);
    assert!(report.saw_unknown);

    // 左行同样含空且非空列相等 → 未知。
    assert_eq!(idx.probe((Some(1), None, Some(3))).0, TriBool::Unknown);

    // 任一非空列不等 → 假匹配，整体为真。
    assert_eq!(idx.probe((Some(9), Some(2), Some(3))).0, TriBool::True);
}

#[test]
fn true_match_dominates_unknown_match() {
    // 右表同时含真匹配行与仅产生未知的行，NOT IN 必须为假。
    let right: Vec<Row> = vec![
        (Some(1), None, Some(3)),
        (Some(1), Some(2), Some(3)),
    ];
    let idx = NotInIndex::build(right);
    let (result, report) = idx.probe((Some(1), Some(2), Some(3)));
    assert_eq!(result, TriBool::False);
    assert!(report.found_true_match);
}

#[test]
fn all_null_left_is_unknown_against_nonempty_table_and_true_against_empty() {
    let idx = NotInIndex::build(vec![(Some(1), Some(2), Some(3))]);
    // 全空左行与任何右行比较都不可能为假或真，只能是未知。
    assert_eq!(idx.probe((None, None, None)).0, TriBool::Unknown);

    let empty = NotInIndex::build(vec![]);
    assert_eq!(empty.probe((None, None, None)).0, TriBool::True);
}

#[test]
fn all_null_right_row_only_produces_unknown() {
    // 右行全空：与任何左行比较都是未知，绝不可能成为真匹配。
    let idx = NotInIndex::build(vec![(None, None, None)]);
    assert_eq!(idx.probe((Some(1), Some(2), Some(3))).0, TriBool::Unknown);
    assert_eq!(idx.probe((None, None, None)).0, TriBool::Unknown);
    assert_eq!(idx.probe((Some(0), None, Some(0))).0, TriBool::Unknown);
}

#[test]
fn duplicate_and_extra_right_rows_do_not_change_results() {
    let right: Vec<Row> = vec![
        (Some(1), None, Some(3)),
        (Some(1), None, Some(3)),
        (Some(1), None, Some(3)),
    ];
    let idx = NotInIndex::build(right);
    assert_eq!(idx.len(), 3);
    assert_eq!(idx.probe((Some(1), Some(2), Some(3))).0, TriBool::Unknown);
    assert_eq!(idx.probe((Some(2), Some(2), Some(3))).0, TriBool::True);
}

#[test]
fn interleaved_null_patterns_across_many_right_rows() {
    // 八种空值组合各放一行，值围绕 10 构造，制造交错的真/未知/假匹配。
    let right: Vec<Row> = vec![
        (Some(10), Some(11), Some(12)), // 0b111
        (Some(10), Some(11), None),     // 0b110
        (Some(10), None, Some(12)),     // 0b101
        (Some(10), None, None),         // 0b100
        (None, Some(11), Some(12)),     // 0b011
        (None, Some(11), None),         // 0b010
        (None, None, Some(12)),         // 0b001
        (None, None, None),             // 0b000
    ];
    // 先建不含全空行的 7 行子表索引（随后完整右表所有权再移入主索引）。
    let idx2 = NotInIndex::build(right[..7].to_vec());
    let idx = NotInIndex::build(right);

    // 与 0b111 行完全相等 → 假。
    assert_eq!(
        idx.probe((Some(10), Some(11), Some(12))).0,
        TriBool::False
    );
    // 非空列与 0b101 行相等、自身第二列为空 → 未知。
    assert_eq!(idx.probe((Some(10), None, Some(12))).0, TriBool::Unknown);
    // 左行无空，与 0b110 行的非空列相等、右行第三列为空 → 未知。
    assert_eq!(
        idx.probe((Some(10), Some(11), Some(99))).0,
        TriBool::Unknown
    );
    // 所有非空列值都避开右表且能与某右行构成“不冲突但双方含空”时为未知：
    // (None,None,Some(99)) 与 (Some(10),None,None) 无非空列冲突，双方都含空 → 未知。
    assert_eq!(idx.probe((None, None, Some(99))).0, TriBool::Unknown);
    // 由于右表含全空行 (None,None,None)，它对任何左行都只能产生未知，
    // 因此这张表上 NOT IN 不可能为真：连三列全新的值也只能得到未知。
    assert_eq!(
        idx.probe((Some(77), Some(88), Some(99))).0,
        TriBool::Unknown
    );

    // 去掉全空行后，三列全新且与每个单空列右行都能制造非空冲突的左行才为真。
    assert_eq!(
        idx2.probe((Some(77), Some(88), Some(99))).0,
        TriBool::True
    );
    // 仅第三列非空且命中 0b001/0b011/0b101/0b111 行 → 未知。
    assert_eq!(idx.probe((None, None, Some(12))).0, TriBool::Unknown);
}

#[test]
fn retain_where_keeps_only_true_rows_and_preserves_original_indices() {
    let right: Vec<Row> = vec![(Some(2), None, Some(7)), (Some(5), Some(5), Some(5))];
    let idx = NotInIndex::build(right);
    let left: Vec<Row> = vec![
        (Some(1), None, Some(7)),    // 0: 真（第一列不等）
        (Some(2), None, Some(7)),    // 1: 未知（含空匹配）
        (Some(5), Some(5), Some(5)), // 2: 假（真匹配）
        (None, None, None),          // 3: 未知
        (Some(9), Some(9), Some(9)), // 4: 真
    ];
    assert_eq!(idx.retain_where(&left), vec![0, 4]);
    assert_eq!(
        idx.probe_all(&left),
        vec![
            TriBool::True,
            TriBool::Unknown,
            TriBool::False,
            TriBool::Unknown,
            TriBool::True,
        ]
    );
}

#[test]
fn index_lookup_count_depends_only_on_null_pattern() {
    // 右表规模差异悬殊，但同一空值组合的查找次数必须完全相同。
    let small = NotInIndex::build(vec![(Some(1), Some(2), Some(3))]);
    let mut big_rows: Vec<Row> = Vec::new();
    for a in 0..50 {
        for b in 0..50 {
            big_rows.push((Some(a), Some(b), None));
        }
    }
    let big = NotInIndex::build(big_rows);
    assert_eq!(big.len(), 2500);

    let cases: [(Row, usize); 8] = [
        ((Some(1), Some(2), Some(3)), 8), // 0 个空列 → 2^3
        ((Some(1), Some(2), None), 4),    // 1 个空列 → 2^2
        ((Some(1), None, Some(3)), 4),
        ((None, Some(2), Some(3)), 4),
        ((Some(1), None, None), 2), // 2 个空列 → 2^1
        ((None, Some(2), None), 2),
        ((None, None, Some(3)), 2),
        ((None, None, None), 1), // 3 个空列 → 2^0
    ];

    for (row, expected) in cases {
        let r1 = small.probe(row).1;
        let r2 = big.probe(row).1;
        assert_eq!(r1.index_lookups, expected, "small table: {row:?}");
        assert_eq!(r2.index_lookups, expected, "big table: {row:?}");
        assert_eq!(r1.index_lookups, r2.index_lookups);
    }
}

#[test]
fn probe_report_fields_are_consistent() {
    let idx = NotInIndex::build(vec![(Some(1), None, Some(3)), (Some(4), Some(5), Some(6))]);

    let (_, report_true): (TriBool, ProbeReport) = idx.probe((Some(9), Some(9), Some(9)));
    assert!(!report_true.saw_unknown);
    assert!(!report_true.found_true_match);
    assert_eq!(report_true.index_lookups, 8);

    let (_, report_unknown) = idx.probe((Some(1), Some(2), Some(3)));
    assert!(report_unknown.saw_unknown);
    assert!(!report_unknown.found_true_match);

    let (_, report_false) = idx.probe((Some(4), Some(5), Some(6)));
    assert!(report_false.found_true_match);
}

/// 确定性线性同余生成器，保证测试可复现、不依赖随机数来源。
struct Lcg(u64);

impl Lcg {
    fn next_u64(&mut self) -> u64 {
        // MMIX 参数。
        self.0 = self
            .0
            .wrapping_mul(6_364_136_223_846_793_005)
            .wrapping_add(1_442_695_040_888_963_407);
        self.0
    }

    /// 以约 1/3 概率生成 NULL，否则生成小范围整数。
    fn next_nullable(&mut self) -> Option<i64> {
        match self.next_u64() % 3 {
            0 => None,
            1 => Some((self.next_u64() % 7) as i64),
            _ => Some(-((self.next_u64() % 5) as i64)),
        }
    }

    fn next_row(&mut self) -> Row {
        (
            self.next_nullable(),
            self.next_nullable(),
            self.next_nullable(),
        )
    }
}

#[test]
fn randomized_cross_check_against_naive_implementation() {
    let mut rng = Lcg(0x1234_5678_9abc_def0);

    // 多轮不同右表规模（含 0 行、1 行的边界）。
    for right_len in [0usize, 1, 2, 7, 25, 80] {
        let right: Vec<Row> = (0..right_len).map(|_| rng.next_row()).collect();
        let index = NotInIndex::build(right.clone());

        for _ in 0..400 {
            let left = rng.next_row();
            let (got, report) = index.probe(left);
            let want = naive_not_in(left, &right);
            assert_eq!(
                got, want,
                "left={left:?} right_len={right_len} mismatch vs naive"
            );

            // 查找次数仅由非空列数决定。
            let non_null = usize::from(left.0.is_some())
                + usize::from(left.1.is_some())
                + usize::from(left.2.is_some());
            assert_eq!(report.index_lookups, 1usize << non_null);

            // 报告标志与三值结果自洽。
            match got {
                TriBool::False => assert!(report.found_true_match),
                TriBool::Unknown => {
                    assert!(report.saw_unknown);
                    assert!(!report.found_true_match);
                }
                TriBool::True => {
                    assert!(!report.saw_unknown);
                    assert!(!report.found_true_match);
                }
            }
        }
    }
}

#[test]
fn randomized_retain_where_matches_naive_filter() {
    let mut rng = Lcg(0x0bad_c0de_dead_beef);
    let right: Vec<Row> = (0..30).map(|_| rng.next_row()).collect();
    let index = NotInIndex::build(right.clone());
    let lefts: Vec<Row> = (0..120).map(|_| rng.next_row()).collect();

    let expected: Vec<usize> = lefts
        .iter()
        .enumerate()
        .filter_map(|(i, row)| {
            (naive_not_in(*row, &right) == TriBool::True).then_some(i)
        })
        .collect();

    assert_eq!(index.retain_where(&lefts), expected);
}

#[test]
fn row_equals_remains_consistent_with_probe_on_singleton_tables() {
    // 对每个右行单独建索引，probe 结果必须等价于对该行做一次三值行比较后的 NOT IN。
    let mut rng = Lcg(1);
    for _ in 0..100 {
        let left = rng.next_row();
        let right = rng.next_row();
        let index = NotInIndex::build(vec![right]);
        let got = index.probe(left).0;
        let want = match row_equals(left, right) {
            TriBool::True => TriBool::False,
            TriBool::Unknown => TriBool::Unknown,
            TriBool::False => TriBool::True,
        };
        assert_eq!(got, want, "left={left:?} right={right:?}");
    }
}
