"""端到端冒烟示例：合成数据跑通“暂停 / 调组 / 断点恢复”全流程。

运行方式（在仓库根目录）：

    python3 -I examples/smoke_demo.py

该脚本不依赖任何第三方库，也不访问网络。
"""

from __future__ import annotations

import json
import os
import sys

# 在 -I 隔离模式下当前目录不会自动进入 sys.path，按脚本位置自举仓库根目录。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from param_scheduler import Scheduler  # noqa: E402


def main() -> None:
    sched = Scheduler(
        {
            "backbone": [1.0, 0.5, 0.25],
            "head": lambda n: 0.9**n,
        }
    )
    sched.add_param("embed.weight", "backbone")
    sched.add_param("head.weight", "head", frozen=True)  # 先暂停，从未更新

    r1 = sched.confirm_step(1)
    print("step 1:", r1.learning_rates, "->", r1.counts_after)
    r2 = sched.confirm_step(2)
    print("step 2:", r2.learning_rates, "->", r2.counts_after)

    # 调组后、确认前“断点”：embed 已属于 head 组，第 3 步尚未确认。
    sched.move_param("embed.weight", "head")
    # head.weight 第一次解冻：从未更新，应从 schedule(0)=1.0 起步。
    sched.unfreeze_param("head.weight")

    blob = json.dumps(sched.state_dict(), indent=2)
    restored = Scheduler.from_state(
        json.loads(blob), schedules={"head": lambda n: 0.9**n}
    )
    plan = restored.plan_step()
    print("restored plan step 3:", plan.learning_rates)
    # embed 计数 2 带进 head 组 -> 0.9^2；head.weight 计数 0 -> 0.9^0。
    assert plan.learning_rates == {
        "embed.weight": 0.81,
        "head.weight": 1.0,
    }, plan.learning_rates

    r3 = restored.confirm_step(3)
    print("step 3:", r3.learning_rates, "->", r3.counts_after)
    assert r3.counts_after == {"embed.weight": 3, "head.weight": 1}

    # 重复确认幂等。
    assert restored.confirm_step(3) is r3
    print("OK: smoke demo passed")


if __name__ == "__main__":
    main()
