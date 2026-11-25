# 盲签遥测凭据演示

一个仅使用 Python 3.11 标准库的本地合成实现：已登记设备每个周期获得一份盲签凭据，采集端只用 RSA 公钥验证并按 `(period, ticket)` 原子核销一次报告。

> **安全边界说明：** 默认密钥是教科书式演示 RSA 参数 `p=61, q=53, n=3233, e=17, d=2753`，极易分解，**不能用于真实遥测或隐私保护**。本仓库只实现题面规定的协议判断、幂等和并发核销逻辑，不包含攻击或利用代码。

## 角色与隐私边界

- **`Device`（设备）**：持有设备编号、公钥和可注入随机源；生成随机票号，编码 `(period, ticket)`，哈希到模 `n` 的可逆元，再用随机可逆因子盲化。设备保存票号和盲因子，之后去盲。
- **`Registrar`（登记端）**：持有 RSA 私钥和已登记设备集合。它能看到 `device_id`、`period` 和盲文整数，但看不到票号或明文哈希。对 `(device_id, period)` 只保存一份答复。
- **`Collector`（采集端）**：只接收 `RsaPublicKey` 和不含设备编号的 `Report`。验证去盲签名后，按 `(period, ticket)` 在线程锁内原子登记，保证同一运行进程内只接收一次。

## 编码与盲签公式

长度前缀使用 4 字节大端 `uint32`：

```text
lp(x) = uint32be(len(x)) || x
period_bytes = ASCII decimal(period)
message = lp(period_bytes) || lp(ticket)
```

哈希到可逆元时，从 `counter=0` 开始：

```text
digest_c = SHA256(domain || message || uint32be(counter))
m_c = int(digest_c) mod n
选择第一个满足 2 <= m_c < n 且 gcd(m_c, n) = 1 的 m_c
```

采集端用相同的确定性搜索恢复 `m`，因此计数器不必额外放入报告。

盲签与去盲：

```text
r = 随机可逆因子
blinded = m * r^e mod n
blinded_signature = blinded^d mod n
signature = blinded_signature * r^{-1} mod n
验证：signature^e mod n == m
```

## 公开 API

主要导出位于 `blind_telemetry`：

- 参数与密钥：
  - `DEMO_KEY_PAIR`、`DEMO_PUBLIC_KEY`、`DEMO_PRIVATE_KEY`
  - `RsaPublicKey(n, e)`、`RsaPrivateKey(public_key, d)`、`RsaKeyPair`
  - `demo_key_pair()`
- 编码/哈希：
  - `length_prefix(data)`、`read_length_prefixed(blob, offset=0)`
  - `encode_credential_message(period, ticket)`
  - `decode_credential_message(blob)`
  - `hash_to_unit(n, period, ticket)` / `hash_to_unit(..., encoded_message=...)`
- 随机源：
  - `random_unit_factor(n, rng=None)`
  - `ScriptedRandomSource(ticket=..., factors=[...], seed=...)`，用于确定性测试和演示；也可注入任何实现 `randbytes(size)` 与 `randrange(start, stop)` 的 `random.Random` 风格对象。
- 协议角色与数据类型：
  - `Device(device_id, public_key, rng=None, ticket_length=16)`
    - `prepare(period, ticket=None, blinding_factor=None) -> PreparedBlindCredential`
    - `unblind(prepared, reply) -> Credential`
    - `request_credential(registrar, period, ...) -> (prepared, credential)`
  - `Registrar(private_key, registered_devices=())`
    - `register_device(device_id)`
    - `issue(device_id, period, blinded_message) -> BlindReply`
  - `Collector(public_key)`
    - `verify_report(report)`
    - `receive(report) -> Receipt`（重复时抛 `DuplicateReportError`）
  - `Credential.to_report(measurement=b"") -> Report`
