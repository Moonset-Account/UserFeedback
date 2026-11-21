//! 端到端测试：调用真实编码 API，并用独立实现的 DNS 线格式解析器从
//! 最终字节解回头部、问题、记录与压缩指针。断言一律基于真实编码产物，
//! 不预置任何演示输出。

use dns_authoritative_encoder::{
    encode_authoritative_response, EncodeError, QueryType, Question, Rdata, RecordGroup,
    ResourceRecord, CLASS_IN, TYPE_A, TYPE_CNAME,
};
use std::net::Ipv4Addr;

#[derive(Debug, PartialEq, Eq)]
enum ParsedRdata {
    A(Ipv4Addr),
    Cname(String),
}

#[derive(Debug)]
struct ParsedRecord {
    name: String,
    rtype: u16,
    class: u16,
    ttl: u32,
    rdlen: u16,
    rdata: ParsedRdata,
}

#[derive(Debug)]
struct ParsedMessage {
    id: u16,
    flags: u16,
    qd: u16,
    an: u16,
    ns: u16,
    ar: u16,
    question: (String, u16, u16),
    answers: Vec<ParsedRecord>,
    additional: Vec<ParsedRecord>,
}

fn u16_at(msg: &[u8], off: usize) -> u16 {
    u16::from_be_bytes([msg[off], msg[off + 1]])
}

fn u32_at(msg: &[u8], off: usize) -> u32 {
    u32::from_be_bytes([msg[off], msg[off + 1], msg[off + 2], msg[off + 3]])
}

/// 解析一个域名；返回点分名字与该名字字段占用的线格式字节数。
/// 独立处理字面标签与压缩指针（高两位 11）。
fn parse_name(msg: &[u8], start: usize) -> (String, usize) {
    let mut labels: Vec<String> = Vec::new();
    let mut off = start;
    let mut field_end: Option<usize> = None;
    let mut guarded = 0usize;
    loop {
        assert!(off < msg.len(), "name ran past message end");
        let tag = msg[off];
        if tag == 0 {
            off += 1;
            break;
        }
        let high_bits = tag & 0xC0;
        if high_bits == POINTER_BITS {
            let target = u16_at(msg, off) as usize & 0x3FFF;
            assert!(target < start, "pointer must point backward");
            if field_end.is_none() {
                field_end = Some(off + 2);
            }
            off = target;
        } else {
            let len = tag as usize;
            assert!(len <= 63, "label length exceeds 63");
            assert!(off + 1 + len <= msg.len(), "label ran past message end");
            labels.push(String::from_utf8(msg[off + 1..off + 1 + len].to_vec()).unwrap());
            off += 1 + len;
        }
        guarded += 1;
        assert!(guarded < 256, "compression loop guard tripped");
    }
    let consumed = field_end.unwrap_or(off) - start;
    (labels.join("."), consumed)
}

const POINTER_BITS: u8 = 0xC0;

fn parse_record(msg: &[u8], mut off: usize) -> (ParsedRecord, usize) {
    let (name, name_len) = parse_name(msg, off);
    off += name_len;
    let rtype = u16_at(msg, off);
    let class = u16_at(msg, off + 2);
    let ttl = u32_at(msg, off + 4);
    let rdlen = u16_at(msg, off + 8);
    off += 10;
    let rdata = match rtype {
        TYPE_A => {
            assert_eq!(rdlen, 4);
            let octets: [u8; 4] = msg[off..off + 4].try_into().unwrap();
            ParsedRdata::A(Ipv4Addr::from(octets))
        }
        TYPE_CNAME => {
            let (target, consumed) = parse_name(msg, off);
            assert_eq!(consumed, rdlen as usize);
            ParsedRdata::Cname(target)
        }
        other => panic!("unexpected record type {other}"),
    };
    off += rdlen as usize;
    (
        ParsedRecord {
            name,
            rtype,
            class,
            ttl,
            rdlen,
            rdata,
        },
        off,
    )
}

