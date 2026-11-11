#include "json.hpp"

#include <cmath>
#include <cstdio>
#include <fstream>
#include <sstream>

namespace memtriage {

namespace {
const char* typeName(JsonValue::Type t) {
    switch (t) {
        case JsonValue::Type::Null:   return "null";
        case JsonValue::Type::Bool:   return "boolean";
        case JsonValue::Type::Number: return "number";
        case JsonValue::Type::String: return "string";
        case JsonValue::Type::Array:  return "array";
        case JsonValue::Type::Object: return "object";
    }
    return "unknown";
}
}  // namespace

bool JsonValue::asBool() const {
    if (type_ != Type::Bool) throw JsonError(std::string("expected boolean, got ") + typeName(type_));
    return bool_;
}

double JsonValue::asDouble() const {
    if (type_ != Type::Number) throw JsonError(std::string("expected number, got ") + typeName(type_));
    return num_;
}

std::int64_t JsonValue::asInt64() const {
    if (type_ != Type::Number) throw JsonError(std::string("expected integer, got ") + typeName(type_));
    if (std::isnan(num_) || std::isinf(num_) ||
        num_ < static_cast<double>(INT64_MIN) ||
        num_ > static_cast<double>(INT64_MAX) ||
        num_ != std::floor(num_)) {
        throw JsonError("number is not an integral value in int64 range");
    }
    return static_cast<std::int64_t>(num_);
}

const std::string& JsonValue::asString() const {
    if (type_ != Type::String) throw JsonError(std::string("expected string, got ") + typeName(type_));
    return str_;
}

const std::vector<JsonValue>& JsonValue::asArray() const {
    if (type_ != Type::Array) throw JsonError(std::string("expected array, got ") + typeName(type_));
    return arr_;
}

std::vector<JsonValue>& JsonValue::asArray() {
    if (type_ != Type::Array) throw JsonError(std::string("expected array, got ") + typeName(type_));
    return arr_;
}

const std::map<std::string, JsonValue>& JsonValue::asObject() const {
    if (type_ != Type::Object) throw JsonError(std::string("expected object, got ") + typeName(type_));
    return obj_;
}

std::map<std::string, JsonValue>& JsonValue::asObject() {
    if (type_ != Type::Object) throw JsonError(std::string("expected object, got ") + typeName(type_));
    return obj_;
}

bool JsonValue::contains(const std::string& key) const {
    return type_ == Type::Object && obj_.find(key) != obj_.end();
}

const JsonValue& JsonValue::at(const std::string& key) const {
    if (type_ != Type::Object) throw JsonError(std::string("expected object when looking up '") + key + "'");
    auto it = obj_.find(key);
    if (it == obj_.end()) throw JsonError("missing required key '" + key + "'");
    return it->second;
}

// ---------------------------------------------------------------------------
// Parser
// ---------------------------------------------------------------------------

JsonValue JsonParser::parse(const std::string& text) {
    JsonParser parser(text);
    parser.skipWhitespace();
    JsonValue root = parser.parseValue();
    parser.skipWhitespace();
    if (parser.pos_ != parser.text_.size()) {
        parser.fail("trailing characters after JSON document");
    }
    return root;
}

JsonValue JsonParser::parseFile(const std::string& path) {
    std::ifstream in(path, std::ios::binary);
    if (!in) throw JsonError("cannot open file: " + path);
    std::ostringstream ss;
    ss << in.rdbuf();
    try {
        return parse(ss.str());
    } catch (const JsonError& e) {
        throw JsonError(path + ": " + e.what());
    }
}

void JsonParser::skipWhitespace() {
    while (pos_ < text_.size()) {
        char c = text_[pos_];
        if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
            ++pos_;
        } else {
            break;
        }
    }
}

char JsonParser::peek() const {
    if (pos_ >= text_.size()) throw JsonError("unexpected end of input at " + position());
    return text_[pos_];
}

char JsonParser::get() {
    if (pos_ >= text_.size()) throw JsonError("unexpected end of input at " + position());
    return text_[pos_++];
}

