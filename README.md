# SEC-VULN-039 — 服务配置权限离线诊断工具

安全部门发现服务配置从共享目录迁出后，定期检查仍报告普通账号具备写入能力。
本工具是一个 **纯 Python 3 标准库、完全离线** 的诊断器，分析三类合成记录：

1. 服务配置清单（inventory）
2. 主体 / 用户组关系（subjects，含嵌套组与成员变化版本）
3. 目录权限快照（snapshot，含未导出对象）

并分别判断每个普通（非特权）账号：

- **能否修改文件内容**（`modify_file`：对文件本身的写权限）
- **能否通过父目录替换条目**（`replace_entry`：对父目录的写权限）

所有路径、账号、组均为虚构数据；工具不访问任何真实主机、云服务或身份系统。

## 1. 目录结构

```
app.py                     # CLI 主入口（analyze / diff / remediate）
acl_diag/
  models.py                # ACE、清单、主体目录、快照等数据模型与常量
  membership.py            # 组成员（含嵌套组）向上展开
  evaluator.py             # 权限收集、优先级判定、缺失记录处理
  diff.py                  # 新旧结论差异（按 服务/路径/主体/动作）
  remediation.py           # 最小整改建议、修复后快照、重新核验
  io_utils.py              # JSON 读写
tests/                     # unittest 测试（36 个，标准库）
examples/scenario_a/       # 合成输入夹具（含成员变化 v1/v2）
tools/local_verify.py      # 本地核验包装器（强制真实执行测试）
```

## 2. 输入格式

### 2.1 主体 / 组关系 `subjects.json`

```json
{
  "version": "synthetic-subjects-v1",
  "accounts": {
    "acc_bob": {"name": "Bob Support (fictional)", "privileged": false}
  },
  "groups": {
    "grp_legacy_share": {
      "name": "Legacy Shared-Drive Writers (fictional)",
      "members": ["acc_bob", "grp_oncall"]
    }
  }
}
```

- 组可以嵌套（组成员是另一个组），成员关系按传递闭包向上展开。
- `privileged=true` 的账号是特权账号，不纳入普通账号审计范围。

### 2.2 权限快照 `snapshot.json`

```json
{
  "version": "synthetic-snapshot-v1",
  "objects": {
    "/srv/fictional/billing": {
      "type": "directory",
      "exported": true,
      "aces": [
        {"ace_type": "allow", "principal": "acc_alice",
         "rights": ["replace_entry"], "inherited": false}
      ],
      "inheritable_aces": [
        {"ace_type": "allow", "principal": "grp_legacy_share",
         "rights": "all", "inherited": false}
      ]
    },
    "/srv/fictional/billing/billing.conf": {
      "type": "file",
      "exported": true,
      "aces": []
    }
  }
}
```

- `ace_type`：`allow` 或 `deny`；`rights`：`modify_file`、`replace_entry`
  或 `"all"`。
- `aces`：直接挂在对象上的条目；`inherited=true` 表示物化的继承条目。
- `inheritable_aces`：目录上的可继承授权，评估后代时自动作为继承条目；
  评估目录自身（条目替换）时作为该目录的显式候选。
- `exported=false` 表示该对象的权限未导出（证据缺口）。

### 2.3 服务配置清单 `inventory.json`

```json
{
  "services": [
    {
      "service": "svc_billing",
      "path": "/srv/fictional/billing/billing.conf",
      "legacy_conclusions": [
        {"subject_id": "acc_bob", "action": "modify_file",
         "conclusion": "allow", "basis": "旧周期检查依据（自由文本）"}
      ]
    }
  ]
}
```

`legacy_conclusions.conclusion` 取值 `allow | deny | unverified`。

## 3. ACL 优先级模型与缺失记录处理

对每一项权利（`modify_file` / `replace_entry`）独立判定，优先级从高到低：

| 优先级 | 规则 | 来源 |
| --- | --- | --- |
| 1（最高） | 显式拒绝 explicit deny | 直接挂在目标对象上 |
| 2 | 显式允许 explicit allow | 直接挂在目标对象上 |
| 3 | 继承拒绝 inherited deny | 父目录继承 |
| 4（最低） | 继承允许 inherited allow | 父目录继承 |

因此 **显式拒绝必然覆盖继承允许**；同来源内拒绝优先于允许。

缺失记录一律判 **`unverified`（待核实）**，绝不臆造默认 allow/deny：

