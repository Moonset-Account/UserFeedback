"""端到端演示：题面要求的两个关键场景。

运行方式（在 workspace 根目录）：
    python3 -I examples/demo.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from temporal_fk import TemporalForeignKeyError, TemporalStore  # noqa: E402


def seed():
    store = TemporalStore()
    txn = store.begin()
    parent_id = txn.insert_parent("k", 0, 10)
    child_id = txn.insert_child("k", 0, 10)
    txn.commit()
    return store, parent_id, child_id


def scenario_same_transaction_split():
    print("== 场景 1：同事务删旧父区间 -> 插入相邻小段 -> 延长子区间 ==")
    store, parent_id, child_id = seed()
    txn = store.begin()
    txn.delete_parent(parent_id)
    txn.insert_parent("k", 0, 4)
    txn.insert_parent("k", 4, 8)
    txn.insert_parent("k", 8, 12)
    txn.update_child(child_id, end=12)
    txn.commit()
    print("提交成功")
    print("  父区间并集:", sorted(store.parent_intervals("k")))
    print("  子区间    :", store.child_intervals("k"))
    print()


def scenario_shrink_reports_missing():
    print("== 场景 2：缩减父区间使已有子记录失去覆盖，返回缺失子区间并拒绝 ==")
    store, parent_id, child_id = seed()
    txn = store.begin()
    txn.update_parent(parent_id, end=6)
    try:
        txn.commit()
    except TemporalForeignKeyError as exc:
        gap = exc.gaps[0]
        print("事务被拒绝:", exc)
        print(f"  子行 id={gap.child_row_id} 实际缺失区间: {list(gap.missing)}")
    print("  已确认父区间保持:", store.get_parent(parent_id).interval)
    print("  已确认子区间保持:", store.get_child(child_id).interval)
    print()


def scenario_interleaved():
    print("== 场景 3：两笔事务交错（缩短父 / 延长子），最终不留未覆盖子记录 ==")
    store, parent_id, child_id = seed()
    shrink = store.begin()
    extend = store.begin()
    shrink.update_parent(parent_id, end=6)
    extend.update_child(child_id, end=12)

    for name, txn in (("缩短父区间", shrink), ("延长子区间", extend)):
        try:
            txn.commit()
            print(f"  {name}事务: 提交成功")
        except TemporalForeignKeyError as exc:
            print(f"  {name}事务: 被拒绝，缺失 {list(exc.gaps[0].missing)}")

    print("  两次拒绝后已确认状态:")
    print("   父:", store.parent_intervals("k"), " 子:", store.child_intervals("k"))

    retry = store.begin()
    retry.delete_parent(parent_id)
    retry.insert_parent("k", 0, 6)
    retry.insert_parent("k", 6, 10)
    retry.insert_parent("k", 10, 12)
    retry.update_child(child_id, end=12)
    retry.commit()
    print("  重试事务（拆段补齐 + 延长子）提交成功")
    print("   父:", sorted(store.parent_intervals("k")), " 子:", store.child_intervals("k"))


if __name__ == "__main__":
    scenario_same_transaction_split()
    scenario_shrink_reports_missing()
    scenario_interleaved()
