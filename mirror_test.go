package mirror

import (
	"bytes"
	"fmt"
	"testing"
)

// syntheticFrame builds a deterministic frame of the given length.
func syntheticFrame(length int) []byte {
	frame := make([]byte, length)
	for i := range frame {
		frame[i] = byte(i % 251)
	}
	return frame
}

// fullFields covers a 70-byte eth/vlan/ipv4/tcp/payload frame without gaps.
func fullFields() []FieldRange {
	return []FieldRange{
		{Name: "eth", Depth: DepthL2, Start: 0, End: 14},
		{Name: "vlan", Depth: DepthL2, Start: 14, End: 18},
		{Name: "ipv4", Depth: DepthL3, Start: 18, End: 38},
		{Name: "tcp", Depth: DepthL4, Start: 38, End: 58},
		{Name: "payload", Depth: DepthL7, Start: 58, End: 70},
	}
}

func TestDepthString(t *testing.T) {
	cases := map[Depth]string{
		DepthL2: "L2", DepthL3: "L3", DepthL4: "L4", DepthL7: "L7",
		Depth(0): "Depth(0)", Depth(8): "Depth(8)",
	}
	for depth, want := range cases {
		if got := depth.String(); got != want {
			t.Errorf("Depth(%d).String() = %q, want %q", int(depth), got, want)
		}
	}
	for _, d := range []Depth{DepthL2, DepthL3, DepthL4, DepthL7} {
		if !d.Valid() {
			t.Errorf("Depth(%d) should be valid", int(d))
		}
	}
	for _, d := range []Depth{0, 5, 8} {
		if d.Valid() {
			t.Errorf("Depth(%d) should be invalid", int(d))
		}
	}
}

func TestFieldRangeHasBounds(t *testing.T) {
	cases := []struct {
		name string
		in   FieldRange
		want bool
	}{
		{"complete", FieldRange{Start: 0, End: 14}, true},
		{"no start", FieldRange{Start: -1, End: 14}, false},
		{"no end", FieldRange{Start: 0, End: -1}, false},
		{"neither", FieldRange{Start: -1, End: -1}, false},
		{"zero width", FieldRange{Start: 0, End: 0}, true},
	}
	for _, tc := range cases {
		if got := tc.in.HasBounds(); got != tc.want {
			t.Errorf("%s: HasBounds() = %v, want %v", tc.name, got, tc.want)
		}
	}
}

func TestConfigValidate(t *testing.T) {
	valid := Receiver{Name: "ids", Ingresses: []string{"ge-0/0/1"}, VLANs: []int{100}, MaxDepth: DepthL4}

	cases := []struct {
		name    string
		cfg     Config
		wantErr bool
	}{
		{"valid", Config{Receivers: []Receiver{valid}}, false},
		{"wildcard ingress", Config{Receivers: []Receiver{{Name: "a", Ingresses: []string{"*"}, MaxDepth: DepthL2}}}, false},
		{"empty config", Config{}, true},
		{"empty name", Config{Receivers: []Receiver{{Name: " ", Ingresses: []string{"*"}, MaxDepth: DepthL2}}}, true},
		{"duplicate name", Config{Receivers: []Receiver{valid, valid}}, true},
		{"no ingress", Config{Receivers: []Receiver{{Name: "a", MaxDepth: DepthL2}}}, true},
		{"zero depth", Config{Receivers: []Receiver{{Name: "a", Ingresses: []string{"*"}, MaxDepth: 0}}}, true},
		{"too deep", Config{Receivers: []Receiver{{Name: "a", Ingresses: []string{"*"}, MaxDepth: 5}}}, true},
		{"blank ingress", Config{Receivers: []Receiver{{Name: "a", Ingresses: []string{" "}, MaxDepth: DepthL2}}}, true},
	}
	for _, tc := range cases {
		err := tc.cfg.Validate()
		if tc.wantErr && err == nil {
			t.Errorf("%s: expected error, got nil", tc.name)
		}
		if !tc.wantErr && err != nil {
			t.Errorf("%s: unexpected error: %v", tc.name, err)
		}
	}
}

