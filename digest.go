package backuprecovery

import (
	"bytes"
	"crypto/sha256"
	"errors"
	"sort"
)

// Digest 使用 SHA-256 计算内容摘要。
func Digest(content []byte) []byte {
	sum := sha256.Sum256(content)
	return sum[:]
}

// digestEqual 常量时间无关地比较两个摘要是否相同。
func digestEqual(a, b []byte) bool {
	return bytes.Equal(a, b)
}

// ApplyPatch 对前像内容真实应用补丁并返回后像。
//
// 校验条件（任一不满足即返回错误）：
//   - 前像长度必须等于 patch.BeforeLength；
//   - 前像摘要必须等于 patch.BeforeDigest；
//   - 各 Hunk 区间不得重叠且不得越界；
//   - 每个 Hunk 偏移处的实际字节必须与其 Old 完全一致；
//   - 应用后的摘要必须等于 patch.AfterDigest。
func ApplyPatch(patch *Patch, before []byte) ([]byte, error) {
	if patch == nil {
		return nil, errors.New("backuprecovery: 补丁为空")
	}
	if len(before) != patch.BeforeLength {
		return nil, errors.New("backuprecovery: 前像长度与补丁不匹配")
	}
	if !digestEqual(Digest(before), patch.BeforeDigest) {
		return nil, errors.New("backuprecovery: 前像摘要与补丁不匹配")
	}

	// 复制一份，避免修改调用方数据。
	working := make([]byte, len(before))
	copy(working, before)

	// 按偏移升序检查区间合法性与重叠，再按偏移降序应用（偏移不会失效）。
	hunks := make([]Hunk, len(patch.Hunks))
	copy(hunks, patch.Hunks)
	sort.Slice(hunks, func(i, j int) bool { return hunks[i].Offset < hunks[j].Offset })
	prevEnd := -1
	for _, h := range hunks {
		if h.Offset < 0 {
			return nil, errors.New("backuprecovery: 补丁偏移为负")
		}
		end := h.Offset + len(h.Old)
		if end > len(working) {
			return nil, errors.New("backuprecovery: 补丁片段越界")
		}
		if h.Offset <= prevEnd {
			return nil, errors.New("backuprecovery: 补丁片段重叠")
		}
		prevEnd = end
	}
	for i := len(hunks) - 1; i >= 0; i-- {
		h := hunks[i]
		end := h.Offset + len(h.Old)
		if !bytes.Equal(working[h.Offset:end], h.Old) {
			return nil, errors.New("backuprecovery: 补丁旧字节与前像不匹配")
		}
		var rebuilt []byte
		rebuilt = append(rebuilt, working[:h.Offset]...)
		rebuilt = append(rebuilt, h.New...)
		rebuilt = append(rebuilt, working[end:]...)
		working = rebuilt
	}

	if !digestEqual(Digest(working), patch.AfterDigest) {
		return nil, errors.New("backuprecovery: 补丁应用后摘要与声明不符")
	}
	return working, nil
}
