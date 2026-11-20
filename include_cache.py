"""头文件包含解析缓存：记录首次构建的 include 查找顺序与命中内容，
并在新源码树上重查，判定哪些编译入口需要重编。

设计要点（对应需求）：
- record() 按目录顺序记录每个 #include 的候选路径、存在性与命中内容摘要，
  嵌套 include 以深度优先方式递归记录。
- 只记录“命中位置及其之前”的候选；命中之后的后备文件不参与比较，
  因此未选中的后备文件可以单独变化而不触发重编。
- 重查时若命中点之前出现了新文件（靠前目录新增同名文件），
  即使字节相同也判定为差异——因为命中路径改变后，
  下一级相对（引号）include 的查找起点就变了。
- 循环包含通过“当前包含栈”截断递归，记录事件但不再展开，保证结束。
- query() 返回是否需要重编，以及从入口到首次差异的一条依赖链。
"""

from __future__ import annotations

import hashlib
import posixpath
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

_INCLUDE_RE = re.compile(r'^[ \t]*#[ \t]*include[ \t]*([<"])([^>"]+)[>"]')


def _norm(path: str) -> str:
    """规范化路径，统一缓存与查询两侧的键。"""
    return posixpath.normpath(path)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class SourceTree:
    """以 {路径: 内容} 形式提供的一棵源码树。"""

    def __init__(self, files: Optional[Dict[str, bytes]] = None):
        self._files: Dict[str, bytes] = {}
        for path, content in (files or {}).items():
            self.add(path, content)

    def add(self, path: str, content) -> None:
        if isinstance(content, str):
            content = content.encode("utf-8")
        self._files[_norm(path)] = content

    def get(self, path: str) -> Optional[bytes]:
        return self._files.get(_norm(path))

    def __contains__(self, path: str) -> bool:
        return _norm(path) in self._files


def parse_include_dirs(options: Sequence[str]) -> List[str]:
    """从编译选项中提取 -I 目录（支持 '-I dir' 与 '-Idir' 两种写法）。"""
    dirs: List[str] = []
    i = 0
    while i < len(options):
        opt = options[i]
        if opt == "-I":
            i += 1
            if i < len(options):
                dirs.append(_norm(options[i]))
        elif opt.startswith("-I") and len(opt) > 2:
            dirs.append(_norm(opt[2:]))
        i += 1
    return dirs


def parse_directives(data: bytes) -> List[Tuple[bool, str]]:
    """解析文件内容中的 #include 指令，返回 (是否引号包含, 名称) 列表。"""
    text = data.decode("utf-8", errors="replace")
    out: List[Tuple[bool, str]] = []
    for line in text.splitlines():
        m = _INCLUDE_RE.match(line)
        if m:
            out.append((m.group(1) == '"', m.group(2)))
    return out


@dataclass
class CandidateRecord:
    """一次 include 查找中、命中位置（含）之前的一个候选路径。"""
    path: str
    existed: bool
    digest: Optional[str]  # existed 为 True 时的内容摘要


@dataclass
class IncludeEvent:
    """一次 include 解析事件。"""
    parent: int            # 产生 includer 文件的事件下标；入口本身为 -1
    includer: str          # 含有该 #include 的文件
    name: str              # include 的名称
    quoted: bool           # 引号包含还是尖括号包含
    candidates: List[CandidateRecord]  # 按查找顺序，截止到命中项
    resolved: str          # 命中路径；未命中为 ""


@dataclass
class EntryRecord:
    """一个编译入口（翻译单元）的完整缓存记录。"""
    entry: str
    options: List[str]
    entry_digest: str
    events: List[IncludeEvent] = field(default_factory=list)


@dataclass
class Difference:
    """首次差异的描述。"""
    kind: str              # 见 IncludeCache.query 中的说明
    path: str              # 差异相关的路径
    event_index: int       # 相关事件下标；与事件无关时为 -1
    detail: str


@dataclass
class QueryResult:
    """重查结果：是否需要重编，以及入口到首次差异的依赖链。"""
    entry: str
    rebuild: bool
    difference: Optional[Difference]
    chain: List[str]       # 人类可读的依赖链，从入口到首次差异


