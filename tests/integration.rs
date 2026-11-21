//! 保留内存索引的集成测试：正常场景、边界、交错（共享子图/环/弱引用）、
//! 错误输入与大规模合成图。所有数值均由真实构建的索引计算，不是预置输出。

use retained_index::{
    BuildError, BuildStats, Dominator, Edge, Reachability, RetainedIndex,
};

fn index_with<'a>(
    sizes: &'a [(&'a str, u64)],
    roots: &'a [&'a str],
    edges: &'a [Edge<&'a str>],
) -> RetainedIndex<&'a str> {
    RetainedIndex::build(sizes.iter().copied(), roots.iter().copied(), edges.iter().cloned())
        .expect("构建索引应当成功")
}

#[test]
fn linear_chain_retains_everything_below() {
    // root -> a -> b -> c，各自 10 字节。
    let sizes = [("root", 10), ("a", 10), ("b", 10), ("c", 10)];
    let roots = ["root"];
    let edges = [
        Edge::strong("root", "a"),
        Edge::strong("a", "b"),
        Edge::strong("b", "c"),
    ];
    let idx = index_with(&sizes, &roots, &edges);

    assert_eq!(idx.len(), 4);
    assert_eq!(idx.stats().reachable_objects, 4);
    assert_eq!(idx.stats().total_reachable_bytes, 40);

    assert_eq!(idx.retention(&"root").unwrap().retained_bytes, 40);
    assert_eq!(idx.retention(&"a").unwrap().retained_bytes, 30);
    assert_eq!(idx.retention(&"b").unwrap().retained_bytes, 20);
    assert_eq!(idx.retention(&"c").unwrap().retained_bytes, 10);

    // 支配者沿链逐级排列，根的支配者是虚拟根。
    assert_eq!(idx.dominator_of(&"root"), Some(Some(Dominator::VirtualRoot)));
    assert_eq!(idx.dominator_of(&"a"), Some(Some(Dominator::Object(1))));
    assert_eq!(idx.dominator_of(&"b"), Some(Some(Dominator::Object(2))));
    assert_eq!(idx.dominator_of(&"c"), Some(Some(Dominator::Object(3))));
    for id in ["root", "a", "b", "c"] {
        assert_eq!(idx.reachability_of(&id), Some(Reachability::Reachable));
    }
}

#[test]
fn diamond_shared_leaf_is_retained_only_by_itself() {
    // 根 r 分出 a、b，a、b 共同指向 leaf。
    let sizes = [("r", 8), ("a", 8), ("b", 8), ("leaf", 100)];
    let roots = ["r"];
    let edges = [
        Edge::strong("r", "a"),
        Edge::strong("r", "b"),
        Edge::strong("a", "leaf"),
        Edge::strong("b", "leaf"),
    ];
    let idx = index_with(&sizes, &roots, &edges);

    // r 保留全部；a、b 互不支配对方也不支配 leaf。
    assert_eq!(idx.retention(&"r").unwrap().retained_bytes, 124);
    assert_eq!(idx.retention(&"a").unwrap().retained_bytes, 8);
    assert_eq!(idx.retention(&"b").unwrap().retained_bytes, 8);
    assert_eq!(idx.retention(&"leaf").unwrap().retained_bytes, 100);

    // leaf 的唯一支配者是 r（两条路径的最近公共支配者）。
    assert_eq!(idx.dominator_of(&"leaf"), Some(Some(Dominator::Object(1))));
    assert_eq!(idx.dominator_of(&"a"), Some(Some(Dominator::Object(1))));
    assert_eq!(idx.dominator_of(&"b"), Some(Some(Dominator::Object(1))));
}

#[test]
fn two_root_paths_sharing_large_buffer_headline_case() {
    // 值班场景：两条根路径各有一个包装对象，共同持有一块大缓冲。
    let sizes = [
        ("wrapper_a", 16),
        ("wrapper_b", 16),
        ("buffer", 4096),
        ("pad_a", 4),
    ];
    let roots = ["wrapper_a", "wrapper_b"];
    let edges = [
        Edge::strong("wrapper_a", "buffer"),
        Edge::strong("wrapper_b", "buffer"),
        Edge::strong("wrapper_a", "pad_a"),
    ];
    let idx = index_with(&sizes, &roots, &edges);

    // 任一包装对象被移除，buffer 仍经另一条路径可达，不能把整块缓冲算进来。
    assert_eq!(idx.retention(&"wrapper_a").unwrap().retained_bytes, 16 + 4);
    assert_eq!(idx.retention(&"wrapper_b").unwrap().retained_bytes, 16);
    // 缓冲自己的保留字节只含自身；其支配者是虚拟根（经两条根路径汇合）。
    let buf = idx.retention(&"buffer").unwrap();
    assert_eq!(buf.retained_bytes, 4096);
    assert_eq!(buf.dominator, Some(Dominator::VirtualRoot));
    // 全堆总量仍然把共享缓冲只计一次。
    assert_eq!(idx.stats().total_reachable_bytes, 16 + 16 + 4096 + 4);
    assert_eq!(idx.stats().distinct_roots, 2);
    // 含两条虚拟根合成强边 + 三条对象强边。
    assert_eq!(idx.stats().strong_edges, 5);
}