fn parse_message(msg: &[u8]) -> ParsedMessage {
    assert!(msg.len() >= 12, "message shorter than header");
    let id = u16_at(msg, 0);
    let flags = u16_at(msg, 2);
    let qd = u16_at(msg, 4);
    let an = u16_at(msg, 6);
    let ns = u16_at(msg, 8);
    let ar = u16_at(msg, 10);
    assert_eq!(qd, 1);

    let mut off = 12;
    let (qname, qlen) = parse_name(msg, off);
    off += qlen;
    let qtype = u16_at(msg, off);
    let qclass = u16_at(msg, off + 2);
    off += 4;

    let mut answers = Vec::new();
    for _ in 0..an {
        let (rr, next) = parse_record(msg, off);
        answers.push(rr);
        off = next;
    }
    let mut additional = Vec::new();
    for _ in 0..ar {
        let (rr, next) = parse_record(msg, off);
        additional.push(rr);
        off = next;
    }
    assert_eq!(off, msg.len(), "trailing bytes after last record");

    ParsedMessage {
        id,
        flags,
        qd,
        an,
        ns,
        ar,
        question: (qname, qtype, qclass),
        answers,
        additional,
    }
}

fn a_record(name: &str, addr: &str, ttl: u32) -> ResourceRecord {
    ResourceRecord::new(name, ttl, Rdata::A(addr.parse::<Ipv4Addr>().unwrap()))
}

fn cname_record(name: &str, target: &str, ttl: u32) -> ResourceRecord {
    ResourceRecord::new(name, ttl, Rdata::Cname(target.to_string()))
}

const QR_AA: u16 = 0x8400;
const QR_AA_TC: u16 = 0x8402;

#[test]
fn normal_a_answer_round_trips_through_wire_bytes() {
    let question = Question::new("host.example.com", QueryType::A);
    let required = vec![RecordGroup::new(vec![a_record(
        "host.example.com",
        "192.0.2.5",
        300,
    )])];

    let resp =
        encode_authoritative_response(0x1234, &question, &required, &[], 512).unwrap();

    assert!(!resp.is_truncated());
    assert_eq!(resp.len(), resp.bytes.len());
    let parsed = parse_message(&resp.bytes);
    assert_eq!(parsed.id, 0x1234);
    assert_eq!(parsed.flags, QR_AA);
    assert_eq!(parsed.qd, 1);
    assert_eq!(parsed.an, 1);
    assert_eq!(parsed.ns, 0);
    assert_eq!(parsed.ar, 0);
    assert_eq!(
        parsed.question,
        ("host.example.com".to_string(), TYPE_A, CLASS_IN)
    );
    assert_eq!(parsed.answers.len(), 1);
    let rr = &parsed.answers[0];
    assert_eq!(rr.name, "host.example.com");
    assert_eq!(rr.rtype, TYPE_A);
    assert_eq!(rr.class, CLASS_IN);
    assert_eq!(rr.ttl, 300);
    assert_eq!(rr.rdlen, 4);
    assert_eq!(rr.rdata, ParsedRdata::A(Ipv4Addr::new(192, 0, 2, 5)));
    assert!(resp.bytes.len() <= 512);
}

#[test]
fn answer_name_reuses_question_name_with_two_byte_pointer() {
    let question = Question::new("host.example.com", QueryType::A);
    let required = vec![RecordGroup::new(vec![a_record(
        "host.example.com",
        "198.51.100.7",
        60,
    )])];
    let resp =
        encode_authoritative_response(0xABCD, &question, &required, &[], 512).unwrap();
    let parsed = parse_message(&resp.bytes);
    assert_eq!(
        parsed.answers[0].rdata,
        ParsedRdata::A(Ipv4Addr::new(198, 51, 100, 7))
    );

    // 问题名从偏移 12 开始；应答名必须是指向该位置的双字节指针 C0 0C。
    let question_wire = [
        4, b'h', b'o', b's', b't', 7, b'e', b'x', b'a', b'm', b'p', b'l', b'e', 3, b'c', b'o',
        b'm', 0,
    ];
    let answer_start = 12 + question_wire.len() + 4;
    assert_eq!(&resp.bytes[12..12 + question_wire.len()], &question_wire);
    assert_eq!(
        &resp.bytes[answer_start..answer_start + 2],
        &[0xC0, 0x0C]
    );
    assert_eq!(
        resp.len(),
        12 + question_wire.len() + 4 + 2 + 2 + 2 + 4 + 2 + 4
    );
    assert_eq!(resp.len(), 50);
}

