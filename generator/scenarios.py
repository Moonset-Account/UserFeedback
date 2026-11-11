"""虚构实验场景定义。

全部数据均为虚构，不对应任何真实机构、账户或付费资源。
每个场景描述源站对“导出链接”的行为方式，采集器只能通过公开页面/接口观察，
不能直接读取这里的定义（独立真值 ground_truth 仅供测试断言使用）。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ScenarioSpec:
    id: str
    title: str
    description: str
    # 链接页面是否由 JavaScript 渲染（Playwright 子集才能取得链接）
    js_rendered: bool
    # 每个 token 的行为序列，循环使用；含义见 source_site.token_store
    token_plan: tuple[str, ...]
    # 刷新达到第几次后业务版本发生变化（从 1 起计）；None 表示版本不变
    version_bump_on_issue: int | None
    # 数据 ZIP 内部业务身份是否与页面一致；False 用于构造业务身份校验失败
    identity_match: bool
    # 期望采集结果
    expect_outcome: str  # success / failed_expired / failed_version_changed / failed_identity
    expect_attempts: int  # 期望下载尝试次数
    expect_refreshes: int  # 期望链接刷新次数


SCENARIOS: dict[str, ScenarioSpec] = {
    "normal": ScenarioSpec(
        id="normal",
        title="正常场景：一次性链接直接成功",
        description="页面给出短时有效一次性链接，首次下载即成功，链接随即失效。",
        js_rendered=False,
        token_plan=("active",),
        version_bump_on_issue=None,
        identity_match=True,
        expect_outcome="success",
        expect_attempts=1,
        expect_refreshes=0,
    ),
    "pre_expired": ScenarioSpec(
        title="边界场景：下载前链接已过期，刷新后成功",
        description="页面给出的链接在下载前即失效（已过期），采集器识别后重新取得地址并成功。",
        id="pre_expired",
        js_rendered=False,
        token_plan=("expired", "active"),
        version_bump_on_issue=None,
        identity_match=True,
        expect_outcome="success",
        expect_attempts=2,
        expect_refreshes=1,
    ),
    "error_html": ScenarioSpec(
        id="error_html",
        title="边界场景：HTTP 200 但返回错误 HTML",
        description="失效链接返回状态码 200 的错误 HTML 页面，需靠内容类型识别，刷新后成功。",
        js_rendered=False,
        token_plan=("error_html", "active"),
        version_bump_on_issue=None,
        identity_match=True,
        expect_outcome="success",
        expect_attempts=2,
        expect_refreshes=1,
    ),
    "already_used": ScenarioSpec(
        id="already_used",
        title="边界场景：一次性链接已被使用，刷新后成功",
        description="链接已被他人/上次请求消费（一次性），源站返回 410，重新取得地址后成功。",
        js_rendered=False,
        token_plan=("used", "active"),
        version_bump_on_issue=None,
        identity_match=True,
        expect_outcome="success",
        expect_attempts=2,
        expect_refreshes=1,
    ),
    "persistent_expired": ScenarioSpec(
        id="persistent_expired",
        title="边界场景：连续失效触发刷新上限，不能无限重试",
        description="每次取得的链接都已过期，采集器在达到刷新上限后停止并如实报告。",
        js_rendered=False,
        token_plan=("expired",),
        version_bump_on_issue=None,
        identity_match=True,
        expect_outcome="failed_expired",
        expect_attempts=4,  # 1 次初取 + 3 次刷新
        expect_refreshes=3,
    ),
    "version_change": ScenarioSpec(
        id="version_change",
        title="边界场景：刷新后实验版本变化，停止采集",
        description="首次链接过期；刷新后页面版本号变化，说明采集对象已改变，采集器立即停止并说明。",
        js_rendered=False,
        token_plan=("expired", "active"),
        version_bump_on_issue=2,
        identity_match=True,
        expect_outcome="failed_version_changed",
        expect_attempts=1,  # 仅第一次下载；版本变化后不再下载
        expect_refreshes=1,
    ),
    "identity_mismatch": ScenarioSpec(
        id="identity_mismatch",
        title="边界场景：内容业务身份不符（终端失败，不刷新）",
        description="链接可下载且结构合法，但包内实验 ID 与页面声明不一致，业务身份校验失败并终止。",
        js_rendered=False,
        token_plan=("active",),
        version_bump_on_issue=None,
        identity_match=False,
        expect_outcome="failed_identity",
        expect_attempts=1,
        expect_refreshes=0,
    ),
    "js_page": ScenarioSpec(
        id="js_page",
        title="浏览器场景：JavaScript 渲染页面（Playwright 协议子集）",
        description="链接由页面脚本注入 DOM，纯 HTTP 解析无法取得；用 Playwright 等待渲染后采集成功。",
        js_rendered=True,
        token_plan=("active",),
        version_bump_on_issue=None,
        identity_match=True,
        expect_outcome="success",
        expect_attempts=1,
        expect_refreshes=0,
    ),
}

SCENARIO_ORDER = [
    "normal",
    "pre_expired",
    "error_html",
    "already_used",
    "persistent_expired",
    "version_change",
    "identity_mismatch",
    "js_page",
]

# 页面“运行示例”使用的两个小场景（一个正常 + 一个边界），目标 15 秒内结束
DEMO_SCENARIOS = ["normal", "pre_expired"]

# 模拟的实验系列（每个场景一个独立实验）
EXPERIMENT_PREFIX = "EXP-FICTION-2026"
BASE_VERSION = "3.4.2"
VERSION_STEP = "3.4.3"