class IncludeCache:
    """include 解析缓存：先 record，后在新树上 query。"""

    def __init__(self):
        self._records: Dict[str, EntryRecord] = {}

    # ------------------------------------------------------------------ #
    # 记录
    # ------------------------------------------------------------------ #
    def record(self, entry: str, options: Sequence[str],
               tree: SourceTree) -> EntryRecord:
        """首次构建时调用：记录入口、编译选项与全部（含嵌套）include 事件。"""
        entry = _norm(entry)
        data = tree.get(entry)
        if data is None:
            raise KeyError(f"入口文件不存在: {entry}")

        include_dirs = parse_include_dirs(options)
        events: List[IncludeEvent] = []
        parse_memo: Dict[str, List[Tuple[bool, str]]] = {}

        def directives_of(path: str) -> List[Tuple[bool, str]]:
            if path not in parse_memo:
                content = tree.get(path)
                parse_memo[path] = parse_directives(content) if content else []
            return parse_memo[path]

        def candidate_paths(includer: str, name: str,
                            quoted: bool) -> List[str]:
            paths: List[str] = []
            if quoted:
                # 引号包含：先从 includer 所在目录开始相对查找
                paths.append(_norm(posixpath.join(
                    posixpath.dirname(includer), name)))
            for d in include_dirs:
                paths.append(_norm(posixpath.join(d, name)))
            return paths

        def walk(includer: str, parent_idx: int, stack: frozenset) -> None:
            for quoted, name in directives_of(includer):
                candidates: List[CandidateRecord] = []
                resolved = ""
                for cand in candidate_paths(includer, name, quoted):
                    cdata = tree.get(cand)
                    candidates.append(CandidateRecord(
                        path=cand,
                        existed=cdata is not None,
                        digest=_digest(cdata) if cdata is not None else None,
                    ))
                    if cdata is not None:
                        resolved = cand
                        break  # 命中即停止：后备候选不记录、不参与比较
                idx = len(events)
                events.append(IncludeEvent(
                    parent=parent_idx,
                    includer=includer,
                    name=name,
                    quoted=quoted,
                    candidates=candidates,
                    resolved=resolved,
                ))
                if not resolved:
                    continue
                if resolved in stack:
                    # 循环包含：记录命中事件但不再展开，保证递归结束
                    continue
                walk(resolved, idx, stack | {resolved})

        walk(entry, -1, frozenset({entry}))
        record = EntryRecord(
            entry=entry,
            options=list(options),
            entry_digest=_digest(data),
            events=events,
        )
        self._records[entry] = record
        return record

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def query(self, entry: str, tree: SourceTree,
              options: Optional[Sequence[str]] = None) -> QueryResult:
        """在新源码树上重查一个入口，返回是否需要重编及差异依赖链。"""
        entry = _norm(entry)
        record = self._records.get(entry)
        if record is None:
            raise KeyError(f"没有该入口的缓存记录: {entry}")

        if options is not None and list(options) != record.options:
            return QueryResult(entry, True, Difference(
                kind="options-changed", path=entry, event_index=-1,
                detail="编译选项与记录不一致"), [entry])

        data = tree.get(entry)
        if data is None:
            return QueryResult(entry, True, Difference(
                kind="entry-missing", path=entry, event_index=-1,
                detail="入口文件在新树中不存在"), [entry])
        if _digest(data) != record.entry_digest:
            return QueryResult(entry, True, Difference(
                kind="entry-changed", path=entry, event_index=-1,
                detail="入口文件内容变化"), [entry])

        for idx, event in enumerate(record.events):
            diff = self._check_event(event, idx, tree)
            if diff is not None:
                return QueryResult(entry, True, diff,
                                   self._build_chain(record, idx))
        return QueryResult(entry, False, None, [])

    def query_all(self, tree: SourceTree,
                  options_map: Optional[Dict[str, Sequence[str]]] = None
                  ) -> Dict[str, QueryResult]:
        """对所有已记录入口重查，返回 {入口: 结果}（含无需重编者）。"""
        results: Dict[str, QueryResult] = {}
        for entry in self._records:
            opts = options_map.get(entry) if options_map else None
            results[entry] = self.query(entry, tree, options=opts)
        return results

    def rebuild_set(self, tree: SourceTree,
                    options_map: Optional[Dict[str, Sequence[str]]] = None
                    ) -> Dict[str, QueryResult]:
        """只返回需要重编的入口及其结果。"""
        return {e: r for e, r in self.query_all(tree, options_map).items()
                if r.rebuild}

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _check_event(self, event: IncludeEvent, idx: int,
                     tree: SourceTree) -> Optional[Difference]:
        """按记录的查找顺序在新树上重放一次 include 解析。

        记录中命中项之前的候选当时都不存在；只要其中任何一个现在出现，
        命中路径就会前移——即使字节相同也必须重编（相对查找起点改变）。
        命中项之后的后备文件从未记录，其变化被自然忽略。
        """
        if event.resolved:
            # 记录时命中：命中项之前的候选当时都不存在
            pre_hit = event.candidates[:-1]
            hit: Optional[CandidateRecord] = event.candidates[-1]
        else:
            # 记录时未命中：所有候选当时都不存在，都要检查是否新出现
            pre_hit = event.candidates
            hit = None

        for cand in pre_hit:
            if tree.get(cand.path) is not None:
                return Difference(
                    kind="earlier-candidate-appeared",
                    path=cand.path,
                    event_index=idx,
                    detail=(
                        f"靠前位置新出现同名文件 {cand.path}，"
                        f"命中路径将由 {event.resolved or '(未命中)'} 前移"
                    ),
                )

        if hit is None:
            return None  # 记录时未命中，且新树中仍无候选出现

        new_data = tree.get(hit.path)
        if new_data is None:
            return Difference(
                kind="hit-missing", path=hit.path, event_index=idx,
                detail=f"原命中文件 {hit.path} 在新树中不存在")
        if _digest(new_data) != hit.digest:
            return Difference(
                kind="hit-changed", path=hit.path, event_index=idx,
                detail=f"命中文件 {hit.path} 内容变化")
        return None

    def _build_chain(self, record: EntryRecord, idx: int) -> List[str]:
        """从入口到指定事件的依赖链（人类可读）。"""
        indexes: List[int] = []
        i = idx
        while i != -1:
            indexes.append(i)
            i = record.events[i].parent
        indexes.reverse()

        chain = [f"entry: {record.entry}"]
        for i in indexes:
            ev = record.events[i]
            opener, closer = ('"', '"') if ev.quoted else ('<', '>')
            target = ev.resolved if ev.resolved else "(未命中)"
            chain.append(
                f"{ev.includer}: #include {opener}{ev.name}{closer} "
                f"-> {target}")
        return chain
