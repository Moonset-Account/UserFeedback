"""输入模型：简化控制流、字段布局、抽象写入与复制指令。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Field:
    name: str
    offset: int
    size: int


@dataclass(frozen=True)
class Buffer:
    size: int | None  # None 表示动态长度（缺少静态布局）
    fields: tuple


@dataclass(frozen=True)
class Op:
    kind: str  # write_field | write | zero_buffer | copy
    args: dict


@dataclass(frozen=True)
class Node:
    id: str
    ops: tuple
    succ: tuple  # 0/1 个后继为顺序，2 个及以上为分支


@dataclass(frozen=True)
class Spec:
    buffer: Buffer
    nodes: tuple
    entry: str


def _require(cond, msg):
    if not cond:
        raise ValueError(msg)


def _parse_op(raw, fields_by_name):
    _require(isinstance(raw, dict) and "op" in raw, f"非法指令: {raw!r}")
    kind = raw["op"]
    if kind == "write_field":
        name = raw.get("field")
        _require(name in fields_by_name, f"write_field 引用了未知字段: {name!r}")
        return Op(kind, {"field": name})
    if kind == "write":
        off, length = raw.get("offset"), raw.get("length")
        _require(isinstance(off, int) and off >= 0, f"write 偏移非法: {off!r}")
        _require(isinstance(length, int) and length > 0, f"write 长度非法: {length!r}")
        return Op(kind, {"offset": off, "length": length})
    if kind == "zero_buffer":
        return Op(kind, {})
    if kind == "copy":
        off = raw.get("offset", 0)
        length = raw.get("length")
        _require(isinstance(off, int) and off >= 0, f"copy 偏移非法: {off!r}")
        _require(
            length == "dynamic" or (isinstance(length, int) and length > 0),
            f"copy 长度非法: {length!r}",
        )
        return Op(kind, {"offset": off, "length": length})
    raise ValueError(f"未知指令类型: {kind!r}")


def load_spec(data):
    """从 dict（通常由 JSON 读入）构造 Spec，并做静态校验。"""
    _require(isinstance(data, dict), "规格必须是对象")
    buf = data.get("buffer") or {}
    size = buf.get("size")
    _require(size is None or (isinstance(size, int) and size > 0),
             f"缓冲区长度非法: {size!r}")
    fields = []
    for f in buf.get("fields", []):
        off, sz = f.get("offset"), f.get("size")
        _require(isinstance(off, int) and off >= 0, f"字段偏移非法: {f!r}")
        _require(isinstance(sz, int) and sz > 0, f"字段长度非法: {f!r}")
        if size is not None:
            _require(off + sz <= size, f"字段越出缓冲区: {f!r}")
        fields.append(Field(f["name"], off, sz))
    fields_by_name = {f.name: f for f in fields}
    _require(len(fields_by_name) == len(fields), "字段名重复")

    raw_nodes = data.get("nodes") or []
    _require(raw_nodes, "控制流不能为空")
    nodes = []
    for nd in raw_nodes:
        nid = nd.get("id")
        _require(isinstance(nid, str) and nid, f"节点 id 非法: {nd!r}")
        ops = tuple(_parse_op(o, fields_by_name) for o in nd.get("ops", []))
        nodes.append(Node(nid, ops, tuple(nd.get("succ", ()))))
    ids = [n.id for n in nodes]
    _require(len(set(ids)) == len(ids), "节点 id 重复")
    id_set = set(ids)
    for n in nodes:
        for s in n.succ:
            _require(s in id_set, f"节点 {n.id!r} 的后继不存在: {s!r}")
    entry = data.get("entry", ids[0])
    _require(entry in id_set, f"入口节点不存在: {entry!r}")

    return Spec(Buffer(size, tuple(fields)), tuple(nodes), entry)
