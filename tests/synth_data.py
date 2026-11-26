"""测试与演示共用的本地合成数据（无任何外部依赖/网络访问）。

表 ``sales`` 按区域分三个分区，刻意覆盖：
* 缺失值（region/amount/cust_id 上的 NULL）；
* 跨分区重复的 cust_id（p1 与 p3 都有 c1，用于验证非互斥去重不能直接相加）；
* 空分区 p4；
* 字符串、整数、浮点、布尔四种类型族，以及 bool/int 不可混比的场景。
"""

from __future__ import annotations

from sharedscan.model import Catalog, Table, make_catalog

SCHEMA = {
    "region": "str",
    "amount": "int",
    "qty": "float",
    "cust_id": "str",
    "flag": "bool",
}

P1 = [
    {"region": "east", "amount": 10, "qty": 1.5, "cust_id": "c1", "flag": True},
    {"region": "east", "amount": 20, "qty": 2.5, "cust_id": "c2", "flag": False},
    {"region": "west", "amount": None, "qty": 3.0, "cust_id": "c1", "flag": True},
    {"region": None, "amount": 7, "qty": 0.5, "cust_id": None, "flag": None},
]

P2 = [
    {"region": "west", "amount": 5, "qty": 1.0, "cust_id": "c3", "flag": True},
    {"region": "west", "amount": 15, "qty": 2.0, "cust_id": "c3", "flag": False},
    {"region": "north", "amount": 30, "qty": 4.0, "cust_id": None, "flag": True},
]

P3 = [
    {"region": "east", "amount": 40, "qty": 5.0, "cust_id": "c1", "flag": False},
    {"region": "north", "amount": None, "qty": 6.0, "cust_id": "c4", "flag": True},
    {"region": "east", "amount": 10, "qty": 1.5, "cust_id": "c5", "flag": None},
]

P4: list[dict] = []


def build_catalog(*, exclusive_cust: bool) -> Catalog:
    """构造目录。

    ``exclusive_cust=True`` 时把 (sales, cust_id) 标示为跨分区互斥；
    默认数据中 p1/p3 共享 c1，因此该标示仅用于专门的互斥测试。
    """
    table = Table(
        name="sales",
        schema=dict(SCHEMA),
        partitions={"p1": list(P1), "p2": list(P2), "p3": list(P3), "p4": list(P4)},
    )
    exclusive = {("sales", "cust_id")} if exclusive_cust else set()
    return make_catalog({"sales": table}, exclusive_distinct=exclusive)
