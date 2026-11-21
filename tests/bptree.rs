//! B+ 树集成测试：正常场景、边界场景与随机交错场景。
//!
//! 所有断言都针对真实数据结构操作后的状态，不使用任何预置输出。

use bplus_tree_index::{
    BPlusTree, Entry, Page, PageId, MAX_CHILDREN, MAX_LEAF_ENTRIES, MIN_CHILDREN,
    MIN_LEAF_ENTRIES,
};
use std::collections::{BTreeSet, HashSet};

/// 简易确定性 LCG 随机数发生器，保证交错场景可复现。
struct Rng(u64);

impl Rng {
    fn new(seed: u64) -> Self {
        Self(seed)
    }

    fn next_u64(&mut self) -> u64 {
        self.0 = self
            .0
            .wrapping_mul(6_364_136_223_846_793_005)
            .wrapping_add(1_442_695_040_888_963_407);
        self.0
    }

    fn below(&mut self, bound: usize) -> usize {
        (self.next_u64() % bound as u64) as usize
    }
}

/// 测试侧的子树最小索引项（沿公共只读 API 下降）。
fn subtree_min_entry(tree: &BPlusTree, mut id: PageId) -> Entry {
    loop {
        match tree.get_page(id).unwrap() {
            Page::Leaf(leaf) => return leaf.entries[0],
            Page::Internal(internal) => id = internal.children[0],
        }
    }
}

/// 测试侧的子树最大索引项。
fn subtree_max_entry(tree: &BPlusTree, mut id: PageId) -> Entry {
    loop {
        match tree.get_page(id).unwrap() {
            Page::Leaf(leaf) => return *leaf.entries.last().unwrap(),
            Page::Internal(internal) => id = *internal.children.last().unwrap(),
        }
    }
}

/// 全量结构不变量检查，返回沿叶链拼接出的全部索引项。
///
/// 覆盖：
/// 1. 从根可达的页集合 == 实际存在的页集合（无悬挂引用、无孤儿页）
/// 2. 容量限制：叶页 2..=4 项、内部页 2..=4 孩子，根页豁免下限
/// 3. 内部页分隔项非递减（重复键可相等），且每个分隔项恰好等于右孩子
///    子树的最小索引项（其 `key` 即右孩子最小键）
/// 4. 子树区间与分隔项一致（左子树最大项 < 分隔项 <= 右子树最小项）
/// 5. 叶页内有序、叶链双向指针闭合、叶链覆盖全部叶页
/// 6. 叶链拼接结果全局有序且条数等于 `tree.len()`
fn check_invariants(tree: &BPlusTree) -> Vec<Entry> {
    let root = tree.root_id();

    // 1. 可达性 DFS，同时做容量/分隔项检查。
    let mut reachable: HashSet<PageId> = HashSet::new();
    let mut leaf_ids_dfs: Vec<PageId> = Vec::new();
    fn visit(
        tree: &BPlusTree,
        id: PageId,
        root: PageId,
        reachable: &mut HashSet<PageId>,
        leaf_ids_dfs: &mut Vec<PageId>,
    ) {
        assert!(reachable.insert(id), "page {id} visited twice");
        match tree.get_page(id).unwrap() {
            Page::Leaf(leaf) => {
                assert!(leaf.entries.len() <= MAX_LEAF_ENTRIES);
                if id != root {
                    assert!(
                        leaf.entries.len() >= MIN_LEAF_ENTRIES,
                        "non-root leaf {id} underflow with {} entries",
                        leaf.entries.len()
                    );
                }
                leaf.entries.windows(2).for_each(|w| assert!(w[0] < w[1]));
                leaf_ids_dfs.push(id);
            }
            Page::Internal(internal) => {
                assert_eq!(internal.separators.len() + 1, internal.children.len());
                if id == root {
                    assert!(internal.children.len() >= 1);
                } else {
                    assert!(internal.children.len() >= MIN_CHILDREN);
                }
                assert!(internal.children.len() <= MAX_CHILDREN);
                // 重复键跨页时相邻分隔项允许相等，因此是非递减而非严格递增。
                internal.separators.windows(2).for_each(|w| assert!(w[0] <= w[1]));
                for (idx, separator) in internal.separators.iter().enumerate() {
                    let right_min = subtree_min_entry(tree, internal.children[idx + 1]);
                    assert_eq!(
                        *separator, right_min,
                        "separator must equal right subtree minimum entry"
                    );
                    let left_max = subtree_max_entry(tree, internal.children[idx]);
                    assert!(
                        left_max < *separator,
                        "left subtree must stay below separator"
                    );
                }
                for child in &internal.children {
                    visit(tree, *child, root, reachable, leaf_ids_dfs);
                }
            }
        }
    }
    visit(tree, root, root, &mut reachable, &mut leaf_ids_dfs);

    let existing: HashSet<PageId> = tree.pages().map(|(id, _)| id).collect();
    assert_eq!(reachable, existing, "orphan or dangling pages detected");

    // 5/6. 叶链检查。
    let chain = tree.leaf_chain();
    assert!(!chain.is_empty());
    assert_eq!(chain.first().copied(), Some(tree.leaf_head()));
    assert_eq!(chain, leaf_ids_dfs, "leaf chain order must match DFS order");

    let head = chain[0];
    assert_eq!(tree.get_page(head).unwrap().as_leaf().unwrap().prev, None);
    let tail = *chain.last().unwrap();
    assert_eq!(tree.get_page(tail).unwrap().as_leaf().unwrap().next, None);

    for window in chain.windows(2) {
        let left = tree.get_page(window[0]).unwrap().as_leaf().unwrap();
        let right = tree.get_page(window[1]).unwrap().as_leaf().unwrap();
        assert_eq!(left.next, Some(window[1]));
        assert_eq!(right.prev, Some(window[0]));
    }

    let mut all: Vec<Entry> = Vec::new();
    for id in &chain {
        let leaf = tree.get_page(*id).unwrap().as_leaf().unwrap();
        all.extend_from_slice(&leaf.entries);
    }
    all.windows(2).for_each(|w| assert!(w[0] < w[1]));
    assert_eq!(all.len(), tree.len());
    all
}

