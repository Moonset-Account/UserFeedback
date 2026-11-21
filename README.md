# 边缘站点目录：环经度最近 K 点查询

把边缘站点建模为整数网格上的点，在只保存叶页包围矩形的索引目录上做
最近 K 点（KNN）查询。仅使用 Python 3.11 标准库，无第三方依赖。

## 距离与决胜规则

- 经度是长度 360 的环，编号 `0..359`，`0` 与 `359` 首尾相接；
- 纬度为普通整数，不做环回；
- 两点距离 = 经度环上的较短步数 + 纬度差绝对值，
  即 `min(|a-b|, 360-|a-b|) + |lat_a-lat_b|`；
- 距离相同时按站点编号 `site_id` 升序决胜。

## 包围矩形与叶页

- 索引目录只保存每个**非空叶页**的经纬度包围矩形 `BoundingBox`
  （`lon_min, lon_max, lat_min, lat_max`），页内站点坐标不进内存；
- `lon_min <= lon_max` 表示普通经度区间；`lon_min > lon_max`
  表示跨过零点的环区间 `[lon_min, 359] ∪ [0, lon_max]`，两端包含；
- 页内站点通过读取回调 `read_page(page_id) -> Sequence[Site]`
  在查询时按需获取，每页最多读取一次。

## 查询算法

1. 对每个叶页计算包围矩形相对查询点的最小可达距离（下界）：
   经度按环区间求最短步数、纬度按一维区间求差，两者相加；
2. 按 `(下界, 页号)` 升序依次打开叶页；
3. 用容量为 K 的最大堆保留当前最优候选。新点 `(距离, 站点编号)`
   严格优于堆顶（距离更近，或同距编号更小）时替换末位；
4. **提前停止**：已凑够 K 个候选，且下一页的下界**严格大于**
   当前最远距离时，剩余页全部跳过。下界等于最远距离时不能停止——
   等距页中编号更小的站点仍可能替换末位；
5. 返回按 `(距离, 站点编号)` 升序排列的 `(Site, 距离)` 列表；
   站点总数不足 K 时返回全部站点。

## 公开 API

包入口 `geoindex/__init__.py` 重新导出以下全部名称。

### 数据类型（均为不可变值对象，带完整参数校验）

| 名称 | 说明 |
| --- | --- |
| `Site(site_id, longitude, latitude)` | 站点；经度必须在 `0..359`，纬度为任意整数 |
| `BoundingBox(lon_min, lon_max, lat_min, lat_max)` | 叶页包围矩形；`wraps_zero` 属性指示是否跨零点 |
| `LeafPageRef(page_id, bbox)` | 目录条目：页号 + 包围矩形 |
| `IndexDirectory(pages)` | 叶页目录；拒绝重复页号，只接受 `LeafPageRef` |

### 函数

| 签名 | 返回 | 说明 |
| --- | --- | --- |
| `ring_longitude_distance(a, b)` | `int` | 两个经度的环短步数（0..180） |
| `site_distance(site, longitude, latitude)` | `int` | 站点到查询点的完整距离 |
| `bbox_lower_bound(bbox, longitude, latitude)` | `int` | 查询点到包围矩形的最小可达距离（页内距离下界） |
| `bbox_from_sites(sites)` | `BoundingBox` | 由非空站点序列构造紧致（非环绕）矩形的便捷函数 |
| `nearest_k(directory, k, longitude, latitude, read_page)` | `list[tuple[Site, int]]` | 最近 K 点查询；`read_page` 为页读取回调 |

回调约定：`read_page(page_id)` 返回该页的 `Site` 序列；目录中的页
均视为非空。回调返回非 `Site` 元素会抛出 `TypeError`。

## 复现步骤

要求 Python 3.11.6（本机 `/usr/local/bin/python3` 即 3.11.6），
无需安装任何依赖。在工作区根目录执行：

```bash
# 方式一：标准 unittest 发现（推荐）
python3 -m unittest discover -s tests -p 'test_*.py' -v

# 方式二：隔离模式（不注入当前目录、忽略环境变量与用户站点包）
python3 -I -m unittest discover -s tests -p 'test_*.py'

# 方式三：无参数入口脚本（普通模式与 -I 均可）
python3 run_tests.py
python3 -I run_tests.py
```

三种方式均从工作区根目录发现 `tests/test_*.py`，当前结果为
**48 个测试全部通过**。

## 验证范围

### 正常场景

- 单页/多页内的距离排序与 K 截断；
- 同距按 `site_id` 升序、相同坐标的站点按编号排序；
- 页按 `(下界, 页号)` 顺序打开，每页至多读取一次；
- 纬度差参与下界计算并影响开页顺序。

### 边界场景

- 经度环回：`359↔0` 距离 1、对跖点距离 180；
- 跨零点包围矩形：点落在任一段内为 0、落在两段之间取最近端点；
- 覆盖整个经度环的矩形（`0..359`）经度贡献恒为 0；单列矩形；
- `k` 大于站点总数返回全部、空目录返回空列表；
- 参数非法（越界经度、非整数、`bool` 冒充整数、重复页号、
  非可回调对象、回调返回非站点等）均抛出明确异常。

### 交错场景（对应题面强调的两条）

- **359 附近查询**：0 附近的跨零页（下界 3）胜过 340 附近的
  同侧页（下界 19），且后者因严格大于下界被提前停止、从未读取
  （`PageOpeningOrderTest.test_wrapping_page_near_zero_beats_same_side_page`）；
- **等距页末位替换**：下界等于当前最远距离时不停止，后开页中
  编号更小的站点替换末位；编号更大时末位不变但该页仍被打开
  （`EarlyStoppingTest` 中两个对照用例）。

### 随机性质测试（真实功能验证，非预置演示）

`tests/test_property.py` 使用固定种子（`random.Random(20261122)` /
`20261123`）生成 400 个随机目录、共约 2400 次随机查询，覆盖随机
页划分、跨零点矩形、包含全部站点的松散矩形与各类 K 值，逐次校验：

1. 索引查询结果与暴力全量排序取前 K **完全一致**；
2. 输出按 `(距离, 站点编号)` 有序；
3. 实际读取的页是 `(下界, 页号)` 全序的一个前缀；
4. 提前停止时每个未打开页的下界严格大于当前最远距离；
5. 站点不足 K 时所有页都被读取；
6. 每个包围矩形的下界不超过其页内任何站点的真实距离（可容许性）。

## 项目结构

```
workspace/
├── geoindex/
│   ├── __init__.py        # 公开 API 重新导出
│   └── core.py            # 距离、包围矩形下界、KNN 查询实现
├── tests/
│   ├── fake_storage.py    # 测试用假页存储，记录开页顺序
│   ├── test_distances.py  # 环距离、站点距离、包围矩形下界
│   ├── test_nearest_k.py  # 开页顺序、环回决胜、提前停止、空目录
│   ├── test_validation.py # 全部公开入口的参数校验
│   └── test_property.py   # 固定种子随机性质测试（对照暴力排序）
├── run_tests.py           # 无参数测试入口（兼容 python3 -I）
└── README.md
```
