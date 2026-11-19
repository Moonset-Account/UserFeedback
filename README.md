# 专网 DNS 否定缓存（NSEC 风格）

为专网解析器实现的否定缓存组件。适用环境：区内只使用单层小写 ASCII
标签，没有通配记录；输入记录均已验签通过，本模块只做缓存与证据判定，
不做密码学校验，也不进行任何外部网络探测。

## 实际入口

- 实现模块：`negative_cache.py`
  - `NSECRecord(zone, owner, next_name, types, expires)`：一条验签通过的
    否定证据记录，含所在区、所有者、规范序下一名称、所有者现有类型、
    失效时间（绝对秒）。
  - `NegativeCache.add(record, now)`：写入记录；已失效记录拒绝写入，
    同 `(zone, owner)` 的旧记录被替换。
  - `NegativeCache.query(zone, name, qtype, now) -> QueryResult`：
    否定判定，返回四种结论之一：
    - `NAME_MISSING`：名称不存在（命中开区间）；
    - `TYPE_MISSING`：名称存在但该类型缺失（命中区间起点）；
    - `INSUFFICIENT`：证据不足；
    - `REVALIDATE`：缓存中相反的有效证据重叠，待重新核验。
- 测试入口（真实断言，unittest）：

  ```sh
  python3 -m unittest discover -s tests
  ```

  使用 Python 3.11.6 验证通过（22 个用例全部 OK）。

## 判定语义（题面交叉约束）

- 按标签字节序，`(owner, next_name)` **开区间**证明区间内名称不存在；
  两个端点均不被覆盖。
- 区间**起点**（owner 本身）只能证明该所有者处某类型缺失；若查询类型
  在 `types` 中，否定缓存无法下结论（`INSUFFICIENT`）。
- `next_name <= owner` 时区间**绕回**排序末尾：覆盖 owner 之后直到末尾、
  以及开头到 next_name 之前的部分。
- 证据按 `zone` **严格隔离**：同名委派点（有 NS 无 SOA）的父区证明不能
  借给子区，子区证明同样不借给父区。
- 冲突检测：名称被两条及以上有效区间同时覆盖，或既被开区间覆盖又是
  某记录起点时，返回 `REVALIDATE`；其中一条记录失效后冲突自动消解。

## 验证范围（tests/test_negative_cache.py）

- **可接受结果**：开区间证明名称不存在、起点证明类型缺失、绕回区间
  两侧覆盖及中段不覆盖。
- **应被阻止/拒绝**：大写标签、通配符 `*`、多层名称、空类型集、
  退化区间（owner == next_name）、失效记录写入、非法查询参数——
  均以 `ValueError` 拒绝。
- **边界/交错场景**：开区间两端点不被覆盖；`now == expires` 失效边界；
  冲突证据在一方失效后消解并恢复确定结论；同名委派点父子区证据隔离；
  同 `(zone, owner)` 记录替换语义。

## 范围说明

仅本地实现与核验：所有测试使用合成夹具，不访问网络、不探测任何真实
目标。验签由上游完成，本模块假设输入记录已通过验签。