/// 仅沿叶链收集某个键的行号。
fn find_via_leaf_chain(tree: &BPlusTree, key: i64) -> Vec<u64> {
    let mut rows = Vec::new();
    let mut id = Some(tree.leaf_head());
    while let Some(current) = id {
        let leaf = tree.get_page(current).unwrap().as_leaf().unwrap();
        for entry in &leaf.entries {
            if entry.key == key {
                rows.push(entry.row_id);
            }
        }
        id = leaf.next;
    }
    rows
}

// ---------------------------------------------------------------------
// 正常场景
// ---------------------------------------------------------------------

#[test]
fn ordered_insert_and_range_scan() {
    let mut tree = BPlusTree::new();
    let mut expected: Vec<Entry> = Vec::new();
    for key in (0..20).rev() {
        let entry = Entry::new(key, 100 + key as u64);
        assert!(tree.insert(entry));
        expected.push(entry);
    }
    expected.sort();

    let all = check_invariants(&tree);
    assert_eq!(all, expected);
    assert!(tree.height() >= 2, "20 entries must force splits");

    // 左闭右开：含 5，不含 10。
    let middle = tree.scan(5, 10);
    assert_eq!(
        middle,
        expected
            .iter()
            .copied()
            .filter(|e| (5..10).contains(&e.key))
            .collect::<Vec<_>>()
    );

    // 反向扫描与正向扫描互为逆序。
    let middle_rev = tree.scan_rev(5, 10);
    let mut middle_reversed = middle.clone();
    middle_reversed.reverse();
    assert_eq!(middle_rev, middle_reversed);
}

