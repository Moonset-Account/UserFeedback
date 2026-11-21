//! # 保留内存索引（Retained-Memory Index）
//!
//! 输入对象大小、若干 GC 根以及带强/弱标记的引用，基于**强可达子图**上的
//! 支配者树（dominator tree）构建索引，使每个对象可以在 O(1) 时间内查询：
//!
//! - 它当前是否强可达（原已不可达的对象可被辨认）；
//! - 它的直接支配者（谁“把守”着它唯一的强可达路径）；
//! - 移除它的全部强入边后，会随之变得不可达的对象总字节（保留字节），
//!   保留字节包含对象自身。
//!
//! 弱引用只做端点校验，不参与可达性与支配关系计算。所有遍历均为迭代实现，
//! 支持成千上万个对象的环与共享子图。
//!
//! ## 典型用法
//!
//! ```
//! use retained_index::{Edge, RetainedIndex};
//!
//! let sizes = [("buffer", 4096u64), ("wrapper_a", 16), ("wrapper_b", 16)];
//! let roots = ["wrapper_a", "wrapper_b"];
//! let edges = [
//!     Edge::strong("wrapper_a", "buffer"),
//!     Edge::strong("wrapper_b", "buffer"),
//! ];
//!
//! let index = RetainedIndex::build(sizes, roots, edges).unwrap();
//! // buffer 被两条根路径共享，任一包装对象都不保留它。
//! assert_eq!(index.retention(&"wrapper_a").unwrap().retained_bytes, 16);
//! assert_eq!(index.retention(&"wrapper_b").unwrap().retained_bytes, 16);
//! assert_eq!(index.retention(&"buffer").unwrap().retained_bytes, 4096);
//! ```

mod dominator;

use std::collections::hash_map::Entry;
use std::collections::HashMap;
use std::error::Error;
use std::fmt;
use std::hash::Hash;

/// 引用的强弱标记。
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum EdgeKind {
    /// 强引用：参与可达性与支配关系计算。
    Strong,
    /// 弱引用：不传递可达性，不进入支配者图（端点仍需是已知对象）。
    Weak,
}

/// 一条从 `source` 指向 `target` 的带标记有向引用。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Edge<I> {
    pub source: I,
    pub target: I,
    pub kind: EdgeKind,
}

impl<I> Edge<I> {
    /// 构造一条强引用。
    pub fn strong(source: I, target: I) -> Self {
        Edge { source, target, kind: EdgeKind::Strong }
    }

    /// 构造一条弱引用。
    pub fn weak(source: I, target: I) -> Self {
        Edge { source, target, kind: EdgeKind::Weak }
    }
}

/// 构建索引时的输入错误。所有校验在任何计算开始前完成。
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub enum BuildError {
    /// `sizes` 中出现了重复的对象标识，附带该标识的调试文本。
    DuplicateObject(String),
    /// 根集合中的对象没有在 `sizes` 中登记，附带该标识的调试文本。
    UnknownRoot(String),
    /// 引用的源或目标没有在 `sizes` 中登记，附带该标识的调试文本。
    UnknownEndpoint(String),
    /// 保留字节（u64）累加溢出，附带发生溢出的内部节点数。
    /// 节点 `0` 表示在汇总虚拟根（即全堆强可达总量）时溢出。
    SizeOverflow(usize),
}

impl fmt::Display for BuildError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            BuildError::DuplicateObject(id) => {
                write!(f, "sizes 中出现重复的对象标识: {id}")
            }
            BuildError::UnknownRoot(id) => {
                write!(f, "根对象未在 sizes 中登记: {id}")
            }
            BuildError::UnknownEndpoint(id) => {
                write!(f, "引用的端点未在 sizes 中登记: {id}")
            }
            BuildError::SizeOverflow(node) => {
                write!(f, "保留字节在节点 {node} 累加时溢出 u64")
            }
        }
    }
}

impl Error for BuildError {}

/// 对象的强可达状态。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Reachability {
    /// 从某个 GC 根（经强边）可达。
    Reachable,
    /// 构建索引时就已经不可达，弱引用不能使其变为可达。
    Unreachable,
}

impl Reachability {
    pub fn is_reachable(self) -> bool {
        matches!(self, Reachability::Reachable)
    }
}

/// 直接支配者的归属。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Dominator {
    /// 支配者是合成的虚拟根，即该对象被一个或多个 GC 根直接持有。
    VirtualRoot,
    /// 支配者是另一个对象，给出其内部节点序号（1 起）。
    Object(usize),
}

/// 单个对象的保留内存查询结果。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Retention {
    /// 构建索引时的强可达状态。
    pub reachability: Reachability,
    /// 直接支配者；不可达对象没有支配者，为 `None`。
    pub dominator: Option<Dominator>,
    /// 移除该对象的全部强入边后会变得不可达的总字节，包含对象自身。
    /// 不可达对象为 0（它本就不占强可达内存）。
    pub retained_bytes: u64,
    /// 对象自身登记的字节数，无论可达与否都可读取。
    pub own_bytes: u64,
}

