# paged-kv-cache

推理服务的分页 KV 缓存管理库（Rust 1.95.0，仅标准库，无第三方依赖）。

## 功能语义

- **分页物理池**：缓存由固定数量的物理页组成（`CacheConfig { page_size, total_pages }`），每页最多容纳 `page_size` 个 Token 槽位，页内允许未填满。
- **模型键命名空间**：每个会话绑定 `ModelKey { model_version, pos_config }`（模型版本 + 位置编码配置）。只有同版本且同位置编码配置的会话之间才能复用前缀。
- **已确认前缀复用**：会话追加 Token 时，先在缓存索引中查找其完整 Token 序列的**最长已确认前缀**，直接链接（引用）已发布的物理页，其余 Token 写入自己的页。
- **半满页写时复制（COW）**：两个会话可引用同一块只填了一半的页；任一方续写不同 Token 时，写方获得私有副本，各自看到自己的尾部，其他会话持有的页保持原内容。
- **容量与回收**：页容量有限。已发布到索引且无任何会话引用的页才可淘汰回收（`evict_unreferenced`，分配压力下也会自动触发）；从未发布的页在引用归零时立即释放。被会话引用的页永不回收。
- **取消与迟到确认**：`cancel_session` 释放会话引用；迟到的片段确认（会话已取消、句柄未知或已消费）一律安全忽略，绝不会把已回收的页归给旧会话。页 / 会话 / 片段句柄 id 单调递增、永不复用。
- **不足报告**：池耗尽时 `append` 失败并返回 `AllocFailure { reusable_len, missing_pages }`（能复用的长度与实际缺页数），本次追加不产生任何副作用，旧会话仍可读。
- **可观测性**：可随时查询各会话页图（`page_map`）、页内片段（`page_fragments`）与总占用（`occupancy`）；取消另一会话或换模型后，本会话仍能读到自己的前缀（`read_prefix`）。

## 公开 API

| 接口 | 说明 |
| --- | --- |
| `PagedCache::new(CacheConfig) -> Result<_, CacheError>` | 创建缓存池；`page_size`/`total_pages` 为 0 返回 `InvalidConfig` |
| `create_session(model_version, pos_config) -> SessionId` | 打开绑定模型键的会话 |
| `append(sid, &[(Token, Vec<u8>)]) -> Result<Vec<FragmentId>, CacheError>` | 追加（Token, 缓存片段）对；返回逐 Token 片段句柄；不足时返回 `CacheError::Allocation(AllocFailure)` |
| `append_tokens(sid, &[Token])` | 空片段负载的便捷追加 |
| `confirm(sid, FragmentId) -> ConfirmOutcome` | 确认片段，扩展并发布已确认前缀；迟到/失效句柄返回 `Ignored` |
| `cancel_session(sid)` | 取消会话并释放页引用 |
| `switch_model(sid, model_version, pos_config)` | 换模型：释放缓存、重置序列（KV 不可跨模型键迁移），不影响其他会话 |
| `evict_unreferenced() -> usize` | 回收所有可淘汰且无引用的页，返回回收数 |
| `reusable_prefix(model_version, pos_config, tokens) -> usize` | 查询某 Token 序列当前可复用的已确认前缀长度 |
| `read_prefix(sid, len) -> Result<Vec<Token>, _>` | 从会话自己的页读取前缀 |
| `page_map(sid) -> Result<Vec<PageMapEntry>, _>` | 会话页图：页 id、起始位置、长度、已确认槽数、是否共享、引用计数 |
| `page_fragments(PageId) -> Option<Vec<FragmentView>>` | 页内片段：槽偏移、Token、确认标记、负载 |
| `occupancy() -> Occupancy` | 总页数 / 已用 / 空闲 / 可淘汰 / 活跃会话数 |
| `session_len(sid) -> Option<usize>` | 会话当前 Token 数 |

关键类型：`ModelKey`、`SessionId`、`PageId`、`FragmentId`、`CacheConfig`、`AllocFailure`、`CacheError`、`ConfirmOutcome`、`PageMapEntry`、`FragmentView`、`Occupancy`。

## 复现步骤

```bash
cargo test --offline
```

工具链：rustc/cargo 1.95.0。无第三方依赖，`--offline` 即可构建与测试。

## 验证范围（测试与题面场景对应）

集成测试位于 `tests/paged_cache.rs`，另有 1 个文档测试，全部真实执行：

| 测试 | 覆盖的题面场景 |
| --- | --- |
| `confirmed_prefix_reuse_is_scoped_by_model_and_pos_config` | 正常路径：同版本同位置编码复用最长已确认前缀；换版本/换位置编码不复用；复用不额外占页 |
| `shared_partial_page_diverges_with_copy_on_write` | 题面主场景：两会话引用同一半满页后分别续写不同 Token，各自看到尾部、他方页保持原内容；第三方会话仍复用原始前缀 |
| `out_of_order_and_duplicate_confirmations` | 边界：乱序确认不提前扩展前缀、重复/未知确认被忽略 |
| `allocation_shortage_reports_reusable_len_and_missing_pages` | 故障恢复：不足时返回可复用长度与实际缺页数，追加无副作用、旧会话可读；取消后复用可淘汰页完成追加 |
| `eviction_reclaims_only_unreferenced_published_pages` | 仅回收索引中可淘汰且无会话引用的页；被引用页不可回收；未发布页引用归零立即释放；回收后前缀不再可复用 |
| `cancel_and_late_confirm_do_not_resurrect_reclaimed_pages` | 取消与迟到确认不会把回收后的页归给旧会话；取消的会话不可再读 |
| `cancel_of_other_session_and_model_switch_keep_own_prefix_readable` | 交错：取消另一会话、本会话换模型后，各自前缀仍可读；换模型后可复用新模型键下的前缀 |
| `inspection_apis_expose_page_map_fragments_and_occupancy` | 页图、页内片段（含负载与确认标记）、总占用的可观测性 |
| `boundary_conditions` | 边界：非法配置、空追加、页边界恰好填满、池满、未知句柄 |
| `interleaved_sessions_stay_consistent` | 交错：多会话多模型交替追加/确认/取消/淘汰，占用账目守恒（used+free=total） |

未执行的测试：无。
