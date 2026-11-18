package certprecheck

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"math/big"
	"strings"
	"testing"
	"time"
)

// ---- 合成证书夹具 ----

func makeCA(t *testing.T, name string, notBefore, notAfter time.Time) (*x509.Certificate, *ecdsa.PrivateKey) {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatalf("生成 CA 密钥失败: %v", err)
	}
	tmpl := &x509.Certificate{
		SerialNumber:          big.NewInt(time.Now().UnixNano()),
		Subject:               pkix.Name{CommonName: name},
		NotBefore:             notBefore,
		NotAfter:              notAfter,
		IsCA:                  true,
		KeyUsage:              x509.KeyUsageCertSign | x509.KeyUsageDigitalSignature,
		BasicConstraintsValid: true,
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	if err != nil {
		t.Fatalf("创建 CA 证书失败: %v", err)
	}
	cert, err := x509.ParseCertificate(der)
	if err != nil {
		t.Fatalf("解析 CA 证书失败: %v", err)
	}
	return cert, key
}

func makeLeaf(t *testing.T, ca *x509.Certificate, caKey *ecdsa.PrivateKey, dnsNames []string, notBefore, notAfter time.Time) *x509.Certificate {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatalf("生成叶子密钥失败: %v", err)
	}
	tmpl := &x509.Certificate{
		SerialNumber: big.NewInt(time.Now().UnixNano()),
		Subject:      pkix.Name{CommonName: "edge-node"},
		DNSNames:     dnsNames,
		NotBefore:    notBefore,
		NotAfter:     notAfter,
		KeyUsage:     x509.KeyUsageDigitalSignature,
		ExtKeyUsage:  []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, ca, &key.PublicKey, caKey)
	if err != nil {
		t.Fatalf("创建叶子证书失败: %v", err)
	}
	cert, err := x509.ParseCertificate(der)
	if err != nil {
		t.Fatalf("解析叶子证书失败: %v", err)
	}
	return cert
}

func poolOf(certs ...*x509.Certificate) *x509.CertPool {
	p := x509.NewCertPool()
	for _, c := range certs {
		p.AddCert(c)
	}
	return p
}

var (
	t0 = time.Date(2026, 3, 1, 0, 0, 0, 0, time.UTC)
	t1 = time.Date(2026, 3, 2, 0, 0, 0, 0, time.UTC)
	t2 = time.Date(2026, 3, 3, 0, 0, 0, 0, time.UTC)
	t3 = time.Date(2026, 3, 4, 0, 0, 0, 0, time.UTC)
)

const domain = "edge.example.com"

// ---- 用例 1: 可接受结果 —— 全部阶段通过 ----

func TestPrecheckAllStagesPass(t *testing.T) {
	oldCA, oldKey := makeCA(t, "old-ca", t0.Add(-24*time.Hour), t3.Add(24*time.Hour))
	newCA, newKey := makeCA(t, "new-ca", t0.Add(-24*time.Hour), t3.Add(24*time.Hour))

	oldLeaf := makeLeaf(t, oldCA, oldKey, []string{domain}, t0.Add(-time.Hour), t3.Add(time.Hour))
	newLeaf := makeLeaf(t, newCA, newKey, []string{domain}, t0.Add(-time.Hour), t3.Add(time.Hour))

	nodes := []Node{
		{ID: "node-old", Chain: []*x509.Certificate{oldLeaf}},
		{ID: "node-new", Chain: []*x509.Certificate{newLeaf}},
	}
	clients := []Client{
		{Name: "client-a", Roots: poolOf(oldCA, newCA), Required: true},
		{Name: "client-b", Roots: poolOf(oldCA, newCA), Required: true},
	}
	stages := []Stage{
		{Name: "stage-1", At: t1, NodeIDs: []string{"node-old"}},
		{Name: "stage-2", At: t2, NodeIDs: []string{"node-old", "node-new"}}, // 滚动阶段新旧并存
		{Name: "stage-3", At: t3, NodeIDs: []string{"node-new"}},
	}

	rep := Precheck(domain, nodes, clients, stages)
	if !rep.OK {
		t.Fatalf("期望全部通过, 实际阻塞于 %q: %+v", rep.BlockingStage, rep.Failures)
	}
	if rep.BlockingStage != "" || len(rep.Failures) != 0 {
		t.Fatalf("通过时不应有阻塞信息: %+v", rep)
	}
}

// ---- 用例 2: 应被阻止 —— 必需客户端不信任新根 ----

