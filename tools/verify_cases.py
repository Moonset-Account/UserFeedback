"""C01-C04 核验用例：直接调用 capsim 实现并执行真实断言。

每个用例接收 Checker，运行过程中累计 passed/failed/skipped，
并返回可放入 JSON 的 seed 与期望/观察摘要（不含凭据或原始敏感数据）。
"""
from __future__ import annotations

from typing import Any

from capsim import (
    ALL_MASK,
    CAP_NAMES,
    Edge,
    CapState,
    Graph,
    analyze,
    file_cap_exec,
    lower_bounding,
    plain_exec,
    raise_ambient,
)
from capsim.caps import NAME_TO_BIT, to_names
from capsim.transitions import TransitionError, apply_operation


class Checker:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.skipped = 0
        self.failures: list[str] = []

    def check(self, condition: bool, message: str) -> None:
        if condition:
            self.passed += 1
        else:
            self.failed += 1
            self.failures.append(message)

    def eq(self, actual: Any, expected: Any, message: str) -> None:
        self.check(
            actual == expected,
            f"{message} | 期望={expected!r} 观察={actual!r}",
        )

    def raises(self, exc_type: type[Exception], fn: Any, message: str) -> None:
        try:
            fn()
        except exc_type:
            self.passed += 1
        except Exception as exc:  # 抛了错误类型也算失败
            self.failed += 1
            self.failures.append(
                f"{message} | 期望 {exc_type.__name__}，观察 {type(exc).__name__}: {exc}"
            )
        else:
            self.failed += 1
            self.failures.append(f"{message} | 期望抛出 {exc_type.__name__}，但未抛出")

    def summary(self) -> dict[str, int]:
        return {"passed": self.passed, "failed": self.failed, "skipped": self.skipped}


# ---------------------------------------------------------------------------
# C01：十二项全集独立枚举的非 root exec 转换（普通执行 vs 文件能力执行）
# ---------------------------------------------------------------------------
def case_c01(chk: Checker) -> dict[str, Any]:
    observations: dict[str, Any] = {"per_cap": {}}
    for cap in CAP_NAMES:
        bit = NAME_TO_BIT[cap]
        # 每项独立构造：P={cap}, I={cap}, E={cap}, A={}, B=全集
        s0 = CapState(permitted=bit, effective=bit, inheritable=bit,
                      ambient=0, bounding=ALL_MASK)

        # 1) 普通执行：P'=(P&I)|A={cap}，E'=0，I/B 不变，A 保留为空
        pe = plain_exec(s0)
        chk.eq(to_names(pe.p), [cap], f"C01 {cap} plain_exec 保留 P")
        chk.eq(pe.e, 0, f"C01 {cap} plain_exec 清空 E")
        chk.eq(to_names(pe.i), [cap], f"C01 {cap} plain_exec 保持 I")
        chk.eq(pe.b, ALL_MASK, f"C01 {cap} plain_exec 保持 B")
        chk.eq(pe.a, 0, f"C01 {cap} plain_exec 保持空 A")

        # 2) 普通执行但 I 不含该项：P'=(P&I)|A=空，普通文件拿不到
        s_noi = CapState(permitted=bit, effective=bit, inheritable=0,
                         ambient=0, bounding=ALL_MASK)
        pe_noi = plain_exec(s_noi)
        chk.eq(pe_noi.p, 0, f"C01 {cap} plain_exec I 不含则 P 丢失")
        chk.eq(pe_noi.e, 0, f"C01 {cap} plain_exec I 不含则 E 清空")

        # 3) 文件能力执行（fP={cap}, fE=1）：P'=fP&B={cap}, E'=P', A 清空
        fe = file_cap_exec(s0, file_permitted=bit, file_effective=True)
        chk.eq(to_names(fe.p), [cap], f"C01 {cap} file_exec 经 fP 获得 P")
        chk.eq(to_names(fe.e), [cap], f"C01 {cap} file_exec fE 置位 E")
        chk.eq(fe.a, 0, f"C01 {cap} file_exec 清空 A")

        # 4) 文件能力但 fE=0：P'={cap}, E'=0
        fe_ne = file_cap_exec(s0, file_permitted=bit, file_effective=False)
        chk.eq(to_names(fe_ne.p), [cap], f"C01 {cap} file_exec fE=0 仍有 P")
        chk.eq(fe_ne.e, 0, f"C01 {cap} file_exec fE=0 时 E 为空")

        # 5) ambient 前提：A={cap} 的普通执行保留 ambient 且 P' 含该项；
        #    同状态走文件能力执行时 ambient 被清空
        s_amb = CapState(permitted=bit, effective=0, inheritable=bit,
                         ambient=bit, bounding=ALL_MASK)
        pe_amb = plain_exec(s_amb)
        chk.eq(to_names(pe_amb.a), [cap], f"C01 {cap} plain_exec 保留 ambient")
        chk.eq(to_names(pe_amb.p), [cap], f"C01 {cap} plain_exec P 含 ambient")
        fe_amb = file_cap_exec(s_amb, file_permitted=0, file_effective=False)
        chk.eq(fe_amb.a, 0, f"C01 {cap} file_exec 清掉 ambient")

        # 6) bounding 不含该项：文件能力被 B 拦截（删去的项不可恢复）
        s_b = CapState(permitted=0, effective=0, inheritable=0,
                       ambient=0, bounding=ALL_MASK & ~bit)
        fe_b = file_cap_exec(s_b, file_permitted=bit, file_effective=True)
        chk.eq(fe_b.p, 0, f"C01 {cap} file_exec 受 B 限制无法获得")
        chk.eq(fe_b.e, 0, f"C01 {cap} file_exec 受 B 限制 E 为空")

        observations["per_cap"][cap] = {
            "plain_p": to_names(pe.p),
            "file_p": to_names(fe.p),
            "file_e": to_names(fe.e),
            "b_blocked_p": to_names(fe_b.p),
        }

    # 全集规模与独立枚举互不污染（每次只处理单项）
    chk.eq(len(CAP_NAMES), 12, "C01 能力全集恰为十二项")
    chk.check(
        all(
            len(v["plain_p"]) <= 1 and len(v["file_p"]) <= 1
            for v in observations["per_cap"].values()
        ),
        "C01 独立枚举不产生跨项能力组合",
    )
    return {
        "seed": "enumerate all 12 caps, independent single-cap states",
        "expected": "plain: P'=(P&I)|A,E'=0; file: P'=fP&B, E'=P' if fE, A cleared",
        "observed_caps": list(observations["per_cap"].keys()),
    }