/// 索引构建的统计信息。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BuildStats {
    /// 登记的对象总数。
    pub total_objects: usize,
    /// 强可达对象数。
    pub reachable_objects: usize,
    /// 构建时即不可达的对象数。
    pub unreachable_objects: usize,
    /// 强边总数，包含虚拟根指向各个**去重后**根对象的合成强边。
    pub strong_edges: usize,
    /// 弱引用总数（不参与图计算）。
    pub weak_edges: usize,
    /// 去重后的根对象数。
    pub distinct_roots: usize,
    /// 全部强可达对象的字节总和（保留在虚拟根处的字节）。
    pub total_reachable_bytes: u64,
}

#[derive(Debug, Clone)]
struct Slot {
    size: u64,
    reachable: bool,
    dominator: Option<Dominator>,
    retained: u64,
}

/// 构建完成的保留内存索引。
///
/// 内部把对象标识映射为 1 起的紧凑节点序号，节点 `0` 保留给合成虚拟根。
/// 建好之后，逐对象查询只做哈希定位与数组读取，不再遍历任何引用。
#[derive(Debug)]
pub struct RetainedIndex<I> {
    // 下标 0 为虚拟根的占位；对象字段从下标 1 开始。
    slots: Vec<Slot>,
    // 与 slots 对齐的对象标识，下标 0 不使用。
    ids: Vec<Option<I>>,
    index: HashMap<I, usize>,
    stats: BuildStats,
}

