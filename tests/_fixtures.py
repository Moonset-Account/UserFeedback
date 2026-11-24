"""测试用本地合成行集与查询构造辅助。"""

import unittest

from querybind import Catalog, TableData
from querybind.model import (
    ColumnRef,
    DerivedTable,
    FuncCall,
    Join,
    Literal,
    SelectNode,
    Star,
    SubqueryExpr,
    TableRef,
    and_,
    col,
    eq,
    lit,
    star,
    subq,
)


def build_catalog():
    return Catalog([
        TableData(
            "dept",
            ["id", "name", "loc"],
            [
                {"id": 1, "name": "Eng", "loc": "BJ"},
                {"id": 2, "name": "Sales", "loc": "SH"},
                {"id": 3, "name": "HR", "loc": "SZ"},
            ],
            unique_keys=[("id",)],
        ),
        TableData(
            "emp",
            ["id", "dept_id", "name", "salary"],
            [
                {"id": 10, "dept_id": 1, "name": "Ann", "salary": 100},
                {"id": 11, "dept_id": 1, "name": "Bob", "salary": 200},
                {"id": 12, "dept_id": 2, "name": "Cyd", "salary": 300},
                {"id": 13, "dept_id": 4, "name": "Dan", "salary": 400},
            ],
            unique_keys=[("id",)],
        ),
        TableData(
            "pay",
            ["emp_id", "bonus"],
            [
                {"emp_id": 10, "bonus": 5},
                {"emp_id": 12, "bonus": 7},
            ],
            unique_keys=[("emp_id",)],
        ),
        TableData(
            "pay_dup",
            ["emp_id", "bonus"],
            [
                {"emp_id": 10, "bonus": 5},
                {"emp_id": 10, "bonus": 9},
            ],
            unique_keys=[],
        ),
    ])


class QueryBindTestCase(unittest.TestCase):
    def setUp(self):
        self.catalog = build_catalog()
