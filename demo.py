"""序列分配器综合演示（全部使用本地合成数据，无网络、无外部依赖）。

运行：python3 demo.py

演示内容：
1. 两个会话交错取号 + 管理员改步长 + 显式指定下一值；
2. 反向序列改步长后到达界限；
3. 绕回序列在当前号段用完/释放前等待，之后才绕回；
4. 预留状态保存/恢复，进程退出遗留号码形成空缺。
"""

from __future__ import annotations

import os
import tempfile

from sequence_allocator import (
    SequenceCatalog,
    SequenceExhaustedError,
)


def part1_interleaving_and_admin_changes() -> None:
    print("=" * 72)
    print("演示 1：两个会话交错取号，管理员改步长 / 显式指定下一值")
    print("=" * 72)
    catalog = SequenceCatalog()
    catalog.create_sequence(
        name="order_id", start_value=1, increment_by=1,
        min_value=1, max_value=100, cache_size=3, cycle=False,
    )
    s1 = catalog.open_session("order_id")
    s2 = catalog.open_session("order_id")

    print("初始步长=1，每会话预留 3 个号：")
    print(f"  会话A nextval -> {s1.nextval()}   # A 预留 1..3")
    print(f"  会话B nextval -> {s2.nextval()}   # B 预留 4..6")
    print(f"  会话A nextval -> {s1.nextval()}")
    print(f"  会话B nextval -> {s2.nextval()}")

    catalog.alter_increment("order_id", 5)
    state = catalog.get_state("order_id")
    print(
        "\n管理员把步长改为 5：A 手里剩余的 3、B 手里剩余的 6 被收回"
        f"（watermark 保持 {state.watermark}，版本升到 v{state.version}）"
    )
    print(f"  会话A nextval -> {s1.nextval()}   # 从旧边界 {state.watermark} 沿 +5 继续，A 预留 11,16,21")
    print(f"  会话B nextval -> {s2.nextval()}   # B 预留 26,31,36")
    print(f"  会话A nextval -> {s1.nextval()}")
    print(f"  会话B nextval -> {s2.nextval()}")
    print("  注意：7..10 随收回的号段永久空缺。")

    catalog.restart_with("order_id", 100)
    state = catalog.get_state("order_id")
    print(
        f"\n管理员显式指定下一值为 100：未消费号段再次被收回"
        f"（版本升到 v{state.version}）"
    )
    print(f"  会话A nextval -> {s1.nextval()}   # 步长 5、上限 100，A 只拿到短批 [100]")
    try:
        s2.nextval()
    except SequenceExhaustedError as exc:
        print(f"  会话B 再取号已到界 -> {exc}")
    state = catalog.get_state("order_id")
    print(
        f"  最终 watermark={state.watermark}，版本=v{state.version}，"
        f"累计收回号段={state.reclaimed_batches} 个"
    )
    s1.close()
    s2.close()


def part2_reverse_sequence() -> None:
    print("\n" + "=" * 72)
    print("演示 2：反向序列，改步长后到达 min_value 结束")
    print("=" * 72)
    catalog = SequenceCatalog()
    catalog.create_sequence(
        name="down_seq", start_value=10, increment_by=-2,
        min_value=0, max_value=10, cache_size=3, cycle=False,
    )
    s3 = catalog.open_session("down_seq")
    s4 = catalog.open_session("down_seq")
    print(f"  会话C nextval -> {s3.nextval()}   # C 预留 10,8,6")
    print(f"  会话D nextval -> {s4.nextval()}   # D 预留 4,2,0")

    catalog.alter_increment("down_seq", -3)
    print("管理员把步长改为 -3：两个会话的剩余号段被收回，watermark=0")
    try:
        s3.nextval()
    except SequenceExhaustedError as exc:
        print(f"  会话C nextval 触发结束：{exc}")
    s3.close()
    s4.close()


