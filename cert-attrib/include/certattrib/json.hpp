// SPDX-License-Identifier: MIT
// 极简 JSON 解析 / 序列化（C++17，无第三方依赖，离线环境可用）。
// 仅支持对象、数组、字符串、数值、布尔、null；数值统一以 double 存储，
// 本项目所有输入均为字符串标识与整数时间，不依赖浮点精度。
#pragma once

#include <cstdint>
#include <map>
#include <memory>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

namespace certattrib {

class JsonError : public std::runtime_error {
public:
    explicit JsonError(const std::string& msg) : std::runtime_error(msg) {}
};

class JsonValue;
using JsonPtr = std::shared_ptr<JsonValue>;

enum class JsonType { Null, Bool, Number, String, Array, Object };

class JsonValue {
public:
    JsonType type = JsonType::Null;
    bool boolean = false;
    double number = 0.0;
    std::string str;
    std::vector<JsonPtr> arr;
    std::map<std::string, JsonPtr> obj;

    bool is(JsonType t) const { return type == t; }

    // 访问器：缺字段 / 类型不符时抛出 JsonError，错误信息携带字段名。
    JsonPtr at(const std::string& key) const {
        auto it = obj.find(key);
        if (it == obj.end()) throw JsonError("missing required field: " + key);
        return it->second;
    }
    JsonPtr getOr(const std::string& key, JsonPtr fallback) const {
        auto it = obj.find(key);
        return it == obj.end() ? fallback : it->second;
    }
    bool has(const std::string& key) const {
        return obj.find(key) != obj.end();
    }
    const std::string& asString(const std::string& ctx) const {
        if (type != JsonType::String)
            throw JsonError(ctx + ": expected string");
        return str;
    }
    double asNumber(const std::string& ctx) const {
        if (type != JsonType::Number)
            throw JsonError(ctx + ": expected number");
        return number;
    }
    bool asBool(const std::string& ctx) const {
        if (type != JsonType::Bool)
            throw JsonError(ctx + ": expected bool");
        return boolean;
    }
    const std::vector<JsonPtr>& asArray(const std::string& ctx) const {
        if (type != JsonType::Array)
            throw JsonError(ctx + ": expected array");
        return arr;
    }
    const std::map<std::string, JsonPtr>& asObject(const std::string& ctx) const {
        if (type != JsonType::Object)
            throw JsonError(ctx + ": expected object");
        return obj;
    }
};

namespace json_detail {

class Parser {
public:
    explicit Parser(const std::string& text) : s_(text), pos_(0) {}

    JsonPtr parse() {
        skipWs();
        JsonPtr v = parseValue();
        skipWs();
        if (pos_ != s_.size())
            throw JsonError("trailing characters at offset " + std::to_string(pos_));
        return v;
    }

private:
    const std::string& s_;
    size_t pos_;

    void skipWs() {
        while (pos_ < s_.size()) {
            char c = s_[pos_];
            if (c == ' ' || c == '\t' || c == '\n' || c == '\r')
                ++pos_;
            else
                break;
        }
    }

    char peek() {
        if (pos_ >= s_.size()) throw JsonError("unexpected end of input");
        return s_[pos_];
    }

    JsonPtr parseValue() {
        skipWs();
        char c = peek();
        switch (c) {
            case '{': return parseObject();
            case '[': return parseArray();
            case '"': {
                auto v = std::make_shared<JsonValue>();
                v->type = JsonType::String;
                v->str = parseString();
                return v;
            }
            case 't': case 'f': return parseBool();
            case 'n': return parseNull();
            default:
                if (c == '-' || (c >= '0' && c <= '9')) return parseNumber();
                throw JsonError(std::string("unexpected character '") + c +
                                "' at offset " + std::to_string(pos_));
        }
    }

    JsonPtr parseObject() {
        auto v = std::make_shared<JsonValue>();
        v->type = JsonType::Object;
        ++pos_; // {
        skipWs();
        if (peek() == '}') { ++pos_; return v; }
        while (true) {
            skipWs();
            if (peek() != '"')
                throw JsonError("object key must be a string at offset " +
                                std::to_string(pos_));
            std::string key = parseString();
            skipWs();
            if (peek() != ':') throw JsonError("expected ':' after key");
            ++pos_;
            v->obj[key] = parseValue();
            skipWs();
            char c = peek();
            if (c == ',') { ++pos_; continue; }
            if (c == '}') { ++pos_; return v; }
            throw JsonError("expected ',' or '}' in object");
        }
    }

