//! 集成测试：正常、边界与交错场景。
//!
//! 覆盖题面点名场景：跨进程别名、一个线程同时等两个位置、移交时目标已
//! 有同一等待者；并核对每次队列快照与唤醒返回值。

use shm_wait_queue::{
    Kernel, RequeueError, WaitError, WaitOutcome,
};

const P1: u32 = 1;
const P2: u32 = 2;
const T1: u32 = 11;
const T2: u32 = 12;
const T3: u32 = 13;

/// 便捷：建内核、建一个对象、写入初值并映射到进程地址。
fn kernel_with_word(pid: u32, addr: u64, value: u64) -> (Kernel, u64) {
    let mut k = Kernel::new();
    let obj = k.create_object();
    k.map(pid, addr, obj, 0);
    k.write_word(pid, addr, value).unwrap();
    (k, obj)
}

// ---------- 正常路径 ----------

#[test]
fn wait_then_wake_fifo_order() {
    let (mut k, _) = kernel_with_word(P1, 0x1000, 0);
    let key = k.resolve(P1, 0x1000).unwrap();

    let w1 = k.wait(T1, P1, &[(0x1000, 0)], None).unwrap();
    let w2 = k.wait(T2, P1, &[(0x1000, 0)], None).unwrap();
    let w3 = k.wait(T3, P1, &[(0x1000, 0)], None).unwrap();
    assert_eq!(k.queue_snapshot(key), vec![w1, w2, w3]);

    // 唤醒 2 个：严格按等待顺序，返回被唤醒者。
    let woken = k.wake(P1, 0x1000, 2);
    assert_eq!(woken, vec![w1, w2]);
    assert_eq!(k.outcome(w1), Some(WaitOutcome::Woken));
    assert_eq!(k.outcome(w2), Some(WaitOutcome::Woken));
    assert_eq!(k.outcome(w3), None);
    assert_eq!(k.queue_snapshot(key), vec![w3]);

    // 唤醒数大于队列长度：只唤醒剩余者。
    let woken = k.wake(P1, 0x1000, 10);
    assert_eq!(woken, vec![w3]);
    assert_eq!(k.queue_snapshot(key), Vec::<u64>::new());
    // 空队列上唤醒返回空。
    assert_eq!(k.wake(P1, 0x1000, 1), Vec::<u64>::new());
}

#[test]
fn wait_value_mismatch_registers_nothing() {
    let (mut k, _) = kernel_with_word(P1, 0x1000, 1);
    let key = k.resolve(P1, 0x1000).unwrap();

    let err = k.wait(T1, P1, &[(0x1000, 0)], None).unwrap_err();
    assert_eq!(
        err,
        WaitError::ValueMismatch { addr: 0x1000, expected: 0, actual: 1 }
    );
    assert_eq!(k.queue_snapshot(key), Vec::<u64>::new());
}

#[test]
fn wait_on_unmapped_address_fails() {
    let mut k = Kernel::new();
    let err = k.wait(T1, P1, &[(0xdead, 0)], None).unwrap_err();
    assert_eq!(err, WaitError::Unmapped { addr: 0xdead });
    // 未映射地址上的唤醒是空操作。
    assert_eq!(k.wake(P1, 0xdead, 5), Vec::<u64>::new());
}

// ---------- 跨进程别名与对象世代 ----------

#[test]
fn cross_process_alias_same_object() {
    let mut k = Kernel::new();
    let obj = k.create_object();
    // 同一对象同一偏移，映射到两个进程的不同虚拟地址。
    k.map(P1, 0x1000, obj, 8);
    k.map(P2, 0x9000, obj, 8);
    k.write_word(P1, 0x1000, 7).unwrap();
    // 别名可见：P2 读到 P1 写的值。
    assert_eq!(k.read_word(P2, 0x9000).unwrap(), 7);

    // 两个进程的线程通过各自地址等待同一底层位置。
    let wa = k.wait(T1, P1, &[(0x1000, 7)], None).unwrap();
    let wb = k.wait(T2, P2, &[(0x9000, 7)], None).unwrap();
    // 两个进程解析出同一规范位置。
    assert_eq!(k.resolve(P1, 0x1000), k.resolve(P2, 0x9000));
    let key = k.resolve(P1, 0x1000).unwrap();
    assert_eq!(k.queue_snapshot(key), vec![wa, wb]);

    // 从 P2 的地址发起唤醒，按等待顺序唤醒 P1 的线程。
    let woken = k.wake(P2, 0x9000, 1);
    assert_eq!(woken, vec![wa]);
    assert_eq!(k.outcome(wa), Some(WaitOutcome::Woken));
    assert_eq!(k.queue_snapshot(key), vec![wb]);
}

