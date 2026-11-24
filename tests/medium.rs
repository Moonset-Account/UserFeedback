//! 持久介质后端单元测试：原子写、刷行/屏障语义、
//! 合法断电像枚举、断电注入与落盘顺序反推。

use persistent_miniheap::medium::{
    LineImage, OrderStrategy, PersistentMedia, ReconstructError, LINE_WORDS,
};
use persistent_miniheap::CrashCut;

fn img(words: &[u64]) -> LineImage {
    let mut a = [0u64; LINE_WORDS];
    a[..words.len()].copy_from_slice(words);
    LineImage(a)
}

#[test]
fn unflushed_writes_are_volatile_and_lost_on_power_cut() {
    let mut m = PersistentMedia::cold_boot(4);
    m.write_word(0, 11);
    m.write_word(9, 22); // 第二行（全局字 8..15）
    // 没有任何 flush：第一个屏障区间内没有待持久行像，唯一合法像就是零基线。
    let regions = m.legal_cuts();
    assert_eq!(regions.len(), 1);
    assert_eq!(regions[0].images.len(), 1);
    assert!(regions[0]
        .images[0]
        .lines
        .iter()
        .all(|l| *l == LineImage::ZERO));

    let cut = regions[0].images[0].cut.clone();
    m.crash(&cut);
    assert!(m.is_powered()); // crash 后自动重新上电
    assert_eq!(m.read_word(0), 0);
    assert_eq!(m.read_word(9), 0);
}

#[test]
fn flush_takes_a_snapshot_later_writes_do_not_change_pending_image() {
    let mut m = PersistentMedia::cold_boot(2);
    m.write_word(0, 1);
    m.flush_line(0); // 待持久快照 [1,0,...]
    m.write_word(0, 2); // 之后的易失写不影响已登记快照

    let region = &m.legal_cuts()[0];
    // 该行有 1 个待持久像：选择 0 -> 零，选择 1 -> 快照值 1（不是 2）。
    let with_flush = region
        .images
        .iter()
        .find(|ci| ci.cut.chosen[0] == 1)
        .expect("image with the flush applied");
    assert_eq!(with_flush.lines[0].word(0), 1);
    let without = region
        .images
        .iter()
        .find(|ci| ci.cut.chosen[0] == 0)
        .expect("image without the flush");
    assert_eq!(without.lines[0].word(0), 0);
}

#[test]
fn same_line_keeps_prefix_order_other_lines_are_independent() {
    let mut m = PersistentMedia::cold_boot(3);
    // 行 0 刷两次，行 1（全局字 8 起）刷一次；都发生在第一道屏障之前（同一区间）。
    m.write_word(0, 100);
    m.flush_line(0);
    m.write_word(8, 200);
    m.flush_line(1);
    m.write_word(0, 101);
    m.flush_line(0);

    let region = &m.legal_cuts()[0];
    assert_eq!(region.pending[0].len(), 2);
    assert_eq!(region.pending[1].len(), 1);
    // 组合数 = (2+1) * (1+1) * 1 = 6，全部不同切割。
    assert_eq!(region.images.len(), 6);

    for ci in &region.images {
        let k0 = ci.cut.chosen[0];
        let k1 = ci.cut.chosen[1];
        // 行 0 只能是前缀：0 -> 0, 1 -> 100, 2 -> 101，绝不跳号。
        let expected0 = match k0 {
            0 => 0,
            1 => 100,
            2 => 101,
            other => panic!("bad prefix {other}"),
        };
        assert_eq!(ci.lines[0].word(0), expected0);
        // 行 1 的选择与行 0 完全独立。
        assert_eq!(ci.lines[1].word(0), if k1 == 1 { 200 } else { 0 });
        assert_eq!(ci.lines[2], LineImage::ZERO);
    }
}

#[test]
fn barrier_makes_every_prior_flush_durable_and_opens_new_region() {
    let mut m = PersistentMedia::cold_boot(2);
    m.write_word(0, 7);
    m.flush_line(0);
    m.barrier();
    // 新的屏障区间：基线必须包含上一区间全部内容。
    let regions = m.legal_cuts();
    assert_eq!(regions.len(), 2);
    assert_eq!(regions[1].baseline[0].word(0), 7);
    assert_eq!(regions[1].images.len(), 1); // 新区间无待持久，仅基线像

    // 在新区间再刷不同行（行 1 的起始全局字是 8）。
    m.write_word(8, 9);
    m.flush_line(1);
    let regions = m.legal_cuts();
    assert_eq!(regions.len(), 2);
    assert_eq!(regions[1].images.len(), 2);
    for ci in &regions[1].images {
        // 无论新区间怎么切，行 0 的旧内容都已被屏障保证持久。
        assert_eq!(ci.lines[0].word(0), 7);
        assert_eq!(ci.lines[1].word(0), if ci.cut.chosen[1] == 1 { 9 } else { 0 });
    }

    // 注入一个“新区间刷出行没落盘”的断电，旧内容仍在。
    let cut = regions[1]
        .images
        .iter()
        .find(|ci| ci.cut.chosen[1] == 0)
        .unwrap()
        .cut
        .clone();
    m.crash(&cut);
    assert_eq!(m.read_word(0), 7);
    assert_eq!(m.read_word(8), 0);
}