    JsonPtr parseArray() {
        auto v = std::make_shared<JsonValue>();
        v->type = JsonType::Array;
        ++pos_; // [
        skipWs();
        if (peek() == ']') { ++pos_; return v; }
        while (true) {
            v->arr.push_back(parseValue());
            skipWs();
            char c = peek();
            if (c == ',') { ++pos_; continue; }
            if (c == ']') { ++pos_; return v; }
            throw JsonError("expected ',' or ']' in array");
        }
    }

    std::string parseString() {
        ++pos_; // opening quote
        std::string out;
        while (pos_ < s_.size()) {
            char c = s_[pos_++];
            if (c == '"') return out;
            if (c == '\\') {
                if (pos_ >= s_.size()) throw JsonError("bad escape at end");
                char e = s_[pos_++];
                switch (e) {
                    case '"': out.push_back('"'); break;
                    case '\\': out.push_back('\\'); break;
                    case '/': out.push_back('/'); break;
                    case 'b': out.push_back('\b'); break;
                    case 'f': out.push_back('\f'); break;
                    case 'n': out.push_back('\n'); break;
                    case 'r': out.push_back('\r'); break;
                    case 't': out.push_back('\t'); break;
                    case 'u': {
                        if (pos_ + 4 > s_.size())
                            throw JsonError("bad unicode escape");
                        unsigned code = 0;
                        for (int i = 0; i < 4; ++i) {
                            char h = s_[pos_++];
                            code <<= 4;
                            if (h >= '0' && h <= '9') code |= static_cast<unsigned>(h - '0');
                            else if (h >= 'a' && h <= 'f') code |= static_cast<unsigned>(h - 'a' + 10);
                            else if (h >= 'A' && h <= 'F') code |= static_cast<unsigned>(h - 'A' + 10);
                            else throw JsonError("bad hex digit in unicode escape");
                        }
                        // 仅处理 BMP，合成验收数据为 ASCII，足够使用。
                        if (code < 0x80) out.push_back(static_cast<char>(code));
                        else if (code < 0x800) {
                            out.push_back(static_cast<char>(0xC0 | (code >> 6)));
                            out.push_back(static_cast<char>(0x80 | (code & 0x3F)));
                        } else {
                            out.push_back(static_cast<char>(0xE0 | (code >> 12)));
                            out.push_back(static_cast<char>(0x80 | ((code >> 6) & 0x3F)));
                            out.push_back(static_cast<char>(0x80 | (code & 0x3F)));
                        }
                        break;
                    }
                    default: throw JsonError(std::string("bad escape \\") + e);
                }
            } else {
                out.push_back(c);
            }
        }
        throw JsonError("unterminated string");
    }

    JsonPtr parseBool() {
        if (s_.compare(pos_, 4, "true") == 0) {
            pos_ += 4;
            auto v = std::make_shared<JsonValue>();
            v->type = JsonType::Bool;
            v->boolean = true;
            return v;
        }
        if (s_.compare(pos_, 5, "false") == 0) {
            pos_ += 5;
            auto v = std::make_shared<JsonValue>();
            v->type = JsonType::Bool;
            v->boolean = false;
            return v;
        }
        throw JsonError("invalid literal");
    }

    JsonPtr parseNull() {
        if (s_.compare(pos_, 4, "null") == 0) {
            pos_ += 4;
            return std::make_shared<JsonValue>();
        }
        throw JsonError("invalid literal");
    }

