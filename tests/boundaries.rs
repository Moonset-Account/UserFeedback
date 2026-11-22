//! 边界与错误路径：路径语法、目录规则、空目录删除、名称冲突等。

use namespace_fs::{FileSystem, FsError, NodeKind};

#[test]
fn path_syntax_and_component_rules() {
    let mut fs = FileSystem::fresh();
    // 空组件与 "." 被忽略，多种写法等价。
    let id1 = fs.mkdir("/d").unwrap();
    let id2 = fs.mkdir("d").unwrap_err(); // 已存在
    assert_eq!(id2, FsError::AlreadyExists("d".into()));
    assert_eq!(fs.mkdir("/./d/./x").unwrap(), fs.stat_path("/d/x").unwrap().id);
    assert_eq!(id1, fs.stat_path("d").unwrap().id);

    // ".." 被直接拒绝。
    assert!(matches!(
        fs.mkdir("/d/../y").unwrap_err(),
        FsError::InvalidPath(_)
    ));

    // 根路径没有最后组件，创建类操作返回 RootReserved。
    assert_eq!(fs.create_file("/").unwrap_err(), FsError::RootReserved);
}

#[test]
fn directory_operation_restrictions() {
    let mut fs = FileSystem::fresh();
    fs.mkdir("/d").unwrap();
    fs.write_file("/d/f", b"x").unwrap();

    // 不能对目录建立硬链接、不能打开目录当文件读。
    assert_eq!(
        fs.link("/d", "/dlink").unwrap_err(),
        FsError::IsADirectory("/d".to_string())
    );
    assert_eq!(
        fs.open("/d").unwrap_err(),
        FsError::IsADirectory("/d".to_string())
    );
    assert_eq!(
        fs.read_path("/d").unwrap_err(),
        FsError::IsADirectory("/d".to_string())
    );

    // 不能 unlink 目录，也不能 rmdir 普通文件。
    assert_eq!(
        fs.unlink("/d").unwrap_err(),
        FsError::IsADirectory("/d".to_string())
    );
    assert_eq!(
        fs.rmdir("/d/f").unwrap_err(),
        FsError::NotADirectoryTarget("/d/f".to_string())
    );

    // 非空目录不能 rmdir。
    assert_eq!(
        fs.rmdir("/d").unwrap_err(),
        FsError::DirectoryNotEmpty("/d".to_string())
    );

    // 路径中间组件是普通文件时报 NotADirectory。
    assert_eq!(
        fs.mkdir("/d/f/sub").unwrap_err(),
        FsError::NotADirectory("/d/f/sub".to_string())
    );

    // 前面的失败操作都不得改变命名空间。
    assert!(fs.stat_path("/d/f").is_ok());
    fs.check_invariants().unwrap();
}

#[test]
fn empty_dir_rmdir_and_name_conflicts() {
    let mut fs = FileSystem::fresh();
    fs.mkdir("/d").unwrap();
    let ino = fs.stat_path("/d").unwrap().id;
    fs.rmdir("/d").unwrap();
    assert!(!fs.inode_exists(ino));
    assert_eq!(fs.list_dir("/").unwrap(), Vec::new());

    fs.create_file("/f").unwrap();
    assert_eq!(
        fs.create_file("/f").unwrap_err(),
        FsError::AlreadyExists("/f".to_string())
    );
    assert_eq!(
        fs.mkdir("/f").unwrap_err(),
        FsError::AlreadyExists("/f".to_string())
    );
    assert_eq!(
        fs.link("/f", "/f").unwrap_err(),
        FsError::AlreadyExists("/f".to_string())
    );
    fs.check_invariants().unwrap();
}

#[test]
fn nlink_reflects_multiple_links_and_partial_removal() {
    let mut fs = FileSystem::fresh();
    fs.create_file("/f").unwrap();
    fs.write_file("/f", b"z").unwrap();
    for n in 1..=4 {
        fs.link("/f", &format!("/l{n}")).unwrap();
    }
    let ino = fs.stat_path("/f").unwrap().id;
    assert_eq!(fs.inode_info(ino).nlink, 5);

    for removed in 1..=4 {
        fs.unlink(&format!("/l{removed}")).unwrap();
        assert_eq!(fs.inode_info(ino).nlink, 5 - removed);
        assert!(fs.inode_exists(ino));
    }
    fs.unlink("/f").unwrap();
    assert!(!fs.inode_exists(ino), "无句柄且 nlink 归零应立即回收");
    fs.check_invariants().unwrap();
}

#[test]
fn listed_entries_are_sorted_and_report_kinds() {
    let mut fs = FileSystem::fresh();
    fs.mkdir("/z").unwrap();
    fs.mkdir("/a").unwrap();
    fs.create_file("/m").unwrap();
    let names: Vec<String> = fs.list_dir("/").unwrap().into_iter().map(|(n, _)| n).collect();
    assert_eq!(names, vec!["a".to_string(), "m".to_string(), "z".to_string()]);
    assert_eq!(fs.stat_path("/a").unwrap().kind, NodeKind::Directory);
    assert_eq!(fs.stat_path("/m").unwrap().kind, NodeKind::File);
}
