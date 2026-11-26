"""复合推理入口的模型装配模块。

一份请求依次经过若干阶段（如向量编码、候选过滤、排序）。配置为每个阶段
列出若干可用版本，每个版本声明：

- 输入/输出张量契约（dtype + shape）；
- 权重块列表（同名块可跨版本、跨阶段共享）；
- 运行工作区大小（字节）。

装配规则：

1. 阶段按配置给出的顺序串行运行，相邻阶段之间上一阶段的输出契约必须与
   下一阶段的输入契约完全一致（接口全匹配）；
2. 工作区在阶段之间接续复用，因此峰值工作区取各阶段工作区的最大值，
   而不是求和；
3. 请求期间所有选中版本的权重都要常驻显存，同名共享权重块只计一次；
4. 峰值显存 = 常驻权重总量 + 峰值工作区，不得超过显存上限。

选择策略：每个阶段的版本按配置中列出的顺序视为偏好（越靠前越优先），
按阶段顺序做字典序回溯搜索，返回第一个满足接口与显存约束的组合。
因此当某阶段降一版即可兼容邻接模型并复用已加载权重时，搜索会自然
选中这组组合；无法装配时返回具体冲突（接口不匹配明细，或接口全匹配
组合中的最低峰值及其超出量）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

__all__ = [
    "TensorContract",
    "WeightBlock",
    "StageVersion",
    "Stage",
    "AssemblyConfig",
    "InterfaceConflict",
    "MemoryConflict",
    "AssemblyResult",
    "load_config",
    "assemble",
]


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TensorContract:
    """张量契约：dtype + shape，完全相等才算匹配。"""

    dtype: str
    shape: tuple[int, ...]

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any], *, where: str) -> "TensorContract":
        if not isinstance(data, Mapping):
            raise ValueError(f"{where}: 张量契约必须是映射，得到 {data!r}")
        try:
            dtype = data["dtype"]
            shape = data["shape"]
        except KeyError as exc:
            raise ValueError(f"{where}: 张量契约缺少字段 {exc}") from exc
        if not isinstance(dtype, str) or not dtype:
            raise ValueError(f"{where}: dtype 必须是非空字符串，得到 {dtype!r}")
        if not isinstance(shape, Sequence) or isinstance(shape, (str, bytes)):
            raise ValueError(f"{where}: shape 必须是正整数序列，得到 {shape!r}")
        shape_t = tuple(shape)
        if not shape_t or not all(isinstance(d, int) and d > 0 for d in shape_t):
            raise ValueError(f"{where}: shape 必须是非空正整数序列，得到 {shape!r}")
        return cls(dtype=dtype, shape=shape_t)

    def describe(self) -> str:
        return f"{self.dtype}{list(self.shape)}"


@dataclass(frozen=True)
class WeightBlock:
    """权重块。同名块被视为同一份共享权重，整个请求期间只驻留一份。"""

    name: str
    bytes: int


@dataclass(frozen=True)
class StageVersion:
    """某阶段的一个可用版本。"""

    stage: str
    version: str
    input: TensorContract
    output: TensorContract
    weights: tuple[WeightBlock, ...]
    workspace_bytes: int


@dataclass(frozen=True)
class Stage:
    """流水线中的一个阶段，versions 按偏好从高到低排列。"""

    name: str
    versions: tuple[StageVersion, ...]


@dataclass(frozen=True)
class AssemblyConfig:
    """装配配置：有序阶段列表 + 显存上限（字节）。"""

    stages: tuple[Stage, ...]
    vram_limit_bytes: int


# ---------------------------------------------------------------------------
# 结果与冲突
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InterfaceConflict:
    """相邻阶段某对版本之间的接口冲突明细。"""

    upstream_stage: str
    upstream_version: str
    downstream_stage: str
    downstream_version: str
    reason: str

    def describe(self) -> str:
        return (
            f"接口冲突: {self.upstream_stage}/{self.upstream_version} -> "
            f"{self.downstream_stage}/{self.downstream_version}: {self.reason}"
        )


@dataclass(frozen=True)
class MemoryConflict:
    """显存冲突：接口全匹配的组合中峰值最低的一组仍然超限。"""

    limit_bytes: int
    best_peak_bytes: int
    best_selection: tuple[tuple[str, str], ...]  # (阶段名, 版本名)

    @property
    def excess_bytes(self) -> int:
        return self.best_peak_bytes - self.limit_bytes

    def describe(self) -> str:
        combo = ", ".join(f"{s}={v}" for s, v in self.best_selection)
        return (
            f"显存冲突: 上限 {self.limit_bytes} 字节，接口全匹配组合中最低峰值为 "
            f"{self.best_peak_bytes} 字节（{combo}），超出 {self.excess_bytes} 字节"
        )


@dataclass(frozen=True)
class AssemblyResult:
    """装配结果。ok 为 True 时 selection/各项用量有效，否则查看冲突字段。"""

    ok: bool
    selection: tuple[StageVersion, ...] = ()
    resident_weight_bytes: int = 0
    peak_workspace_bytes: int = 0
    peak_bytes: int = 0
    interface_conflicts: tuple[InterfaceConflict, ...] = ()
    memory_conflict: MemoryConflict | None = None

    def summary(self) -> str:
        if self.ok:
            combo = ", ".join(f"{v.stage}={v.version}" for v in self.selection)
            return (
                f"装配成功: {combo}；常驻权重 {self.resident_weight_bytes} 字节，"
                f"峰值工作区 {self.peak_workspace_bytes} 字节，"
                f"峰值显存 {self.peak_bytes} 字节"
            )
        parts = ["装配失败:"]
        parts.extend(c.describe() for c in self.interface_conflicts)
        if self.memory_conflict is not None:
            parts.append(self.memory_conflict.describe())
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# 配置加载与校验
# ---------------------------------------------------------------------------


def load_config(data: Mapping[str, Any]) -> AssemblyConfig:
    """从映射（如 JSON/YAML 解析结果）加载并校验装配配置。

    配置结构::

        {
          "vram_limit_bytes": 4096,
          "stages": [
            {
              "name": "encoder",
              "versions": [
                {
                  "version": "enc-v2",
                  "input":  {"dtype": "float16", "shape": [1, 512]},
                  "output": {"dtype": "float16", "shape": [1, 1024]},
                  "weights": [{"name": "enc_backbone", "bytes": 1000}],
                  "workspace_bytes": 256
                },
                ...
              ]
            },
            ...
          ]
        }

    校验失败抛出 ValueError。
    """
    if not isinstance(data, Mapping):
        raise ValueError(f"配置必须是映射，得到 {data!r}")

    limit = data.get("vram_limit_bytes")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        raise ValueError(f"vram_limit_bytes 必须是非负整数，得到 {limit!r}")

    raw_stages = data.get("stages")
    if not isinstance(raw_stages, Sequence) or isinstance(raw_stages, (str, bytes)):
        raise ValueError("stages 必须是非空序列")
    if len(raw_stages) == 0:
        raise ValueError("stages 至少要包含一个阶段")

    block_sizes: dict[str, int] = {}  # 共享块名 -> 字节数，全局必须一致
    stages: list[Stage] = []
    seen_stage_names: set[str] = set()

    for s_idx, raw_stage in enumerate(raw_stages):
        where_s = f"stages[{s_idx}]"
        if not isinstance(raw_stage, Mapping):
            raise ValueError(f"{where_s}: 阶段必须是映射，得到 {raw_stage!r}")
        name = raw_stage.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(f"{where_s}: 阶段名必须是非空字符串，得到 {name!r}")
        if name in seen_stage_names:
            raise ValueError(f"{where_s}: 阶段名重复: {name!r}")
        seen_stage_names.add(name)

        raw_versions = raw_stage.get("versions")
        if not isinstance(raw_versions, Sequence) or isinstance(raw_versions, (str, bytes)):
            raise ValueError(f"{where_s}: versions 必须是非空序列")
        if len(raw_versions) == 0:
            raise ValueError(f"{where_s}: 阶段 {name!r} 至少要有一个版本")

        versions: list[StageVersion] = []
        seen_versions: set[str] = set()
        for v_idx, raw_ver in enumerate(raw_versions):
            where_v = f"{where_s}.versions[{v_idx}]"
            if not isinstance(raw_ver, Mapping):
                raise ValueError(f"{where_v}: 版本必须是映射，得到 {raw_ver!r}")
            ver_name = raw_ver.get("version")
            if not isinstance(ver_name, str) or not ver_name:
                raise ValueError(f"{where_v}: 版本名必须是非空字符串，得到 {ver_name!r}")
            if ver_name in seen_versions:
                raise ValueError(f"{where_v}: 阶段 {name!r} 内版本名重复: {ver_name!r}")
            seen_versions.add(ver_name)

            in_contract = TensorContract.from_mapping(raw_ver.get("input"), where=f"{where_v}.input")
            out_contract = TensorContract.from_mapping(raw_ver.get("output"), where=f"{where_v}.output")

            raw_weights = raw_ver.get("weights", [])
            if not isinstance(raw_weights, Sequence) or isinstance(raw_weights, (str, bytes)):
                raise ValueError(f"{where_v}: weights 必须是序列")
            weights: list[WeightBlock] = []
            local_blocks: set[str] = set()
            for w_idx, raw_w in enumerate(raw_weights):
                where_w = f"{where_v}.weights[{w_idx}]"
                if not isinstance(raw_w, Mapping):
                    raise ValueError(f"{where_w}: 权重块必须是映射，得到 {raw_w!r}")
                w_name = raw_w.get("name")
                w_bytes = raw_w.get("bytes")
                if not isinstance(w_name, str) or not w_name:
                    raise ValueError(f"{where_w}: 权重块名必须是非空字符串，得到 {w_name!r}")
                if not isinstance(w_bytes, int) or isinstance(w_bytes, bool) or w_bytes < 0:
                    raise ValueError(f"{where_w}: 权重块字节数必须是非负整数，得到 {w_bytes!r}")
                if w_name in local_blocks:
                    raise ValueError(f"{where_w}: 同一版本内权重块重复: {w_name!r}")
                local_blocks.add(w_name)
                if w_name in block_sizes and block_sizes[w_name] != w_bytes:
                    raise ValueError(
                        f"{where_w}: 共享权重块 {w_name!r} 的字节数不一致: "
                        f"{block_sizes[w_name]} vs {w_bytes}"
                    )
                block_sizes[w_name] = w_bytes
                weights.append(WeightBlock(name=w_name, bytes=w_bytes))

            workspace = raw_ver.get("workspace_bytes")
            if not isinstance(workspace, int) or isinstance(workspace, bool) or workspace < 0:
                raise ValueError(f"{where_v}: workspace_bytes 必须是非负整数，得到 {workspace!r}")

            versions.append(
                StageVersion(
                    stage=name,
                    version=ver_name,
                    input=in_contract,
                    output=out_contract,
                    weights=tuple(weights),
                    workspace_bytes=workspace,
                )
            )
        stages.append(Stage(name=name, versions=tuple(versions)))

    return AssemblyConfig(stages=tuple(stages), vram_limit_bytes=limit)


# ---------------------------------------------------------------------------
# 装配搜索
# ---------------------------------------------------------------------------


def _mismatch_reason(up_out: TensorContract, down_in: TensorContract) -> str:
    parts: list[str] = []
    if up_out.dtype != down_in.dtype:
        parts.append(f"dtype 不一致（上游输出 {up_out.dtype}，下游要求 {down_in.dtype}）")
    if up_out.shape != down_in.shape:
        parts.append(
            f"shape 不一致（上游输出 {list(up_out.shape)}，下游要求 {list(down_in.shape)}）"
        )
    return "；".join(parts) if parts else "契约不一致"


def assemble(config: AssemblyConfig) -> AssemblyResult:
    """按版本偏好在显存上限内搜索接口全匹配的组合。

    返回 AssemblyResult：
    - 成功：selection 为各阶段选中的版本，并给出常驻权重、峰值工作区与
      峰值显存（最高使用量）；
    - 失败：interface_conflicts 列出搜索中遇到的相邻阶段接口冲突明细；
      若存在接口全匹配的组合但全部超限，则 memory_conflict 给出其中峰值
      最低的组合及超出量。
    """
    stages = config.stages
    limit = config.vram_limit_bytes

    mismatches: dict[tuple[str, str, str, str], InterfaceConflict] = {}
    # 接口全匹配组合中的最低峰值（用于显存冲突报告）
    best_over_limit: tuple[int, tuple[StageVersion, ...], int, int] | None = None

    chosen: list[StageVersion] = []
    resident: dict[str, int] = {}  # 已加载共享块：名 -> 字节

    def dfs(idx: int) -> tuple[StageVersion, ...] | None:
        nonlocal best_over_limit
        if idx == len(stages):
            resident_bytes = sum(resident.values())
            peak_workspace = max(v.workspace_bytes for v in chosen)
            peak = resident_bytes + peak_workspace
            if peak <= limit:
                return tuple(chosen)
            if best_over_limit is None or peak < best_over_limit[0]:
                best_over_limit = (peak, tuple(chosen), resident_bytes, peak_workspace)
            return None

        stage = stages[idx]
        for ver in stage.versions:  # 按偏好顺序尝试
            if chosen:
                prev = chosen[-1]
                if prev.output != ver.input:
                    key = (prev.stage, prev.version, ver.stage, ver.version)
                    if key not in mismatches:
                        mismatches[key] = InterfaceConflict(
                            upstream_stage=prev.stage,
                            upstream_version=prev.version,
                            downstream_stage=ver.stage,
                            downstream_version=ver.version,
                            reason=_mismatch_reason(prev.output, ver.input),
                        )
                    continue
            added: list[str] = []
            for block in ver.weights:
                if block.name not in resident:
                    resident[block.name] = block.bytes
                    added.append(block.name)
            chosen.append(ver)
            found = dfs(idx + 1)
            if found is not None:
                return found
            chosen.pop()
            for name in added:
                del resident[name]
        return None

    found = dfs(0)
    if found is not None:
        resident_bytes = sum({b.name: b.bytes for v in found for b in v.weights}.values())
        peak_workspace = max(v.workspace_bytes for v in found)
        return AssemblyResult(
            ok=True,
            selection=found,
            resident_weight_bytes=resident_bytes,
            peak_workspace_bytes=peak_workspace,
            peak_bytes=resident_bytes + peak_workspace,
        )

    memory_conflict = None
    if best_over_limit is not None:
        peak, sel, _, _ = best_over_limit
        memory_conflict = MemoryConflict(
            limit_bytes=limit,
            best_peak_bytes=peak,
            best_selection=tuple((v.stage, v.version) for v in sel),
        )
    return AssemblyResult(
        ok=False,
        interface_conflicts=tuple(mismatches.values()),
        memory_conflict=memory_conflict,
    )
