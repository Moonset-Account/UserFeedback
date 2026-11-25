"""参数组学习率调度器的实现。

设计要点
========

* 参数身份：由调用方提供稳定的 ``param_id``（推荐使用字符串，例如参数名），
  使得状态可以落盘并在恢复后保持身份一致。
* 有效更新次数：仅当参数处于非暂停状态、且所在步被 :meth:`Scheduler.confirm_step`
  确认完成时才加 1。暂停中的参数不计步，其计数被保留。
* 学习率安排：每个组持有一个 *schedule*，既可以是 ``update_count -> lr`` 的可调用
  对象，也可以是按顺序取值的序列（序列耗尽后停在最后一个值）。第 ``n`` 次有效
  更新对应的计划学习率为 ``schedule(n)``，即从未更新的参数从 ``schedule(0)`` 开始。
* 确认语义：步从 1 开始连续编号，:meth:`Scheduler.confirm_step` 推进游标、为当时
  未暂停的参数计数加 1，并把结果缓存；重复确认同一步直接返回缓存结果，不重复计数。
* 恢复语义：:meth:`Scheduler.state_dict` / :meth:`Scheduler.load_state_dict` 保存
  分组、参数计数、暂停状态与已确认步结果。若调组发生在确认之前而进程中断，恢复后
  参数已属于新组，下一步的计划自然按它在新组中已有的计数取学习率。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Union

__all__ = [
    "Schedule",
    "Scheduler",
    "SchedulerError",
    "StepPlan",
    "StepResult",
]

#: 学习率安排：可调用对象（按有效更新次数取值）或值序列。
Schedule = Union[Callable[[int], float], Sequence[float]]

_STATE_VERSION = 1


class SchedulerError(RuntimeError):
    """调度器所有可预期错误的基类。"""


@dataclass(frozen=True)
class StepPlan:
    """某个待执行步的计划。

    :ivar step: 步编号（从 1 开始）。
    :ivar learning_rates: 当前未暂停参数的计划学习率，按参数 ID 给出。
    :ivar frozen: 当前处于暂停状态、本步不会更新的参数 ID。
    """

    step: int
    learning_rates: Dict[Any, float]
    frozen: tuple

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step": self.step,
            "learning_rates": dict(self.learning_rates),
            "frozen": list(self.frozen),
        }


@dataclass(frozen=True)
class StepResult:
    """某个已确认步的结果（重复确认时返回同一份内容）。

    :ivar step: 步编号。
    :ivar updated: 本步实际发生有效更新的参数 ID。
    :ivar learning_rates: 这些参数在本步使用的学习率（更新前取值）。
    :ivar counts_after: 确认后各参数的累计有效更新次数。
    :ivar frozen: 本步处于暂停状态的参数 ID。
    :ivar next_plan: 计数更新后为下一个步生成的计划。
    """

    step: int
    updated: tuple
    learning_rates: Dict[Any, float]
    counts_after: Dict[Any, int]
    frozen: tuple
    next_plan: StepPlan

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step": self.step,
            "updated": list(self.updated),
            "learning_rates": dict(self.learning_rates),
            "counts_after": dict(self.counts_after),
            "frozen": list(self.frozen),
            "next_plan": self.next_plan.to_dict(),
        }


@dataclass
class _ParamState:
    """参数的内部状态。"""

    param_id: Any
    group: str
    update_count: int = 0
    frozen: bool = False


@dataclass
class _GroupSpec:
    """学习率组的内部定义。"""

    name: str
    kind: str  # "callable" | "sequence"
    func: Optional[Callable[[int], float]] = None
    values: Optional[List[float]] = None

    def lr_at(self, update_count: int) -> float:
        if self.kind == "callable":
            return float(self.func(update_count))  # type: ignore[misc]
        values = self._ensure_values()
        index = min(update_count, len(values) - 1)
        return float(values[index])

    def _ensure_values(self) -> List[float]:
        if not self.values:
            raise SchedulerError(f"组 {self.name!r} 的学习率序列为空")
        return self.values

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "values": self.values}


def _build_spec(name: str, schedule: Schedule) -> _GroupSpec:
    if callable(schedule):
        return _GroupSpec(name=name, kind="callable", func=schedule)
    if isinstance(schedule, Sequence) and not isinstance(schedule, (str, bytes)):
        values = [float(v) for v in schedule]
        if not values:
            raise SchedulerError(f"组 {name!r} 的学习率序列不能为空")
        return _GroupSpec(name=name, kind="sequence", values=values)
    raise SchedulerError(
        f"组 {name!r} 的 schedule 必须是 callable 或数值序列，"
        f"实际为 {type(schedule).__name__}"
    )


class Scheduler:
    """参数组学习率调度器。

    典型用法::

        sched = Scheduler({"backbone": [0.1, 0.05], "head": lambda n: 0.01})
        sched.add_param("layer1.weight", "backbone")
        plan = sched.plan_step()          # 第 1 步的计划
        result = sched.confirm_step(1)    # 训练完成后确认，计数 +1
        sched.freeze_param("layer1.weight")
        sched.move_param("layer1.weight", "head")
        sched.unfreeze_param("layer1.weight")

    调度器本身不执行训练，只负责“计划 / 确认 / 状态管理”。
    """

    def __init__(self, groups: Mapping[str, Schedule]):
        if not groups:
            raise SchedulerError("至少需要定义一个学习率组")
        self._groups: Dict[str, _GroupSpec] = {}
        for name, schedule in groups.items():
            self._register_group(name, schedule)
        self._params: Dict[Any, _ParamState] = {}
        self._confirmed: Dict[int, StepResult] = {}
        self._last_completed_step: int = 0

    # ------------------------------------------------------------------ #
    # 组与参数管理
    # ------------------------------------------------------------------ #
    def _register_group(self, name: str, schedule: Schedule) -> None:
        if not isinstance(name, str) or not name:
            raise SchedulerError("组名必须是非空字符串")
        if name in self._groups:
            raise SchedulerError(f"组 {name!r} 已存在")
        self._groups[name] = _build_spec(name, schedule)

    def add_group(self, name: str, schedule: Schedule) -> None:
        """动态新增一个学习率组。"""
        self._register_group(name, schedule)

    def add_param(
        self,
        param_id: Any,
        group: str,
        *,
        frozen: bool = False,
        update_count: int = 0,
    ) -> None:
        """把参数加入指定组。

        :param param_id: 参数身份，需可哈希且在调度器内唯一；推荐字符串。
        :param group: 目标学习率组。
        :param frozen: 是否加入后立即暂停。
        :param update_count: 初始有效更新次数（默认 0，即从第一步开始算）。
        """
        self._require_hashable(param_id)
        if param_id in self._params:
            raise SchedulerError(f"参数 {param_id!r} 已存在")
        if group not in self._groups:
            raise SchedulerError(f"未知学习率组 {group!r}")
        if update_count < 0:
            raise SchedulerError("update_count 不能为负数")
        self._params[param_id] = _ParamState(
            param_id=param_id,
            group=group,
            update_count=int(update_count),
            frozen=bool(frozen),
        )

    def freeze_param(self, param_id: Any) -> None:
        """暂停参数：后续步不计更新，计数保留。幂等。"""
        self._require_param(param_id).frozen = True

    def unfreeze_param(self, param_id: Any) -> None:
        """解除暂停：从未更新过的参数将从 schedule(0)（第一步）开始。幂等。"""
        self._require_param(param_id).frozen = False

    def move_param(self, param_id: Any, group: str) -> None:
        """按参数身份把它迁移到另一个学习率组，计数与暂停状态保留。"""
        if group not in self._groups:
            raise SchedulerError(f"未知学习率组 {group!r}")
        state = self._require_param(param_id)
        state.group = group

    def get_count(self, param_id: Any) -> int:
        """返回参数累计的有效更新次数。"""
        return self._require_param(param_id).update_count

    def get_group(self, param_id: Any) -> str:
        """返回参数当前所在的学习率组。"""
        return self._require_param(param_id).group

    def is_frozen(self, param_id: Any) -> bool:
        """返回参数当前是否处于暂停状态。"""
        return self._require_param(param_id).frozen

    def current_lr(self, param_id: Any) -> float:
        """按参数当前计数与所在组计算此刻的计划学习率。"""
        state = self._require_param(param_id)
        return self._groups[state.group].lr_at(state.update_count)

    @property
    def next_step(self) -> int:
        """下一个待确认的步编号（已确认步数 + 1）。"""
        return self._last_completed_step + 1

    @property
    def last_completed_step(self) -> int:
        """最近一个已确认完成的步编号，没有确认过则为 0。"""
        return self._last_completed_step

    def param_ids(self) -> List[Any]:
        """返回全部已注册参数 ID（按注册顺序）。"""
        return list(self._params.keys())

    # ------------------------------------------------------------------ #
    # 计划与确认
    # ------------------------------------------------------------------ #
    def _build_plan(self, step: int) -> StepPlan:
        learning_rates: Dict[Any, float] = {}
        frozen: List[Any] = []
        for param_id, state in self._params.items():
            if state.frozen:
                frozen.append(param_id)
            else:
                learning_rates[param_id] = self._groups[state.group].lr_at(
                    state.update_count
                )
        return StepPlan(step=step, learning_rates=learning_rates, frozen=tuple(frozen))

    def plan_step(self, step: Optional[int] = None) -> StepPlan:
        """生成某个步的计划；默认生成下一个待确认步的计划。

        计划是只读快照，反映调用时刻的分组/暂停/计数状态。
        """
        if step is None:
            step = self.next_step
        self._validate_pending_step(step)
        return self._build_plan(step)

    def confirm_step(self, step: int) -> StepResult:
        """确认某一步训练已完成。

        确认动作一次性完成三件事：

        1. 以确认时刻未暂停的参数作为本步有效更新的参数（按更新前计数取学习率）；
        2. 这些参数的有效更新次数各加 1，暂停中的参数不计步；
        3. 产出计数更新后的下一个步计划并缓存结果。

        重复确认同一步返回首次确认的缓存结果，不会重复计数。
        """
        if not isinstance(step, int) or isinstance(step, bool):
            raise SchedulerError("step 必须是整数")
        if step <= 0:
            raise SchedulerError("步编号必须从 1 开始")

        cached = self._confirmed.get(step)
        if cached is not None:
            return cached
        self._validate_pending_step(step)

        plan = self._build_plan(step)
        updated_ids = tuple(plan.learning_rates.keys())
        for param_id in updated_ids:
            self._params[param_id].update_count += 1

        counts_after = {pid: st.update_count for pid, st in self._params.items()}
        result = StepResult(
            step=step,
            updated=updated_ids,
            learning_rates=dict(plan.learning_rates),
            counts_after=counts_after,
            frozen=plan.frozen,
            next_plan=self._build_plan(step + 1),
        )
        self._confirmed[step] = result
        self._last_completed_step = step
        return result

    def _validate_pending_step(self, step: int) -> None:
        if not isinstance(step, int) or isinstance(step, bool):
            raise SchedulerError("step 必须是整数")
        if step <= 0:
            raise SchedulerError("步编号必须从 1 开始")
        if step <= self._last_completed_step:
            raise SchedulerError(
                f"步 {step} 已确认完成，请通过 get_result 查看其结果"
            )
        if step != self.next_step:
            raise SchedulerError(
                f"步编号不连续：下一个应为 {self.next_step}，收到 {step}"
            )

    def get_result(self, step: int) -> StepResult:
        """获取已确认步的结果；未确认的步抛出 :class:`SchedulerError`。"""
        try:
            return self._confirmed[step]
        except KeyError:
            raise SchedulerError(f"步 {step} 尚未确认完成") from None

    def is_confirmed(self, step: int) -> bool:
        """某一步是否已确认完成。"""
        return step in self._confirmed

    def confirmed_steps(self) -> List[int]:
        """返回全部已确认步编号（升序）。"""
        return sorted(self._confirmed.keys())

    # ------------------------------------------------------------------ #
    # 状态保存与恢复
    # ------------------------------------------------------------------ #
    def state_dict(self) -> Dict[str, Any]:
        """导出可 JSON 序列化的完整状态。

        可调用的 schedule 只记录类型，无法序列化其代码，恢复时需重新提供；
        序列 schedule 的取值会完整保存。
        """
        return {
            "version": _STATE_VERSION,
            "last_completed_step": self._last_completed_step,
            "groups": [spec.to_dict() for spec in self._groups.values()],
            "params": [
                {
                    "param_id": state.param_id,
                    "group": state.group,
                    "update_count": state.update_count,
                    "frozen": state.frozen,
                }
                for state in self._params.values()
            ],
            "confirmed": {
                str(step): result.to_dict()
                for step, result in self._confirmed.items()
            },
        }

    def load_state_dict(self, state: Mapping[str, Any]) -> None:
        """从 :meth:`state_dict` 导出的映射恢复状态。

        恢复前调度器必须已按相同组名注册好各组（组结构通过构造函数或
        :meth:`add_group` 建立）。序列组的取值会与保存点逐一核对；可调用组
        无法核对代码，仅核对类型，调用方需保证其逻辑与保存前一致。
        """
        if not isinstance(state, Mapping):
            raise SchedulerError("state 必须是映射类型")
        version = state.get("version")
        if version != _STATE_VERSION:
            raise SchedulerError(
                f"不支持的状态版本 {version!r}，期望 {_STATE_VERSION}"
            )

        saved_groups = state.get("groups", [])
        if not isinstance(saved_groups, list) or len(saved_groups) != len(
            self._groups
        ):
            raise SchedulerError(
                "组结构不匹配：恢复前注册的组与保存状态中的组数量不一致"
            )
        for saved in saved_groups:
            name = saved.get("name")
            if name not in self._groups:
                raise SchedulerError(f"保存状态包含未注册的组 {name!r}")
            spec = self._groups[name]
            if saved.get("kind") != spec.kind:
                raise SchedulerError(
                    f"组 {name!r} 的 schedule 类型与保存状态不一致"
                )
            if (
                spec.kind == "sequence"
                and list(saved.get("values", [])) != spec.values
            ):
                raise SchedulerError(f"组 {name!r} 的序列取值与保存状态不一致")

        restored = self._build_restored(state)
        self._params = restored["params"]
        self._confirmed = restored["confirmed"]
        self._last_completed_step = restored["last_completed_step"]

    @classmethod
    def from_state(
        cls,
        state: Mapping[str, Any],
        schedules: Optional[Mapping[str, Schedule]] = None,
    ) -> "Scheduler":
        """从保存状态直接构建调度器，无需预先注册组。

        :param state: :meth:`state_dict` 导出的映射。
        :param schedules: 组名到 schedule 的映射。序列组可省略（使用保存的取值）；
            可调用组必须提供，因为其代码无法序列化。
        """
        if not isinstance(state, Mapping):
            raise SchedulerError("state 必须是映射类型")
        if state.get("version") != _STATE_VERSION:
            raise SchedulerError(
                f"不支持的状态版本 {state.get('version')!r}，期望 {_STATE_VERSION}"
            )
        schedules = dict(schedules or {})
        group_args: Dict[str, Schedule] = {}
        for saved in state.get("groups", []):
            name, kind = saved.get("name"), saved.get("kind")
            if name in schedules:
                group_args[name] = schedules[name]
            elif kind == "sequence":
                group_args[name] = list(saved.get("values", []))
            else:
                raise SchedulerError(
                    f"可调用组 {name!r} 无法从状态恢复，必须在 schedules 中重新提供"
                )
        scheduler = cls(group_args)
        scheduler.load_state_dict(state)
        return scheduler

    def _build_restored(self, state: Mapping[str, Any]) -> Dict[str, Any]:
        last_completed = state.get("last_completed_step")
        if not isinstance(last_completed, int) or isinstance(
            last_completed, bool
        ) or last_completed < 0:
            raise SchedulerError("last_completed_step 非法")

        params: Dict[Any, _ParamState] = {}
        for raw in state.get("params", []):
            pid = raw["param_id"]
            self._require_hashable(pid)
            if pid in params:
                raise SchedulerError(f"保存状态中参数 {pid!r} 重复")
            group = raw["group"]
            if group not in self._groups:
                raise SchedulerError(f"参数 {pid!r} 引用了未知组 {group!r}")
            count = raw["update_count"]
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise SchedulerError(f"参数 {pid!r} 的 update_count 非法")
            params[pid] = _ParamState(
                param_id=pid,
                group=group,
                update_count=count,
                frozen=bool(raw["frozen"]),
            )

        confirmed: Dict[int, StepResult] = {}
        for raw_key, raw_result in state.get("confirmed", {}).items():
            try:
                step = int(raw_key)
            except (TypeError, ValueError):
                raise SchedulerError(f"已确认步编号非法：{raw_key!r}") from None
            if str(step) != str(raw_key):
                raise SchedulerError(f"已确认步编号非法：{raw_key!r}")
            confirmed[step] = self._restore_result(step, raw_result, params)

        if confirmed:
            if sorted(confirmed) != list(range(1, max(confirmed) + 1)):
                raise SchedulerError("已确认步编号必须从 1 开始连续")
        if last_completed != (max(confirmed) if confirmed else 0):
            raise SchedulerError("last_completed_step 与已确认步记录不一致")
        return {
            "params": params,
            "confirmed": confirmed,
            "last_completed_step": last_completed,
        }

    def _restore_result(
        self,
        step: int,
        raw: Mapping[str, Any],
        params: Mapping[Any, _ParamState],
    ) -> StepResult:
        if raw.get("step") != step:
            raise SchedulerError(f"步 {step} 的记录内容不一致")
        updated = tuple(raw["updated"])
        frozen = tuple(raw["frozen"])
        learning_rates = dict(raw["learning_rates"])
        counts_after = dict(raw["counts_after"])

        # 只做结构校验：历史步反映的是当时的状态，不能与参数的最终状态比较
        # （参数之后可能被暂停/调组，计数也会继续增长）。
        for pid in updated:
            if pid not in params:
                raise SchedulerError(f"步 {step} 引用了未知参数 {pid!r}")
        for pid in frozen:
            if pid not in params:
                raise SchedulerError(f"步 {step} 的 frozen 引用了未知参数 {pid!r}")
        if set(updated) & set(frozen):
            raise SchedulerError(f"步 {step} 同一参数不能同时更新与暂停")
        if set(learning_rates) != set(updated):
            raise SchedulerError(f"步 {step} 的 learning_rates 与 updated 不一致")
        for pid, count in counts_after.items():
            if pid not in params:
                raise SchedulerError(f"步 {step} 引用了未知参数 {pid!r}")
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise SchedulerError(f"步 {step} 的计数非法")
        raw_plan = raw["next_plan"]
        next_plan = StepPlan(
            step=raw_plan["step"],
            learning_rates=dict(raw_plan["learning_rates"]),
            frozen=tuple(raw_plan["frozen"]),
        )
        if next_plan.step != step + 1:
            raise SchedulerError(f"步 {step} 的 next_plan 步编号不正确")
        return StepResult(
            step=step,
            updated=updated,
            learning_rates=learning_rates,
            counts_after=counts_after,
            frozen=frozen,
            next_plan=next_plan,
        )

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    @staticmethod
    def _require_hashable(param_id: Any) -> None:
        try:
            hash(param_id)
        except TypeError as exc:
            raise SchedulerError("param_id 必须可哈希") from exc

    def _require_param(self, param_id: Any) -> _ParamState:
        try:
            return self._params[param_id]
        except KeyError:
            raise SchedulerError(f"未知参数 {param_id!r}") from None
