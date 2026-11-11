#ifndef MEMTRIAGE_JSON_HPP
#define MEMTRIAGE_JSON_HPP

#include <cstdint>
#include <map>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>

namespace memtriage {

class JsonError : public std::runtime_error {
public:
    explicit JsonError(const std::string& msg) : std::runtime_error(msg) {}
};

class JsonValue {
public:
    enum class Type { Null, Bool, Number, String, Array, Object };

    JsonValue() : type_(Type::Null), num_(0.0), bool_(false) {}

    static JsonValue makeBool(bool b) {
        JsonValue v;
        v.type_ = Type::Bool;
        v.bool_ = b;
        return v;
    }
    static JsonValue makeNumber(double d) {
        JsonValue v;
        v.type_ = Type::Number;
        v.num_ = d;
        return v;
    }
    static JsonValue makeString(std::string s) {
        JsonValue v;
        v.type_ = Type::String;
        v.str_ = std::move(s);
        return v;
    }
    static JsonValue makeArray() {
        JsonValue v;
        v.type_ = Type::Array;
        return v;
    }
    static JsonValue makeObject() {
        JsonValue v;
        v.type_ = Type::Object;
        return v;
    }

    Type type() const { return type_; }
    bool isNull() const { return type_ == Type::Null; }
    bool isBool() const { return type_ == Type::Bool; }
    bool isNumber() const { return type_ == Type::Number; }
    bool isString() const { return type_ == Type::String; }
    bool isArray() const { return type_ == Type::Array; }
    bool isObject() const { return type_ == Type::Object; }

    bool asBool() const;
    double asDouble() const;
    std::int64_t asInt64() const;
    const std::string& asString() const;
    const std::vector<JsonValue>& asArray() const;
    std::vector<JsonValue>& asArray();
    const std::map<std::string, JsonValue>& asObject() const;
    std::map<std::string, JsonValue>& asObject();

    bool contains(const std::string& key) const;
    const JsonValue& at(const std::string& key) const;

private:
    Type type_;
    double num_;
    bool bool_;
    std::string str_;
    std::vector<JsonValue> arr_;
    std::map<std::string, JsonValue> obj_;
};

class JsonParser {
public:
    // Parses an entire JSON document. Throws JsonError with position info.
    static JsonValue parse(const std::string& text);
    static JsonValue parseFile(const std::string& path);

private:
    explicit JsonParser(std::string text) : text_(std::move(text)), pos_(0) {}

    JsonValue parseValue();
    JsonValue parseObject();
    JsonValue parseArray();
    JsonValue parseString();
    JsonValue parseNumber();
    JsonValue parseLiteral();

    void skipWhitespace();
    char peek() const;
    char get();
    void expect(char c);
    std::string readRawString();
    std::string decodeString(const std::string& raw);
    [[noreturn]] void fail(const std::string& msg) const;
    std::string position() const;

    std::string text_;
    std::size_t pos_;
};

// Serialization used for the machine-readable JSON report.
std::string jsonEscape(const std::string& s);

}  // namespace memtriage

#endif