#[test]
fn duplicate_keys_span_pages_and_partial_delete_stays_consistent() {
    let mut tree = BPlusTree::new();

    // 同一个键 10 条记录，叶页最多 4 项，必然跨多个叶页；
    // 再穿插其他键（两侧偏移保证不会再落到键 7 上），让同键记录与其他记录共享叶页。
    for row in 0..10u64 {
        tree.insert(Entry::new(7, row));
        tree.insert(Entry::new(8 + row as i64, 1000 + row));
        tree.insert(Entry::new(6 - row as i64, 2000 + row));
    }
    check_invariants(&tree);
    assert!(tree.height() >= 2);

    let chain = tree.leaf_chain();
    let occupied_by_key = chain
        .iter()
        .filter(|id| {
            tree.get_page(**id)
                .unwrap()
                .as_leaf()
                .unwrap()
                .entries
                .iter()
                .any(|e| e.key == 7)
        })
        .count();
    assert!(occupied_by_key >= 3, "10 duplicate rows must span >=3 leaves");

    // 删除前：树定位与叶链扫描结果一致。
    assert_eq!(tree.find(7), (0..10u64).collect::<Vec<_>>());
    assert_eq!(tree.find(7), find_via_leaf_chain(&tree, 7));

    // 连续删掉该重复键的一部分（偶数行号），保留奇数行号。
    for row in (0..10u64).step_by(2) {
        assert!(tree.delete(Entry::new(7, row)));
    }
    check_invariants(&tree);

    // 删除后仍要求两条访问路径找到同一批行号。
    let remaining: Vec<u64> = vec![1, 3, 5, 7, 9];
    assert_eq!(tree.find(7), remaining);
    assert_eq!(tree.find(7), find_via_leaf_chain(&tree, 7));
    for row in 0..10u64 {
        assert_eq!(
            tree.contains(Entry::new(7, row)),
            remaining.contains(&row),
        );
    }

    // 两端叶子的前后指针对得上。
    let chain = tree.leaf_chain();
    assert_eq!(
        tree.get_page(chain[0]).unwrap().as_leaf().unwrap().prev,
        None
    );
    assert_eq!(
        tree.get_page(*chain.last().unwrap())
            .unwrap()
            .as_leaf()
            .unwrap()
            .next,
        None
    );
    for window in chain.windows(2) {
        let left = tree.get_page(window[0]).unwrap().as_leaf().unwrap();
        let right = tree.get_page(window[1]).unwrap().as_leaf().unwrap();
        assert_eq!(left.next, Some(window[1]));
        assert_eq!(right.prev, Some(window[0]));
    }

    // 再删到只剩一条同键记录，仍然一致。
    for row in [1u64, 3, 5, 7] {
        assert!(tree.delete(Entry::new(7, row)));
    }
    check_invariants(&tree);
    assert_eq!(tree.find(7), vec![9]);
    assert_eq!(tree.find(7), find_via_leaf_chain(&tree, 7));

    // 已删除的二元组重复删除是无效操作。
    assert!(!tree.delete(Entry::new(7, 0)));
    assert_eq!(tree.find(7), vec![9]);
}

// ---------------------------------------------------------------------
// 边界场景
// ---------------------------------------------------------------------

#[test]
fn empty_and_degenerate_ranges() {
    let mut tree = BPlusTree::new();
    // 空树扫描。
    assert!(tree.scan(-100, 100).is_empty());
    assert!(tree.scan_rev(-100, 100).is_empty());
    // 空树删除无效。
    assert!(!tree.delete(Entry::new(1, 1)));

    tree.insert(Entry::new(5, 0));
    tree.insert(Entry::new(5, 1));

    // 起点 == 终点：空区间。
    assert!(tree.scan(5, 5).is_empty());
    assert!(tree.scan_rev(5, 5).is_empty());
    // 起点 > 终点：空区间。
    assert!(tree.scan(10, 5).is_empty());
    assert!(tree.scan_rev(10, 5).is_empty());
    // 不相交区间。
    assert!(tree.scan(6, 100).is_empty());
    assert!(tree.scan_rev(i64::MIN, 5).is_empty());
    // 极端键边界。
    assert_eq!(
        tree.scan(i64::MIN, i64::MAX),
        vec![Entry::new(5, 0), Entry::new(5, 1)]
    );
    assert_eq!(
        tree.scan_rev(i64::MIN, i64::MAX),
        vec![Entry::new(5, 1), Entry::new(5, 0)]
    );
}

#[test]
fn root_may_stay_below_minimum() {
    let mut tree = BPlusTree::new();
    tree.insert(Entry::new(1, 0));
    assert_eq!(tree.height(), 1);
    let page = tree.get_page(tree.root_id()).unwrap();
    assert_eq!(page.as_leaf().unwrap().entries.len(), 1);
    check_invariants(&tree);

    // 根叶页删空后仍然是合法根（豁免下限）。
    assert!(tree.delete(Entry::new(1, 0)));
    assert_eq!(tree.height(), 1);
    assert!(tree.is_empty());
    check_invariants(&tree);

    // 空树可继续使用。
    tree.insert(Entry::new(2, 0));
    assert!(tree.contains(Entry::new(2, 0)));
    check_invariants(&tree);
}

