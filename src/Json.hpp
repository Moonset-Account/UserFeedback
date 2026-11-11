#ifndef FMTREWRITE_JSON_HPP
#define FMTREWRITE_JSON_HPP

// Minimal, self-contained JSON parser (RFC 8259 subset used by clang's
// -Xclang -ast-dump=json output). Supports objects, arrays, strings,
// numbers (stored as text), booleans, null. A Document owns the whole node
// arena; node pointers are valid for the Document's lifetime.

#include <cstdint>
#include <map>
#include <memory>
#include <string>
#include <string_view>
#include <vector>

namespace json {

class Value;
using Object = std::map<std::string, const Value*>;
using Array = std::vector<const Value*>;

enum class Type { Null, Bool, Number, String, Array, Object };

class Value {
public:
    Type type = Type::Null;
    bool boolean = false;
    std::string text;               // decoded string, or raw number text
    Array array;
    Object object;

    const Value* find(std::string_view key) const {
        auto it = object.find(std::string(key));
        return it == object.end() ? nullptr : it->second;
    }
    const Value* at(std::string_view key) const { return find(key); }
    bool isString() const { return type == Type::String; }
    std::string_view asString() const { return text; }
    bool isObject() const { return type == Type::Object; }
    bool isArray() const { return type == Type::Array; }
};

class Document {
public:
    static Document parse(std::string_view input);
    Document() = default;
    Document(Document&&) noexcept = default;
    Document& operator=(Document&&) noexcept = default;
    Document(const Document&) = delete;
    Document& operator=(const Document&) = delete;

    const Value* root() const { return root_; }

private:
    struct Parser;
    std::vector<std::unique_ptr<Value>> pool_;
    const Value* root_ = nullptr;
    friend struct Parser;
};

} // namespace json

#endif // FMTREWRITE_JSON_HPP
