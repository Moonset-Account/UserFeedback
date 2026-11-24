//! 持久小对象堆集成测试：
//! - 正常/边界路径与代次句柄；
//! - 撤根后立即复用槽；
//! - 对替换与撤根协议枚举每个屏障区间的全部合法断电像，
//!   用独立遍历审计恢复结果，并反推每个失败像的落盘顺序；
//! - 随机交错 + 随机断电的模糊测试。

use persistent_miniheap::medium::{OrderStrategy, PersistentMedia};
use persistent_miniheap::{HeapError, PersistentHeap};

const LINES: usize = persistent_miniheap::heap::TOTAL_LINES;
const SLOTS: usize = persistent_miniheap::heap::SLOT_COUNT;
const ROOTS: usize = persistent_miniheap::heap::ROOT_COUNT;
const MAX_PAYLOAD: usize = persistent_miniheap::heap::MAX_PAYLOAD_WORDS;

fn fresh_heap() -> PersistentHeap {
    PersistentHeap::format(PersistentMedia::cold_boot(LINES))
}

/// 仅枚举第 `start_region` 个屏障区间起（即某次操作自身产生的区间）
/// 的全部合法断电像。每个像都从同一历史 fork、注入断电、重新挂载，
/// 跑独立遍历审计后交给 `check` 做场景断言。
fn enumerate_recoveries_from<F>(media: &PersistentMedia, start_region: usize, mut check: F)
where
    F: FnMut(&PersistentHeap, &persistent_miniheap::RecoveryReport, &persistent_miniheap::CrashImage),
{
    let regions = media.legal_cuts();
    assert!(start_region < regions.len());
    let mut total = 0usize;
    for region in regions.iter().skip(start_region) {
        for image in &region.images {
            let mut probe = media.fork();
            probe.crash(&image.cut);
            let (heap, report) = PersistentHeap::mount(probe);
            let audit = heap.audit();
            assert!(audit.disjoint, "reachable/free overlap after recovery: {audit:?}");
            assert!(
                audit.covers_all_slots,
                "slot not covered after recovery: {audit:?}"
            );
            assert!(audit.headers_ok, "reachable header mismatch: {audit:?}");
            check(&heap, &report, image);
            total += 1;
        }
    }
    assert!(total >= 2, "exercise expected multiple legal crash images");
}

#[test]
fn publish_read_replace_happy_path() {
    let mut heap = fresh_heap();
    let h1 = heap.publish(0, &[10, 20, 30]).unwrap();
    assert_eq!(heap.read(h1).unwrap().payload, vec![10, 20, 30]);

    let h2 = heap.replace(0, &[40, 50]).unwrap();
    assert_ne!(h1, h2, "replacement must mint a new generation handle");
    // 新对象整个可见。
    assert_eq!(heap.read(h2).unwrap().payload, vec![40, 50]);
    // 旧句柄失效：旧槽已回收，不能读到旧对象。
    assert_eq!(heap.read(h1), Err(HeapError::BadSlot { slot: h1.slot }));
    // 根指向新句柄。
    assert_eq!(heap.root_handle(0).unwrap(), Some(h2));

    let audit = heap.audit();
    assert!(audit.disjoint && audit.covers_all_slots && audit.headers_ok);
}

#[test]
fn payload_length_boundaries_are_enforced() {
    let mut heap = fresh_heap();
    // 最小 1 个字、最大 MAX_PAYLOAD 个字都合法。
    let h_min = heap.publish(0, &[1]).unwrap();
    let big: Vec<u64> = (1..=MAX_PAYLOAD as u64).collect();
    let h_max = heap.publish(1, &big).unwrap();
    assert_eq!(heap.read(h_min).unwrap().payload, vec![1]);
    assert_eq!(heap.read(h_max).unwrap().payload, big);

    // 空载荷与超长载荷都被拒绝，且不消耗槽。
    let free_before = heap.free_table().len();
    assert!(matches!(
        heap.replace(0, &[]),
        Err(HeapError::BadPayloadLen { len: 0 })
    ));
    let too_long = vec![0u64; MAX_PAYLOAD + 1];
    assert!(matches!(
        heap.replace(0, &too_long),
        Err(HeapError::BadPayloadLen { .. })
    ));
    assert_eq!(heap.free_table().len(), free_before);
}

