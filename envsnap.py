"""环境快照重建库（environment snapshot reconstruction）。

在构建机上重放某个步骤时，需要从一段带分支的环境变更记录还原该步骤
"真正读到" 的值。本库把环境变更建模成一个有向无环的事件日志：

* ``assign``   —— 覆盖一个变量。
    - ``eager=True``（立即展开）：在赋值当时按当时值展开 ``$VAR`` /
      ``${VAR}`` 引用，未定义引用按空字符串处理（与 shell 一致）。
    - ``eager=False``（惰性表达式）：原样保存表达式，留到读取时才展开，
      因此可以表达当时尚未定义、读取时才定义的间接引用，也可以形成环。
* ``unset``    —— 删除一个变量。删除后再对同名变量赋值即"删除后重建"，
  旧绑定不会出现在最小来源切片中（它已被删除事件终结、被新绑定遮蔽）。
* ``spawn``    —— 派生进程。子进程继承派生瞬间父进程的环境快照，
  随后父子的修改互相独立。
* ``read``     —— 在某一步读取一个变量，记录读出的结论。

读取结论有三种状态（见 :class:`ReadStatus`）：

* ``VALUE``  —— 读到具体字符串（包括空字符串 ``""`` 与未定义是两回事）；
* ``UNDEFINED`` —— 该变量在本进程及所有祖先的历史上都不存在，
  或最后一个生效事件是删除；
* ``CYCLE``  —— 惰性表达式之间存在间接引用环，诊断信息给出完整引用环链。

对每次读取，:func:`explain` 给出所依赖的 *最小来源切片*：只保留决定该
读值的生效（未被遮蔽的）事件及其传递依赖，外加必要的 ``spawn`` 边。
用 :func:`replay` 重放这个切片，必然产生相同的读值或同一循环诊断
（:meth:`Explanation.verify` 会实际执行并断言这一点）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Sequence, Tuple

__all__ = [
    "ReadStatus",
    "Event",
    "ReadResult",
    "CycleError",
    "Recorder",
    "Engine",
    "Explanation",
    "explain",
    "replay",
    "replay_all",
    "expand_references",
]


class ReadStatus(str, Enum):
    """读取结论的三态。"""

    VALUE = "value"
    UNDEFINED = "undefined"
    CYCLE = "cycle"


@dataclass(frozen=True)
class Event:
    """环境变更日志中的一条不可变事件。

    属性含义：

    id        全局单调编号（同一进程内严格递增，spawn 的父子事件满足
              父先于子）。
    kind      ``"assign" | "unset" | "spawn" | "read"``。
    proc      所属进程 id（根进程为 0）。
    name      assign/unset/read 的变量名；spawn 事件为 ``None``。
    raw       assign 的原始 RHS（eager 模式也是展开前的原文）。
    eager     assign 是否在赋值当时立即展开。
    child     spawn 事件派生出的子进程 id。
    """

    id: int
    kind: str
    proc: int
    name: Optional[str] = None
    raw: Optional[str] = None
    eager: bool = True
    child: Optional[int] = None


@dataclass(frozen=True)
class ReadResult:
    """一次读取的结论。"""

    status: ReadStatus
    value: Optional[str] = None
    cycle: Optional[Tuple[str, ...]] = None
    event_id: Optional[int] = None

    @property
    def is_value(self) -> bool:
        return self.status is ReadStatus.VALUE

    @property
    def is_undefined(self) -> bool:
        return self.status is ReadStatus.UNDEFINED

    @property
    def is_cycle(self) -> bool:
        return self.status is ReadStatus.CYCLE


class CycleError(ValueError):
    """eager 赋值在展开时就卷入了惰性引用环。

    环是在 *赋值当时* 被检测到的，因此不会静默写入一个绑定；
    读取期发现的环则以 :class:`ReadResult` 的 ``CYCLE`` 状态返回。
    """

    def __init__(self, cycle: Sequence[str]):
        self.cycle: Tuple[str, ...] = tuple(cycle)
        super().__init__("indirect reference cycle: " + " -> ".join(self.cycle))


# ---------------------------------------------------------------------------
# 引用展开（$VAR / ${VAR}，未定义引用展开为空字符串，与 shell 一致）
# ---------------------------------------------------------------------------


def expand_references(
    expression: str,
    resolve,
    stack: Tuple[str, ...],
) -> Tuple[ReadStatus, Optional[str], Optional[Tuple[str, ...]], frozenset[int]]:
    """按 shell 风格展开表达式中的引用。

    ``resolve(name, stack)`` 返回 ``(status, value, cycle, deps)`` 四元组；
    本函数把字面量片段与各引用的展开结果拼接起来，并汇总依赖集合。
    任何引用命中环则整体返回 ``CYCLE``（带回同一个确定性环链）。
    """

    parts: List[str] = []
    deps: set[int] = set()
    i = 0
    n = len(expression)
    while i < n:
        ch = expression[i]
        if ch != "$":
            parts.append(ch)
            i += 1
            continue
        if i + 1 >= n:
            raise ValueError("dangling '$' at end of expression")
        nxt = expression[i + 1]
        if nxt == "$":
            parts.append("$")
            i += 2
            continue
        if nxt == "{":
            end = expression.find("}", i + 2)
            if end == -1:
                raise ValueError("unterminated '${' in expression")
            ref = expression[i + 2 : end]
            if not ref:
                raise ValueError("empty reference '${}' in expression")
            i = end + 1
        elif nxt.isalpha() or nxt == "_":
            j = i + 1
            while j < n and (expression[j].isalnum() or expression[j] == "_"):
                j += 1
            ref = expression[i + 1 : j]
            i = j
        else:
            raise ValueError(f"invalid reference after '$': {nxt!r}")
        status, value, cycle, ref_deps = resolve(ref, stack)
        if status is ReadStatus.CYCLE:
            # 环之前已遍历到的绑定仍是诊断所需的来源，必须保留。
            deps.update(ref_deps)
            return ReadStatus.CYCLE, None, tuple(cycle), frozenset(deps)
        deps.update(ref_deps)
        if status is ReadStatus.VALUE:
            parts.append(value)
        # UNDEFINED 引用按空字符串拼接，本身不产生新依赖。
    return ReadStatus.VALUE, "".join(parts), None, frozenset(deps)


# ---------------------------------------------------------------------------
# 引擎：持有分支化的事件日志，并在日志上做纯函数式解析
# ---------------------------------------------------------------------------


@dataclass
class _ProcState:
    parent: Optional[int]
    # 每个变量按事件 id 升序的历史（assign / unset）。
    history: Dict[str, List[Event]] = field(default_factory=dict)
    # 派生出的子进程（按 spawn 事件 id 升序）。
    children: List[Tuple[int, int]] = field(default_factory=list)


class Engine:
    """事件日志的持有者与解析器，可直接用于手动构造轨迹。"""

    def __init__(self) -> None:
        self._next_id: int = 1
        self._next_proc: int = 1
        self._events: List[Event] = []
        self._procs: Dict[int, _ProcState] = {0: _ProcState(parent=None)}

    # -- 基础访问 ----------------------------------------------------------

    @property
    def events(self) -> Tuple[Event, ...]:
        """全部事件，按全局 id 排序。"""
        return tuple(self._events)

    def event(self, event_id: int) -> Event:
        return self._events[event_id - 1]

    def _alloc_id(self) -> int:
        eid = self._next_id
        self._next_id += 1
        return eid

    # -- 记录动作 ----------------------------------------------------------

    def record_assign(
        self,
        proc: int,
        name: str,
        raw: str,
        eager: bool = True,
    ) -> int:
        """记录一次覆盖赋值，返回事件 id。

        eager 赋值会在 *赋值当时* 按当时值展开；若展开卷入惰性引用环，
        抛出 :class:`CycleError` 且不写入任何事件。
        """
        if proc not in self._procs:
            raise KeyError(f"unknown process: {proc}")
        if eager:
            before = self._next_id - 1
            status, value, cycle, _deps = self._expand_eager(raw, proc, before)
            if status is ReadStatus.CYCLE:
                raise CycleError(cycle or ())
            assert value is not None
            ev = Event(self._alloc_id(), "assign", proc, name, value, True)
        else:
            ev = Event(self._alloc_id(), "assign", proc, name, raw, False)
        self._events.append(ev)
        self._procs[proc].history.setdefault(name, []).append(ev)
        return ev.id

    def record_unset(self, proc: int, name: str) -> int:
        """记录一次变量删除，返回事件 id。"""
        if proc not in self._procs:
            raise KeyError(f"unknown process: {proc}")
        ev = Event(self._alloc_id(), "unset", proc, name)
        self._events.append(ev)
        self._procs[proc].history.setdefault(name, []).append(ev)
        return ev.id

    def record_spawn(self, parent_proc: int) -> Tuple[int, int]:
        """记录一次进程派生，返回 ``(spawn_event_id, child_proc_id)``。

        子进程继承 *派生瞬间* 父进程的环境：实现上不为子进程复制绑定，
        解析时沿父链回溯并要求祖先事件早于对应的 spawn 事件即可，
        因此派生之后父子的修改天然互相独立。
        """
        if parent_proc not in self._procs:
            raise KeyError(f"unknown process: {parent_proc}")
        child = self._next_proc
        self._next_proc += 1
        ev = Event(self._alloc_id(), "spawn", parent_proc, child=child)
        self._events.append(ev)
        self._procs[parent_proc].children.append((ev.id, child))
        self._procs[child] = _ProcState(parent=parent_proc)
        return ev.id, child

    def record_read(self, proc: int, name: str) -> Tuple[int, ReadResult]:
        """记录一次读取并立即返回读取结论（同时写入 read 事件）。"""
        if proc not in self._procs:
            raise KeyError(f"unknown process: {proc}")
        result = self.read_at(proc, name, self._next_id - 1)
        ev = Event(self._alloc_id(), "read", proc, name)
        self._events.append(ev)
        return ev.id, result

    # -- 读取与解析 --------------------------------------------------------

    def read_at(self, proc: int, name: str, before_id: int) -> ReadResult:
        """在给定进程、给定历史点（只看 id <= before_id 的事件）读取变量。"""
        status, value, cycle, deps = self._resolve_top(proc, name, (), before_id)
        return ReadResult(status=status, value=value, cycle=cycle)

    def snapshot(self, proc: int, before_id: Optional[int] = None) -> Dict[str, str]:
        """还原某进程在某历史点的环境快照（只含已定义变量的字面值）。

        环上的惰性变量不包含在快照中（它读不出字面值）。
        """
        if before_id is None:
            before_id = self._next_id - 1
        out: Dict[str, str] = {}
        names: set[str] = set()
        cur: Optional[int] = proc
        while cur is not None:
            names.update(self._procs[cur].history.keys())
            cur = self._procs[cur].parent
        for name in sorted(names):
            status, value, _cycle, _deps = self._resolve_top(proc, name, (), before_id)
            if status is ReadStatus.VALUE:
                out[name] = value  # type: ignore[assignment]
        return out

    def _spawn_chain(self, proc: int) -> List[int]:
        """从根进程到达 ``proc`` 所必需的 spawn 事件 id 链。"""
        edges: List[int] = []
        cur = proc
        while self._procs[cur].parent is not None:
            parent = self._procs[cur].parent
            edge = next(
                eid for eid, child in self._procs[parent].children if child == cur
            )
            edges.append(edge)
            cur = parent
        return edges

    def _terminal(
        self, proc: int, name: str, before_id: int
    ) -> Optional[Event]:
        """沿父链找到决定变量当前状态的最后一个生效事件。

        ``None`` 表示未定义。回溯到父进程时，只允许早于本进程 spawn
        事件的祖先历史——子进程持有的是派生瞬间的快照，派生之后父进程
        的新增/修改对子进程不可见。
        """
        cur: Optional[int] = proc
        while cur is not None:
            state = self._procs[cur]
            latest: Optional[Event] = None
            for ev in state.history.get(name, ()):
                if ev.id <= before_id:
                    latest = ev
                else:
                    break
            if latest is not None:
                return latest
            if state.parent is None:
                return None
            edge = next(
                eid for eid, child in self._procs[state.parent].children if child == cur
            )
            before_id = edge
            cur = state.parent
        return None

    def _resolve_top(
        self,
        proc: int,
        name: str,
        stack: Tuple[str, ...],
        before_id: int,
    ):
        """解析一个顶层（或被引用的）变量，并收集最小依赖。

        整个解析过程统一使用 *读取时刻* 的截断点 ``before_id``：lazy
        表达式在读取时才展开，因此能看到赋值之后、读取之前的变更。
        """
        # 非根进程的任何切片都必须带上 spawn 链，否则重放无法重建该进程。
        deps: set[int] = set(self._spawn_chain(proc))
        term = self._terminal(proc, name, before_id)
        if term is None:
            return ReadStatus.UNDEFINED, None, None, frozenset(deps)
        deps.add(term.id)
        if term.kind == "unset":
            return ReadStatus.UNDEFINED, None, None, frozenset(deps)
        if term.eager:
            # eager 绑定保存的就是赋值当时已展开的字面值。
            return ReadStatus.VALUE, term.raw, None, frozenset(deps)
        # 惰性绑定：读取时才按读取时刻的环境展开。
        return self._expand_lazy(term.raw, proc, stack + (name,), before_id, deps)

    def _expand_lazy(
        self,
        expression: str,
        proc: int,
        stack: Tuple[str, ...],
        before_id: int,
        seed_deps: set[int],
    ):
        def resolve(ref: str, st: Tuple[str, ...]):
            if ref in st:
                idx = st.index(ref)
                chain = st[idx:] + (ref,)
                # 闭包点本身没有新事件，但此前遍历到的绑定仍由下层带回。
                return ReadStatus.CYCLE, None, chain, frozenset()
            return self._resolve_top(proc, ref, st, before_id)

        status, value, cycle, ref_deps = expand_references(expression, resolve, stack)
        deps = set(seed_deps)
        deps.update(ref_deps)
        return status, value, cycle, frozenset(deps)

    def _expand_eager(self, expression: str, proc: int, before_id: int):
        """eager 赋值当时的展开：环按 CycleError 语义上报。"""
        def resolve(ref: str, st: Tuple[str, ...]):
            if ref in st:
                idx = st.index(ref)
                chain = st[idx:] + (ref,)
                return ReadStatus.CYCLE, None, tuple(chain), frozenset()
            return self._resolve_top(proc, ref, st, before_id)

        return expand_references(expression, resolve, ())

    # -- 切片与解释 --------------------------------------------------------

    def explain(self, read_event_id: int) -> "Explanation":
        """对日志中已记录的 read 事件给出结论与最小来源切片。"""
        ev = self.event(read_event_id)
        if ev.kind != "read":
            raise ValueError(f"event {read_event_id} is not a read event")
        # read 事件本身的 id 是最后分配的，解析时排除它自身。
        status, value, cycle, deps = self._resolve_top(ev.proc, ev.name, (), ev.id - 1)
        result = ReadResult(status=status, value=value, cycle=cycle, event_id=ev.id)
        slice_ids = tuple(sorted(deps))
        return Explanation(
            read_event=ev,
            result=result,
            slice_ids=slice_ids,
            slice_events=tuple(self.event(i) for i in slice_ids),
            engine=self,
        )


class Recorder:
    """面向单条轨迹的便捷记录器，方法名对应题面中的动作。"""

    def __init__(self) -> None:
        self.engine = Engine()
        self.proc: int = 0

    def set(self, name: str, value: str, eager: bool = True) -> int:
        """覆盖变量；``eager=False`` 时保存表达式，留到读取时展开。"""
        return self.engine.record_assign(self.proc, name, value, eager)

    def delete(self, name: str) -> int:
        """删除变量。"""
        return self.engine.record_unset(self.proc, name)

    def spawn(self) -> "Recorder":
        """派生子进程，返回子进程自己的记录器；父子随后独立修改。"""
        _eid, child = self.engine.record_spawn(self.proc)
        child_rec = Recorder.__new__(Recorder)
        child_rec.engine = self.engine
        child_rec.proc = child
        return child_rec

    def read(self, name: str) -> ReadResult:
        """在当前步骤读取变量，并把 read 事件写入日志。"""
        _eid, result = self.engine.record_read(self.proc, name)
        return result

    def explain(self, name: str) -> "Explanation":
        """读取变量并立即返回带最小切片的解释（会写入一条 read 事件）。"""
        eid, _result = self.engine.record_read(self.proc, name)
        return self.engine.explain(eid)

    def explain_event(self, read_event_id: int) -> "Explanation":
        return self.engine.explain(read_event_id)

    @property
    def events(self) -> Tuple[Event, ...]:
        return self.engine.events

    def snapshot(self) -> Dict[str, str]:
        return self.engine.snapshot(self.proc)


@dataclass(frozen=True)
class Explanation:
    """一次读取的解释：结论 + 最小来源切片。"""

    read_event: Event
    result: ReadResult
    slice_ids: Tuple[int, ...]
    slice_events: Tuple[Event, ...]
    engine: Engine

    def verify(self) -> ReadResult:
        """实际重放最小切片，断言重放结论与原结论一致，返回重放结论。

        比对的是状态、值与环链（``CYCLE`` 时要求 *同一个* 循环诊断），
        而不仅仅是状态类别。
        """
        replayed = replay(self.slice_events, self.read_event.proc, self.read_event.name)
        if replayed.status is not self.result.status:
            raise AssertionError(
                f"replay status {replayed.status!r} != {self.result.status!r}"
            )
        if replayed.value != self.result.value:
            raise AssertionError(
                f"replay value {replayed.value!r} != {self.result.value!r}"
            )
        if replayed.cycle != self.result.cycle:
            raise AssertionError(
                f"replay cycle {replayed.cycle!r} != {self.result.cycle!r}"
            )
        return replayed


# ---------------------------------------------------------------------------
# 切片重放
# ---------------------------------------------------------------------------


def _replay_engine(events: Sequence[Event]) -> Tuple[Engine, Dict[int, int]]:
    """在全新引擎上重放事件切片，返回 ``(引擎, 原始进程号 -> 重放进程号)``。

    事件按 id 升序应用（id 与 happens-before 关系一致：同一进程内有序、
    spawn 的父事件先于子进程的任何事件）。多级派生时孙进程的编号由
    record_spawn 重新分配，因此必须维护原始进程号到重放进程号的映射。
    read 事件不在此应用，由调用方统一在切片末尾执行。
    """
    engine = Engine()
    proc_map: Dict[int, int] = {0: 0}
    for ev in sorted(events, key=lambda e: e.id):
        if ev.kind == "assign":
            engine.record_assign(proc_map[ev.proc], ev.name, ev.raw, ev.eager)
        elif ev.kind == "unset":
            engine.record_unset(proc_map[ev.proc], ev.name)
        elif ev.kind == "spawn":
            _eid, new_child = engine.record_spawn(proc_map[ev.proc])
            proc_map[ev.child] = new_child
        elif ev.kind == "read":
            continue
        else:  # pragma: no cover - 防御性分支
            raise ValueError(f"unknown event kind: {ev.kind}")
    return engine, proc_map


def replay(
    events: Sequence[Event],
    proc: int,
    name: str,
) -> ReadResult:
    """在一个全新引擎上重放事件切片，并在指定进程读取变量。"""
    engine, proc_map = _replay_engine(events)
    if proc not in proc_map:
        raise ValueError(
            f"slice does not reconstruct process {proc}; "
            f"is the required spawn edge missing?"
        )
    return engine.read_at(proc_map[proc], name, engine._next_id - 1)


def replay_all(events: Sequence[Event]) -> Dict[int, ReadResult]:
    """重放日志并对 *所有* read 事件给出结论，键为 read 事件 id。

    每个 read 只能看到 id 严格小于它的事件（读取之后的变更不影响该读取），
    因此按 id 升序应用日志，每遇到一条 read 就在当前前缀上解析一次。
    重放引擎重新分配的进程号通过 spawn 顺序映射回原始进程号。
    """
    engine = Engine()
    proc_map: Dict[int, int] = {0: 0}
    out: Dict[int, ReadResult] = {}
    for ev in sorted(events, key=lambda e: e.id):
        if ev.kind == "assign":
            engine.record_assign(proc_map[ev.proc], ev.name, ev.raw, ev.eager)
        elif ev.kind == "unset":
            engine.record_unset(proc_map[ev.proc], ev.name)
        elif ev.kind == "spawn":
            _eid, new_child = engine.record_spawn(proc_map[ev.proc])
            proc_map[ev.child] = new_child
        elif ev.kind == "read":
            out[ev.id] = engine.read_at(
                proc_map[ev.proc], ev.name, engine._next_id - 1
            )
        else:  # pragma: no cover
            raise ValueError(f"unknown event kind: {ev.kind}")
    return out


def explain(engine: Engine, read_event_id: int) -> Explanation:
    """模块级便捷入口，等价于 ``engine.explain(read_event_id)``。"""
    return engine.explain(read_event_id)