#[test]
fn merges_shrink_height_back_to_one() {
    let mut tree = BPlusTree::new();
    for key in 0..40i64 {
        tree.insert(Entry::new(key, 0));
        tree.insert(Entry::new(key, 1));
    }
    let max_height = tree.height();
    assert!(max_height >= 2);
    check_invariants(&tree);

    // 交错顺序删除，迫使借位与合并不断向上传播。
    let mut keys: Vec<i64> = (0..40).collect();
    // 用固定洗牌打乱删除顺序。
    let mut rng = Rng::new(0xCAFE);
    for i in (1..keys.len()).rev() {
        let j = rng.below(i + 1);
        keys.swap(i, j);
    }
    for key in keys {
        assert!(tree.delete(Entry::new(key, 0)));
        assert!(tree.delete(Entry::new(key, 1)));
    }

    assert!(tree.is_empty());
    assert_eq!(tree.height(), 1);
    assert_eq!(tree.leaf_chain(), vec![tree.root_id()]);
    check_invariants(&tree);
    assert_eq!(tree.page_count(), 1, "all merged pages must be reclaimed");
}

#[test]
fn split_capacity_limits_are_exact() {
    let mut tree = BPlusTree::new();
    // 单调插入直到出现内部页。
    let mut key = 0i64;
    while tree.height() < 2 {
        tree.insert(Entry::new(key, 0));
        key += 1;
    }
    check_invariants(&tree);
    for (id, page) in tree.pages() {
        match page {
            Page::Leaf(leaf) => {
                assert!(leaf.entries.len() <= MAX_LEAF_ENTRIES, "leaf {id} overflow");
                if id != tree.root_id() {
                    assert!(leaf.entries.len() >= MIN_LEAF_ENTRIES);
                }
            }
            Page::Internal(internal) => {
                assert!(internal.children.len() <= MAX_CHILDREN, "node {id} overflow");
                if id != tree.root_id() {
                    assert!(internal.children.len() >= MIN_CHILDREN);
                }
            }
        }
    }
}

// ---------------------------------------------------------------------
// 交错场景：随机操作 + 参考模型对照
// ---------------------------------------------------------------------

fn model_scan(model: &BTreeSet<Entry>, start: i64, end: i64) -> Vec<Entry> {
    model
        .iter()
        .copied()
        .filter(|e| e.key >= start && e.key < end)
        .collect()
}

#[test]
fn interleaved_small_key_space_with_duplicates() {
    // 键空间小 -> 大量重复键与跨页同键记录，重点压分裂/借位/合并。
    let mut tree = BPlusTree::new();
    let mut model: BTreeSet<Entry> = BTreeSet::new();
    let mut rng = Rng::new(20261122);
    let keys = 12i64;
    let rows = 6u64;

    for _ in 0..30_000 {
        let op = rng.below(3);
        let key = rng.below(keys as usize) as i64;
        let row = rng.below(rows as usize) as u64;
        let entry = Entry::new(key, row);

        match op {
            0 => assert_eq!(tree.insert(entry), model.insert(entry)),
            1 => assert_eq!(tree.delete(entry), model.remove(&entry)),
            _ => {
                let start = rng.below((keys + 2) as usize) as i64 - 1;
                let end = rng.below((keys + 2) as usize) as i64 - 1;
                let fwd = tree.scan(start, end);
                assert_eq!(fwd, model_scan(&model, start, end));
                let mut expect_rev = fwd.clone();
                expect_rev.reverse();
                assert_eq!(tree.scan_rev(start, end), expect_rev);
            }
        }

        assert_eq!(tree.len(), model.len());
        check_invariants(&tree);
    }

    // 终局全量比对：每个键的行号集合、正反向全区间扫描。
    let all = check_invariants(&tree);
    let expected_all: Vec<Entry> = model.iter().copied().collect();
    assert_eq!(all, expected_all);
    for key in 0..keys {
        let got: BTreeSet<u64> = tree.find(key).into_iter().collect();
        let want: BTreeSet<u64> = model
            .iter()
            .filter(|e| e.key == key)
            .map(|e| e.row_id)
            .collect();
        assert_eq!(got, want);
        assert_eq!(got.into_iter().collect::<Vec<_>>(), find_via_leaf_chain(&tree, key));
    }
    assert_eq!(tree.scan(i64::MIN, i64::MAX), expected_all);
}