// Test_TwoReceiversShareFrameWithDifferentDepth is the core distribution case:
// one capture, an L4-scoped receiver and an L2-scoped receiver that both pass
// authorization but retain different byte lengths.
func Test_TwoReceiversShareFrameWithDifferentDepth(t *testing.T) {
	frame := syntheticFrame(70)
	cfg := Config{Receivers: []Receiver{
		{Name: "forensics", Ingresses: []string{"*"}, VLANs: []int{100}, MaxDepth: DepthL4},
		{Name: "mirror-lite", Ingresses: []string{"ge-0/0/7"}, VLANs: []int{100}, MaxDepth: DepthL2},
	}}
	capture := Capture{ID: "c-1", Ingress: "ge-0/0/7", VLANs: []int{100}, Frame: frame, Fields: fullFields()}

	got := Distribute(cfg, capture)
	if len(got.Copies) != 2 {
		t.Fatalf("got %d copies, want 2", len(got.Copies))
	}
	if got.CaptureID != "c-1" {
		t.Fatalf("capture id = %q, want c-1", got.CaptureID)
	}

	full, ok := got.CopyFor("forensics")
	if !ok || !full.Allowed {
		t.Fatalf("forensics copy missing or denied: %+v", full)
	}
	if len(full.Data) != 58 {
		t.Fatalf("forensics retained %d bytes, want 58", len(full.Data))
	}
	if !bytes.Equal(full.Data, frame[:58]) {
		t.Fatal("forensics bytes differ from the frame prefix")
	}
	wantFields := []string{"eth", "vlan", "ipv4", "tcp"}
	if fmt.Sprint(full.Fields) != fmt.Sprint(wantFields) {
		t.Fatalf("forensics fields = %v, want %v", full.Fields, wantFields)
	}
	if full.Truncation == nil || full.Truncation.Reason != ReasonDepthLimit || full.Truncation.Field != "payload" || full.Truncation.Offset != 58 {
		t.Fatalf("forensics truncation = %+v, want depth_limit on payload at 58", full.Truncation)
	}

	light, ok := got.CopyFor("mirror-lite")
	if !ok || !light.Allowed {
		t.Fatalf("mirror-lite copy missing or denied: %+v", light)
	}
	if len(light.Data) != 18 {
		t.Fatalf("mirror-lite retained %d bytes, want 18 (eth + vlan)", len(light.Data))
	}
	if !bytes.Equal(light.Data, frame[:18]) {
		t.Fatal("mirror-lite bytes differ from the frame prefix")
	}
	if light.Truncation == nil || light.Truncation.Reason != ReasonDepthLimit || light.Truncation.Field != "ipv4" || light.Truncation.Offset != 18 {
		t.Fatalf("mirror-lite truncation = %+v, want depth_limit on ipv4 at 18", light.Truncation)
	}

	// The two retained prefixes must not share backing storage.
	light.Data[0] ^= 0xff
	if full.Data[0] == light.Data[0] {
		t.Fatal("receiver copies alias the same bytes")
	}
	if frame[0] == light.Data[0] {
		t.Fatal("receiver copy aliases the source frame")
	}
}

func Test_ExactlyAtDepthLimitIsComplete(t *testing.T) {
	frame := syntheticFrame(70)
	cfg := Config{Receivers: []Receiver{
		{Name: "l7", Ingresses: []string{"*"}, VLANs: []int{10}, MaxDepth: DepthL7},
	}}
	capture := Capture{ID: "c", Ingress: "x", VLANs: []int{10}, Frame: frame, Fields: fullFields()}
	c := Distribute(cfg, capture).Copies[0]
	if !c.Allowed || c.Truncation != nil {
		t.Fatalf("want full copy without truncation, got %+v", c.Truncation)
	}
	if len(c.Data) != 70 || !bytes.Equal(c.Data, frame) {
		t.Fatalf("copy is not the full frame: len=%d", len(c.Data))
	}
	if fmt.Sprint(c.Fields) != "[eth vlan ipv4 tcp payload]" {
		t.Fatalf("fields = %v, want all five", c.Fields)
	}
}

