"""提案协商核心算法。

选择优先级（严格确定性，保证唯一结果）：
1. 本端提案顺序优先（本地提案索引升序）；
2. 同一对提案内按对端提案顺序（外层对端提案索引升序）；
3. 候选套件按本端组内偏好枚举：加密 -> 完整性 -> 密钥交换组，
   各清单均取本端提案中的顺序，第一个通过强度门槛的套件即最终结果。

结构性约束：
- 套件的三个组件必须共同来自同一份本端提案和同一份对端提案，
  任何组件只出现在另一份提案里都不能拼接；
- AEAD（内置认证）加密不得另配完整性算法；
- 非 AEAD 加密必须搭配一个双方共有的完整性算法。
"""

from __future__ import annotations

from .errors import (
    NoCommonProposalError,
    PolicyRejectionError,
    UnknownAlgorithmError,
)
from .models import PairDiagnostic, Proposal, RejectedSuite, SelectedSuite
from .policy import Policy


def _ordered_intersection(local: list[str], remote: list[str]) -> list[str]:
    """按本端清单顺序返回双方共有项。"""
    remote_set = set(remote)
    return [item for item in local if item in remote_set]


def validate_proposal(proposal: Proposal, policy: Policy) -> None:
    """对单份提案做结构与语义校验，非法即快速失败。

    校验内容：引用的算法/分组必须在政策注册表内；同一份提案不得
    混用 AEAD 与非 AEAD 加密；AEAD 提案不得另配完整性算法；
    非 AEAD 提案必须声明完整性算法。
    """
    pid = proposal.proposal_id
    if not proposal.encryption_algorithms:
        raise ValueError(f"提案 {pid} 缺少加密算法")
    if not proposal.key_exchange_groups:
        raise ValueError(f"提案 {pid} 缺少密钥交换组")

    aead_flags = [policy.is_aead(enc) for enc in proposal.encryption_algorithms]
    if any(aead_flags) and not all(aead_flags):
        raise ValueError(
            f"提案 {pid} 不得在同一份提案中混用 AEAD 与非 AEAD 加密算法"
        )

    if all(aead_flags):
        if proposal.integrity_algorithms:
            raise ValueError(
                f"提案 {pid} 使用内置认证的 AEAD 加密，不能另配完整性算法: "
                f"{proposal.integrity_algorithms}"
            )
    elif not proposal.integrity_algorithms:
        raise ValueError(f"提案 {pid} 使用非 AEAD 加密，必须声明完整性算法")

    for enc in proposal.encryption_algorithms:
        policy.strength_of(enc)
    for integ in proposal.integrity_algorithms:
        policy.strength_of(integ)
    for group in proposal.key_exchange_groups:
        policy.strength_of(group)


def _pair_diagnostic(
    local: Proposal,
    remote: Proposal,
    common_enc: list[str],
    common_int: list[str],
    common_grp: list[str],
    reason: str,
) -> PairDiagnostic:
    return PairDiagnostic(
        local_proposal_id=local.proposal_id,
        remote_proposal_id=remote.proposal_id,
        common_encryption=tuple(common_enc),
        common_integrity=tuple(common_int),
        common_groups=tuple(common_grp),
        reason=reason,
    )


