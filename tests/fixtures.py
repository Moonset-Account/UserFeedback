"""合成 CFG 夹具：以链式调用快速构建控制流图，全部数据本地生成。"""

from __future__ import annotations

from typing import Dict, List, Optional

from sigreview.loader import load_dict


class CFGBuilder:
    def __init__(self) -> None:
        self.nodes: List[dict] = []
        self._seq = 0

    def _nid(self, prefix: str) -> str:
        nid = f"{prefix}{self._seq}"
        self._seq += 1
        return nid

    def snapshot(self, var: str, artifact: str, version: str,
                 nid: Optional[str] = None) -> str:
        nid = nid or self._nid("s")
        self.nodes.append({
            "id": nid, "type": "snapshot", "var": var,
            "artifact": artifact, "version": version,
        })
        return nid

    def verify(self, var: str, task: str, nid: Optional[str] = None) -> str:
        nid = nid or self._nid("v")
        self.nodes.append({
            "id": nid, "type": "verify", "var": var, "task": task,
        })
        return nid

    def wait(self, tasks: List[str], kind: str = "wait_all",
             nid: Optional[str] = None) -> str:
        nid = nid or self._nid("w")
        self.nodes.append({"id": nid, "type": kind, "tasks": list(tasks)})
        return nid

    def branch(self, task: str, true_next: Optional[str] = None,
               false_next: Optional[str] = None,
               nid: Optional[str] = None) -> str:
        nid = nid or self._nid("b")
        node = {"id": nid, "type": "branch", "task": task}
        if true_next is not None:
            node["true_next"] = true_next
        if false_next is not None:
            node["false_next"] = false_next
        self.nodes.append(node)
        return nid

    def publish(self, var: str, nid: Optional[str] = None) -> str:
        nid = nid or self._nid("p")
        self.nodes.append({"id": nid, "type": "publish", "var": var})
        return nid

    def link(self, src: str, dst: str) -> None:
        """给非 branch 节点补单后继。"""
        node = self._get(src)
        if node["type"] == "branch":
            raise ValueError("branch 节点请用 branch() 的 true/false 参数")
        node["next"] = dst

    def link_branch(self, nid: str, true_next: Optional[str] = None,
                    false_next: Optional[str] = None) -> None:
        node = self._get(nid)
        if true_next is not None:
            node["true_next"] = true_next
        if false_next is not None:
            node["false_next"] = false_next

    def _get(self, nid: str) -> dict:
        for node in self.nodes:
            if node["id"] == nid:
                return node
        raise KeyError(nid)

    def build(self, entry: Optional[str] = None):
        data: Dict[str, dict] = {"nodes": self.nodes}
        data["entry"] = entry or self.nodes[0]["id"]
        return load_dict(data)
