"""可运行示例：重复最小值、全空聚合列、负数。

运行::

    python3 demo.py

脚本只做打印，不做断言；对应的断言在 tests/test_worked_examples.py。
"""

from grouping_sets import GroupingSetsEngine, GroupingSetsSpec, format_rows


def spec():
    return GroupingSetsSpec(
        grouping_columns=["grp"],
        grouping_sets=[["grp"], []],
        aggregates=[
            ("count", "*", "n"),
            ("count", "v", "n_v"),
            ("sum", "v", "total"),
            ("min", "v", "lowest"),
        ],
    )


def show(title, engine):
    print(title)
    print(format_rows(engine.spec, engine.current_rows()))
    print()


def repeated_minimum():
    engine = GroupingSetsEngine(spec())
    engine.apply(
        adds=[
            {"grp": "g", "v": 4},
            {"grp": "g", "v": 1},
            {"grp": "g", "v": 1},
            {"grp": "g", "v": 7},
        ]
    )
    show("[重复最小值] v = [4, 1, 1, 7]", engine)

    batch = engine.apply(retracts=[{"grp": "g", "v": 1}])
    print("撤掉一个 1 -> 本批 changed =", batch.changed)
    show("[重复最小值] 撤掉一个 1 后 v = [4, 1, 7]", engine)

    batch = engine.apply(retracts=[{"grp": "g", "v": 1}])
    print("再撤掉最后一个 1 -> 本批 changed =", batch.changed)
    show("[重复最小值] 再撤掉一个 1 后 v = [4, 7]，最小值上移到 4", engine)


def all_null_column():
    engine = GroupingSetsEngine(spec())
    engine.apply(
        adds=[
            {"grp": "g", "v": None},
            {"grp": "g", "v": None},
            {"grp": "g", "v": None},
        ]
    )
    show("[全空聚合列] v = [None, None, None]: COUNT(*)=3, COUNT(v)=0, SUM/MIN 为空", engine)

    engine.apply(adds=[{"grp": "g", "v": 8}])
    show("[全空聚合列] 加入 v=8 后", engine)

    engine.apply(retracts=[{"grp": "g", "v": 8}])
    show("[全空聚合列] 再撤掉 v=8，SUM/MIN 回到空", engine)


def negative_numbers():
    engine = GroupingSetsEngine(spec())
    engine.apply(
        adds=[
            {"grp": "g", "v": -5},
            {"grp": "g", "v": -2},
            {"grp": "g", "v": -10},
        ]
    )
    show("[负数] v = [-5, -2, -10]: sum=-17, min=-10", engine)

    engine.apply(retracts=[{"grp": "g", "v": -10}])
    show("[负数] 撤掉 -10 后 v = [-5, -2]: sum=-7, min=-5", engine)

    engine.apply(adds=[{"grp": "g", "v": -3}])
    show("[负数] 加入 -3 后 v = [-5, -2, -3]: sum=-10, min=-5", engine)


def duplicate_sets_and_bitmap():
    spec_dup = GroupingSetsSpec(
        grouping_columns=["region", "product"],
        grouping_sets=[["region", "product"], ["region"], [], ["region"]],
        aggregates=[("count", "*", "n"), ("min", "amount", "lowest")],
    )
    engine = GroupingSetsEngine(spec_dup)
    engine.apply(
        adds=[
            {"region": "n", "product": None, "amount": 5},
            {"region": "n", "product": "a", "amount": 2},
        ]
    )
    show(
        "[重复分组集合与位图] 集合 0 与集合 3 都是 [region]，各一份结果；\n"
        "  集合 0 第一行的 product=None 且位图末位为 0 -> 数据 NULL；\n"
        "  集合 1 的 product=None 且位图末位为 1 -> 省略列的小计空值；\n"
        "  两者在 product 列上都是 None，只能靠位图区分。",
        engine,
    )
    engine = GroupingSetsEngine(spec_dup)
    engine.apply(adds=[{"region": "n", "product": "a", "amount": 2}])
    print("一次撤回只对每个分组集合状态扣一次（4 个状态各剩 0 行）：")
    engine.apply(retracts=[{"region": "n", "product": "a", "amount": 2}])
    print("  stored_row_counts =", engine.stored_row_counts(), "\n")


if __name__ == "__main__":
    repeated_minimum()
    all_null_column()
    negative_numbers()
    duplicate_sets_and_bitmap()