#[test]
fn capacity_is_conserved_and_returns_after_drop() {
    let mut heap = fresh_heap();
    // 4 个根先占 4 个槽。
    for root in 0..ROOTS {
        heap.publish(root, &[root as u64 + 1]).unwrap();
    }
    assert_eq!(heap.free_table().len(), SLOTS - ROOTS);

    // 替换借用一个空闲槽并在收尾归还，稳态容量守恒。
    let h = heap.replace(0, &[11]).unwrap();
    assert!(heap.read(h).is_ok());
    assert_eq!(heap.free_table().len(), SLOTS - ROOTS);
    assert_eq!(ROOTS + heap.free_table().len(), SLOTS);

    // 撤一个根后容量立刻回来，可以再分配。
    heap.drop_root(1).unwrap();
    let reused = heap.publish(1, &[777]).unwrap();
    assert!(heap.read(reused).is_ok());
    assert_eq!(heap.read(reused).unwrap().payload, vec![777]);
    assert_eq!(ROOTS + heap.free_table().len(), SLOTS);
}

#[test]
fn drop_root_then_immediately_reuse_same_slot_generation_bumps() {
    let mut heap = fresh_heap();
    let first = heap.publish(2, &[111, 222]).unwrap();
    let slot = first.slot;

    // 撤根：根项清空并屏障，紧接着回收，槽必须立刻出现在空闲表。
    heap.drop_root(2).unwrap();
    assert_eq!(heap.root_handle(2).unwrap(), None);
    let free = heap.free_table();
    assert!(
        free.iter().any(|h| h.slot == slot),
        "slot must be free immediately: {free:?}"
    );

    // 立刻复用同一槽（first-fit 会挑编号最小的空闲槽，撤下的槽号最小）。
    let second = heap.publish(2, &[333]).unwrap();
    assert_eq!(second.slot, slot, "expected immediate reuse of the same slot");
    // 代次：分配 +1，撤根回收再 +1，再次分配又 +1 => 比首代大 2。
    assert_eq!(second.generation, first.generation + 2);

    // 旧句柄绝不可能读到复用后的新对象。
    assert_eq!(
        heap.read(first),
        Err(HeapError::BadSlot { slot }),
        "stale handle must fail after slot reuse"
    );
    // 新句柄读到的是新对象。
    assert_eq!(heap.read(second).unwrap().payload, vec![333]);

    let audit = heap.audit();
    assert!(audit.disjoint && audit.covers_all_slots && audit.headers_ok);
}

#[test]
fn every_legal_power_cut_between_replace_barriers_lands_on_old_or_full_new_root() {
    let mut heap = fresh_heap();
    let old = heap.publish(0, &[1, 2, 3]).unwrap();
    let start = heap.media().region_count();

    // 在一次完整替换的历史上枚举该操作自身的全部合法断电像。
    let new = heap.replace(0, &[9, 8, 7, 6]).unwrap();

    enumerate_recoveries_from(heap.media(), start, |heap, _report, image| {
        let root = heap.root_handle(0).unwrap();
        match root {
            None => panic!("root 0 must always resolve to old or new object: {image:?}"),
            Some(h) => {
                // 二分不变量：要么旧根旧对象完整可见，要么新根新对象完整可见。
                if h == old {
                    assert_eq!(heap.read(h).unwrap().payload, vec![1, 2, 3]);
                } else {
                    assert_eq!(h, new, "root must be exactly old or new handle");
                    assert_eq!(heap.read(h).unwrap().payload, vec![9, 8, 7, 6]);
                }
                // 任何情况下旧句柄都不允许读到复用槽里的新对象。
                if let Ok(view) = heap.read(old) {
                    assert_eq!(
                        view.payload,
                        vec![1, 2, 3],
                        "old handle can only see old object"
                    );
                }
            }
        }
    });
}