#[test]
fn cname_chain_compresses_targets_and_round_trips() {
    let question = Question::new("host.example.com", QueryType::Cname);
    // 组 1：CNAME host.example.com → alias.example.com（example.com 复用问题后缀）
    // 组 2：A alias.example.com（整名复用组 1 的 CNAME 目标）
    let required = vec![
        RecordGroup::new(vec![cname_record(
            "host.example.com",
            "alias.example.com",
            100,
        )]),
        RecordGroup::new(vec![a_record("alias.example.com", "203.0.113.9", 200)]),
    ];
    let resp = encode_authoritative_response(7, &question, &required, &[], 512).unwrap();
    let parsed = parse_message(&resp.bytes);
    assert_eq!(parsed.flags, QR_AA);
    assert_eq!(parsed.an, 2);
    assert_eq!(parsed.answers[0].rtype, TYPE_CNAME);
    assert_eq!(parsed.answers[0].class, CLASS_IN);
    assert_eq!(parsed.answers[0].name, "host.example.com");
    assert_eq!(
        parsed.answers[0].rdata,
        ParsedRdata::Cname("alias.example.com".to_string())
    );
    assert_eq!(parsed.answers[1].name, "alias.example.com");
    assert_eq!(
        parsed.answers[1].rdata,
        ParsedRdata::A(Ipv4Addr::new(203, 0, 113, 9))
    );
    assert_eq!(parsed.answers[1].ttl, 200);

    // CNAME 属主名完整复用问题名：C0 0C。
    let question_wire_len = 18;
    let cname_name_off = 12 + question_wire_len + 4;
    assert_eq!(
        &resp.bytes[cname_name_off..cname_name_off + 2],
        &[0xC0, 0x0C]
    );
    // CNAME RDATA：逐字 "alias"（6 字节）后指针到问题中的 example.com（偏移 17）。
    let rdlen_pos = cname_name_off + 2 + 2 + 2 + 4;
    assert_eq!(u16_at(&resp.bytes, rdlen_pos), 8);
    let rdata_pos = rdlen_pos + 2;
    assert_eq!(
        &resp.bytes[rdata_pos..rdata_pos + 8],
        &[5, b'a', b'l', b'i', b'a', b's', 0xC0, 0x11]
    );
}

#[test]
fn longest_suffix_is_preferred_over_shorter_dictionary_entries() {
    // 问题先写出 a.b.example.com；附加记录 c.b.example.com 应复用 3 标签后缀
    // 而不是更短的 example.com。
    let question = Question::new("a.b.example.com", QueryType::A);
    let additional = vec![RecordGroup::new(vec![a_record(
        "c.b.example.com",
        "192.0.2.10",
        1,
    )])];
    let resp =
        encode_authoritative_response(1, &question, &[], &additional, 512).unwrap();
    let parsed = parse_message(&resp.bytes);
    assert_eq!(parsed.ar, 1);
    assert_eq!(parsed.additional[0].name, "c.b.example.com");

    // 问题线格式：1a 1b 7example 3com 0 = 17 字节；
    // b.example.com 标签边界位于偏移 14，因此新名是 1c C0 0E。
    let ar_name_off = 12 + 17 + 4;
    assert_eq!(
        &resp.bytes[ar_name_off..ar_name_off + 4],
        &[1, b'c', 0xC0, 0x0E]
    );
}

#[test]
fn equal_length_suffix_match_selects_earliest_encodable_position() {
    // 问题 a.zone.net；第一个必需记录 b.zone.net（逐字，登记整名边界）；
    // 第二个必需记录 c.zone.net 与两者都共享 2 标签后缀，等长取最早位置
    // （问题里的 zone.net，偏移 14）。
    let question = Question::new("a.zone.net", QueryType::A);
    let required = vec![RecordGroup::new(vec![
        a_record("b.zone.net", "192.0.2.20", 10),
        a_record("c.zone.net", "192.0.2.21", 10),
    ])];
    let resp =
        encode_authoritative_response(9, &question, &required, &[], 512).unwrap();
    let parsed = parse_message(&resp.bytes);
    assert_eq!(parsed.an, 2);
    // 问题：1a 1z 4zone 3net 0 = 12 字节；zone.net 边界在 12+2 = 14。
    // 第一条记录（名字 4 字节 + 固定 10 + 地址 4 = 18 字节）后，第二条名字位于偏移 46。
    let second_name_off = 12 + 12 + 4 + 18;
    assert_eq!(second_name_off, 46);
    assert_eq!(
        &resp.bytes[second_name_off..second_name_off + 4],
        &[1, b'c', 0xC0, 0x0E]
    );
}

