//! 权威 DNS 应答线格式编码器（仅依赖 Rust 标准库）。
//!
//! 输入查询 ID、一条可完整放入预算的问题、必须回答的有序记录组（Answer 区）
//! 与可选附加记录组（Additional 区），按 DNS 线格式（RFC 1035）编码应答。
//!
//! 约束：
//! - 名字为合法小写 ASCII 域名，记录仅含 A 与目标为域名的 CNAME，类别均为 IN；
//! - 应答头初始只置 QR、AA 位，必需组整体放不下时追加置 TC 并立即结束；
//! - 名称压缩尽量复用此前已写出名字的最长后缀，等长时取最早可编码位置，
//!   且只有确实能省字节时才发指针；
//! - 必需组按组原子提交（失败整组撤回），附加组失败则整组跳过并继续后续组。

#![deny(unsafe_code)]

use std::fmt;
use std::net::Ipv4Addr;

/// IN 类别。
pub const CLASS_IN: u16 = 1;
/// A 记录类型。
pub const TYPE_A: u16 = 1;
/// CNAME 记录类型。
pub const TYPE_CNAME: u16 = 5;

/// DNS 定长头长度（字节）。
const HEADER_LEN: usize = 12;
/// 单个标签的最大线格式长度。
const MAX_LABEL_LEN: usize = 63;
/// 未压缩域名的最大线格式长度（含结尾零标签）。
const MAX_NAME_WIRE_LEN: usize = 255;
/// 压缩指针 14 位偏移的最大值。
const MAX_POINTER_OFFSET: usize = 0x3FFF;
/// 指针首字节的高两位标记。
const POINTER_MARKER: u8 = 0xC0;

/// 编码过程中可能出现的错误。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum EncodeError {
    /// 域名不合法（非小写 ASCII、标签为空/超长、总长超 255 等）。
    NameInvalid(String),
    /// 字节预算不足以容纳头或调用方保证可完整放入的问题。
    BufferTooSmall {
        /// 继续编码至少需要的总字节数。
        needed: usize,
        /// 调用方给出的字节预算。
        budget: usize,
    },
    /// 某个区的记录数超过 16 位计数可表达的范围。
    CountOverflow,
}

impl fmt::Display for EncodeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            EncodeError::NameInvalid(name) => write!(f, "invalid domain name: {name:?}"),
            EncodeError::BufferTooSmall { needed, budget } => write!(
                f,
                "byte budget too small: need at least {needed} bytes, budget is {budget}"
            ),
            EncodeError::CountOverflow => {
                write!(f, "section record count exceeds u16 range")
            }
        }
    }
}

impl std::error::Error for EncodeError {}

/// 问题支持的查询类型。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum QueryType {
    /// A 查询（主机地址）。
    A = TYPE_A as isize,
    /// CNAME 查询（规范名）。
    Cname = TYPE_CNAME as isize,
}

impl QueryType {
    /// 返回线格式类型值。
    pub fn wire_value(self) -> u16 {
        match self {
            QueryType::A => TYPE_A,
            QueryType::Cname => TYPE_CNAME,
        }
    }
}

/// 查询问题：QNAME + QTYPE + QCLASS(IN)。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Question {
    /// 查询域名（合法小写 ASCII）。
    pub qname: String,
    /// 查询类型。
    pub qtype: QueryType,
}

impl Question {
    /// 创建一条 IN 类问题。
    pub fn new(qname: impl Into<String>, qtype: QueryType) -> Self {
        Self {
            qname: qname.into(),
            qtype,
        }
    }
}

/// 资源记录的 RDATA：本模块只支持 A 与目标为域名的 CNAME。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Rdata {
    /// 四个字节的 IPv4 地址。
    A(Ipv4Addr),
    /// CNAME 目标域名（合法小写 ASCII）。
    Cname(String),
}

impl Rdata {
    fn wire_type(&self) -> u16 {
        match self {
            Rdata::A(_) => TYPE_A,
            Rdata::Cname(_) => TYPE_CNAME,
        }
    }
}

