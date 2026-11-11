// SPDX-License-Identifier: MIT
// 报告生成：把渠道判定与分歧归因序列化为 JSON，并提供人类可读文本视图。
#pragma once

#include <string>

#include "certattrib/engine.hpp"
#include "certattrib/json.hpp"

namespace certattrib {

JsonPtr channelVerdictToJson(const ChannelVerdict& v);
JsonPtr divergenceToJson(const DivergenceReport& r);

// 一份完整报告：全部叶证书在全部渠道下的判定 + 两两渠道分歧。
JsonPtr buildFullReport(const Engine& engine, const Dataset& ds);

std::string renderTextReport(const JsonPtr& report);

}  // namespace certattrib
