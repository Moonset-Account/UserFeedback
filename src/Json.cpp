#include "Json.hpp"

#include <stdexcept>

namespace json {

struct Document::Parser {
    std::string_view s;
    size_t pos = 0;
    std::vector<std::unique_ptr<Value>> pool;

    explicit Parser(std::string_view input) : s(input) {}

    [[noreturn]] void fail(const std::string& msg) const {
        throw std::runtime_error("json parse error at offset " +
                                 std::to_string(pos) + ": " + msg);
    }

    void skipWs() {
        while (pos < s.size()) {
            char c = s[pos];
            if (c == ' ' || c == '\t' || c == '\n' || c == '\r')
                ++pos;
            else
                break;
        }
    }

    char peek() const {
        if (pos >= s.size())
            throw std::runtime_error("json parse error: unexpected end of input");
        return s[pos];
    }

    char get() {
        if (pos >= s.size())
            throw std::runtime_error("json parse error: unexpected end of input");
        return s[pos++];
    }

    void expect(char c) {
        if (get() != c) fail(std::string("expected '") + c + "'");
    }

    Value* make() {
        pool.push_back(std::make_unique<Value>());
        return pool.back().get();
    }

    Value* parseValue() {
        skipWs();
        char c = peek();
        switch (c) {
        case '{': return parseObject();
        case '[': return parseArray();
        case '"': return parseString();
        case 't': case 'f': return parseBool();
        case 'n': return parseNull();
        default:
            if (c == '-' || (c >= '0' && c <= '9')) return parseNumber();
            fail("unexpected character");
        }
    }

    Value* parseObject() {
        Value* v = make();
        v->type = Type::Object;
        expect('{');
        skipWs();
        if (peek() == '}') { get(); return v; }
        while (true) {
            skipWs();
            if (peek() != '"') fail("expected string key");
            Value* key = parseString();
            skipWs();
            expect(':');
            Value* val = parseValue();
            v->object.emplace(key->text, val);
            skipWs();
            char c = get();
            if (c == ',') continue;
            if (c == '}') break;
            fail("expected ',' or '}'");
        }
        return v;
    }

    Value* parseArray() {
        Value* v = make();
        v->type = Type::Array;
        expect('[');
        skipWs();
        if (peek() == ']') { get(); return v; }
        while (true) {
            v->array.push_back(parseValue());
            skipWs();
            char c = get();
            if (c == ',') continue;
            if (c == ']') break;
            fail("expected ',' or ']'");
        }
        return v;
    }

    static void appendUtf8(std::string& out, uint32_t cp) {
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

    uint32_t parseHex4() {
        uint32_t cp = 0;
        for (int i = 0; i < 4; ++i) {
            char c = get();
            cp <<= 4;
            if (c >= '0' && c <= '9') cp += static_cast<uint32_t>(c - '0');
            else if (c >= 'a' && c <= 'f') cp += static_cast<uint32_t>(c - 'a' + 10);
            else if (c >= 'A' && c <= 'F') cp += static_cast<uint32_t>(c - 'A' + 10);
            else fail("invalid unicode escape");
        }
        return cp;
    }

    Value* parseString() {
        Value* v = make();
        v->type = Type::String;
        expect('"');
        std::string& out = v->text;
        while (true) {
            char c = get();
            if (c == '"') break;
            if (c == '\0') fail("unterminated string");
            if (c != '\\') {
                out.push_back(c);
                continue;
            }
            char e = get();
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
                uint32_t cp = parseHex4();
                if (cp >= 0xD800 && cp <= 0xDBFF) {
                    // high surrogate; expect low surrogate
                    if (get() != '\\' || get() != 'u') fail("bad surrogate pair");
                    uint32_t lo = parseHex4();
                    if (lo < 0xDC00 || lo > 0xDFFF) fail("bad low surrogate");
                    cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                } else if (cp >= 0xDC00 && cp <= 0xDFFF) {
                    fail("unexpected low surrogate");
                }
                appendUtf8(out, cp);
                break;
            }
            default: fail("invalid escape");
            }
        }
        return v;
    }

    Value* parseBool() {
        Value* v = make();
        v->type = Type::Bool;
        if (s.compare(pos, 4, "true") == 0) {
            pos += 4;
            v->boolean = true;
        } else if (s.compare(pos, 5, "false") == 0) {
            pos += 5;
            v->boolean = false;
        } else {
            fail("invalid literal");
        }
        return v;
    }

    Value* parseNull() {
        Value* v = make();
        if (s.compare(pos, 4, "null") != 0) fail("invalid literal");
        pos += 4;
        return v;
    }

    Value* parseNumber() {
        Value* v = make();
        v->type = Type::Number;
        size_t start = pos;
        if (peek() == '-') ++pos;
        while (pos < s.size()) {
            char c = s[pos];
            if ((c >= '0' && c <= '9') || c == '.' || c == 'e' || c == 'E' ||
                c == '+' || c == '-')
                ++pos;
            else
                break;
        }
        v->text = std::string(s.substr(start, pos - start));
        return v;
    }
};

Document Document::parse(std::string_view input) {
    Document doc;
    Parser parser(input);
    parser.skipWs();
    Value* root = parser.parseValue();
    parser.skipWs();
    if (parser.pos != input.size())
        throw std::runtime_error("json: trailing data at offset " +
                                 std::to_string(parser.pos));
    doc.pool_ = std::move(parser.pool);
    doc.root_ = root;
    return doc;
}

} // namespace json