func Test_OriginalVLANAndIngressDecideNotFrameBytes(t *testing.T) {
	// The frame bytes still visibly encode tag 200 in the VLAN field, and an
	// intermediate link has rewritten the live tag to 300. Authorization must
	// follow Capture.VLANs, which holds the original ingress values.
	frame := syntheticFrame(70)
	frame[14], frame[15] = 0x00, 0xc8 // TCI showing VLAN 200 inside the frame

	cfg := Config{Receivers: []Receiver{
		{Name: "orig-100", Ingresses: []string{"*"}, VLANs: []int{100}, MaxDepth: DepthL4},
		{Name: "framebytes-200", Ingresses: []string{"*"}, VLANs: []int{200}, MaxDepth: DepthL4},
		{Name: "orig-300", Ingresses: []string{"*"}, VLANs: []int{300}, MaxDepth: DepthL4},
		{Name: "wrong-port", Ingresses: []string{"ge-0/0/9"}, VLANs: []int{300}, MaxDepth: DepthL4},
	}}
	capture := Capture{ID: "c", Ingress: "ge-0/0/1", VLANs: []int{300}, Frame: frame, Fields: fullFields()}

	got := Distribute(cfg, capture)
	if c, _ := got.CopyFor("orig-100"); c.Allowed || c.Denied != DenyVLAN {
		t.Fatalf("orig-100 receiver = allowed:%v denied:%q, want vlan denial", c.Allowed, c.Denied)
	} else if c.DenyDetail == "" {
		t.Fatal("denial detail must explain the VLAN mismatch")
	}
	if c, _ := got.CopyFor("framebytes-200"); c.Allowed || c.Denied != DenyVLAN {
		t.Fatalf("framebytes-200 receiver = %+v, want vlan denial from frame bytes", c)
	}
	if c, _ := got.CopyFor("wrong-port"); c.Allowed || c.Denied != DenyIngress {
		t.Fatalf("wrong-port receiver = %+v, want ingress denial", c)
	}
	allowed, _ := got.CopyFor("orig-300")
	if !allowed.Allowed || len(allowed.Data) != 58 {
		t.Fatalf("orig-300 receiver = allowed:%v len:%d, want allowed with 58 bytes", allowed.Allowed, len(allowed.Data))
	}
}

func Test_HeaderBeyondFrameAndFullyExcludedHeader(t *testing.T) {
	frame := syntheticFrame(20)
	cfg := Config{Receivers: []Receiver{{Name: "l3", Ingresses: []string{"*"}, AllowUntagged: true, MaxDepth: DepthL3}}}
	fields := []FieldRange{
		{Name: "eth", Depth: DepthL2, Start: 0, End: 14},
		{Name: "ipv4", Depth: DepthL3, Start: 14, End: 34},
	}
	// The frame is shorter than the declared ipv4 header, so only eth can be
	// confirmed and the copy stops on the out-of-range header.
	c := Distribute(cfg, Capture{ID: "s", Ingress: "x", Frame: frame, Fields: fields}).Copies[0]
	if len(c.Data) != 14 {
		t.Fatalf("retained %d bytes, want 14", len(c.Data))
	}
	if c.Truncation == nil || c.Truncation.Reason != ReasonOutOfBounds || c.Truncation.Field != "ipv4" {
		t.Fatalf("truncation = %+v, want out_of_bounds on ipv4", c.Truncation)
	}

	// With a limit that excludes the first header entirely, nothing is copied.
	l2 := Config{Receivers: []Receiver{{Name: "l2", Ingresses: []string{"*"}, AllowUntagged: true, MaxDepth: DepthL2}}}
	deep := []FieldRange{{Name: "ipv4", Depth: DepthL3, Start: 0, End: 20}}
	c2 := Distribute(l2, Capture{ID: "s2", Ingress: "x", Frame: frame, Fields: deep}).Copies[0]
	if len(c2.Data) != 0 || c2.Truncation == nil || c2.Truncation.Reason != ReasonDepthLimit {
		t.Fatalf("deep-only copy = %+v, want depth_limit with no bytes", c2)
	}
}