# ---------------------------------------------------------------------------
# C02：ambient 前提、降 bounding 需 CAP_SETPCAP、删除项不可恢复
# ---------------------------------------------------------------------------
def case_c02(chk: Checker) -> dict[str, Any]:
    net_raw = NAME_TO_BIT["CAP_NET_RAW"]
    setpcap = NAME_TO_BIT["CAP_SETPCAP"]
    kill = NAME_TO_BIT["CAP_KILL"]

    base = CapState(permitted=net_raw | setpcap, effective=setpcap,
                    inheritable=net_raw, bounding=ALL_MASK)

    # 提升 ambient：P∩I 前提
    ok = raise_ambient(base, "CAP_NET_RAW")
    chk.eq(to_names(ok.a), ["CAP_NET_RAW"], "C02 NET_RAW 同时在 P/I，可入 ambient")

    no_inh = CapState(permitted=net_raw | setpcap, effective=setpcap,
                      inheritable=0, bounding=ALL_MASK)
    chk.raises(TransitionError, lambda: raise_ambient(no_inh, "CAP_NET_RAW"),
               "C02 缺 inheritable 不能提升 ambient")

    no_perm = CapState(permitted=setpcap, effective=setpcap,
                       inheritable=net_raw, bounding=ALL_MASK)
    chk.raises(TransitionError, lambda: raise_ambient(no_perm, "CAP_NET_RAW"),
               "C02 缺 permitted 不能提升 ambient")

    # SETPCAP 永不允许进入 ambient
    chk.raises(TransitionError, lambda: raise_ambient(base, "CAP_SETPCAP"),
               "C02 CAP_SETPCAP 禁止入 ambient")

    # 降 bounding 需要 effective CAP_SETPCAP
    dropped = lower_bounding(base, "CAP_KILL")
    chk.eq(to_names(dropped.b), [c for c in CAP_NAMES if c != "CAP_KILL"],
           "C02 有 SETPCAP 可从 bounding 删除 KILL")
    no_priv = CapState(permitted=setpcap, effective=0, inheritable=0,
                       bounding=ALL_MASK)
    chk.raises(TransitionError, lambda: lower_bounding(no_priv, "CAP_KILL"),
               "C02 无 effective SETPCAP 不能降 bounding")

    # 重复删除：已删除项不能恢复/再删
    chk.raises(TransitionError, lambda: lower_bounding(dropped, "CAP_KILL"),
               "C02 已从 bounding 删除的项不能重复删除")

    # 删除后无法经文件能力恢复
    fe = file_cap_exec(dropped, file_permitted=kill, file_effective=True)
    chk.eq(fe.p & kill, 0, "C02 文件能力无法恢复已删除的 KILL")
    chk.eq(fe.e & kill, 0, "C02 已删除项不会出现在 effective")

    # 删除后无法提升 ambient（B 前提失败），且普通执行也无法拿回
    chk.raises(TransitionError, lambda: raise_ambient(dropped, "CAP_KILL"),
               "C02 已删除项不能提升 ambient")
    pe = plain_exec(dropped)
    chk.eq(pe.b & kill, 0, "C02 exec 后 bounding 仍不含已删除项")

    # 降 bounding 同步清掉该项 ambient（cap_drop_ambient，保持 A⊆B）
    amb_state = CapState(permitted=net_raw | setpcap, effective=setpcap,
                         inheritable=net_raw, ambient=net_raw, bounding=ALL_MASK)
    drop_raw = lower_bounding(amb_state, "CAP_NET_RAW")
    chk.eq(drop_raw.a & net_raw, 0, "C02 降 bounding 同步清除该项 ambient")

    return {
        "seed": "P={NET_RAW,SETPCAP}, E={SETPCAP}, I={NET_RAW}, B=all",
        "expected": "ambient needs P&I (SETPCAP forbidden); bset drop needs E SETPCAP; dropped caps never return",
        "observed_b_after_drop": to_names(dropped.b),
    }


