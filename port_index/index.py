"""文档 ``ports`` 数组上的内存复合多键索引。

文档结构::

    {
        "id": "d1",
        "ports": [
            {"room": "west", "speed": 1000},
            {"room": "east", "speed": 9000},
        ],
    }

索引为每个端口元素维护一条有序条目 ``(room, speed, doc_id)``。
同一文档里两个完全相同的端口会产生两条条目，这与 B 树多键索引
"数组每个元素一条索引键" 的语义一致；删除其中一个重复端口时，
另一条条目仍然保留。

两种查询语义：

* ``same_element=True``（默认）：机房与速率下限必须由**同一个**端口
  元素满足。复合索引条目本身即可证明该事实，因此**回表次数为 0**。
* ``same_element=False``：允许同一文档的**两个不同元素**分别满足
  机房条件和速率条件。复合多键索引无法用单条条目证明这种跨元素
  合取，需要把机房分片内的候选文档逐一回表过滤。

无论一个文档有多少个匹配端口，结果中文档只出现一次。
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

# 用于在有序条目列表中构造 "某个 room 分片的上界"。
# 条目的 speed 是有限整数，必定小于 inf；第三分量不会真正参与比较。
_INF = float("inf")


@dataclass(frozen=True)
class Port:
    """一个机架端口：所属机房与整数速率。"""

    room: str
    speed: int

    @classmethod
    def from_mapping(cls, raw: Any) -> "Port":
        if not isinstance(raw, dict):
            raise TypeError("每个端口必须是对象/映射")
        if "room" not in raw or "speed" not in raw:
            raise ValueError("每个端口必须包含 'room' 和 'speed' 字段")
        room = raw["room"]
        speed = raw["speed"]
        if not isinstance(room, str) or not room:
            raise TypeError("端口 'room' 必须是非空字符串")
        # bool 是 int 的子类，需要显式排除。
        if isinstance(speed, bool) or not isinstance(speed, int):
            raise TypeError("端口 'speed' 必须是整数")
        if speed < 0:
            raise ValueError("端口 'speed' 不能为负数")
        return cls(room=room, speed=speed)


@dataclass(frozen=True)
class QueryResult:
    """一次索引查询的结果与执行统计。

    属性说明：

    * ``doc_ids``：去重后的匹配文档 id，按索引键顺序排列；
    * ``entries_examined``：扫描过的索引条目数（去重前的候选条目数）；
    * ``candidate_docs``：扫描条目中涉及的去重候选文档数；
    * ``fetches``：回表次数，即真正回到文档存储中取整条文档的次数；
    * ``same_element``：是否要求机房与速率下限出自同一端口元素。
    """

    doc_ids: Tuple[str, ...]
    entries_examined: int
    candidate_docs: int
    fetches: int
    same_element: bool


class PortCompositeIndex:
    """``ports`` 数组上的有序复合多键索引。"""

    def __init__(self) -> None:
        # 文档存储：doc_id -> 文档副本（回表时从这里取）。
        self._docs: Dict[str, dict] = {}
        # 有序索引条目：(room, speed, doc_id)，每个端口元素一条。
        self._entries: List[Tuple[str, int, str]] = []

    # ------------------------------------------------------------------
    # 基本属性
    # ------------------------------------------------------------------

    @property
    def entry_count(self) -> int:
        """当前索引条目总数（重复端口各计一条）。"""
        return len(self._entries)

    def __len__(self) -> int:
        return len(self._docs)

    def __contains__(self, doc_id: object) -> bool:
        return doc_id in self._docs

    def get_document(self, doc_id: str) -> dict:
        """按 id 取回文档（回表动作的底层接口）。"""
        return self._docs[doc_id]

    # ------------------------------------------------------------------
    # 写操作：新增 / 替换 / 删除
    # ------------------------------------------------------------------

    def add_document(self, document: dict) -> None:
        """新增一个文档；文档 id 已存在时抛出 ``ValueError``。"""
        doc_id, ports = self._validate_document(document)
        if doc_id in self._docs:
            raise ValueError(
                f"文档 {doc_id!r} 已在索引中，请使用 replace_document 替换"
            )
        self._docs[doc_id] = self._freeze_document(doc_id, ports)
        for port in ports:
            self._insert_entry((port.room, port.speed, doc_id))

    def replace_document(self, document: dict) -> None:
        """整体替换一个已存在文档；文档不存在时抛出 ``KeyError``。

        旧文档的全部索引条目先移除，再按新端口重新建条目，
        因此删掉某个重复端口后，对应条目也会同步消失。
        """
        doc_id, ports = self._validate_document(document)
        if doc_id not in self._docs:
            raise KeyError(f"文档 {doc_id!r} 不在索引中，请使用 add_document 新增")
        self._remove_document_entries(doc_id)
        self._docs[doc_id] = self._freeze_document(doc_id, ports)
        for port in ports:
            self._insert_entry((port.room, port.speed, doc_id))

    def delete_document(self, doc_id: str) -> None:
        """删除文档及其全部索引条目；文档不存在时抛出 ``KeyError``。"""
        if doc_id not in self._docs:
            raise KeyError(f"文档 {doc_id!r} 不在索引中")
        self._remove_document_entries(doc_id)
        del self._docs[doc_id]

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def query(
        self, room: str, min_speed: int, *, same_element: bool = True
    ) -> QueryResult:
        """查询拥有满足条件端口的文档。

        参数：

        * ``room``：机房名称，端口的 room 必须与之相等；
        * ``min_speed``：速率下限（含），端口 speed 必须大于等于它；
        * ``same_element``：为 ``True`` 时两个条件必须由同一个端口
          元素满足；为 ``False`` 时允许同一文档的两个元素分别满足。

        同一个文档即使有多个匹配端口，结果中也只返回一次。
        """
        if not isinstance(room, str) or not room:
            raise TypeError("room 必须是非空字符串")
        if isinstance(min_speed, bool) or not isinstance(min_speed, int):
            raise TypeError("min_speed 必须是整数")
        if min_speed < 0:
            raise ValueError("min_speed 不能为负数")

        if same_element:
            return self._query_same_element(room, min_speed)
        return self._query_split_element(room, min_speed)

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _query_same_element(self, room: str, min_speed: int) -> QueryResult:
        # 复合键 (room, speed, doc_id) 上的范围扫描：
        # 下界定在 (room, min_speed)，上界定在 room 分片末尾。
        lo = bisect_left(self._entries, (room, min_speed))
        hi = self._room_hi(room)

        seen: set = set()
        doc_ids: List[str] = []
        for entry in self._entries[lo:hi]:
            doc_id = entry[2]
            if doc_id not in seen:
                seen.add(doc_id)
                doc_ids.append(doc_id)

        # 索引条目本身同时携带 room 与 speed，无需回表。
        return QueryResult(
            doc_ids=tuple(doc_ids),
            entries_examined=hi - lo,
            candidate_docs=len(seen),
            fetches=0,
            same_element=True,
        )

    def _query_split_element(self, room: str, min_speed: int) -> QueryResult:
        # 跨元素合取无法由单条复合键证明：先扫出该 room 分片的全部
        # 条目，去重得到候选文档，再逐篇回表检查 "存在 room 端口"
        # 与 "存在高速率端口" 两个独立存在性条件。
        lo = bisect_left(self._entries, (room,))
        hi = self._room_hi(room)

        candidate_order: List[str] = []
        seen: set = set()
        for entry in self._entries[lo:hi]:
            doc_id = entry[2]
            if doc_id not in seen:
                seen.add(doc_id)
                candidate_order.append(doc_id)

        matched: List[str] = []
        for doc_id in candidate_order:
            document = self._docs[doc_id]  # 回表
            ports = document["ports"]
            has_room = any(port["room"] == room for port in ports)
            has_speed = any(port["speed"] >= min_speed for port in ports)
            if has_room and has_speed:
                matched.append(doc_id)

        return QueryResult(
            doc_ids=tuple(matched),
            entries_examined=hi - lo,
            candidate_docs=len(seen),
            fetches=len(seen),
            same_element=False,
        )

    def _room_hi(self, room: str) -> int:
        """room 分片（任意 speed / doc_id）之后的第一个位置。"""
        return bisect_right(self._entries, (room, _INF, _INF))

    def _insert_entry(self, entry: Tuple[str, int, str]) -> None:
        position = bisect_left(self._entries, entry)
        self._entries.insert(position, entry)

    def _remove_document_entries(self, doc_id: str) -> None:
        self._entries = [entry for entry in self._entries if entry[2] != doc_id]

    @staticmethod
    def _validate_document(document: Any) -> Tuple[str, List[Port]]:
        if not isinstance(document, dict):
            raise TypeError("文档必须是映射类型")
        doc_id = document.get("id")
        if not isinstance(doc_id, str) or not doc_id:
            raise ValueError("文档 'id' 必须是非空字符串")
        raw_ports = document.get("ports")
        if not isinstance(raw_ports, list):
            raise TypeError("文档 'ports' 必须是数组")
        ports = [Port.from_mapping(raw) for raw in raw_ports]
        return doc_id, ports

    @staticmethod
    def _freeze_document(doc_id: str, ports: List[Port]) -> dict:
        # 存副本，避免调用方之后修改入参而破坏索引一致性。
        return {
            "id": doc_id,
            "ports": [{"room": port.room, "speed": port.speed} for port in ports],
        }