#[test]
fn every_legal_power_cut_between_drop_barriers_never_resurrects() {
    let mut heap = fresh_heap();
    let h = heap.publish(1, &[42]).unwrap();
    let start = heap.media().region_count();
    heap.drop_root(1).unwrap();

    enumerate_recoveries_from(heap.media(), start, |heap, report, _image| {
        // 根要么还在（旧对象完整可达），要么彻底消失，绝不复活成半对象。
        match heap.root_handle(1).unwrap() {
            Some(alive) => {
                assert_eq!(alive, h);
                assert_eq!(heap.read(alive).unwrap().payload, vec![42]);
            }
            None => {
                assert_eq!(heap.read(h), Err(HeapError::BadSlot { slot: h.slot }));
            }
        }
        // 恢复报告统计与审计必须自洽。
        assert_eq!(report.reachable_slots + report.free_slots, SLOTS);
    });
}

#[test]
fn recovery_rolls_back_pending_slots_without_leak() {
    let mut heap = fresh_heap();
    let _keep = heap.publish(0, &[5]).unwrap();
    let start = heap.media().region_count();

    // 触发一次替换；其早期屏障区间里新槽会停在 PENDING。
    let _new = heap.replace(0, &[7, 8]).unwrap();

    let mut saw_pending_rollback = false;
    let mut saw_clean = false;
    for region in heap.media().legal_cuts().into_iter().skip(start) {
        for image in region.images {
            let mut probe = heap.media().fork();
            probe.crash(&image.cut);
            let (recovered, report) = PersistentHeap::mount(probe);
            if report.rolled_back_pending > 0 {
                saw_pending_rollback = true;
            }
            if report.rolled_back_pending == 0 {
                saw_clean = true;
            }
            let audit = recovered.audit();
            assert!(audit.disjoint && audit.covers_all_slots && audit.headers_ok);
            assert_eq!(report.reachable_slots + report.free_slots, SLOTS);
            // 根 0 始终解析到一个完整可读对象（旧或新）。
            let h = recovered.root_handle(0).unwrap().expect("root 0 survives");
            assert!(recovered.read(h).is_ok());
        }
    }
    assert!(saw_pending_rollback, "some power cut must leave a PENDING slot");
    assert!(saw_clean, "later power cuts must recover with nothing to roll back");
}

#[test]
fn reconstruct_disk_order_for_each_recovered_failure_image() {
    let mut heap = fresh_heap();
    let _old = heap.publish(0, &[1]).unwrap();
    let start = heap.media().region_count();
    let _new = heap.replace(0, &[2, 3]).unwrap();
    let media = heap.media();

    let mut checked = 0usize;
    for region in media.legal_cuts().into_iter().skip(start) {
        for image in region.images {
            // 从失败像反推落盘顺序，两种行间仲裁都必须能还原。
            for strategy in [OrderStrategy::LineAscending, OrderStrategy::LineDescending] {
                let rec = media
                    .reconstruct(region.region, &image.lines, strategy)
                    .expect("every legal failure image must be reconstructable");
                let replayed = PersistentMedia::replay_order(&rec.replay_order, media.line_count());
                assert_eq!(replayed, image.lines, "replay must reproduce the failure image");
                // 反推切割必须保持同行前缀。
                for (line, choice) in rec.cut.chosen.iter().enumerate() {
                    assert!(*choice <= region.pending[line].len());
                }
            }
            checked += 1;
        }
    }
    assert!(checked >= 4, "expected a rich set of failure images, got {checked}");
}

