"""四类操作的非 root 能力转换语义（依据 capabilities(7)）。

统一前提（任务给定）：非 root 主体、no_new_privs 关闭、securebits 默认、
所有文件均无 setuid/setgid、排除用户命名空间与 root 特殊规则。

操作：
    plain_exec      普通执行（非特权文件，文件能力为空）
    file_cap_exec   文件能力执行（文件带 permitted/inheritable/effective 能力）
    lower_bounding  PR_CAPBSET_DROP：从 bounding 删去一项
    raise_ambient   PR_CAP_AMBIENT_RAISE：把一项放入 ambient

内核忠实度补充（均在 README 注明，可由测试固定）：
  * execve 时文件允许集要与当前 bounding 相交（F_perm & B），B 之外的项拿不到；
  * ambient 不能包含 CAP_SETPCAP（以及本全集之外的 CAP_SETFCAP），
    对应 prctl(PR_CAP_AMBIENT) 对这两项的 EPERM/EINVAL；
  * 已从 bounding 删除的项无法再经文件能力或 ambient 恢复（集合单调性）；
  * 执行带文件能力的特权文件会清空 ambient，普通文件则保留 ambient。
"""
from __future__ import annotations

from typing import Any

from .caps import NAME_TO_BIT, to_mask
from .state import CapState


class TransitionError(Exception):
    """操作前提不满足或参数非法（对应内核 prctl/execve 的失败路径）。"""


# prctl(PR_CAP_AMBIENT) 明确禁止放入 ambient 的能力
AMBIENT_FORBIDDEN = frozenset({"CAP_SETPCAP", "CAP_SETFCAP"})


def _bit_of(cap: str) -> int:
    try:
        return NAME_TO_BIT[cap]
    except KeyError:
        raise TransitionError(f"能力不在十二项全集内: {cap!r}") from None


def plain_exec(state: CapState) -> CapState:
    """普通执行：非特权文件（无 setuid/setgid、无文件能力）。

    非 root execve（capabilities(7)）：
        P' = (P & I) | A
        E' = 0
        I' = I；B' = B
        A' = A（非特权文件保留 ambient；ambient 与 B 的约束在下次
               特权文件执行/模型输入处体现，普通文件公式本身为 P&I | A）
    """
    new_p = (state.p & state.i) | state.a
    return CapState(
        permitted=new_p,
        effective=0,
        inheritable=state.i,
        ambient=state.a,
        bounding=state.b,
    )


def file_cap_exec(
    state: CapState,
    file_permitted: Any = 0,
    file_inheritable: Any = 0,
    file_effective: bool = False,
) -> CapState:
    """文件能力执行：文件携带 fP/fI，可选 fE（effective 位）。

    非 root execve 特权文件：
        P' = (P & I & fI) | (fP & B) | A
        E' = P' 若 fE=1，否则 0
        I' = I；B' = B
        A' = 0（特权文件触发 ambient 清空，见 cap_bprm_creds_from_file）
    fI 项同样受 B 约束，故 (P & I & fI) 再与 B 相交。
    """
    fp = to_mask(file_permitted)
    fi = to_mask(file_inheritable)
    inherited = state.p & state.i & fi & state.b
    file_allowed = fp & state.b
    new_p = inherited | file_allowed
    new_e = new_p if file_effective else 0
    return CapState(
        permitted=new_p,
        effective=new_e,
        inheritable=state.i,
        ambient=0,
        bounding=state.b,
    )


def lower_bounding(state: CapState, cap: str) -> CapState:
    """PR_CAPBSET_DROP：从 bounding 集合删去一项。

    前提（任务规则）：当前 effective 必须含 CAP_SETPCAP。
    内核忠实度：
      * 删除已不在 B 中的项对应 EINVAL；
      * cap_bset drop 会同时清掉该项的 ambient（cap_drop_ambient），保持 A⊆B；
      * P/E/I 不立即变化，但 B 单调缩小，被删项在后续 exec 中无法再获得。
    """
    if not (state.e & NAME_TO_BIT["CAP_SETPCAP"]):
        raise TransitionError("降低 bounding 需要 effective CAP_SETPCAP")
    bit = _bit_of(cap)
    if not (state.b & bit):
        raise TransitionError(f"{cap} 已不在 bounding 中，不能重复删除（EINVAL）")
    return CapState(
        permitted=state.p,
        effective=state.e,
        inheritable=state.i,
        ambient=state.a & ~bit,
        bounding=state.b & ~bit,
    )


def raise_ambient(state: CapState, cap: str) -> CapState:
    """PR_CAP_AMBIENT_RAISE：把一项提升进 ambient。

    任务前提：该项必须同时在 permitted 与 inheritable 中。
    内核额外前提：该项在 bounding 中，且 CAP_SETPCAP/CAP_SETFCAP 永不可入 ambient。
    已在 ambient 中时为幂等 no-op（对应内核 cap_raise 的重复提升成功语义）。
    """
    if cap in AMBIENT_FORBIDDEN:
        raise TransitionError(f"{cap} 不允许进入 ambient（内核 prctl 拒绝）")
    bit = _bit_of(cap)
    if not (state.p & bit):
        raise TransitionError(f"提升 ambient 失败：{cap} 不在 permitted 中")
    if not (state.i & bit):
        raise TransitionError(f"提升 ambient 失败：{cap} 不在 inheritable 中")
    if not (state.b & bit):
        raise TransitionError(f"提升 ambient 失败：{cap} 已从 bounding 删除，不可恢复")
    if state.a & bit:
        return state
    return CapState(
        permitted=state.p,
        effective=state.e,
        inheritable=state.i,
        ambient=state.a | bit,
        bounding=state.b,
    )


def apply_operation(state: CapState, operation: dict[str, Any]) -> CapState:
    """按操作字典分发到对应转换。操作字典即图边上的 op。"""
    op_type = operation.get("type")
    if op_type == "plain_exec":
        return plain_exec(state)
    if op_type == "file_cap_exec":
        return file_cap_exec(
            state,
            file_permitted=operation.get("permitted", 0),
            file_inheritable=operation.get("inheritable", 0),
            file_effective=bool(operation.get("effective", False)),
        )
    if op_type == "lower_bounding":
        return lower_bounding(state, operation["cap"])
    if op_type == "raise_ambient":
        return raise_ambient(state, operation["cap"])
    raise TransitionError(f"未知操作类型: {op_type!r}")
