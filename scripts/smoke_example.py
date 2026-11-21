"""人工冒烟脚本：用一个相互引用的两服务例子演示公开 API 的真实行为。

运行（在工作区根目录）：
    python3 -I scripts/smoke_example.py
脚本通过自身位置定位工作区根并加入 sys.path，因此在 -I 隔离模式下也可运行。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dr_planner import Backup, Service, plan_recovery  # noqa: E402


def main() -> None:
    orders = Service(
        "orders",
        weight=3,
        backups=(
            Backup("o-old", 100, 200, frozenset((("inventory", 80),))),
            Backup("o-new", 120, 600, frozenset((("inventory", 110),))),
        ),
    )
    inventory = Service(
        "inventory",
        weight=2,
        backups=(
            Backup("i-old", 80, 150),
            Backup("i-new", 110, 500),
        ),
    )

    for budget in (349, 500, 700, 1100):
        result = plan_recovery([orders, inventory], budget)
        selected = (
            None
            if result.selected is None
            else [backup.backup_id for backup in result.selected]
        )
        print(
            f"budget={budget:>5} | feasible={result.feasible!s:<5} | "
            f"selected={selected} | bytes={result.total_bytes} | "
            f"loss={result.loss} | budget_can_fix={result.budget_can_fix} | "
            f"required_budget={result.required_budget} | reason={result.reason}"
        )


if __name__ == "__main__":
    main()
