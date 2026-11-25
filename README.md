# Host File Sandbox Permission Model

一个仅依赖 Python 标准库、可运行的主机文件沙箱权限模型。它用内存目录树和合成数据验证分层授权、跨目录移动的提权防护，以及已打开句柄与旧目录句柄的交错语义。

- Python：3.11.6（无需安装第三方依赖）
- 验证命令：`python3 -m unittest discover -s tests -v`
- 演示命令：`python3 -m host_sandbox`

## 公开 API

核心导出位于 `host_sandbox`：

```python
from host_sandbox import (
    Perm,
    Node,
    Rule,
    PolicyLayer,
    Sandbox,
    PermissionDeniedError,
    MoveError,
    MoveEndpointDeniedError,
    MoveEscalationError,
)
```

### 1. 目录树与目录授权

使用 `Node.root()`、`Node.directory()`、`Node.file()` 构造合成目录树。节点有两类授权：

- `grants=` / `subtree_grants=`：目录授予其整个子树的权限，目录自身也包含在内。可传 `{主体: 权限集合}`，也可直接传权限集合表示 `"*"` 全局授权。
- `acl=` / `self_grants=`：只授予该节点自身，不向后代传播；同样支持全局权限集合。

```python
root = Node.root(children=[
    Node.directory(
        "src",
        grants={"alice": {Perm.READ, Perm.MOVE_OUT}},
        children=[Node.file("a.txt", b"hello")],
    ),
    Node.directory(
        "dst",
        grants={"alice": {Perm.MOVE_IN}},
    ),
])
sandbox = Sandbox(root)
```

`"*"` 表示所有主体。节点 `inode` 在移动后保持不变；路径只是节点当前位置。

也可以使用 JSON 风格配置：

```python
sandbox = Sandbox.from_config({
    "tree": {
        "name": "",
        "type": "directory",
        "subtree_grants": {"*": ["read"]},
        "children": [{"name": "f", "type": "file", "content": "x"}],
    },
    "layers": [{
        "name": "example-layer",
        "managed_permissions": ["read"],
        "rules": [{"path": "/", "principal": "*", "permissions": ["read"]}],
    }],
})
```

### 2. 分层规则

`PolicyLayer(name, managed_permissions, rules)` 声明该层管理哪些权限。`Rule(path, permissions, principal="*")` 是路径前缀允许规则：

- `/a` 上的规则适用于 `/a` 及其所有后代。
- 同一层内，根到目标节点的所有祖先规则取并集，许可沿路径累积，不会被更近的规则覆盖。
- 对权限 `p`，只有每个 `managed_permissions` 包含 `p` 的层都累积出 `p`，该层才批准。
- 不管理 `p` 的层对 `p` 没有否决权。
- 目录/自身授权是基础许可；默认拒绝。即使策略层允许，目录授权未授予时仍拒绝。

常用查询：

```python
sandbox.can("/src/a.txt", "alice", Perm.READ)
decision = sandbox.evaluate("/src/a.txt", "alice", Perm.READ)
sandbox.check_permission("/src/a.txt", "alice", Perm.READ)  # 拒绝时抛异常
sandbox.effective_permissions("/src/a.txt", "alice")
```

### 3. 跨目录移动与提权防护

推荐 API：

```python
plan = sandbox.preview_move("/src/subtree", "/dst", "alice")  # 只预演，始终回滚
plan = sandbox.move_subtree("/src/subtree", "/dst", "alice")  # 检查通过后提交
```

也提供类似 `mv` 的 `sandbox.move("/src/a", "/dst/new-name", "alice")`。

移动判断包括：

1. 源父目录必须有 `Perm.MOVE_OUT`。
2. 目标父目录必须有 `Perm.MOVE_IN`。
3. 对移动子树中的每个节点，比较移动前后的 `read/write/execute` 有效权。
4. 默认比较所有显式出现在授权或规则中的主体、操作者，以及一个匿名陌生主体 `@anonymous-stranger`，因此通配授权造成的新增访问也会被捕获。
5. 只要任一节点在任一主体下新增 r/w/x，就抛出 `MoveEscalationError`，其中 `.gains` 给出 inode、主体、前后路径和新增权限。
6. 拒绝时通过事务性临时移动和精确回滚保持原目录结构完整。`preview_move()` 即使允许也不会提交。
7. 根节点移动、移动到自身后代、目标重名等结构错误抛出 `MoveError`，且不修改树。