/// 一条资源记录（类别固定 IN）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResourceRecord {
    /// 属主域名。
    pub name: String,
    /// 32 位 TTL。
    pub ttl: u32,
    /// RDATA。
    pub rdata: Rdata,
}

impl ResourceRecord {
    /// 创建一条 IN 类资源记录。
    pub fn new(name: impl Into<String>, ttl: u32, rdata: Rdata) -> Self {
        Self {
            name: name.into(),
            ttl,
            rdata,
        }
    }
}

/// 有序记录组：组内记录要么整体提交，要么整体撤回/跳过。
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct RecordGroup {
    /// 组内有序记录。
    pub records: Vec<ResourceRecord>,
}

impl RecordGroup {
    /// 用一组有序记录创建记录组。
    pub fn new(records: Vec<ResourceRecord>) -> Self {
        Self { records }
    }
}

/// 成功编码后的应答产物；所有字段均可从 [`EncodedResponse::bytes`] 解出。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EncodedResponse {
    /// 完整 DNS 应答线格式字节。
    pub bytes: Vec<u8>,
    /// 是否置了 TC（某个必需组整体放不下）。
    pub truncated: bool,
    /// 头中的 QDCOUNT（恒为 1）。
    pub question_count: u16,
    /// 头中的 ANCOUNT。
    pub answer_count: u16,
    /// 头中的 NSCOUNT（恒为 0）。
    pub name_server_count: u16,
    /// 头中的 ARCOUNT。
    pub additional_count: u16,
}

impl EncodedResponse {
    /// 应答总长度（字节），等于线格式字节数。
    pub fn len(&self) -> usize {
        self.bytes.len()
    }

    /// 应答是否为空（DNS 头恒存在，因此恒为 false）。
    pub fn is_empty(&self) -> bool {
        self.bytes.is_empty()
    }

    /// 返回线格式字节切片。
    pub fn as_bytes(&self) -> &[u8] {
        &self.bytes
    }

    /// 是否因必需组放不下而被截断。
    pub fn is_truncated(&self) -> bool {
        self.truncated
    }
}

/// 压缩字典条目：某条已编码名字在其标签边界 `i` 上，
/// 绝对偏移 `offset` 处开始的逻辑标签序列为 `labels`。
#[derive(Debug, Clone)]
struct DictEntry {
    offset: usize,
    labels: Vec<Vec<u8>>,
}

/// 最小字节汇抽象，使同一套名称编码逻辑既可写临时 Vec，也可写主缓冲。
trait ByteSink {
    fn sink_push(&mut self, byte: u8);
    fn sink_extend(&mut self, bytes: &[u8]);
}

impl ByteSink for Vec<u8> {
    fn sink_push(&mut self, byte: u8) {
        self.push(byte);
    }
    fn sink_extend(&mut self, bytes: &[u8]) {
        self.extend_from_slice(bytes);
    }
}

/// 把合法域名拆成标签字节序列；空字符串表示根名（线格式为单个 0）。
fn split_name(name: &str) -> Result<Vec<Vec<u8>>, EncodeError> {
    if name.is_empty() {
        return Ok(Vec::new());
    }
    let bytes = name.as_bytes();
    let legal = bytes
        .iter()
        .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || *b == b'-' || *b == b'.');
    if !legal {
        return Err(EncodeError::NameInvalid(name.to_string()));
    }
    let mut labels = Vec::new();
    let mut wire_len = 1usize; // 结尾零标签
    for raw in name.split('.') {
        if raw.is_empty() || raw.len() > MAX_LABEL_LEN {
            return Err(EncodeError::NameInvalid(name.to_string()));
        }
        wire_len += 1 + raw.len();
        labels.push(raw.as_bytes().to_vec());
    }
    if wire_len > MAX_NAME_WIRE_LEN {
        return Err(EncodeError::NameInvalid(name.to_string()));
    }
    Ok(labels)
}

/// 标签边界 `i` 之前逐字标签前缀占用的字节数。
fn literal_prefix_len(labels: &[Vec<u8>], i: usize) -> usize {
    labels[..i].iter().map(|label| 1 + label.len()).sum()
}

