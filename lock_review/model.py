"""数据模型：维护者导出的抽象动作流规范。

规范为 JSON 文档，结构如下::

    {
      "configs": [
        {"id": "cfg-standard", "enables": ["realtime", "on-access"]}
      ],
      "exclusive_groups": [["scan-mode", "restore-mode"]],
      "streams": [
        {
          "id": "quarantine-stop",
          "phase": "quarantine",
          "mode": "stop-mode",
          "conditions": ["realtime"],
          "steps": [
            {"op": "acquire", "lock": "obj-table", "mode": "write"},
            {"op": "cancel", "releases": ["obj-table"]},
            {"op": "release", "lock": "obj-table"}
          ]
        }
      ]
    }

* ``configs``   —— 支持的配置组合；``enables`` 列出该组合启用的条件。
* ``exclusive_groups`` —— 互斥运维模式组，同组内不同模式不会并发运行。
* ``streams``   —— 抽象动作流；``phase`` 为处置阶段（如 quarantine/recovery），
  ``conditions`` 为启用条件，``steps`` 记录锁的申请、释放、读写模式与取消路径。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Tuple


@dataclass(frozen=True)
class Config:
    """一个支持的配置组合。"""

    id: str
    enables: frozenset


@dataclass(frozen=True)
class Stream:
    """一条抽象动作流。"""

    id: str
    phase: str
    mode: str
    conditions: frozenset
    steps: Tuple[Dict[str, Any], ...]


@dataclass(frozen=True)
class Spec:
    """完整规范：配置组合、互斥模式组与动作流。"""

    configs: Tuple[Config, ...] = field(default_factory=tuple)
    exclusive_groups: Tuple[frozenset, ...] = field(default_factory=tuple)
    streams: Tuple[Stream, ...] = field(default_factory=tuple)


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise ValueError(message)


def load_spec(doc: Dict[str, Any]) -> Spec:
    """从已解析的 JSON 字典构建 :class:`Spec`，并做结构校验。"""
    _require(isinstance(doc, dict), "规范必须是 JSON 对象")

    configs: List[Config] = []
    for raw in doc.get("configs", []):
        _require(isinstance(raw.get("id"), str), "config 缺少字符串 id")
        enables = raw.get("enables", [])
        _require(isinstance(enables, list), f"config {raw['id']} 的 enables 必须是数组")
        configs.append(Config(id=raw["id"], enables=frozenset(enables)))

    groups: List[frozenset] = []
    for raw in doc.get("exclusive_groups", []):
        _require(isinstance(raw, list) and len(raw) >= 2, "互斥组至少包含两个模式")
        groups.append(frozenset(raw))

    streams: List[Stream] = []
    for raw in doc.get("streams", []):
        for key in ("id", "phase", "mode"):
            _require(isinstance(raw.get(key), str), f"动作流缺少字符串字段 {key}")
        steps = raw.get("steps", [])
        _require(isinstance(steps, list), f"动作流 {raw['id']} 的 steps 必须是数组")
        for step in steps:
            op = step.get("op")
            _require(op in ("acquire", "release", "cancel"),
                     f"动作流 {raw['id']} 含未知操作 {op!r}")
            if op in ("acquire", "release"):
                _require(isinstance(step.get("lock"), str),
                         f"动作流 {raw['id']} 的 {op} 缺少 lock")
            if op == "acquire":
                _require(step.get("mode", "write") in ("read", "write"),
                         f"动作流 {raw['id']} 的 acquire 模式必须是 read/write")
            if op == "cancel":
                _require(isinstance(step.get("releases", []), list),
                         f"动作流 {raw['id']} 的 cancel 缺少 releases 数组")
        streams.append(Stream(
            id=raw["id"],
            phase=raw["phase"],
            mode=raw["mode"],
            conditions=frozenset(raw.get("conditions", [])),
            steps=tuple(dict(s) for s in steps),
        ))

    return Spec(configs=tuple(configs),
                exclusive_groups=tuple(groups),
                streams=tuple(streams))


def load_spec_file(path: "str | Path") -> Spec:
    """从 JSON 文件加载规范。"""
    with open(path, "r", encoding="utf-8") as fh:
        return load_spec(json.load(fh))