- 目标路径不在快照中，或 `exported=false`；
- `replace_entry` 的父目录缺失或未导出（即“目录权限未导出”场景）；
- 主体在账号清单中不存在（身份无法解析）；
- 对象已导出但没有任何适用于该主体、该动作的 ACE。

每条结论都附带 `reasons`（依据）与命中/候选 ACE 列表，可逐条追溯。

## 4. 两个判定问题

- `modify_file`：以配置文件自身为权限目标。
- `replace_entry`：以 `dirname(path)` 父目录为权限目标，模拟“删旧文件、
  放新文件”式的目录项替换——即使文件本身只读，父目录可写仍可替换。

## 5. CLI 用法（可重跑入口）

```bash
# 全量分析：每个普通账号对每项配置的两项权利
/usr/local/bin/python3 app.py analyze \
  --inventory examples/scenario_a/inventory.json \
  --subjects  examples/scenario_a/subjects_v1.json \
  --snapshot  examples/scenario_a/snapshot.json

# 新旧结论差异（服务/路径/主体/动作 + 双方依据）
/usr/local/bin/python3 app.py diff   ...同上参数...

# 最小整改 + 修复后快照 + 重新核验
/usr/local/bin/python3 app.py remediate ...同上参数... \
  --fixed-snapshot /path/to/fixed_snapshot.json --fail-on-risk
```

退出码：`0` 成功；`1` `--fail-on-risk` 下整改后普通账号仍有 allow；
`2` 输入缺失或格式错误。

### 整改规则（最小化）

- 允许来自**继承** ACE → 在目标对象上为该账号加一条**显式拒绝**
  （显式拒绝覆盖继承允许，祖先目录不动）。
- 允许来自目录自身的**可继承授权**（可能服务其他子项）→ 在该目录上加
  显式拒绝，而不是删除授权。
- 允许来自对象上的**普通显式允许** ACE → 仅移除该 ACE。
- 待核实项不做 ACE 变更，列入 `unresolved`，要求先补全权限导出。

`reverification` 对修复后快照重跑完整分析，逐行给出
`post_fix_decision` 与 `still_allowed`。

## 6. 合成场景与原始要求的映射

| 原始要求 | 场景 / case_id | 测试位置 |
| --- | --- | --- |
| 组成员变化 | v1→v2 中 bob 被移出 `grp_legacy_share` / `grp_ops_staff`；CASE-01 | `tests/test_membership.py::test_membership_change_removes_group_match`、`tests/test_evaluator.py::test_billing_denied_after_membership_removal` |
| 显式拒绝覆盖继承允许 | carol 对 `authd.yaml`；CASE-02 | `tests/test_evaluator.py::test_explicit_deny_overrides_inherited_allow` |
| 普通文件只读但目录项可替换 | alice 对 `routes.conf`；CASE-03 | `tests/test_evaluator.py::test_readonly_file_but_directory_entry_replaceable` |
| 目录权限未导出 | ledger 父目录 `exported=false`；CASE-04 | `tests/test_evaluator.py::test_unexported_parent_directory_is_unverified` |
| 缺失记录待核实 | 缺失路径、未知主体、无适用 ACE；CASE-05 | `tests/test_evaluator.py::test_missing_file_is_unverified_not_deny` 等 |
| 新旧结论差异 | diff 8 行，4 行翻转；CASE-06 | `tests/test_diff.py` |
| 最小整改 + 修复快照 + 重新核验 | CASE-07，残留 allow=0，证据缺口保留待核实 | `tests/test_remediation.py` |

## 7. 本地核验

```bash
/usr/local/bin/python3 tools/local_verify.py
```

包装器行为（不依赖 unittest “零测试也退出 0” 的行为）：

1. 用 `unittest.defaultTestLoader.discover("tests")` 取套件并检查
   `countTestCases() >= 1`；
2. 实际运行，统计**真实执行且未跳过**的测试数，必须 >= 1；任何失败、
   错误、异常、零测试、全部跳过均以非零退出；
3. 额外执行 7 条样例/边界断言（CASE-01…CASE-07），逐条记录输入、
   期望值、实际值与通过情况，写入 `tools/local_verify_report.json`。

## 8. 限制

- 仅处理合成 JSON；不解析真实文件系统 ACL、不做网络调用。
- 路径按 POSIX 风格字符串处理，仅用于父子关系推导。
- 待核实是证据状态而非安全结论：整改报告明确要求先补全导出，
  不允许用新增 ACE 掩盖证据缺口。
- 仅使用 Python 3 标准库，无第三方依赖、无需 pip/venv。
