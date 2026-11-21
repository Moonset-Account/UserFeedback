//! 紧凑节点索引图上的即时支配者计算。
//!
//! 实现 Cooper–Harvey–Kennedy《A Simple, Fast Dominance Algorithm》的迭代
//! 版本：先做迭代 DFS 得到后序编号与逆后序（RPO），再按 RPO 反复扫描，
//! 用 `intersect` 在已有支配者链上手指对走，直到收敛。
//!
//! 节点编号为 `0..n`，其中节点 `0` 约定为入口（调用方放入虚拟根）。
//! 从入口不可达的节点，其即时支配者保持为 [`UNKNOWN`]。

/// 表示“尚未计算/不可达”的哨兵节点编号。
pub(crate) const UNKNOWN: usize = usize::MAX;

/// 支配关系计算结果。
#[derive(Debug, Clone)]
pub(crate) struct Dominance {
    /// 每个节点的即时支配者；入口的支配者是它自己，不可达节点为 [`UNKNOWN`]。
    pub idom: Vec<usize>,
    /// 计算过程中使用的逆后序（RPO），入口排在最前。
    pub rpo: Vec<usize>,
}

/// 在给定邻接表上计算即时支配者。
///
/// `succ` 为出边表、`pred` 为入边表，二者描述同一张图。
pub(crate) fn compute(succ: &[Vec<usize>], pred: &[Vec<usize>]) -> Dominance {
    let n = succ.len();
    // 后序编号：先弹出的节点编号小，入口最后弹出、编号最大。
    let mut po = vec![UNKNOWN; n];
    // 收集后序（弹出顺序），最后原地反转为 RPO。
    let mut rpo: Vec<usize> = Vec::with_capacity(n);

    // 迭代 DFS，栈帧为 (节点, 下一个待处理的出边下标)。
    if n > 0 {
        po[0] = 0; // 先标记为已访问，弹出时再写入真正的后序编号。
        let mut stack: Vec<(usize, usize)> = vec![(0, 0)];
        while let Some((u, slot)) = stack.last_mut() {
            if *slot < succ[*u].len() {
                let v = succ[*u][*slot];
                *slot += 1;
                if po[v] == UNKNOWN {
                    po[v] = 0;
                    stack.push((v, 0));
                }
            } else {
                let (u, _) = stack.pop().expect("栈帧必然存在");
                po[u] = rpo.len();
                rpo.push(u);
            }
        }
    }

    let mut idom = vec![UNKNOWN; n];
    if rpo.is_empty() {
        return Dominance { idom, rpo };
    }

    // rpo 此时是后序（入口在末尾），反转为逆后序（入口在最前）。
    rpo.reverse();
    idom[0] = 0;

    // 后序编号越小，节点在 DFS 树中越深；支配者总是祖先，编号更大。
    // 两根手指各自沿 idom 上爬，较深（编号较小）的一方先爬，直到相遇。
    fn intersect(po: &[usize], idom: &[usize], mut f1: usize, mut f2: usize) -> usize {
        while f1 != f2 {
            while po[f1] < po[f2] {
                f1 = idom[f1];
            }
            while po[f2] < po[f1] {
                f2 = idom[f2];
            }
        }
        f1
    }

    let mut changed = true;
    while changed {
        changed = false;
        for &b in rpo.iter().skip(1) {
            let mut new_idom = UNKNOWN;
            for &p in &pred[b] {
                // 回边的源点可能在首轮尚未算出 idom，跳过并依赖外层迭代收敛。
                if idom[p] == UNKNOWN {
                    continue;
                }
                new_idom = if new_idom == UNKNOWN {
                    p
                } else {
                    intersect(&po, &idom, p, new_idom)
                };
            }
            if new_idom != UNKNOWN && idom[b] != new_idom {
                idom[b] = new_idom;
                changed = true;
            }
        }
    }

    Dominance { idom, rpo }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn graph(n: usize, edges: &[(usize, usize)]) -> (Vec<Vec<usize>>, Vec<Vec<usize>>) {
        let mut succ = vec![Vec::new(); n];
        let mut pred = vec![Vec::new(); n];
        for &(u, v) in edges {
            succ[u].push(v);
            pred[v].push(u);
        }
        (succ, pred)
    }

    #[test]
    fn diamond_shared_join_dominated_by_entry() {
        // 0 -> 1, 0 -> 2, 1 -> 3, 2 -> 3
        let (succ, pred) = graph(4, &[(0, 1), (0, 2), (1, 3), (2, 3)]);
        let d = compute(&succ, &pred);
        assert_eq!(d.idom, vec![0, 0, 0, 0]);
    }

    #[test]
    fn cycle_and_back_edges_converge() {
        // 0 -> 1 -> 2 -> 1（回边），2 -> 3
        let (succ, pred) = graph(4, &[(0, 1), (1, 2), (2, 1), (2, 3)]);
        let d = compute(&succ, &pred);
        assert_eq!(d.idom, vec![0, 0, 1, 2]);
    }

    #[test]
    fn disconnected_nodes_stay_unknown() {
        // 入口只能到达 1；2、3 是另一个孤立连通分量。
        let (succ, pred) = graph(4, &[(0, 1), (2, 3)]);
        let d = compute(&succ, &pred);
        assert_eq!(d.idom[0], 0);
        assert_eq!(d.idom[1], 0);
        assert_eq!(d.idom[2], UNKNOWN);
        assert_eq!(d.idom[3], UNKNOWN);
    }

    #[test]
    fn self_loop_does_not_poison_entry() {
        let (succ, pred) = graph(2, &[(0, 0), (0, 1)]);
        let d = compute(&succ, &pred);
        assert_eq!(d.idom, vec![0, 0]);
    }

    #[test]
    fn empty_graph_is_handled() {
        let succ: Vec<Vec<usize>> = Vec::new();
        let pred: Vec<Vec<usize>> = Vec::new();
        let d = compute(&succ, &pred);
        assert!(d.idom.is_empty());
        assert!(d.rpo.is_empty());
    }
}
