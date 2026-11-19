"""应急账号恢复演练库。

输入:
  - 初始可用凭据及其所在故障域
  - 候选配方: 每份配方写明解锁目标(凭据或账号)、可接受材料清单、
    以及需凑齐的"不同材料"种数(同一材料的多个别名只算一个)
  - 待撤掉的故障域列表

输出(对每个被撤掉的故障域):
  - 从剩余初始材料出发可推导出的可恢复项
  - 一条真正能依次执行的解锁顺序(逐步可行)
  - 无法恢复的项, 以及互相保管解锁材料且无外部起点导致的阻塞环
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence


# ---------------------------------------------------------------------------
# 输入模型
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Credential:
    """一件初始可用的凭据及其所在故障域。"""

    name: str
    domain: str


@dataclass(frozen=True)
class Recipe:
    """一份解锁配方。

    output:    解锁目标(凭据或账号)的名字
    materials: 可接受的材料名字清单(可含别名)
    need:      需凑齐的不同材料种数(别名归一后计数)
    """

    output: str
    materials: tuple[str, ...]
    need: int

    def __init__(self, output: str, materials: Sequence[str], need: int):
        object.__setattr__(self, "output", output)
        object.__setattr__(self, "materials", tuple(materials))
        object.__setattr__(self, "need", need)


# ---------------------------------------------------------------------------
# 输出模型
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class UnlockStep:
    """解锁顺序中的一步: 用 used 中的材料解锁 output。"""

    output: str
    used: tuple[str, ...]


@dataclass
class DomainReport:
    """撤掉某个故障域后的演练结果。"""

    domain: str
    removed: list[str]            # 被撤掉的初始凭据(归一后)
    available_initial: list[str]  # 剩余的初始材料(归一后)
    recovered: list[str]          # 推导出的可恢复项(不含剩余初始材料)
    plan: list[UnlockStep]        # 一条可依次执行的解锁顺序
    blocked: list[str]            # 无法恢复的项
    cycles: list[list[str]]       # 阻塞环(互相保管解锁材料且无外部起点)


# ---------------------------------------------------------------------------
# 别名归一
# ---------------------------------------------------------------------------

def _canonicalizer(aliases: Mapping[str, str] | None):
    table = dict(aliases or {})

    def canon(name: str) -> str:
        seen = set()
        cur = name
        while cur in table:
            if cur in seen:
                raise ValueError(f"别名表存在循环: {name!r}")
            seen.add(cur)
            cur = table[cur]
        return cur

    return canon


# ---------------------------------------------------------------------------
# 输入校验
# ---------------------------------------------------------------------------

def _normalize_credentials(credentials, canon) -> dict[str, set[str]]:
    if isinstance(credentials, Mapping):
        items = list(credentials.items())
    else:
        items = []
        for c in credentials:
            if isinstance(c, Credential):
                items.append((c.name, c.domain))
            else:
                name, domain = c
                items.append((name, domain))
    # 同一材料的多个别名设备可部署在不同故障域, 互为冗余
    result: dict[str, set[str]] = {}
    for name, domain in items:
        if not name or not isinstance(name, str):
            raise ValueError(f"凭据名非法: {name!r}")
        if not domain or not isinstance(domain, str):
            raise ValueError(f"凭据 {name!r} 的故障域非法: {domain!r}")
        result.setdefault(canon(name), set()).add(domain)
    return result


def _normalize_recipes(recipes, canon) -> list[Recipe]:
    normalized: list[Recipe] = []
    for r in recipes:
        if not isinstance(r, Recipe):
            r = Recipe(r["output"], r["materials"], r["need"])
        output = canon(r.output)
        if not output:
            raise ValueError("配方缺少解锁目标")
        if not r.materials:
            raise ValueError(f"配方 {output!r} 的材料清单为空")
        if r.need < 1:
            raise ValueError(f"配方 {output!r} 的 need 必须 >= 1, 实际为 {r.need}")
        if r.need > len(r.materials):
            raise ValueError(
                f"配方 {output!r} 需凑齐 {r.need} 种不同材料, "
                f"但清单只列了 {len(r.materials)} 项, 永远不可能满足"
            )
        # 别名归一后种数可能少于 need: 配方合法但无法满足, 推导时会被阻塞
        materials = tuple(sorted({canon(m) for m in r.materials}))
        normalized.append(Recipe(output, materials, r.need))
    return normalized


# ---------------------------------------------------------------------------
# 推导: 不动点闭包 + 可执行顺序
# ---------------------------------------------------------------------------

def _derive(available: set[str], recipes: list[Recipe]):
    """从 available 出发反复触发可满足配方。

    返回 (最终可用集合, 解锁顺序, 未触发配方)。
    顺序记录即真实执行顺序: 每一步触发时其材料已全部就位。
    """
    plan: list[UnlockStep] = []
    pending = list(recipes)
    progressed = True
    while progressed:
        progressed = False
        still: list[Recipe] = []
        for r in pending:
            if r.output in available:
                continue  # 目标已持有, 无需再解锁
            usable = tuple(m for m in r.materials if m in available)
            if len(usable) >= r.need:
                plan.append(UnlockStep(r.output, usable[: r.need]))
                available.add(r.output)
                progressed = True
            else:
                still.append(r)
        pending = still
    return available, plan, pending


# ---------------------------------------------------------------------------
# 阻塞环检测(Tarjan SCC, 迭代实现)
# ---------------------------------------------------------------------------

def _find_cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    index: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    counter = 0
    cycles: list[list[str]] = []

    for start in sorted(graph):
        if start in index:
            continue
        index[start] = lowlink[start] = counter
        counter += 1
        stack.append(start)
        on_stack.add(start)
        todo = [(start, iter(sorted(graph[start])))]
        while todo:
            node, it = todo[-1]
            descended = False
            for nxt in it:
                if nxt not in index:
                    index[nxt] = lowlink[nxt] = counter
                    counter += 1
                    stack.append(nxt)
                    on_stack.add(nxt)
                    todo.append((nxt, iter(sorted(graph.get(nxt, ())))))
                    descended = True
                    break
                if nxt in on_stack:
                    lowlink[node] = min(lowlink[node], index[nxt])
            if descended:
                continue
            todo.pop()
            is_root = lowlink[node] == index[node]
            if is_root:
                scc = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    scc.append(w)
                    if w == node:
                        break
                self_loop = len(scc) == 1 and scc[0] in graph.get(scc[0], ())
                if len(scc) > 1 or self_loop:
                    cycles.append(sorted(scc))
            if todo and not is_root:
                parent = todo[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[node])
    return sorted(cycles)


def _blocking_cycles(pending: list[Recipe], available: set[str]) -> list[list[str]]:
    """在未触发配方之间建依赖图并找环。

    边 X -> Y: 某个产出 X 的未触发配方缺材料 Y, 且 Y 也只能由未触发配方产出
    (即 Y 不在可用集合中)。环上的项互相保管解锁材料且没有外部起点。
    """
    producers: dict[str, list[Recipe]] = {}
    for r in pending:
        producers.setdefault(r.output, []).append(r)
    graph: dict[str, set[str]] = {}
    for r in pending:
        missing = {m for m in r.materials if m not in available}
        deps = {m for m in missing if m in producers}
        graph.setdefault(r.output, set()).update(deps)
    return _find_cycles(graph)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def run_drill(
    credentials,
    recipes,
    domains: Iterable[str],
    aliases: Mapping[str, str] | None = None,
) -> list[DomainReport]:
    """对每个指定故障域做一次撤除演练。

    credentials: {名字: 故障域} 或 Credential/(名字, 故障域) 序列
    recipes:     Recipe 序列(或等价 mapping)
    domains:     要分别撤掉的故障域
    aliases:     {别名: 本名}, 同一材料的多个别名只算一个
    """
    canon = _canonicalizer(aliases)
    cred_map = _normalize_credentials(credentials, canon)
    norm_recipes = _normalize_recipes(recipes, canon)

    reports: list[DomainReport] = []
    for domain in domains:
        # 同一材料的别名设备只要还有一台存活, 该材料就仍可用
        initial = {n for n, doms in cred_map.items() if doms - {domain}}
        removed = sorted(set(cred_map) - initial)
        available, plan, pending = _derive(set(initial), norm_recipes)
        blocked = sorted({r.output for r in pending} - available)
        reports.append(
            DomainReport(
                domain=domain,
                removed=removed,
                available_initial=sorted(initial),
                recovered=sorted(available - initial),
                plan=plan,
                blocked=blocked,
                cycles=_blocking_cycles(pending, available),
            )
        )
    return reports


# ---------------------------------------------------------------------------
# 演示入口: python drill.py
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    creds = {
        "badge-primary": "room-a",
        "badge-backup": "room-b",      # badge-backup 是 badge-primary 的别名
        "token-admin": "room-b",
    }
    recipes = [
        Recipe("vault-key", ["badge-primary", "token-admin"], need=2),
        Recipe("account-emergency", ["vault-key"], need=1),
    ]
    for report in run_drill(
        creds, recipes, ["room-a", "room-b"],
        aliases={"badge-backup": "badge-primary"},
    ):
        print(f"撤掉故障域 {report.domain}:")
        print(f"  剩余初始材料: {report.available_initial}")
        print(f"  可恢复项:     {report.recovered}")
        print(f"  解锁顺序:     {[(s.output, list(s.used)) for s in report.plan]}")
        print(f"  无法恢复:     {report.blocked}")
        print(f"  阻塞环:       {report.cycles}")