void JsonParser::expect(char c) {
    if (pos_ >= text_.size() || text_[pos_] != c) {
        fail(std::string("expected '") + c + "'");
    }
    ++pos_;
}

void JsonParser::fail(const std::string& msg) const {
    throw JsonError(msg + " at " + position());
}

std::string JsonParser::position() const {
    std::size_t line = 1;
    std::size_t col = 1;
    for (std::size_t i = 0; i < pos_ && i < text_.size(); ++i) {
        if (text_[i] == '\n') {
            ++line;
            col = 1;
        } else {
            ++col;
        }
    }
    std::ostringstream ss;
    ss << "line " << line << " column " << col;
    return ss.str();
}

JsonValue JsonParser::parseValue() {
    skipWhitespace();
    char c = peek();
    switch (c) {
        case '{': return parseObject();
        case '[': return parseArray();
        case '"': return parseString();
        case 't': case 'f': case 'n': return parseLiteral();
        default:
            if (c == '-' || (c >= '0' && c <= '9')) return parseNumber();
            fail("unexpected character");
    }
}

JsonValue JsonParser::parseObject() {
    JsonValue obj = JsonValue::makeObject();
    expect('{');
    skipWhitespace();
    if (peek() == '}') { get(); return obj; }
    while (true) {
        skipWhitespace();
        if (peek() != '"') fail("expected string key in object");
        JsonValue key = parseString();
        skipWhitespace();
        expect(':');
        skipWhitespace();
        JsonValue val = parseValue();
        obj.asObject()[key.asString()] = std::move(val);
        skipWhitespace();
        char c = get();
        if (c == ',') continue;
        if (c == '}') break;
        fail("expected ',' or '}' in object");
    }
    return obj;
}

JsonValue JsonParser::parseArray() {
    JsonValue arr = JsonValue::makeArray();
    expect('[');
    skipWhitespace();
    if (peek() == ']') { get(); return arr; }
    while (true) {
        skipWhitespace();
        arr.asArray().push_back(parseValue());
        skipWhitespace();
        char c = get();
        if (c == ',') continue;
        if (c == ']') break;
        fail("expected ',' or ']' in array");
    }
    return arr;
}

JsonValue JsonParser::parseString() {
    return JsonValue::makeString(decodeString(readRawString()));
}

std::string JsonParser::readRawString() {
    expect('"');
    std::string raw;
    while (true) {
        if (pos_ >= text_.size()) fail("unterminated string");
        char c = text_[pos_++];
        if (c == '"') break;
        if (static_cast<unsigned char>(c) < 0x20) fail("unescaped control character in string");
        if (c == '\\') {
            if (pos_ >= text_.size()) fail("unterminated escape sequence");
            char e = text_[pos_++];
            raw.push_back('\\');
            raw.push_back(e);
            if (e == 'u') {
                for (int i = 0; i < 4; ++i) {
                    if (pos_ >= text_.size()) fail("incomplete \\u escape");
                    raw.push_back(text_[pos_++]);
                }
            }
        } else {
            raw.push_back(c);
        }
    }
    return raw;
}

namespace {
int hexValue(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

void appendUtf8(std::string& out, std::uint32_t cp) {
    if (cp <= 0x7F) {
        out.push_back(static_cast<char>(cp));
    } else if (cp <= 0x7FF) {
        out.push_back(static_cast<char>(0xC0 | (cp >> 6)));
        out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
    } else if (cp <= 0xFFFF) {
        out.push_back(static_cast<char>(0xE0 | (cp >> 12)));
        out.push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3F)));
        out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
    } else {
        out.push_back(static_cast<char>(0xF0 | (cp >> 18)));
        out.push_back(static_cast<char>(0x80 | ((cp >> 12) & 0x3F)));
        out.push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3F)));
        out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
    }
}
}  // namespace

