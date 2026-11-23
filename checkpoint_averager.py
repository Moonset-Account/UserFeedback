"""可复核的检查点权重平均器。

针对重试/分叉训练运行产生的检查点目录：

* 检查点用若干有理数（``fractions.Fraction``）表示参数，携带父节点列表、
  逻辑步与内容摘要（digest）。
* 同一逻辑步可登记多个尝试；不同分支可通过多父节点检查点再合并。
* 发布清单（``Manifest``）给出逻辑步顺序、每步允许的祖先范围与可选的固定
  尝试；``evaluate`` 枚举所有血缘上前后可达的合法选择，按最近 W 步计算顺序
  相关的指数平均（窗口首项为初值，系数为清单给定的有理数）。
* 对每个参数维度给出所有合法选择中可达到的最小/最大平均值，各附一条完整
  见证路径（共享祖先只列一次）。只有每一维的值都唯一（min == max）才可发布；
  否则报告首个分歧步及竞争摘要。
* 目录支持迟到补登记（任意顺序登记）与固定旧修订回读（按修订号快照查询）。

仅使用标准库，Python 3.11。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Optional, Sequence


# ---------------------------------------------------------------------------
# 检查点与摘要
# ---------------------------------------------------------------------------

def compute_digest(step: int, parents: Sequence[str], params: Sequence[Fraction]) -> str:
    """按规范序列化计算内容摘要。"""
    payload = {
        "step": step,
        "parents": list(parents),
        "params": [str(Fraction(p)) for p in params],
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


@dataclass(frozen=True)
class Checkpoint:
    """一个检查点：参数为若干有理数，含父节点、逻辑步与内容摘要。"""

    checkpoint_id: str
    step: int
    parents: tuple[str, ...]
    params: tuple[Fraction, ...]
    digest: str

    @classmethod
    def create(
        cls,
        checkpoint_id: str,
        step: int,
        parents: Sequence[str] = (),
        params: Sequence[Fraction | int | str] = (),
    ) -> "Checkpoint":
        parents = tuple(parents)
        params = tuple(Fraction(p) for p in params)
        if len(set(parents)) != len(parents):
            raise ValueError(f"重复父节点: {parents!r}")
        digest = compute_digest(step, parents, params)
        return cls(checkpoint_id, step, parents, params, digest)


# ---------------------------------------------------------------------------
# 检查点目录：迟到补登记 + 固定旧修订回读
# ---------------------------------------------------------------------------

class CheckpointDirectory:
    """检查点目录。

    每次 ``register`` 产生一个新修订号（从 1 开始递增）。所有查询可传入
    ``revision`` 固定回读旧修订：只能看到在该修订（含）之前登记、且当时
    未被替换的记录。登记顺序任意，允许迟到补登记较早逻辑步的尝试。
    """

    def __init__(self) -> None:
        self._revision = 0
        # checkpoint_id -> (registered_revision, checkpoint)
        self._records: dict[str, tuple[int, Checkpoint]] = {}

    @property
    def revision(self) -> int:
        return self._revision

    def register(self, checkpoint: Checkpoint) -> int:
        """登记检查点，返回新修订号。允许任意顺序（迟到补登记）。"""
        cid = checkpoint.checkpoint_id
        if cid in self._records:
            raise ValueError(f"检查点已登记: {cid}")
        self._revision += 1
        self._records[cid] = (self._revision, checkpoint)
        return self._revision

    def get(self, checkpoint_id: str, revision: Optional[int] = None) -> Optional[Checkpoint]:
        rec = self._records.get(checkpoint_id)
        if rec is None:
            return None
        rev, ckpt = rec
        if revision is not None and rev > revision:
            return None
        return ckpt

    def at_step(self, step: int, revision: Optional[int] = None) -> list[Checkpoint]:
        """某逻辑步在指定修订下可见的全部尝试，按 id 排序保证确定性。"""
        out = []
        for rev, ckpt in self._records.values():
            if ckpt.step == step and (revision is None or rev <= revision):
                out.append(ckpt)
        out.sort(key=lambda c: c.checkpoint_id)
        return out

    def is_reachable(
        self, ancestor_id: str, descendant_id: str, revision: Optional[int] = None
    ) -> bool:
        """在指定修订下，ancestor 是否沿父链可达 descendant（含相等）。"""
        if ancestor_id == descendant_id:
            return self.get(ancestor_id, revision) is not None
        seen: set[str] = set()
        stack = [descendant_id]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            ckpt = self.get(cur, revision)
            if ckpt is None:
                continue
            for p in ckpt.parents:
                if p == ancestor_id:
                    return True
                stack.append(p)
        return False

    def lineage_path(
        self, ancestor_id: str, descendant_id: str, revision: Optional[int] = None
    ) -> Optional[list[str]]:
        """返回一条从 ancestor 到 descendant 的父链路径（含两端，按血缘先后）。

        不存在时返回 None。多条路径时取确定性的一条（BFS，父节点按序展开）。
        """
        if ancestor_id == descendant_id:
            return [ancestor_id] if self.get(ancestor_id, revision) is not None else None
        # 从 descendant 沿父链 BFS 找 ancestor，记录前驱。
        prev: dict[str, Optional[str]] = {descendant_id: None}
        queue = [descendant_id]
        while queue:
            cur = queue.pop(0)
            ckpt = self.get(cur, revision)
            if ckpt is None:
                continue
            for p in ckpt.parents:
                if p in prev:
                    continue
                prev[p] = cur
                if p == ancestor_id:
                    # 还原路径
                    path = [ancestor_id]
                    node = cur
                    chain = [node]
                    while prev[node] is not None:
                        node = prev[node]
                        chain.append(node)
                    # chain 是从 descendant 侧回到 ancestor 子节点的逆序
                    path.extend(reversed(chain))
                    return path
                queue.append(p)
        return None


# ---------------------------------------------------------------------------
# 发布清单
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StepRequirement:
    """清单中一个逻辑步的要求。

    * ``step``: 需要的逻辑步。
    * ``allowed_ancestors``: 可选；所选检查点必须是其中至少一个检查点的
      后代（或其本身），限定允许的祖先范围。
    * ``fixed``: 可选；固定选择某个尝试（检查点 id）。
    """

    step: int
    allowed_ancestors: Optional[frozenset[str]] = None
    fixed: Optional[str] = None


@dataclass(frozen=True)
class Manifest:
    """发布清单：逻辑步顺序 + 指数平均窗口与系数。"""

    requirements: tuple[StepRequirement, ...]
    window: int
    alpha: Fraction

    def __post_init__(self) -> None:
        if not self.requirements:
            raise ValueError("清单至少需要一个逻辑步")
        steps = [r.step for r in self.requirements]
        if len(set(steps)) != len(steps):
            raise ValueError(f"逻辑步重复: {steps!r}")
        if self.window < 1:
            raise ValueError("窗口 W 至少为 1")
        if not (Fraction(0) <= Fraction(self.alpha) <= Fraction(1)):
            raise ValueError("系数 alpha 必须在 [0, 1] 内")


# ---------------------------------------------------------------------------
# 评估结果
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Witness:
    """一条完整见证路径：每个清单步选中的检查点 + 展开后的血缘路径。

    ``path`` 为从根到末步选中检查点的完整血缘链，共享祖先只出现一次。
    """

    chosen: tuple[str, ...]
    path: tuple[str, ...]


@dataclass(frozen=True)
class Divergence:
    """不可发布时的分歧报告。"""

    dimension: int
    step: int
    position: int
    choice_min: tuple[str, str]  # (checkpoint_id, digest)
    choice_max: tuple[str, str]


@dataclass(frozen=True)
class DimensionResult:
    dimension: int
    minimum: Fraction
    maximum: Fraction
    min_witness: Witness
    max_witness: Witness

    @property
    def unique(self) -> bool:
        return self.minimum == self.maximum


@dataclass(frozen=True)
class PublishResult:
    feasible: bool
    publishable: bool
    window_steps: tuple[int, ...]
    dimensions: tuple[DimensionResult, ...]
    divergence: Optional[Divergence]
    revision: int


class InfeasibleManifestError(ValueError):
    """清单在当前目录修订下不存在合法选择。"""


# ---------------------------------------------------------------------------
# 评估
# ---------------------------------------------------------------------------

def _window_weights(n: int, window: int, alpha: Fraction) -> list[Fraction]:
    """各清单位置在最近 W 步指数平均中的权重（窗口首项为初值）。

    s_0 = x_first；s_t = alpha * x_t + (1 - alpha) * s_{t-1}。
    窗口外的位置权重为 0（仅参与血缘约束）。
    """
    alpha = Fraction(alpha)
    w = min(window, n)
    start = n - w
    weights = [Fraction(0)] * n
    weights[start] = (1 - alpha) ** (w - 1)
    for i in range(start + 1, n):
        weights[i] = alpha * (1 - alpha) ** (n - 1 - i)
    return weights


def _candidates(
    directory: CheckpointDirectory, req: StepRequirement, revision: Optional[int]
) -> list[Checkpoint]:
    if req.fixed is not None:
        ckpt = directory.get(req.fixed, revision)
        if ckpt is None:
            return []
        if ckpt.step != req.step:
            raise ValueError(
                f"固定尝试 {req.fixed} 的逻辑步 {ckpt.step} 与清单要求 {req.step} 不符"
            )
        pool = [ckpt]
    else:
        pool = directory.at_step(req.step, revision)
    if req.allowed_ancestors is not None:
        pool = [
            c
            for c in pool
            if any(
                directory.is_reachable(a, c.checkpoint_id, revision)
                for a in req.allowed_ancestors
            )
        ]
    return pool


def _dp_extremes(
    directory: CheckpointDirectory,
    levels: list[list[Checkpoint]],
    weights: list[Fraction],
    dim: int,
    revision: Optional[int],
    minimize: bool,
) -> tuple[Fraction, list[str]]:
    """对单个维度做链式 DP，返回 (极值, 各位置选中的检查点 id 列表)。"""
    n = len(levels)
    # best[i][cid] = (累计值, 前驱 cid)
    best: list[dict[str, tuple[Fraction, Optional[str]]]] = []
    first: dict[str, tuple[Fraction, Optional[str]]] = {}
    for c in levels[0]:
        first[c.checkpoint_id] = (weights[0] * c.params[dim], None)
    best.append(first)
    for i in range(1, n):
        cur: dict[str, tuple[Fraction, Optional[str]]] = {}
        for c in levels[i]:
            chosen: Optional[tuple[Fraction, str]] = None
            for pid, (val, _) in best[i - 1].items():
                if not directory.is_reachable(pid, c.checkpoint_id, revision):
                    continue
                cand = (val, pid)
                if chosen is None:
                    chosen = cand
                elif minimize and cand[0] < chosen[0]:
                    chosen = cand
                elif not minimize and cand[0] > chosen[0]:
                    chosen = cand
                elif cand[0] == chosen[0] and cand[1] < chosen[1]:
                    chosen = cand  # 确定性平局
            if chosen is not None:
                cur[c.checkpoint_id] = (chosen[0] + weights[i] * c.params[dim], chosen[1])
        if not cur:
            raise InfeasibleManifestError(f"清单第 {i} 个逻辑步无可达选择")
        best.append(cur)
    # 末端取极值（平局取 id 较小者保证确定性）
    last = best[-1]
    key = min if minimize else max
    target = key(v for v, _ in last.values())
    finals = sorted(cid for cid, (v, _) in last.items() if v == target)
    cid = finals[0]
    chosen_ids = [cid]
    for i in range(n - 1, 0, -1):
        _, pid = best[i][cid]
        assert pid is not None
        chosen_ids.append(pid)
        cid = pid
    chosen_ids.reverse()
    return target, chosen_ids


def _build_witness(
    directory: CheckpointDirectory,
    chosen_ids: Sequence[str],
    revision: Optional[int],
) -> Witness:
    """把选中序列展开成完整血缘路径，共享祖先只算一次。"""
    path: list[str] = []
    seen: set[str] = set()
    # 首段：从根到第一个选中检查点（沿父链取确定性路径）
    first = chosen_ids[0]
    ckpt = directory.get(first, revision)
    assert ckpt is not None
    # 找一条到根（无可见父节点）的链
    chain = [first]
    node = first
    while True:
        c = directory.get(node, revision)
        assert c is not None
        visible_parents = [p for p in c.parents if directory.get(p, revision) is not None]
        if not visible_parents:
            break
        node = visible_parents[0]
        chain.append(node)
    for cid in reversed(chain):
        if cid not in seen:
            seen.add(cid)
            path.append(cid)
    # 后续段：从前一个选中到下一个选中的父链
    for prev_id, next_id in zip(chosen_ids, chosen_ids[1:]):
        seg = directory.lineage_path(prev_id, next_id, revision)
        if seg is None:
            raise InfeasibleManifestError(f"{prev_id} 不可达 {next_id}")
        for cid in seg:
            if cid not in seen:
                seen.add(cid)
                path.append(cid)
    return Witness(chosen=tuple(chosen_ids), path=tuple(path))


def evaluate(
    directory: CheckpointDirectory,
    manifest: Manifest,
    revision: Optional[int] = None,
) -> PublishResult:
    """评估清单：返回每个维度的最小/最大平均与见证路径及可发布性。"""
    rev = directory.revision if revision is None else revision
    reqs = manifest.requirements
    n = len(reqs)

    levels = [_candidates(directory, r, rev) for r in reqs]
    for i, (req, cands) in enumerate(zip(reqs, levels)):
        if not cands:
            raise InfeasibleManifestError(f"清单第 {i} 个逻辑步 {req.step} 无候选检查点")

    dim_count = len(levels[0][0].params)
    for cands in levels:
        for c in cands:
            if len(c.params) != dim_count:
                raise ValueError("候选检查点参数维度不一致")

    weights = _window_weights(n, manifest.window, manifest.alpha)
    w = min(manifest.window, n)
    window_steps = tuple(r.step for r in reqs[n - w:])

    dimensions: list[DimensionResult] = []
    for dim in range(dim_count):
        min_val, min_ids = _dp_extremes(directory, levels, weights, dim, rev, minimize=True)
        max_val, max_ids = _dp_extremes(directory, levels, weights, dim, rev, minimize=False)
        dimensions.append(
            DimensionResult(
                dimension=dim,
                minimum=min_val,
                maximum=max_val,
                min_witness=_build_witness(directory, min_ids, rev),
                max_witness=_build_witness(directory, max_ids, rev),
            )
        )

    publishable = all(d.unique for d in dimensions)
    divergence: Optional[Divergence] = None
    if not publishable:
        for d in dimensions:
            if d.unique:
                continue
            # 从产生不同平均值的两条路径中找首个分歧步
            for pos, (a, b) in enumerate(
                zip(d.min_witness.chosen, d.max_witness.chosen)
            ):
                if a != b:
                    ca = directory.get(a, rev)
                    cb = directory.get(b, rev)
                    assert ca is not None and cb is not None
                    divergence = Divergence(
                        dimension=d.dimension,
                        step=reqs[pos].step,
                        position=pos,
                        choice_min=(a, ca.digest),
                        choice_max=(b, cb.digest),
                    )
                    break
            if divergence is not None:
                break

    return PublishResult(
        feasible=True,
        publishable=publishable,
        window_steps=window_steps,
        dimensions=tuple(dimensions),
        divergence=divergence,
        revision=rev,
    )
