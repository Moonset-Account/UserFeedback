//! 原子目录项交换：跨目录交换、句柄与路径分别读到不同 inode、
//! 目录相对路径随目录移动、成环拒绝与边界条件。

use namespace_fs::{FileSystem, FsError, NodeKind};

#[test]
fn swap_across_directories_handle_vs_path_diverge() {
    let mut fs = FileSystem::fresh();
    fs.mkdir("/d1").unwrap();
    fs.mkdir("/d2").unwrap();
    fs.create_file("/d1/f").unwrap();
    fs.write_file("/d1/f", b"one").unwrap();
    fs.create_file("/d2/f").unwrap();
    fs.write_file("/d2/f", b"two").unwrap();

    let ino_one = fs.stat_path("/d1/f").unwrap().id;
    let ino_two = fs.stat_path("/d2/f").unwrap().id;
    assert_ne!(ino_one, ino_two);

    // 事先打开 /d1/f，句柄固定到 inode_one。
    let handle = fs.open("/d1/f").unwrap();
    assert_eq!(handle.inode, ino_one);
    assert_eq!(fs.read_handle(&handle).unwrap(), b"one");

    // 两个位于不同目录的目录项原子交换，只改目录项引用的 inode。
    fs.swap("/d1/f", "/d2/f").unwrap();
    fs.check_invariants().unwrap();

    // 旧句柄继续读原 inode；路径解析跟随目录项，读到另一个 inode。
    assert_eq!(fs.read_handle(&handle).unwrap(), b"one");
    assert_eq!(fs.read_path("/d1/f").unwrap(), b"two");
    assert_eq!(fs.read_path("/d2/f").unwrap(), b"one");
    assert_eq!(fs.stat_path("/d1/f").unwrap().id, ino_two);
    assert_eq!(fs.stat_path("/d2/f").unwrap().id, ino_one);

    // 链接计数不变（目录项数量没变，只是换了挂载位置）。
    assert_eq!(fs.inode_info(ino_one).nlink, 1);
    assert_eq!(fs.inode_info(ino_two).nlink, 1);
    fs.close(handle).unwrap();
    fs.check_invariants().unwrap();
}

#[test]
fn relative_paths_inside_swapped_directories_move_with_them() {
    let mut fs = FileSystem::fresh();
    fs.mkdir("/a").unwrap();
    fs.mkdir("/b").unwrap();
    fs.mkdir("/a/inner").unwrap();
    fs.create_file("/a/inner/note").unwrap();
    fs.write_file("/a/inner/note", b"moved").unwrap();
    fs.mkdir("/b/slot").unwrap();

    let inner_ino = fs.stat_path("/a/inner").unwrap().id;
    let slot_ino = fs.stat_path("/b/slot").unwrap().id;
    fs.swap("/a/inner", "/b/slot").unwrap();
    fs.check_invariants().unwrap();

    // 整个子树随目录移动：相对路径 inner/note 的内容在新位置可访问；
    // 原槽位 /a/inner 现在指向交换过来的空目录 slot，而不再是 inner。
    assert_eq!(fs.read_path("/b/slot/note").unwrap(), b"moved");
    assert_eq!(fs.stat_path("/a/inner").unwrap().id, slot_ino);
    assert_eq!(fs.stat_path("/b/slot").unwrap().id, inner_ino);
    assert!(fs.read_path("/a/inner/note").is_err(), "旧位置下的子文件已随目录迁走");

    // 目录类型保持不变，目录 nlink 固定为 1。
    assert_eq!(fs.stat_path("/b/slot").unwrap().kind, NodeKind::Directory);
    assert_eq!(fs.stat_path("/b/slot").unwrap().nlink, 1);
}

#[test]
fn swap_that_would_cycle_is_rejected() {
    let mut fs = FileSystem::fresh();
    fs.mkdir("/a").unwrap();
    fs.mkdir("/a/b").unwrap();
    fs.mkdir("/c").unwrap();

    // 把祖先目录 /a 换到它自己的后代 /a/b 名下会成环，必须拒绝且状态不变。
    let err = fs.swap("/a", "/a/b").unwrap_err();
    assert!(matches!(err, FsError::WouldCycle { .. }), "实际错误: {err}");
    fs.check_invariants().unwrap();
    assert!(fs.stat_path("/a").is_ok());
    assert!(fs.stat_path("/a/b").is_ok());

    // 反向参数同样拒绝。
    assert!(matches!(
        fs.swap("/a/b", "/a").unwrap_err(),
        FsError::WouldCycle { .. }
    ));

    // 跨目录交换 /a/b 与 /c 合法；交换后原 c 位于 /a/b，原 b 位于 /c。
    fs.swap("/a/b", "/c").unwrap();
    fs.check_invariants().unwrap();
    // 再把 /a 换到其后代位置依然被拒。
    assert!(matches!(
        fs.swap("/a", "/a/b").unwrap_err(),
        FsError::WouldCycle { .. }
    ));
}

#[test]
fn swap_boundaries_missing_entries_and_file_dir_mix() {
    let mut fs = FileSystem::fresh();
    fs.mkdir("/d").unwrap();
    fs.create_file("/d/f").unwrap();
    fs.write_file("/d/f", b"x").unwrap();
    fs.create_file("/file").unwrap();

    // 任一目录项不存在都报错；深层路径在第一个缺失组件处即返回 NotFound。
    assert_eq!(
        fs.swap("/d/f", "/d/missing").unwrap_err(),
        FsError::NotFound("/d/missing".to_string())
    );
    assert_eq!(
        fs.swap("/nope/x", "/d/f").unwrap_err(),
        FsError::NotFound("/nope/x".to_string())
    );

    // 文件与目录混排交换是允许的（只有目录-目录才可能成环）。
    let dir_ino = fs.stat_path("/d").unwrap().id;
    let file_ino = fs.stat_path("/file").unwrap().id;
    fs.swap("/d", "/file").unwrap();
    fs.check_invariants().unwrap();
    assert_eq!(fs.stat_path("/d").unwrap().id, file_ino);
    assert_eq!(fs.stat_path("/file").unwrap().id, dir_ino);
    // 目录移动后，其内部相对路径仍然可用。
    assert_eq!(fs.read_path("/file/f").unwrap(), b"x");

    // 自己和自己交换是无操作，不报错。
    fs.swap("/d", "/d").unwrap();
    fs.check_invariants().unwrap();

    // 根目录不能参与交换。
    assert_eq!(fs.swap("/", "/d").unwrap_err(), FsError::RootReserved);
}