#[test]
fn interleaved_delete_heavy_large_space() {
    // 键空间大、删除占比高 -> 反复合并并最终降高。
    let mut tree = BPlusTree::new();
    let mut model: BTreeSet<Entry> = BTreeSet::new();
    let mut rng = Rng::new(0x5EED_1234);
    let keys = 200i64;
    let rows = 3u64;

    for _ in 0..40_000 {
        let key = rng.below(keys as usize) as i64;
        let row = rng.below(rows as usize) as u64;
        let entry = Entry::new(key, row);
        let prefer_delete = rng.below(2) == 0 && !model.is_empty();

        if prefer_delete {
            assert_eq!(tree.delete(entry), model.remove(&entry));
        } else {
            assert_eq!(tree.insert(entry), model.insert(entry));
        }
        assert_eq!(tree.len(), model.len());
        check_invariants(&tree);
    }
    assert!(tree.height() >= 1);
    check_invariants(&tree);

    // 清空整棵树，验证高树一路合并回单叶。
    let remaining: Vec<Entry> = model.iter().copied().collect();
    for entry in remaining {
        assert!(tree.delete(entry));
    }
    assert!(tree.is_empty());
    assert_eq!(tree.height(), 1);
    assert_eq!(tree.page_count(), 1);
    check_invariants(&tree);
    assert!(tree.scan(i64::MIN, i64::MAX).is_empty());
    assert!(tree.scan_rev(i64::MIN, i64::MAX).is_empty());

    // 清空后重新插入仍然工作。
    tree.insert(Entry::new(-5, 7));
    assert_eq!(tree.find(-5), vec![7]);
    check_invariants(&tree);
}

#[test]
fn separator_tracks_right_subtree_minimum_after_deletes() {
    // 专门构造“删除子树最小项”的场景，验证分隔键随之修正。
    let mut tree = BPlusTree::new();
    for key in 0..30i64 {
        tree.insert(Entry::new(key, 0));
        tree.insert(Entry::new(key, 1));
    }
    check_invariants(&tree);

    // 从最小键开始连续删除，每次都可能让某个右孩子的最小项发生变化。
    for key in 0..20i64 {
        tree.delete(Entry::new(key, 0));
        tree.delete(Entry::new(key, 1));
        check_invariants(&tree);
        // 分隔键正确的直接外在表现：剩余数据仍可被区间扫描完整命中。
        let got = tree.scan(key + 1, i64::MAX);
        assert!(got.iter().all(|e| e.key > key));
        assert_eq!(got.len(), tree.len());
    }
}

#[test]
fn extreme_key_boundaries_and_row_ids() {
    let mut tree = BPlusTree::new();
    let extremes = [
        Entry::new(i64::MIN, u64::MIN),
        Entry::new(i64::MIN, u64::MAX),
        Entry::new(i64::MAX, u64::MIN),
        Entry::new(i64::MAX, u64::MAX),
        Entry::new(0, u64::MAX),
    ];
    for entry in extremes {
        tree.insert(entry);
    }
    // 再插入大量同键记录把极端键挤到不同叶页。
    for row in 1..12u64 {
        tree.insert(Entry::new(i64::MAX, row));
    }
    check_invariants(&tree);

    for entry in extremes {
        assert!(tree.contains(entry), "extreme entry {entry:?} must exist");
    }
    let min_rows = tree.find(i64::MIN);
    assert_eq!(min_rows, vec![u64::MIN, u64::MAX]);
    assert_eq!(min_rows, find_via_leaf_chain(&tree, i64::MIN));
    let max_rows = tree.find(i64::MAX);
    assert_eq!(max_rows.len(), 13);
    assert_eq!(max_rows[0], u64::MIN);
    assert_eq!(*max_rows.last().unwrap(), u64::MAX);
    assert_eq!(max_rows, find_via_leaf_chain(&tree, i64::MAX));

    // 包含两端的闭包式扫描（上界取 i64::MAX 时再用单独全量校验补齐）。
    let all = tree.scan(i64::MIN, i64::MAX);
    assert_eq!(all.first(), Some(&Entry::new(i64::MIN, u64::MIN)));
    assert!(all.iter().all(|e| e.key < i64::MAX));
    let full_chain = check_invariants(&tree);
    assert_eq!(full_chain.first(), Some(&Entry::new(i64::MIN, u64::MIN)));
    assert_eq!(full_chain.last(), Some(&Entry::new(i64::MAX, u64::MAX)));

    // 反向扫描同样能覆盖到 i64::MIN 的项。
    let rev = tree.scan_rev(i64::MIN, 0);
    assert_eq!(
        rev,
        vec![Entry::new(i64::MIN, u64::MAX), Entry::new(i64::MIN, u64::MIN)]
    );
}

