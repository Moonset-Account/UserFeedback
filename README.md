# 化名登记模块 (Pseudonym Registry)

为不同合作方发放各自的用户化名，支持账号合并与误合并后的原始身份拆分。

- 纯 Python 3.11 标准库实现，无第三方依赖。
- 核心模块：`pseudonym_registry/core.py`。
- 演示脚本：`demo.py`。
- 测试：`tests/test_*.py`（`unittest`）。

## 快速开始

```bash
python3 demo.py
python3 -m unittest discover -s tests -v
```

演示依次完成：两个合作方各自看到不同化名 → 两人被误合并 → 两边同时登记 →
拆分 → 重放各方在登记时看到的身份 → 导出两个合作方的数据并证明无共同标识。

## 概念与保证

| 概念 | 说明 |
| --- | --- |
| 原始主体号 `subject_id` | 身份源给出的稳定编号，每次登入都携带；只出现在运营侧审计视图中。 |
| 合作方 `partner` | 化名的接收方。合作方之间互相隔离。 |
| 化名 `handle` | `psn_` 前缀的不透明字符串，一旦发放**永不改名**。 |
| 内部账号 `acct_...` | 仅登记模块内部使用，绝不导出。 |
| 身份簇 `grp_...` | 一个或多个被合并账号组成的内部身份；仅内部使用。 |
| 绑定 `Binding` | 合作方持有的一个化名，携带创建它的原始主体号（`initiator`）作为归属凭据。 |

保证：

1. **化名稳定**：同一合作方对同一原始主体的化名在合并、拆分前后保持不变。
2. **化名隔离**：不同合作方对同一主体得到不同化名；任一导出中只有该合作方自己的化名。
3. **导出无共同编号**：`export_for_partner` / `replay` 只含本合作方的不透明化名与
   通用状态/事件词（如 `active`、`registered`、`identity-merged`），不含
   `acct_`/`grp_`/`sub_` 内部编号、不含原始主体号、不含全局流水号。因此任两个
   合作方的导出**没有任何共同标识**。
4. **合并期间新建绑定记录发起主体**：账号处于合并状态时，某合作方首次登记会创建
   绑定并记录发起登入的原始主体号（`initiator`）。
5. **拆分只向相应主体恢复**：拆分时每个活动绑定只回到创建它的原始主体；在合并前
   没有自己绑定的主体，拆分后重新登入会得到新化名。
6. **无法归属的旧式绑定暂停**：`import_legacy_binding` 导入的无来源绑定在拆分时
   被**暂停**（`status="suspended"`），不会被猜测归给任何一方；只能由运营显式
   `adopt_legacy` 后恢复。
7. **并发安全**：所有公开方法持有同一把可重入锁；两个合作方同时登记不会重复发号
   或丢失绑定。

## 公开 API

所有方法都是线程安全的。异常语义：参数非法 → `ValueError`；未知主体/账号/化名 →
`KeyError`。

### 登入与登记

```python
PseudonymRegistry()
login(partner, institution, subject_id) -> str
register(partner, institution, subject_id) -> dict
enroll(subject_id, institution="") -> str   # 返回内部账号 id（运营用）
```

- `login` 返回该合作方看到的化名；主体不存在时自动登记。
- `register` 与 `login` 同规则，额外返回 `created_seq` / `created_phase` / `status`。
- 每次登入记录来源机构与原始主体号。
- `subject_id` 不能为空，也不能以 `acct_`/`grp_`/`psn_`/`sub_` 开头（防止与内部
  编号混淆）。

### 合并与拆分

```python
merge(subject_a, subject_b) -> str          # 返回内部簇 id（运营用）
split(subject_id) -> SplitReport
```

- `merge` 让两个原始主体解析为同一内部身份；已发放的化名不改名，并保留各自
  `initiator`。合并期间该身份的某合作方只看到一个化名（最早发放的归属化名）。
- `split` 返回 `SplitReport`：

