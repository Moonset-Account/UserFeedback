"""域名规范化与标签级比较。

规则(来自需求):
- 去掉末尾根点(一个或多个结尾的 ".")。
- 按标签(label)比较,而不是子串比较。
- 忽略大小写(使用 casefold)。
- 支持国际化域名与其 ASCII(punycode, xn--)表示互相匹配:
  两侧都规范化为 Unicode 形式后比较。
"""


def _normalize_label(label):
    """规范化单个标签:小写折叠 + punycode 解码为 Unicode。"""
    text = label.strip().casefold()
    if text.startswith("xn--"):
        try:
            # 标准库 idna 编解码器(IDNA 2003),把 ACE 标签还原为 Unicode。
            decoded = text.encode("ascii").decode("idna")
            return decoded.casefold()
        except Exception:
            # 非法 punycode 按普通 ASCII 标签处理,不抛错。
            return text
    return text


def normalize_name(name):
    """规范化完整域名。

    - 去除首尾空白与末尾根点;
    - 返回以 "." 连接、逐标签规范化后的字符串;
    - 根域名本身规范化为空字符串 ""。
    """
    if not isinstance(name, str):
        raise ValueError("domain name must be a string, got %r" % type(name).__name__)
    text = name.strip()
    # 去掉末尾根点(允许多个结尾点,视为同一根点语义)。
    while text.endswith("."):
        text = text[:-1]
    if text == "":
        return ""
    labels = text.split(".")
    return ".".join(_normalize_label(label) for label in labels)


def split_labels(name):
    """把(已规范化或原始的)域名拆成标签列表,根域名为空列表。"""
    normalized = normalize_name(name)
    if normalized == "":
        return []
    return normalized.split(".")


def suffix_matches(query, suffix):
    """判断 query 是否落在 suffix 之下(含相等),按标签边界比较。

    例如 suffix="corp.example.com":
      - "corp.example.com"      -> True
      - "www.corp.example.com"  -> True
      - "notcorp.example.com"   -> False(标签边界,不是子串)
      - "example.com"           -> False
    """
    q = normalize_name(query)
    s = normalize_name(suffix)
    if s == "":
        return True  # 根后缀匹配一切
    return q == s or q.endswith("." + s)