#[test]
fn underflow_borrows_from_sibling_before_merging() {
    // 场景一：欠载叶页有一个 4 项的富余兄弟（这里落在右侧，走“向右借”路径），
    // 删除一项后必须借位，表现为页数/树高完全不变，且欠载叶恢复到下限。
    let mut tree = BPlusTree::new();
    for key in 0..12i64 {
        tree.insert(Entry::new(key, 0));
        tree.insert(Entry::new(key, 1));
    }
    check_invariants(&tree);
    assert!(tree.height() >= 2);

    let chain = tree.leaf_chain();
    let mut borrowed_case = None;
    for window in chain.windows(2) {
        let left = tree.get_page(window[0]).unwrap().as_leaf().unwrap();
        let right = tree.get_page(window[1]).unwrap().as_leaf().unwrap();
        // 左叶 2 项（删一项即欠载），右叶 4 项（借出后仍有 3 项）。
        if left.entries.len() == MIN_LEAF_ENTRIES
            && right.entries.len() == MAX_LEAF_ENTRIES
        {
            borrowed_case = Some((window[0], window[1], left.entries[0]));
            break;
        }
    }
    let (left_id, right_id, victim) =
        borrowed_case.expect("deterministic layout has a (2,4) adjacent pair");
    let pages_before = tree.page_count();
    assert!(tree.delete(victim));
    check_invariants(&tree);
    assert_eq!(
        tree.page_count(),
        pages_before,
        "borrowing from a rich sibling must not merge or reclaim pages"
    );
    assert!(!tree.contains(victim));
    // 借位后欠载的左叶恢复到下限，富余的右叶借出一项（4 -> 3）。
    let left_leaf = tree.get_page(left_id).unwrap().as_leaf().unwrap();
    assert_eq!(left_leaf.entries.len(), MIN_LEAF_ENTRIES);
    let right_leaf = tree.get_page(right_id).unwrap().as_leaf().unwrap();
    assert_eq!(right_leaf.entries.len(), MAX_LEAF_ENTRIES - 1);

    // 场景二：相邻兄弟都恰好只有下限（2 项），删除后无人可借，只能合并。
    let mut tree = BPlusTree::new();
    // 8 项分裂后的叶链为 [2, 2, 4]，先从 4 项叶移走两项形成三个各 2 项的叶页。
    for key in 0..8i64 {
        tree.insert(Entry::new(key, 0));
    }
    check_invariants(&tree);
    let chain = tree.leaf_chain();
    assert_eq!(chain.len(), 3);
    let rich_victims: Vec<Entry> = tree.get_page(chain[2])
        .unwrap()
        .as_leaf()
        .unwrap()
        .entries[..2]
        .to_vec();
    for entry in rich_victims {
        assert!(tree.delete(entry));
    }
    check_invariants(&tree);
    assert_eq!(tree.page_count(), 4, "three 2-entry leaves + internal root");
    for id in tree.leaf_chain() {
        assert_eq!(
            tree.get_page(id).unwrap().as_leaf().unwrap().entries.len(),
            MIN_LEAF_ENTRIES
        );
    }
    let pages_before_merge = tree.page_count();

    // 再删最左叶一项：它降到 1 项欠载，右兄弟只有 2 项无法借出 -> 合并，
    // 页数恰好减少 1（被合并的叶页回收）。
    let final_victim = tree.get_page(chain[0])
        .unwrap()
        .as_leaf()
        .unwrap()
        .entries[0];
    assert!(tree.delete(final_victim));
    check_invariants(&tree);
    assert_eq!(
        tree.page_count(),
        pages_before_merge - 1,
        "when no sibling can lend, the pages must merge"
    );
    assert!(!tree.contains(final_victim));

    // 继续清空：合并一路传播，最终只剩一个根叶页。
    let entries = check_invariants(&tree);
    for entry in entries {
        assert!(tree.delete(entry));
    }
    check_invariants(&tree);
    assert_eq!(tree.page_count(), 1);
    assert_eq!(tree.height(), 1);
    assert!(tree.is_empty());
}

