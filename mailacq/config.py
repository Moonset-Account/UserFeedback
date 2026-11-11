"""项目路径与固定配置。"""
from __future__ import annotations

from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
GENERATED_DIR = ROOT_DIR / "generated"
INSTANCE_DIR = ROOT_DIR / "instance"
RUNS_DIR = INSTANCE_DIR / "runs"

# 模拟邮箱的虚构登录凭据（任意值均可登录，这里仅用于界面展示）
MOCK_USERNAME = "lab-reader@example.test"
MOCK_PASSWORD = "fictional-password"
MAILBOX = "INBOX"

# 采集器声明支持的 IMAP 协议子集（README 中有完整说明）
SUPPORTED_IMAP_SUBSET = (
    "IMAP4rev1 只读子集: CAPABILITY/LOGIN/LOGOUT/NOOP/SELECT(EXAMINE, READ-ONLY)/"
    "STATUS/UID SEARCH/UID FETCH (UID FLAGS RFC822); 拒绝 STORE/APPEND/DELETE/EXPUNGE"
)


def ensure_dirs() -> None:
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