func TestPrecheckBlockedByUntrustedRoot(t *testing.T) {
	oldCA, oldKey := makeCA(t, "old-ca", t0.Add(-24*time.Hour), t3.Add(24*time.Hour))
	newCA, newKey := makeCA(t, "new-ca", t0.Add(-24*time.Hour), t3.Add(24*time.Hour))

	oldLeaf := makeLeaf(t, oldCA, oldKey, []string{domain}, t0.Add(-time.Hour), t3.Add(time.Hour))
	newLeaf := makeLeaf(t, newCA, newKey, []string{domain}, t0.Add(-time.Hour), t3.Add(time.Hour))

	nodes := []Node{
		{ID: "node-old", Chain: []*x509.Certificate{oldLeaf}},
		{ID: "node-new", Chain: []*x509.Certificate{newLeaf}},
	}
	clients := []Client{
		{Name: "client-full", Roots: poolOf(oldCA, newCA), Required: true},
		{Name: "client-legacy", Roots: poolOf(oldCA), Required: true}, // 只信任旧根
	}
	stages := []Stage{
		{Name: "stage-1", At: t1, NodeIDs: []string{"node-old"}},
		{Name: "stage-2", At: t2, NodeIDs: []string{"node-old", "node-new"}},
	}

	rep := Precheck(domain, nodes, clients, stages)
	if rep.OK {
		t.Fatal("期望被阻塞, 实际通过")
	}
	if rep.BlockingStage != "stage-2" {
		t.Fatalf("阻塞阶段应为 stage-2, 实际 %q", rep.BlockingStage)
	}
	if len(rep.Failures) != 1 {
		t.Fatalf("应只有 1 条失败, 实际 %d: %+v", len(rep.Failures), rep.Failures)
	}
	f := rep.Failures[0]
	if f.Client != "client-legacy" || f.Node != "node-new" {
		t.Fatalf("失败应定位到 client-legacy/node-new, 实际 %+v", f)
	}
	if !strings.Contains(f.Reason, "信任的根") {
		t.Fatalf("失败原因应说明信任根问题, 实际 %q", f.Reason)
	}
}

// ---- 用例 3: 应被阻止 —— 域名不匹配 ----

func TestPrecheckBlockedByHostnameMismatch(t *testing.T) {
	ca, key := makeCA(t, "ca", t0.Add(-24*time.Hour), t3.Add(24*time.Hour))
	leaf := makeLeaf(t, ca, key, []string{"other.example.com"}, t0.Add(-time.Hour), t3.Add(time.Hour))

	nodes := []Node{{ID: "node-1", Chain: []*x509.Certificate{leaf}}}
	clients := []Client{{Name: "client-a", Roots: poolOf(ca), Required: true}}
	stages := []Stage{{Name: "stage-1", At: t1, NodeIDs: []string{"node-1"}}}

	rep := Precheck(domain, nodes, clients, stages)
	if rep.OK {
		t.Fatal("期望域名不匹配被阻塞, 实际通过")
	}
	if rep.BlockingStage != "stage-1" {
		t.Fatalf("阻塞阶段应为 stage-1, 实际 %q", rep.BlockingStage)
	}
	if !strings.Contains(rep.Failures[0].Reason, "域名不匹配") {
		t.Fatalf("失败原因应为域名不匹配, 实际 %q", rep.Failures[0].Reason)
	}
}

// ---- 用例 4: 边界 —— 有效期边界的通过/拒绝 ----

func TestPrecheckValidityBoundary(t *testing.T) {
	ca, key := makeCA(t, "ca", t0.Add(-24*time.Hour), t3.Add(24*time.Hour))
	// 叶子有效期 [t1, t2]
	leaf := makeLeaf(t, ca, key, []string{domain}, t1, t2)
	nodes := []Node{{ID: "node-1", Chain: []*x509.Certificate{leaf}}}
	clients := []Client{{Name: "client-a", Roots: poolOf(ca), Required: true}}

	cases := []struct {
		name   string
		at     time.Time
		wantOK bool
		reason string
	}{
		{"NotBefore 前一秒应拒绝", t1.Add(-time.Second), false, "尚未生效"},
		{"恰在 NotBefore 应通过", t1, true, ""},
		{"恰在 NotAfter 应通过", t2, true, ""},
		{"NotAfter 后一秒应拒绝", t2.Add(time.Second), false, "已过期"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			stages := []Stage{{Name: "s", At: tc.at, NodeIDs: []string{"node-1"}}}
			rep := Precheck(domain, nodes, clients, stages)
			if rep.OK != tc.wantOK {
				t.Fatalf("OK=%v, 期望 %v (failures=%+v)", rep.OK, tc.wantOK, rep.Failures)
			}
			if !tc.wantOK && !strings.Contains(rep.Failures[0].Reason, tc.reason) {
				t.Fatalf("失败原因应包含 %q, 实际 %q", tc.reason, rep.Failures[0].Reason)
			}
		})
	}
}