- 错误类型：
  - `UnknownDeviceError`、`PeriodConflictError`
  - `BlindMessageError`、`InvalidBlindReplyError`
  - `InvalidCredentialError`、`DuplicateReportError`
  - `EncodingError`、`HashToUnitError`、`RandomSourceError`

## 快速示例

```python
from blind_telemetry import (
    Collector,
    DEMO_KEY_PAIR,
    Device,
    Registrar,
    ScriptedRandomSource,
)

keys = DEMO_KEY_PAIR
registrar = Registrar(keys.private_key, registered_devices={"device-1"})
collector = Collector(keys.public_key)  # 采集端只有公钥

# 测试中用脚本随机源；生产默认使用 random.SystemRandom。
rng = ScriptedRandomSource(ticket=b"0123456789abcdef", factors=[42])
device = Device("device-1", keys.public_key, rng=rng, ticket_length=16)

prepared = device.prepare(period=1)
reply = registrar.issue("device-1", 1, prepared.blinded_message)
credential = device.unblind(prepared, reply)

report = credential.to_report("temperature_c=21.5")
receipt = collector.receive(report)
print(receipt.sequence, receipt.ticket.hex())

# 原样重试返回同一个 BlindReply 对象，不会再次执行私钥签名。
assert registrar.issue("device-1", 1, prepared.blinded_message) is reply
```

## 运行方法

要求 Python 3.11，不安装第三方依赖。

在工作区根目录运行全部测试：

```bash
python3 -m unittest discover -s tests -v
```

运行可复现的本地合成场景：

```bash
python3 -m blind_telemetry.demo
# 或
python3 -m blind_telemetry
```

演示会实际执行并打印：

1. 正常签发、去盲、公钥验证和报告接收；
2. 登记端原样重试返回同一答复，同设备同周期的不同盲文被拒绝；
3. 注入两个不同盲因子 `2` 和 `3`，得到不同盲文，但去盲后是同一个可用 RSA 签名；
4. 同周期同票号重复上报被核销集合拒绝；
5. 把周期从 `1` 改成 `2` 的跨周期重放无法通过公钥验证；
6. 两个线程用屏障同时提交同一份报告，恰好一个得到回执、另一个得到 `DuplicateReportError`。

## 验证范围

当前 `tests/` 包含 29 个用例，覆盖：

- 长度前缀编码、严格解码、截断/尾随字节、字段边界和拼接歧义；
- 周期、票号类型与空票号等非法输入；
- 计数器哈希结果的确定性、模数范围、与 `n` 的可逆性，以及周期/票号变化的影响；
- 盲化/去盲代数关系，以及两个可注入盲因子恢复相同明文签名；
- 随机可逆因子拒绝采样和失效随机源；
- 未登记设备、非法盲文、同设备同周期不同盲文冲突；
- 原样重试返回同一对象且私钥操作次数不增加；
- 采集端拒绝私钥类型、正常报告、篡改票号/签名、同周期重复和跨周期重放；
- 双线程精确并发重复上报；
- 5 轮、每轮 8 线程的竞争上报，每轮恰好一个成功者；
- 8 个相同登记重试和 8 个不同盲文竞争的登记端锁行为；
- `python3 -m blind_telemetry.demo` 的全部关键输出路径。

最近一次实测命令与结果：

```text
$ python3 --version
Python 3.11.6

$ python3 -m unittest discover -s tests -v
Ran 29 tests ...

OK
```

## 已如实记录的限制

- 默认 RSA 参数仅为教科书演示，不具备真实抗攻击安全强度。
- `Collector` 的原子性由进程内 `threading.Lock` 和字典唯一键保证；测试覆盖线程并发，但**没有**声称覆盖多进程、多主机或重启后的持久化核销。
- 当前已接收报告保存在内存中，进程重启后核销记录会清空。题面未要求持久化；如需生产化，应另加带唯一约束的持久存储、真实足够大的 RSA 参数、密钥管理、传输安全和测量体完整性保护。