#[test]
fn object_generation_distinguishes_reused_id() {
    let mut k = Kernel::new();
    let obj = k.create_object();
    k.map(P1, 0x1000, obj, 0);
    k.write_word(P1, 0x1000, 0).unwrap();

    // 等待者登记在旧世代上，带绝对截止。
    let old_waiter = k.wait(T1, P1, &[(0x1000, 0)], Some(100)).unwrap();
    let old_key = k.resolve(P1, 0x1000).unwrap();

    // 解除映射并销毁对象；随后复用同一 id（世代 +1）并重新映射同一地址。
    assert!(k.unmap(P1, 0x1000));
    assert!(k.destroy_object(obj));
    let reused = k.create_object();
    assert_eq!(reused, obj);
    k.map(P1, 0x1000, reused, 0);
    k.write_word(P1, 0x1000, 0).unwrap();

    let new_key = k.resolve(P1, 0x1000).unwrap();
    assert_ne!(old_key, new_key, "世代必须区分复用前后的位置");
    assert_eq!(old_key.object, new_key.object);
    assert!(new_key.generation > old_key.generation);

    // 新世代位置上的唤醒触及不到旧世代的等待者。
    let woken = k.wake(P1, 0x1000, 1);
    assert_eq!(woken, Vec::<u64>::new());
    assert!(k.is_pending(old_waiter));
    assert_eq!(k.queue_snapshot(old_key), vec![old_waiter]);
    assert_eq!(k.queue_snapshot(new_key), Vec::<u64>::new());

    // 旧等待者只能由超时（或取消）终结。
    let expired = k.advance_time(100);
    assert_eq!(expired, vec![old_waiter]);
    assert_eq!(k.outcome(old_waiter), Some(WaitOutcome::TimedOut));
    assert_eq!(k.queue_snapshot(old_key), Vec::<u64>::new());
}

// ---------- 一个线程同时等两个位置 ----------

#[test]
fn one_thread_waits_on_two_locations() {
    let mut k = Kernel::new();
    let o1 = k.create_object();
    let o2 = k.create_object();
    k.map(P1, 0x1000, o1, 0);
    k.map(P1, 0x2000, o2, 0);
    k.write_word(P1, 0x1000, 5).unwrap();
    k.write_word(P1, 0x2000, 9).unwrap();
    let k1 = k.resolve(P1, 0x1000).unwrap();
    let k2 = k.resolve(P1, 0x2000).unwrap();

    // 全部比较成功才原子登记：两个队列都出现该等待者。
    let w = k.wait(T1, P1, &[(0x1000, 5), (0x2000, 9)], None).unwrap();
    assert_eq!(k.queue_snapshot(k1), vec![w]);
    assert_eq!(k.queue_snapshot(k2), vec![w]);
    assert_eq!(k.registrations(w), vec![k1, k2]);

    // 在任一登记处完成后，其他登记被同时解除。
    let woken = k.wake(P1, 0x2000, 1);
    assert_eq!(woken, vec![w]);
    assert_eq!(k.outcome(w), Some(WaitOutcome::Woken));
    assert_eq!(k.queue_snapshot(k1), Vec::<u64>::new());
    assert_eq!(k.queue_snapshot(k2), Vec::<u64>::new());
    // 另一位置上的后续唤醒返回空。
    assert_eq!(k.wake(P1, 0x1000, 1), Vec::<u64>::new());
}

#[test]
fn multi_location_wait_is_atomic_on_mismatch() {
    let mut k = Kernel::new();
    let o1 = k.create_object();
    let o2 = k.create_object();
    k.map(P1, 0x1000, o1, 0);
    k.map(P1, 0x2000, o2, 0);
    k.write_word(P1, 0x1000, 5).unwrap();
    k.write_word(P1, 0x2000, 1).unwrap();
    let k1 = k.resolve(P1, 0x1000).unwrap();
    let k2 = k.resolve(P1, 0x2000).unwrap();

    // 第一个位置匹配、第二个不匹配：整体失败，任何队列都不登记。
    let err = k.wait(T1, P1, &[(0x1000, 5), (0x2000, 9)], None).unwrap_err();
    assert_eq!(
        err,
        WaitError::ValueMismatch { addr: 0x2000, expected: 9, actual: 1 }
    );
    assert_eq!(k.queue_snapshot(k1), Vec::<u64>::new());
    assert_eq!(k.queue_snapshot(k2), Vec::<u64>::new());
}