// ---- 用例 5: 交错场景 —— 滚动阶段新旧节点并存时的部分失败 ----

func TestPrecheckRollingStagePartialFailure(t *testing.T) {
	oldCA, oldKey := makeCA(t, "old-ca", t0.Add(-24*time.Hour), t3.Add(24*time.Hour))
	newCA, newKey := makeCA(t, "new-ca", t0.Add(-24*time.Hour), t3.Add(24*time.Hour))

	oldLeaf := makeLeaf(t, oldCA, oldKey, []string{domain}, t0.Add(-time.Hour), t3.Add(time.Hour))
	// 新节点证书在 stage-2 时刻尚未生效, 到 stage-3 才生效。
	newLeaf := makeLeaf(t, newCA, newKey, []string{domain}, t2.Add(time.Hour), t3.Add(24*time.Hour))

	nodes := []Node{
		{ID: "node-old", Chain: []*x509.Certificate{oldLeaf}},
		{ID: "node-new", Chain: []*x509.Certificate{newLeaf}},
	}
	clients := []Client{
		{Name: "client-req", Roots: poolOf(oldCA, newCA), Required: true},
		// 非必需客户端即使不信任任何根也不应阻塞。
		{Name: "client-opt", Roots: poolOf(), Required: false},
	}
	stages := []Stage{
		{Name: "stage-1", At: t1, NodeIDs: []string{"node-old"}},
		{Name: "stage-2", At: t2, NodeIDs: []string{"node-old", "node-new"}},
		{Name: "stage-3", At: t3, NodeIDs: []string{"node-new"}},
	}

	rep := Precheck(domain, nodes, clients, stages)
	if rep.OK {
		t.Fatal("期望 stage-2 被阻塞, 实际通过")
	}
	if rep.BlockingStage != "stage-2" {
		t.Fatalf("阻塞阶段应为 stage-2, 实际 %q", rep.BlockingStage)
	}
	// 旧节点在该阶段验证通过, 失败应只涉及新节点与必需客户端。
	if len(rep.Failures) != 1 {
		t.Fatalf("应只有 1 条失败, 实际 %d: %+v", len(rep.Failures), rep.Failures)
	}
	f := rep.Failures[0]
	if f.Client != "client-req" || f.Node != "node-new" {
		t.Fatalf("失败应定位到 client-req/node-new, 实际 %+v", f)
	}
	if !strings.Contains(f.Reason, "尚未生效") {
		t.Fatalf("失败原因应为尚未生效, 实际 %q", f.Reason)
	}
}

// ---- 用例 6: 交错场景 —— 首个阻塞阶段之后不再继续 ----

func TestPrecheckStopsAtFirstBlockingStage(t *testing.T) {
	caA, keyA := makeCA(t, "ca-a", t0.Add(-24*time.Hour), t3.Add(24*time.Hour))
	caB, keyB := makeCA(t, "ca-b", t0.Add(-24*time.Hour), t3.Add(24*time.Hour))

	leafA := makeLeaf(t, caA, keyA, []string{domain}, t0.Add(-time.Hour), t3.Add(time.Hour))
	leafB := makeLeaf(t, caB, keyB, []string{domain}, t0.Add(-time.Hour), t3.Add(time.Hour))

	nodes := []Node{
		{ID: "node-a", Chain: []*x509.Certificate{leafA}},
		{ID: "node-b", Chain: []*x509.Certificate{leafB}},
	}
	// 客户端只信任 caA, 因此 stage-1(node-a) 通过, stage-2(node-b) 阻塞,
	// stage-3 也存在但不应出现在报告中。
	clients := []Client{{Name: "client-a", Roots: poolOf(caA), Required: true}}
	stages := []Stage{
		{Name: "stage-1", At: t1, NodeIDs: []string{"node-a"}},
		{Name: "stage-2", At: t2, NodeIDs: []string{"node-b"}},
		{Name: "stage-3", At: t3, NodeIDs: []string{"node-b"}},
	}

	rep := Precheck(domain, nodes, clients, stages)
	if rep.OK {
		t.Fatal("期望被阻塞, 实际通过")
	}
	if rep.BlockingStage != "stage-2" {
		t.Fatalf("应停在首个阻塞阶段 stage-2, 实际 %q", rep.BlockingStage)
	}
	for _, f := range rep.Failures {
		if f.Stage != "stage-2" {
			t.Fatalf("失败明细不应包含后续阶段: %+v", f)
		}
	}
}