```python
SplitReport.subject_ids   # 参与拆分的原始主体号
SplitReport.members       # 内部账号 id（运营用）
SplitReport.restored      # {原始主体号: [恢复给它的化名]}
SplitReport.suspended     # 被暂停的旧式化名
```

### 旧式绑定

```python
import_legacy_binding(partner, internal_id, institution="legacy") -> str
adopt_legacy(partner, handle, subject_id) -> None
```

- `import_legacy_binding` 导入无来源绑定；对同一 partner/账号幂等，若已有常规绑定
  则报 `ValueError`。
- `adopt_legacy` 由运营在带外核对后显式归属；登记模块自身从不猜测。

### 沿革解释

```python
explain(partner, handle) -> dict              # 运营视图：含 initiator、主体号、事件序列
explain_for_partner(partner, handle) -> dict  # 合作方视图：handle/status/timeline
handle_for(partner, subject_id) -> str | None
```

`explain` 的事件序列完整记录 `issued`、`merged`、`seen`、`restored`、`suspended`、
`adopted` 等，用于解释“曾经给同一合作方见过的化名”的沿革。

### 合作方导出与重放

```python
export_for_partner(partner) -> list[dict]   # [{handle, status, lineage}]
export_json(partner) -> str
replay(partner, only_registrations=False) -> list[dict]
```

- `export_for_partner` 字段固定为 `handle` / `status` / `lineage`。
- `replay` 每行 `{step, action, handle, phase, institution}`；`step` 是本合作方内
  的序号，`handle` 是该时刻合作方实际看到的化名。`only_registrations=True` 只返回
  发放新化名的时刻。

### 运营查询

```python
active_handles(partner) -> list[str]
suspended_handles(partner) -> list[str]
account_id(subject_id) -> str
subjects() -> list[str]
merged_subjects() -> list[list[str]]
audit_log() -> list[dict]   # 含主体号与内部编号，禁止交给合作方
```

> `audit_log`、`explain`、`SplitReport.members`、`account_id` 含内部信息，只供
> 运营使用，不得进入合作方导出。

## 语义细节

### 合并期间的可见化名

同一合作方在合并身份上只看到一个化名（最早发放、且带归属的化名）。若该合作方在
合并前已分别认识两人，则它持有两个化名：较早者为活动（`active`），较晚者在合并
期间折叠（`merged`），拆分后各自回到所属主体并恢复 `active`。

### 并发登记

合并期间两个合作方同时登记时，各自创建一条绑定；锁保证不会把同一化名发给两个
合作方，也不会丢失任何一次登记。每条绑定都记录其 `initiator`，拆分时各归其主。

### 误合并与拆分

拆分后：

- 有归属凭据的绑定只回到 `initiator` 对应的原始主体；
- 无凭据（`legacy`）绑定被暂停，等待显式 `adopt_legacy`；
- 原先没有自己绑定的主体，下次登入得到新化名。

## 验证范围

`python3 -m unittest discover -s tests -v` 覆盖：

- `tests/test_login.py`：化名稳定性、合作方隔离、登记幂等、参数校验、`subject_id`
  与内部编号前缀冲突。
- `tests/test_merge_split.py`：合并可见性、可逆性、两方/三方合并与拆分、合并期间
  绑定归属、拆分只回所属主体、非法输入。
- `tests/test_legacy.py`：旧式绑定的保留、拆分暂停、重新登入不误用、运营显式采纳、
  幂等与互斥。
- `tests/test_export.py`：导出字段白名单、两合作方导出无共同标识、无内部/主体号、
  状态随拆分变化、replay 的合作方隔离与有序性。
- `tests/test_lineage.py`：单条/合并/拆分化名的完整沿革、折叠状态、合作方视图隐藏
  主体号。
- `tests/test_concurrency.py`：两合作方同时登记、同一合作方并发登入只发一个化名、
  拆分后并发重新登记互不影响。
- `tests/test_demo.py`：演示脚本可执行且满足场景断言。

边界与交错场景均由本地合成数据驱动，不访问网络、不读取凭据、不安装依赖。
