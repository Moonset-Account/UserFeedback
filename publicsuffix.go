package cookiejar

import "strings"

// PublicSuffixList 由调用方注入，提供公共后缀判断。
//
// 设计为接口而不是内置列表，是因为嵌入式浏览器的发布节奏、内嵌资源与
// 合规要求各不相同：调用方可以包装 golang.org/x/net/publicsuffix，
// 也可以提供冻结的自定义列表。本包不发起任何网络访问。
type PublicSuffixList interface {
	// PublicSuffix 返回 host 的公共后缀以及该后缀是否来自已知规则。
	// 例如 ("example.co.uk") -> ("co.uk", true)；
	// 未知顶级域 ("example.zzz") -> ("zzz", false)。
	PublicSuffix(host string) (suffix string, known bool)
}

// StaticPublicSuffixList 是测试与轻量嵌入场景使用的内存静态列表。
//
// 列表中的条目必须是规范化的小写域段（如 "co.uk"），匹配采用完整段比较：
// 取主机名中最长的、与列表条目完全相等的结尾段序列作为公共后缀。
type StaticPublicSuffixList struct {
	suffixes map[string]struct{}
}

// NewStaticPublicSuffixList 用给定后缀条目构造静态列表。
func NewStaticPublicSuffixList(entries []string) *StaticPublicSuffixList {
	l := &StaticPublicSuffixList{suffixes: make(map[string]struct{}, len(entries))}
	for _, e := range entries {
		e = strings.ToLower(strings.Trim(strings.TrimSpace(e), "."))
		if e != "" {
			l.suffixes[e] = struct{}{}
		}
	}
	return l
}

// PublicSuffix 实现 PublicSuffixList。
func (l *StaticPublicSuffixList) PublicSuffix(host string) (string, bool) {
	host = strings.ToLower(strings.TrimSuffix(host, "."))
	if _, ok := l.suffixes[host]; ok {
		return host, true
	}
	best := ""
	known := false
	for suffix := range l.suffixes {
		if strings.HasSuffix(host, "."+suffix) && len(suffix) > len(best) {
			best = suffix
			known = true
		}
	}
	if known {
		return best, true
	}
	// 回退：最后一个 DNS 标签始终视为后缀，但标记为未知规则。
	if i := strings.LastIndex(host, "."); i >= 0 {
		return host[i+1:], false
	}
	return host, false
}

// isIPLiteral 判断主机是否为 IP 字面量（v4 或 v6）。
func isIPLiteral(host string) bool {
	if netParseIP(host) != nil {
		return true
	}
	return false
}

// registrableDomain 计算容量桶使用的可注册域（eTLD+1）。
//
// 规则：
//  1. IP 字面量与无标签点的主机各自独立成桶；
//  2. 后缀已知时，取后缀左侧紧邻的一个标签与后缀拼接；
//  3. 后缀未知（私有/未知顶级域）时，保守地按最后两个标签处理，
//     主机本身只有一个标签时直接使用主机名。
func registrableDomain(host string, psl PublicSuffixList) string {
	host = strings.ToLower(strings.TrimSuffix(host, "."))
	if isIPLiteral(host) || !strings.Contains(host, ".") {
		return host
	}
	suffix, known := psl.PublicSuffix(host)
	suffix = strings.TrimSuffix(suffix, ".")
	if !known {
		labels := strings.Split(host, ".")
		if len(labels) <= 2 {
			return host
		}
		return strings.Join(labels[len(labels)-2:], ".")
	}
	if host == suffix {
		return host
	}
	remainder := strings.TrimSuffix(host[:len(host)-len(suffix)], ".")
	i := strings.LastIndex(remainder, ".")
	var label string
	if i == -1 {
		label = remainder
	} else {
		label = remainder[i+1:]
	}
	if label == "" {
		return suffix
	}
	return label + "." + suffix
}