#[test]
fn names_without_shared_suffix_are_fully_literal() {
    let question = Question::new("alpha.example.org", QueryType::A);
    let required = vec![RecordGroup::new(vec![a_record(
        "beta.other.net",
        "192.0.2.30",
        10,
    )])];
    let resp =
        encode_authoritative_response(2, &question, &required, &[], 512).unwrap();
    let parsed = parse_message(&resp.bytes);
    assert_eq!(parsed.answers[0].name, "beta.other.net");
    // 整段逐字，不含任何指针字节（高两位 11）。
    let answer_name_wire = [
        4, b'b', b'e', b't', b'a', 5, b'o', b't', b'h', b'e', b'r', 3, b'n', b'e', b't', 0,
    ];
    let name_off = 12 + (1 + 5 + 1 + 7 + 1 + 3 + 1) + 4; // alpha.example.org = 20
    assert_eq!(&resp.bytes[name_off..name_off + answer_name_wire.len()], &answer_name_wire);
    assert!(!resp.bytes[name_off..name_off + answer_name_wire.len()]
        .iter()
        .any(|b| b & 0xC0 == 0xC0));
}

#[test]
fn response_uses_exact_remaining_space_without_padding() {
    let question = Question::new("host.example.com", QueryType::A);
    let required = vec![RecordGroup::new(vec![a_record(
        "host.example.com",
        "192.0.2.5",
        300,
    )])];
    // 头 12 + 问题名 18 + 问题尾 4 + 应答（指针名 2 + 类型2 + 类2 + TTL4 + 长度2 + 地址4）= 50。
    let exact = 12 + 18 + 4 + 2 + 2 + 2 + 4 + 2 + 4;
    assert_eq!(exact, 50);
    let resp =
        encode_authoritative_response(3, &question, &required, &[], exact).unwrap();
    assert_eq!(resp.len(), exact);
    parse_message(&resp.bytes);
}

#[test]
fn one_byte_less_than_exact_rolls_back_required_group_and_sets_tc() {
    let question = Question::new("host.example.com", QueryType::A);
    let required = vec![RecordGroup::new(vec![a_record(
        "host.example.com",
        "192.0.2.5",
        300,
    )])];
    let exact = 50;
    let resp =
        encode_authoritative_response(3, &question, &required, &[], exact - 1).unwrap();
    assert!(resp.is_truncated());
    assert_eq!(resp.answer_count, 0);
    assert_eq!(resp.additional_count, 0);
    // 截断后长度恰好停在问题结束处，没有残留半条记录。
    assert_eq!(resp.len(), 12 + 18 + 4);
    let parsed = parse_message(&resp.bytes);
    assert_eq!(parsed.flags, QR_AA_TC);
    assert_eq!(parsed.an, 0);
    assert_eq!(parsed.ar, 0);
    assert!(parsed.answers.is_empty());
}

#[test]
fn failed_required_group_halts_later_required_groups() {
    let question = Question::new("host.example.com", QueryType::A);
    let required = vec![
        RecordGroup::new(vec![a_record("huge.first.example.com", "192.0.2.1", 10)]),
        RecordGroup::new(vec![a_record("host.example.com", "192.0.2.2", 10)]),
    ];
    let resp =
        encode_authoritative_response(4, &question, &required, &[], 36).unwrap();
    assert!(resp.is_truncated());
    assert_eq!(resp.answer_count, 0);
    let parsed = parse_message(&resp.bytes);
    assert_eq!(parsed.flags, QR_AA_TC);
    assert!(parsed.answers.is_empty());
}

#[test]
fn oversized_additional_groups_are_skipped_but_later_groups_continue() {
    let question = Question::new("host.example.com", QueryType::A);
    let additional = vec![
        // 第一组两条，整体放不下 → 整组跳过。
        RecordGroup::new(vec![
            a_record("one.add.example.com", "192.0.2.31", 10),
            a_record("two.add.example.com", "192.0.2.32", 10),
        ]),
        // 下一条单记录恰好能放进剩余空间 → 提交。
        RecordGroup::new(vec![a_record("host.example.com", "192.0.2.33", 10)]),
    ];
    // 头+问题占 34；单条指针化 A 记录 16 字节，预算只允许后者。
    let resp =
        encode_authoritative_response(5, &question, &[], &additional, 34 + 16).unwrap();
    assert!(!resp.is_truncated());
    assert_eq!(resp.len(), 50);
    assert_eq!(resp.additional_count, 1);
    let parsed = parse_message(&resp.bytes);
    assert_eq!(parsed.flags, QR_AA);
    assert_eq!(parsed.ar, 1);
    assert_eq!(parsed.additional.len(), 1);
    assert_eq!(parsed.additional[0].name, "host.example.com");
    assert_eq!(
        parsed.additional[0].rdata,
        ParsedRdata::A(Ipv4Addr::new(192, 0, 2, 33))
    );
}

