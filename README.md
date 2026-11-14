# 分块加密快照的验证后原子恢复工具

纯 Python 3 标准库实现（AES-256-GCM 为本地实现，无第三方依赖）。CLI 主入口为 `app.py`。

## 运行入口

```bash
# 创建分块加密备份（生成清单、分块和按租户组织的密钥记录）
/usr/local/bin/python3 app.py backup \
    --tenant acme --snapshot-id snap-001 --key-id k1 \
    --source /path/to/file --chunks-dir ./chunks --keys-dir ./keys \
    --chunk-size 65536

# 验证并原子恢复（全部验证通过后才替换目标文件）
/usr/local/bin/python3 app.py restore \
    --tenant acme --snapshot-id snap-001 \
    --target /path/to/target --chunks-dir ./chunks --keys-dir ./keys

# 一键本地核验（单元测试 + 本题样例/边界断言）
/usr/local/bin/python3 tools/local_verify.py
```

## 输入格式

```
chunks/<snapshot_id>/manifest.json   # 清单
chunks/<snapshot_id>/<任意名>.json    # 每个分块一个文件
keys/<tenant>/<key_id>.json          # 按租户组织的密钥记录
```

- **清单**：`tenant`、`snapshot_id`、`key_id`、`total_chunks`（总块数）、`original_length`（原文件长度）。
- **分块**：`index`（序号）、`nonce`（12 字节，base64）、`ciphertext`（base64）、`tag`（16 字节 AES-256-GCM 认证标签，base64）。
- **密钥记录**：`key_id`、`key_hex`（32 字节 AES-256 密钥）、`status`（`active` / `decrypt-only` / `revoked`）。

每个分块的 GCM 附加认证数据（AAD）绑定清单全部字段与块序号，因此清单字段和块顺序都被认证。分块文件可以乱序、任意命名，以文件内的 `index` 为准，输出按序号拼接。

## 输出语义

- 恢复成功：标准输出 `restore ok: <N> bytes written ...`，退出码 0；目标文件通过同目录临时文件 + `os.replace` 原子替换。
- 恢复失败：标准错误输出 `restore failed: <原因>`，退出码 1；**目标文件保持原样**，恢复产生的临时文件被清理。
- 可检测的失败原因：重块（duplicate chunk）、缺块（missing chunks）、越界序号（unexpected chunk index）、认证失败（authentication failed，含清单被篡改）、还原长度不符（length mismatch）、租户/快照不匹配、密钥缺失或被吊销（revoked）。
- 历史快照：旧密钥在对应租户下仍为 `active` 或 `decrypt-only` 时可用于解密旧快照；`revoked` 或缺失则拒绝。

## 需求到测试/样例的映射

| 需求 | 验证位置 |
| --- | --- |
| AES-256-GCM 实现正确性 | `tests/test_restore.py::AesGcmVectorTests`（FIPS-197 与 NIST GCM 公开向量） |
| 乱序输入按序号拼接 | `RestoreSuccessTests::test_out_of_order_and_odd_filenames` |
| 历史快照用仍获准的旧密钥 | `RestoreSuccessTests::test_historical_snapshot_uses_still_authorized_old_key` |
| 重块/缺块/越界序号 | `RestoreFailureTests::test_duplicate_chunk` / `test_missing_chunk` / `test_unexpected_extra_chunk_index` |
| 认证失败（密文、标签、清单篡改） | `test_corrupted_last_chunk` / `test_tampered_tag` / `test_tampered_manifest_fails_authentication` |
| 还原长度不符 | `RestoreFailureTests::test_restored_length_mismatch` |
| 失败时给出原因、保留目标、清理临时文件 | `RestoreFailureTests::assert_failed_cleanly`（所有失败用例共用断言） |
| 验证通过后原子替换 | `RestoreSuccessTests` + `_atomic_replace`（同目录临时文件 + fsync + `os.replace`） |
| 同一目标两次恢复演示（先坏后好） | `TwoPhaseDemoTest::test_corrupt_then_intact_restore_on_same_target` 及 `tools/local_verify.py` 的 `DEMO-1`/`DEMO-2` 样例断言 |

## 限制

- 仅支持 96 位 nonce、128 位标签的 AES-256-GCM；纯 Python 实现未做侧信道防护，适合离线核验与中小文件。
- 整个快照在内存中完成解密与拼接后再落盘，超大文件受内存限制。
- 密钥以本地 JSON 记录模拟"按租户组织的密钥目录"，`status` 字段模拟密钥授权状态；不连接任何真实 KMS/云服务。
