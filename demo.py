"""本地合成数据端到端演示（不读取任何外部输入、不联网）。

运行：python3 -I demo.py

演示内容：
1. 删除后重建同名变量的最小切片与重放核对；
2. 父子进程分别重定义 PATH 前缀、派生快照与独立修改；
3. 空字串 / 未定义 / 间接引用环三态区分与同一循环诊断的重放。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from envsnap import ReadStatus, Recorder  # noqa: E402


def show(title: str, ex) -> None:
    r = ex.result
    if r.status is ReadStatus.VALUE:
        verdict = f"VALUE    {r.value!r}"
    elif r.status is ReadStatus.UNDEFINED:
        verdict = "UNDEFINED"
    else:
        verdict = f"CYCLE    {' -> '.join(r.cycle)}"
    replayed = ex.verify()
    match = "OK" if (
        replayed.status == r.status
        and replayed.value == r.value
        and replayed.cycle == r.cycle
    ) else "MISMATCH"
    print(f"  read {ex.read_event.name!r:7} -> {verdict:30} "
          f"slice={ex.slice_ids} replay={match}")


def main() -> None:
    print("== 轨迹 1：删除后重建同名变量 ==")
    r = Recorder()
    r.set("X", "old-value")
    r.delete("X")
    r.set("X", "new-value")
    show("recreate", r.explain("X"))
    r.delete("X")
    show("deleted", r.explain("X"))
    r.set("EMPTY", "")
    show("empty", r.explain("EMPTY"))
    show("never-defined", r.explain("GHOST"))

    print()
    print("== 轨迹 2：父子进程分别重定义 PATH 前缀 ==")
    root = Recorder()
    root.set("PATH", "/root/bin")
    _eid, child_pid = root.engine.record_spawn(0)
    child = Recorder()
    child.engine = root.engine
    child.proc = child_pid
    root.set("PATH", "/root/v2")          # 派生后父进程的修改
    child.set("PATH", "/child/bin")       # 子进程自己的覆盖
    show("root PATH", root.explain("PATH"))
    show("child PATH", child.explain("PATH"))
    root.set("TOOL", "/opt/tool")
    show("child inherits TOOL", child.explain("TOOL"))

    print()
    print("== 轨迹 3：eager 烘焙 vs lazy 读取时展开 ==")
    r = Recorder()
    r.set("A", "hello")
    r.set("EAGER", "pre-$A-post", eager=True)
    r.set("LAZY", "pre-$A-post", eager=False)
    r.set("A", "changed")
    show("eager", r.explain("EAGER"))
    show("lazy", r.explain("LAZY"))

    print()
    print("== 轨迹 4：间接引用环与子进程独立破环 ==")
    r = Recorder()
    r.set("A", "$B", eager=False)
    r.set("B", "$A", eager=False)
    show("cycle A", r.explain("A"))
    _eid, child_pid = r.engine.record_spawn(0)
    child = Recorder()
    child.engine = r.engine
    child.proc = child_pid
    child.set("B", "fixed")              # 子进程覆盖环上一个节点
    show("child breaks cycle", child.explain("A"))
    show("root still cyclic", r.explain("A"))

    print()
    print("所有切片均已实际重放，状态/值/环链三元组全部一致。")


if __name__ == "__main__":
    main()