#[test]
fn interleaved_groups_compress_across_sections_and_all_pointers_resolve() {
    let question = Question::new("svc.corp.example.com", QueryType::A);
    let required = vec![
        RecordGroup::new(vec![cname_record(
            "svc.corp.example.com",
            "node.corp.example.com",
            50,
        )]),
        RecordGroup::new(vec![a_record("node.corp.example.com", "10.0.0.1", 60)]),
    ];
    let additional = vec![RecordGroup::new(vec![a_record(
        "edge.corp.example.com",
        "10.0.0.2",
        70,
    )])];
    let resp =
        encode_authoritative_response(0x77, &question, &required, &additional, 512).unwrap();
    let parsed = parse_message(&resp.bytes);
    assert_eq!(parsed.an, 2);
    assert_eq!(parsed.ar, 1);
    assert_eq!(
        parsed.answers[0].rdata,
        ParsedRdata::Cname("node.corp.example.com".to_string())
    );
    assert_eq!(parsed.answers[1].name, "node.corp.example.com");
    assert_eq!(parsed.additional[0].name, "edge.corp.example.com");
    // 所有指针必须是合法的 11xxxxxx + 14 位向后偏移。
    for (i, b) in resp.bytes.iter().enumerate() {
        if b & 0xC0 == 0xC0 {
            let target = usize::from(u16_at(&resp.bytes, i) & 0x3FFF);
            assert!(target < i, "pointer at {i} targets {target} (not backward)");
        }
    }
}

#[test]
fn invalid_names_propagate_name_error() {
    let question = Question::new("Bad.Example.COM", QueryType::A);
    let err = encode_authoritative_response(1, &question, &[], &[], 512).unwrap_err();
    assert!(matches!(err, EncodeError::NameInvalid(_)));

    let good_question = Question::new("ok.example.com", QueryType::A);
    let bad_required = vec![RecordGroup::new(vec![a_record(
        "bad_exmple.com",
        "192.0.2.1",
        10,
    )])];
    let err =
        encode_authoritative_response(1, &good_question, &bad_required, &[], 512).unwrap_err();
    assert!(matches!(err, EncodeError::NameInvalid(_)));
}

#[test]
fn budgets_smaller_than_header_and_question_overflow_are_errors() {
    let question = Question::new("host.example.com", QueryType::A);
    let err = encode_authoritative_response(1, &question, &[], &[], 11).unwrap_err();
    match err {
        EncodeError::BufferTooSmall { needed, budget } => {
            assert_eq!(needed, 12);
            assert_eq!(budget, 11);
        }
        other => panic!("expected BufferTooSmall, got {other:?}"),
    }

    // 头能放下，但问题（18 + 4 = 22 字节）放不下。
    let err = encode_authoritative_response(1, &question, &[], &[], 20).unwrap_err();
    match err {
        EncodeError::BufferTooSmall { needed, budget } => {
            assert!(needed > budget);
            assert_eq!(budget, 20);
        }
        other => panic!("expected BufferTooSmall, got {other:?}"),
    }
}

#[test]
fn empty_groups_and_root_question_name_are_supported() {
    let question = Question::new("", QueryType::A);
    let required = vec![RecordGroup::new(vec![]), RecordGroup::new(vec![])];
    let additional = vec![RecordGroup::new(vec![])];
    let resp =
        encode_authoritative_response(0x00FF, &question, &required, &additional, 64).unwrap();
    assert!(!resp.is_truncated());
    assert_eq!(resp.answer_count, 0);
    assert_eq!(resp.additional_count, 0);
    assert_eq!(resp.len(), 12 + 1 + 4);
    let parsed = parse_message(&resp.bytes);
    assert_eq!(parsed.question.0, "");
    assert_eq!(parsed.flags, QR_AA);
}
