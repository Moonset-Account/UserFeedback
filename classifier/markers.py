"""多语言明确标记库。

标记按语义意图分组（而非单个关键词），并要求标记出现在特定区域或满足组合条件，
避免“正文里出现‘维护’两个字就误判维护页”的单关键词陷阱。
所有词条均为虚构源站模板中实际使用的文案。
"""

from __future__ import annotations

# 空结果的显式标记：出现在空状态区域（#empty-state / .empty-block / role=status）
EMPTY_MARKERS = {
    "zh": [
        "未找到符合条件的资料",
        "没有匹配的资料",
        "暂无相关资料",
        "没有检索到结果",
        "当前条件下暂无资料",
    ],
    "en": [
        "No matching documents found",
        "No results found",
        "There are no documents matching your criteria",
    ],
    "ja": [
        "該当する資料はありません",
        "検索結果が見つかりませんでした",
    ],
}

# 维护标记：要求出现在维护横幅 / 标题 / 维护专用区域
MAINTENANCE_MARKERS = {
    "zh": [
        "系统维护中",
        "站点维护",
        "维护升级",
        "例行维护",
        "服务暂时不可用，预计",
    ],
    "en": [
        "Under maintenance",
        "Scheduled maintenance",
        "Service temporarily unavailable",
    ],
    "ja": [
        "メンテナンス中",
        "定期メンテナンス",
    ],
}

# 访问提示标记：登录 / 权限 / 验证
ACCESS_MARKERS = {
    "zh": [
        "请先登录后查看",
        "登录后即可查看完整资料",
        "当前账号无权访问",
        "权限不足",
        "完成安全验证后继续",
    ],
    "en": [
        "Please sign in to continue",
        "You do not have permission to access",
        "Verify you are human",
    ],
    "ja": [
        "ログインしてください",
        "アクセス権限がありません",
    ],
}

# 错误页标记（HTTP 200 包裹的应用错误）
ERROR_MARKERS = {
    "zh": [
        "应用程序发生错误",
        "服务器开小差了",
        "页面加载失败，请稍后重试",
        "请求处理失败",
    ],
    "en": [
        "Application error",
        "Something went wrong",
        "Failed to load this page",
    ],
    "ja": [
        "アプリケーションエラー",
        "ページの読み込みに失敗しました",
    ],
}

# 维护专用区域 / 横幅的结构标识
MAINTENANCE_REGION_HINTS = ("maintenance", "maintain-banner", "maintenance-banner")
EMPTY_REGION_HINTS = ("empty-state", "empty-block", "no-result", "noresult")
ACCESS_REGION_HINTS = ("login-gate", "access-notice", "permission-tip", "captcha")
ERROR_REGION_HINTS = ("error-panel", "error-page", "app-error")

# 截断 / 残缺的硬信号
TRUNCATION_MARKERS = [
    "渲染中断：模板执行未完成",
    "Render aborted: template incomplete",
    "テンプレート描画が中断されました",
]
SKELETON_HINTS = ("skeleton", "placeholder-block", "loading-placeholder")


def all_markers(table: dict[str, list[str]]) -> list[str]:
    out: list[str] = []
    for words in table.values():
        out.extend(words)
    return out
