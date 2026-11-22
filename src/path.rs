//! 极简路径解析。
//!
//! 路径以 `/` 分隔组件：
//! - 空组件与 `.` 组件被忽略；
//! - `..` 不做父目录跳转，直接拒绝（本模型没有“..”目录项语义，
//!   允许它会让目录交换后的相对路径语义变得模糊）；
//! - 绝对路径与无前导 `/` 的路径都按“相对于根”处理。

use crate::FsError;

/// 将路径切分为有序的组件名，例如 `/a/./b/c` -> `["a", "b", "c"]`。
pub(crate) fn components(path: &str) -> Result<Vec<String>, FsError> {
    let mut out = Vec::new();
    for part in path.split('/') {
        match part {
            "" | "." => {}
            ".." => return Err(FsError::InvalidPath(path.to_string())),
            other => out.push(other.to_string()),
        }
    }
    Ok(out)
}

/// 取父目录组件与最后一个组件名。
///
/// `"/a/b"` -> `(vec!["a"], "b")`；根路径 `"/"` 没有最后组件，返回 `None`。
pub(crate) fn split_parent_name(path: &str) -> Result<Option<(Vec<String>, String)>, FsError> {
    let comps = components(path)?;
    match comps.split_last() {
        Some((name, prefix)) => Ok(Some((prefix.to_vec(), name.clone()))),
        None => Ok(None),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn splits_components() {
        assert_eq!(components("/a/b/c").unwrap(), vec!["a", "b", "c"]);
        assert_eq!(components("a/./b/").unwrap(), vec!["a", "b"]);
        assert!(components("/").unwrap().is_empty());
        assert!(components("..").is_err());
        assert!(components("a/../b").is_err());
    }

    #[test]
    fn splits_parent() {
        let Some((prefix, name)) = split_parent_name("/a/b").unwrap() else {
            panic!("/a/b 应当能拆出父目录与名称");
        };
        assert_eq!(prefix, vec!["a".to_string()]);
        assert_eq!(name, "b");
        assert!(split_parent_name("/").unwrap().is_none());
        assert!(split_parent_name("a").unwrap().is_some());
    }
}
