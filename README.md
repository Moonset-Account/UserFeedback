# 漏洞台账修复状态判定程序（SEC-20261114-041）

读取本地**有向提交图**、每条维护线**认可的修复标识**、**撤回修复的提交标识**和
**部署构建 ID**，按构建的实际来源提交推导漏洞修复状态，并为每个部署输出
**依据链**与**无法判定的缺失记录**。

仅使用 Python 3 标准库，无第三方依赖；CLI 主入口为 `app.py`。

## 1. 快速开始

```bash
# 文件输入，结果写到标准输出
/usr/local/bin/python3 app.py examples/example_input.json --pretty

# 标准输入
cat examples/example_input.json | /usr/local/bin/python3 app.py

# 输出到文件
/usr/local/bin/python3 app.py examples/example_input.json --output result.json

# 本地核验（unittest + 样例/边界断言，任一失败以非零码退出）
/usr/local/bin/python3 tools/local_verify.py
```

退出码：`0` 成功；`2` 输入结构/引用/环错误；`3` 文件 IO 错误。

## 2. 输入格式（JSON）

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `commits` | 数组 | 有向提交图，边方向为 **子提交 → 父提交**（`parents`） |
| `commits[].id` | 字符串 | 提交标识，全图唯一 |
| `commits[].parents` | 字符串数组 | 父提交标识；合并节点可有多个父，普通节点 0/1 个 |
| `commits[].fix_ids` | 字符串数组 | 该提交携带的修复标识（修复生效事件） |
| `commits[].reverts` | 数组 | 撤回记录：元素可为修复标识字符串，或 `{"fix_id": "F1"}`；`{"fix_id": null}` 表示裸撤回（撤回当前全部生效修复） |
| `maintenance_lines` | 数组 | 维护线定义 |
| `maintenance_lines[].name` | 字符串 | 维护线名称，唯一 |
| `maintenance_lines[].roots` | 字符串数组 | 维护线历史起点（最老提交）；从根沿 children 方向覆盖整条线 |
| `maintenance_lines[].recognized_fix_ids` | 字符串数组 | 本维护线**认可**的修复标识；不被认可的标识不参与状态推导，只记入缺失记录 |
| `builds` | 数组 | 部署构建记录 |
| `builds[].build_id` | 字符串 | 部署构建 ID，唯一 |
| `builds[].line` | 字符串 | 构建所属维护线，构建**只在其维护线上判断** |
| `builds[].source_commit` | 字符串 | 构建实际来源提交 |
| `merge_resolutions` | 数组（可空） | 合并节点的显式解决记录 |
| `merge_resolutions[].commit` | 字符串 | 合并提交标识 |
| `merge_resolutions[].resolved` | `"fixed"` / `"affected"` | 显式裁定结果 |
| `merge_resolutions[].fix_id` | 字符串或 null | 针对的修复标识；null 表示通配该线全部受认可标识 |
| `merge_resolutions[].line` | 字符串或 null | 行级记录只对该维护线生效；null 为全局记录 |

最小示例见 `examples/example_input.json`。

## 3. 判定语义

### 3.1 单修复标识在节点上的状态

| 状态 | 含义 |
| --- | --- |
| `active` | 修复生效 |
| `inactive` | 修复曾生效，当前被撤回 |
| `unknown` | 父线矛盾且无显式解决，或未知状态向下传播 |
| `absent` | 该维护线祖先链上从未出现此修复标识 |

传播规则：

1. 维护线根提交上所有受认可标识初始为 `absent`。
2. 非合并节点继承可达父提交的状态；`fix_ids` 事件置为 `active`（同一标识可多次生效，
   即撤回后恢复修复）；撤回事件把 `active` 置为 `inactive`。
3. 合并节点汇合多个父线：
   - 任一父线为 `unknown` → 合并结果为 `unknown`（原因随状态传播）；
   - 各父线结论不一致（`active` / `inactive` / `absent` 任意混合）→ `unknown`，
     记录 `merge_conflict` 与 `missing_merge_resolution`；
   - 全部一致 → 继承该一致状态。
4. 合并节点存在显式解决记录时，以记录为准并清除矛盾原因；行级记录优先于全局记录。
5. 对处于 `unknown` 的标识执行撤回不会改变状态（事件标记为 `revert_while_unknown`）。

### 3.2 部署级结论（`status`）

对构建所属维护线、且位于来源提交祖先链上的历史逐节点推导，再汇总：

