"""静态链接归档成员选择预演。

模拟链接器对普通对象与归档（静态库）的符号解析过程：

- 普通对象总是选入；
- 归档成员仅当能提供当前未满足的强引用（strong undef）的定义时被抽取；
- 单个归档从头反复扫描，直到一次完整扫描不再抽取任何成员（稳定）；
- 显式归档组把组内归档按序反复扫描直到稳定；组外已经越过的归档不回头；
- 弱引用（weak undef）本身不触发抽取；
- 已选对象中的强定义可替代先前的弱定义；
- 两个强定义并存时报来源冲突（保留先出现的定义）。

输入为记录列表（见 README），输出：选中的成员顺序、最终符号来源、
仍未满足的强引用，以及强定义冲突列表。
"""

from __future__ import annotations

import json
import sys

STRONG = "strong"
WEAK = "weak"

# 符号表条目状态
DEF_STRONG = "def_strong"
DEF_WEAK = "def_weak"
UNDEF_STRONG = "undef_strong"
UNDEF_WEAK = "undef_weak"

_UNDEFINED = (UNDEF_STRONG, UNDEF_WEAK)


def _normalize_syms(entries):
    """把 ["foo", {"name": "bar", "kind": "weak"}] 统一成 (name, kind) 序列。

    裸字符串等价于强符号。
    """
    out = []
    for entry in entries or []:
        if isinstance(entry, str):
            out.append((entry, STRONG))
        else:
            out.append((entry["name"], entry.get("kind", STRONG)))
    return out


class LinkState:
    """链接过程的中间状态。"""

    def __init__(self):
        # name -> {"state": ..., "source": str|None, "referenced_by": [unit, ...]}
        self.symbols = {}
        self.selected = []          # 归档成员抽取顺序（含触发符号）
        self.included_objects = []  # 普通对象（总是选入）
        self.conflicts = []         # 强定义冲突
        self._extracted = set()     # (archive_name, member_name)

    # -- 符号表操作 -----------------------------------------------------

    def _define(self, unit, name, kind):
        sym = self.symbols.get(name)
        if kind == STRONG:
            if sym and sym["state"] == DEF_STRONG:
                self.conflicts.append({
                    "symbol": name,
                    "sources": [sym["source"], unit],
                    "kept": sym["source"],
                })
                return
            refs = sym["referenced_by"] if sym else []
            # 强定义覆盖未定义条目，也替代先前的弱定义
            self.symbols[name] = {
                "state": DEF_STRONG, "source": unit, "referenced_by": refs,
            }
        else:  # 弱定义：仅在尚无定义时占位，不覆盖任何已有定义
            if sym is None or sym["state"] in _UNDEFINED:
                refs = sym["referenced_by"] if sym else []
                self.symbols[name] = {
                    "state": DEF_WEAK, "source": unit, "referenced_by": refs,
                }

    def _reference(self, unit, name, kind):
        sym = self.symbols.get(name)
        if sym is None:
            self.symbols[name] = {
                "state": UNDEF_STRONG if kind == STRONG else UNDEF_WEAK,
                "source": None,
                "referenced_by": [unit],
            }
        elif sym["state"] in _UNDEFINED:
            if kind == STRONG:
                sym["state"] = UNDEF_STRONG  # 强引用把弱未定义升级为强未定义
            if unit not in sym["referenced_by"]:
                sym["referenced_by"].append(unit)
        # 已有定义：引用直接被满足，无需记录

    def include_unit(self, unit, defines, undefs):
        """把一个编译单元（普通对象或被抽取的归档成员）并入符号表。"""
        for name, kind in _normalize_syms(defines):
            self._define(unit, name, kind)
        for name, kind in _normalize_syms(undefs):
            self._reference(unit, name, kind)

    # -- 归档扫描 -------------------------------------------------------

    def _member_trigger(self, member):
        """若该成员能满足某个当前未满足的强引用，返回触发符号名，否则 None。

        弱引用不触发抽取。
        """
        for name, kind in _normalize_syms(member.get("defines")):
            if kind != STRONG:
                continue
            sym = self.symbols.get(name)
            if sym and sym["state"] == UNDEF_STRONG:
                return name
        return None

    def _scan_archive_once(self, archive):
        """按成员顺序扫描归档一遍，抽取可满足的成员；返回是否有抽取。"""
        extracted_any = False
        for member in archive.get("members", []):
            key = (archive["name"], member["name"])
            if key in self._extracted:
                continue
            trigger = self._member_trigger(member)
            if trigger is None:
                continue
            self._extracted.add(key)
            unit = "%s(%s)" % (archive["name"], member["name"])
            self.selected.append({
                "archive": archive["name"],
                "member": member["name"],
                "trigger": trigger,
            })
            self.include_unit(unit, member.get("defines"), member.get("undefs"))
            extracted_any = True
        return extracted_any

    def process_archive(self, archive):
        """单个归档：从头反复扫描直到稳定。"""
        while self._scan_archive_once(archive):
            pass

    def process_group(self, archives):
        """显式归档组：组内归档按序反复扫描直到稳定。

        组处理完毕后，组外之前已经越过的归档不会被重新扫描。
        """
        changed = True
        while changed:
            changed = False
            for archive in archives:
                if self._scan_archive_once(archive):
                    changed = True


def link(inputs):
    """按顺序处理输入记录，返回链接报告。"""
    state = LinkState()
    for record in inputs:
        rtype = record.get("type")
        if rtype == "object":
            state.included_objects.append(record["name"])
            state.include_unit(
                record["name"], record.get("defines"), record.get("undefs"))
        elif rtype == "archive":
            state.process_archive(record)
        elif rtype == "group":
            state.process_group(record.get("archives", []))
        else:
            raise ValueError("未知记录类型: %r" % (rtype,))
    return build_report(state)


def build_report(state):
    symbols = {}
    unsatisfied_strong = {}
    unsatisfied_weak = {}
    for name in sorted(state.symbols):
        sym = state.symbols[name]
        if sym["state"] == DEF_STRONG:
            symbols[name] = {"kind": STRONG, "source": sym["source"]}
        elif sym["state"] == DEF_WEAK:
            symbols[name] = {"kind": WEAK, "source": sym["source"]}
        elif sym["state"] == UNDEF_STRONG:
            unsatisfied_strong[name] = list(sym["referenced_by"])
        else:
            unsatisfied_weak[name] = list(sym["referenced_by"])
    return {
        "included_objects": list(state.included_objects),
        "selected_members": list(state.selected),
        "symbols": symbols,
        "unsatisfied_strong_undefs": unsatisfied_strong,
        "unsatisfied_weak_undefs": unsatisfied_weak,
        "conflicts": list(state.conflicts),
    }


def main(argv):
    if len(argv) != 2:
        sys.stderr.write("用法: python3 linker_sim.py <input.json>\n")
        return 2
    with open(argv[1], "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    report = link(doc["inputs"])
    json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
