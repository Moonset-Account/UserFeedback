# 电子证据封存与最小化披露模块

按有序清单对电子证据生成**带独立随机盐的摘要树（Merkle 树）**，将可信根保留在封存记录中；
导出副本时只附**批准披露**条目的名称、内容、盐、位置与包含证明。复核端使用**独立取得**的
可信根与批准位置清单验证：换序、缺少获批条目、未批准条目或证据内容变化均判失败，
副本不暴露任何被遮盖条目的名称、内容或盐。

零依赖，仅使用 Node.js 内置 `node:crypto`（SHA-256 + CSPRNG）。运行环境：Node.js ≥ 20，
本地验证版本 Node.js v24.15.0，纯 ESM。

## 入口与模块结构

| 文件 | 职责 |
| --- | --- |
| `src/index.js` | 公共 API 统一出口（实际入口，`package.json` 的 `main`/`exports` 指向它） |
| `src/digest.js` | 叶子/内部节点摘要原语、独立随机盐生成（固定域分隔 + 长度前缀编码） |
| `src/tree.js` | 有序摘要树构建、包含证明生成与独立验证（奇数节点向上提升） |
| `src/seal.js` | 封存 `sealManifest`、披露导出 `createDisclosure`、复核 `verifyDisclosure` |
| `src/codec.js` | 封存记录/披露副本的 JSON 编解码（Buffer 字段统一 base64） |
| `tests/tree.test.mjs` | 摘要原语与摘要树单元测试（`node:test`） |
| `tests/acceptance.test.mjs` | 端到端验收测试，覆盖全部交叉约束（`node:test`） |

运行测试：

```bash
node --test tests/*.test.mjs
# 或（package.json 中的等价脚本）
npm test
```

## 数据模型与密码学设计

### 叶子承诺（每个条目独立盐）

```
leaf = SHA256( LEAF_TAG
             || u32be(len(position)) || position
             || u32be(len(salt))     || salt        // 16 字节 CSPRNG 随机盐，每条独立
             || u32be(len(name))     || name
             || u32be(len(content))  || content )
```

- **独立随机盐**：`crypto.randomBytes(16)`，每个条目生成且互不复用；相同内容在两个位置
  因盐不同、且位置被纳入承诺，产生不同叶子，各自独立验证。
- **位置绑定**：`position` 进入叶子摘要，条目无法在位置间互换。
- **长度前缀编码**：所有字段以 4 字节大端长度定界，杜绝字段拼接歧义。

### 内部节点与可信根

```
node = SHA256( NODE_TAG || u32be(len(left)) || left || u32be(len(right)) || right )
```

- 叶子严格按清单顺序排列；某层节点数为奇数时，最后一个节点**与自身配对向上提升**。
- 叶子与内部节点使用不同域分隔标签，内部摘要无法被伪造为叶子承诺。
- 树顶摘要即**可信根**（32 字节），与 `leafCount`、算法、封存时间一起写入封存记录。

### 包含证明（验证所需的最小摘要集合）

每个证明步骤显式携带 `{ level, side, digest }`：

- `level`：步骤所在层（0 为叶子层；提升层跳号，不出现在证明中）；
- `side`：兄弟节点方向（`left`/`right`）；
- `digest`：兄弟摘要（固定 32 字节，**只有摘要，没有任何兄弟条目的名称、内容或盐**）。

复核端不信任证明中的方向与层级，全部由 `position` 与 `leafCount` 重新推导并逐字段校验，
错层、错向、截断、多余步骤一律以 `PROOF_MALFORMED` 拒绝；最终重算根与可信根不等则
`ROOT_MISMATCH`。

## 使用流程

```js
import {
  sealManifest,        // 封存：生成盐、摘要树、可信根
  createDisclosure,    // 导出：只含批准位置的最小副本
  verifyDisclosure,    // 复核：独立可信根 + 批准位置清单
  serializeRecord, parseRecord,
  serializeBundle, parseBundle,
} from './src/index.js';

// 1. 保管方封存
const entries = [
  { name: '合同正本.pdf', content: '...' },
  { name: '往来邮件.eml', content: '...' },
  // content 支持字符串或 Buffer（二进制证据）
];
const { sealedRecord, sealedPackage } = sealManifest(entries);
// sealedRecord：{ formatVersion, algorithm, leafCount, root(Buffer), sealedAt }
// sealedPackage：保管方持有，含全部盐/内容/树层，用于日后出副本；不对外披露

// 2. 按批准位置清单导出披露副本（内部排序，乱序提供亦可）
const bundle = createDisclosure(sealedPackage, [0, 2]);
// bundle.entries 严格按位置升序，每项含 position/name/content/salt/digest/proof
// 被遮盖条目的名称、内容、盐均不在 bundle 中；bundle 本身不含可信根

// 3. 带外传递（模拟复核端独立取得可信根与批准清单）
const recordJson = serializeRecord(sealedRecord);
const bundleJson = serializeBundle(bundle);
const recordForReviewer = parseRecord(recordJson);
const bundleForReviewer = parseBundle(bundleJson);

// 4. 复核端独立验证
const result = verifyDisclosure(bundleForReviewer, recordForReviewer, [0, 2]);
// 成功：{ ok: true, verifiedCount, leafCount }
// 失败：{ ok: false, code, reason, position? }，不抛异常
```

