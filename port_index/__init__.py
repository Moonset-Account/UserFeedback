"""复合多键索引组件。

对外暴露：

* :class:`Port` —— 机架端口（机房 + 整数速率）；
* :class:`PortCompositeIndex` —— 支持新增、替换、删除文档的复合索引；
* :class:`QueryResult` —— 查询结果，含候选数与回表次数统计。
"""

from .index import Port, PortCompositeIndex, QueryResult

__all__ = ["Port", "PortCompositeIndex", "QueryResult"]
