# -*- coding: utf-8 -*-
"""科研测量数值采集器（locale-aware measurement collector）。

模块划分：
- locales:   地区数字格式规则（小数点 / 分组分隔符）
- parser:    单条数值字面量解析（保留原文，输出逐步解释）
- units:     计量单位白名单校验
- extractor: 测量记录解析（限定符、不确定度、区间、缺失值、精度）
- fetchers:  规定公开接口采集（文件 / HTTP / 浏览器 DOM）
- tasks:     采集任务编排与重解释
"""

__all__ = ["locales", "parser", "units", "extractor", "fetchers", "tasks"]
