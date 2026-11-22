//! 故障恢复：提交标记前后的模拟断电、日志尾部撕裂、
//! 崩溃时未写释放记录的孤儿 inode 回收、以及重开后的完整一致性。

use namespace_fs::{CrashPoint, FileSystem, FsError, NodeKind, Record};

fn snapshot(fs: FileSystem) -> Vec<u8> {
    fs.into_disk()
}

#[test]
fn crash_before_commit_discards_whole_transaction() {
    let mut fs = FileSystem::fresh();
    fs.mkdir("/d").unwrap();
    fs.write_file("/d/f", b"keep").unwrap();
    let before_len = fs.disk_len();

    // 在“删除 + nlink 递减 + 可能的释放”这一事务提交前断电。
    fs.arm_crash(CrashPoint::BeforeCommit);
    let crash = fs.unlink("/d/f").unwrap_err();
    assert!(matches!(crash, FsError::CorruptJournal(_)));
    assert!(fs.disk_len() > before_len, "事务体应已写入介质");

    let disk = snapshot(fs);
    let recovered = FileSystem::reopen(disk).unwrap();
    recovered.check_invariants().unwrap();

    // 整个未提交事务被丢弃：文件、链接计数、内容全部保持断电前状态。
    let info = recovered.stat_path("/d/f").unwrap();
    assert_eq!(info.kind, NodeKind::File);
    assert_eq!(info.nlink, 1);
    assert!(info.allocated);
    assert_eq!(recovered.read_path("/d/f").unwrap(), b"keep");
}

#[test]
fn crash_after_commit_keeps_transaction() {
    let mut fs = FileSystem::fresh();
    fs.mkdir("/d").unwrap();
    fs.write_file("/d/f", b"v1").unwrap();

    fs.arm_crash(CrashPoint::AfterCommit);
    let crash = fs.write_file("/d/f", b"v2").unwrap_err();
    assert!(matches!(crash, FsError::CorruptJournal(_)));

    let recovered = FileSystem::reopen(snapshot(fs)).unwrap();
    recovered.check_invariants().unwrap();
    assert_eq!(recovered.read_path("/d/f").unwrap(), b"v2");
}

#[test]
fn torn_log_tail_is_ignored_on_reopen() {
    let mut fs = FileSystem::fresh();
    fs.mkdir("/d").unwrap();
    fs.write_file("/d/f", b"stable").unwrap();
    let good_len = fs.disk_len();

    // 再做一个完整事务，然后把介质截断到该事务中间，模拟部分写入。
    fs.write_file("/d/f", b"torn").unwrap();
    let mut disk = snapshot(fs);
    assert!(disk.len() > good_len + 3);
    disk.truncate(good_len + 3);

    let recovered = FileSystem::reopen(disk).unwrap();
    recovered.check_invariants().unwrap();
    assert_eq!(recovered.read_path("/d/f").unwrap(), b"stable");
}

#[test]
fn orphan_inode_with_zero_links_is_reclaimed_on_reopen() {
    // 构造“事务已提交 nlink=0 但 InodeFree 尚未单独成事务”的局面：
    // 通过在 open 状态下 unlink 到 nlink=0，再直接在重开路径验证回收。
    let mut fs = FileSystem::fresh();
    fs.write_file("/f", b"orphan").unwrap();
    let ino = fs.stat_path("/f").unwrap().id;
    let _handle = fs.open("/f").unwrap();
    fs.unlink("/f").unwrap(); // nlink=0，句柄仍持有，内存中不释放
    assert!(fs.inode_exists(ino));
    assert_eq!(fs.inode_info(ino).nlink, 0);

    // 重开后句柄是易失状态、全部消失；nlink=0 的 inode 必须被回收，
    // 且回收动作补写进日志。
    let disk = snapshot(fs);
    let recovered = FileSystem::reopen(disk).unwrap();
    recovered.check_invariants().unwrap();
    assert!(!recovered.inode_exists(ino));

    let records = recovered.committed_records().unwrap();
    assert!(records
        .iter()
        .any(|r| matches!(r, Record::InodeFree(id) if id == &ino)));

    // 再次重开仍然一致（补写的释放记录可重复回放）。
    let again = FileSystem::reopen(recovered.into_disk()).unwrap();
    again.check_invariants().unwrap();
    assert!(!again.inode_exists(ino));

    // 腾出的号可被新文件复用。
    let mut again = again;
    again.create_file("/new").unwrap();
    assert_eq!(again.stat_path("/new").unwrap().id, ino);
    again.check_invariants().unwrap();
}

#[test]
fn reopen_replays_swap_and_link_counts_exactly() {
    let mut fs = FileSystem::fresh();
    fs.mkdir("/d1").unwrap();
    fs.mkdir("/d2").unwrap();
    fs.write_file("/d1/f", b"one").unwrap();
    fs.write_file("/d2/f", b"two").unwrap();
    fs.link("/d1/f", "/d1/alias").unwrap();
    let ino_one = fs.stat_path("/d1/f").unwrap().id;
    fs.swap("/d1/f", "/d2/f").unwrap();
    let before = fs.disk_len();
    fs.check_invariants().unwrap();

    let recovered = FileSystem::reopen(snapshot(fs)).unwrap();
    recovered.check_invariants().unwrap();
    assert_eq!(recovered.disk_len(), before, "无孤儿时重开不应追加日志");

    // 交换结果完整保留。
    assert_eq!(recovered.read_path("/d1/f").unwrap(), b"two");
    assert_eq!(recovered.read_path("/d2/f").unwrap(), b"one");
    // 硬链接关系保留：alias 仍指向 inode_one，nlink=2。
    assert_eq!(recovered.stat_path("/d1/alias").unwrap().id, ino_one);
    assert_eq!(recovered.inode_info(ino_one).nlink, 2);
    assert_eq!(recovered.read_path("/d1/alias").unwrap(), b"one");

    // 日志中确实记录了交换与链接计数调整。
    let records = recovered.committed_records().unwrap();
    assert!(records.iter().any(|r| matches!(r, Record::EntrySwap(..))));
    assert!(records.iter().any(|r| matches!(r, Record::Nlink(id, d) if id == &ino_one && *d == 1)));
}

#[test]
fn reopen_after_rmdir_reclaims_directory_inode() {
    let mut fs = FileSystem::fresh();
    fs.mkdir("/tmp").unwrap();
    let tmp_ino = fs.stat_path("/tmp").unwrap().id;
    fs.rmdir("/tmp").unwrap();
    assert!(!fs.inode_exists(tmp_ino));

    let recovered = FileSystem::reopen(snapshot(fs)).unwrap();
    recovered.check_invariants().unwrap();
    assert!(!recovered.inode_exists(tmp_ino));
    assert_eq!(recovered.list_dir("/").unwrap(), Vec::new());
}
