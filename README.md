# 登录验证与哈希参数升级组件

兼容旧参数口令哈希的登录模块：按记录自身携带的算法与参数版本验证口令，
一次成功验证后透明升级为本地配置的新参数，并把算法、参数版本持久化到本地
JSON 模拟存储。全部实现仅使用 Python 3.11 标准库。

## 目录结构与实际入口

```
login/
  __init__.py      # 对外统一导出入口（LoginService / JsonFileCredentialStore 等）
  exceptions.py    # 登录模块异常类型
  hashing.py       # PBKDF2-HMAC-SHA256 哈希、参数版本 v1/v2、恒定时间比较
  storage.py       # CredentialRecord 与 JSON 文件存储（原子写 + CAS 乐观锁）
  service.py       # LoginService：验证、升级、改口令、停用；LoginTracer 钩子
tests/
  test_login.py    # unittest 验收用例（11 个）
```

主要编程入口：

- `LoginService(store, tracer=None)`
  - `register(user_id, password, param_version="v2", disabled=False)`
  - `login(user_id, password) -> Session`（验证 + 必要时升级）
  - `change_password(user_id, new_password)`（推进 `record_version`）
  - `set_disabled(user_id, disabled)`
- `JsonFileCredentialStore(base_dir)`：每用户一个 JSON 文件，
  `compare_and_swap` 按 `record_version` 做乐观锁写入。
- 持久化字段：`algorithm`、`param_version`、`iterations`、`salt_hex`、
  `hash_hex`、`disabled`、`record_version`、`updated_at`。

## 运行方式

在工作区根目录执行：

```bash
python3 -I -m unittest discover -s tests -v
# 或
python3 -I tests/test_login.py
```

`-I` 为隔离模式，避免当前目录下的同名模块污染导入。

## 参数版本

| 版本 | 算法 | 迭代次数 | 角色 |
| --- | --- | --- | --- |
| `v1` | PBKDF2-HMAC-SHA256 | 50,000 | 旧参数（兼容验证） |
| `v2` | PBKDF2-HMAC-SHA256 | 200,000 | 本地当前参数（`CURRENT_PARAM_VERSION`） |

## 题面约束与实现/验证对应关系

1. **兼容旧哈希，成功验证后升级新参数**
   - 验证使用记录自身的 `algorithm`/`param_version`；登录临界区内若版本低于
     当前配置，重新生成新盐与新哈希并 CAS 落盘，`record_version` 加一。
   - 用例：`test_legacy_hash_login_succeeds_and_upgrades_once`
     （同时断言 JSON 文件中持久化了算法与参数版本，第二次登录不再升级）。
2. **验证期间改口令：旧口令不能建立会话，旧升级不得覆盖新记录**
   - 先验证后取锁；锁内重读并比较 `record_version`，版本变化时对新记录重新
     验证当前口令，不匹配即抛 `CredentialModifiedDuringLogin`；存储层 CAS 在
     版本不符时拒绝写入，作为最后一道防线。
   - 用例：`test_password_changed_during_verify_old_password_rejected`、
     `test_cas_rejects_stale_upgrade_and_never_overwrites_new_record`。
3. **两个并发登录只升级一次**
   - 每用户 `RLock` 串行化升级临界区；第二个登录锁内重读发现已是当前参数即
     跳过升级。用计数 tracer 断言升级提交恰好一次。
   - 用例：`test_concurrent_logins_upgrade_exactly_once`。
4. **账户停用稳定拒绝**
   - 验证前与升级临界区内各检查一次 `disabled`；重复尝试结果一致，且停用
     状态下不产生升级写入。
   - 用例：`test_disabled_account_is_stably_rejected`、
     `test_disabled_during_verify_is_rejected`（验证后停用的交错场景）。
5. **升级中断后可用有效记录重试**
   - 升级阶段异常统一转为 `UpgradeInterrupted`；存储采用
     “临时文件 + `os.replace`”原子写，中断不产生半成品，旧记录保持可用。
   - 用例：`test_upgrade_interruption_keeps_old_record_and_retry_succeeds`。

## 验收用例分类（tests/test_login.py，共 11 个）

- 可接受结果：旧哈希登录并升级、并发登录单次升级、中断后重试成功、
  跨实例持久化读取、当前参数记录免升级。
- 应阻止/拒绝：错误口令（记录不变）、停用账户稳定拒绝、验证期间改口令后
  旧口令被拒、过期版本 CAS 被拒、未知用户与非法 ID。
- 边界/交错：验证通过后改口令、验证通过后停用、CAS 版本竞争、原子写无
  临时文件残留。

## 范围说明

本组件为本地防御性实现与合成夹具验证，不包含任何真实目标探测、网络访问或
攻击载荷；存储目录由调用方通过 `JsonFileCredentialStore(base_dir)` 指定，
测试使用 `tempfile` 临时目录并在用例结束后清理。
