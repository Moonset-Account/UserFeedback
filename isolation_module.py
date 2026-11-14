"""隔离状态模块（控制台侧）。

场景：隔离命令发出后终端可能先掉线，之后把多次操作的回执一并补上来，
回执顺序与发送顺序无关。本模块负责：

- 操作端可对同一设备请求隔离(isolate)或解除(release)；
- 每次请求分配全局递增的命令编号(cmd_id)和按设备递增的操作序号(op_seq)；
- 回执携带设备、命令编号和执行结果；
- 控制台展示：期望状态、最后确认状态、仍待确认的命令；
- 先前操作的成功回执只补全历史，不能盖过已确认的更新操作；
- 失败时可重发当前意图：沿用原操作序号、分配新命令编号、保留发送历史。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

# 设备状态
STATE_UNKNOWN = "unknown"
STATE_ISOLATED = "isolated"
STATE_RELEASED = "released"

# 操作意图
ACTION_ISOLATE = "isolate"
ACTION_RELEASE = "release"

ACTION_TO_STATE = {
    ACTION_ISOLATE: STATE_ISOLATED,
    ACTION_RELEASE: STATE_RELEASED,
}

# 发送记录状态
STATUS_PENDING = "pending"
STATUS_ACK_SUCCESS = "ack_success"
STATUS_ACK_FAILED = "ack_failed"

# 回执结果
RESULT_SUCCESS = "success"
RESULT_FAILED = "failed"


class UnknownDeviceError(KeyError):
    """回执或重发指向了从未发起过请求的设备。"""


class UnknownCommandError(KeyError):
    """回执携带的命令编号在该设备上不存在。"""


class DuplicateReceiptError(ValueError):
    """同一命令编号收到重复回执。"""


class NoIntentError(ValueError):
    """设备尚无任何操作意图，无法重发。"""


class IntentAlreadyConfirmedError(ValueError):
    """当前意图已被成功回执确认，无需也不允许再重发。"""


class InvalidActionError(ValueError):
    """非法操作类型。"""


class InvalidResultError(ValueError):
    """非法回执结果。"""


@dataclass
class SendRecord:
    """一次命令发送记录（含重发）。"""

    cmd_id: int
    op_seq: int
    action: str
    send_order: int  # 发送先后顺序（逻辑时钟）
    status: str = STATUS_PENDING

    def as_dict(self) -> dict:
        return {
            "cmd_id": self.cmd_id,
            "op_seq": self.op_seq,
            "action": self.action,
            "send_order": self.send_order,
            "status": self.status,
        }


@dataclass
class DeviceRecord:
    """单设备的控制台侧账本。"""

    device_id: str
    expected_state: str = STATE_UNKNOWN       # 期望状态：最新意图对应的状态
    confirmed_state: str = STATE_UNKNOWN      # 最后确认状态：已确认的最大 op_seq 对应的状态
    confirmed_op_seq: int = 0                 # 已确认的最大操作序号
    current_op_seq: int = 0                   # 当前意图的操作序号
    current_action: Optional[str] = None      # 当前意图
    next_op_seq: int = 0
    sends: List[SendRecord] = field(default_factory=list)
    by_cmd: Dict[int, SendRecord] = field(default_factory=dict)


class IsolationConsole:
    """控制台侧隔离状态模块。"""

    def __init__(self) -> None:
        self._devices: Dict[str, DeviceRecord] = {}
        self._next_cmd_id = 1
        self._clock = 0

    # ------------------------------------------------------------------ 发送

    def request(self, device_id: str, action: str) -> SendRecord:
        """操作端发起新的隔离/解除意图：分配新 op_seq 与新 cmd_id。"""
        if action not in ACTION_TO_STATE:
            raise InvalidActionError(action)
        dev = self._devices.setdefault(device_id, DeviceRecord(device_id))
        dev.next_op_seq += 1
        dev.current_op_seq = dev.next_op_seq
        dev.current_action = action
        dev.expected_state = ACTION_TO_STATE[action]
        return self._new_send(dev, action, dev.current_op_seq)

    def resend(self, device_id: str) -> SendRecord:
        """重发当前意图：沿用原 op_seq，分配新 cmd_id，历史保留在 sends 中。"""
        dev = self._devices.get(device_id)
        if dev is None or dev.current_op_seq == 0 or dev.current_action is None:
            raise NoIntentError(device_id)
        if dev.confirmed_op_seq >= dev.current_op_seq:
            raise IntentAlreadyConfirmedError(device_id)
        return self._new_send(dev, dev.current_action, dev.current_op_seq)

    def _new_send(self, dev: DeviceRecord, action: str, op_seq: int) -> SendRecord:
        self._clock += 1
        rec = SendRecord(
            cmd_id=self._next_cmd_id,
            op_seq=op_seq,
            action=action,
            send_order=self._clock,
        )
        self._next_cmd_id += 1
        dev.sends.append(rec)
        dev.by_cmd[rec.cmd_id] = rec
        return rec

    # ------------------------------------------------------------------ 回执

    def handle_receipt(self, device_id: str, cmd_id: int, result: str) -> dict:
        """处理一条回执。

        成功回执：若其 op_seq 大于已确认序号，则推进最后确认状态；
        否则仅补全历史（标记该发送记录成功），不覆盖已确认的更新状态。
        失败回执：只标记该发送记录失败，不改变任何确认状态。
        """
        if result not in (RESULT_SUCCESS, RESULT_FAILED):
            raise InvalidResultError(result)
        dev = self._device(device_id)
        rec = dev.by_cmd.get(cmd_id)
        if rec is None:
            raise UnknownCommandError(cmd_id)
        if rec.status != STATUS_PENDING:
            raise DuplicateReceiptError(cmd_id)

        applied = False
        if result == RESULT_SUCCESS:
            rec.status = STATUS_ACK_SUCCESS
            if rec.op_seq > dev.confirmed_op_seq:
                dev.confirmed_op_seq = rec.op_seq
                dev.confirmed_state = ACTION_TO_STATE[rec.action]
                applied = True
        else:
            rec.status = STATUS_ACK_FAILED

        return {
            "device": device_id,
            "cmd_id": cmd_id,
            "op_seq": rec.op_seq,
            "action": rec.action,
            "result": result,
            "applied": applied,
            "confirmed_state": dev.confirmed_state,
            "confirmed_op_seq": dev.confirmed_op_seq,
        }

    # ------------------------------------------------------------------ 视图

    def console_view(self, device_id: str) -> dict:
        """控制台视图：期望状态、最后确认状态、仍待确认的命令。"""
        dev = self._device(device_id)
        return {
            "device": device_id,
            "expected_state": dev.expected_state,
            "last_confirmed_state": dev.confirmed_state,
            "confirmed_op_seq": dev.confirmed_op_seq,
            "current_intent": (
                None
                if dev.current_action is None
                else {"op_seq": dev.current_op_seq, "action": dev.current_action}
            ),
            "pending_commands": [
                {"cmd_id": r.cmd_id, "op_seq": r.op_seq, "action": r.action}
                for r in dev.sends
                if r.status == STATUS_PENDING
            ],
        }

    def send_history(self, device_id: str) -> List[dict]:
        """完整发送历史（含重发与各自回执结果）。"""
        dev = self._device(device_id)
        return [r.as_dict() for r in dev.sends]

    def device_ids(self) -> List[str]:
        return sorted(self._devices)

    def _device(self, device_id: str) -> DeviceRecord:
        try:
            return self._devices[device_id]
        except KeyError:
            raise UnknownDeviceError(device_id)