#[test]
fn strongly_connected_cycle_is_handled() {
    // x <-> y 成环，x -> z；根直接持有 x。
    let sizes = [("x", 10), ("y", 20), ("z", 30)];
    let roots = ["x"];
    let edges = [
        Edge::strong("x", "y"),
        Edge::strong("y", "x"),
        Edge::strong("x", "z"),
    ];
    let idx = index_with(&sizes, &roots, &edges);

    assert_eq!(idx.stats().reachable_objects, 3);
    // 移除 x 的强入边（根边 + y->x）后，整个环连同 z 都失去可达性。
    assert_eq!(idx.retention(&"x").unwrap().retained_bytes, 60);
    // y 由 x 支配；断开 y->x 不会让 x 不可达，y 只保留自身。
    assert_eq!(idx.retention(&"y").unwrap().retained_bytes, 20);
    assert_eq!(idx.retention(&"z").unwrap().retained_bytes, 30);
    assert_eq!(idx.dominator_of(&"y"), Some(Some(Dominator::Object(1))));
    assert_eq!(idx.dominator_of(&"z"), Some(Some(Dominator::Object(1))));
}

#[test]
fn weak_references_do_not_confer_reachability() {
    // cache 弱引用 big；big 没有任何强入边，构建时即不可达。
    let sizes = [("cache", 64), ("big", 1024), ("entry", 8)];
    let roots = ["cache"];
    let edges = [
        Edge::weak("cache", "big"),
        Edge::strong("cache", "entry"),
    ];
    let idx = index_with(&sizes, &roots, &edges);

    assert_eq!(idx.stats().weak_edges, 1);
    assert_eq!(idx.reachability_of(&"big"), Some(Reachability::Unreachable));
    let big = idx.retention(&"big").unwrap();
    assert_eq!(big.reachability, Reachability::Unreachable);
    assert_eq!(big.retained_bytes, 0);
    assert_eq!(big.own_bytes, 1024); // 自身大小仍然可查
    assert_eq!(big.dominator, None);
    // 可达部分不受弱引用影响。
    assert_eq!(idx.retention(&"cache").unwrap().retained_bytes, 64 + 8);
    assert_eq!(idx.stats().total_reachable_bytes, 72);
    assert_eq!(idx.stats().unreachable_objects, 1);
}

#[test]
fn multiple_roots_and_unreachable_subgraph() {
    let sizes = [
        ("r1", 1),
        ("r2", 1),
        ("shared", 10),
        ("orphan", 7),
        ("orphan_child", 3),
    ];
    let roots = ["r1", "r2"];
    let edges = [
        Edge::strong("r1", "shared"),
        Edge::strong("r2", "shared"),
        // 孤立分量：orphan 不是根，只能到达 orphan_child，整体不可达。
        Edge::strong("orphan", "orphan_child"),
    ];
    let idx = index_with(&sizes, &roots, &edges);

    assert_eq!(idx.stats().reachable_objects, 3);
    assert_eq!(idx.stats().unreachable_objects, 2);
    for id in ["orphan", "orphan_child"] {
        let r = idx.retention(&id).unwrap();
        assert_eq!(r.reachability, Reachability::Unreachable);
        assert_eq!(r.retained_bytes, 0);
        assert_eq!(r.dominator, None);
    }
    // shared 被两个根共享，支配者是虚拟根，保留字节只含自身。
    assert_eq!(idx.retention(&"shared").unwrap().dominator, Some(Dominator::VirtualRoot));
    assert_eq!(idx.retention(&"shared").unwrap().retained_bytes, 10);
    assert_eq!(idx.retention(&"r1").unwrap().retained_bytes, 1);
    assert_eq!(idx.retention(&"r2").unwrap().retained_bytes, 1);
}

#[test]
fn single_root_with_self_loop() {
    let sizes = [("r", 5), ("o", 9)];
    let roots = ["r"];
    let edges = [Edge::strong("r", "r"), Edge::strong("r", "o")];
    let idx = index_with(&sizes, &roots, &edges);

    assert_eq!(idx.retention(&"r").unwrap().retained_bytes, 14);
    assert_eq!(idx.retention(&"o").unwrap().retained_bytes, 9);
    assert_eq!(idx.dominator_of(&"o"), Some(Some(Dominator::Object(1))));
}