std::string JsonParser::decodeString(const std::string& raw) {
    std::string out;
    out.reserve(raw.size());
    for (std::size_t i = 0; i < raw.size();) {
        char c = raw[i++];
        if (c != '\\') {
            out.push_back(c);
            continue;
        }
        char e = raw[i++];
        switch (e) {
            case '"':  out.push_back('"'); break;
            case '\\': out.push_back('\\'); break;
            case '/':  out.push_back('/'); break;
            case 'b':  out.push_back('\b'); break;
            case 'f':  out.push_back('\f'); break;
            case 'n':  out.push_back('\n'); break;
            case 'r':  out.push_back('\r'); break;
            case 't':  out.push_back('\t'); break;
            case 'u': {
                auto readHex = [&](std::size_t at) -> std::uint32_t {
                    std::uint32_t v = 0;
                    for (int k = 0; k < 4; ++k) {
                        int h = hexValue(raw[at + static_cast<std::size_t>(k)]);
                        if (h < 0) fail("invalid \\u escape");
                        v = (v << 4) | static_cast<std::uint32_t>(h);
                    }
                    return v;
                };
                std::uint32_t cp = readHex(i);
                i += 4;
                if (cp >= 0xD800 && cp <= 0xDBFF) {
                    if (i + 6 <= raw.size() && raw[i] == '\\' && raw[i + 1] == 'u') {
                        std::uint32_t lo = readHex(i + 2);
                        if (lo >= 0xDC00 && lo <= 0xDFFF) {
                            cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                            i += 6;
                        } else {
                            fail("invalid low surrogate in \\u escape");
                        }
                    } else {
                        fail("expected low surrogate after high surrogate");
                    }
                } else if (cp >= 0xDC00 && cp <= 0xDFFF) {
                    fail("unexpected low surrogate");
                }
                appendUtf8(out, cp);
                break;
            }
            default:
                fail(std::string("invalid escape '\\") + e + "'");
        }
    }
    return out;
}

JsonValue JsonParser::parseNumber() {
    std::size_t start = pos_;
    if (peek() == '-') ++pos_;
    if (pos_ >= text_.size()) fail("invalid number");
    if (text_[pos_] == '0') {
        ++pos_;
    } else if (text_[pos_] >= '1' && text_[pos_] <= '9') {
        while (pos_ < text_.size() && text_[pos_] >= '0' && text_[pos_] <= '9') ++pos_;
    } else {
        fail("invalid number");
    }
    if (pos_ < text_.size() && text_[pos_] == '.') {
        ++pos_;
        if (pos_ >= text_.size() || text_[pos_] < '0' || text_[pos_] > '9') fail("invalid fraction");
        while (pos_ < text_.size() && text_[pos_] >= '0' && text_[pos_] <= '9') ++pos_;
    }
    if (pos_ < text_.size() && (text_[pos_] == 'e' || text_[pos_] == 'E')) {
        ++pos_;
        if (pos_ < text_.size() && (text_[pos_] == '+' || text_[pos_] == '-')) ++pos_;
        if (pos_ >= text_.size() || text_[pos_] < '0' || text_[pos_] > '9') fail("invalid exponent");
        while (pos_ < text_.size() && text_[pos_] >= '0' && text_[pos_] <= '9') ++pos_;
    }
    std::string token = text_.substr(start, pos_ - start);
    try {
        return JsonValue::makeNumber(std::stod(token));
    } catch (const std::exception&) {
        fail("number out of range: " + token);
    }
}

JsonValue JsonParser::parseLiteral() {
    if (text_.compare(pos_, 4, "true") == 0) {
        pos_ += 4;
        return JsonValue::makeBool(true);
    }
    if (text_.compare(pos_, 5, "false") == 0) {
        pos_ += 5;
        return JsonValue::makeBool(false);
    }
    if (text_.compare(pos_, 4, "null") == 0) {
        pos_ += 4;
        return JsonValue();
    }
    fail("invalid literal");
}

std::string jsonEscape(const std::string& s) {
    std::string out;
    out.reserve(s.size() + 2);
    for (char c : s) {
        switch (c) {
            case '"':  out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\b': out += "\\b"; break;
            case '\f': out += "\\f"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (static_cast<unsigned char>(c) < 0x20) {
                    char buf[8];
                    std::snprintf(buf, sizeof(buf), "\\u%04x", static_cast<unsigned char>(c));
                    out += buf;
                } else {
                    out.push_back(c);
                }
        }
    }
    return out;
}

}  // namespace memtriage