func Test_QinQRequiresEveryOriginalTag(t *testing.T) {
	fields := []FieldRange{
		{Name: "eth", Depth: DepthL2, Start: 0, End: 14},
		{Name: "vlan-outer", Depth: DepthL2, Start: 14, End: 18},
		{Name: "vlan-inner", Depth: DepthL2, Start: 18, End: 22},
		{Name: "ipv4", Depth: DepthL3, Start: 22, End: 42},
	}
	frame := syntheticFrame(42)
	cfg := Config{Receivers: []Receiver{
		{Name: "only-outer", Ingresses: []string{"*"}, VLANs: []int{100}, AllowUntagged: true, MaxDepth: DepthL3},
		{Name: "both", Ingresses: []string{"*"}, VLANs: []int{100, 200}, MaxDepth: DepthL3},
	}}
	capture := Capture{ID: "qinq", Ingress: "ae0", VLANs: []int{100, 200}, Frame: frame, Fields: fields}

	got := Distribute(cfg, capture)
	if c, _ := got.CopyFor("only-outer"); c.Allowed || c.Denied != DenyVLAN {
		t.Fatalf("only-outer receiver should be denied as a whole: %+v", c)
	}
	c, _ := got.CopyFor("both")
	if !c.Allowed {
		t.Fatalf("both receiver denied: %q", c.Denied)
	}
	if len(c.Data) != 42 {
		t.Fatalf("both receiver retained %d bytes, want 42", len(c.Data))
	}
}

func Test_UntaggedPolicy(t *testing.T) {
	frame := syntheticFrame(38)
	fields := []FieldRange{
		{Name: "eth", Depth: DepthL2, Start: 0, End: 14},
		{Name: "ipv4", Depth: DepthL3, Start: 14, End: 34},
	}
	cfg := Config{Receivers: []Receiver{
		{Name: "strict", Ingresses: []string{"ge-0/0/2"}, VLANs: []int{100}, MaxDepth: DepthL3},
		{Name: "untagged-ok", Ingresses: []string{"ge-0/0/2"}, VLANs: []int{100}, AllowUntagged: true, MaxDepth: DepthL3},
	}}
	capture := Capture{ID: "u", Ingress: "ge-0/0/2", Frame: frame, Fields: fields}

	got := Distribute(cfg, capture)
	if c, _ := got.CopyFor("strict"); c.Allowed || c.Denied != DenyUntagged {
		t.Fatalf("strict receiver should deny untagged: %+v", c)
	}
	ok, _ := got.CopyFor("untagged-ok")
	if !ok.Allowed || len(ok.Data) != 34 {
		t.Fatalf("untagged-ok = allowed:%v len:%d, want allowed with 34 bytes", ok.Allowed, len(ok.Data))
	}
	if ok.Truncation == nil || ok.Truncation.Reason != ReasonUncoveredBytes || ok.Truncation.Offset != 34 {
		t.Fatalf("untagged-ok truncation = %+v, want uncovered_bytes at 34", ok.Truncation)
	}
}

func Test_IngressScope(t *testing.T) {
	frame := syntheticFrame(14)
	fields := []FieldRange{{Name: "eth", Depth: DepthL2, Start: 0, End: 14}}
	cfg := Config{Receivers: []Receiver{
		{Name: "port", Ingresses: []string{"ge-0/0/1", "ge-0/0/2"}, AllowUntagged: true, MaxDepth: DepthL2},
		{Name: "any", Ingresses: []string{"*"}, AllowUntagged: true, MaxDepth: DepthL2},
	}}
	capture := Capture{ID: "i", Ingress: "ge-0/0/9", Frame: frame, Fields: fields}

	got := Distribute(cfg, capture)
	if c, _ := got.CopyFor("port"); c.Allowed || c.Denied != DenyIngress {
		t.Fatalf("port receiver should deny ingress: %+v", c)
	}
	if c, _ := got.CopyFor("any"); !c.Allowed || len(c.Data) != 14 {
		t.Fatalf("wildcard receiver should allow 14 bytes: allowed=%v denied=%q len=%d", c.Allowed, c.Denied, len(c.Data))
	}
}

func Test_MissingBoundsStopsAtKnownHeaders(t *testing.T) {
	frame := syntheticFrame(70)
	fields := []FieldRange{
		{Name: "eth", Depth: DepthL2, Start: 0, End: 14},
		{Name: "vlan", Depth: DepthL2, Start: 14, End: -1}, // VLAN header end not documented
		{Name: "ipv4", Depth: DepthL3, Start: 18, End: 38},
	}
	cfg := Config{Receivers: []Receiver{{Name: "r", Ingresses: []string{"*"}, AllowUntagged: true, MaxDepth: DepthL4}}}
	c := Distribute(cfg, Capture{ID: "m", Ingress: "x", Frame: frame, Fields: fields}).Copies[0]

	if !c.Allowed {
		t.Fatalf("authorization should still pass: %q", c.Denied)
	}
	if len(c.Data) != 14 || !bytes.Equal(c.Data, frame[:14]) {
		t.Fatalf("retained %d bytes, want the confirmed 14", len(c.Data))
	}
	if fmt.Sprint(c.Fields) != "[eth]" {
		t.Fatalf("fields = %v, want [eth]", c.Fields)
	}
	tr := c.Truncation
	if tr == nil || tr.Reason != ReasonMissingBounds || tr.Field != "vlan" || tr.Depth != DepthL2 || tr.Offset != 14 {
		t.Fatalf("truncation = %+v, want missing_bounds on vlan at 14", tr)
	}
	if tr.Detail == "" {
		t.Fatal("truncation detail must explain the stop")
	}
}

