# ACL 建模：共享目录中新文件的继承与有效权限预演

纯 Python 3.11 的进程内模型，用于**预演**（不触碰真实文件系统）共享目录里新建文件最终对哪些主体可读、可写、可执行。
模型对应 POSIX ACL（`acl(5)`）的三段语义：**创建继承**、**访问判定**、**chmod 对组类位的处理**。

所有数据都是本地合成夹具；不读取真实 ACL、不调用特权系统调用、不联网。

## 实际入口

| 用途 | 入口 |
| --- | --- |
| 库 API | `acl_model.py`（`parse_acl` / `create_acl` / `create_file` / `AclFile.evaluate` / `AclFile.chmod` / `AclFile.observe`） |
| 命令行演示 | `.venv/bin/python acl_model.py [--default-acl TEXT \| --no-default-acl] [--umask 022] [--mode 750] [--chmod 700]` |
| 验收测试 | `.venv/bin/python -m unittest discover -s tests -t . -v` |

`tests/__init__.py` 让 `tests` 成为可导入包，因此 `-t .` 从仓库根目录发现用例。

### 命令行示例

```console
$ .venv/bin/python acl_model.py --mode 740 --chmod 700
Parent default ACL:
default:user::rwx
default:user:alice:rwx
default:user:bob:rw-
default:group::r-x
default:group:devs:rwx
default:mask::rwx
default:other::---

Created ACL (requested mode 740):
user::rwx
user:alice:rwx
user:bob:rw-
group::r-x
group:devs:rwx
mask::r--
other::---
...
```

退出码：`0` 正常，`2` 表示 ACL 文本 / 模式非法（不会部分应用）。

## 建模的规则

### 1. 创建

输入：父目录默认条目、创建请求的属主/组类/其他三组权限、调用方身份。

- **父目录有默认条目**：按请求分别裁剪
  - `user::` ← 默认 `user::` & 请求属主位
  - `mask::` ← 默认 `mask::` & 请求组类位（无 `mask::` 的极简默认 ACL 则裁剪 `group::`）
  - `other::` ← 默认 `other::` & 请求其他位
  - 点名用户（`user:NAME:`）、点名组（`group:NAME:`）、`group::` 保留默认值，**不受请求裁剪**
  - 该路径下**忽略进程 umask**（与 `acl(5)` 一致）
- **无默认条目**：请求权限先按进程 umask 去掉对应位，再作为极简访问 ACL（无 `mask::`、无点名条目）。

### 2. 访问判定（优先级严格递减）

1. `uid == 文件属主` → `user::`，**不受 mask 限制**。属主即使也被点名为用户、也在命中组里，仍走属主规则。
2. `uid` 命中点名用户 → `user:NAME:` **与 mask 求交**。
3. 命中组：文件所属组 + 所有命中的点名组，权限**求并集**，再**与 mask 求交**。命中任一即走此规则，不回落到 `other`。`Subject.primary_group` 会并入 `groups` 一起参与匹配。
4. 以上都未命中 → `other::`，**不受 mask 限制**。

`AclFile.evaluate()` 返回 `Evaluation(rule, raw, effective, matched_groups, mask)`，把"哪条规则生效、原始权限、最终权限"一起暴露出来，便于核对边界。

### 3. 后续改「组类位」（chmod）

- ACL **有扩展 mask**（存在 `mask::`）→ 只改 `mask::`，`group::` 与点名条目保持原值。
- **普通文件**（极简 ACL，无 `mask::`）→ 改 `group::`。
- 两种情况都会同时更新 `user::` 与 `other::`。

因此 `chmod` 会把**所有非属主、非 other 的主体**（点名用户、所属组、点名组）一起抬升或压低，而属主与 other 不受组类位影响 — 这正是 `observe()` 用来观察的联动效应。

### 4. 多主体联动观察

`AclFile.observe(mapping | iterable)` 一次返回 `{标签: 有效权限}`；重复标签会抛 `AclError`，避免预演里静默覆盖某个主体。

## 验证范围（`tests/test_acl_model.py`，57 个用例）

| 领域 | 覆盖点 |
| --- | --- |
| 解析 | `rwx` / `r-x` / `r` / 八进制、`u:`/`g:`/`m:`/`o:` 短标签、`default:` 前缀、`format()` 往返 |
| 拒绝（不可接受输入） | 缺必需条目、重复条目、未知标签、无 mask 的点名条目、空点名、混合 `default:` 与访问条目、非法权限文本/八进制、越界模式、`chmod` 非法值不改变状态 |
| 创建 | 默认 ACL 裁剪属主/mask/other，点名条目与 `group::` 保留；请求组类为 0 时 mask 归零但点名条目仍在；极简默认 ACL 裁剪 `group::`；无默认时 umask 生效；**默认 ACL 存在时 umask 被忽略** |
| 访问 | 属主优先于点名用户；点名用户受 mask 限制；点名用户优先于组；多组并集后受 mask 限制；owning group 单独命中；未命中回落 other；other/mask 不受对方影响；mask=0 时属主与 other 仍可用 |
| chmod | 扩展 ACL 只改 mask；极简 ACL 改 `group::`；多个主体随组类位一起变化；非法 chmod 不产生部分修改 |
| 交叉约束 | 创建请求 → 后续点名用户可达权限；属主兼点名用户兼组成员仍走属主；三类主体（点名用户/所属组/点名组）在多次 chmod 中同向联动；点名用户不会"继承"组并集 |

每个行为都至少有一个**可接受**用例、一个**应被拒绝**用例，以及同时影响两者的**边界/交错**用例（例如 `mask` 归零、请求组类位为 0、`other` 与 `mask` 分离、连续两次 chmod 的抬升/压低）。

## 已知限制

- 仅建模 `r/w/x` 三位，不含 ACL 的 `X`/`s`/`t` 语义与 CAP_DAC_OVERRIDE 等内核特权旁路。
- 属主字段是任意可哈希标签（如字符串），不是真实 UID 解析；`Subject.primary_group` 只是并进组成员集合，不做 NSS 解析。
- 创建时"调用方身份"由调用方显式传入 `owner`/`group`（来自 `os.getuid()` 之类的外部信息），模型本身不查询系统账号。
- 仅在本地合成夹具上验证；不读取、不写入真实文件系统 ACL。
