package distinctagg

import (
	"encoding/binary"
	"fmt"
	"math"
)

// ValueKind 标识一个标量值的物理类型。
type ValueKind uint8

const (
	KindNull ValueKind = iota
	KindString
	KindInt
	KindFloat
	KindBool
)

// Value 是一个输入单元格。NULL 是一种独立状态（IsNull），
// 与任何非空值（包括空字符串、0、false）都不可混淆。
type Value struct {
	Kind   ValueKind
	IsNull bool
	Str    string
	Int    int64
	Float  float64
	Bool   bool
}

// NullValue 构造一个显式 NULL。
func NullValue() Value { return Value{Kind: KindNull, IsNull: true} }

// StringValue / IntValue / FloatValue / BoolValue 构造非空标量。
func StringValue(s string) Value { return Value{Kind: KindString, Str: s} }
func IntValue(i int64) Value     { return Value{Kind: KindInt, Int: i} }
func FloatValue(f float64) Value { return Value{Kind: KindFloat, Float: f} }
func BoolValue(b bool) Value     { return Value{Kind: KindBool, Bool: b} }

// Equal 比较两个值，NULL 只等于 NULL。
func (v Value) Equal(o Value) bool {
	if v.IsNull || o.IsNull {
		return v.IsNull && o.IsNull
	}
	if v.Kind != o.Kind {
		return false
	}
	switch v.Kind {
	case KindString:
		return v.Str == o.Str
	case KindInt:
		return v.Int == o.Int
	case KindFloat:
		return v.Float == o.Float
	case KindBool:
		return v.Bool == o.Bool
	default:
		return false
	}
}

// 键编码中的类型标记：0 保留给 NULL，非空类型使用 11..14，
// 这样 NULL 与任何非空值的编码都不可能冲突。
const (
	tagNull   = 0
	tagString = 11
	tagInt    = 12
	tagFloat  = 13
	tagBool   = 14
)

// encodeKey 把一列值序列化为无歧义的字节串：
// 每个值带类型标记，变长内容带长度前缀，因此不需要分隔符，
// 也不存在 ("a","bc") 与 ("ab","c") 之类的边界碰撞。
func encodeKey(vals []Value) string {
	b := make([]byte, 0, 32)
	for _, v := range vals {
		if v.IsNull {
			b = append(b, tagNull)
			continue
		}
		switch v.Kind {
		case KindString:
			b = append(b, tagString)
			b = appendUvarint(b, uint64(len(v.Str)))
			b = append(b, v.Str...)
		case KindInt:
			b = append(b, tagInt)
			var tmp [binary.MaxVarintLen64]byte
			n := binary.PutVarint(tmp[:], v.Int)
			b = append(b, tmp[:n]...)
		case KindFloat:
			b = append(b, tagFloat)
			var tmp [8]byte
			binary.BigEndian.PutUint64(tmp[:], math.Float64bits(v.Float))
			b = append(b, tmp[:]...)
		case KindBool:
			b = append(b, tagBool)
			if v.Bool {
				b = append(b, 1)
			} else {
				b = append(b, 0)
			}
		default:
			panic(fmt.Sprintf("distinctagg: unsupported value kind %d", v.Kind))
		}
	}
	return string(b)
}

func appendUvarint(b []byte, v uint64) []byte {
	var tmp [binary.MaxVarintLen64]byte
	n := binary.PutUvarint(tmp[:], v)
	return append(b, tmp[:n]...)
}

// mapKey 给组键加上组集编号前缀，保证不同组集的组互不干扰。
func mapKey(setID int, groupKey string) string {
	var tmp [binary.MaxVarintLen64]byte
	n := binary.PutUvarint(tmp[:], uint64(setID))
	return string(tmp[:n]) + groupKey
}