func Test_UnknownStartBoundaryStopsImmediately(t *testing.T) {
	frame := syntheticFrame(70)
	fields := []FieldRange{
		{Name: "eth", Depth: DepthL2, Start: 0, End: 14},
		{Name: "vlan", Depth: DepthL2, Start: -1, End: 18},
		{Name: "ipv4", Depth: DepthL3, Start: 18, End: 38},
	}
	cfg := Config{Receivers: []Receiver{{Name: "r", Ingresses: []string{"*"}, AllowUntagged: true, MaxDepth: DepthL4}}}
	c := Distribute(cfg, Capture{ID: "m", Ingress: "x", Frame: frame, Fields: fields}).Copies[0]

	if len(c.Data) != 14 {
		t.Fatalf("retained %d bytes, want 14", len(c.Data))
	}
	if c.Truncation == nil || c.Truncation.Reason != ReasonMissingBounds || c.Truncation.Field != "vlan" {
		t.Fatalf("truncation = %+v, want missing_bounds on vlan", c.Truncation)
	}
}

func Test_NoFieldDataWithFrameBytes(t *testing.T) {
	cfg := Config{Receivers: []Receiver{{Name: "r", Ingresses: []string{"*"}, AllowUntagged: true, MaxDepth: DepthL4}}}
	c := Distribute(cfg, Capture{ID: "n", Ingress: "x", Frame: syntheticFrame(60)}).Copies[0]

	if !c.Allowed {
		t.Fatalf("authorization should pass: %q", c.Denied)
	}
	if len(c.Data) != 0 {
		t.Fatalf("retained %d bytes, want none", len(c.Data))
	}
	if c.Truncation == nil || c.Truncation.Reason != ReasonNoFieldData {
		t.Fatalf("truncation = %+v, want no_field_data", c.Truncation)
	}
}

func Test_EmptyFrameWithoutFieldsIsComplete(t *testing.T) {
	cfg := Config{Receivers: []Receiver{{Name: "r", Ingresses: []string{"*"}, AllowUntagged: true, MaxDepth: DepthL2}}}
	c := Distribute(cfg, Capture{ID: "e", Ingress: "x"}).Copies[0]
	if !c.Allowed || c.Truncation != nil || len(c.Data) != 0 {
		t.Fatalf("empty frame result = %+v", c)
	}
}

