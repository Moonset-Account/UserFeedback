"""控制流图的节点与图模型。

节点类型（构建器导出的有限指令集）：
    snapshot   制品快照赋值：var = snapshot(artifact, version)
    verify     创建带编号的验签任务：task = verify(var, task_id)
    wait_all   等待一批任务全部结束（等待本身不产生信任）
    wait_any   等待一批任务中首个结束（同样不产生信任）
    branch     按任务返回值（真/假）分支
    publish    发布某个快照变量
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

SNAPSHOT = "snapshot"
VERIFY = "verify"
WAIT_ALL = "wait_all"
WAIT_ANY = "wait_any"
BRANCH = "branch"
PUBLISH = "publish"

NODE_TYPES = frozenset(
    {SNAPSHOT, VERIFY, WAIT_ALL, WAIT_ANY, BRANCH, PUBLISH}
)


@dataclass(frozen=True)
class Node:
    """CFG 中的一条指令。

    不同类型使用不同字段：
      snapshot: var, artifact, version
      verify:   var, task_id
      wait_*:   task_ids
      branch:   task_id
      publish:  var
    后继优先使用 edges（branch 有两条），否则用 next。
    """

    id: str
    type: str
    var: Optional[str] = None
    artifact: Optional[str] = None
    version: Optional[str] = None
    task_id: Optional[str] = None
    task_ids: tuple = ()
    next: Optional[str] = None
    true_next: Optional[str] = None
    false_next: Optional[str] = None
    raw: dict = field(default_factory=dict, compare=False)

    def successors(self) -> List[str]:
        """按节点类型解析后继节点 id 列表。"""
        if self.type == BRANCH:
            return [s for s in (self.true_next, self.false_next) if s is not None]
        return [self.next] if self.next is not None else []


@dataclass
class CFG:
    entry: str
    nodes: dict  # id -> Node