#[test]
fn no_reachable_objects_when_roots_are_empty() {
    let sizes = [("a", 1), ("b", 2)];
    let edges = [Edge::strong("a", "b")];
    let idx = index_with(&sizes, &[], &edges);

    assert_eq!(idx.stats().reachable_objects, 0);
    assert_eq!(idx.stats().total_reachable_bytes, 0);
    for id in ["a", "b"] {
        assert_eq!(idx.retention(&id).unwrap().retained_bytes, 0);
        assert_eq!(idx.retention(&id).unwrap().dominator, None);
    }
}

#[test]
fn empty_index_is_supported() {
    let idx: RetainedIndex<&str> =
        RetainedIndex::build(std::iter::empty(), std::iter::empty(), std::iter::empty()).unwrap();
    assert!(idx.is_empty());
    assert_eq!(idx.len(), 0);
    assert_eq!(idx.retention(&"missing"), None);
    assert_eq!(idx.retention_at(0), None);
    assert_eq!(idx.retention_at(1), None);
    assert_eq!(idx.stats(), &BuildStats {
        total_objects: 0,
        reachable_objects: 0,
        unreachable_objects: 0,
        strong_edges: 0,
        weak_edges: 0,
        distinct_roots: 0,
        total_reachable_bytes: 0,
    });
    assert_eq!(idx.retention_iter().count(), 0);
}

#[test]
fn duplicate_edges_are_kept_and_harmless() {
    let sizes = [("r", 2), ("o", 3)];
    let roots = ["r"];
    let edges = [Edge::strong("r", "o"), Edge::strong("r", "o")];
    let idx = index_with(&sizes, &roots, &edges);
    // 两条强边都计入统计，但支配关系与保留字节不受影响。
    assert_eq!(idx.stats().strong_edges, 3); // 1 条虚拟根边 + 2 条重复边
    assert_eq!(idx.retention(&"r").unwrap().retained_bytes, 5);
    assert_eq!(idx.retention(&"o").unwrap().retained_bytes, 3);
}

#[test]
fn duplicate_roots_are_deduplicated() {
    let sizes = [("r", 2), ("o", 3)];
    let roots = ["r", "r", "r"];
    let edges = [Edge::strong("r", "o")];
    let idx = index_with(&sizes, &roots, &edges);
    assert_eq!(idx.stats().distinct_roots, 1);
    assert_eq!(idx.stats().strong_edges, 2); // 仅一条虚拟根合成边
    assert_eq!(idx.retention(&"r").unwrap().retained_bytes, 5);
}

#[test]
fn unknown_id_queries_return_none() {
    let sizes = [("r", 1)];
    let idx = index_with(&sizes, &["r"], &[]);
    assert_eq!(idx.retention(&"ghost"), None);
    assert_eq!(idx.reachability_of(&"ghost"), None);
    assert_eq!(idx.dominator_of(&"ghost"), None);
    assert_eq!(idx.node_of(&"ghost"), None);
}

#[test]
fn build_errors_are_reported_before_any_computation() {
    let dup = RetainedIndex::build(
        [("a", 1u64), ("a", 2u64)],
        ["a"],
        std::iter::empty::<Edge<&str>>(),
    );
    assert!(matches!(dup, Err(BuildError::DuplicateObject(_))));

    let bad_root = RetainedIndex::build(
        [("a", 1u64)],
        ["ghost"],
        std::iter::empty::<Edge<&str>>(),
    );
    assert!(matches!(bad_root, Err(BuildError::UnknownRoot(_))));

    let bad_target = RetainedIndex::build(
        [("a", 1u64)],
        ["a"],
        [Edge::strong("a", "ghost")],
    );
    assert!(matches!(bad_target, Err(BuildError::UnknownEndpoint(_))));

    let bad_source = RetainedIndex::build(
        [("a", 1u64)],
        ["a"],
        [Edge::weak("ghost", "a")],
    );
    assert!(matches!(bad_source, Err(BuildError::UnknownEndpoint(_))));
}

#[test]
fn integer_id_type_works_just_as_well() {
    // 泛型标识：u32 而非字符串，验证 ID 类型不绑定任何特定类型。
    let sizes = [(1u32, 100u64), (2u32, 50u64)];
    let roots = [1u32];
    let edges = [Edge::strong(1u32, 2u32)];
    let idx = RetainedIndex::build(sizes, roots, edges).unwrap();
    assert_eq!(idx.retention(&1u32).unwrap().retained_bytes, 150);
    assert_eq!(idx.retention(&2u32).unwrap().retained_bytes, 50);
    assert_eq!(idx.node_of(&2u32), Some(2));
}