def negotiate(
    local_proposals: list[Proposal],
    remote_proposals: list[Proposal],
    policy: Policy,
) -> SelectedSuite:
    """在两组有序提案间协商出唯一套件。

    抛出：
      UnknownAlgorithmError: 提案引用了政策未登记的算法；
      ValueError: 提案结构非法（混用 AEAD、AEAD 另配完整性等）；
      NoCommonProposalError: 双方没有任何共同的完整套件（结构性失败）；
      PolicyRejectionError: 共同套件存在但全部未达强度门槛（政策淘汰）。
    """
    if not local_proposals:
        raise ValueError("本端提案清单为空")
    if not remote_proposals:
        raise ValueError("对端提案清单为空")

    local_ids = [p.proposal_id for p in local_proposals]
    if len(set(local_ids)) != len(local_ids):
        raise ValueError("本端提案 id 存在重复")
    remote_ids = [p.proposal_id for p in remote_proposals]
    if len(set(remote_ids)) != len(remote_ids):
        raise ValueError("对端提案 id 存在重复")

    for proposal in (*local_proposals, *remote_proposals):
        validate_proposal(proposal, policy)

    any_complete_common = False
    rejected: list[RejectedSuite] = []
    diagnostics: list[PairDiagnostic] = []
    min_strength = policy.min_strength

    # 先按本端提案顺序，再按对端提案顺序。
    for local in local_proposals:
        for remote in remote_proposals:
            common_enc = _ordered_intersection(
                local.encryption_algorithms, remote.encryption_algorithms
            )
            common_int = _ordered_intersection(
                local.integrity_algorithms, remote.integrity_algorithms
            )
            common_grp = _ordered_intersection(
                local.key_exchange_groups, remote.key_exchange_groups
            )

            if not common_enc:
                diagnostics.append(
                    _pair_diagnostic(
                        local, remote, common_enc, common_int, common_grp,
                        "没有共同的加密算法",
                    )
                )
                continue
            if not common_grp:
                diagnostics.append(
                    _pair_diagnostic(
                        local, remote, common_enc, common_int, common_grp,
                        "没有共同的密钥交换组",
                    )
                )
                continue

            # 协商层面的 AEAD 约束（双保险，validate_proposal 已拦截本端非法提案）：
            # 同一份提案内的加密类型一致，因此取第一个共同加密即可判定。
            aead = policy.is_aead(common_enc[0])
            if aead:
                if local.integrity_algorithms or remote.integrity_algorithms:
                    diagnostics.append(
                        _pair_diagnostic(
                            local, remote, common_enc, common_int, common_grp,
                            "AEAD 加密内置认证，不能另配完整性算法",
                        )
                    )
                    continue
            elif not common_int:
                diagnostics.append(
                    _pair_diagnostic(
                        local, remote, common_enc, common_int, common_grp,
                        "非 AEAD 加密缺少共同的完整性算法",
                    )
                )
                continue

            # 该对提案可以枚举出完整共同套件：按本端组内偏好
            # 加密 -> 完整性 -> 组 的字典序枚举。
            any_complete_common = True
            integrity_choices: list[str | None] = (
                [None] if aead else list(common_int)
            )
            for enc in common_enc:
                for integrity in integrity_choices:
                    for group in common_grp:
                        strength = policy.suite_strength(enc, integrity, group)
                        if strength >= min_strength:
                            return SelectedSuite(
                                encryption=enc,
                                integrity=integrity,
                                group=group,
                                strength=strength,
                                local_proposal_id=local.proposal_id,
                                remote_proposal_id=remote.proposal_id,
                            )
                        rejected.append(
                            RejectedSuite(
                                encryption=enc,
                                integrity=integrity,
                                group=group,
                                strength=strength,
                                local_proposal_id=local.proposal_id,
                                remote_proposal_id=remote.proposal_id,
                                reason=(
                                    f"套件强度 {strength} 低于政策最低等级 "
                                    f"{min_strength}"
                                ),
                            )
                        )
            diagnostics.append(
                _pair_diagnostic(
                    local, remote, common_enc, common_int, common_grp,
                    "共同套件全部被强度政策淘汰",
                )
            )

    if any_complete_common:
        raise PolicyRejectionError(
            f"双方存在共同套件，但全部低于政策最低强度等级 {min_strength}",
            rejected=rejected,
            min_strength=min_strength,
        )
    raise NoCommonProposalError(
        "双方没有任何一对提案可以组成完整的共同套件",
        diagnostics=diagnostics,
    )


def negotiate_rekey(
    local_proposals: list[Proposal],
    remote_proposals: list[Proposal],
    policy: Policy,
    prior_suite_strength: int,
) -> SelectedSuite:
    """换钥协商：在政策门槛之上叠加“上一代连接最低等级”要求。

    生效门槛取 ``max(policy.min_strength, prior_suite_strength)``，
    即新套件不得弱于政策要求，也不得弱于上一代连接的等级。
    """
    from dataclasses import replace

    effective = replace(
        policy, min_strength=max(policy.min_strength, prior_suite_strength)
    )
    return negotiate(local_proposals, remote_proposals, effective)
