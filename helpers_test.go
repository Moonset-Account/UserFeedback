package backuprecovery

import (
	"bytes"
	"testing"
)

// applyHunksForTest 在测试夹具中按生产语义（高偏移优先、区间不重叠）应用 hunk。
func applyHunksForTest(t *testing.T, before []byte, hunks []Hunk) []byte {
	t.Helper()
	out := append([]byte(nil), before...)
	// 从高偏移向低偏移应用，偏移不会因长度变化而失效。
	indexed := make([]int, len(hunks))
	for i := range hunks {
		indexed[i] = i
	}
	for i := 0; i < len(indexed); i++ {
		for j := i + 1; j < len(indexed); j++ {
			if hunks[indexed[j]].Offset > hunks[indexed[i]].Offset {
				indexed[i], indexed[j] = indexed[j], indexed[i]
			}
		}
	}
	for _, k := range indexed {
		h := hunks[k]
		end := h.Offset + len(h.Old)
		if end > len(out) {
			t.Fatalf("测试夹具 hunk 越界: offset=%d old=%d len=%d", h.Offset, len(h.Old), len(out))
		}
		if !bytes.Equal(out[h.Offset:end], h.Old) {
			t.Fatalf("测试夹具 hunk 旧字节不匹配")
		}
		rebuilt := append([]byte(nil), out[:h.Offset]...)
		rebuilt = append(rebuilt, h.New...)
		rebuilt = append(rebuilt, out[end:]...)
		out = rebuilt
	}
	return out
}

// mustPatch 构造一条摘要自洽的补丁并返回补丁与后像内容。
func mustPatch(t *testing.T, id, page string, gen int, before []byte, hunks []Hunk) (*Patch, []byte) {
	t.Helper()
	after := applyHunksForTest(t, before, hunks)
	return &Patch{
		ID:           id,
		PageID:       page,
		Generation:   gen,
		BeforeDigest: Digest(before),
		BeforeLength: len(before),
		AfterDigest:  Digest(after),
		Hunks:        hunks,
	}, after
}

func mustBase(t *testing.T, page string, gen int, content []byte) *BaseImage {
	t.Helper()
	return &BaseImage{
		PageID:     page,
		Generation: gen,
		Digest:     Digest(content),
		Content:    append([]byte(nil), content...),
	}
}

func baseEntry(id string, b *BaseImage, cost int) Entry {
	return Entry{ID: id, Base: b, ReadCost: cost}
}

func patchEntry(id, patchID string, cost int) Entry {
	return Entry{ID: id, PatchID: patchID, ReadCost: cost}
}

func targetOf(t *testing.T, page string, gen int, content []byte) Target {
	t.Helper()
	return Target{PageID: page, Generation: gen, Digest: Digest(content), Length: len(content)}
}

// validateChainIndependently 在测试中独立重放来源链：逐条真实应用补丁并重算摘要。
func validateChainIndependently(t *testing.T, c SourceChain, wantDigest []byte) {
	t.Helper()
	if len(c.Steps) == 0 {
		t.Fatalf("页 %s 来源链为空", c.PageID)
	}
	var content []byte
	for i, s := range c.Steps {
		switch s.Kind {
		case "base":
			if !digestEqual(Digest(s.Base.Content), s.Base.Digest) {
				t.Fatalf("步骤 %d 基础页摘要不符", i)
			}
			content = append([]byte(nil), s.Base.Content...)
		case "patch":
			out, err := ApplyPatch(s.Patch, content)
			if err != nil {
				t.Fatalf("步骤 %d 补丁 %s 应用失败: %v", i, s.Patch.ID, err)
			}
			if !digestEqual(Digest(out), s.Patch.AfterDigest) {
				t.Fatalf("步骤 %d 补丁 %s 重算摘要与声明不符", i, s.Patch.ID)
			}
			content = out
		case "retain":
			if !digestEqual(Digest(content), s.To) {
				t.Fatalf("步骤 %d RETAIN 摘要不符", i)
			}
		default:
			t.Fatalf("未知步骤类型: %s", s.Kind)
		}
	}
	if !digestEqual(Digest(content), wantDigest) {
		t.Fatalf("页 %s 来源链终态摘要与目标不符", c.PageID)
	}
}

func contains(haystack []string, needle string) bool {
	for _, s := range haystack {
		if s == needle {
			return true
		}
	}
	return false
}