#[test]
fn size_overflow_at_object_is_detected() {
    // 单个对象自身大小接近 u64 上限，再加一个子节点就溢出。
    let sizes = [("r", u64::MAX - 1), ("o", 2)];
    let roots = ["r"];
    let edges = [Edge::strong("r", "o")];
    let err = RetainedIndex::build(sizes, roots, edges).unwrap_err();
    // 溢出归因于 r 的子树汇总（内部节点 1）。
    assert_eq!(err, BuildError::SizeOverflow(1));
}

#[test]
fn size_overflow_at_virtual_root_is_detected() {
    // 两个根各持一半多，单独都不溢出，但全堆总量在虚拟根处溢出。
    let sizes = [("r1", u64::MAX / 2 + 1), ("r2", u64::MAX / 2 + 1)];
    let roots = ["r1", "r2"];
    let err = RetainedIndex::<&str>::build(sizes, roots, std::iter::empty()).unwrap_err();
    assert_eq!(err, BuildError::SizeOverflow(0));
}

#[test]
fn large_synthetic_graph_with_cycles_and_shared_subgraph() {
    // 20_000 个对象：
    //   0..10_000      主链（含回边成环：9999 -> 5000）
    //   10_000..19_999 大缓冲对象，每个都被主链上前 100 个节点共享持有
    // 另加 100 个孤立对象验证不可达统计。
    const CHAIN: usize = 10_000;
    const BUFFERS: usize = 10_000;
    const ORPHANS: usize = 100;
    let total = CHAIN + BUFFERS + ORPHANS;

    let sizes = (0..total).map(|i| {
        let bytes = if i >= CHAIN && i < CHAIN + BUFFERS { 4u64 } else { 1u64 };
        (i as u32, bytes)
    });
    let roots = [0u32];

    let mut edges: Vec<Edge<u32>> = Vec::new();
    for i in 0..CHAIN - 1 {
        edges.push(Edge::strong(i as u32, (i + 1) as u32));
    }
    // 回边制造环。
    edges.push(Edge::strong((CHAIN - 1) as u32, 5000u32));
    // 共享子图：前 100 个链节点都强引用全部缓冲对象（100 万条边）。
    for i in 0..100 {
        for b in CHAIN..CHAIN + BUFFERS {
            edges.push(Edge::strong(i as u32, b as u32));
        }
    }

    let idx = RetainedIndex::build(sizes, roots, edges).expect("大规模构建应成功");

    assert_eq!(idx.len(), total);
    assert_eq!(idx.stats().reachable_objects, CHAIN + BUFFERS);
    assert_eq!(idx.stats().unreachable_objects, ORPHANS);
    assert_eq!(
        idx.stats().total_reachable_bytes,
        (CHAIN as u64) + (BUFFERS as u64) * 4
    );

    // 孤立对象：不可达、保留 0、自身大小仍为 1。
    let orphan = (CHAIN + BUFFERS) as u32;
    let r = idx.retention(&orphan).unwrap();
    assert_eq!(r.reachability, Reachability::Unreachable);
    assert_eq!(r.retained_bytes, 0);
    assert_eq!(r.own_bytes, 1);

    // 链尾节点只保留自身（回边不改变支配关系，后继 5000 并不被它支配）。
    let tail = idx.retention(&((CHAIN - 1) as u32)).unwrap();
    assert_eq!(tail.retained_bytes, 1);

    // 前 100 个链节点都不能把共享缓冲算进自己的保留字节：
    // 它们的保留值只等于主链后缀（每节点 1 字节），不含任何 4 字节缓冲。
    for i in 1..100u32 {
        assert_eq!(
            idx.retention(&i).unwrap().retained_bytes,
            (CHAIN as u64) - (i as u64)
        );
    }
    // 缓冲对象的支配者是链根（内部节点 1）：100 条持有路径在链根之后才分叉。
    let first_buffer = idx.retention(&(CHAIN as u32)).unwrap();
    assert_eq!(first_buffer.retained_bytes, 4);
    assert_eq!(first_buffer.dominator, Some(Dominator::Object(1)));

    // 节点 100 不参与共享持有，其保留字节是主链后缀 100..10_000 的自身字节，
    // 共 9_900 个链节点（回边不引入额外对象）。
    assert_eq!(idx.retention(&100u32).unwrap().retained_bytes, (CHAIN - 100) as u64);

    // 根保留全部可达字节（共享缓冲只计一次）。
    assert_eq!(
        idx.retention(&0u32).unwrap().retained_bytes,
        (CHAIN as u64) + (BUFFERS as u64) * 4
    );

    // 按内部序号查询与按 ID 查询一致，且是纯数组读取。
    let by_node = idx.retention_at(1).unwrap();
    let by_id = idx.retention(&0u32).unwrap();
    assert_eq!(by_node, by_id);

    // 遍历接口覆盖全部对象，顺序与登记顺序一致。
    assert_eq!(idx.retention_iter().count(), total);
}
