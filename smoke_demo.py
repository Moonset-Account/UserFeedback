"""端到端冒烟演示：乱序上报 + 重试拆分 + 缺口等待与补齐。

运行（隔离模式，不加载站点包与当前目录以外的模块）：
    python3 -I smoke_demo.py
"""

from pipeline_collect_merge import (
    Collector,
    Interval,
    Replacement,
    ShardAttempt,
)
from pipeline_collect_merge.transforms import LabeledTransform


def main() -> None:
    collector = Collector()

    # 乱序上报两个根分片：B 先到、A 后到。
    collector.report_attempt(
        ShardAttempt("B", 0, Interval(6, 12), LabeledTransform("B0"), True)
    )
    collector.report_attempt(
        ShardAttempt("A", 0, Interval(0, 6), LabeledTransform("A0"), True)
    )

    # A 的重试把 [0,6) 拆成两段，先只到第一段 -> 等待缺片段。
    collector.report_replacement(
        Replacement("A", 0, [("A1", 1), ("A2", 1)])
    )
    collector.report_attempt(
        ShardAttempt("A1", 1, Interval(0, 3), LabeledTransform("a1"), True)
    )
    waiting = collector.merge()
    print(
        "step1 status:",
        waiting.status,
        "missing:",
        [(iv.lo, iv.hi) for iv in waiting.missing],
    )
    assert waiting.status == "waiting"
    assert waiting.missing == [Interval(3, 6)]

    # 第二段到齐 -> 替换启用，按区间顺序输出完整变换。
    collector.report_attempt(
        ShardAttempt("A2", 1, Interval(3, 6), LabeledTransform("a2"), True)
    )
    ready = collector.merge()
    print("step2 status:", ready.status)
    print("used_segments:", ready.used_segments)
    print("full_transform([]):", ready.full_transform([]))
    assert ready.status == "ready"
    assert ready.used_segments == [("A1", 1), ("A2", 1), ("B", 0)]
    assert ready.full_transform([]) == ["a1", "a2", "B0"]
    print("smoke OK")


if __name__ == "__main__":
    main()
