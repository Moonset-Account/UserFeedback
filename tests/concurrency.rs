//! 并发与交错场景：多线程混合压力测试 + 确定性的操作交错序列。

use std::sync::{Arc, Mutex};

use namespace_fs::{CrashPoint, FileSystem};

/// 多线程混合操作：每个线程反复创建、硬链接、交换、删除文件，
/// 全程通过外层 Mutex 串行化提交；结束后校验不变量，并经日志重开再校验一次。
#[test]
fn multithreaded_mixed_workload_preserves_invariants() {
    let fs = Arc::new(Mutex::new(FileSystem::fresh()));
    let mut joins = Vec::new();

    for thread_id in 0..4 {
        let fs = Arc::clone(&fs);
        joins.push(std::thread::spawn(move || {
            for i in 0..75 {
                let tag = format!("t{thread_id}_{i}");
                let mut guard = fs.lock().unwrap();
                let file_path = format!("/{tag}");
                let link_path = format!("/{tag}_link");
                let swap_path = format!("/swap_{}", i % 8);

                guard.create_file(&file_path).unwrap();
                guard.write_file(&file_path, tag.as_bytes()).unwrap();

                // 一半迭代建立硬链接，再删掉原链接，验证计数与延迟释放。
                if i % 2 == 0 {
                    guard.link(&file_path, &link_path).unwrap();
                    let ino = guard.stat_path(&link_path).unwrap().id;
                    assert_eq!(guard.inode_info(ino).nlink, 2);
                    guard.unlink(&file_path).unwrap();
                    assert_eq!(guard.inode_info(ino).nlink, 1);
                }

                // 与公共交换槽位交换（先确保槽位存在）。
                if guard.stat_path(&swap_path).is_err() {
                    guard.create_file(&swap_path).unwrap();
                }
                let victim = if guard.stat_path(&link_path).is_ok() {
                    link_path.clone()
                } else {
                    file_path.clone()
                };
                guard.swap(&victim, &swap_path).unwrap();
                guard.check_invariants().unwrap();
            }
        }));
    }

    for j in joins {
        j.join().unwrap();
    }

    let fs = Arc::try_unwrap(fs).map_err(|_| "仍有线程持有 fs").unwrap();
    let fs = fs.into_inner().unwrap();
    fs.check_invariants().unwrap();

    // 从日志重开，命名空间必须仍然一致，且所有存活文件内容可按路径读出。
    let disk = fs.into_disk();
    let recovered = FileSystem::reopen(disk).unwrap();
    recovered.check_invariants().unwrap();
    for (name, ino) in recovered.list_dir("/").unwrap() {
        let info = recovered.inode_info(ino);
        assert!(info.allocated, "目录项 {name} 指向已回收 inode");
        if info.kind == namespace_fs::NodeKind::File {
            let _ = recovered.read_path(&format!("/{name}")).unwrap();
        }
    }
}

/// 确定性交错：打开句柄 → 交换 → 用新链接顶替路径 → 删原链接 → 关句柄，
/// 逐步核对“路径读值”和“旧句柄读值”分属不同 inode。
#[test]
fn deterministic_interleaving_swap_link_unlink_close() {
    let mut fs = FileSystem::fresh();
    fs.mkdir("/a").unwrap();
    fs.mkdir("/b").unwrap();
    fs.write_file("/a/f", b"original").unwrap();
    fs.write_file("/b/f", b"replacement").unwrap();

    // 1) 打开 /a/f，句柄绑定原 inode。
    let original_ino = fs.stat_path("/a/f").unwrap().id;
    let replacement_ino = fs.stat_path("/b/f").unwrap().id;
    let handle = fs.open("/a/f").unwrap();

    // 2) 跨目录交换。
    fs.swap("/a/f", "/b/f").unwrap();
    assert_eq!(fs.read_handle(&handle).unwrap(), b"original");
    assert_eq!(fs.read_path("/a/f").unwrap(), b"replacement");

    // 3) 给原 inode 增加一个硬链接（现在它挂在 /b/f）。
    fs.link("/b/f", "/a/backup").unwrap();
    assert_eq!(fs.inode_info(original_ino).nlink, 2);
    assert_eq!(fs.read_path("/a/backup").unwrap(), b"original");

    // 4) 删掉 /b/f 这一个链接：原 inode 仍有 /a/backup 与打开句柄。
    fs.unlink("/b/f").unwrap();
    assert_eq!(fs.inode_info(original_ino).nlink, 1);
    assert!(fs.inode_exists(original_ino));
    assert_eq!(fs.read_handle(&handle).unwrap(), b"original");

    // 5) 再删掉最后一个目录项：仅靠句柄存活。
    fs.unlink("/a/backup").unwrap();
    assert_eq!(fs.inode_info(original_ino).nlink, 0);
    assert!(fs.inode_exists(original_ino));
    assert_eq!(fs.read_handle(&handle).unwrap(), b"original");
    assert!(fs.stat_path("/a/backup").is_err());

    // 顶替路径始终指向 replacement inode。
    assert_eq!(fs.stat_path("/a/f").unwrap().id, replacement_ino);
    fs.check_invariants().unwrap();

    // 6) 关闭句柄：原 inode 真实回收，号被后续创建复用。
    fs.close(handle).unwrap();
    assert!(!fs.inode_exists(original_ino));
    fs.create_file("/c").unwrap();
    assert_eq!(fs.stat_path("/c").unwrap().id, original_ino);
    fs.check_invariants().unwrap();
}

/// 崩溃注入与正常操作交错：事务被断电中断后，句柄语义仍成立，
/// 重开得到一致命名空间。
#[test]
fn crash_interleaved_between_successful_operations() {
    let mut fs = FileSystem::fresh();
    fs.write_file("/f", b"v0").unwrap();
    let handle = fs.open("/f").unwrap();

    fs.arm_crash(CrashPoint::BeforeCommit);
    assert!(fs.write_file("/f", b"v1").is_err());

    // 断电后句柄仍可读旧值（当前句柄对象本身仍有效）。
    assert_eq!(fs.read_handle(&handle).unwrap(), b"v0");

    let mut recovered = FileSystem::reopen(fs.into_disk()).unwrap();
    recovered.check_invariants().unwrap();
    assert_eq!(recovered.read_path("/f").unwrap(), b"v0");

    // 易失句柄不跨重开；重开后重新打开并核对链接计数。
    let reopened = recovered.open("/f").unwrap();
    assert_eq!(recovered.read_handle(&reopened).unwrap(), b"v0");
    assert_eq!(recovered.inode_info(reopened.inode).nlink, 1);
}
