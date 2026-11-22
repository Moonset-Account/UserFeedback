//! `ProgressCursor` 的分段推进与确认点对接测试。

use rngflow::rng::{Purpose, StreamKey};
use rngflow::{CallProgress, Checkpoint, ProgressCursor};

#[test]
fn progress_cursor_resumes_across_segments_and_checkpoint() {
    let key = StreamKey::new(3, 5, 8, "blocks/1/dropout", 2, Purpose::DROPOUT);

    // 第一段：dropout 推进 6 个位置，routing 推进 2 个位置，然后落确认点。
    let mut cursor = ProgressCursor::new(key);
    for _ in 0..6 {
        cursor.stream(Purpose::DROPOUT).accept();
    }
    for _ in 0..2 {
        cursor.stream(Purpose::ROUTING).accept();
    }
    let mut cp = Checkpoint::new();
    for (stream_key, next, rej) in cursor.progress().snapshot() {
        cp.record_progress(stream_key, next, rej);
    }

    // 第二段：模拟进程重启，从确认点恢复每条流后继续推进。
    let mut entries = Vec::new();
    for (_, record) in cp.iter() {
        entries.push((record.key.purpose, record.next_position, record.rejections.clone()));
    }
    let mut resumed = ProgressCursor::from_progress(CallProgress::restore(key, entries));
    for _ in 0..4 {
        resumed.stream(Purpose::DROPOUT).accept();
    }

    // 与一次跑完的游标逐位置比对。
    let mut full = ProgressCursor::new(key);
    for _ in 0..10 {
        full.stream(Purpose::DROPOUT).accept();
    }
    for _ in 0..2 {
        full.stream(Purpose::ROUTING).accept();
    }
    let dropout_resumed = resumed.progress().stream_ref(Purpose::DROPOUT).unwrap();
    let dropout_full = full.progress().stream_ref(Purpose::DROPOUT).unwrap();
    assert_eq!(dropout_resumed.progress(), dropout_full.progress());
    for p in 0..dropout_full.accepted_len() {
        assert_eq!(
            dropout_resumed.accepted_word(p),
            dropout_full.accepted_word(p),
            "分段恢复后位置 {p} 的接受值漂移"
        );
    }
}