def part3_cycle_waits_for_ranges() -> None:
    print("\n" + "=" * 72)
    print("演示 3：绕回前等当前号段用完或释放")
    print("=" * 72)

    print("场景 A：到界时仍有会话持有号段，先等待，用完后再绕回")
    catalog = SequenceCatalog()
    catalog.create_sequence(
        name="cyclic_a", start_value=1, increment_by=1,
        min_value=1, max_value=6, cache_size=3, cycle=True,
    )
    e = catalog.open_session("cyclic_a")
    f = catalog.open_session("cyclic_a")
    print(f"  会话E nextval -> {e.nextval()}   # E 预留 1,2,3")
    print(f"  会话F nextval -> {f.nextval()}   # F 预留 4,5,6")
    print(f"  会话F nextval -> {f.nextval()}")
    print(f"  会话F nextval -> {f.nextval()}   # F 缓存耗尽，[4,6] 释放")
    print(f"  会话E nextval -> {e.nextval()}   # E 发缓存值 2；F 抢走短批 [6]，watermark 到界")
    try:
        f.nextval()  # F 想绕回，但 E 仍持有 [1,3]
    except SequenceExhaustedError as exc:
        print(f"  会话F 想绕回但号段未释放 -> {exc}")
    print(f"  会话E nextval -> {e.nextval()}   # E 发 3，[1,3] 释放，绕回通道打开")
    print(f"  会话E nextval -> {e.nextval()}   # E 绕回，新批 1,2,3")
    print(f"  会话F nextval -> {f.nextval()}   # F 绕回，拿到 4,5,6")
    print(f"  会话E nextval -> {e.nextval()}")
    print(f"  会话E nextval -> {e.nextval()}   # E 用完 [1,3] 并释放")
    try:
        e.nextval()  # 轮到 E 撞界，F 还持有 [4,6]
    except SequenceExhaustedError as exc:
        print(f"  会话E 再预留想绕回，但 F 仍持有号段 -> {exc}")
    print(f"  会话F nextval -> {f.nextval()}")
    print(f"  会话F nextval -> {f.nextval()}   # F 用完释放")
    print(f"  会话E nextval -> {e.nextval()}   # 无号段持有，E 再次绕回得到 1")
    e.close()
    f.close()

    print("\n场景 B：会话显式 close 释放号段，另一会话立即可以绕回")
    catalog2 = SequenceCatalog()
    catalog2.create_sequence(
        name="cyclic_b", start_value=1, increment_by=1,
        min_value=1, max_value=4, cache_size=3, cycle=True,
    )
    g = catalog2.open_session("cyclic_b")
    h = catalog2.open_session("cyclic_b")
    print(f"  会话G nextval -> {g.nextval()}   # G 预留 1,2,3")
    print(f"  会话H nextval -> {h.nextval()}   # H 预留短批 [4]，watermark 到界")
    print(f"  会话G nextval -> {g.nextval()}   # 缓存命中，发 2，不会触发绕回")
    h.close()  # 释放 [4]
    print("  会话H close，号段 [4] 被释放")
    print(f"  会话G nextval -> {g.nextval()}   # 缓存最后一个值 3")
    print(f"  会话G nextval -> {g.nextval()}   # 无号段持有，顺利绕回得到 1")
    g.close()


def part4_persistence_and_gaps() -> None:
    print("\n" + "=" * 72)
    print("演示 4：预留状态保存/恢复，进程退出遗留号码形成空缺")
    print("=" * 72)
    state_path = os.path.join(tempfile.gettempdir(), "seq_allocator_demo.json")

    catalog = SequenceCatalog()
    catalog.create_sequence(
        name="persisted", start_value=1, increment_by=1,
        min_value=1, max_value=1_000_000, cache_size=10, cycle=False,
    )
    session = catalog.open_session("persisted")
    first = session.nextval()
    print(f"  旧进程：会话预留 1..10，只消费了 {first}，随后保存状态并退出")
    catalog.save(state_path)
    print(f"  状态文件：{state_path}")
    del catalog, session  # 模拟进程退出，会话内 2..10 丢失

    restored = SequenceCatalog.load(state_path)
    new_session = restored.open_session("persisted")
    nxt = new_session.nextval()
    print(f"  新进程：恢复 watermark=10，新会话第一个值 -> {nxt}（2..10 成为空缺）")
    new_session.close()


if __name__ == "__main__":
    part1_interleaving_and_admin_changes()
    part2_reverse_sequence()
    part3_cycle_waits_for_ranges()
    part4_persistence_and_gaps()