# ---------------------------------------------------------------------------
# C03：effective 门控限制可达边；分支状态独立，不拼接不存在的能力组合
# ---------------------------------------------------------------------------
def case_c03(chk: Checker) -> dict[str, Any]:
    kill = NAME_TO_BIT["CAP_KILL"]
    net_raw = NAME_TO_BIT["CAP_NET_RAW"]
    chown = NAME_TO_BIT["CAP_CHOWN"]

    initial = CapState(permitted=kill | net_raw | chown,
                       effective=kill | net_raw | chown,
                       inheritable=kill | net_raw | chown,
                       bounding=ALL_MASK)

    edges = [
        # 左分支：普通执行（丢 E，P 经 I 保留）-> NET_RAW 门控边 -> 汇聚
        Edge("S", "L", {"type": "plain_exec"}),
        Edge("L", "J", {"type": "plain_exec"}, requires_effective=("CAP_NET_RAW",),
             label="L->J [need NET_RAW]"),
        # 右分支：文件能力执行 fE=1 保住 E（仅 NET_RAW）-> NET_RAW 门控边 -> 汇聚
        Edge("S", "R", {"type": "file_cap_exec",
                        "permitted": ["CAP_NET_RAW"], "effective": True}),
        Edge("R", "J", {"type": "plain_exec"}, requires_effective=("CAP_NET_RAW",),
             label="R->J [need NET_RAW]"),
        # 直连门控边（初始 E 充足，可达）
        Edge("S", "J", {"type": "plain_exec"}, requires_effective=("CAP_NET_RAW",),
             label="S->J [need NET_RAW]"),
        # 汇聚后两条出口
        Edge("J", "X1", {"type": "file_cap_exec",
                         "permitted": ["CAP_CHOWN"], "effective": True}),
        Edge("J", "X2", {"type": "plain_exec"}),
    ]
    graph = Graph(edges, exits=["X1", "X2"])
    result = analyze(graph, initial, start="S")

    # 门控：左分支在 L 已丢 E，L->J 不可达；右分支 fE 保住 E，R->J 可达；直连可达
    blocked_ids = {b["edge"] for b in result["blocked_edges"]}
    chk.check("L->J [need NET_RAW]" in blocked_ids, "C03 左分支 NET_RAW 门控边被阻断")
    chk.check("R->J [need NET_RAW]" not in blocked_ids, "C03 右分支 NET_RAW 门控边可达")
    chk.check("S->J [need NET_RAW]" not in blocked_ids, "C03 初始 E 充足时直连门控边可达")

    # 节点级状态独立保存：J 上存在多个不同状态，不做并集
    j_states = [r for r in result["all_reached"] if r["node"] == "J"]
    chk.check(len(j_states) >= 2, "C03 汇聚节点保留多条分支的独立状态")
    for r in j_states:
        live = set(r["state"]["P"]) | set(r["state"]["E"]) | set(r["state"]["A"])
        # 深度>=2 的汇聚态不可能仍持有全部三项（普通执行已清 E、文件能力只给 NET_RAW）
        chk.check(
            not (r["depth"] >= 2 and live == {"CAP_KILL", "CAP_NET_RAW", "CAP_CHOWN"}),
            "C03 不存在把分支能力拼成的全集组合",
        )

    # 右分支经文件能力只拿到 NET_RAW，不会凭空带上左分支的 KILL/CHOWN
    r_states = [r for r in result["all_reached"] if r["node"] == "R"]
    chk.eq(len(r_states), 1, "C03 右分支节点仅一个状态")
    chk.eq(set(r_states[0]["state"]["P"]), {"CAP_NET_RAW"},
           "C03 文件能力只授予声明的 NET_RAW")
    chk.eq(set(r_states[0]["state"]["E"]), {"CAP_NET_RAW"},
           "C03 fE=1 时 effective 仅含 NET_RAW")

    # 全部出口均列出且各自携带实际可达状态
    chk.eq(set(result["exit_states"].keys()), {"X1", "X2"}, "C03 两个出口都有可达状态")
    chk.check(len(result["exit_states"]["X1"]) >= 1, "C03 X1 至少一条可达路径")
    chk.check(len(result["exit_states"]["X2"]) >= 1, "C03 X2 至少一条可达路径")

    # 终止性：展开有限且每个 (节点,状态) 只记录一次
    keys = [
        (r["node"], tuple(sorted((k, tuple(v)) for k, v in r["state"].items())))
        for r in result["all_reached"]
    ]
    chk.eq(len(keys), len(set(keys)), "C03 同节点同状态不重复展开")

    return {
        "seed": "P=E=I={KILL,NET_RAW,CHOWN}, B=all; NET_RAW-gated diamond graph",
        "expected": "L->J blocked (E lost after plain exec); R->J open (fE keeps NET_RAW); branch states never unioned",
        "observed_reached": result["reached_count"],
        "observed_blocked": sorted(blocked_ids),
    }


