"""电话坐席代办高风险操作的确认服务（defensive confirmation）。

公开 API
--------
* ``Field`` / ``Candidate`` / ``CandidateBook`` —— 候选操作与字段别名；
* ``Segment`` / ``PlaybackEvent`` / ``RepetitionEvent`` —— 播报与事件；
* ``HomophoneTable`` —— 显式同音别名等价类；
* ``plan_confirmation`` —— 在时长预算内选最短区分播报；
* ``ConfirmationSession`` —— 消费事件、提交时重算区分性并做复诵唯一性校验。
"""

from .model import (
    Candidate,
    CandidateBook,
    Field,
    PlaybackEvent,
    RepetitionEvent,
    Segment,
)
from .planner import ConfirmationPlan, plan_confirmation, sound_signature
from .session import (
    ConfirmationSession,
    SubmitResult,
    SubmitStatus,
    candidates_consistent_with_repetitions,
)
from .sound import HomophoneTable, base_sound_key, sound_key

__all__ = [
    "Field",
    "Candidate",
    "CandidateBook",
    "Segment",
    "PlaybackEvent",
    "RepetitionEvent",
    "HomophoneTable",
    "ConfirmationPlan",
    "ConfirmationSession",
    "SubmitResult",
    "SubmitStatus",
    "plan_confirmation",
    "sound_signature",
    "sound_key",
    "base_sound_key",
    "candidates_consistent_with_repetitions",
]