/// 名称压缩编码核心。
///
/// `abs_base` 为名字字段首字节在最终报文中的绝对偏移；字典条目与指针目标
/// 统一使用最终报文绝对坐标，从而能正确校验 14 位指针范围，主游标与单记录
/// 编码器行为完全一致。
fn write_name_inner(
    labels: &[Vec<u8>],
    sink: &mut impl ByteSink,
    abs_base: usize,
    dict: &mut Vec<DictEntry>,
) -> Result<(), EncodeError> {
    let n = labels.len();
    if n == 0 {
        // 根名：单个零标签。
        sink.sink_push(0);
        return Ok(());
    }

    // 在字典中寻找最优后缀：最长匹配优先，等长取最早（最小）绝对偏移。
    let mut best: Option<(usize, usize)> = None; // (匹配标签数 k, 指针目标绝对偏移)
    for entry in dict.iter() {
        let m = entry.labels.len();
        let mut k = m.min(n);
        while k > 0 && entry.labels[m - k..] != labels[n - k..] {
            k -= 1;
        }
        if k == 0 {
            continue;
        }
        // 命中的后缀从该条目第 (m-k) 个逻辑标签开始，因此指针目标为
        // 条目绝对起点加上前 (m-k) 个标签的逐字长度（k==m 时偏移为 0）。
        let target = entry.offset + literal_prefix_len(&entry.labels, m - k);
        if target > MAX_POINTER_OFFSET {
            continue; // 目标无法用 14 位指针编码，该字典条目不可用
        }
        let suffix_bytes: usize = labels[n - k..].iter().map(|label| label.len()).sum();
        let literal_cost = suffix_bytes + k + 1; // 标签长度字节 + 标签 + 结尾零
        if literal_cost <= 2 {
            continue; // 只有能省字节才用指针
        }
        let better = match best {
            None => true,
            Some((best_k, best_offset)) => k > best_k || (k == best_k && target < best_offset),
        };
        if better {
            best = Some((k, target));
        }
    }

    // abs_base 即名字字段首字节在最终报文中的绝对偏移；字段内边界只需
    // 加上逐字前缀长度即可得到绝对偏移，不再叠加 sink 当前长度。
    match best {
        Some((k, target)) => {
            let literal_labels = n - k;
            // 逐字前缀的每个标签边界都登记进字典，供后续名字复用。
            for i in 0..literal_labels {
                dict.push(DictEntry {
                    offset: abs_base + literal_prefix_len(labels, i),
                    labels: labels[i..].to_vec(),
                });
            }
            for i in 0..literal_labels {
                sink.sink_push(labels[i].len() as u8);
                sink.sink_extend(&labels[i]);
            }
            sink.sink_push(POINTER_MARKER | (target >> 8) as u8);
            sink.sink_push((target & 0xFF) as u8);
        }
        None => {
            // 无可复用后缀：完整逐字编码，登记全部标签边界。
            for i in 0..n {
                dict.push(DictEntry {
                    offset: abs_base + literal_prefix_len(labels, i),
                    labels: labels[i..].to_vec(),
                });
            }
            for label in labels {
                sink.sink_push(label.len() as u8);
                sink.sink_extend(label);
            }
            sink.sink_push(0);
        }
    }
    Ok(())
}

/// 主报文游标：所有写入先做容量检查，保证失败时不留半截内容。
struct Cursor<'a> {
    buf: &'a mut [u8],
    pos: usize,
    dict: Vec<DictEntry>,
}

impl<'a> Cursor<'a> {
    fn remaining(&self) -> usize {
        self.buf.len() - self.pos
    }

    fn take(&mut self, n: usize) -> Result<&mut [u8], EncodeError> {
        if self.remaining() < n {
            return Err(EncodeError::BufferTooSmall {
                needed: self.pos + n,
                budget: self.buf.len(),
            });
        }
        let start = self.pos;
        self.pos += n;
        Ok(&mut self.buf[start..self.pos])
    }

    fn put_u16(&mut self, value: u16) -> Result<(), EncodeError> {
        self.take(2)?.copy_from_slice(&value.to_be_bytes());
        Ok(())
    }

