"""第二因素登记簿。

恢复成功时只能替换申请中指定的那个因素，因此把用户现有因素抽象成
:class:`Factor`，并由 :class:`FactorRegistry` 统一管理。登记簿自带锁，
恢复流程的替换动作与并发读取可以安全交错。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class Factor:
    """一个已登记的第二因素。

    :param factor_id: 因素唯一标识，恢复申请里指定的就是它。
    :param factor_type: 因素类型（例如 ``totp`` / ``webauthn`` / ``sms``）。
    :param label: 展示用名称。
    :param enabled: 是否可用；被替换下来的旧因素会被置为停用。
    """

    factor_id: str
    factor_type: str
    label: str
    enabled: bool = True
    secret: str = ""


class FactorRegistry:
    """内存版因素登记簿，按用户隔离各自的因素集合。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._factors: Dict[str, Dict[str, Factor]] = {}

    def add_factor(self, user_id: str, factor: Factor) -> Factor:
        """为用户登记一个新因素；ID 冲突时抛出 :class:`ValueError`。"""
        with self._lock:
            bucket = self._factors.setdefault(user_id, {})
            if factor.factor_id in bucket:
                raise ValueError(f"factor already registered: {factor.factor_id}")
            bucket[factor.factor_id] = factor
            return factor

    def get_factor(self, user_id: str, factor_id: str) -> Optional[Factor]:
        """读取用户的指定因素，不存在返回 ``None``。"""
        with self._lock:
            bucket = self._factors.get(user_id, {})
            return bucket.get(factor_id)

    def list_factors(self, user_id: str) -> List[Factor]:
        """列出用户的全部因素（含已停用），按因素 ID 排序保证结果稳定。"""
        with self._lock:
            bucket = self._factors.get(user_id, {})
            return [bucket[key] for key in sorted(bucket)]

    def enabled_factor_ids(self, user_id: str) -> List[str]:
        """列出用户当前仍可用的因素 ID。"""
        return [f.factor_id for f in self.list_factors(user_id) if f.enabled]

    def replace_factor(
        self,
        user_id: str,
        target_factor_id: str,
        new_factor: Factor,
    ) -> Factor:
        """替换指定因素。

        只停用并覆盖 ``target_factor_id`` 指向的那一个因素，同账户下的
        其他因素保持原状。目标因素不存在时抛出 :class:`LookupError`，
        新因素 ID 与现有其他因素冲突时抛出 :class:`ValueError`。
        """
        with self._lock:
            bucket = self._factors.setdefault(user_id, {})
            if target_factor_id not in bucket:
                raise LookupError(f"target factor not found: {target_factor_id}")
            if (
                new_factor.factor_id in bucket
                and new_factor.factor_id != target_factor_id
            ):
                raise ValueError(
                    f"factor already registered: {new_factor.factor_id}"
                )
            old = bucket[target_factor_id]
            # 旧因素停用但保留记录，便于审计“被替换的是什么”。
            bucket[target_factor_id] = Factor(
                factor_id=old.factor_id,
                factor_type=old.factor_type,
                label=old.label,
                enabled=False,
                secret=old.secret,
            )
            bucket[new_factor.factor_id] = new_factor
            return new_factor