#[test]
fn free_table_compaction_never_loses_available_slots() {
    let mut heap = fresh_heap();
    // 反复在 4 个根上发布/替换/撤根，制造大量分配与回收；
    // 任何时刻空闲表项数 + 存活根占用槽数都必须等于总槽数，
    // 且连续分配不会因为表内空洞而误报 NoSpace。
    for round in 0..8 {
        for root in 0..ROOTS {
            let payload = vec![round as u64 * 10 + root as u64];
            if heap.root_handle(root).unwrap().is_some() {
                heap.replace(root, &payload).unwrap();
            } else {
                heap.publish(root, &payload).unwrap();
            }
        }
        // 撤掉偶数根再立刻重发，强制摘除非零表项并压缩。
        for root in (0..ROOTS).step_by(2) {
            heap.drop_root(root).unwrap();
            heap.publish(root, &[round as u64 + 100]).unwrap();
        }
        let live = ROOTS;
        assert_eq!(
            live + heap.free_table().len(),
            SLOTS,
            "round {round}: free table lost a slot"
        );
        // 空闲表内部不允许空洞：有效项必须连续位于表头。
        let entries = heap.free_table();
        assert_eq!(entries.len(), SLOTS - ROOTS);
        let mut seen_null = false;
        for index in 0..SLOTS {
            let word = heap.line_image(persistent_miniheap::heap::FREE_TABLE_LINE).0[index];
            if word == 0 {
                seen_null = true;
            } else if seen_null {
                panic!("free table has a hole at index {index}");
            }
        }
    }
}

#[test]
fn one_replacement_touches_many_lines_and_survives_any_interleaving() {
    let mut heap = fresh_heap();
    let old = heap.publish(0, &[1]).unwrap();
    let start = heap.media().region_count();
    let new = heap.replace(0, &[2, 3, 4]).unwrap();

    // 一次替换必然改动至少三类行：空闲表行、新槽行、根表行；
    // 回收阶段还会刷旧槽行与重建后的空闲表行。
    let media = heap.media();
    let regions: Vec<_> = media.legal_cuts().into_iter().skip(start).collect();
    let mut touched_lines = std::collections::HashSet::new();
    for region in &regions {
        for (line, queue) in region.pending.iter().enumerate() {
            if !queue.is_empty() {
                touched_lines.insert(line);
            }
        }
    }
    assert!(
        touched_lines.contains(&persistent_miniheap::heap::ROOT_LINE),
        "root table line must be rewritten"
    );
    assert!(
        touched_lines.contains(&persistent_miniheap::heap::FREE_TABLE_LINE),
        "free table line must be rewritten"
    );
    assert!(
        touched_lines.contains(&persistent_miniheap::heap::FIRST_SLOT_LINE),
        "at least one slot line must be rewritten"
    );
    assert!(touched_lines.len() >= 3, "one replacement spans multiple lines");

    // 任意行间交错落盘（legal_cuts 已枚举所有独立选择）后，
    // 恢复结果只可能是完整旧根或完整新根。
    for region in &regions {
        for image in &region.images {
            let mut probe = media.fork();
            probe.crash(&image.cut);
            let (recovered, _report) = PersistentHeap::mount(probe);
            let h = recovered.root_handle(0).unwrap().expect("root always resolves");
            if h == old {
                assert_eq!(recovered.read(h).unwrap().payload, vec![1]);
            } else {
                assert_eq!(h, new);
                assert_eq!(recovered.read(h).unwrap().payload, vec![2, 3, 4]);
            }
        }
    }
}

#[test]
fn interleaved_multi_root_replacements_keep_each_root_independent() {
    let mut heap = fresh_heap();
    let a0 = heap.publish(0, &[100]).unwrap();
    let b0 = heap.publish(1, &[200]).unwrap();
    let c0 = heap.publish(2, &[300]).unwrap();
    let start = heap.media().region_count();

    // 交错替换：A 换新、C 换新、A 再换新，B 全程不动。
    let a1 = heap.replace(0, &[101]).unwrap();
    let c1 = heap.replace(2, &[301, 302]).unwrap();
    let a2 = heap.replace(0, &[102]).unwrap();

    assert_eq!(heap.read(a2).unwrap().payload, vec![102]);
    assert_eq!(heap.read(b0).unwrap().payload, vec![200]);
    assert_eq!(heap.read(c1).unwrap().payload, vec![301, 302]);
    // 过期句柄全部失效。
    for stale in [a0, a1, c0] {
        assert_eq!(
            heap.read(stale),
            Err(HeapError::BadSlot {
                slot: stale.slot
            })
        );
    }

    // 在交错历史上枚举断电：每个根都只能落在完整对象上，根 B 恒稳定。
    enumerate_recoveries_from(heap.media(), start, |heap, _report, _image| {
        let a = heap.root_handle(0).unwrap().unwrap();
        let b = heap.root_handle(1).unwrap().unwrap();
        let c = heap.root_handle(2).unwrap().unwrap();
        assert!(heap.read(a).is_ok());
        assert_eq!(
            heap.read(b).unwrap().payload,
            vec![200],
            "untouched root B stable"
        );
        assert!(heap.read(c).is_ok());
        // 三个根互不相同槽。
        let slots = [a.slot, b.slot, c.slot];
        assert_eq!(slots.windows(2).filter(|w| w[0] == w[1]).count(), 0);
    });
}

