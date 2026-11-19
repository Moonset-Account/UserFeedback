"""构建器导出 JSON 的解析与结构性校验。

支持的文档形态::

    {
      "entry": "n0",
      "nodes": [
        {"id": "n0", "type": "snapshot", "var": "a",
         "artifact": "lib", "version": "1", "next": "n1"},
        {"id": "n1", "type": "verify", "var": "a", "task": "t1",
         "next": "n2"},
        {"id": "n2", "type": "wait_all", "tasks": ["t1"], "next": "n3"},
        {"id": "n3", "type": "branch", "task": "t1",
         "true_next": "n4", "false_next": "n5"},
        {"id": "n4", "type": "publish", "var": "a"}
      ],
      "edges": [["n5", "n6"], ...]   # 可选，用于补充后继
    }

结构问题（重复节点、悬挂后继、入口缺失等）抛 :class:`CFGFormatError`；
同一 task 编号绑定多次等语义结构问题以 finding 形式返回（见 analysis）。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple

from .model import NODE_TYPES, CFG, Node


class CFGFormatError(ValueError):
    """CFG 文档结构不合法，无法进入数据流分析。"""


def _require(d: dict, key: str, nid: str) -> Any:
    if key not in d:
        raise CFGFormatError(f"节点 {nid!r} 缺少字段 {key!r}")
    return d[key]


def _as_id_list(value: Any, nid: str, field_name: str) -> Tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise CFGFormatError(
            f"节点 {nid!r} 的 {field_name!r} 必须是非空数组"
        )
    out: List[str] = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise CFGFormatError(
                f"节点 {nid!r} 的 {field_name!r} 含非法任务编号"
            )
        out.append(item)
    return tuple(out)


def _build_node(d: dict) -> Node:
    if not isinstance(d, dict):
        raise CFGFormatError("nodes 数组的元素必须是对象")
    nid = d.get("id")
    if not isinstance(nid, str) or not nid:
        raise CFGFormatError("每个节点都需要字符串类型的 id")
    ntype = d.get("type")
    if ntype not in NODE_TYPES:
        raise CFGFormatError(
            f"节点 {nid!r} 的类型 {ntype!r} 不受支持，"
            f"合法类型：{sorted(NODE_TYPES)}"
        )

    kwargs: Dict[str, Any] = {"id": nid, "type": ntype, "raw": d}

    if ntype == "snapshot":
        kwargs["var"] = _require(d, "var", nid)
        kwargs["artifact"] = _require(d, "artifact", nid)
        kwargs["version"] = str(_require(d, "version", nid))
        kwargs["next"] = d.get("next")
    elif ntype == "verify":
        kwargs["var"] = _require(d, "var", nid)
        kwargs["task_id"] = _require(d, "task", nid)
        kwargs["next"] = d.get("next")
    elif ntype in ("wait_all", "wait_any"):
        kwargs["task_ids"] = _as_id_list(
            _require(d, "tasks", nid), nid, "tasks"
        )
        kwargs["next"] = d.get("next")
    elif ntype == "branch":
        kwargs["task_id"] = _require(d, "task", nid)
        kwargs["true_next"] = d.get("true_next")
        kwargs["false_next"] = d.get("false_next")
    elif ntype == "publish":
        kwargs["var"] = _require(d, "var", nid)
        kwargs["next"] = d.get("next")
    return Node(**kwargs)


def load_dict(data: Any) -> CFG:
    """把普通 dict（已解析的 JSON）转换为 :class:`CFG`。"""
    if not isinstance(data, dict):
        raise CFGFormatError("CFG 文档顶层必须是对象")
    if "nodes" not in data or not isinstance(data["nodes"], list):
        raise CFGFormatError("CFG 文档需要 nodes 数组")
    entry = data.get("entry")
    if not isinstance(entry, str) or not entry:
        raise CFGFormatError("CFG 文档需要字符串类型的 entry")

    nodes: Dict[str, Node] = {}
    for raw in data["nodes"]:
        node = _build_node(raw)
        if node.id in nodes:
            raise CFGFormatError(f"节点 id 重复：{node.id!r}")
        nodes[node.id] = node

    if entry not in nodes:
        raise CFGFormatError(f"入口节点 {entry!r} 不存在")

    # 可选的全局 edges 仅允许补充缺失后继，不允许与节点内后继冲突。
    extra_edges: List[Tuple[str, str]] = []
    for edge in data.get("edges", []):
        if (
            not isinstance(edge, list)
            or len(edge) != 2
            or not all(isinstance(x, str) for x in edge)
        ):
            raise CFGFormatError("edges 元素必须是 [源id, 目标id]")
        extra_edges.append((edge[0], edge[1]))

    for src, dst in extra_edges:
        if src not in nodes:
            raise CFGFormatError(f"边的源节点 {src!r} 不存在")
        if dst not in nodes:
            raise CFGFormatError(f"边的目标节点 {dst!r} 不存在")
        node = nodes[src]
        if node.type == "branch":
            raise CFGFormatError(
                f"branch 节点 {src!r} 的后继必须由 true_next/false_next 给出"
            )
        if node.next is not None:
            raise CFGFormatError(f"节点 {src!r} 的后继重复定义")
        nodes[src] = Node(
            id=node.id,
            type=node.type,
            var=node.var,
            artifact=node.artifact,
            version=node.version,
            task_id=node.task_id,
            task_ids=node.task_ids,
            next=dst,
            true_next=node.true_next,
            false_next=node.false_next,
            raw=node.raw,
        )

    # 悬挂后继检查。
    for node in nodes.values():
        for succ in node.successors():
            if succ not in nodes:
                raise CFGFormatError(
                    f"节点 {node.id!r} 的后继 {succ!r} 不存在"
                )
    return CFG(entry=entry, nodes=nodes)


def load_file(path: str) -> CFG:
    """从 JSON 文件加载 CFG。"""
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return load_dict(data)
