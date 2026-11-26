"""谓词的三值逻辑（3VL）求值与结构遍历。

SQL 风格规则：
* 任何一侧为 NULL 的比较结果为 UNKNOWN；
* ``IS NULL`` / ``IS NOT NULL`` 永远返回真或假，不会产生 UNKNOWN；
* AND / OR 按 Kleene 三值逻辑组合；
* 行通过筛选当且仅当谓词求值为 ``True``（UNKNOWN 与 False 一样被过滤）。
"""

from __future__ import annotations

from typing import Optional

from .model import And, Cmp, IsNotNull, IsNull, Or, Predicate, Row, type_family


def _cmp(left: object, op: str, right: object) -> Optional[bool]:
    """比较两个值；任一为 NULL 返回 ``None``（UNKNOWN）。

    字符串与数字/布尔之间不可比较，结果也是 UNKNOWN；
    数字族（int/float）内部可比较，bool 与 int 不混合比较。
    """
    if left is None or right is None:
        return None
    lf, rf = type_family(left), type_family(right)
    if lf != rf and not ({lf, rf} <= {"int", "float"}):
        return None
    if lf == "bool" or rf == "bool":
        if lf != rf:
            return None
    if op == "=":
        return left == right
    if op == "!=":
        return left != right
    if op == "<":
        return left < right
    if op == "<=":
        return left <= right
    if op == ">":
        return left > right
    if op == ">=":
        return left >= right
    raise ValueError(f"未知比较运算符: {op!r}")


def eval_predicate(pred: Optional[Predicate], row: Row) -> Optional[bool]:
    """对行求谓词值，返回 True / False / None(UNKNOWN)。"""
    if pred is None:
        return True
    if isinstance(pred, Cmp):
        return _cmp(row.get(pred.column), pred.op, pred.literal)
    if isinstance(pred, IsNull):
        return row.get(pred.column) is None
    if isinstance(pred, IsNotNull):
        return row.get(pred.column) is not None
    if isinstance(pred, And):
        lv = eval_predicate(pred.left, row)
        if lv is False:
            return False
        rv = eval_predicate(pred.right, row)
        if rv is False:
            return False
        if lv is None or rv is None:
            return None
        return True
    if isinstance(pred, Or):
        lv = eval_predicate(pred.left, row)
        if lv is True:
            return True
        rv = eval_predicate(pred.right, row)
        if rv is True:
            return True
        if lv is None or rv is None:
            return None
        return False
    raise TypeError(f"未知谓词节点: {type(pred)!r}")


def row_passes(pred: Optional[Predicate], row: Row) -> bool:
    """行是否通过筛选：仅 ``True`` 通过，UNKNOWN/False 均不通过。"""
    return eval_predicate(pred, row) is True


def walk_predicate(pred: Optional[Predicate]):
    """深度优先产出谓词树中的全部节点。"""
    if pred is None:
        return
    yield pred
    if isinstance(pred, (And, Or)):
        yield from walk_predicate(pred.left)
        yield from walk_predicate(pred.right)


def referenced_columns(pred: Optional[Predicate]) -> frozenset[str]:
    """谓词引用到的全部列名。"""
    cols: set[str] = set()
    for node in walk_predicate(pred):
        if isinstance(node, (Cmp, IsNull, IsNotNull)):
            cols.add(node.column)
    return frozenset(cols)