    /// 先在临时缓冲中完成名称编码（含压缩决策），整块可容纳时再原子提交。
    fn write_name(&mut self, name: &str) -> Result<(), EncodeError> {
        let labels = split_name(name)?;
        let mut tmp: Vec<u8> = Vec::new();
        let mut local_dict: Vec<DictEntry> = Vec::new();
        // self.pos 是这个名字字段首字节在最终报文中的绝对偏移；
        // 临时缓冲从空开始，核心函数据此直接登记绝对偏移的字典条目。
        write_name_inner(&labels, &mut tmp, self.pos, &mut local_dict)?;
        let slot = self.take(tmp.len())?;
        slot.copy_from_slice(&tmp);
        self.dict.extend(local_dict);
        Ok(())
    }
}

/// 临时名称编码器：用于单条记录，字典偏移按最终报文绝对偏移记账。
///
/// `dict` 以主字典快照初始化，使记录名/CNAME 目标能复用问题区及此前记录
/// 已写出的名字；本记录新登记的条目保留在末尾，提交时由调用方并回主字典。
struct NameWriter {
    out: Vec<u8>,
    dict: Vec<DictEntry>,
    base: usize,
}

impl NameWriter {
    fn new(base: usize, existing: &[DictEntry]) -> Self {
        Self {
            out: Vec::new(),
            dict: existing.to_vec(),
            base,
        }
    }

    /// 本记录新登记（而非继承自主字典）的条目数。
    fn added_entries(&self, inherited: usize) -> usize {
        self.dict.len() - inherited
    }

    fn write_name(&mut self, name: &str) -> Result<(), EncodeError> {
        let labels = split_name(name)?;
        // 这个名字字段从 out 当前长度处开始，其最终报文绝对偏移为
        // base + out.len()；核心函数产出的条目偏移因此落在统一绝对坐标。
        let abs_base = self.base + self.out.len();
        write_name_inner(&labels, &mut self.out, abs_base, &mut self.dict)
    }
}

/// 把单条记录编码为线格式字节；`base` 为该记录在最终报文中的绝对起点，
/// `existing` 为主字典当前快照。返回的新条目偏移已是最终报文绝对偏移。
fn encode_record(
    record: &ResourceRecord,
    base: usize,
    existing: &[DictEntry],
) -> Result<(Vec<u8>, Vec<DictEntry>), EncodeError> {
    let inherited = existing.len();
    let mut writer = NameWriter::new(base, existing);
    writer.write_name(&record.name)?;

    writer.out.extend_from_slice(&record.rdata.wire_type().to_be_bytes());
    writer.out.extend_from_slice(&CLASS_IN.to_be_bytes());
    writer.out.extend_from_slice(&record.ttl.to_be_bytes());

    let rdlen_pos = writer.out.len();
    writer.out.extend_from_slice(&0u16.to_be_bytes()); // RDLENGTH 占位

    match &record.rdata {
        Rdata::A(addr) => writer.out.extend_from_slice(&addr.octets()),
        Rdata::Cname(target) => writer.write_name(target)?,
    }

    let rdlen = (writer.out.len() - rdlen_pos - 2) as u16;
    writer.out[rdlen_pos..rdlen_pos + 2].copy_from_slice(&rdlen.to_be_bytes());

    let added = writer.added_entries(inherited);
    let new_entries = writer.dict[writer.dict.len() - added..].to_vec();
    Ok((writer.out, new_entries))
}

/// 尝试整体提交一个记录组。
///
/// 成功返回 `true` 且字典合并主字典；任一条记录放不下则字节与字典全部
/// 回滚到组前快照并返回 `false`。
fn try_commit_group(cursor: &mut Cursor<'_>, group: &RecordGroup) -> Result<bool, EncodeError> {
    let snapshot_pos = cursor.pos;
    let snapshot_dict = cursor.dict.len();

    for record in &group.records {
        let (wire, local_dict) = encode_record(record, cursor.pos, &cursor.dict)?;
        if cursor.remaining() < wire.len() {
            cursor.pos = snapshot_pos;
            cursor.dict.truncate(snapshot_dict);
            return Ok(false);
        }
        let start = cursor.pos;
        cursor.buf[start..start + wire.len()].copy_from_slice(&wire);
        cursor.pos += wire.len();
        // NameWriter 以 cursor.pos 为绝对 base，条目偏移已是最终报文绝对偏移，直接并入。
        cursor.dict.extend(local_dict);
    }
    Ok(true)
}