同一目录改名会从旧名移除并创建新名，因此本实现保守地要求该目录同时具备 `MOVE_OUT` 与 `MOVE_IN`。

### 4. 已打开句柄和旧目录句柄

```python
read_handle = sandbox.open_read("/src/a.txt", "alice")
directory_handle = sandbox.open_directory("/src", "alice")

read_handle.read()                         # 保留打开时取得的 READ
directory_handle.open_read("a.txt")        # 新打开，按当前路径和当前策略核查
```

语义：

- `ReadOnlyHandle` 只快照保留 `READ`。之后撤销目录授权、删除层规则或移动节点，已打开的只读句柄仍可 `read()`。
- `DirectoryHandle` 保存稳定 inode，但不保存旧路径许可。通过它做相对路径新打开时，从该目录的**当前位置**解析 `..`，并重新执行当前授权和分层检查。
- 相对路径中的 `..` 遵循移动后的当前父目录；逃逸沙箱根的路径停在根。
- 关闭后的句柄不可再用。

## 题面要求的增权示例

可直接运行：

```bash
python3 -m host_sandbox
```

合成场景由 `host_sandbox.examples.escalation_example()` 构造：

```text
/
├── src/                         授予 operator: read, move_out
│   └── movable/                 自身 ACL: read, write, execute
│       ├── notes.txt            自身 ACL: write（原本即可写）
│       └── fragment/
│           └── report.sh       移动前仅经 /src 继承 read
└── drop/                        授予 operator: read, write, move_in
```

策略层同时批准 rwx，并在 `/src` 批准 `move_out`、在 `/drop` 批准 `move_in`。因此：

- 两端移动权限齐备；
- `/src/movable` 目录自身原本已有 rwx，不增权；
- `notes.txt` 原本已有 write，不增权；
- `fragment` 和 `report.sh` 会因 `/drop` 的子树授权新增 `write`。

实际拒绝结果为：

```text
operator gains ['write'] on /drop/movable/fragment
operator gains ['write'] on /drop/movable/fragment/report.sh
```

抛出 `MoveEscalationError` 后，测试和演示均断言原路径仍存在、`/drop/movable` 不存在，并且完整快照与尝试前一致。

## 测试与验证范围

运行：

```bash
python3 -m unittest discover -s tests -v
```

最近一次实际运行环境为 Python 3.11.6 / Darwin，结果：

```text
Ran 22 tests in 0.006s

OK
```

测试文件覆盖：

- `tests/test_permissions.py`
  - 同层祖先路径并集累积；
  - 多个管理层必须全部批准；
  - 目录子树授权与节点自身 ACL 的区别；
  - 全局权限集合到通配主体的转换；
  - JSON 风格配置加载。
- `tests/test_moves.py`
  - 典型后代增权被拒绝并完整回滚；
  - 无增权跨目录移动成功且 inode 稳定；
  - 源/目标两端移动权限缺一不可；
  - 通配目标授权给匿名主体增权时被捕获；
  - 根、移入自身后代、目标重名等结构边界；
  - `preview_move()` 不提交；
  - `mv` 风格完整目标路径改名。
- `tests/test_handles.py`
  - 撤销目录授权后旧只读句柄仍可读；
  - 删除策略层规则后旧只读句柄仍可读，但新开打开被拒；
  - 移动后旧目录句柄的相对路径和 `..` 按当前位置核查；
  - 关闭句柄边界；
  - 演示程序实际执行并断言树完整。
- `tests/test_boundaries.py`
  - 默认拒绝；
  - 无层管理某权限时目录授权即可决定；
  - `..` 不能逃逸沙箱根；
  - 路径规范化；
  - 同目录改名的权限要求。

所有验证数据均为工作区内构造的本地合成数据。实现不访问网络、不读取凭据、不安装依赖，也不包含漏洞利用、入侵或规避载荷；只实现题面要求的防御性授权判断。当前范围是虚拟目录树和句柄语义，不建模符号链接或真实操作系统 inode 权限位。