# ---------------------------------------------------------------------------
# C04：含环图的去重收敛、全部出口状态、越界最短操作序列
# ---------------------------------------------------------------------------
def case_c04(chk: Checker) -> dict[str, Any]:
    net_raw = NAME_TO_BIT["CAP_NET_RAW"]
    setpcap = NAME_TO_BIT["CAP_SETPCAP"]
    kill = NAME_TO_BIT["CAP_KILL"]

    initial = CapState(permitted=net_raw | setpcap | kill,
                       effective=net_raw | setpcap | kill,
                       inheritable=net_raw,
                       ambient=0,
                       bounding=ALL_MASK)

    edges = [
        # 环：S -> L(升 ambient) -> S(普通执行)，可反复走
        Edge("S", "L", {"type": "raise_ambient", "cap": "CAP_NET_RAW"}),
        Edge("L", "S", {"type": "plain_exec"}, label="L->S loop"),
        # 降 bounding 出口链（需要 SETPCAP，初始 E 具备）
        Edge("S", "D1", {"type": "lower_bounding", "cap": "CAP_KILL"}),
        Edge("D1", "D2", {"type": "lower_bounding", "cap": "CAP_KILL"},
             label="D1->D2 redrop"),  # 已删除 -> 转换失败，记录 blocked
        Edge("D1", "E1", {"type": "file_cap_exec",
                          "permitted": ["CAP_NET_RAW"], "effective": True}),
        Edge("D1", "E2", {"type": "plain_exec"}),
    ]
    graph = Graph(edges, exits=["E1", "E2"])
    allowed = ["CAP_NET_RAW", "CAP_SETPCAP"]  # KILL 不在允许集合内
    result = analyze(graph, initial, allowed=allowed, start="S")

    # 去重收敛：环上状态重复到达即终止，整体有限
    chk.check(result["reached_count"] >= 1, "C04 含环展开有限收敛")
    chk.check(result["reached_count"] <= 12,
              f"C04 状态数在有限小范围内（观察 {result['reached_count']}）")
    s_states = [r for r in result["all_reached"] if r["node"] == "S"]
    chk.check(len(s_states) >= 2, "C04 环回后 S 至少出现初始与环回两种状态")
    # 再绕一圈回到的 S 状态与首次环回相同（A 已含 NET_RAW，幂等），不会无限增长
    chk.check(all(len(r["state"]["A"]) <= 1 for r in s_states),
              "C04 ambient 不随循环无限累积（重复状态终止）")

    # 全部出口及各自实际可达状态
    chk.eq(set(result["exit_states"].keys()), {"E1", "E2"}, "C04 两个出口均可达并列状态")
    e1 = result["exit_states"]["E1"]
    e2 = result["exit_states"]["E2"]
    chk.check(all("CAP_KILL" not in r["state"]["P"] for r in e1),
              "C04 E1 文件能力无法拿回已删除的 KILL")
    chk.check(all("CAP_KILL" not in r["state"]["B"] for r in e1 + e2),
              "C04 出口状态的 bounding 均不含已删除 KILL")

    # 重复 drop 被记录为 blocked
    blocked_ids = {b["edge"] for b in result["blocked_edges"]}
    chk.check("D1->D2 redrop" in blocked_ids, "C04 重复删除 bounding 被阻断并记录")

    # 越界：初始/降 B 前状态含 KILL，给出最短操作序列（深度=0 的起点即越界）
    chk.check(len(result["violations"]) >= 1, "C04 检测到超出允许集合的状态")
    first = result["violations"][0]
    chk.eq(first["extra_caps"], ["CAP_KILL"], "C04 越界能力为 KILL")
    chk.eq(first["depth"], 0, "C04 最短越界序列为初始状态（零步）")

    # 非零步越界也存在：S->D1 后状态仍持 KILL 于 P/E，深度 1
    later = [v for v in result["violations"] if v["depth"] == 1]
    chk.check(len(later) >= 1, "C04 存在深度 1 的越界状态及最短序列")
    target = later[0]
    chk.eq([step["edge"] for step in target["shortest_sequence"]], ["S->D1"],
           "C04 深度1越界的最短序列恰为一次 lower_bounding")

    # 重放最短序列，必须复现观察到的状态（路径可重放）
    cur: CapState = initial
    for step in target["shortest_sequence"]:
        cur = apply_operation(cur, step["op"])
    chk.eq(cur.to_dict(), target["state"], "C04 最短序列重放与观察状态一致")

    # D1 之后的出口不再越界（KILL 已从 live 集合消失：plain_exec 清 E，文件能力不给 KILL）
    for r in e1 + e2:
        live = set(r["state"]["P"]) | set(r["state"]["E"]) | set(r["state"]["A"])
        chk.check("CAP_KILL" not in live, "C04 出口 live 集合不再含越界 KILL")

    return {
        "seed": "P=E={NET_RAW,SETPCAP,KILL}, I={NET_RAW}; cyclic graph; allowed={NET_RAW,SETPCAP}",
        "expected": "loop dedup converges; E1/E2 listed; KILL violations with shortest sequences",
        "observed_reached": result["reached_count"],
        "observed_violations": len(result["violations"]),
        "observed_blocked": sorted(blocked_ids),
    }


CASES = {
    "C01": ("非 root exec 转换独立枚举（普通执行 vs 文件能力执行）", case_c01),
    "C02": ("ambient 前提 / CAP_SETPCAP 降 bounding / 删除不可恢复", case_c02),
    "C03": ("effective 门控与分支状态独立（不拼接能力组合）", case_c03),
    "C04": ("含环图去重收敛、出口状态与越界最短操作序列", case_c04),
}
