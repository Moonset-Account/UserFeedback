"""邮件加密网关的投递包编排（只做防御性的安全判断）。

公开 API 见 :mod:`mail_gateway` 包的 ``__init__``。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Key:
    """一把收件密钥。

    - ``key_id``：出现在包头上的收件密钥标识。
    - ``suite``：该密钥所属的加密套件。
    - ``holders``：持有此钥、因而能用它解开封套的所有人（含组外人员）。
    - ``identity``：该标识在包头上反查到的身份（可以是匿名标识）。
    - ``resolvers``：输入明确给出的、能把该标识反查到身份的人。
    """

    key_id: str
    suite: str
    holders: frozenset[str]
    identity: str
    resolvers: frozenset[str]


@dataclass(frozen=True)
class Recipient:
    """一名收件人。

    - ``recipient_id``：收件人标识。
    - ``suites``：该收件人支持的加密套件。
    - ``can_see``：允许其知道身份的同信收件人集合（密送可见性约束）。
    - ``decrypt_scope``：允许其解密正文时，封套持有人必须落在其中的人员范围。
    """

    recipient_id: str
    suites: frozenset[str]
    can_see: frozenset[str]
    decrypt_scope: frozenset[str]


@dataclass(frozen=True)
class PackageOption:
    """某个收件人子集作为一个包时的评估结果。"""

    members: frozenset[str]
    feasible: bool
    suite: str | None
    envelope_key_ids: tuple[str, ...]
    violations: tuple[str, ...]


def evaluate_package(
    members: frozenset[str] | set[str],
    recipients: dict[str, Recipient],
    keys: list[Key] | dict[str, Key],
) -> PackageOption:
    """评估给定收件人子集能否组成一个合法的投递包。

    一个包合法必须同时满足：

    1. 套件约束：存在一种所有成员都支持的加密套件，且只能选一种。
    2. 包头可见性：每个选中密钥的身份只能被其 ``resolvers`` 反查到；
       凡能反查到身份的包内成员，必须都在该身份主人的 ``can_see``
       允许名单里。
    3. 解密范围：每把选中密钥的全部持有人（含组外持钥人）都必须落在
       包内每名收件人的 ``decrypt_scope`` 内——组外未授权持钥人能
       打开的包直接排除。
    4. 可解密封套：选出的封套密钥要覆盖全部成员，每名成员至少持有
       其中一把有效密钥。

    返回 :class:`PackageOption`；不可行时 ``violations`` 给出全部原因。
    """
    members = frozenset(members)
    if not members:
        return PackageOption(frozenset(), False, None, (), ("empty package",))

    key_list = list(keys.values()) if isinstance(keys, dict) else list(keys)
    reasons: list[str] = []

    # ---- 1. 大家共同支持的加密套件（一个包只能选一种） ----
    common_suites: set[str] | None = None
    for rid in sorted(members):
        r = recipients[rid]
        common_suites = set(r.suites) if common_suites is None else common_suites & set(r.suites)
    assert common_suites is not None
    if not common_suites:
        reasons.append(
            "no common cipher suite among recipients: " + ", ".join(sorted(members))
        )
        return PackageOption(members, False, None, (), tuple(reasons))
    suite = sorted(common_suites)[0]

    # ---- 2/3. 逐把密钥做两道安全检查 ----
    admissible: list[Key] = []
    for key in sorted(key_list, key=lambda k: k.key_id):
        if key.suite != suite:
            continue

        key_reasons: list[str] = []

        # 包头反查：能反查到身份的包内成员必须在身份主人的可见名单内。
        visible_to = key.resolvers & members
        for viewer in sorted(visible_to):
            owner = recipients[key.identity] if key.identity in recipients else None
            allowed = owner.can_see if owner is not None else frozenset()
            if viewer not in allowed:
                key_reasons.append(
                    f"key {key.key_id!r} reveals identity {key.identity!r} to "
                    f"{viewer!r}, who is not on its visibility allow-list"
                )

        # 解密范围：所有持钥人（含组外）都必须在每名包内成员的授权范围内。
        for rid in sorted(members):
            outside = key.holders - recipients[rid].decrypt_scope
            if outside:
                key_reasons.append(
                    f"key {key.key_id!r} is held outside the decrypt scope of "
                    f"{rid!r}: {sorted(outside)}"
                )

        if key_reasons:
            reasons.extend(key_reasons)
        else:
            admissible.append(key)

    # ---- 4. 集合覆盖：用最少的可接受封套覆盖全部成员 ----
    cover = _minimum_envelope_cover(members, admissible)
    if cover is None:
        missing = sorted(
            rid
            for rid in members
            if not any(rid in k.holders for k in admissible)
        )
        if missing:
            reasons.append(
                "no admissible key produces an envelope for: " + ", ".join(missing)
            )
        else:
            reasons.append("admissible keys cannot jointly cover every recipient")
        return PackageOption(members, False, None, (), tuple(reasons))

    return PackageOption(
        members=members,
        feasible=True,
        suite=suite,
        envelope_key_ids=tuple(k.key_id for k in cover),
        violations=(),
    )


def _minimum_envelope_cover(
    members: frozenset[str], admissible_keys: list[Key]
) -> tuple[Key, ...] | None:
    """用最少的封套密钥覆盖全部成员；同数取 key_id 字典序最小的方案。"""
    index = {rid: i for i, rid in enumerate(sorted(members))}
    full = (1 << len(members)) - 1

    # 每把密钥覆盖的成员位掩码；去掉不覆盖任何成员的密钥。
    key_masks: list[tuple[Key, int]] = []
    for key in admissible_keys:
        mask = 0
        for holder in key.holders & members:
            mask |= 1 << index[holder]
        if mask:
            key_masks.append((key, mask))

    # dp[mask] = 覆盖 mask 的最优方案（封套数最少，平局按 key_id 元组字典序）。
    dp: dict[int, tuple[Key, ...]] = {0: ()}
    for mask in range(full + 1):
        if mask not in dp:
            continue
        current = dp[mask]
        for key, key_mask in key_masks:
            new_mask = mask | key_mask
            candidate = tuple(sorted(current + (key,), key=lambda k: k.key_id))
            best = dp.get(new_mask)
            candidate_ids = tuple(k.key_id for k in candidate)
            best_ids = tuple(k.key_id for k in best) if best is not None else None
            if best is None or (len(candidate), candidate_ids) < (len(best), best_ids):
                dp[new_mask] = candidate

    chosen = dp.get(full)
    return tuple(sorted(chosen, key=lambda k: k.key_id)) if chosen is not None else None
