#!/usr/bin/env python3
"""风险复核排班工具 CLI 主入口。

用法：
    python3 app.py run --shift samples/shift_s1.json --state state.json
    python3 app.py run --shift samples/shift_s2.json --state state.json
    python3 app.py sample --workdir /tmp/sched_demo

子命令：
    run     执行一个班次的排班；同事务（shift_id）重试复用首次锁定名单。
    sample  在指定目录运行内置的两班连续样例，展示低频商户组何时被补进。
"""

import argparse
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import store
from scheduler import plan_shift

SAMPLES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples")


def load_shift_spec(path):
    with open(path, encoding="utf-8") as f:
        spec = json.load(f)
    if "shift_id" not in spec:
        raise ValueError("shift spec missing shift_id")
    return spec


def execute_shift(spec, state, force=False):
    """执行一个班次；若该 shift_id 已锁定且未指定 force，则复用首次锁定名单。

    返回 (result, state, reused)。
    """
    shift_id = spec["shift_id"]
    locks = state.setdefault("locks", {})
    if not force and shift_id in locks:
        locked = json.loads(json.dumps(locks[shift_id]))  # 深拷贝，避免调用方改动
        locked["reused_lock"] = True
        return locked, state, True

    result = plan_shift(
        records=spec.get("records", []),
        reserved_slots=spec.get("reserved_slots", 0),
        general_slots=spec.get("general_slots", 0),
        group_caps=spec.get("group_caps", {}),
        carryover=state.get("carryover", []),
        claimed=state.get("claimed", []),
    )
    result["shift_id"] = shift_id
    result["reused_lock"] = False

    # 已被本班领取的交易进入 claimed，后续批次不再进入
    state.setdefault("claimed", []).extend(s["txn_id"] for s in result["selected"])
    # 因组上限落选的记录进入下一班的顺延队列
    state["carryover"] = result["deferred_cap"]
    locks[shift_id] = result
    return result, state, False


def cmd_run(args):
    spec = load_shift_spec(args.shift)
    state = store.load_state(args.state)
    result, state, reused = execute_shift(spec, state, force=args.force)
    store.save_state(args.state, state)
    out = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(out + "\n")
    print(out)
    if reused:
        print("# 事务重试：复用首次锁定的名单 (shift_id=%s)" % spec["shift_id"],
              file=sys.stderr)
    return 0


def _print_shift_summary(result):
    print("班次 %s (reused_lock=%s)" % (result["shift_id"], result["reused_lock"]))
    for s in result["selected"]:
        print("  入选 %-4s 组=%-6s 风险=%d 原因=%s"
              % (s["txn_id"], s["merchant_group"], s["risk_level"], s["reason"]))
    for rec in result["deferred_cap"]:
        print("  落选 %-4s 组=%-6s -> 保留在队列 %s (组上限顺延)"
              % (rec["txn_id"], rec["merchant_group"], rec["queue"]))
    for rec in result["pending"]:
        print("  落选 %-4s 组=%-6s -> 保留在队列 %s (名额耗尽)"
              % (rec["txn_id"], rec["merchant_group"], rec["queue"]))
    for txn in result["excluded_claimed"]:
        print("  过滤 %-4s 已被前一班领取，不再进入本批" % txn)


def cmd_sample(args):
    workdir = args.workdir
    os.makedirs(workdir, exist_ok=True)
    state_path = os.path.join(workdir, "state.json")
    results = []
    for name in ("shift_s1.json", "shift_s2.json"):
        src = os.path.join(SAMPLES_DIR, name)
        dst = os.path.join(workdir, name)
        shutil.copyfile(src, dst)
        spec = load_shift_spec(dst)
        state = store.load_state(state_path)
        result, state, _ = execute_shift(spec, state)
        store.save_state(state_path, state)
        results.append(result)
        with open(os.path.join(workdir, "result_%s.json" % spec["shift_id"]),
                  "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
            f.write("\n")

    print("== 两班连续运行样例 ==")
    for result in results:
        _print_shift_summary(result)
        print()
    rare = [s for r in results for s in r["selected"] if s["merchant_group"] == "rare"]
    if rare:
        print("低频商户组 rare 在班次 %s 被补进 (txn=%s, 原因=%s)"
              % (results[1]["shift_id"], rare[0]["txn_id"], rare[0]["reason"]))
    print("样例产物已写入 %s" % workdir)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="风险复核排班工具")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="执行一个班次的排班")
    p_run.add_argument("--shift", required=True, help="班次输入 JSON 路径")
    p_run.add_argument("--state", required=True, help="本地状态 JSON 路径")
    p_run.add_argument("--out", help="结果输出路径（默认仅打印到 stdout）")
    p_run.add_argument("--force", action="store_true",
                       help="忽略已锁定名单，强制重算（默认重试复用首次锁定名单）")
    p_run.set_defaults(func=cmd_run)

    p_sample = sub.add_parser("sample", help="运行两班连续样例")
    p_sample.add_argument("--workdir", required=True, help="样例产物输出目录")
    p_sample.set_defaults(func=cmd_sample)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
