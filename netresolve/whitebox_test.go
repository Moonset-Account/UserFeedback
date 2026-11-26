package netresolve

import (
	"net/netip"
	"testing"
)

// TestCompareSourceWhiteBox 直接覆盖并列裁决的全部比较分支，
// 其中“同接口不同查询名”分支在端到端搜索域短路语义下不可达，
// 因此用同包白盒方式保证该确定性逻辑被断言。
func TestCompareSourceWhiteBox(t *testing.T) {
	base := Candidate{Source: QuerySource{InterfaceID: "wifi", QueryName: "a.example"}}
	cases := []struct {
		name string
		x, y Candidate
		want int
	}{
		{
			name: "interface order cell before wifi lexicographically",
			x:    Candidate{Source: QuerySource{InterfaceID: "cell", QueryName: "q"}},
			y:    Candidate{Source: QuerySource{InterfaceID: "wifi", QueryName: "q"}},
			want: -1,
		},
		{
			name: "interface order reversed returns +1",
			x:    Candidate{Source: QuerySource{InterfaceID: "wifi", QueryName: "q"}},
			y:    Candidate{Source: QuerySource{InterfaceID: "cell", QueryName: "q"}},
			want: 1,
		},
		{
			name: "query name order on same interface",
			x:    Candidate{Source: QuerySource{InterfaceID: "wifi", QueryName: "a.example"}},
			y:    Candidate{Source: QuerySource{InterfaceID: "wifi", QueryName: "b.example"}},
			want: -1,
		},
		{
			name: "query name order reversed returns +1",
			x:    Candidate{Source: QuerySource{InterfaceID: "wifi", QueryName: "b.example"}},
			y:    Candidate{Source: QuerySource{InterfaceID: "wifi", QueryName: "a.example"}},
			want: 1,
		},
		{
			name: "fully equal source",
			x:    base,
			y:    base,
			want: 0,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := compareSource(tc.x, tc.y); got != tc.want {
				t.Fatalf("compareSource = %d, want %d", got, tc.want)
			}
		})
	}
}

// TestSourcePreferenceWhiteBox 覆盖最低优先级档（非全球单播、非私有/回环）。
func TestSourcePreferenceWhiteBox(t *testing.T) {
	multicastV4 := Candidate{Addr: netip.MustParseAddr("224.0.0.1")}
	if got := sourcePreference(multicastV4); got != 2 {
		t.Fatalf("multicast v4 preference = %d, want 2 (lowest)", got)
	}
	unspecified := Candidate{Addr: netip.MustParseAddr("2001:db8::9")}
	if got := sourcePreference(unspecified); got != 0 {
		t.Fatalf("global unicast preference = %d, want 0", got)
	}
}

// TestSortFamilyDeterministic 验证同接口同查询名、同优先级下
// “晚过期优先，再相同按地址字典序”的确定性裁决。
func TestSortFamilyDeterministic(t *testing.T) {
	cands := []Candidate{
		{
			Addr: netip.MustParseAddr("192.0.2.9"), Family: FamilyIPv4,
			Source:    QuerySource{InterfaceID: "wifi", QueryName: "q"},
			ExpiresAt: mkTime(50),
		},
		{
			Addr: netip.MustParseAddr("192.0.2.1"), Family: FamilyIPv4,
			Source:    QuerySource{InterfaceID: "wifi", QueryName: "q"},
			ExpiresAt: mkTime(50),
		},
		{
			Addr: netip.MustParseAddr("192.0.2.2"), Family: FamilyIPv4,
			Source:    QuerySource{InterfaceID: "wifi", QueryName: "q"},
			ExpiresAt: mkTime(90),
		},
		// 同优先级但来源接口不同（cell 字典序先于 wifi），覆盖 sortFamily
		// 内部 compareSource 非零的裁决分支。
		{
			Addr: netip.MustParseAddr("192.0.2.3"), Family: FamilyIPv4,
			Source:    QuerySource{InterfaceID: "cell", QueryName: "q"},
			ExpiresAt: mkTime(90),
		},
	}
	sortFamily(cands)
	got := []string{
		cands[0].Addr.String(), cands[1].Addr.String(),
		cands[2].Addr.String(), cands[3].Addr.String(),
	}
	want := []string{"192.0.2.3", "192.0.2.2", "192.0.2.1", "192.0.2.9"}
	if !equalStrings(got, want) {
		t.Fatalf("sortFamily = %v, want %v", got, want)
	}
}

// TestAnswerLiveAtWhiteBox 覆盖新鲜度判定的两个边界：
// 未来取得的应答不可见；零 TTL 信息的应答视为长期有效。
func TestAnswerLiveAtWhiteBox(t *testing.T) {
	future := Answer{ObtainedAt: mkTime(10), ExpiresAt: mkTime(100)}
	if future.liveAt(mkTime(5)) {
		t.Fatal("answer obtained in the future must not be live")
	}
	zeroTTL := Answer{ObtainedAt: mkTime(0)}
	if !zeroTTL.liveAt(mkTime(9999)) {
		t.Fatal("answer with zero expiry should be treated as live")
	}
	expiring := Answer{ObtainedAt: mkTime(0), ExpiresAt: mkTime(10)}
	if !expiring.liveAt(mkTime(9)) {
		t.Fatal("answer one second before expiry should be live")
	}
	if expiring.liveAt(mkTime(10)) {
		t.Fatal("answer at exact expiry instant must be stale")
	}
}

// TestMergeInterleaveWhiteBox 验证一族先耗尽时另一族补齐剩余槽位。
func TestMergeInterleaveWhiteBox(t *testing.T) {
	first := []Candidate{
		{Addr: netip.MustParseAddr("2001:db8::a")},
		{Addr: netip.MustParseAddr("2001:db8::b")},
		{Addr: netip.MustParseAddr("2001:db8::c")},
	}
	second := []Candidate{
		{Addr: netip.MustParseAddr("192.0.2.1")},
	}
	merged := mergeInterleave(first, second)
	want := []string{"2001:db8::a", "192.0.2.1", "2001:db8::b", "2001:db8::c"}
	var got []string
	for _, c := range merged {
		got = append(got, c.Addr.String())
	}
	if !equalStrings(got, want) {
		t.Fatalf("merge = %v, want %v", got, want)
	}

	// 空切片不应产生异常。
	if len(mergeInterleave(nil, nil)) != 0 {
		t.Fatal("merge of two empty slices must be empty")
	}
}