func Test_MalformedMetadataCases(t *testing.T) {
	frame := syntheticFrame(70)
	cfg := Config{Receivers: []Receiver{{Name: "r", Ingresses: []string{"*"}, AllowUntagged: true, MaxDepth: DepthL4}}}

	cases := []struct {
		name   string
		fields []FieldRange
		reason Reason
		field  string
		cut    int
	}{
		{
			name:   "gap between headers",
			fields: []FieldRange{{Name: "eth", Depth: DepthL2, Start: 0, End: 14}, {Name: "ipv4", Depth: DepthL3, Start: 16, End: 36}},
			reason: ReasonGap, field: "ipv4", cut: 14,
		},
		{
			name:   "overlapping headers",
			fields: []FieldRange{{Name: "eth", Depth: DepthL2, Start: 0, End: 14}, {Name: "ipv4", Depth: DepthL3, Start: 12, End: 32}},
			reason: ReasonOverlap, field: "ipv4", cut: 14,
		},
		{
			name:   "end beyond frame",
			fields: []FieldRange{{Name: "eth", Depth: DepthL2, Start: 0, End: 14}, {Name: "ipv4", Depth: DepthL3, Start: 14, End: 100}},
			reason: ReasonOutOfBounds, field: "ipv4", cut: 14,
		},
		{
			name:   "zero width header",
			fields: []FieldRange{{Name: "eth", Depth: DepthL2, Start: 0, End: 14}, {Name: "ipv4", Depth: DepthL3, Start: 14, End: 14}},
			reason: ReasonInvalidBounds, field: "ipv4", cut: 14,
		},
		{
			name:   "negative width header",
			fields: []FieldRange{{Name: "eth", Depth: DepthL2, Start: 0, End: 14}, {Name: "ipv4", Depth: DepthL3, Start: 20, End: 14}},
			reason: ReasonInvalidBounds, field: "ipv4", cut: 14,
		},
		{
			name:   "first header does not start at zero",
			fields: []FieldRange{{Name: "eth", Depth: DepthL2, Start: 4, End: 18}},
			reason: ReasonGap, field: "eth", cut: 0,
		},
		{
			name:   "undefined depth",
			fields: []FieldRange{{Name: "eth", Depth: DepthL2, Start: 0, End: 14}, {Name: "mpls", Depth: Depth(8), Start: 14, End: 18}},
			reason: ReasonUnknownDepth, field: "mpls", cut: 14,
		},
		{
			name:   "depth zero",
			fields: []FieldRange{{Name: "eth", Depth: DepthL2, Start: 0, End: 14}, {Name: "mpls", Depth: Depth(0), Start: 14, End: 18}},
			reason: ReasonUnknownDepth, field: "mpls", cut: 14,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			c := Distribute(cfg, Capture{ID: "x", Ingress: "x", Frame: frame, Fields: tc.fields}).Copies[0]
			if !c.Allowed {
				t.Fatalf("authorization denied: %q", c.Denied)
			}
			if len(c.Data) != tc.cut {
				t.Fatalf("retained %d bytes, want %d", len(c.Data), tc.cut)
			}
			if c.Truncation == nil {
				t.Fatal("expected a truncation record")
			}
			if c.Truncation.Reason != tc.reason || c.Truncation.Field != tc.field || c.Truncation.Offset != tc.cut {
				t.Fatalf("truncation = %+v, want reason=%s field=%s offset=%d", c.Truncation, tc.reason, tc.field, tc.cut)
			}
			if c.Truncation.Detail == "" {
				t.Fatal("truncation detail is empty")
			}
		})
	}
}

func Test_OutOfOrderFieldsAreSorted(t *testing.T) {
	frame := syntheticFrame(70)
	fields := []FieldRange{
		{Name: "tcp", Depth: DepthL4, Start: 38, End: 58},
		{Name: "eth", Depth: DepthL2, Start: 0, End: 14},
		{Name: "vlan", Depth: DepthL2, Start: 14, End: 18},
		{Name: "ipv4", Depth: DepthL3, Start: 18, End: 38},
	}
	cfg := Config{Receivers: []Receiver{{Name: "r", Ingresses: []string{"*"}, AllowUntagged: true, MaxDepth: DepthL4}}}
	c := Distribute(cfg, Capture{ID: "o", Ingress: "x", Frame: frame, Fields: fields}).Copies[0]

	if len(c.Data) != 58 {
		t.Fatalf("retained %d bytes, want 58", len(c.Data))
	}
	if fmt.Sprint(c.Fields) != "[eth vlan ipv4 tcp]" {
		t.Fatalf("fields = %v, want sorted order", c.Fields)
	}
}

func Test_DepthLimitReportedBeforeMalformedDeeperHeader(t *testing.T) {
	// A deeper header with no boundaries must not be blamed for the stop when
	// the receiver limit already excludes it.
	frame := syntheticFrame(70)
	fields := []FieldRange{
		{Name: "eth", Depth: DepthL2, Start: 0, End: 14},
		{Name: "ipv4", Depth: DepthL3, Start: 14, End: 34},
		{Name: "tcp", Depth: DepthL4, Start: -1, End: -1},
	}
	cfg := Config{Receivers: []Receiver{{Name: "r", Ingresses: []string{"*"}, AllowUntagged: true, MaxDepth: DepthL3}}}
	c := Distribute(cfg, Capture{ID: "d", Ingress: "x", Frame: frame, Fields: fields}).Copies[0]

	if len(c.Data) != 34 {
		t.Fatalf("retained %d bytes, want 34", len(c.Data))
	}
	if c.Truncation == nil || c.Truncation.Reason != ReasonDepthLimit || c.Truncation.Field != "tcp" {
		t.Fatalf("truncation = %+v, want depth_limit on tcp", c.Truncation)
	}
}

