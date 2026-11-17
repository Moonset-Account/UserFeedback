"""核心分析逻辑。

分析分四个阶段，与任务中的四类核对一一对应：

1. 自动挂载决策：Pod 的 automountServiceAccountToken 覆盖 ServiceAccount
   的同名设置，两者都缺省时按 Kubernetes 默认值 True 处理。
2. 准入变更引擎：按记录顺序执行 addVolume / removeVolume / renameVolume /
   patchVolume / setVolumeMounts，维护最终 volumes / volumeMounts，并对每个
   生效字段保留来源（原始 Pod 或具体 mutation id）。
3. 容器维度解析：initContainers / containers / ephemeralContainers 分别处理；
   显式 projected serviceAccountToken 沿 volume -> volumeMount 独立追踪，
   共享卷只挂载到部分容器时自然得到独立结果。
4. 令牌审计：把容器实际能读到的令牌与其登记的必要 audience 对比，区分
   多余暴露 / 用途不符 / 信息不足；只读挂载（readOnly=true）同样可读。

本模块只处理 JSON 形式的合成清单，不连接任何真实集群。
"""

from __future__ import annotations

import copy
import json
from typing import Any, Dict, List, Optional, Tuple

# 自动挂载令牌的固定标识（非真实卷名，仅用于分析输出）
AUTO_TOKEN_ID = "auto:serviceaccount-token"
# 清单未给定时 Kubernetes 默认受众
DEFAULT_AUDIENCE = "kubernetes.default.svc"
# 自动挂载令牌的默认有效期（秒），清单未登记时使用
DEFAULT_AUTO_EXPIRATION = 3600

_CONTAINER_SECTIONS = (
    ("initContainers", "init"),
    ("containers", "app"),
    ("ephemeralContainers", "ephemeral"),
)