// ---------- 随机交错 + 随机断电模糊测试 ----------

struct Rng(u64);

impl Rng {
    fn next(&mut self) -> u64 {
        // xorshift64
        let mut x = self.0;
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        self.0 = x;
        x
    }
    fn below(&mut self, n: usize) -> usize {
        (self.next() as usize) % n
    }
}

#[test]
fn fuzz_random_interleavings_and_power_cuts_have_no_leak_or_stale_read() {
    const ITERATIONS: usize = 200;
    let mut rng = Rng(0x9E37_79B9_7F4A_7C15);

    for iter in 0..ITERATIONS {
        let mut heap = fresh_heap();

        for _step in 0..10 {
            let root = rng.below(ROOTS);
            let occupied = heap.root_handle(root).unwrap().is_some();
            let len = 1 + rng.below(MAX_PAYLOAD);
            let payload: Vec<u64> =
                (0..len).map(|i| i as u64 * 7 + iter as u64 + 1).collect();

            let op = rng.below(3);
            let result = match (op, occupied) {
                (2, true) => heap.drop_root(root).map(|_| ()),
                (1, true) | (_, true) => heap.replace(root, &payload).map(|_| ()),
                _ => heap.publish(root, &payload).map(|_| ()),
            };
            // 唯一允许的错误是容量不足，且失败后堆必须仍可审计。
            if let Err(e) = result {
                assert_eq!(e, HeapError::NoSpace, "unexpected error {e:?}");
                let audit = heap.audit();
                assert!(audit.disjoint && audit.covers_all_slots);
                continue;
            }

            // 每步之后随机挑一个合法断电像注入（用 fork，不污染主历史）。
            if rng.below(2) == 0 {
                let regions = heap.media().legal_cuts();
                let region = &regions[rng.below(regions.len())];
                let image = &region.images[rng.below(region.images.len())];
                let mut probe = heap.media().fork();
                probe.crash(&image.cut);
                let (recovered, report) = PersistentHeap::mount(probe);

                let audit = recovered.audit();
                assert!(audit.disjoint, "iter {iter}: overlap {audit:?}");
                assert!(audit.covers_all_slots, "iter {iter}: leak {audit:?}");
                assert!(audit.headers_ok, "iter {iter}: bad headers");
                assert_eq!(
                    report.reachable_slots + report.free_slots,
                    SLOTS,
                    "iter {iter}: partition mismatch"
                );

                // 恢复后每个存活根都必须读到完整对象，绝不暴露半对象。
                for root in 0..ROOTS {
                    if let Some(h) = recovered.root_handle(root).unwrap() {
                        let view = recovered.read(h).expect("live root reads fully");
                        assert!(
                            !view.payload.is_empty() && view.payload.len() <= MAX_PAYLOAD,
                            "iter {iter}: malformed object"
                        );
                    }
                }
            }
        }

        // 每轮结束再做一次全量枚举，保证所有屏障区间的所有像都可恢复。
        let regions = heap.media().legal_cuts();
        for region in &regions {
            for image in &region.images {
                let mut probe = heap.media().fork();
                probe.crash(&image.cut);
                let (recovered, report) = PersistentHeap::mount(probe);
                assert!(recovered.audit().disjoint && recovered.audit().covers_all_slots);
                assert_eq!(report.reachable_slots + report.free_slots, SLOTS);
            }
        }
    }
}
