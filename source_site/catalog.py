"""公开场景目录 —— 只含可对外暴露的元数据，不含任何期望分类真值。

控制台（webapp）只允许导入本模块；期望分类真值仅存在于 scenarios.py，
且仅供 tests/ 使用。
"""

from __future__ import annotations

# (id, 路径, HTTP 状态, 标题, 场景说明)
PUBLIC_SCENARIOS: list[dict] = [
    {"id": "normal", "path": "/scenarios/normal", "status": 200,
     "title": "正常资料页",
     "note": "完整结构地标 + 4 张资料卡片 + 数据表格；公告含“维护”字样但在普通公告区"},
    {"id": "normal-keyword", "path": "/scenarios/normal-keyword", "status": 200,
     "title": "含“维护”关键词的正常正文",
     "note": "正文多次出现“维护”，但无维护专用区域 —— 单关键词不得判为维护页"},
    {"id": "empty-zh", "path": "/scenarios/empty-zh", "status": 200,
     "title": "合法空结果（中文）",
     "note": "空状态区域带 role=status 与明确空结果标记，必须与采集失败分开"},
    {"id": "empty-en", "path": "/scenarios/empty-en", "status": 200,
     "title": "合法空结果（英文）", "note": "多语言空结果标记识别"},
    {"id": "empty-ja", "path": "/scenarios/empty-ja", "status": 200,
     "title": "合法空结果（日文）", "note": "多语言空结果标记识别"},
    {"id": "maintenance-zh", "path": "/scenarios/maintenance-zh", "status": 200,
     "title": "维护页面（中文）",
     "note": "维护横幅专用区域 + 维护标记，HTTP 仍为 200"},
    {"id": "maintenance-en", "path": "/scenarios/maintenance-en", "status": 200,
     "title": "维护页面（英文）", "note": "多语言维护标记"},
    {"id": "maintenance-ja", "path": "/scenarios/maintenance-ja", "status": 200,
     "title": "维护页面（日文）", "note": "多语言维护标记"},
    {"id": "access-login-zh", "path": "/scenarios/access-login-zh", "status": 200,
     "title": "登录门禁提示", "note": "登录提示区域，非错误页"},
    {"id": "access-permission-zh", "path": "/scenarios/access-permission-zh",
     "status": 200, "title": "权限不足提示", "note": "权限区域，与错误页区分"},
    {"id": "access-captcha-zh", "path": "/scenarios/access-captcha-zh",
     "status": 200, "title": "安全验证提示", "note": "人机验证区域"},
    {"id": "access-en", "path": "/scenarios/access-en", "status": 200,
     "title": "访问提示（英文）", "note": "多语言访问标记"},
    {"id": "access-ja", "path": "/scenarios/access-ja", "status": 200,
     "title": "访问提示（日文）", "note": "多语言访问标记"},
    {"id": "error-zh", "path": "/scenarios/error-zh", "status": 200,
     "title": "应用错误页（中文，HTTP 200）",
     "note": "与正常模板共用 header/nav/breadcrumb/footer，错误面板区分"},
    {"id": "error-en", "path": "/scenarios/error-en", "status": 200,
     "title": "应用错误页（英文，HTTP 200）",
     "note": "与正常模板高度相似的错误页"},
    {"id": "broken-truncated", "path": "/scenarios/broken-truncated",
     "status": 200, "title": "模板截断页",
     "note": "截断声明 + 未闭合标签 + 无 footer，残缺硬门控"},
    {"id": "broken-skeleton", "path": "/scenarios/broken-skeleton",
     "status": 200, "title": "骨架占位页",
     "note": "骨架块占比 ≥30%，无实质内容"},
    {"id": "broken-unclosed", "path": "/scenarios/broken-unclosed",
     "status": 200, "title": "标签未闭合页",
     "note": "无截断声明但 ≥3 个未闭合容器，结构契约失败"},
    {"id": "ambiguous", "path": "/scenarios/ambiguous", "status": 200,
     "title": "自相矛盾歧义页",
     "note": "空状态标记与资料卡片同时存在，应隔离而非强行归类"},
    {"id": "uncertain-busy", "path": "/scenarios/uncertain-busy", "status": 200,
     "title": "信号稀薄忙碌页",
     "note": "无标记、无资料、无骨架，置信不足应隔离"},
    {"id": "maintenance-500", "path": "/scenarios/maintenance-500",
     "status": 500, "title": "非 200 维护页",
     "note": "验证语义判定不被状态码绑死：500 下仍是维护语义"},
]

PUBLIC_TRANSPORT_SCENARIOS: list[dict] = [
    {"id": "tcp-reset", "path": "/__transport/reset", "status": None,
     "title": "连接被重置",
     "note": "源站直接关闭连接 —— 采集失败，与合法空结果严格分开"},
]

DEMO_SCENARIO_IDS = ["normal", "empty-zh"]


def public_catalog() -> list[dict]:
    """返回全部场景的公开副本（调用方修改不影响注册表）。"""
    return [dict(s) for s in PUBLIC_SCENARIOS + PUBLIC_TRANSPORT_SCENARIOS]