// ---------- 移交（requeue） ----------

#[test]
fn requeue_checks_source_value_first() {
    let mut k = Kernel::new();
    let o1 = k.create_object();
    let o2 = k.create_object();
    k.map(P1, 0x1000, o1, 0);
    k.map(P1, 0x2000, o2, 0);
    k.write_word(P1, 0x1000, 42).unwrap();
    let src = k.resolve(P1, 0x1000).unwrap();
    let dst = k.resolve(P1, 0x2000).unwrap();

    let w = k.wait(T1, P1, &[(0x1000, 42)], None).unwrap();

    // 源值不符：报错且源、目标队列均不变。
    let err = k.requeue(P1, 0x1000, 41, 1, 1, 0x2000).unwrap_err();
    assert_eq!(err, RequeueError::SourceMismatch { expected: 41, actual: 42 });
    assert_eq!(k.queue_snapshot(src), vec![w]);
    assert_eq!(k.queue_snapshot(dst), Vec::<u64>::new());
    assert!(k.is_pending(w));
}

#[test]
fn requeue_wakes_then_moves_to_target_tail() {
    let mut k = Kernel::new();
    let o1 = k.create_object();
    let o2 = k.create_object();
    k.map(P1, 0x1000, o1, 0);
    k.map(P1, 0x2000, o2, 0);
    k.write_word(P1, 0x1000, 0).unwrap();
    k.write_word(P1, 0x2000, 0).unwrap();
    let src = k.resolve(P1, 0x1000).unwrap();
    let dst = k.resolve(P1, 0x2000).unwrap();

    // 源队列 5 个等待者，目标队列已有 1 个。
    let w: Vec<u64> = (0..5)
        .map(|i| k.wait(100 + i, P1, &[(0x1000, 0)], None).unwrap())
        .collect();
    let d = k.wait(200, P1, &[(0x2000, 0)], None).unwrap();

    // 唤醒前 2 个，随后 2 个移交目标队尾，第 5 个留在源队列。
    let report = k.requeue(P1, 0x1000, 0, 2, 2, 0x2000).unwrap();
    assert_eq!(report.woken, vec![w[0], w[1]]);
    assert_eq!(report.moved, vec![w[2], w[3]]);
    assert_eq!(k.outcome(w[0]), Some(WaitOutcome::Woken));
    assert_eq!(k.outcome(w[1]), Some(WaitOutcome::Woken));

    // 移交者排在目标原有等待者之后（队尾），保持相对顺序。
    assert_eq!(k.queue_snapshot(dst), vec![d, w[2], w[3]]);
    assert_eq!(k.queue_snapshot(src), vec![w[4]]);
    // 移交者的登记位置已更新为目标。
    assert_eq!(k.registrations(w[2]), vec![dst]);

    // 在目标上唤醒，命中移交过来的等待者。
    let woken = k.wake(P1, 0x2000, 2);
    assert_eq!(woken, vec![d, w[2]]);
}

#[test]
fn requeue_does_not_duplicate_waiter_already_on_target() {
    let mut k = Kernel::new();
    let o1 = k.create_object();
    let o2 = k.create_object();
    k.map(P1, 0x1000, o1, 0);
    k.map(P1, 0x2000, o2, 0);
    k.write_word(P1, 0x1000, 0).unwrap();
    k.write_word(P1, 0x2000, 0).unwrap();
    let src = k.resolve(P1, 0x1000).unwrap();
    let dst = k.resolve(P1, 0x2000).unwrap();

    // both 同时登记在源和目标；plain 只在源。
    let both = k.wait(T1, P1, &[(0x1000, 0), (0x2000, 0)], None).unwrap();
    let plain = k.wait(T2, P1, &[(0x1000, 0)], None).unwrap();
    assert_eq!(k.queue_snapshot(src), vec![both, plain]);
    assert_eq!(k.queue_snapshot(dst), vec![both]);

    // 移交源队列全部 2 项到目标。
    let report = k.requeue(P1, 0x1000, 0, 0, 2, 0x2000).unwrap();
    // both 已在目标登记：不重复排队，仅解除其源登记；plain 正常移动。
    assert_eq!(report.moved, vec![plain]);
    assert_eq!(k.queue_snapshot(src), Vec::<u64>::new());
    assert_eq!(
        k.queue_snapshot(dst),
        vec![both, plain],
        "同一等待者在目标队列中只能出现一次"
    );
    assert_eq!(k.registrations(both), vec![dst]);
    assert_eq!(k.registrations(plain), vec![dst]);

    // 在目标唤醒一次只完成 both 一次。
    let woken = k.wake(P1, 0x2000, 1);
    assert_eq!(woken, vec![both]);
    assert_eq!(k.outcome(both), Some(WaitOutcome::Woken));
    assert_eq!(k.queue_snapshot(dst), vec![plain]);
}