func Test_DeniedReceiversStillReported(t *testing.T) {
	cfg := Config{Receivers: []Receiver{
		{Name: "a", Ingresses: []string{"nope"}, MaxDepth: DepthL2},
		{Name: "b", Ingresses: []string{"*"}, AllowUntagged: true, MaxDepth: DepthL2},
	}}
	got := Distribute(cfg, Capture{ID: "z", Ingress: "here", Frame: syntheticFrame(10),
		Fields: []FieldRange{{Name: "eth", Depth: DepthL2, Start: 0, End: 10}}})
	if len(got.Copies) != 2 {
		t.Fatalf("got %d copies, want both receivers represented", len(got.Copies))
	}
	if c, _ := got.CopyFor("a"); c.Allowed || c.Denied != DenyIngress {
		t.Fatalf("receiver a = %+v", c)
	}
	if _, ok := got.CopyFor("missing"); ok {
		t.Fatal("CopyFor reported a receiver that was never configured")
	}
}

func Test_DistributeAllKeepsCapturesIndependent(t *testing.T) {
	cfg := Config{Receivers: []Receiver{
		{Name: "wide", Ingresses: []string{"*"}, VLANs: []int{100}, AllowUntagged: true, MaxDepth: DepthL4},
		{Name: "narrow", Ingresses: []string{"*"}, VLANs: []int{100}, AllowUntagged: true, MaxDepth: DepthL2},
	}}

	tagged := Capture{ID: "tagged", Ingress: "ge-0/0/1", VLANs: []int{100}, Frame: syntheticFrame(70), Fields: fullFields()}
	untagged := Capture{ID: "untagged", Ingress: "ge-0/0/1", Frame: syntheticFrame(38), Fields: []FieldRange{
		{Name: "eth", Depth: DepthL2, Start: 0, End: 14},
		{Name: "ipv4", Depth: DepthL3, Start: 14, End: 34},
	}}

	results := DistributeAll(cfg, []Capture{tagged, untagged})
	if len(results) != 2 {
		t.Fatalf("got %d results, want 2", len(results))
	}
	if results[0].CaptureID != "tagged" || results[1].CaptureID != "untagged" {
		t.Fatalf("result order = %q, %q", results[0].CaptureID, results[1].CaptureID)
	}

	wide1, _ := results[0].CopyFor("wide")
	wide2, _ := results[1].CopyFor("wide")
	narrow2, _ := results[1].CopyFor("narrow")

	if !wide1.Allowed || !wide2.Allowed || !narrow2.Allowed {
		t.Fatalf("expected all copies allowed: %+v %+v %+v", wide1, wide2, narrow2)
	}
	if len(wide1.Data) != 58 || len(wide2.Data) != 34 || len(narrow2.Data) != 14 {
		t.Fatalf("lengths = %d/%d/%d, want 58/34/14", len(wide1.Data), len(wide2.Data), len(narrow2.Data))
	}

	// Mutating one capture's copy must not touch another capture's copy.
	wide1.Data[0] ^= 0xff
	if tagged.Frame[0] == wide1.Data[0] {
		t.Fatal("copy aliases the tagged source frame")
	}
	if wide2.Data[0] == wide1.Data[0] {
		t.Fatal("copies from different captures alias")
	}
}

func Test_DistributeAllUntaggedDeniedByDefault(t *testing.T) {
	cfg := Config{Receivers: []Receiver{
		{Name: "scoped", Ingresses: []string{"*"}, VLANs: []int{100}, MaxDepth: DepthL4},
	}}
	capture := Capture{ID: "u", Ingress: "ge-0/0/1", Frame: syntheticFrame(38), Fields: []FieldRange{
		{Name: "eth", Depth: DepthL2, Start: 0, End: 14},
		{Name: "ipv4", Depth: DepthL3, Start: 14, End: 34},
	}}
	c := DistributeAll(cfg, []Capture{capture})[0].Copies[0]
	if c.Allowed || c.Denied != DenyUntagged || c.Data != nil {
		t.Fatalf("untagged capture should be denied without bytes: %+v", c)
	}
}

