"""RPA 规划器：把类型化矩阵翻译成逐格执行计划。

规划阶段只依赖采集结果与网格配置，不启动浏览器：
1. 列定位：以「可见列标题」为唯一锚点。隐藏列不在可见标题集合中，
   其所有数据格直接拒绝（rejected，原因：列不可见，无法按可见标题定位）。
2. 只读识别：网格配置声明的只读格拒绝写入，并给出明确解释；
   其他格的成功不得掩盖这些失败。
3. 输入策略：
   - missing：跳过（目标格保持未写入状态，回读时应仍为缺失）；
   - 含制表符或换行的文本：模态多行编辑器 + 真实粘贴；
   - 危险公式样式：先把格设为 text 格式再逐格键盘输入，防止被解释为公式；
     若目标控件无法保证文本保真，则拒绝该格（浏览器侧二次确认）；
   - 其余：逐格键盘输入（真实按键，不是 JS 直接赋值）。
   - empty：聚焦后清空（真实 Delete/Backspace 路径）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Set, Tuple

from .matrix import ExperimentMatrix, GridConfig
from .value_model import TypedValue, ValueKind, is_dangerous_formula


class Strategy(str, Enum):
    SKIP_MISSING = "skip_missing"        # 缺失值：不触碰目标格
    CLEAR_EMPTY = "clear_empty"          # 空字符串：聚焦清空
    TYPE_CELL = "type_cell"              # 逐格键盘输入
    PASTE_MULTILINE = "paste_multiline"  # 模态编辑器真实粘贴（含 \\t/\\n/\\r）


class PlanStatus(str, Enum):
    PLANNED = "planned"      # 可执行
    REJECTED = "rejected"    # 保真无法保证，拒绝该格


@dataclass
class CellPlan:
    row_index: int
    column_key: str
    kind: str
    raw_text: Optional[str]
    status: PlanStatus
    strategy: Optional[Strategy]
    target_format: Optional[str]   # text / number，输入前先设置的格格式
    reason: Optional[str] = None
    dangerous: bool = False

    def to_dict(self) -> Dict:
        return {
            "row_index": self.row_index,
            "column_key": self.column_key,
            "kind": self.kind,
            "raw_text": self.raw_text,
            "status": self.status.value,
            "strategy": self.strategy.value if self.strategy else None,
            "target_format": self.target_format,
            "reason": self.reason,
            "dangerous": self.dangerous,
        }


@dataclass
class ExecutionPlan:
    scenario: str
    visible_columns: List[str]
    hidden_columns: List[str]
    cell_plans: List[CellPlan] = field(default_factory=list)

    @property
    def writable_plans(self) -> List[CellPlan]:
        return [p for p in self.cell_plans if p.status is PlanStatus.PLANNED]

    @property
    def rejected_plans(self) -> List[CellPlan]:
        return [p for p in self.cell_plans if p.status is PlanStatus.REJECTED]

    def summary(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for p in self.cell_plans:
            key = f"{p.status.value}"
            out[key] = out.get(key, 0) + 1
        return out


def plan_execution(matrix: ExperimentMatrix,
                   grid: GridConfig) -> ExecutionPlan:
    hidden: Set[str] = set(grid.hidden_columns)
    readonly: Set[Tuple[int, str]] = {
        (c["row"], c["column"]) for c in grid.readonly_cells
    }
    visible = [c.key for c in matrix.columns if c.key not in hidden]

    ep = ExecutionPlan(
        scenario=matrix.scenario,
        visible_columns=visible,
        hidden_columns=list(hidden),
    )

    for r_idx, cell in matrix.walk_cells():
        key = cell.column.key
        val: TypedValue = cell.value
        base = dict(row_index=r_idx, column_key=key, kind=val.kind.value,
                    raw_text=val.raw_text)

        # 1) 隐藏列：无法按可见标题定位 -> 拒绝
        if key in hidden:
            ep.cell_plans.append(CellPlan(
                **base, status=PlanStatus.REJECTED, strategy=None,
                target_format=None,
                reason=f"列「{key}」在网格中被隐藏，自动化只能按可见列标题定位，"
                       f"无法保证写入与回读，拒绝该格",
            ))
            continue

        # 2) 只读格：拒绝并解释
        if (r_idx, key) in readonly:
            ep.cell_plans.append(CellPlan(
                **base, status=PlanStatus.REJECTED, strategy=None,
                target_format=None,
                reason=f"第 {r_idx + 1} 数据行的「{key}」为只读格，"
                       f"控件拒绝写入；为避免静默丢数据，本格标记失败",
            ))
            continue

        # 3) 缺失：跳过，不触碰目标格
        if val.kind is ValueKind.MISSING:
            ep.cell_plans.append(CellPlan(
                **base, status=PlanStatus.PLANNED,
                strategy=Strategy.SKIP_MISSING, target_format=None,
                reason="缺失值：保持目标格未写入状态",
            ))
            continue

        # 4) 空字符串：聚焦清空
        if val.kind is ValueKind.EMPTY:
            ep.cell_plans.append(CellPlan(
                **base, status=PlanStatus.PLANNED,
                strategy=Strategy.CLEAR_EMPTY, target_format="text",
            ))
            continue

        dangerous = is_dangerous_formula(val.raw_text or "")
        has_special = any(ch in (val.raw_text or "") for ch in "\t\n\r")

        if has_special:
            strategy = Strategy.PASTE_MULTILINE
        else:
            strategy = Strategy.TYPE_CELL

        if val.kind is ValueKind.DECIMAL:
            target_format = "number"
        else:
            target_format = "text"  # id_text / date_text / text 一律文本格式承载

        reason = None
        if dangerous:
            reason = ("公式样式字符串：先锁定文本格式再输入，"
                      "控件若无法保真则浏览器侧拒绝并回传失败")

        ep.cell_plans.append(CellPlan(
            **base, status=PlanStatus.PLANNED, strategy=strategy,
            target_format=target_format, reason=reason, dangerous=dangerous,
        ))

    return ep
