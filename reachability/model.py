"""数据模型与输入校验。

四类输入文档（均为 JSON）：
- lockfile 摘要：依赖实例列表，实例 id 形如 ``name@version``，用于区分
  同一依赖的不同版本实例。
- 调用边：带命名空间的函数调用边，记录构建模式（build_mode）与目标解析
  状态（resolution: resolved / unresolved）。
- 入口清单：各运行模式（release / test / dev ...）的入口符号列表。
- 受影响函数标识：带实例命名空间的符号，如 ``acme@1.4.0:parse_token``。
"""
from dataclasses import dataclass
from typing import Dict, Tuple


class InputError(ValueError):
    """输入文档校验失败。"""


VALID_RESOLUTIONS = ("resolved", "unresolved")
ALL_MODES = "all"


@dataclass(frozen=True)
class Instance:
    id: str
    name: str
    version: str


@dataclass(frozen=True)
class Edge:
    caller: str
    callee: str
    build_mode: str
    resolution: str
    reason: str = ""


@dataclass(frozen=True)
class Affected:
    symbol: str
    advisory: str
    severity: str


@dataclass(frozen=True)
class Dataset:
    instances: Tuple[Instance, ...]
    edges: Tuple[Edge, ...]
    modes: Dict[str, Tuple[str, ...]]
    affected: Tuple[Affected, ...]


def _require(cond, msg):
    if not cond:
        raise InputError(msg)


def _require_str(value, what):
    _require(isinstance(value, str) and value.strip() != "",
             f"{what} 必须是非空字符串, 实际: {value!r}")
    return value


def dataset_from_dicts(lockfile, edges_doc, entries_doc, affected_doc):
    """从四个已解析的 JSON 文档构造并校验 Dataset。"""
    # ---- 锁文件摘要：依赖实例 ----
    _require(isinstance(lockfile, dict), "lockfile 必须是 JSON 对象")
    raw_instances = lockfile.get("instances")
    _require(isinstance(raw_instances, list) and raw_instances,
             "lockfile.instances 必须是非空数组")
    instances = []
    seen_ids = set()
    for i, item in enumerate(raw_instances):
        _require(isinstance(item, dict), f"lockfile.instances[{i}] 必须是对象")
        inst_id = _require_str(item.get("id"), f"lockfile.instances[{i}].id")
        name = _require_str(item.get("name"), f"lockfile.instances[{i}].name")
        version = _require_str(item.get("version"),
                               f"lockfile.instances[{i}].version")
        _require(inst_id == f"{name}@{version}",
                 f"lockfile.instances[{i}].id 应为 'name@version', 实际: {inst_id!r}")
        _require(inst_id not in seen_ids,
                 f"依赖实例 id 重复: {inst_id!r}")
        seen_ids.add(inst_id)
        instances.append(Instance(id=inst_id, name=name, version=version))

    # ---- 入口清单：运行模式 ----
    _require(isinstance(entries_doc, dict), "entries 必须是 JSON 对象")
    raw_modes = entries_doc.get("modes")
    _require(isinstance(raw_modes, dict) and raw_modes,
             "entries.modes 必须是非空对象")
    modes = {}
    for mode, entries in raw_modes.items():
        _require_str(mode, "entries.modes 的模式名")
        _require(isinstance(entries, list) and entries,
                 f"entries.modes[{mode!r}] 必须是非空数组")
        seen = set()
        norm = []
        for j, entry in enumerate(entries):
            _require_str(entry, f"entries.modes[{mode!r}][{j}]")
            _require(entry not in seen,
                     f"模式 {mode!r} 的入口重复: {entry!r}")
            seen.add(entry)
            norm.append(entry)
        modes[mode] = tuple(norm)

    # ---- 调用边 ----
    _require(isinstance(edges_doc, dict), "call_edges 必须是 JSON 对象")
    raw_edges = edges_doc.get("edges")
    _require(isinstance(raw_edges, list), "call_edges.edges 必须是数组")
    edges = []
    for i, item in enumerate(raw_edges):
        _require(isinstance(item, dict), f"call_edges.edges[{i}] 必须是对象")
        caller = _require_str(item.get("caller"),
                              f"call_edges.edges[{i}].caller")
        callee = _require_str(item.get("callee"),
                              f"call_edges.edges[{i}].callee")
        build_mode = _require_str(item.get("build_mode"),
                                  f"call_edges.edges[{i}].build_mode")
        _require(build_mode == ALL_MODES or build_mode in modes,
                 f"call_edges.edges[{i}].build_mode 未知: {build_mode!r} "
                 f"(应为 'all' 或已声明模式之一)")
        resolution = _require_str(item.get("resolution"),
                                  f"call_edges.edges[{i}].resolution")
        _require(resolution in VALID_RESOLUTIONS,
                 f"call_edges.edges[{i}].resolution 未知: {resolution!r}")
        reason = item.get("reason", "")
        _require(isinstance(reason, str),
                 f"call_edges.edges[{i}].reason 必须是字符串")
        if resolution == "unresolved":
            _require(reason.strip() != "",
                     f"call_edges.edges[{i}] 未解析, 必须给出 reason")
        edges.append(Edge(caller=caller, callee=callee, build_mode=build_mode,
                          resolution=resolution, reason=reason))

    # ---- 受影响函数标识 ----
    _require(isinstance(affected_doc, dict), "affected 必须是 JSON 对象")
    raw_affected = affected_doc.get("affected")
    _require(isinstance(raw_affected, list) and raw_affected,
             "affected.affected 必须是非空数组")
    affected = []
    seen_symbols = set()
    for i, item in enumerate(raw_affected):
        _require(isinstance(item, dict), f"affected.affected[{i}] 必须是对象")
        symbol = _require_str(item.get("symbol"),
                              f"affected.affected[{i}].symbol")
        advisory = _require_str(item.get("advisory"),
                                f"affected.affected[{i}].advisory")
        severity = _require_str(item.get("severity"),
                                f"affected.affected[{i}].severity")
        _require(":" in symbol,
                 f"受影响符号缺少命名空间分隔符 ':': {symbol!r}")
        inst_id = symbol.split(":", 1)[0]
        _require(inst_id in seen_ids,
                 f"受影响符号 {symbol!r} 指向未知依赖实例 {inst_id!r}")
        _require(symbol not in seen_symbols,
                 f"受影响符号重复: {symbol!r}")
        seen_symbols.add(symbol)
        affected.append(Affected(symbol=symbol, advisory=advisory,
                                 severity=severity))

    return Dataset(instances=tuple(instances), edges=tuple(edges),
                   modes=modes, affected=tuple(affected))
