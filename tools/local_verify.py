#!/usr/bin/env python3
"""本地核验入口（运行入口，不是独立合格证明）。

用法：
    python -I tools/local_verify.py --list
    python -I tools/local_verify.py --case C01
    python -I tools/local_verify.py --case C02
    python -I tools/local_verify.py --case C03
    python -I tools/local_verify.py --case C04

任一断言失败、入口缺失、未实现、零断言或整组跳过均以非零退出。
每组输出一条 JSON 记录，包含 case、实际断言数、passed/failed/skipped，
以及样本/种子与期望/观察摘要。输出不含凭据或原始敏感样本内容。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from satoken_analyzer import AnalysisError, analyze, resolve_automount  # noqa: E402

SAMPLES = REPO_ROOT / "samples"


# --------------------------------------------------------------------------- #
# 轻量断言框架：每个 check 抛 AssertionError 即记一次失败
# --------------------------------------------------------------------------- #
class Check:
    def __init__(self, name: str, fn: Callable[[], None]):
        self.name = name
        self.fn = fn


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def check_eq(actual: Any, expected: Any, label: str) -> None:
    if actual != expected:
        raise AssertionError("%s: 期望 %r，观察 %r" % (label, expected, actual))


def load_sample(filename: str) -> Dict[str, Any]:
    path = SAMPLES / filename
    if not path.is_file():
        raise AnalysisError("样本缺失: %s" % path)
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def find_container(result: Dict[str, Any], name: str) -> Dict[str, Any]:
    for item in result["containerResults"]:
        if item["container"] == name:
            return item
    raise AssertionError("结果中缺少容器: %s" % name)


def finding_ids(container: Dict[str, Any], key: str) -> List[str]:
    return [f["tokenId"] for f in container["findings"][key]]


# --------------------------------------------------------------------------- #
# C01：自动挂载覆盖 + 显式 projected token 独立路径
# --------------------------------------------------------------------------- #
def case_c01() -> Tuple[str, List[Check], Dict[str, Any], Dict[str, Any]]:
    filename = "c01_automount_override_and_projected.json"
    manifest = load_sample(filename)
    result = analyze(manifest)
    checks: List[Check] = []

    def t_pod_overrides_sa() -> None:
        basis = result["automountDecision"]
        check_eq(basis["effective"], False, "C01 自动挂载最终值")
        check_eq(basis["podValue"], False, "C01 pod 值")
        check_eq(basis["serviceAccountValue"], True, "C01 SA 值")
        check_eq(
            basis["provenance"]["source"],
            "pod",
            "C01 自动挂载来源应为 Pod 覆盖",
        )

    def t_app_reads_only_explicit_projected() -> None:
        app = find_container(result, "app")
        check_eq(app["autoMountTokenPresent"], False, "C01 app 不应有自动挂载令牌")
        check_eq(len(app["readableTokens"]), 1, "C01 app 可读令牌数")
        token = app["readableTokens"][0]
        check_eq(token["kind"], "projected", "C01 令牌类型")
        check_eq(token["volumeName"], "explicit-api-token", "C01 卷名")
        check_eq(token["subPath"], "token", "C01 卷内路径")
        check_eq(
            token["mountPath"], "/var/run/secrets/api/token", "C01 容器内路径"
        )
        check_eq(token["readOnly"], True, "C01 只读挂载标记")
        check_eq(
            token["audiences"], ["https://api.example.internal"], "C01 audience"
        )
        check_eq(token["expirationSeconds"], 1800, "C01 有效期")

    def t_other_containers_have_no_token() -> None:
        sidecar = find_container(result, "sidecar-no-mount")
        initc = find_container(result, "init-setup")
        check_eq(len(sidecar["readableTokens"]), 0, "C01 sidecar 不应读到令牌")
        check_eq(len(initc["readableTokens"]), 0, "C01 init 不应读到令牌")
        unmet = [u["audience"] for u in sidecar["findings"]["unmetRequirement"]]
        check(
            "kubernetes.default.svc" in unmet,
            "C01 sidecar 必要 audience 未被满足: %r" % unmet,
        )

    def t_app_findings_clean() -> None:
        app = find_container(result, "app")
        for key in ("overexposed", "purposeMismatch", "insufficientInfo"):
            check_eq(
                len(app["findings"][key]), 0, "C01 app 不应有 %s" % key
            )

    def t_automount_branches() -> None:
        # Pod 未设置 -> 沿用 SA
        pod = {"spec": {"automountServiceAccountToken": None}}
        sa = {"automountServiceAccountToken": True}
        decision, prov = resolve_automount(pod, sa)
        check_eq(decision, True, "C01 分支: SA=true 生效")
        check_eq(prov["provenance"]["source"], "serviceAccount", "C01 分支来源 SA")
        # 两者都未设置 -> 默认 true
        decision, prov = resolve_automount({"spec": {}}, {})
        check_eq(decision, True, "C01 分支: 默认 true")
        check_eq(prov["provenance"]["source"], "default", "C01 分支来源 default")
        # Pod=true 覆盖 SA=false
        decision, prov = resolve_automount(
            {"spec": {"automountServiceAccountToken": True}},
            {"automountServiceAccountToken": False},
        )
        check_eq(decision, True, "C01 分支: Pod=true 覆盖 SA=false")
        check_eq(prov["provenance"]["source"], "pod", "C01 分支来源 Pod")

    checks = [
        Check("pod-overrides-serviceaccount-automount", t_pod_overrides_sa),
        Check("explicit-projected-token-independent-path", t_app_reads_only_explicit_projected),
        Check("unmounted-containers-read-nothing", t_other_containers_have_no_token),
        Check("app-has-no-audit-findings", t_app_findings_clean),
        Check("automount-decision-branches", t_automount_branches),
    ]

    observed = {
        "automount": result["automountDecision"]["effective"],
        "perContainer": {
            c["container"]: {
                "type": c["containerType"],
                "readable": c["summary"]["readableTokenIds"],
                "findings": {
                    k: len(c["findings"][k])
                    for k in ("overexposed", "purposeMismatch", "insufficientInfo", "unmetRequirement")
                },
            }
            for c in result["containerResults"]
        },
    }
    expected = {
        "automount": False,
        "app": ["explicit-api-token:token"],
        "sidecar-no-mount": [],
        "init-setup": [],
    }
    return filename, checks, expected, observed


# --------------------------------------------------------------------------- #
# C02：准入顺序、最终配置与字段溯源
# --------------------------------------------------------------------------- #
def case_c02() -> Tuple[str, List[Check], Dict[str, Any], Dict[str, Any]]:
    filename = "c02_mutation_order_provenance.json"
    manifest = load_sample(filename)
    result = analyze(manifest)
    checks: List[Check] = []

    def t_mutation_order() -> None:
        ids = [m["id"] for m in result["admissionOrder"]]
        check_eq(
            ids,
            [
                "mut-1-add-injected",
                "mut-2-rename-old",
                "mut-3-patch-renamed",
                "mut-4-set-mounts",
                "mut-5-remove-injected",
            ],
            "C02 准入顺序",
        )

    def t_final_volumes() -> None:
        names = [v["name"] for v in result["effectiveConfig"]["volumes"]]
        check_eq(names, ["renamed-token"], "C02 最终卷集合")
        prov = {
            p["volumeName"]: p for p in result["effectiveConfig"]["volumeProvenance"]
        }["renamed-token"]
        check_eq(prov["fieldProvenance"]["name"]["source"], "mutation", "C02 卷名来源类型")
        check_eq(
            prov["fieldProvenance"]["name"]["mutationId"],
            "mut-2-rename-old",
            "C02 卷名溯源 mutation",
        )
        check_eq(
            prov["fieldProvenance"]["projected"]["mutationId"],
            "mut-3-patch-renamed",
            "C02 projected 字段溯源 mutation",
        )
        check_eq(len(prov["renameChain"]), 1, "C02 重命名链长度")
        check_eq(prov["renameChain"][0]["from"], "old-token", "C02 重命名源")
        check_eq(prov["renameChain"][0]["to"], "renamed-token", "C02 重命名目标")

    def t_final_mounts_and_provenance() -> None:
        main = next(
            c for c in result["effectiveConfig"]["containers"] if c["name"] == "main"
        )
        check_eq(len(main["volumeMounts"]), 1, "C02 最终挂载数（级联删除后）")
        mount_view = main["volumeMounts"][0]
        mount = mount_view["mount"]
        check_eq(mount["name"], "renamed-token", "C02 挂载卷名")
        check_eq(mount["mountPath"], "/etc/secrets/renamed", "C02 挂载路径")
        check_eq(mount["readOnly"], True, "C02 只读值")
        for field in ("name", "mountPath", "readOnly"):
            check_eq(
                mount_view["provenance"][field]["mutationId"],
                "mut-4-set-mounts",
                "C02 挂载字段 %s 溯源" % field,
            )
        check_eq(mount_view["dangling"], False, "C02 不应有悬空挂载")
        removed = result["effectiveConfig"]["removedMounts"]
        check_eq(
            [(r["volumeName"], r["container"]) for r in removed],
            [("injected-token", "main")],
            "C02 级联删除挂载记录",
        )

    def t_token_reflects_latest_mutation() -> None:
        main = find_container(result, "main")
        check_eq(len(main["readableTokens"]), 1, "C02 main 可读令牌数")
        token = main["readableTokens"][0]
        check_eq(token["tokenId"], "renamed-token:token", "C02 令牌 id")
        check_eq(token["audiences"], ["aud-renamed"], "C02 最终 audience")
        check_eq(token["expirationSeconds"], 1200, "C02 最终有效期")
        check_eq(
            main["summary"]["overexposedCount"], 0, "C02 不应有多余暴露"
        )

    checks = [
        Check("mutation-order-preserved", t_mutation_order),
        Check("final-volumes-and-volume-provenance", t_final_volumes),
        Check("final-mounts-and-mount-provenance", t_final_mounts_and_provenance),
        Check("effective-token-comes-from-last-write", t_token_reflects_latest_mutation),
    ]

    observed = {
        "finalVolumes": [v["name"] for v in result["effectiveConfig"]["volumes"]],
        "mainMounts": [
            m["mount"]["name"]
            for c in result["effectiveConfig"]["containers"]
            if c["name"] == "main"
            for m in c["volumeMounts"]
        ],
        "mainTokens": find_container(result, "main")["summary"]["readableTokenIds"],
    }
    expected = {
        "finalVolumes": ["renamed-token"],
        "mainMounts": ["renamed-token"],
        "mainTokens": ["renamed-token:token"],
    }
    return filename, checks, expected, observed


# --------------------------------------------------------------------------- #
# C03：三类容器独立结果 + 共享卷部分挂载
# --------------------------------------------------------------------------- #
def case_c03() -> Tuple[str, List[Check], Dict[str, Any], Dict[str, Any]]:
    filename = "c03_container_types_shared_volume.json"
    manifest = load_sample(filename)
    result = analyze(manifest)
    checks: List[Check] = []

    def t_container_types_present() -> None:
        by_name = {c["container"]: c for c in result["containerResults"]}
        expected_types = {
            "init-migrator": "init",
            "app-a": "app",
            "app-b": "app",
            "debug-shell": "ephemeral",
        }
        for name, ctype in expected_types.items():
            check(name in by_name, "C03 缺少容器 %s" % name)
            check_eq(by_name[name]["containerType"], ctype, "C03 类型 %s" % name)

    def t_shared_volume_partial_mount() -> None:
        initc = find_container(result, "init-migrator")
        app_a = find_container(result, "app-a")
        app_b = find_container(result, "app-b")
        debug = find_container(result, "debug-shell")
        check_eq(len(initc["readableTokens"]), 1, "C03 init 可读令牌数")
        check_eq(
            initc["readableTokens"][0]["tokenId"],
            "shared-billing-token:billing",
            "C03 init 令牌 id",
        )
        check_eq(
            initc["readableTokens"][0]["readOnly"], True, "C03 init 只读挂载"
        )
        check_eq(len(app_a["readableTokens"]), 1, "C03 app-a 可读令牌数")
        check_eq(
            app_a["readableTokens"][0]["mountPath"],
            "/var/secrets/billing/billing",
            "C03 app-a 容器内路径",
        )
        check_eq(len(app_b["readableTokens"]), 0, "C03 app-b 不应读到共享卷令牌")
        check_eq(len(debug["readableTokens"]), 0, "C03 临时容器不应读到令牌")
        unmet_b = [u["audience"] for u in app_b["findings"]["unmetRequirement"]]
        check(
            "billing.example" in unmet_b,
            "C03 app-b 必要 audience 未满足: %r" % unmet_b,
        )
        unmet_d = [u["audience"] for u in debug["findings"]["unmetRequirement"]]
        check(
            "debug.audience" in unmet_d,
            "C03 debug-shell 必要 audience 未满足: %r" % unmet_d,
        )

    def t_no_automount_anywhere() -> None:
        check_eq(
            result["automountDecision"]["effective"], False, "C03 自动挂载关闭"
        )
        for container in result["containerResults"]:
            check_eq(
                container["autoMountTokenPresent"],
                False,
                "C03 %s 不应有自动挂载令牌" % container["container"],
            )
            check_eq(
                len(container["danglingMounts"]),
                0,
                "C03 %s 不应有悬空挂载" % container["container"],
            )

    checks = [
        Check("init-app-ephemeral-independent-results", t_container_types_present),
        Check("shared-volume-mounted-by-subset", t_shared_volume_partial_mount),
        Check("automount-disabled-explicit-projection-only", t_no_automount_anywhere),
    ]

    observed = {
        c["container"]: {
            "type": c["containerType"],
            "readable": c["summary"]["readableTokenIds"],
        }
        for c in result["containerResults"]
    }
    expected = {
        "init-migrator": ["shared-billing-token:billing"],
        "app-a": ["shared-billing-token:billing"],
        "app-b": [],
        "debug-shell": [],
    }
    return filename, checks, expected, observed


# --------------------------------------------------------------------------- #
# C04：多余暴露 / 用途不符 / 信息不足；只读仍可读
# --------------------------------------------------------------------------- #
def case_c04() -> Tuple[str, List[Check], Dict[str, Any], Dict[str, Any]]:
    filename = "c04_audience_audit.json"
    manifest = load_sample(filename)
    result = analyze(manifest)
    checks: List[Check] = []

    def t_worker_overexposed_readonly_still_reads() -> None:
        worker = find_container(result, "worker")
        check_eq(len(worker["readableTokens"]), 1, "C04 worker 可读令牌数")
        token = worker["readableTokens"][0]
        check_eq(token["readOnly"], True, "C04 worker 为只读挂载")
        over = worker["findings"]["overexposed"]
        check_eq(len(over), 1, "C04 worker 多余暴露条数")
        check_eq(over[0]["extraAudiences"], ["admin.aud"], "C04 多余 audience")
        check_eq(over[0]["readOnly"], True, "C04 暴露记录保留只读标记")
        check_eq(worker["summary"]["purposeMismatchCount"], 0, "C04 worker 无用途不符")
        check_eq(worker["summary"]["insufficientInfoCount"], 0, "C04 worker 信息充分")
        check_eq(worker["summary"]["unmetRequirementCount"], 0, "C04 worker 需求满足")

    def t_batch_purpose_mismatch() -> None:
        batch = find_container(result, "batch")
        check_eq(len(batch["readableTokens"]), 1, "C04 batch 可读令牌数")
        check_eq(batch["readableTokens"][0]["readOnly"], False, "C04 batch 非只读")
        mismatch = batch["findings"]["purposeMismatch"]
        check_eq(len(mismatch), 1, "C04 batch 用途不符条数")
        check_eq(mismatch[0]["tokenPurpose"], "other-batch-purpose", "C04 令牌用途")
        check_eq(mismatch[0]["containerPurpose"], "batch-jobs", "C04 容器用途")
        check_eq(batch["summary"]["overexposedCount"], 0, "C04 batch 无多余暴露")

    def t_lone_unregistered_token_insufficient_info() -> None:
        lone = find_container(result, "lone")
        check_eq(len(lone["readableTokens"]), 1, "C04 lone 仍能读到未登记令牌")
        check_eq(
            lone["readableTokens"][0]["readOnly"], True, "C04 lone 为只读挂载"
        )
        info = lone["findings"]["insufficientInfo"]
        check_eq(len(info), 1, "C04 lone 信息不足条数")
        check_eq(
            info[0]["tokenId"], "rogue-token:token", "C04 信息不足令牌 id"
        )
        check_eq(
            lone["requiredAudiences"], ["rogue.aud"], "C04 lone 已登记必要 audience"
        )
        check_eq(lone["summary"]["overexposedCount"], 0, "C04 lone 无多余暴露")

    def t_unlisted_container_insufficient_info() -> None:
        unlisted = find_container(result, "unlisted")
        check_eq(len(unlisted["readableTokens"]), 1, "C04 unlisted 可读令牌数")
        check_eq(
            unlisted["readableTokens"][0]["registryMatched"],
            True,
            "C04 quiet-token 已登记用途",
        )
        info = unlisted["findings"]["insufficientInfo"]
        check_eq(len(info), 1, "C04 unlisted 信息不足条数")
        check_eq(
            info[0]["tokenId"], "quiet-token:token", "C04 信息不足令牌 id"
        )
        check_eq(unlisted["requiredAudiences"], [], "C04 unlisted 无需求登记")
        check_eq(
            unlisted["summary"]["overexposedCount"], 0, "C04 unlisted 不计多余暴露"
        )

    def t_kube_app_unmet() -> None:
        kube = find_container(result, "kube-app")
        check_eq(len(kube["readableTokens"]), 0, "C04 kube-app 无令牌")
        unmet = [u["audience"] for u in kube["findings"]["unmetRequirement"]]
        check_eq(unmet, ["kubernetes.default.svc"], "C04 kube-app 未满足 audience")

    checks = [
        Check("overexposure-readonly-still-readable", t_worker_overexposed_readonly_still_reads),
        Check("purpose-mismatch-detected", t_batch_purpose_mismatch),
        Check("insufficient-info-unregistered-token", t_lone_unregistered_token_insufficient_info),
        Check("insufficient-info-unlisted-container", t_unlisted_container_insufficient_info),
        Check("unmet-requirement-when-no-token", t_kube_app_unmet),
    ]

    observed = {
        c["container"]: {
            "readable": c["summary"]["readableTokenIds"],
            "overexposed": c["summary"]["overexposedCount"],
            "purposeMismatch": c["summary"]["purposeMismatchCount"],
            "insufficientInfo": c["summary"]["insufficientInfoCount"],
            "unmet": c["summary"]["unmetRequirementCount"],
        }
        for c in result["containerResults"]
    }
    expected = {
        "worker": {"overexposed": 1, "purposeMismatch": 0, "insufficientInfo": 0},
        "batch": {"overexposed": 0, "purposeMismatch": 1, "insufficientInfo": 0},
        "lone": {"overexposed": 0, "insufficientInfo": 1},
        "unlisted": {"insufficientInfo": 1, "overexposed": 0},
        "kube-app": {"unmet": 1},
    }
    return filename, checks, expected, observed


CASES = {
    "C01": case_c01,
    "C02": case_c02,
    "C03": case_c03,
    "C04": case_c04,
}


def run_case(case_id: str) -> Dict[str, Any]:
    builder = CASES[case_id]
    try:
        filename, checks, expected, observed = builder()
    except Exception as exc:  # 样本/入口本身不可用 -> 记失败而非崩溃
        return {
            "case": case_id,
            "sample": None,
            "assertions": {"total": 0, "passed": 0, "failed": 1, "skipped": 0},
            "expectedSummary": "case builder 可执行",
            "observedSummary": "构建阶段异常: %s: %s" % (type(exc).__name__, exc),
            "failures": [{"check": "case-builder", "error": str(exc)}],
            "ok": False,
        }

    failures = []
    passed = 0
    for check_item in checks:
        try:
            check_item.fn()
            passed += 1
        except AssertionError as exc:
            failures.append({"check": check_item.name, "error": str(exc)})
        except Exception as exc:  # 实现缺陷也算断言失败
            failures.append(
                {
                    "check": check_item.name,
                    "error": "未预期异常 %s: %s" % (type(exc).__name__, exc),
                }
            )

    total = len(checks)
    skipped = 0
    record = {
        "case": case_id,
        "sample": "samples/%s" % filename,
        "assertions": {
            "total": total,
            "passed": passed,
            "failed": len(failures),
            "skipped": skipped,
        },
        "expectedSummary": expected,
        "observedSummary": observed,
        "failures": failures,
        "ok": not failures and total > 0 and passed > 0,
    }
    return record


def main(argv: List[str]) -> int:
    if len(argv) == 2 and argv[1] == "--list":
        print(
            json.dumps(
                [
                    {
                        "case": "C01",
                        "title": "Pod 覆盖 SA 自动挂载；projected token 独立路径",
                        "sample": "samples/c01_automount_override_and_projected.json",
                    },
                    {
                        "case": "C02",
                        "title": "准入顺序、最终卷/挂载与字段溯源",
                        "sample": "samples/c02_mutation_order_provenance.json",
                    },
                    {
                        "case": "C03",
                        "title": "三类容器独立结果；共享卷部分挂载",
                        "sample": "samples/c03_container_types_shared_volume.json",
                    },
                    {
                        "case": "C04",
                        "title": "多余暴露/用途不符/信息不足；只读仍可读",
                        "sample": "samples/c04_audience_audit.json",
                    },
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if len(argv) == 3 and argv[1] == "--case":
        case_id = argv[2]
        if case_id not in CASES:
            print(
                json.dumps(
                    {"error": "未知 case: %s，可用: %s" % (case_id, ", ".join(CASES))},
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            return 2
        record = run_case(case_id)
        print(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True))
        stats = record["assertions"]
        if (
            stats["failed"]
            or stats["total"] == 0
            or stats["skipped"] == stats["total"]
            or not record["ok"]
        ):
            return 1
        return 0

    print(
        "用法:\n"
        "  python -I tools/local_verify.py --list\n"
        "  python -I tools/local_verify.py --case C01|C02|C03|C04",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