impl<I> RetainedIndex<I>
where
    I: Eq + Hash + Clone + fmt::Debug,
{
    /// 构建保留内存索引。
    ///
    /// - `sizes`：`(对象标识, 自身字节)` 列表，标识不可重复；
    /// - `roots`：GC 根直接持有的对象，等价于来自虚拟根的强边（自动去重）；
    /// - `edges`：带强弱标记的有向引用；弱引用不参与可达性，但端点必须已知。
    pub fn build<S, R, E>(sizes: S, roots: R, edges: E) -> Result<RetainedIndex<I>, BuildError>
    where
        S: IntoIterator<Item = (I, u64)>,
        R: IntoIterator<Item = I>,
        E: IntoIterator<Item = Edge<I>>,
    {
        // 1. 登记对象，节点 0 留给虚拟根（只占槽位，不存标识）。
        let mut slots: Vec<Slot> = Vec::new();
        let mut ids: Vec<Option<I>> = Vec::new();
        slots.push(Slot { size: 0, reachable: false, dominator: None, retained: 0 });
        ids.push(None);
        let mut index: HashMap<I, usize> = HashMap::new();
        for (id, size) in sizes {
            match index.entry(id) {
                Entry::Occupied(e) => {
                    return Err(BuildError::DuplicateObject(format!("{:?}", e.key())));
                }
                Entry::Vacant(e) => {
                    let node = slots.len();
                    ids.push(Some(e.key().clone()));
                    e.insert(node);
                    slots.push(Slot {
                        size,
                        reachable: false,
                        dominator: None,
                        retained: 0,
                    });
                }
            }
        }
        let n = slots.len();

        // 2. 收集根（去重），等价于虚拟根 -> 根对象的强边。
        let mut root_nodes: Vec<usize> = Vec::new();
        let mut is_root = vec![false; n];
        for id in roots {
            let &node = index
                .get(&id)
                .ok_or_else(|| BuildError::UnknownRoot(format!("{id:?}")))?;
            if !is_root[node] {
                is_root[node] = true;
                root_nodes.push(node);
            }
        }

        // 3. 收集引用并校验端点；弱引用单独计数。
        let mut strong_pairs: Vec<(usize, usize)> = Vec::new();
        let mut weak_edges = 0usize;
        for edge in edges {
            let Edge { source, target, kind } = edge;
            let s = *index
                .get(&source)
                .ok_or_else(|| BuildError::UnknownEndpoint(format!("{source:?}")))?;
            let t = *index
                .get(&target)
                .ok_or_else(|| BuildError::UnknownEndpoint(format!("{target:?}")))?;
            match kind {
                EdgeKind::Strong => strong_pairs.push((s, t)),
                EdgeKind::Weak => weak_edges += 1,
            }
        }

        // 4. 构建紧凑图：节点 0 为虚拟根，其出边指向各个根。
        //    重复边、自环原样保留，CHW 算法对它们天然稳健。
        let mut succ: Vec<Vec<usize>> = vec![Vec::new(); n];
        let mut pred: Vec<Vec<usize>> = vec![Vec::new(); n];
        for &r in &root_nodes {
            succ[0].push(r);
            pred[r].push(0);
        }
        for (s, t) in strong_pairs {
            succ[s].push(t);
            pred[t].push(s);
        }
        let strong_edges = root_nodes.len() + succ[1..].iter().map(Vec::len).sum::<usize>();

        // 5. 支配者计算（内部含迭代 DFS，自动区分可达/不可达节点）。
        let dominance = dominator::compute(&succ, &pred);
        let dominator::Dominance { idom, rpo } = dominance;

        // 6. 标记可达性并记录直接支配者。
        let mut reachable_objects = 0usize;
        for node in 1..n {
            if idom[node] != dominator::UNKNOWN {
                slots[node].reachable = true;
                reachable_objects += 1;
                slots[node].dominator = Some(if idom[node] == 0 {
                    Dominator::VirtualRoot
                } else {
                    Dominator::Object(idom[node])
                });
            }
        }

        // 7. 逆 RPO 自底向上累加保留字节：
        //    每个节点的保留字节 = 自身大小 + 支配者树中所有子节点的保留字节。
        //    逆 RPO 中父（支配者）排在子之前，反向遍历时子必先于父完成累加。
        let mut retained = vec![0u64; n];
        for &node in rpo.iter().rev() {
            retained[node] = retained[node]
                .checked_add(if node == 0 { 0 } else { slots[node].size })
                .ok_or(BuildError::SizeOverflow(node))?;
            if node != 0 {
                let parent = idom[node];
                // 父节点累加子树时溢出，归因到父节点（0 即全堆总量）。
                retained[parent] = retained[parent]
                    .checked_add(retained[node])
                    .ok_or(BuildError::SizeOverflow(parent))?;
            }
        }
        for node in 1..n {
            slots[node].retained = if slots[node].reachable { retained[node] } else { 0 };
        }

        let total_reachable_bytes = retained[0];
        let stats = BuildStats {
            total_objects: n - 1,
            reachable_objects,
            unreachable_objects: (n - 1) - reachable_objects,
            strong_edges,
            weak_edges,
            distinct_roots: root_nodes.len(),
            total_reachable_bytes,
        };

        Ok(RetainedIndex { slots, ids, index, stats })
    }

    /// 登记的对象总数。
    pub fn len(&self) -> usize {
        self.slots.len() - 1
    }

    /// 是否没有登记任何对象。
    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    /// 对象的内部节点序号（1 起）；未知标识返回 `None`。
    pub fn node_of(&self, id: &I) -> Option<usize> {
        self.index.get(id).copied()
    }

    /// 按内部节点序号（1 起）反查对象标识；序号 0（虚拟根）或越界返回 `None`。
    pub fn id_at(&self, node: usize) -> Option<&I> {
        self.ids.get(node).and_then(|id| id.as_ref())
    }

    /// 构建索引时该对象是否强可达；未知标识返回 `None`。
    pub fn reachability_of(&self, id: &I) -> Option<Reachability> {
        self.node_of(id).map(|node| {
            if self.slots[node].reachable {
                Reachability::Reachable
            } else {
                Reachability::Unreachable
            }
        })
    }

    /// 直接支配者：不可达对象返回 `Some(None)`，未知标识返回 `None`。
    pub fn dominator_of(&self, id: &I) -> Option<Option<Dominator>> {
        self.node_of(id).map(|node| self.slots[node].dominator)
    }

    /// 完整的保留内存查询结果；未知标识返回 `None`。
    pub fn retention(&self, id: &I) -> Option<Retention> {
        self.node_of(id).map(|node| {
            let slot = &self.slots[node];
            Retention {
                reachability: if slot.reachable {
                    Reachability::Reachable
                } else {
                    Reachability::Unreachable
                },
                dominator: slot.dominator,
                retained_bytes: slot.retained,
                own_bytes: slot.size,
            }
        })
    }

    /// 按内部节点序号（1 起，等价于 `sizes` 的登记顺序）查询。
    /// 序号越界返回 `None`。
    pub fn retention_at(&self, node: usize) -> Option<Retention> {
        if node == 0 || node >= self.slots.len() {
            return None;
        }
        let slot = &self.slots[node];
        Some(Retention {
            reachability: if slot.reachable {
                Reachability::Reachable
            } else {
                Reachability::Unreachable
            },
            dominator: slot.dominator,
            retained_bytes: slot.retained,
            own_bytes: slot.size,
        })
    }

    /// 遍历全部对象的查询结果，顺序与 `sizes` 的登记顺序一致。
    pub fn retention_iter(&self) -> impl Iterator<Item = Retention> + '_ {
        self.slots[1..].iter().map(|slot| Retention {
            reachability: if slot.reachable {
                Reachability::Reachable
            } else {
                Reachability::Unreachable
            },
            dominator: slot.dominator,
            retained_bytes: slot.retained,
            own_bytes: slot.size,
        })
    }

    /// 构建统计信息。
    pub fn stats(&self) -> &BuildStats {
        &self.stats
    }
}

