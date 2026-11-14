"""内存系统适配器：模拟系统剪贴板、焦点窗口、确认弹框与操作时钟。

用于把 复制 -> 弹框 -> 切换窗口 -> 重新粘贴 的完整链路串起来，
不触碰真实系统剪贴板或任何外部系统。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Deque, List, Optional, Tuple


@dataclass
class ClipboardState:
    version: int
    payload: str
    text_representation: str
    content_kind: str          # "text" | "image"
    classification: str
    source_workspace: str


class InMemorySystemAdapter:
    """系统适配器的内存实现。

    - copy(): 写入剪贴板，版本号自增（内容变化 => 版本变化）。
    - focus(): 切换焦点窗口（目标工作区）。
    - answer(): 预置确认弹框的回答。
    - tick(): 推进操作时钟。
    - paste(): 触发一次裁决并把交付结果记录到 pasted。
    """

    def __init__(self) -> None:
        self.clipboard: Optional[ClipboardState] = None
        self.focused_workspace: Optional[str] = None
        self.clock: float = 0.0
        self._answers: Deque[bool] = deque()
        self.dialog_prompts: List[str] = []
        # 已交付的粘贴：(目标工作区, 动作, 交付内容)
        self.pasted: List[Tuple[str, str, Optional[str]]] = []

    # -- 测试/演示脚本操作 -------------------------------------------------

    def copy(self, payload: str, content_kind: str, classification: str,
             workspace: str, text_representation: Optional[str] = None) -> int:
        version = (self.clipboard.version + 1) if self.clipboard else 1
        if text_representation is None:
            text_representation = payload if content_kind == "text" else ""
        self.clipboard = ClipboardState(
            version=version,
            payload=payload,
            text_representation=text_representation,
            content_kind=content_kind,
            classification=classification,
            source_workspace=workspace,
        )
        return version

    def focus(self, workspace: str) -> None:
        self.focused_workspace = workspace

    def answer(self, value: bool) -> None:
        self._answers.append(value)

    def tick(self, seconds: float) -> None:
        self.clock += seconds

    def paste(self, adjudicator):
        """触发一次粘贴裁决，并把交付结果记录下来。"""
        result = adjudicator.handle_paste(self)
        if result.action.value in ("paste", "paste_text"):
            self.pasted.append((self.focused_workspace,
                                result.action.value,
                                result.delivered_payload))
        return result

    # -- 裁决器调用的适配器接口 ---------------------------------------------

    def get_source_workspace(self) -> str:
        self._require_clipboard()
        return self.clipboard.source_workspace

    def get_target_workspace(self) -> str:
        if self.focused_workspace is None:
            raise RuntimeError("no focused window")
        return self.focused_workspace

    def get_classification(self) -> str:
        self._require_clipboard()
        return self.clipboard.classification

    def get_content_kind(self) -> str:
        self._require_clipboard()
        return self.clipboard.content_kind

    def get_clipboard_version(self) -> int:
        self._require_clipboard()
        return self.clipboard.version

    def get_payload(self) -> str:
        self._require_clipboard()
        return self.clipboard.payload

    def get_text_representation(self) -> str:
        self._require_clipboard()
        return self.clipboard.text_representation

    def now(self) -> float:
        return self.clock

    def confirm(self, prompt: str) -> bool:
        self.dialog_prompts.append(prompt)
        if self._answers:
            return self._answers.popleft()
        return False  # 无预置回答时默认拒绝（安全缺省）

    def _require_clipboard(self) -> None:
        if self.clipboard is None:
            raise RuntimeError("clipboard is empty")