#[test]
fn crash_applies_exactly_the_chosen_prefixes() {
    let region = {
        let mut m = PersistentMedia::cold_boot(2);
        m.write_word(0, 1);
        m.flush_line(0);
        m.write_word(0, 2);
        m.flush_line(0);
        m.write_word(8, 5);
        m.flush_line(1);
        m.legal_cuts()[0].clone()
    };
    for ci in region.images {
        let mut probe = PersistentMedia::cold_boot(2);
        // 复刻同样的事件序列。
        probe.write_word(0, 1);
        probe.flush_line(0);
        probe.write_word(0, 2);
        probe.flush_line(0);
        probe.write_word(8, 5); // 行 1
        probe.flush_line(1);
        probe.crash(&ci.cut);
        assert_eq!(probe.line_image(0), ci.lines[0]);
        assert_eq!(probe.line_image(1), ci.lines[1]);
    }
}

#[test]
#[should_panic(expected = "not a prefix")]
fn non_prefix_or_oversized_cut_is_rejected() {
    let mut m = PersistentMedia::cold_boot(1);
    m.write_word(0, 1);
    m.flush_line(0);
    m.write_word(0, 2);
    m.flush_line(0);
    // 只有 2 个待持久像，要求落 3 个，超出前缀范围。
    let bad = CrashCut {
        region: 0,
        chosen: vec![3],
    };
    m.crash(&bad);
}

#[test]
fn reconstruct_reproduces_every_legal_crash_image() {
    let mut m = PersistentMedia::cold_boot(3);
    // 每个行像都取不同内容，保证每个前缀长度对应唯一行像。
    m.write_word(0, 100);
    m.flush_line(0); // 行 0 行像 A：[100,0,...]
    m.write_word(8, 200);
    m.flush_line(1);
    m.write_word(0, 0);
    m.write_word(1, 101);
    m.flush_line(0); // 行 0 行像 B：[0,101,...]，与 A 不同
    m.barrier();
    m.write_word(16, 300);
    m.flush_line(2);

    // 对两个区间的每个合法像，都必须能反推出落盘顺序并重放还原。
    for region in m.legal_cuts() {
        for ci in &region.images {
            for strategy in [OrderStrategy::LineAscending, OrderStrategy::LineDescending] {
                let rec = m
                    .reconstruct(region.region, &ci.lines, strategy)
                    .expect("legal image must be reconstructable");
                // 反推切割必须是同行前缀；当相邻切割行像相同（重复刷同值）
                // 时，反推得到最靠后的切割，这仍是合法前缀。
                for (line, choice) in rec.cut.chosen.iter().enumerate() {
                    let queue_len = region.pending[line].len();
                    assert!(*choice <= queue_len, "reconstructed cut must be a prefix");
                }
                let replayed = PersistentMedia::replay_order(&rec.replay_order, m.line_count());
                assert_eq!(replayed, ci.lines, "replay must reproduce the observed failure image");
                // 顺序校验：按行拆分重放序列，每行都必须等于
                // “可选基线行像 + 当前区间待持久队列的一个前缀”。
                for line in 0..m.line_count() {
                    let per_line: Vec<LineImage> = rec
                        .replay_order
                        .iter()
                        .filter(|(l, _)| *l == line)
                        .map(|(_, image)| *image)
                        .collect();
                    let mut expected: Vec<LineImage> = Vec::new();
                    if region.baseline[line] != LineImage::ZERO {
                        expected.push(region.baseline[line]);
                    }
                    expected.extend(region.pending[line].iter().take(rec.cut.chosen[line]));
                    assert_eq!(per_line, expected, "line {line} replay order must follow log order");
                }
            }
        }
    }
}

#[test]
fn reconstruct_detects_impossible_and_wrong_region_targets() {
    let mut m = PersistentMedia::cold_boot(2);
    // 区间 0：行 0 刷值 1。
    m.write_word(0, 1);
    m.flush_line(0);
    m.barrier();
    // 区间 1：行 1 刷值 2（行 0 的基线仍是值 1）。
    m.write_word(8, 2);
    m.flush_line(1);

    // 区间 1 的目标像里出现一个从未刷出过的值。
    let mut impossible = vec![LineImage::ZERO; 2];
    impossible[0] = img(&[999]);
    let err = m
        .reconstruct(1, &impossible, OrderStrategy::LineAscending)
        .unwrap_err();
    assert_eq!(err, ReconstructError::NoMatchingImage { line: 0 });

    // 值 2 的行像只存在于区间 1；在区间 0 里反推应报“区间不符”。
    let mut stale = vec![LineImage::ZERO; 2];
    stale[1] = img(&[2]);
    let err = m
        .reconstruct(0, &stale, OrderStrategy::LineAscending)
        .unwrap_err();
    assert_eq!(
        err,
        ReconstructError::OutsideRegion {
            line: 1,
            given_region: 0,
            found_region: 1,
        }
    );
}

#[test]
fn word_writes_are_independent_across_lines_and_preserve_full_line_images() {
    let mut m = PersistentMedia::cold_boot(2);
    for w in 0..(2 * LINE_WORDS) {
        m.write_word(w, (w as u64) + 10);
    }
    m.flush_line(0);
    m.flush_line(1);
    m.barrier();
    for w in 0..(2 * LINE_WORDS) {
        assert_eq!(m.read_word(w), (w as u64) + 10);
    }
    // 屏障后断电：内容全部保留。
    let cut = m.legal_cuts().last().unwrap().images[0].cut.clone();
    m.crash(&cut);
    for w in 0..(2 * LINE_WORDS) {
        assert_eq!(m.read_word(w), (w as u64) + 10);
    }
}