#[test]
fn internal_node_borrow_during_cascading_delete() {
    // 构造三层树，再从一侧连续删除，迫使内部页之间也发生借位/合并传播。
    let mut tree = BPlusTree::new();
    for key in 0..80i64 {
        tree.insert(Entry::new(key, 0));
        tree.insert(Entry::new(key, 1));
    }
    check_invariants(&tree);
    assert!(tree.height() >= 3);

    let mut model: BTreeSet<Entry> = BTreeSet::new();
    for key in 0..80i64 {
        model.insert(Entry::new(key, 0));
        model.insert(Entry::new(key, 1));
    }

    // 升序删除左侧数据，让欠载从最左子树开始逐级向上传播。
    for key in 0..60i64 {
        assert!(tree.delete(Entry::new(key, 0)));
        assert!(tree.delete(Entry::new(key, 1)));
        model.remove(&Entry::new(key, 0));
        model.remove(&Entry::new(key, 1));
        check_invariants(&tree);
    }

    let expected: Vec<Entry> = model.iter().copied().collect();
    assert_eq!(check_invariants(&tree), expected);
    assert_eq!(
        tree.scan(i64::MIN, i64::MAX),
        expected,
        "remaining entries stay reachable via tree descent"
    );
    let mut expected_rev = expected.clone();
    expected_rev.reverse();
    assert_eq!(tree.scan_rev(i64::MIN, i64::MAX), expected_rev);
}

#[test]
fn interleaved_multiple_seeds_against_reference_model() {
    // 多种子、不同键/行号空间的交错操作，每个操作后都全量校验结构不变量，
    // 并与 BTreeSet 参考模型逐项对照插入/删除/双向扫描结果。
    for seed in [1u64, 42, 99, 1_000_003, 0xDEAD_BEEF] {
        let mut rng = Rng::new(seed);
        let mut tree = BPlusTree::new();
        let mut model: BTreeSet<Entry> = BTreeSet::new();
        let keys = 6 + seed as i64 % 25;
        let rows = 2 + (seed as usize % 5) as u64;

        for _ in 0..8_000 {
            let key = rng.below(keys as usize) as i64;
            let row = rng.below(rows as usize) as u64;
            let entry = Entry::new(key, row);
            let op = rng.below(4);

            match op {
                0 | 1 => assert_eq!(tree.insert(entry), model.insert(entry), "seed {seed}"),
                2 => assert_eq!(tree.delete(entry), model.remove(&entry), "seed {seed}"),
                _ => {
                    let start = rng.below((keys + 2) as usize) as i64 - 1;
                    let end = rng.below((keys + 2) as usize) as i64 - 1;
                    let fwd = tree.scan(start, end);
                    assert_eq!(fwd, model_scan(&model, start, end), "seed {seed}");
                    let mut expect_rev = fwd;
                    expect_rev.reverse();
                    assert_eq!(tree.scan_rev(start, end), expect_rev, "seed {seed}");
                }
            }
            assert_eq!(tree.len(), model.len(), "seed {seed}");
            check_invariants(&tree);
        }

        // 终局：全部键的树定位结果与叶链扫描结果一致。
        for key in 0..keys {
            let via_tree: BTreeSet<u64> = tree.find(key).into_iter().collect();
            let via_chain: BTreeSet<u64> = find_via_leaf_chain(&tree, key).into_iter().collect();
            assert_eq!(via_tree, via_chain, "seed {seed} key {key}");
        }
        assert_eq!(
            check_invariants(&tree),
            model.iter().copied().collect::<Vec<_>>(),
            "seed {seed}"
        );
    }
}