| 结论 | 中文标签 | 规则 |
| --- | --- | --- |
| `affected` | 受影响 | 所有受认可标识在祖先链上从未出现 |
| `fixed` | 已修复 | 无未知，且每个出现过的标识最终均为 `active` |
| `reaffected` | 重新受影响 | 无未知，但至少一个标识最终为 `inactive`（生效后被撤回） |
| `unknown` | 未知 | 任一标识最终为 `unknown`，或来源提交不在维护线可达范围内 |

后续恢复修复（再次出现 `fix_ids` 事件）可把 `reaffected` 再次转回 `fixed`。

### 3.3 输出格式

```json
{
  "schema_version": "1.0",
  "deployments": [
    {
      "build_id": "B-1",
      "line": "LTS",
      "source_commit": "c3",
      "status": "reaffected",
      "status_label": "重新受影响",
      "fix_statuses": {
        "F1": {"state": "inactive", "status": "reaffected", "encountered": true}
      },
      "evidence_chain": [
        {
          "commit": "c0",
          "parents": [],
          "events": [{"type": "baseline", "fix_id": "F1", "result": "absent"}] ,
          "states": {"F1": "absent"}
        }
      ],
      "missing_records": []
    }
  ]
}
```

- `evidence_chain`：来源提交祖先链上的节点，按拓扑序（父先于子）排列；每个节点给出
  父提交、事件（`baseline` / `fix` / `revert` / `merge` / `resolution` / `ignored`）
  与该节点上各标识的状态，形成可追溯的依据链。
- `missing_records`：无法判定或数据不完整的记录，稳定 `code` 取值：
  - `merge_conflict`：合并父线状态矛盾（含各方父提交清单）；
  - `missing_merge_resolution`：矛盾合并点缺少显式解决记录；
  - `unknown_propagated`：未知状态从父节点继续传播；
  - `source_not_reachable_from_line`：来源提交无法从维护线根到达；
  - `unrecognized_fix_record` / `unrecognized_revert_record`：标识不被该维护线认可；
  - `revert_without_prior_fix`：撤回了认可但此前从未生效的标识；
  - `bare_revert_without_active_fix`：裸撤回时没有任何生效中的修复；
  - `resolution_on_non_merge` / `resolution_unrecognized_fix`：解决记录使用异常。

## 4. 需求到测试的映射

测试夹具集中在 `tests/scenarios.py`，使用稳定 `case_id`，单测与核验脚本共用同一份
输入与期望值（不重复维护两套数据）。

| 原始需求要点 | case_id | 断言内容 |
| --- | --- | --- |
| 未修复/已修复/撤回后重新受影响/恢复后再次已修复 | SC-01 | 四个构建的总体与单修复状态、依据链状态序列 |
| 构建只在其维护线上判断 | SC-04、SC-10 | 同一提交在两条线结论不同；行级解决记录不跨线泄漏 |
| 合并多父、矛盾且无显式解决 → 未知 | SC-02、SC-07 | `unknown` + `merge_conflict` + `missing_merge_resolution`，三父合并同样适用 |
| 显式解决记录消除矛盾 | SC-03、SC-10 | `resolution` 事件清除 unknown，结论转 fixed |
| 撤回后恢复可再次已修复 | SC-01、SC-08 | inactive → active 的再生效 |
| 裸撤回撤回全部生效修复 | SC-05 | `{"fix_id": null}` 的处理 |
| 无法判定的缺失记录 | SC-02、SC-06、SC-09 | 缺失记录 code 列表逐条匹配 |
| 不被认可的记录不改变结论 | SC-04、SC-09 | 结论仍为 affected，异常进入 missing_records |
| 有向环 / 悬空父提交拒绝 | SC-11、SC-12 | 解析阶段抛出 InputError |
| 未知沿后继提交传播 | test_unknown_propagates_after_merge | `unknown_propagated` 原因链 |
| CLI 文件/标准输入/退出码 | test_cli.py | 端到端子进程断言 |

## 5. 本地核验与限制

- 运行 ` /usr/local/bin/python3 tools/local_verify.py`：
  1. `unittest.defaultTestLoader.discover("tests")` 发现套件并校验
     `countTestCases() >= 1`；
  2. 实际运行并统计真实执行且未跳过的测试数，全部跳过/零测试/失败/异常均非零退出；
  3. 逐条执行 12 个场景的 18 条样例/边界断言，打印每条 `case_id` 的实际输出与期望值。
- 不使用 pip、venv、pytest 或任何外部库；不发起网络访问；所有输入均为工作区内的
  合成离线夹具。
- 限制：输入规模需能在内存中完整承载；状态判定基于单一 JSON 快照，不读取真实 git
  仓库；解决记录仅支持 `fixed` / `affected` 两种裁定。
