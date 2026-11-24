# 隐私审查预览与发布模块（合成数据，本地运行）

面向“匿名威胁研判样本分次发布、分析组成员交叉”场景的防御性 k-匿名审查模块。
仅使用 Python 3.11 标准库与本地合成数据，不联网、不读取凭据、不安装依赖。

## 审查规则

1. **按内部主体号连接可见列**：对每位能看到本次发布的成员，把其所属分析组在
   已发布视图中可见的历史列，与本次拟公开列按主体号连接成一行。
2. **缺失即可见值**：历史视图未覆盖某主体（或该列缺值）时，以单例哨兵
   `MISSING` 作为该主体的可见值参与分组，缺失主体不会被丢弃。
3. **等价类分组与 k 判断**：对本次样本覆盖的主体，按组合后的可见值分组成等价类，
   统计每组的**不同主体数**；任一受众组组合存在小于 `k` 个主体的组，即拒绝发布。
4. **风险输出不含主体编号**：只返回有风险的受众组组合（排序去重后的组 id 元组）
   与该组合下的最小组大小。
5. **预览绑定版本**：预览结果记录当时的已发布视图版本号；发布时若版本未变化，
   沿用预览结论；若期间已有新视图发布，则按最新版本重算（结果标记
   `recomputed=True`），因此“两次预览各自合格、合并发布形成单人组”时，第一项可发布，
   第二项重算后拒绝。
6. **幂等**：同一 `request_id` 再次提交（预览或发布）直接返回首次存储的结果；
   相同 id 但载荷不同会抛出 `ValueError`。

## 公开 API

包名 `privacy_review`：

| 符号 | 说明 |
| --- | --- |
| `ReviewService(members, k)` | 审查服务，持有成员、已发布视图与历次结果 |
| `ReviewService.preview(request)` | 按当前视图版本计算预览，不改变已发布状态 |
| `ReviewService.publish(request, preview=None)` | 发布；可传入预览对象做版本绑定校验，接受则为每个受众组生成下一版本视图 |
| `ReviewService.add_published_view(view)` | 预置历史已发布视图（版本号必须连续，从 1 开始） |
| `ReviewService.current_version` / `published_views` | 当前版本号 / 已发布视图元组 |
| `Member(member_id, groups)` | 成员；可同时属于多个分析组，组 id 排序去重 |
| `PublishedView(version, group_id, columns, records)` | 已发布视图；`records` 为 `{主体: {列: 值}}` |
| `PublishRequest(request_id, columns, records, audience_groups)` | 拟发布请求；行必须恰好包含声明的全部列 |
| `ReviewResult` | `decision`（`Decision.ACCEPT/REJECT`）、`base_version`、`k`、`risks`、`minimum_group_size`、`recomputed`、`published_version`，含 `to_dict()` |
| `Risk` | `audience_combination` 与 `minimum_group_size`，无主体编号 |
| `MISSING` | 缺失可见值单例哨兵 |
| `build_interleaving_scenario()` | 合成交错发布场景（4 主体、k=2、交叉成员） |
| `build_missing_value_scenario()` | 合成缺失值分组场景（唯一缺失拒绝 / 共同缺失接受） |

## 运行方法

```bash
python3 -m unittest discover -s tests -v
```

隔离模式（忽略环境变量与用户 site-packages）同样验证通过：

```bash
python3 -I -m unittest discover -s tests
```

## 验证范围

共 31 个测试，全部基于本地确定性合成数据：

- `tests/test_grouping.py`（7 个）：历史列与新列连接分组、恰好等于 k 的边界、
  k=1、多等价类最小组大小、`MISSING` 作为显式可见值（唯一缺失拒绝、共同缺失接受）。
- `tests/test_audience.py`（6 个）：交叉成员按受众组组合去重评估、同组合成员不重复
  上报、无关受众组不参与、组内多视图列取并集、结果中不出现任何主体编号、未知受众组报错。
- `tests/test_versioning.py`（9 个）：两次预览在同一基版本均合格；第一项发布生成
  新版本视图；第二项按最新视图重算后因单人组拒绝且不产生视图；预览/发布幂等返回
  同一存储对象；无预览直发、当前预览免重算、被拒结果可复查、跨请求 id 预览报错。
- `tests/test_validation.py`（9 个）：k 与成员/视图/请求的入参校验、版本连续性、
  列名冲突（新列遮蔽历史可见列）拦截、载荷漂移拦截、`to_dict()` 不含主体编号、
  无相关历史时全部主体按全 MISSING 前缀处理。

另通过 `python3 -m py_compile` 对全部模块做了字节码编译检查，并以 `python3 -I`
冒烟脚本人工复核了交错发布与缺失值两条主链路的实际输出。

## 非目标

- 不生成、执行任何利用/入侵/攻击/规避载荷，只实现题面规定的防御性判断。
- 不包含持久化、并发控制与真实数据接入；状态保存在单次进程的 `ReviewService` 实例内。
