// Package certprecheck 提供边缘节点证书轮换的上线前预检能力。
//
// 输入候选节点的证书链、服务域名、客户端信任根以及分批切换时刻，
// 在每个阶段按该阶段指定的时间验证证书链与域名。滚动阶段可能同时
// 存在新旧节点，只有当每个必需客户端对它在该阶段可能访问的每类
// 节点都验证通过时才可推进；否则输出首个阻塞阶段及具体客户端、
// 节点与失败原因。
package certprecheck

import (
	"crypto/x509"
	"errors"
	"fmt"
	"time"
)

// Node 表示一个候选边缘节点及其证书链（叶子证书在前，中间证书随后）。
type Node struct {
	ID    string
	Chain []*x509.Certificate
}

// Client 表示一个需要完成身份校验的客户端及其信任根。
type Client struct {
	Name string
	// Roots 为该客户端的信任根池。
	Roots *x509.CertPool
	// Required 为 true 时表示必需客户端：其验证失败会阻塞推进。
	Required bool
}

// Stage 表示一个分批切换阶段：在 At 时刻，NodeIDs 列出的节点同时在线。
type Stage struct {
	Name    string
	At      time.Time
	NodeIDs []string
}

// Failure 描述一次具体的验证失败。
type Failure struct {
	Stage  string
	Client string
	Node   string
	Reason string
}

// Report 为预检结果。OK 为 true 表示所有阶段均可推进；
// 否则 BlockingStage 为首个阻塞阶段，Failures 为该阶段的全部失败明细。
type Report struct {
	OK            bool
	BlockingStage string
	Failures      []Failure
}

// Precheck 按阶段顺序执行预检，返回首个阻塞阶段的报告；全部通过时 OK 为 true。
func Precheck(domain string, nodes []Node, clients []Client, stages []Stage) Report {
	byID := make(map[string]Node, len(nodes))
	for _, n := range nodes {
		byID[n.ID] = n
	}

	for _, st := range stages {
		var failures []Failure
		for _, c := range clients {
			if !c.Required {
				continue
			}
			for _, id := range st.NodeIDs {
				n, ok := byID[id]
				if !ok {
					failures = append(failures, Failure{
						Stage:  st.Name,
						Client: c.Name,
						Node:   id,
						Reason: "节点未定义",
					})
					continue
				}
				if err := verifyNode(domain, n, c.Roots, st.At); err != nil {
					failures = append(failures, Failure{
						Stage:  st.Name,
						Client: c.Name,
						Node:   n.ID,
						Reason: err.Error(),
					})
				}
			}
		}
		if len(failures) > 0 {
			return Report{OK: false, BlockingStage: st.Name, Failures: failures}
		}
	}
	return Report{OK: true}
}

// verifyNode 在指定时刻 at 用客户端信任根 roots 验证节点证书链与服务域名。
func verifyNode(domain string, n Node, roots *x509.CertPool, at time.Time) error {
	if len(n.Chain) == 0 {
		return errors.New("证书链为空")
	}
	leaf := n.Chain[0]
	if leaf == nil {
		return errors.New("叶子证书为空")
	}

	// 显式检查有效期边界，给出精确原因。
	if at.Before(leaf.NotBefore) {
		return fmt.Errorf("证书尚未生效: 当前时间 %s 早于 NotBefore %s",
			at.UTC().Format(time.RFC3339), leaf.NotBefore.UTC().Format(time.RFC3339))
	}
	if at.After(leaf.NotAfter) {
		return fmt.Errorf("证书已过期: 当前时间 %s 晚于 NotAfter %s",
			at.UTC().Format(time.RFC3339), leaf.NotAfter.UTC().Format(time.RFC3339))
	}

	intermediates := x509.NewCertPool()
	for _, c := range n.Chain[1:] {
		if c != nil {
			intermediates.AddCert(c)
		}
	}

	opts := x509.VerifyOptions{
		DNSName:       domain,
		Roots:         roots,
		Intermediates: intermediates,
		CurrentTime:   at,
	}
	if _, err := leaf.Verify(opts); err != nil {
		return fmt.Errorf("链验证失败: %w", translateVerifyError(err))
	}
	return nil
}

// translateVerifyError 将 x509 验证错误转换为可读原因。
func translateVerifyError(err error) error {
	var hostErr x509.HostnameError
	if errors.As(err, &hostErr) {
		return fmt.Errorf("域名不匹配: 证书对服务域名 %q 无效", hostErr.Host)
	}
	var unknownAuth x509.UnknownAuthorityError
	if errors.As(err, &unknownAuth) {
		return errors.New("证书未由客户端信任的根签发")
	}
	var invalidErr x509.CertificateInvalidError
	if errors.As(err, &invalidErr) {
		return fmt.Errorf("证书无效: %s", invalidErr.Detail)
	}
	return err
}
