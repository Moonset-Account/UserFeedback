//! 正常路径：硬链接计数、打开句柄固定 inode、链接删光后仍可读、
//! 最终关闭句柄触发真实回收。

use namespace_fs::{FileSystem, NodeKind};

#[test]
fn hardlinks_share_inode_and_counts() {
    let mut fs = FileSystem::fresh();
    fs.create_file("/f").unwrap();
    fs.write_file("/f", b"hello").unwrap();
    fs.link("/f", "/g").unwrap();
    fs.link("/f", "/h").unwrap();

    let f = fs.stat_path("/f").unwrap();
    let g = fs.stat_path("/g").unwrap();
    let h = fs.stat_path("/h").unwrap();
    assert_eq!(f.id, g.id);
    assert_eq!(g.id, h.id);
    assert_eq!(f.nlink, 3);
    assert_eq!(f.kind, NodeKind::File);
    fs.check_invariants().unwrap();

    // 任一链接写入，其它链接读到同一内容。
    fs.write_file("/g", b"world").unwrap();
    assert_eq!(fs.read_path("/f").unwrap(), b"world");
    assert_eq!(fs.read_path("/h").unwrap(), b"world");
}

#[test]
fn open_handle_survives_unlink_of_all_links() {
    let mut fs = FileSystem::fresh();
    fs.create_file("/f").unwrap();
    fs.write_file("/f", b"data-1").unwrap();
    fs.link("/f", "/g").unwrap();
    let ino = fs.stat_path("/f").unwrap().id;

    let h1 = fs.open("/f").unwrap();
    let h2 = fs.open("/g").unwrap();
    assert_eq!(h1.inode, ino);
    assert_eq!(h2.inode, ino);
    assert_eq!(fs.inode_info(ino).open_count, 2);

    // 删光全部目录项：路径解析失败，但句柄仍然有效。
    fs.unlink("/f").unwrap();
    assert_eq!(fs.inode_info(ino).nlink, 1);
    fs.unlink("/g").unwrap();
    let info = fs.inode_info(ino);
    assert_eq!(info.nlink, 0);
    assert!(info.allocated, "仍有打开句柄，inode 不能被释放");
    assert!(fs.stat_path("/f").is_err());
    assert_eq!(fs.read_handle(&h1).unwrap(), b"data-1");
    assert_eq!(fs.read_handle(&h2).unwrap(), b"data-1");
    fs.check_invariants().unwrap();

    // 关闭一个句柄还不够。
    fs.close(h1).unwrap();
    assert!(fs.inode_exists(ino), "还有一个句柄，仍不能释放");

    // 最后一个句柄关闭：inode 真实回收。
    fs.close(h2).unwrap();
    assert!(!fs.inode_exists(ino));
    let info = fs.inode_info(ino);
    assert!(!info.allocated);
    fs.check_invariants().unwrap();
}

#[test]
fn freed_inode_number_is_reused_lifo() {
    let mut fs = FileSystem::fresh();
    fs.create_file("/a").unwrap();
    let ino_a = fs.stat_path("/a").unwrap().id;
    fs.create_file("/b").unwrap();
    fs.unlink("/a").unwrap();
    assert!(!fs.inode_exists(ino_a));

    fs.create_file("/c").unwrap();
    assert_eq!(
        fs.stat_path("/c").unwrap().id,
        ino_a,
        "腾出的 inode 号应当被新文件复用"
    );
    fs.check_invariants().unwrap();

    // 日志里能看到完整的 InodeFree 与后续 InodeAlloc 复用记录。
    let records = fs.committed_records().unwrap();
    assert!(records.iter().any(|r| matches!(r, namespace_fs::Record::InodeFree(id) if id == &ino_a)));
    assert!(records.iter().any(
        |r| matches!(r, namespace_fs::Record::InodeAlloc(id, namespace_fs::NodeKind::File, 1) if id == &ino_a)
    ));
}

#[test]
fn bad_handle_and_double_close_are_rejected() {
    let mut fs = FileSystem::fresh();
    fs.create_file("/f").unwrap();
    fs.write_file("/f", b"x").unwrap();
    let h = fs.open("/f").unwrap();
    fs.close(h.clone()).unwrap();
    assert_eq!(
        fs.close(h).unwrap_err(),
        namespace_fs::FsError::BadHandle(1)
    );
}

#[test]
fn close_driven_reclamation_is_journaled_and_survives_reopen() {
    let mut fs = FileSystem::fresh();
    fs.create_file("/f").unwrap();
    fs.write_file("/f", b"ephemeral").unwrap();
    let ino = fs.stat_path("/f").unwrap().id;
    let handle = fs.open("/f").unwrap();

    // 删光链接后 inode 仍被句柄持有，日志里此时只有 nlink 递减、没有 InodeFree。
    fs.unlink("/f").unwrap();
    assert!(fs.inode_exists(ino));
    assert!(fs
        .committed_records()
        .unwrap()
        .iter()
        .all(|r| !matches!(r, namespace_fs::Record::InodeFree(id) if id == &ino)));

    // 关闭最后句柄才真实释放，且 InodeFree 进入已提交日志。
    fs.close(handle).unwrap();
    assert!(!fs.inode_exists(ino));
    assert!(fs.committed_records().unwrap().iter().any(
        |r| matches!(r, namespace_fs::Record::InodeFree(id) if id == &ino)
    ));

    // 重放日志后 inode 仍然是已回收状态，号进入空闲池并被新文件复用。
    let mut reopened = FileSystem::reopen(fs.into_disk()).unwrap();
    reopened.check_invariants().unwrap();
    assert!(!reopened.inode_exists(ino));
    reopened.create_file("/g").unwrap();
    assert_eq!(reopened.stat_path("/g").unwrap().id, ino);
}
