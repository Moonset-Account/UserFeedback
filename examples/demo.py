"""端到端示例：三入三出开关排程、回放与读后重排。

直接运行（在工作区根目录）：
    python3 -I examples/demo.py
所有输出均由真实求解/回放产生，不预置结果。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from switch_scheduler import (
    Problem,
    lower_bounds,
    replay,
    replan,
    solve,
)
from switch_scheduler.model import InfeasibleError


def show(title, schedule):
    print(f"\n=== {title}：{schedule.num_beats} 拍，变更 {schedule.total_changes} 次 ===")
    for r in schedule.records:
        print(
            f"拍 {r.beat}: 目标匹配={r.matching} 变更端口={list(r.changed_ports)} "
            f"发数={[list(e) for e in r.sends]} 留空={[list(e) for e in r.idle_edges]}"
        )


def main():
    # 两个置换叠加：每行每列负载均为 2
    demand = [
        [1, 1, 0],
        [0, 1, 1],
        [1, 0, 1],
    ]
    print("需求矩阵：")
    for row in demand:
        print(" ", row)

    problem = Problem.build(demand)
    lb = lower_bounds(problem)
    print("\n行列最大负载下界 =", lb["load_bound"], "（注意：真实最优拍数更大）")

    schedule = solve(problem)
    show("冷启动最优排程", schedule)
    print(">> 最优拍数", schedule.num_beats, "> 下界", lb["load_bound"],
          "：重配延迟使负载下界不可达")

    # 上帧保留第一个置换连线
    warm = Problem.build(demand, previous_matching=(0, 1, 2))
    show("热启动（保留连线）最优排程", solve(warm))

    # 回放设备指令，独立核对是否精确清空
    result = replay(problem, schedule.matchings())
    print("\n回放完成矩阵：")
    for row in result.completed:
        print(" ", list(row))
    print("是否精确清空：", result.cleared, "| 首个错过需求：", result.first_missed)

    # 不可行窗口：需求 2 但窗口只开 1 拍（静态即可判定，build 阶段抛出）
    try:
        Problem.build([[2, 0]], windows={(0, 0): (0, 0)})
    except InfeasibleError as err:
        print("\n不可行示例：", err)
        print("首个错过需求 ->", err.missed)

    # 读后重排：冻结第 0 拍，设备回报零完成，从真实连线重排剩余业务
    frozen, suffix = replan(
        problem,
        [schedule.records[0].matching],
        actual_completion=[[0, 0, 0], [0, 0, 0], [0, 0, 0]],
    )
    show("重排后缀（绝对拍号）", suffix)
    combined = [r.matching for r in frozen.records] + [
        r.matching for r in suffix.records
    ]
    print("前缀+后缀拼接回放是否清空：", replay(problem, combined).cleared)


if __name__ == "__main__":
    main()