/// 编码权威应答。
///
/// 参数：
/// - `query_id`：查询 ID，原样写入头；
/// - `question`：唯一问题，调用方保证其可完整放入预算；
/// - `required`：Answer 区有序必需记录组，按组原子提交；
/// - `additional`：Additional 区可选记录组，失败整组跳过并继续；
/// - `budget`：调用方给出的字节预算。
///
/// 返回的 [`EncodedResponse`] 中计数、长度与名称指针均可从最终字节解出。
pub fn encode_authoritative_response(
    query_id: u16,
    question: &Question,
    required: &[RecordGroup],
    additional: &[RecordGroup],
    budget: usize,
) -> Result<EncodedResponse, EncodeError> {
    if budget < HEADER_LEN {
        return Err(EncodeError::BufferTooSmall {
            needed: HEADER_LEN,
            budget,
        });
    }

    let mut buf = vec![0u8; budget];
    let mut cursor = Cursor {
        buf: &mut buf,
        pos: 0,
        dict: Vec::new(),
    };

    // 占位定长头；提交策略确定后再回填各字段。
    cursor.take(HEADER_LEN)?;

    // 问题区（QDCOUNT 恒为 1）。
    cursor.write_name(&question.qname)?;
    cursor.put_u16(question.qtype.wire_value())?;
    cursor.put_u16(CLASS_IN)?;

    // 必需组：任一整组放不下 → 回滚、置 TC、立即结束。
    let mut answer_count: u16 = 0;
    let mut truncated = false;
    for group in required {
        if try_commit_group(&mut cursor, group)? {
            let group_len = u16::try_from(group.records.len())
                .map_err(|_| EncodeError::CountOverflow)?;
            answer_count = answer_count
                .checked_add(group_len)
                .ok_or(EncodeError::CountOverflow)?;
        } else {
            truncated = true;
            break;
        }
    }

    // 附加组：整组放不下就跳过该组，继续尝试后续组；截断后不再处理。
    let mut additional_count: u16 = 0;
    if !truncated {
        for group in additional {
            if try_commit_group(&mut cursor, group)? {
                let group_len = u16::try_from(group.records.len())
                    .map_err(|_| EncodeError::CountOverflow)?;
                additional_count = additional_count
                    .checked_add(group_len)
                    .ok_or(EncodeError::CountOverflow)?;
            }
        }
    }

    let total_len = cursor.pos;
    let flags: u16 = 0x8400u16 | if truncated { 0x0002 } else { 0 }; // QR + AA (+TC)

    buf[0..2].copy_from_slice(&query_id.to_be_bytes());
    buf[2..4].copy_from_slice(&flags.to_be_bytes());
    buf[4..6].copy_from_slice(&1u16.to_be_bytes()); // QDCOUNT
    buf[6..8].copy_from_slice(&answer_count.to_be_bytes()); // ANCOUNT
    buf[8..10].copy_from_slice(&0u16.to_be_bytes()); // NSCOUNT
    buf[10..12].copy_from_slice(&additional_count.to_be_bytes()); // ARCOUNT

    buf.truncate(total_len);

    Ok(EncodedResponse {
        bytes: buf,
        truncated,
        question_count: 1,
        name_server_count: 0,
        answer_count,
        additional_count,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn entry(offset: usize, labels: &[&str]) -> DictEntry {
        DictEntry {
            offset,
            labels: labels.iter().map(|s| s.as_bytes().to_vec()).collect(),
        }
    }

    fn encode_with_dict(
        name: &str,
        base: usize,
        dict: &mut Vec<DictEntry>,
    ) -> Vec<u8> {
        let labels = split_name(name).unwrap();
        let mut out = Vec::new();
        write_name_inner(&labels, &mut out, base, dict).unwrap();
        out
    }

    #[test]
    fn root_name_is_single_zero_octet() {
        let mut dict = Vec::new();
        assert_eq!(encode_with_dict("", 0, &mut dict), vec![0]);
    }

    #[test]
    fn full_name_match_emits_pointer() {
        let mut dict = vec![entry(12, &["host", "example", "com"])];
        assert_eq!(
            encode_with_dict("host.example.com", 0, &mut dict),
            vec![0xC0, 0x0C]
        );
    }

    #[test]
    fn longest_suffix_wins_over_shorter_match() {
        let mut dict = vec![
            entry(20, &["host", "sub", "example", "com"]),
            entry(40, &["example", "com"]),
        ];
        // 可匹配 3 个标签：目标为条目 20 中 sub 标签的边界，
        // 即 20 + (1+4) = 25（0x19），优于只匹配 2 个标签的条目 40。
        assert_eq!(
            encode_with_dict("new.sub.example.com", 0, &mut dict),
            vec![3, b'n', b'e', b'w', 0xC0, 0x19]
        );
    }

    #[test]
    fn equal_length_match_uses_earliest_offset() {
        let mut dict = vec![
            entry(30, &["a", "example", "com"]),
            entry(50, &["b", "example", "com"]),
        ];
        // 两者都只后缀匹配 2 个标签；最早目标为 30 + (1+1) = 32（0x20）。
        assert_eq!(
            encode_with_dict("x.example.com", 0, &mut dict),
            vec![1, b'x', 0xC0, 0x20]
        );
    }

    #[test]
    fn single_label_suffix_still_saves_one_byte_minimum() {
        // 长度为 1 的标签：逐字需 3 字节（1+1+0），指针 2 字节，仍省 1 字节。
        let mut dict = vec![entry(10, &["x"])];
        assert_eq!(
            encode_with_dict("y.x", 0, &mut dict),
            vec![1, b'y', 0xC0, 0x0A]
        );
    }

    #[test]
    fn no_shared_suffix_is_fully_literal() {
        let mut dict = vec![entry(12, &["example", "org"])];
        assert_eq!(
            encode_with_dict("other.net", 0, &mut dict),
            vec![5, b'o', b't', b'h', b'e', b'r', 3, b'n', b'e', b't', 0]
        );
    }

    #[test]
    fn pointer_beyond_14_bit_range_is_rejected() {
        // 目标 0x4000 超出 14 位指针范围，该字典条目不可用，回退逐字编码。
        let mut dict = vec![entry(0x4000, &["a", "com"])];
        assert_eq!(
            encode_with_dict("b.com", 0, &mut dict),
            vec![1, b'b', 3, b'c', b'o', b'm', 0]
        );
    }

    #[test]
    fn prefix_boundaries_are_registered_for_later_names() {
        let mut dict = vec![entry(12, &["long", "example", "com"])];
        // "host.long.example.com" → 逐字 "host" 后整后缀指针到 12 处的 long.example.com。
        let first = encode_with_dict("host.long.example.com", 16, &mut dict);
        assert_eq!(first, vec![4, b'h', b'o', b's', b't', 0xC0, 0x0C]);
        // 新登记的整名边界（绝对偏移 16）可让后续名字以更长的 4 标签后缀复用。
        let second = encode_with_dict("a.host.long.example.com", 40, &mut dict);
        assert_eq!(second, vec![1, b'a', 0xC0, 0x10]);
    }

    #[test]
    fn rejects_illegal_names() {
        for bad in [
            "Example.com",
            "bad..com",
            ".leading.com",
            "trailing.com.",
            "under_score.com",
            "white space.com",
        ] {
            assert!(matches!(
                split_name(bad),
                Err(EncodeError::NameInvalid(_))
            ));
        }
        let long_label = "a".repeat(64);
        assert!(matches!(
            split_name(&format!("{long_label}.com")),
            Err(EncodeError::NameInvalid(_))
        ));
        assert!(matches!(
            split_name(&"a".repeat(256)),
            Err(EncodeError::NameInvalid(_))
        ));
    }

    #[test]
    fn accepts_legal_names() {
        assert!(split_name("a-b.c9.example.com").is_ok());
        assert!(split_name("").is_ok());
        assert!(split_name("x").is_ok());
    }
}
