"""告警事件生命周期核心模块。

办公终端的拦截上报会在短时间内刷出一串相同告警。本模块按
(租户 tenant, 终端 endpoint, 检测项 detection) 三元组管理告警事件:

- 首次命中立即通知(每个新事件只通知一次);
- 首次命中后十分钟聚合窗口内的重复命中汇总到同一事件, 不重复通知, 计数保留;
- 收到"已确认处置"的回执即关闭事件;
- 关闭前发生的迟到上报归回旧事件(计入 late_count, 不重开、不通知),
  关闭后发生的新上报开启新事件(重新通知);
- 重复 report_id 不增加计数(全局去重);
- advance_to() 显式推进时间, 使聚合窗口已结束的打开事件到期(EXPIRED);
  到期事件窗口内发生的迟到上报仍归回该事件。

归属判定一律使用上报自带的"发生时间 occurred_at", 与"到达时间 arrived_at"
无关, 因此延迟到达的上报仍能归回正确的事件。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

OPEN = "OPEN"
CLOSED = "CLOSED"
EXPIRED = "EXPIRED"

DEFAULT_WINDOW_SECONDS = 600  # 十分钟聚合窗口

CONFIRMED = "confirmed"


def parse_time(value):
    """把 epoch 秒(int/float)或 ISO-8601 字符串归一化为 UTC aware datetime。"""
    if isinstance(value, bool):
        raise ValueError("invalid time value: %r" % (value,))
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z") or text.endswith("z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    raise ValueError("unsupported time value: %r" % (value,))


def format_time(dt):
    """把 datetime 格式化为 UTC ISO-8601(带 Z 后缀)。"""
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class AlertEvent:
    """一个聚合告警事件。"""

    event_id: str
    tenant: str
    endpoint: str
    detection: str
    first_occurred_at: datetime
    window_seconds: int = DEFAULT_WINDOW_SECONDS
    state: str = OPEN
    count: int = 0            # 打开期间聚合的去重上报数
    late_count: int = 0       # 关闭/到期后归回本事件的迟到上报数
    last_occurred_at: datetime = None
    closed_at: datetime = None
    close_reason: str = None
    report_ids: set = field(default_factory=set)

    @property
    def key(self):
        return (self.tenant, self.endpoint, self.detection)

    @property
    def window_end(self):
        """聚合窗口为 [first_occurred_at, window_end) 左闭右开。"""
        return self.first_occurred_at + timedelta(seconds=self.window_seconds)

    @property
    def total_count(self):
        return self.count + self.late_count

    def to_dict(self):
        return {
            "event_id": self.event_id,
            "tenant": self.tenant,
            "endpoint": self.endpoint,
            "detection": self.detection,
            "state": self.state,
            "count": self.count,
            "late_count": self.late_count,
            "total_count": self.total_count,
            "first_occurred_at": format_time(self.first_occurred_at),
            "last_occurred_at": format_time(self.last_occurred_at),
            "window_end": format_time(self.window_end),
            "closed_at": format_time(self.closed_at),
            "close_reason": self.close_reason,
            "report_ids": sorted(self.report_ids),
        }


class AlertManager:
    """按 (租户, 终端, 检测项) 管理告警事件生命周期的核心类。"""

    def __init__(self, window_seconds=DEFAULT_WINDOW_SECONDS):
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self.window_seconds = int(window_seconds)
        self.events = {}        # event_id -> AlertEvent
        self.key_index = {}     # key -> [event_id, ...] 按创建顺序
        self.report_index = {}  # report_id -> event_id (全局去重)
        self.receipt_index = {} # receipt_id -> event_id (回执去重)
        self.notifications = [] # 通知发件箱: 每个新事件恰好一条
        self.now = None         # 显式时钟, 由 advance_to 推进
        self._seq = 0

    # ------------------------------------------------------------------ hits

    def handle_hit(self, report_id, tenant, endpoint, detection,
                   occurred_at, arrived_at):
        """处理一条终端拦截上报, 返回描述处理结果的动作字典。"""
        occurred = parse_time(occurred_at)
        arrived = parse_time(arrived_at)
        key = (tenant, endpoint, detection)

        if report_id in self.report_index:
            # 重复事件 ID: 不增加任何计数。
            original = self.events[self.report_index[report_id]]
            return self._hit_action("duplicate_ignored", original, report_id,
                                    False, occurred, arrived)

        event = self._latest_event(key)

        if event is None:
            event = self._create_event(key, occurred)
            self._register_report(event, report_id, occurred)
            self._notify(event, arrived)
            return self._hit_action("new_event", event, report_id,
                                    True, occurred, arrived)

        if event.state == OPEN:
            if occurred < event.window_end:
                self._register_report(event, report_id, occurred)
                return self._hit_action("aggregated", event, report_id,
                                        False, occurred, arrived)
            # 发生时间已越过聚合窗口: 旧事件到期, 开启新事件并重新通知。
            self._expire(event, "window_expired")
            new_event = self._create_event(key, occurred)
            self._register_report(new_event, report_id, occurred)
            self._notify(new_event, arrived)
            return self._hit_action(
                "new_event", new_event, report_id, True, occurred, arrived,
                extra={"superseded_event_id": event.event_id})

        if event.state == CLOSED:
            if occurred <= event.closed_at:
                # 关闭前发生的迟到上报: 归回旧事件, 不重开、不通知。
                self._register_late(event, report_id, occurred)
                return self._hit_action("late_attributed", event, report_id,
                                        False, occurred, arrived)
            # 关闭后发生的新上报: 开启新事件。
            new_event = self._create_event(key, occurred)
            self._register_report(new_event, report_id, occurred)
            self._notify(new_event, arrived)
            return self._hit_action("new_event", new_event, report_id,
                                    True, occurred, arrived)

        # event.state == EXPIRED
        if occurred < event.window_end:
            # 窗口内发生、到期后才到达的迟到上报: 归回旧事件。
            self._register_late(event, report_id, occurred)
            return self._hit_action("late_attributed", event, report_id,
                                    False, occurred, arrived)
        new_event = self._create_event(key, occurred)
        self._register_report(new_event, report_id, occurred)
        self._notify(new_event, arrived)
        return self._hit_action("new_event", new_event, report_id,
                                True, occurred, arrived)

    # --------------------------------------------------------------- receipts

    def handle_receipt(self, receipt_id, event_id, disposition, received_at):
        """处理一条处置回执, 返回描述处理结果的动作字典。"""
        received = parse_time(received_at)

        if receipt_id in self.receipt_index:
            return {
                "action": "duplicate_receipt_ignored",
                "receipt_id": receipt_id,
                "event_id": self.receipt_index[receipt_id],
                "received_at": format_time(received),
            }
        self.receipt_index[receipt_id] = event_id

        event = self.events.get(event_id)
        if event is None:
            return {
                "action": "receipt_unknown_event",
                "receipt_id": receipt_id,
                "event_id": event_id,
                "received_at": format_time(received),
            }
        if disposition != CONFIRMED:
            return {
                "action": "receipt_not_confirmed",
                "receipt_id": receipt_id,
                "event_id": event_id,
                "event_state": event.state,
                "disposition": disposition,
                "received_at": format_time(received),
            }
        if event.state == CLOSED:
            return {
                "action": "receipt_already_closed",
                "receipt_id": receipt_id,
                "event_id": event_id,
                "event_state": event.state,
                "received_at": format_time(received),
            }
        # OPEN 或 EXPIRED 事件收到已确认处置回执: 关闭。
        event.state = CLOSED
        event.closed_at = received
        event.close_reason = "disposition_confirmed"
        return {
            "action": "closed",
            "receipt_id": receipt_id,
            "event_id": event_id,
            "event_state": event.state,
            "closed_at": format_time(event.closed_at),
            "count": event.count,
            "late_count": event.late_count,
            "total_count": event.total_count,
            "received_at": format_time(received),
        }

    # ------------------------------------------------------------------ clock

    def advance_to(self, now):
        """显式推进时间; 聚合窗口已结束的打开事件随之到期(EXPIRED)。"""
        now = parse_time(now)
        if self.now is not None and now < self.now:
            raise ValueError("cannot move time backwards: %s < %s"
                             % (format_time(now), format_time(self.now)))
        self.now = now
        expired = []
        for event in self.events.values():
            if event.state == OPEN and event.window_end <= now:
                self._expire(event, "window_expired")
                expired.append(event.event_id)
        return {
            "action": "advanced",
            "now": format_time(now),
            "expired": expired,
        }

    # ----------------------------------------------------------------- introspect

    def snapshot(self):
        """返回当前全部事件与通知的只读快照(字典)。"""
        return {
            "now": format_time(self.now),
            "window_seconds": self.window_seconds,
            "events": [self.events[eid].to_dict() for eid in self._ordered_ids()],
            "notifications": list(self.notifications),
        }

    # ----------------------------------------------------------------- internal

    def _ordered_ids(self):
        return ["EVT-%04d" % i for i in range(1, self._seq + 1)]

    def _latest_event(self, key):
        ids = self.key_index.get(key)
        if not ids:
            return None
        return self.events[ids[-1]]

    def _create_event(self, key, occurred):
        self._seq += 1
        event_id = "EVT-%04d" % self._seq
        event = AlertEvent(
            event_id=event_id,
            tenant=key[0],
            endpoint=key[1],
            detection=key[2],
            first_occurred_at=occurred,
            window_seconds=self.window_seconds,
            last_occurred_at=occurred,
        )
        self.events[event_id] = event
        self.key_index.setdefault(key, []).append(event_id)
        return event

    def _register_report(self, event, report_id, occurred):
        event.report_ids.add(report_id)
        event.count += 1
        if event.last_occurred_at is None or occurred > event.last_occurred_at:
            event.last_occurred_at = occurred
        self.report_index[report_id] = event.event_id

    def _register_late(self, event, report_id, occurred):
        event.report_ids.add(report_id)
        event.late_count += 1
        if event.last_occurred_at is None or occurred > event.last_occurred_at:
            event.last_occurred_at = occurred
        self.report_index[report_id] = event.event_id

    def _notify(self, event, arrived):
        self.notifications.append({
            "event_id": event.event_id,
            "tenant": event.tenant,
            "endpoint": event.endpoint,
            "detection": event.detection,
            "first_occurred_at": format_time(event.first_occurred_at),
            "notified_at": format_time(arrived),
        })

    def _expire(self, event, reason):
        event.state = EXPIRED
        event.close_reason = reason

    def _hit_action(self, action, event, report_id, notified,
                    occurred, arrived, extra=None):
        out = {
            "action": action,
            "report_id": report_id,
            "event_id": event.event_id,
            "tenant": event.tenant,
            "endpoint": event.endpoint,
            "detection": event.detection,
            "event_state": event.state,
            "count": event.count,
            "late_count": event.late_count,
            "total_count": event.total_count,
            "notified": notified,
            "occurred_at": format_time(occurred),
            "arrived_at": format_time(arrived),
        }
        if extra:
            out.update(extra)
        return out