## 复核端判定规则与错误码

复核端只持有三样东西：披露副本、独立渠道取得的封存记录（可信根 + leafCount）、
独立取得的批准位置清单。验证内容：

1. 格式与算法版本（`BUNDLE_MALFORMED` / `RECORD_MALFORMED` / `ALGORITHM_MISMATCH` /
   `FORMAT_MISMATCH` / `LEAF_COUNT_MISMATCH` / `APPROVED_LIST_MALFORMED`）；
2. 副本位置合法、不重复且**严格按位置升序**——换序即 `ORDER_VIOLATION`；
3. 批准集合完整性：缺少任一获批条目 → `MISSING_POSITION`；
   出现任何未批准条目 → `UNAPPROVED_POSITION`；位置越界 → `POSITION_OUT_OF_RANGE`；
4. 逐项重算叶子承诺：与副本自带摘要不一致 → `LEAF_DIGEST_MISMATCH`
   （内容、盐、名称任一被改都会触发）；
5. 用包含证明重算可信根：证明结构非法 → `PROOF_MALFORMED`，根不一致 → `ROOT_MISMATCH`。

## 验证范围（测试覆盖）

共 42 个测试用例，全部执行真实断言。另做过 1–200 叶子、共 20100 个
`(叶子数, 位置)` 组合的全量证明健全性检查（临时脚本，已删除）。

### 可接受结果（应通过）

- 交错批准/遮盖的部分披露，经 JSON 序列化往返后通过；
- 全量披露（批准清单乱序提供，导出自动按位置升序）；
- 空批准清单生成空副本并通过（最小披露边界）；
- 二进制 Buffer 内容编解码后验证通过；
- 单条目封存：空证明、空批准、全披露均成立。

### 应被阻止/拒绝的结果

- 副本条目换序 → `ORDER_VIOLATION`；
- 缺少获批条目 → `MISSING_POSITION`；夹带未批准条目 → `UNAPPROVED_POSITION`；
- 证据内容、盐、名称被改：未同步自带摘要 → `LEAF_DIGEST_MISMATCH`；
  攻击者连叶子摘要一并伪造 → 证明层 `ROOT_MISMATCH`；
- 条目互换位置（含按新位置重算承诺）→ 证明结构或根校验拒绝；
- 使用另一次封存的可信根、可信根/证明摘要被篡改 → `ROOT_MISMATCH`；
- 证明方向或层级被伪造、证明截断或含多余步骤 → `PROOF_MALFORMED`；
- 位置伪造为越界值、副本与记录 leafCount 不一致、批准清单越界/重复，均有对应错误码；
- 生成阶段即拒绝非法批准位置（越界/重复）与非法封存输入（空清单、缺名称、非法内容）。

### 影响两者的边界与交错场景

- **重复内容两位置各自验证**：同内容不同位置独立盐、独立承诺、独立证明，分别通过；
- 两次封存同一清单：随机盐不同导致可信根不同，跨封存副本交叉验证全部失败，自身仍有效；
- 奇数叶子树的提升节点（含在非叶子层提升）证明可正确生成与验证；
- **最小披露**：独立封存下逐字段断言，被遮盖条目的名称、内容、盐均不出现在序列化副本中，
  证明步骤仅含 `level/side/32 字节摘要`；副本不含 `root`/`sealedAt`，
  复核端必须使用独立来源的可信根；
- 编解码往返后字节级一致且仍可验证。

## 安全边界说明

- 本模块只保证**封存完整性、最小披露与独立可验证性**；可信根到复核端的带外信任通道、
  封存记录本身的防篡改保管不在模块范围内。
- 封存包（`sealedPackage`）含全部明文与盐，仅供保管方内部使用，禁止随披露副本外传。
- 全部测试使用本地合成夹具，不包含任何真实目标探测、攻击或漏洞利用行为。
