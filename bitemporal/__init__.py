"""双时态关系表：业务有效期 + 录入提交号。

公开 API 见 :mod:`bitemporal.table`，并在本包根重新导出。
"""

from .table import BitemporalTable, Record, Segment

__all__ = ["BitemporalTable", "Record", "Segment"]