#[test]
fn requeue_preserves_absolute_deadline() {
    let mut k = Kernel::new();
    let o1 = k.create_object();
    let o2 = k.create_object();
    k.map(P1, 0x1000, o1, 0);
    k.map(P1, 0x2000, o2, 0);
    k.write_word(P1, 0x1000, 0).unwrap();
    k.write_word(P1, 0x2000, 0).unwrap();

    // 绝对截止 50，移交后仍然生效。
    let w = k.wait(T1, P1, &[(0x1000, 0)], Some(50)).unwrap();
    let report = k.requeue(P1, 0x1000, 0, 0, 1, 0x2000).unwrap();
    assert_eq!(report.moved, vec![w]);

    k.advance_time(49);
    assert!(k.is_pending(w));
    let expired = k.advance_time(50);
    assert_eq!(expired, vec![w]);
    assert_eq!(k.outcome(w), Some(WaitOutcome::TimedOut));
}

// ---------- 取消 / 超时 / 唤醒同时发生的输入事件序 ----------

#[test]
fn simultaneous_events_decided_by_input_order_cancel_vs_wake() {
    // 先取消后唤醒：取消生效，唤醒落空。
    let (mut k, _) = kernel_with_word(P1, 0x1000, 0);
    let w = k.wait(T1, P1, &[(0x1000, 0)], None).unwrap();
    assert!(k.cancel(w));
    assert_eq!(k.outcome(w), Some(WaitOutcome::Cancelled));
    assert_eq!(k.wake(P1, 0x1000, 1), Vec::<u64>::new());
    // 重复取消返回 false，结果唯一。
    assert!(!k.cancel(w));

    // 先唤醒后取消：唤醒生效，取消落空。
    let (mut k, _) = kernel_with_word(P1, 0x1000, 0);
    let w = k.wait(T1, P1, &[(0x1000, 0)], None).unwrap();
    assert_eq!(k.wake(P1, 0x1000, 1), vec![w]);
    assert!(!k.cancel(w));
    assert_eq!(k.outcome(w), Some(WaitOutcome::Woken));
}

#[test]
fn simultaneous_events_decided_by_input_order_timeout_vs_wake() {
    // 先推进时间到截止：超时生效，之后的唤醒落空。
    let (mut k, _) = kernel_with_word(P1, 0x1000, 0);
    let w = k.wait(T1, P1, &[(0x1000, 0)], Some(30)).unwrap();
    let expired = k.advance_time(30);
    assert_eq!(expired, vec![w]);
    assert_eq!(k.wake(P1, 0x1000, 1), Vec::<u64>::new());
    assert_eq!(k.outcome(w), Some(WaitOutcome::TimedOut));

    // 同一逻辑时刻先唤醒：唤醒生效，之后推进时间不再超时。
    let (mut k, _) = kernel_with_word(P1, 0x1000, 0);
    let w = k.wait(T1, P1, &[(0x1000, 0)], Some(30)).unwrap();
    assert_eq!(k.wake(P1, 0x1000, 1), vec![w]);
    let expired = k.advance_time(30);
    assert_eq!(expired, Vec::<u64>::new());
    assert_eq!(k.outcome(w), Some(WaitOutcome::Woken));
}

#[test]
fn timeout_order_is_deterministic_for_equal_deadlines() {
    let (mut k, _) = kernel_with_word(P1, 0x1000, 0);
    // 三个等待者同一绝对截止：按登记顺序超时。
    let w1 = k.wait(T1, P1, &[(0x1000, 0)], Some(10)).unwrap();
    let w2 = k.wait(T2, P1, &[(0x1000, 0)], Some(10)).unwrap();
    let w3 = k.wait(T3, P1, &[(0x1000, 0)], Some(5)).unwrap();
    // 截止更早者先超时；同截止按登记序。
    let expired = k.advance_time(10);
    assert_eq!(expired, vec![w3, w1, w2]);
}