func Test_InterleavedCapturesForSameReceiver(t *testing.T) {
	// Two captures of different original tags for the same receiver, delivered
	// in an interleaved sequence: authorization must be evaluated per capture.
	cfg := Config{Receivers: []Receiver{
		{Name: "r100", Ingresses: []string{"*"}, VLANs: []int{100}, MaxDepth: DepthL3},
		{Name: "r200", Ingresses: []string{"*"}, VLANs: []int{200}, MaxDepth: DepthL3},
	}}
	fields := []FieldRange{
		{Name: "eth", Depth: DepthL2, Start: 0, End: 14},
		{Name: "vlan", Depth: DepthL2, Start: 14, End: 18},
		{Name: "ipv4", Depth: DepthL3, Start: 18, End: 38},
	}
	c100 := Capture{ID: "a", Ingress: "ge-0/0/1", VLANs: []int{100}, Frame: syntheticFrame(40), Fields: fields}
	c200 := Capture{ID: "b", Ingress: "ge-0/0/1", VLANs: []int{200}, Frame: syntheticFrame(40), Fields: fields}

	results := DistributeAll(cfg, []Capture{c100, c200, c100})
	want := []struct {
		allow100 bool
		allow200 bool
	}{
		{true, false},
		{false, true},
		{true, false},
	}
	for i, w := range want {
		a, _ := results[i].CopyFor("r100")
		b, _ := results[i].CopyFor("r200")
		if a.Allowed != w.allow100 || b.Allowed != w.allow200 {
			t.Fatalf("result %d: r100=%v r200=%v, want %v/%v", i, a.Allowed, b.Allowed, w.allow100, w.allow200)
		}
		if a.Allowed && len(a.Data) != 38 {
			t.Fatalf("result %d: r100 retained %d bytes, want 38", i, len(a.Data))
		}
		if b.Allowed && len(b.Data) != 38 {
			t.Fatalf("result %d: r200 retained %d bytes, want 38", i, len(b.Data))
		}
	}
}

func Test_DistributeAllEmpty(t *testing.T) {
	cfg := Config{Receivers: []Receiver{{Name: "r", Ingresses: []string{"*"}, MaxDepth: DepthL2}}}
	if got := DistributeAll(cfg, nil); len(got) != 0 {
		t.Fatalf("got %d results, want none", len(got))
	}
}

func Test_DistributeDoesNotMutateCapture(t *testing.T) {
	frame := syntheticFrame(70)
	original := append([]byte(nil), frame...)
	fields := fullFields()
	cfg := Config{Receivers: []Receiver{{Name: "r", Ingresses: []string{"*"}, AllowUntagged: true, MaxDepth: DepthL4}}}
	capture := Capture{ID: "x", Ingress: "x", Frame: frame, Fields: fields}

	got := Distribute(cfg, capture)
	if len(got.Copies[0].Data) != 58 {
		t.Fatalf("unexpected copy length %d", len(got.Copies[0].Data))
	}
	got.Copies[0].Data[5] ^= 0xff
	if !bytes.Equal(frame, original) {
		t.Fatal("Distribute or the caller corrupted the source frame")
	}
	if len(capture.Fields) != 5 || capture.Fields[1].Start != 14 {
		t.Fatal("field metadata was mutated")
	}
}

func BenchmarkDistribute(b *testing.B) {
	frame := syntheticFrame(1500)
	cfg := Config{Receivers: []Receiver{
		{Name: "one", Ingresses: []string{"*"}, VLANs: []int{100}, MaxDepth: DepthL4},
		{Name: "two", Ingresses: []string{"*"}, VLANs: []int{100}, MaxDepth: DepthL7},
	}}
	capture := Capture{ID: "b", Ingress: "x", VLANs: []int{100}, Frame: frame, Fields: []FieldRange{
		{Name: "eth", Depth: DepthL2, Start: 0, End: 14},
		{Name: "vlan", Depth: DepthL2, Start: 14, End: 18},
		{Name: "ipv4", Depth: DepthL3, Start: 18, End: 38},
		{Name: "tcp", Depth: DepthL4, Start: 38, End: 58},
		{Name: "payload", Depth: DepthL7, Start: 58, End: 1500},
	}}
	b.ReportAllocs()
	for i := 0; i < b.N; i++ {
		_ = Distribute(cfg, capture)
	}
}