    JsonPtr parseNumber() {
        size_t start = pos_;
        if (peek() == '-') ++pos_;
        while (pos_ < s_.size() && (s_[pos_] >= '0' && s_[pos_] <= '9')) ++pos_;
        if (pos_ < s_.size() && s_[pos_] == '.') {
            ++pos_;
            while (pos_ < s_.size() && (s_[pos_] >= '0' && s_[pos_] <= '9')) ++pos_;
        }
        if (pos_ < s_.size() && (s_[pos_] == 'e' || s_[pos_] == 'E')) {
            ++pos_;
            if (pos_ < s_.size() && (s_[pos_] == '+' || s_[pos_] == '-')) ++pos_;
            while (pos_ < s_.size() && (s_[pos_] >= '0' && s_[pos_] <= '9')) ++pos_;
        }
        auto v = std::make_shared<JsonValue>();
        v->type = JsonType::Number;
        v->number = std::stod(s_.substr(start, pos_ - start));
        return v;
    }
};

class Writer {
public:
    explicit Writer(bool pretty) : pretty_(pretty) {}

    std::string write(const JsonPtr& v) {
        writeValue(v, 0);
        return out_.str();
    }

private:
    std::ostringstream out_;
    bool pretty_;

    void indent(int depth) {
        if (pretty_) {
            out_ << '\n';
            for (int i = 0; i < depth; ++i) out_ << "  ";
        }
    }

    void writeValue(const JsonPtr& v, int depth) {
        switch (v->type) {
            case JsonType::Null: out_ << "null"; break;
            case JsonType::Bool: out_ << (v->boolean ? "true" : "false"); break;
            case JsonType::Number: {
                // 本项目数值均为整数（epoch 秒、版本号），按整数输出避免 ".0"。
                long long n = static_cast<long long>(v->number);
                out_ << n;
                break;
            }
            case JsonType::String: writeString(v->str); break;
            case JsonType::Array: {
                if (v->arr.empty()) { out_ << "[]"; break; }
                out_ << '[';
                for (size_t i = 0; i < v->arr.size(); ++i) {
                    if (i) out_ << ',';
                    indent(depth + 1);
                    writeValue(v->arr[i], depth + 1);
                }
                indent(depth);
                out_ << ']';
                break;
            }
            case JsonType::Object: {
                if (v->obj.empty()) { out_ << "{}"; break; }
                out_ << '{';
                size_t i = 0;
                for (const auto& kv : v->obj) {
                    if (i++) out_ << ',';
                    indent(depth + 1);
                    writeString(kv.first);
                    out_ << (pretty_ ? ": " : ":");
                    writeValue(kv.second, depth + 1);
                }
                indent(depth);
                out_ << '}';
                break;
            }
        }
    }

    void writeString(const std::string& s) {
        out_ << '"';
        for (char c : s) {
            switch (c) {
                case '"': out_ << "\\\""; break;
                case '\\': out_ << "\\\\"; break;
                case '\b': out_ << "\\b"; break;
                case '\f': out_ << "\\f"; break;
                case '\n': out_ << "\\n"; break;
                case '\r': out_ << "\\r"; break;
                case '\t': out_ << "\\t"; break;
                default:
                    if (static_cast<unsigned char>(c) < 0x20) {
                        char buf[8];
                        std::snprintf(buf, sizeof(buf), "\\u%04x",
                                      static_cast<unsigned char>(c));
                        out_ << buf;
                    } else {
                        out_ << c;
                    }
            }
        }
        out_ << '"';
    }
};

}  // namespace json_detail

inline JsonPtr parseJson(const std::string& text) {
    return json_detail::Parser(text).parse();
}

inline std::string dumpJson(const JsonPtr& v, bool pretty = true) {
    return json_detail::Writer(pretty).write(v);
}

// 构造辅助函数。
inline JsonPtr jNull() { return std::make_shared<JsonValue>(); }
inline JsonPtr jBool(bool b) {
    auto v = std::make_shared<JsonValue>();
    v->type = JsonType::Bool;
    v->boolean = b;
    return v;
}
inline JsonPtr jNum(long long n) {
    auto v = std::make_shared<JsonValue>();
    v->type = JsonType::Number;
    v->number = static_cast<double>(n);
    return v;
}
inline JsonPtr jStr(const std::string& s) {
    auto v = std::make_shared<JsonValue>();
    v->type = JsonType::String;
    v->str = s;
    return v;
}
inline JsonPtr jArr() {
    auto v = std::make_shared<JsonValue>();
    v->type = JsonType::Array;
    return v;
}
inline JsonPtr jObj() {
    auto v = std::make_shared<JsonValue>();
    v->type = JsonType::Object;
    return v;
}

}  // namespace certattrib
