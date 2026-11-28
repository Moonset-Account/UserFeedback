# 扩展打包器临时文件操作 —— 静态准入模块

为本地生成代码提供的**纯静态**准入检查器。输入被严格限定为结构化操作对象与
控制流描述（不读取文件系统、不执行代码、不访问网络、无第三方依赖，仅使用
Python 3.11 标准库）。

## 准入规则（与题面逐条对应）

1. **目录授权只在确认建立并按所有权条款锁定后成立。**
   `EstablishDirectory` 仅使目录进入“已建立”状态，本身结论为
   `REVIEW`（未授权）；只有所有者一致的 `LockDirectory` 成功后，目录才能
   接收文件或作为移动目标。锁定身份与创建身份不一致时目录被确定性拒绝
   （`DENY`），且不可恢复。
2. **继承目录的所有权需显式确认。** `InheritDirectory` 本身保留待复核；
   未确认前不可接收对象；只有以声明的预期所有者身份锁定后才完成授权，
   身份不符则拒绝。
3. **移动分别确认两侧。** `MoveObject` 先检查源对象的私有处理状态
   （非私有 → `DENY`；跨分支不可达/位置不确定 → `REVIEW`），再检查目标
   目录的可接收性（已锁定 → 放行；未锁定/未确认继承 → `DENY`；所有权按
   分支未知 → `REVIEW`）。
4. **按分支未知的目录所有权保留待复核。** `UnknownBranch` 声明受影响的
   目录与对象；`Branch` 汇合时若目录状态、锁定所有者或对象位置在分支间
   发散，同样降级为 `REVIEW`，绝不基于乐观假设放行。
5. **审计逐操作定位。** 每个具体操作产生一条 `AuditEntry`，记录结论、原因
   码、**依据目录 ID**（`basis_directory_id`）、**依据身份**
   （`basis_identity`）、涉及对象 ID 与分支路径。
6. **对象以不可变 ID 标识，路径/名字只是可复用绑定。** 复用旧路径名创建
   的是全新对象，按自身创建身份重新准入，不能继承旧对象的任何授权；
   移动只改绑定，对象 ID 与创建身份保持不变。

结论聚合优先级：`DENY > REVIEW > ALLOW`。`REVIEW`/`DENY` 的移动不会改动
任何状态（无副作用，便于复核后重放）。

## 公开 API

```python
from admission import (
    # 操作（admission.model）
    EstablishDirectory,   # 建立目录（未授权，待锁定）
    InheritDirectory,     # 继承目录（所有权待确认）
    LockDirectory,        # 按所有权条款锁定
    CreateFile,           # 在目录内创建文件
    MoveObject,           # 换名/跨目录移动
    Sequence,             # 顺序控制流
    Branch,               # 互斥分支，汇合后合并状态
    UnknownBranch,        # 静态不可展开分支的所有权/可达性声明
    Verdict,              # ALLOW / REVIEW / DENY
    # 分析器（admission.analyzer）
    admit,                # 唯一入口：admit(程序) -> AuditReport
    AuditEntry, AuditReport,
)
```

- `admit(program)`：接受单个操作、`Sequence`，或操作的 `list`/`tuple`。
  结构非法时返回带 `structural_errors` 的 `DENY` 报告（无条目）。
- `AuditReport.verdict`：整体结论；`.entries`：逐操作审计元组；
  `.entries_for(object_id)`：按对象 ID 过滤；`.has_reason(code)` /
  `.reasons()`：原因码查询。
- `AuditEntry` 字段：`sequence_index`（具体操作先序序号）、`operation`、
  `verdict`、`reason`、`basis_directory_id`、`basis_identity`、
  `object_id`、`branch_path`、`detail`。
- 稳定原因码定义在 `admission/analyzer.py` 顶部（如
  `CREATE_IN_NON_LOCKED_DIRECTORY`、`MOVE_SOURCE_NOT_PRIVATE`、
  `BRANCH_UNKNOWN_DIRECTORY_OWNERSHIP` 等）。

## 目录结构

```
admission/
  __init__.py     # 公开 API 导出
  model.py        # 结构化输入模型（frozen dataclass + Verdict 枚举）
  analyzer.py     # 状态机、分支合并、逐操作判定、审计报告
tests/
  test_normal_flows.py        # 正常路径与审计定位
  test_boundaries.py          # 未锁定/身份不符/非私有源/路径复用/结构校验
  test_branch_interleaving.py # 分支合流、未知所有权、部分分支建立等交错
  test_semantics.py           # REVIEW 无副作用、聚合优先级、审计查询
demo_smoke.py                 # 端到端冒烟演示（人工观察用）
```

## 运行方法

```bash
# 题面要求的命令（工作区根目录）
python3 -m unittest discover -s tests -v

# 隔离模式（忽略环境变量与用户站点包，验证无隐式依赖）
python3 -I -m unittest discover -s tests -t .

# 冒烟演示
python3 -I demo_smoke.py
```

## 验证范围（本地合成数据）

全部为本地构造的结构化对象，无真实文件系统目标、无网络、无外部依赖。
共 43 个 unittest 用例，覆盖：

- **正常场景**：建立→锁定→创建→同目录换名/跨目录移动；list 输入；
  一致分支汇合后继续放行；先序序号与嵌套分支审计路径。
- **边界与负向**：未锁定即接收、锁定不存在目录、锁定所有者不匹配且目录
  永久拒绝、重复锁定、ID 重复、继承目录未确认接收/身份不符锁定、移动未知
  源、非私有源移动、未知/未锁定目标、目标名字占用（且被拒移动不改绑定）、
  旧路径名复用不继承授权、结构非法输入。
- **交错（控制流）**：`UnknownBranch` 影响目录/对象后的 `REVIEW`、仅部分
  分支锁定或建立目录、分支间锁定所有者发散、分支间移动到不同目录导致位置
  不确定、分支内 `DENY` 主导整体结论、空分支合流不产生伪歧义、嵌套分支。
- **语义保证**：`REVIEW`/`DENY` 移动无状态变更、结论聚合优先级、
  每条审计都能定位目录与身份、结构错误报告不含条目。

### 如实记录的限制

- 判定基于调用方提供的结构化标识与控制流声明；模块不校验标识与真实文件
  系统的对应关系（输入限定本身就不包含外部世界）。
- `UnknownBranch` 的影响范围由输入显式声明，未声明的影响静态上不可见，
  这是“输入限定到结构化对象和控制流”的固有边界。
- 所有结论均由上述 43 个自动化用例与 `demo_smoke.py` 实际执行验证；
  未做也无法做真实打包器对接测试。