class AnalysisError(ValueError):
    """清单结构或准入变更记录不合法。"""


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _prov(
    source: str,
    mutation_id: Optional[str] = None,
    field_path: Optional[str] = None,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    """构造一条字段溯源记录。"""
    entry: Dict[str, Any] = {"source": source}
    if mutation_id is not None:
        entry["mutationId"] = mutation_id
    if field_path is not None:
        entry["fieldPath"] = field_path
    if note is not None:
        entry["note"] = note
    return entry


def _as_list(value: Any, name: str) -> List[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise AnalysisError("%s 必须是数组" % name)
    return value


def _must_mapping(value: Any, name: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise AnalysisError("%s 必须是对象" % name)
    return value


def _token_registry(manifest: Dict[str, Any]) -> Dict[str, Any]:
    registry = manifest.get("tokenRegistry", {})
    if registry is None:
        return {}
    return _must_mapping(registry, "tokenRegistry")


# --------------------------------------------------------------------------- #
# 阶段 1：自动挂载决策（C01 前半部分）
# --------------------------------------------------------------------------- #
def resolve_automount(
    pod: Dict[str, Any], service_account: Optional[Dict[str, Any]]
) -> Tuple[bool, Dict[str, Any]]:
    """按 Pod 覆盖 ServiceAccount 的规则计算 automountServiceAccountToken。

    返回 (最终是否自动挂载, 决策溯源)。
    """
    pod_spec = pod.get("spec", {}) or {}
    pod_value = pod_spec.get("automountServiceAccountToken")
    sa_value = (
        service_account.get("automountServiceAccountToken")
        if service_account
        else None
    )

    if pod_value is not None:
        if not isinstance(pod_value, bool):
            raise AnalysisError("pod.spec.automountServiceAccountToken 必须是布尔值")
        decision = bool(pod_value)
        provenance = _prov(
            "pod",
            field_path="spec.automountServiceAccountToken",
            note="Pod 显式设置覆盖 ServiceAccount 设置",
        )
    elif sa_value is not None:
        if not isinstance(sa_value, bool):
            raise AnalysisError(
                "serviceAccount.automountServiceAccountToken 必须是布尔值"
            )
        decision = bool(sa_value)
        provenance = _prov(
            "serviceAccount",
            field_path="automountServiceAccountToken",
            note="Pod 未设置，沿用 ServiceAccount 设置",
        )
    else:
        decision = True
        provenance = _prov(
            "default",
            note="Pod 与 ServiceAccount 均未设置，按 Kubernetes 默认 true",
        )

    basis = {
        "podValue": pod_value,
        "serviceAccountValue": sa_value,
        "effective": decision,
        "provenance": provenance,
    }
    return decision, basis


# --------------------------------------------------------------------------- #
# 阶段 2：准入变更引擎（C02）
# --------------------------------------------------------------------------- #
def _initial_state(pod: Dict[str, Any]) -> Dict[str, Any]:
    """从原始 Pod 构造可变状态与初始溯源。"""
    spec = pod.get("spec", {}) or {}
    volumes: Dict[str, Dict[str, Any]] = {}
    volume_prov: Dict[str, Dict[str, Any]] = {}
    for index, raw_volume in enumerate(_as_list(spec.get("volumes"), "spec.volumes")):
        volume = _must_mapping(raw_volume, "spec.volumes[%d]" % index)
        name = volume.get("name")
        if not name:
            raise AnalysisError("spec.volumes[%d] 缺少 name" % index)
        if name in volumes:
            raise AnalysisError("初始卷名重复: %s" % name)
        volumes[name] = copy.deepcopy(volume)
        volume_prov[name] = {
            "name": _prov("pod", field_path="spec.volumes[%d].name" % index),
            "projected": _prov(
                "pod", field_path="spec.volumes[%d].projected" % index
            ),
            "renameChain": [],
        }

    containers: Dict[Tuple[str, str], Dict[str, Any]] = {}
    mount_prov: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for section, ctype in _CONTAINER_SECTIONS:
        for index, raw_c in enumerate(_as_list(spec.get(section), section)):
            container = _must_mapping(raw_c, "%s[%d]" % (section, index))
            cname = container.get("name")
            if not cname:
                raise AnalysisError("%s[%d] 缺少 name" % (section, index))
            key = (ctype, cname)
            containers[key] = copy.deepcopy(container)
            prov_list: List[Dict[str, Any]] = []
            for m_index, raw_mount in enumerate(
                _as_list(container.get("volumeMounts"), "%s.volumeMounts" % cname)
            ):
                mount = _must_mapping(
                    raw_mount, "%s.volumeMounts[%d]" % (cname, m_index)
                )
                if not mount.get("name"):
                    raise AnalysisError(
                        "%s.volumeMounts[%d] 缺少 name" % (cname, m_index)
                    )
                prov_list.append(
                    {
                        "name": _prov(
                            "pod",
                            field_path="%s[%d].volumeMounts[%d].name"
                            % (section, index, m_index),
                        ),
                        "mountPath": _prov(
                            "pod",
                            field_path="%s[%d].volumeMounts[%d].mountPath"
                            % (section, index, m_index),
                        ),
                        "readOnly": _prov(
                            "pod",
                            field_path="%s[%d].volumeMounts[%d].readOnly"
                            % (section, index, m_index),
                        ),
                    }
                )
            mount_prov[key] = prov_list

    return {
        "volumes": volumes,
        "volumeProv": volume_prov,
        "containers": containers,
        "mountProv": mount_prov,
    }


def _container_key(containers: Dict[Tuple[str, str], Any], container_name: str):
    for key in containers:
        if key[1] == container_name:
            return key
    return None


def _apply_mutation(state: Dict[str, Any], mutation: Dict[str, Any], order: int) -> None:
    mutation_id = mutation.get("id") or "mutation-%d" % order
    op = mutation.get("op")
    if not op:
        raise AnalysisError("变更记录 #%d 缺少 op" % order)

    volumes = state["volumes"]
    volume_prov = state["volumeProv"]
    containers = state["containers"]
    mount_prov = state["mountProv"]

    if op == "addVolume":
        volume = _must_mapping(mutation.get("volume"), "%s.volume" % mutation_id)
        name = volume.get("name")
        if not name:
            raise AnalysisError("%s: addVolume 的卷缺少 name" % mutation_id)
        if name in volumes:
            raise AnalysisError("%s: 卷名已存在: %s" % (mutation_id, name))
        volumes[name] = copy.deepcopy(volume)
        volume_prov[name] = {
            "name": _prov(
                "mutation", mutation_id, "volume.name", "准入新增卷"
            ),
            "projected": _prov(
                "mutation", mutation_id, "volume.projected", "准入新增卷"
            ),
            "renameChain": [],
        }

    elif op == "removeVolume":
        name = mutation.get("volumeName")
        if name not in volumes:
            raise AnalysisError(
                "%s: removeVolume 目标卷不存在: %s" % (mutation_id, name)
            )
        del volumes[name]
        del volume_prov[name]
        # 同步清理指向该卷的挂载（悬空挂载在最终配置中单独报告）
        for key, container in containers.items():
            mounts = container.get("volumeMounts") or []
            kept = [m for m in mounts if m.get("name") != name]
            if len(kept) != len(mounts):
                removed = len(mounts) - len(kept)
                container["volumeMounts"] = kept
                mount_prov[key] = mount_prov[key][: len(kept)]
                state.setdefault("removedMounts", []).append(
                    {
                        "mutationId": mutation_id,
                        "volumeName": name,
                        "container": key[1],
                        "count": removed,
                    }
                )

    elif op == "renameVolume":
        old = mutation.get("from")
        new = mutation.get("to")
        if old not in volumes:
            raise AnalysisError(
                "%s: renameVolume 源卷不存在: %s" % (mutation_id, old)
            )
        if not new:
            raise AnalysisError("%s: renameVolume 缺少 to" % mutation_id)
        if new in volumes:
            raise AnalysisError(
                "%s: renameVolume 目标名已存在: %s" % (mutation_id, new)
            )
        volumes[new] = volumes.pop(old)
        prov = volume_prov.pop(old)
        prov["name"] = _prov(
            "mutation",
            mutation_id,
            "renameVolume",
            "卷由 %s 重命名为 %s" % (old, new),
        )
        prov["renameChain"].append(
            {"from": old, "to": new, "mutationId": mutation_id}
        )
        volume_prov[new] = prov
        for key, container in containers.items():
            for index, mount in enumerate(container.get("volumeMounts") or []):
                if mount.get("name") == old:
                    mount["name"] = new
                    mount_prov[key][index]["name"] = _prov(
                        "mutation",
                        mutation_id,
                        "renameVolume",
                        "挂载随卷重命名 %s -> %s" % (old, new),
                    )

    elif op == "patchVolume":
        name = mutation.get("volumeName")
        if name not in volumes:
            raise AnalysisError(
                "%s: patchVolume 目标卷不存在: %s" % (mutation_id, name)
            )
        patch = _must_mapping(mutation.get("patch"), "%s.patch" % mutation_id)
        volumes[name].update(copy.deepcopy(patch))
        for field in patch:
            volume_prov[name][field] = _prov(
                "mutation",
                mutation_id,
                "patch.%s" % field,
                "准入覆盖卷字段 %s" % field,
            )

    elif op == "setVolumeMounts":
        cname = mutation.get("container")
        key = _container_key(containers, cname)
        if key is None:
            raise AnalysisError(
                "%s: setVolumeMounts 目标容器不存在: %s" % (mutation_id, cname)
            )
        raw_mounts = _as_list(
            mutation.get("volumeMounts"), "%s.volumeMounts" % mutation_id
        )
        new_mounts: List[Dict[str, Any]] = []
        new_prov: List[Dict[str, Any]] = []
        for m_index, raw_mount in enumerate(raw_mounts):
            mount = _must_mapping(
                raw_mount, "%s.volumeMounts[%d]" % (mutation_id, m_index)
            )
            if not mount.get("name"):
                raise AnalysisError(
                    "%s: volumeMounts[%d] 缺少 name" % (mutation_id, m_index)
                )
            new_mounts.append(copy.deepcopy(mount))
            new_prov.append(
                {
                    "name": _prov(
                        "mutation",
                        mutation_id,
                        "volumeMounts[%d].name" % m_index,
                        "准入整体替换容器挂载",
                    ),
                    "mountPath": _prov(
                        "mutation",
                        mutation_id,
                        "volumeMounts[%d].mountPath" % m_index,
                        "准入整体替换容器挂载",
                    ),
                    "readOnly": _prov(
                        "mutation",
                        mutation_id,
                        "volumeMounts[%d].readOnly" % m_index,
                        "准入整体替换容器挂载",
                    ),
                }
            )
        containers[key]["volumeMounts"] = new_mounts
        mount_prov[key] = new_prov

    else:
        raise AnalysisError("%s: 未知变更类型 op=%s" % (mutation_id, op))


def _build_effective_config(state: Dict[str, Any]) -> Dict[str, Any]:
    """把引擎状态整理成最终配置 + 逐字段溯源。"""
    effective_volumes = []
    for name, volume in state["volumes"].items():
        effective_volumes.append({"name": name, "volume": copy.deepcopy(volume)})

    effective_containers = []
    for (ctype, cname), container in state["containers"].items():
        mounts = []
        for index, mount in enumerate(container.get("volumeMounts") or []):
            prov = state["mountProv"][(ctype, cname)][index]
            dangling = mount.get("name") not in state["volumes"]
            mounts.append(
                {
                    "index": index,
                    "mount": copy.deepcopy(mount),
                    "dangling": dangling,
                    "provenance": prov,
                }
            )
        effective_containers.append(
            {
                "containerType": ctype,
                "name": cname,
                "volumeMounts": mounts,
            }
        )

    return {
        "volumes": effective_volumes,
        "volumeProvenance": [
            {
                "volumeName": name,
                "fieldProvenance": {
                    "name": copy.deepcopy(state["volumeProv"][name]["name"]),
                    "projected": copy.deepcopy(
                        state["volumeProv"][name]["projected"]
                    ),
                },
                "renameChain": copy.deepcopy(
                    state["volumeProv"][name]["renameChain"]
                ),
            }
            for name in state["volumes"]
        ],
        "containers": effective_containers,
        "removedMounts": state.get("removedMounts", []),
    }


# --------------------------------------------------------------------------- #
# 阶段 3：projected token 提取与容器可读性（C01 后半 / C03）
# --------------------------------------------------------------------------- #
def _extract_projected_tokens(
    volume: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """从一个 projected 卷中提取 serviceAccountToken 来源。"""
    projected = volume.get("projected")
    if not isinstance(projected, dict):
        return []
    sources = projected.get("sources")
    if not isinstance(sources, list):
        return []
    tokens: List[Dict[str, Any]] = []
    for source in sources:
        if not isinstance(source, dict):
            continue
        spec = source.get("serviceAccountToken")
        if not isinstance(spec, dict):
            continue
        audiences = spec.get("audience") or spec.get("audiences")
        if audiences is None:
            audience_list = [DEFAULT_AUDIENCE]
            audience_defaulted = True
        else:
            if not isinstance(audiences, list):
                raise AnalysisError(
                    "卷 %s 的 serviceAccountToken.audience 必须是数组"
                    % volume.get("name")
                )
            audience_list = list(audiences)
            audience_defaulted = False
        tokens.append(
            {
                "path": spec.get("path"),
                "audiences": audience_list,
                "audienceDefaulted": audience_defaulted,
                "expirationSeconds": spec.get("expirationSeconds"),
            }
        )
    return tokens


def _registry_lookup(
    registry: Dict[str, Any], volume_name: str, path: Optional[str]
) -> Optional[Dict[str, Any]]:
    """tokenRegistry 支持按 volume 或 volume:path 两种键登记。"""
    if path is not None:
        key = "%s:%s" % (volume_name, path)
        if key in registry:
            return _must_mapping(registry[key], "tokenRegistry.%s" % key)
    if volume_name in registry:
        entry = registry[volume_name]
        if isinstance(entry, dict):
            return entry
    return None


def _resolve_container_tokens(
    container_view: Dict[str, Any],
    volumes: Dict[str, Dict[str, Any]],
    registry: Dict[str, Any],
    automount_effective: bool,
    auto_registry: Optional[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """返回 (可读令牌列表, 悬空挂载列表)。"""
    readable: List[Dict[str, Any]] = []
    dangling_mounts: List[Dict[str, Any]] = []

    if automount_effective:
        audiences = (
            list(auto_registry["audiences"])
            if auto_registry and isinstance(auto_registry.get("audiences"), list)
            else [DEFAULT_AUDIENCE]
        )
        expiration = (
            auto_registry.get("expirationSeconds")
            if auto_registry
            else None
        )
        if expiration is None:
            expiration = DEFAULT_AUTO_EXPIRATION
        purpose = auto_registry.get("purpose") if auto_registry else None
        readable.append(
            {
                "tokenId": AUTO_TOKEN_ID,
                "kind": "autoMount",
                "volumeName": None,
                "subPath": None,
                "mountPath": None,
                "readOnly": None,
                "audiences": audiences,
                "expirationSeconds": expiration,
                "registeredPurpose": purpose,
                "registryMatched": auto_registry is not None,
            }
        )

    for mount_view in container_view["volumeMounts"]:
        mount = mount_view["mount"]
        vname = mount.get("name")
        if mount_view["dangling"] or vname not in volumes:
            dangling_mounts.append(
                {
                    "volumeName": vname,
                    "mountPath": mount.get("mountPath"),
                }
            )
            continue
        volume = volumes[vname]
        for token in _extract_projected_tokens(volume):
            path = token["path"]
            registration = _registry_lookup(registry, vname, path)
            # 卷内来源路径 + 挂载点拼成容器内实际文件路径
            mount_path = mount.get("mountPath")
            full_path = (
                "%s/%s" % (mount_path.rstrip("/"), path)
                if mount_path and path
                else mount_path or path
            )
            readable.append(
                {
                    "tokenId": "%s:%s" % (vname, path) if path else vname,
                    "kind": "projected",
                    "volumeName": vname,
                    "subPath": path,
                    "mountPath": full_path,
                    "readOnly": bool(mount.get("readOnly", False)),
                    "audiences": list(token["audiences"]),
                    "audienceDefaulted": token["audienceDefaulted"],
                    "expirationSeconds": token["expirationSeconds"],
                    "registeredPurpose": (
                        registration.get("purpose") if registration else None
                    ),
                    "registryMatched": registration is not None,
                }
            )
    return readable, dangling_mounts


# --------------------------------------------------------------------------- #
# 阶段 4：audience 审计（C04）
# --------------------------------------------------------------------------- #
def _assess_container(
    container_name: str,
    container_type: str,
    readable: List[Dict[str, Any]],
    requirement: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    findings: Dict[str, List[Dict[str, Any]]] = {
        "overexposed": [],
        "purposeMismatch": [],
        "insufficientInfo": [],
        "unmetRequirement": [],
    }

    if requirement is None:
        # 没有任何用途登记：能读到令牌也无法判断必要性
        for token in readable:
            findings["insufficientInfo"].append(
                {
                    "tokenId": token["tokenId"],
                    "reason": "容器未登记必要 audience（containerRequirements 缺项）",
                }
            )
        required_audiences: List[str] = []
        registered_purpose = None
    else:
        raw_required = requirement.get("requiredAudiences")
        if not isinstance(raw_required, list) or not raw_required:
            raise AnalysisError(
                "containerRequirements.%s.requiredAudiences 必须是非空数组"
                % container_name
            )
        required_audiences = list(raw_required)
        registered_purpose = requirement.get("declaredPurpose")

    for token in readable:
        if not token["registryMatched"]:
            findings["insufficientInfo"].append(
                {
                    "tokenId": token["tokenId"],
                    "reason": "令牌未在 tokenRegistry 登记用途，无法核对用途是否相符",
                }
            )
        else:
            token_purpose = token.get("registeredPurpose")
            if (
                registered_purpose is not None
                and token_purpose is not None
                and token_purpose != registered_purpose
            ):
                findings["purposeMismatch"].append(
                    {
                        "tokenId": token["tokenId"],
                        "tokenPurpose": token_purpose,
                        "containerPurpose": registered_purpose,
                        "reason": "令牌登记用途与容器声明用途不一致",
                    }
                )

        if requirement is not None:
            extra = [a for a in token["audiences"] if a not in required_audiences]
            if extra:
                findings["overexposed"].append(
                    {
                        "tokenId": token["tokenId"],
                        "extraAudiences": extra,
                        "requiredAudiences": required_audiences,
                        "readOnly": token["readOnly"],
                        "reason": "令牌携带容器必要 audience 之外的受众；只读挂载仍可读取令牌",
                    }
                )

    if requirement is not None:
        covered = {a for token in readable for a in token["audiences"]}
        for audience in required_audiences:
            if audience not in covered:
                findings["unmetRequirement"].append(
                    {
                        "audience": audience,
                        "reason": "容器登记的必要 audience 没有任何可读令牌覆盖",
                    }
                )

    readable_ids = [t["tokenId"] for t in readable]
    return {
        "container": container_name,
        "containerType": container_type,
        "requiredAudiences": required_audiences,
        "declaredPurpose": registered_purpose,
        "readableTokens": readable,
        "findings": findings,
        "summary": {
            "readableTokenCount": len(readable),
            "overexposedCount": len(findings["overexposed"]),
            "purposeMismatchCount": len(findings["purposeMismatch"]),
            "insufficientInfoCount": len(findings["insufficientInfo"]),
            "unmetRequirementCount": len(findings["unmetRequirement"]),
            "hasOverexposure": bool(findings["overexposed"]),
            "hasPurposeMismatch": bool(findings["purposeMismatch"]),
            "hasInsufficientInfo": bool(findings["insufficientInfo"]),
            "readableTokenIds": readable_ids,
        },
    }


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def analyze(manifest: Dict[str, Any]) -> Dict[str, Any]:
    """分析一份完整清单，返回可 JSON 序列化的结构化结果。"""
    manifest = _must_mapping(manifest, "manifest")
    pod = _must_mapping(manifest.get("pod"), "pod")
    if not pod.get("spec") or not isinstance(pod["spec"], dict):
        raise AnalysisError("pod.spec 缺失或不是对象")

    service_account = manifest.get("serviceAccount")
    if service_account is not None and not isinstance(service_account, dict):
        raise AnalysisError("serviceAccount 必须是对象")

    mutations = _as_list(manifest.get("admissionMutations"), "admissionMutations")
    requirements = _must_mapping(
        manifest.get("containerRequirements", {}), "containerRequirements"
    )
    registry = _token_registry(manifest)

    # 阶段 1
    automount_effective, automount_basis = resolve_automount(pod, service_account)

    # 阶段 2：按顺序执行准入变更
    state = _initial_state(pod)
    mutation_trace = []
    for order, raw_mutation in enumerate(mutations, start=1):
        mutation = _must_mapping(raw_mutation, "admissionMutations[%d]" % (order - 1))
        mutation_id = mutation.get("id") or "mutation-%d" % order
        _apply_mutation(state, mutation, order)
        mutation_trace.append(
            {
                "order": order,
                "id": mutation_id,
                "op": mutation.get("op"),
            }
        )
    effective_config = _build_effective_config(state)

    # 阶段 3 + 4：逐容器解析并审计
    auto_registry = registry.get(AUTO_TOKEN_ID)
    container_results = []
    for container_view in effective_config["containers"]:
        cname = container_view["name"]
        ctype = container_view["containerType"]
        readable, dangling = _resolve_container_tokens(
            container_view,
            state["volumes"],
            registry,
            automount_effective,
            auto_registry if isinstance(auto_registry, dict) else None,
        )
        assessment = _assess_container(
            cname, ctype, readable, requirements.get(cname)
        )
        assessment["danglingMounts"] = dangling
        assessment["autoMountTokenPresent"] = automount_effective
        container_results.append(assessment)

    sa_named = pod["spec"].get("serviceAccountName") or pod["spec"].get(
        "serviceAccount"
    )
    sa_actual = service_account.get("metadata", {}).get("name") if service_account else None

    return {
        "podName": (pod.get("metadata") or {}).get("name"),
        "serviceAccount": {
            "namedInPod": sa_named,
            "providedInManifest": sa_actual,
            "matches": bool(sa_actual) and sa_named == sa_actual,
        },
        "automountDecision": automount_basis,
        "admissionOrder": mutation_trace,
        "effectiveConfig": effective_config,
        "containerResults": container_results,
    }


def analyze_file(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    return analyze(manifest)
