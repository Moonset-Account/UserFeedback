//! 页式 B+ 树索引。
//!
//! 索引项为整数键 + 行号的二元组 [`Entry`]，按 `(key, row_id)` 全序排列，因此同一
//! 个键的多条记录（重复键）可以跨多个叶页存放。
//!
//! # 结构参数
//! - 叶页容量：2..=4 项（根页不受下限约束）
//! - 内部页孩子数：2..=4（根页不受下限约束）
//! - 内部页的每个分隔项始终等于其右侧孩子子树中的最小索引项；其 `key` 即
//!   右孩子子树的最小键。用完整二元组（而不是只用键）做分隔符，才能让
//!   按完整二元组的下降在重复键跨页时也精确命中目标叶页。
//! - 叶页之间通过 `prev` / `next` 指针组成双向叶链
//!
//! # 支持的操作
//! - 逐条插入（[`BPlusTree::insert`]），溢出时分裂并向上传播
//! - 按完整二元组删除（[`BPlusTree::delete`]），非根欠载时先向同父相邻兄弟借位，
//!   借不到才合并；根内部页只剩一个孩子时降低树高
//! - 正反向扫描左闭右开键值区间（[`BPlusTree::scan`] / [`BPlusTree::scan_rev`]）
//! - 只读页结构与叶链访问（[`BPlusTree::get_page`] / [`BPlusTree::leaf_chain`] 等）
//!
//! 页目前存放在进程内的哈希表中，以 [`PageId`] 寻址；所有算法都按“每次只通过页号
//! 取页”的分页方式组织，后续可替换为磁盘/缓冲池实现而不改上层逻辑。

use std::collections::HashMap;

/// 整数键类型。
pub type Key = i64;
/// 行号类型。
pub type RowId = u64;
/// 页号类型。
pub type PageId = u32;

/// 非根叶页允许的最小项数。
pub const MIN_LEAF_ENTRIES: usize = 2;
/// 叶页允许的最大项数，超出即分裂。
pub const MAX_LEAF_ENTRIES: usize = 4;
/// 非根内部页允许的最小孩子数。
pub const MIN_CHILDREN: usize = 2;
/// 内部页允许的最大孩子数，超出即分裂。
pub const MAX_CHILDREN: usize = 4;

/// 索引项：`(整数键, 行号)` 二元组。
///
/// 顺序由派生的 [`Ord`] 给出，先比 `key` 再比 `row_id`，因此同键记录按行号有序。
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct Entry {
    /// 整数键。
    pub key: Key,
    /// 行号，同一键内用它区分不同记录。
    pub row_id: RowId,
}

impl Entry {
    /// 构造一个索引项。
    pub const fn new(key: Key, row_id: RowId) -> Self {
        Self { key, row_id }
    }
}

/// 叶页：存放索引项，并用双向指针串联相邻叶页。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LeafPage {
    /// 按 `(key, row_id)` 升序存放的索引项。
    pub entries: Vec<Entry>,
    /// 右兄弟叶页页号（叶链后继）。
    pub next: Option<PageId>,
    /// 左兄弟叶页页号（叶链前驱）。
    pub prev: Option<PageId>,
}

/// 内部页：`separators[i]` 始终等于 `children[i+1]` 子树的最小索引项。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct InternalPage {
    /// 分隔项，长度恒为 `children.len() - 1`，按全序非递减（重复键时可能相等）。
    pub separators: Vec<Entry>,
    /// 孩子页号。
    pub children: Vec<PageId>,
}

/// 磁盘页的两种形态。字段全部只读对外暴露，变更只能经由 [`BPlusTree`] 的方法进行。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Page {
    /// 叶页。
    Leaf(LeafPage),
    /// 内部页。
    Internal(InternalPage),
}

impl Page {
    /// 若是叶页，返回其只读引用。
    pub fn as_leaf(&self) -> Option<&LeafPage> {
        match self {
            Page::Leaf(leaf) => Some(leaf),
            Page::Internal(_) => None,
        }
    }

    /// 若是内部页，返回其只读引用。
    pub fn as_internal(&self) -> Option<&InternalPage> {
        match self {
            Page::Internal(internal) => Some(internal),
            Page::Leaf(_) => None,
        }
    }

    /// 是否为叶页。
    pub fn is_leaf(&self) -> bool {
        matches!(self, Page::Leaf(_))
    }

    fn as_leaf_mut(&mut self) -> Option<&mut LeafPage> {
        match self {
            Page::Leaf(leaf) => Some(leaf),
            Page::Internal(_) => None,
        }
    }

    fn as_internal_mut(&mut self) -> Option<&mut InternalPage> {
        match self {
            Page::Internal(internal) => Some(internal),
            Page::Leaf(_) => None,
        }
    }
}

/// 页式 B+ 树索引。
#[derive(Debug)]
pub struct BPlusTree {
    pages: HashMap<PageId, Page>,
    root: PageId,
    next_id: PageId,
    len: usize,
}

impl Default for BPlusTree {
    fn default() -> Self {
        Self::new()
    }
}

impl BPlusTree {
    /// 创建一棵空树，初始只有一个空的根叶页。
    pub fn new() -> Self {
        let mut pages = HashMap::new();
        let root = 0;
        pages.insert(
            root,
            Page::Leaf(LeafPage {
                entries: Vec::new(),
                next: None,
                prev: None,
            }),
        );
        Self {
            pages,
            root,
            next_id: 1,
            len: 0,
        }
    }

    /// 当前索引项总数。
    pub fn len(&self) -> usize {
        self.len
    }

    /// 索引是否为空。
    pub fn is_empty(&self) -> bool {
        self.len == 0
    }

    /// 根页页号。
    pub fn root_id(&self) -> PageId {
        self.root
    }

    /// 当前分配的页数（含根页）。
    pub fn page_count(&self) -> usize {
        self.pages.len()
    }

    /// 按页号只读取页；页号不存在（例如已被合并回收）时返回 `None`。
    pub fn get_page(&self, id: PageId) -> Option<&Page> {
        self.pages.get(&id)
    }

    /// 遍历当前所有页（顺序不保证，仅用于只读检查/测试）。
    pub fn pages(&self) -> impl Iterator<Item = (PageId, &Page)> {
        self.pages.iter().map(|(id, page)| (*id, page))
    }

    /// 树高：只有根叶页时为 1。
    pub fn height(&self) -> usize {
        let mut height = 1;
        let mut id = self.root;
        while let Page::Internal(internal) = &self.pages[&id] {
            id = internal.children[0];
            height += 1;
        }
        height
    }

    // ------------------------------------------------------------------
    // 插入
    // ------------------------------------------------------------------

    /// 逐条插入一个索引项。
    ///
    /// 返回 `true` 表示插入成功；若完全相同的 `(key, row_id)` 已存在则返回
    /// `false` 且树保持不变。叶页溢出（5 项）时分裂，内部页溢出（5 个孩子）
    /// 时继续向上分裂，必要时生成新根。
    pub fn insert(&mut self, entry: Entry) -> bool {
        let path = self.find_leaf_path(entry);
        let leaf_id = *path.last().expect("path always ends at a leaf");

        let pos = {
            let leaf = self.pages[&leaf_id].as_leaf().expect("path ends at leaf");
            let pos = leaf.entries.partition_point(|candidate| *candidate <= entry);
            if pos > 0 && leaf.entries[pos - 1] == entry {
                return false;
            }
            pos
        };

        {
            let leaf = self
                .pages
                .get_mut(&leaf_id)
                .unwrap()
                .as_leaf_mut()
                .unwrap();
            leaf.entries.insert(pos, entry);
        }
        self.len += 1;

        let overflow = self.pages[&leaf_id]
            .as_leaf()
            .unwrap()
            .entries
            .len()
            > MAX_LEAF_ENTRIES;
        if overflow {
            let (new_id, separator) = self.split_leaf(leaf_id);
            // path 的最后一个元素是被分裂的叶页本身，去掉后才是其祖先链。
            let mut ancestors = path;
            ancestors.pop();
            self.insert_in_parent(leaf_id, new_id, separator, ancestors);
        }
        true
    }

    /// 沿根到叶按完整二元组定位归属叶页，返回完整页号路径（含根与叶）。
    ///
    /// 下降规则：进入第一个分隔项严格大于目标项的孩子；分隔项与目标相等时走
    /// 左侧，保证重复二元组的落点稳定且唯一。
    fn find_leaf_path(&self, entry: Entry) -> Vec<PageId> {
        let mut path = Vec::new();
        let mut id = self.root;
        loop {
            path.push(id);
            match &self.pages[&id] {
                Page::Leaf(_) => return path,
                Page::Internal(internal) => {
                    let idx = internal
                        .separators
                        .partition_point(|separator| *separator <= entry);
                    id = internal.children[idx];
                }
            }
        }
    }

    /// 分裂溢出叶页：保留左半，新建右半并接入叶链。返回 `(新页号, 提升分隔项)`。
    fn split_leaf(&mut self, leaf_id: PageId) -> (PageId, Entry) {
        let new_id = self.alloc_page_id();
        let (moved, old_next, separator) = {
            let leaf = self
                .pages
                .get_mut(&leaf_id)
                .unwrap()
                .as_leaf_mut()
                .unwrap();
            let at = leaf.entries.len() / 2;
            let moved: Vec<Entry> = leaf.entries.drain(at..).collect();
            let separator = moved[0];
            (moved, leaf.next, separator)
        };

        self.pages.insert(
            new_id,
            Page::Leaf(LeafPage {
                entries: moved,
                next: old_next,
                prev: Some(leaf_id),
            }),
        );
        if let Some(next_id) = old_next {
            self.pages
                .get_mut(&next_id)
                .unwrap()
                .as_leaf_mut()
                .unwrap()
                .prev = Some(new_id);
        }
        self.pages
            .get_mut(&leaf_id)
            .unwrap()
            .as_leaf_mut()
            .unwrap()
            .next = Some(new_id);

        (new_id, separator)
    }

    /// 分裂溢出内部页，返回 `(新页号, 提升到父页的分隔项)`。
    fn split_internal(&mut self, id: PageId) -> (PageId, Entry) {
        let new_id = self.alloc_page_id();
        let (new_separators, new_children, promoted) = {
            let node = self
                .pages
                .get_mut(&id)
                .unwrap()
                .as_internal_mut()
                .unwrap();
            // 5 个孩子时 at=2：左留 2 个孩子，右得 3 个孩子，提升原 separators[1]。
            let at = node.children.len() / 2;
            let promoted = node.separators[at - 1];
            let new_separators: Vec<Entry> = node.separators.drain(at..).collect();
            let new_children: Vec<PageId> = node.children.drain(at..).collect();
            node.separators.pop(); // 提升项不再留在左半
            (new_separators, new_children, promoted)
        };
        self.pages.insert(
            new_id,
            Page::Internal(InternalPage {
                separators: new_separators,
                children: new_children,
            }),
        );
        (new_id, promoted)
    }

    /// 把 `(separator, right)` 插到 `left` 右侧的父级位置，并递归处理父级溢出。
    ///
    /// `ancestors` 是 `left` 的根到父页路径（不含 `left` 自身）。
    fn insert_in_parent(
        &mut self,
        left: PageId,
        right: PageId,
        separator: Entry,
        mut ancestors: Vec<PageId>,
    ) {
        match ancestors.pop() {
            None => {
                // left 原本就是根页：生成新的内部根，树高加一。
                let new_root = InternalPage {
                    separators: vec![separator],
                    children: vec![left, right],
                };
                let root_id = self.alloc_page_id();
                self.pages.insert(root_id, Page::Internal(new_root));
                self.root = root_id;
            }
            Some(parent_id) => {
                let pos = {
                    let parent = self.pages[&parent_id].as_internal().unwrap();
                    parent
                        .children
                        .iter()
                        .position(|child| *child == left)
                        .expect("left child must exist in parent")
                };
                {
                    let parent = self
                        .pages
                        .get_mut(&parent_id)
                        .unwrap()
                        .as_internal_mut()
                        .unwrap();
                    parent.children.insert(pos + 1, right);
                    parent.separators.insert(pos, separator);
                }
                if self.pages[&parent_id]
                    .as_internal()
                    .unwrap()
                    .children
                    .len()
                    > MAX_CHILDREN
                {
                    let (new_id, promoted) = self.split_internal(parent_id);
                    self.insert_in_parent(parent_id, new_id, promoted, ancestors);
                }
            }
        }
    }

    // ------------------------------------------------------------------
    // 删除
    // ------------------------------------------------------------------

    /// 按完整二元组删除一个索引项。
    ///
    /// 返回 `true` 表示确实删除了一项；树中不存在该二元组时返回 `false` 且树
    /// 保持不变。删除后若出现非根欠载，依次尝试向左/右同父兄弟借位，都借不到
    /// 才合并；合并沿祖先链向上传播，根内部页只剩一个孩子时降高。
    pub fn delete(&mut self, entry: Entry) -> bool {
        let path = self.find_leaf_path(entry);
        let leaf_id = *path.last().unwrap();

        let removed = {
            let leaf = self
                .pages
                .get_mut(&leaf_id)
                .unwrap()
                .as_leaf_mut()
                .unwrap();
            match leaf.entries.iter().position(|candidate| *candidate == entry) {
                Some(pos) => {
                    leaf.entries.remove(pos);
                    true
                }
                None => false,
            }
        };
        if !removed {
            return false;
        }
        self.len -= 1;

        self.rebalance_after_delete(path);
        if self.len > 0 {
            // 删除的可能是某子树最小项，统一自顶向下重算分隔项，维持
            // “分隔项 == 右孩子子树最小项”的不变量。
            self.repair_separators(self.root);
        }
        true
    }

    /// 自叶向根处理欠载；最后处理根降高。
    fn rebalance_after_delete(&mut self, path: Vec<PageId>) {
        let mut path = path;
        let mut child_id = match path.pop() {
            Some(id) => id,
            None => return,
        };

        while let Some(parent_id) = path.pop() {
            if !self.is_underflow(child_id) {
                // 借位/合并没有发生时，祖先的孩子数不可能变化，提前结束。
                return;
            }
            self.rebalance_child(parent_id, child_id);
            child_id = parent_id;
        }

        // child_id 即根页：内部根只剩一个孩子时摘掉根，树高减一。
        if let Page::Internal(internal) = &self.pages[&self.root] {
            if internal.children.len() == 1 {
                let only_child = internal.children[0];
                self.pages.remove(&self.root);
                self.root = only_child;
            }
        }
    }

    /// 根页豁免容量下限；其余页按各自下限判断欠载。
    fn is_underflow(&self, id: PageId) -> bool {
        if id == self.root {
            return false;
        }
        match &self.pages[&id] {
            Page::Leaf(leaf) => leaf.entries.len() < MIN_LEAF_ENTRIES,
            Page::Internal(internal) => internal.children.len() < MIN_CHILDREN,
        }
    }

    /// 处理 `child_id` 在 `parent_id` 下的欠载：先借位，后合并。
    fn rebalance_child(&mut self, parent_id: PageId, child_id: PageId) {
        let (idx, left_id, right_id) = {
            let parent = self.pages[&parent_id].as_internal().unwrap();
            let idx = parent
                .children
                .iter()
                .position(|child| *child == child_id)
                .unwrap();
            let left_id = if idx > 0 { Some(parent.children[idx - 1]) } else { None };
            let right_id = if idx + 1 < parent.children.len() {
                Some(parent.children[idx + 1])
            } else {
                None
            };
            (idx, left_id, right_id)
        };

        // 优先向相邻同父兄弟借：先左后右。
        if let Some(sibling) = left_id {
            if self.can_lend(sibling) {
                self.borrow_from_left(parent_id, idx, sibling, child_id);
                return;
            }
        }
        if let Some(sibling) = right_id {
            if self.can_lend(sibling) {
                self.borrow_from_right(parent_id, idx, child_id, sibling);
                return;
            }
        }

        // 两侧都借不到：与左兄弟合并；没有左兄弟时与右兄弟合并。
        if let Some(sibling) = left_id {
            self.merge_siblings(parent_id, sibling, child_id);
        } else {
            let sibling = right_id.expect("underflowed non-root node has a sibling");
            self.merge_siblings(parent_id, child_id, sibling);
        }
    }

    /// 兄弟页是否高于下限、可以借出一项/一个孩子。
    fn can_lend(&self, id: PageId) -> bool {
        match &self.pages[&id] {
            Page::Leaf(leaf) => leaf.entries.len() > MIN_LEAF_ENTRIES,
            Page::Internal(internal) => internal.children.len() > MIN_CHILDREN,
        }
    }

    /// 子树根的最小索引项（沿最左孩子下降到叶页取首项）。
    fn subtree_min_entry(&self, mut id: PageId) -> Entry {
        loop {
            match &self.pages[&id] {
                Page::Leaf(leaf) => return leaf.entries[0],
                Page::Internal(internal) => id = internal.children[0],
            }
        }
    }

    /// 从左兄弟借一项/一个孩子到 `child_id`，并修正父页分隔项。
    fn borrow_from_left(
        &mut self,
        parent_id: PageId,
        idx: usize,
        left_id: PageId,
        child_id: PageId,
    ) {
        let child_is_leaf = self.pages[&child_id].is_leaf();
        if child_is_leaf {
            let moved = {
                let left = self
                    .pages
                    .get_mut(&left_id)
                    .unwrap()
                    .as_leaf_mut()
                    .unwrap();
                left.entries.pop().unwrap()
            };
            let child = self
                .pages
                .get_mut(&child_id)
                .unwrap()
                .as_leaf_mut()
                .unwrap();
            child.entries.insert(0, moved);
        } else {
            let (moved_child, separator_between) = {
                let parent = self.pages[&parent_id].as_internal().unwrap();
                let separator_between = parent.separators[idx - 1];
                let left = self
                    .pages
                    .get_mut(&left_id)
                    .unwrap()
                    .as_internal_mut()
                    .unwrap();
                let moved_child = left.children.pop().unwrap();
                left.separators.pop();
                (moved_child, separator_between)
            };
            let child = self
                .pages
                .get_mut(&child_id)
                .unwrap()
                .as_internal_mut()
                .unwrap();
            child.children.insert(0, moved_child);
            child.separators.insert(0, separator_between);
        }

        // 父页中指向 child 的分隔项恒为 child 子树的最小项。
        let new_separator = self.subtree_min_entry(child_id);
        self.pages
            .get_mut(&parent_id)
            .unwrap()
            .as_internal_mut()
            .unwrap()
            .separators[idx - 1] = new_separator;
    }

    /// 从右兄弟借一项/一个孩子到 `child_id`，并修正父页分隔项。
    fn borrow_from_right(
        &mut self,
        parent_id: PageId,
        idx: usize,
        child_id: PageId,
        right_id: PageId,
    ) {
        let child_is_leaf = self.pages[&child_id].is_leaf();
        if child_is_leaf {
            let moved = {
                let right = self
                    .pages
                    .get_mut(&right_id)
                    .unwrap()
                    .as_leaf_mut()
                    .unwrap();
                right.entries.remove(0)
            };
            let child = self
                .pages
                .get_mut(&child_id)
                .unwrap()
                .as_leaf_mut()
                .unwrap();
            child.entries.push(moved);
        } else {
            let (moved_child, separator_between) = {
                let parent = self.pages[&parent_id].as_internal().unwrap();
                let separator_between = parent.separators[idx];
                let right = self
                    .pages
                    .get_mut(&right_id)
                    .unwrap()
                    .as_internal_mut()
                    .unwrap();
                let moved_child = right.children.remove(0);
                right.separators.remove(0);
                (moved_child, separator_between)
            };
            let child = self
                .pages
                .get_mut(&child_id)
                .unwrap()
                .as_internal_mut()
                .unwrap();
            child.separators.push(separator_between);
            child.children.push(moved_child);
        }

        // 右兄弟借出首项后，它的最小项发生变化，父页分隔项同步更新。
        let new_separator = self.subtree_min_entry(right_id);
        self.pages
            .get_mut(&parent_id)
            .unwrap()
            .as_internal_mut()
            .unwrap()
            .separators[idx] = new_separator;
    }

    /// 把 `right_id` 合并进 `left_id`（两者为同父相邻兄弟），并从父页摘掉右孩子。
    fn merge_siblings(&mut self, parent_id: PageId, left_id: PageId, right_id: PageId) {
        let (right_pos, separator) = {
            let parent = self.pages[&parent_id].as_internal().unwrap();
            let right_pos = parent
                .children
                .iter()
                .position(|child| *child == right_id)
                .unwrap();
            (right_pos, parent.separators[right_pos - 1])
        };

        let right_page = self.pages.remove(&right_id).expect("right sibling exists");
        match right_page {
            Page::Leaf(mut right_leaf) => {
                let next = right_leaf.next;
                let left = self
                    .pages
                    .get_mut(&left_id)
                    .unwrap()
                    .as_leaf_mut()
                    .unwrap();
                left.entries.append(&mut right_leaf.entries);
                debug_assert!(left.entries.len() <= MAX_LEAF_ENTRIES);
                left.next = next;
                if let Some(next_id) = next {
                    self.pages
                        .get_mut(&next_id)
                        .unwrap()
                        .as_leaf_mut()
                        .unwrap()
                        .prev = Some(left_id);
                }
            }
            Page::Internal(mut right_node) => {
                let left = self
                    .pages
                    .get_mut(&left_id)
                    .unwrap()
                    .as_internal_mut()
                    .unwrap();
                // 父页中隔开两兄弟的分隔项下沉为合并页的中间分隔项。
                left.separators.push(separator);
                left.separators.append(&mut right_node.separators);
                left.children.append(&mut right_node.children);
                debug_assert!(left.children.len() <= MAX_CHILDREN);
            }
        }

        let parent = self
            .pages
            .get_mut(&parent_id)
            .unwrap()
            .as_internal_mut()
            .unwrap();
        parent.children.remove(right_pos);
        parent.separators.remove(right_pos - 1);
    }

    /// 自顶向下重算整棵树的分隔项，返回 `id` 子树的最小索引项。
    fn repair_separators(&mut self, id: PageId) -> Entry {
        if self.pages[&id].is_leaf() {
            return self.pages[&id].as_leaf().unwrap().entries[0];
        }

        let children = self.pages[&id]
            .as_internal()
            .unwrap()
            .children
            .clone();
        let mut mins = Vec::with_capacity(children.len());
        for child in children {
            mins.push(self.repair_separators(child));
        }
        let repaired = mins[1..].to_vec();
        self.pages
            .get_mut(&id)
            .unwrap()
            .as_internal_mut()
            .unwrap()
            .separators = repaired;
        mins[0]
    }

    // ------------------------------------------------------------------
    // 查询与扫描
    // ------------------------------------------------------------------

    /// 沿内部页按完整二元组下降，定位 `entry` 应归属的叶页页号。
    ///
    /// 下降规则：进入第一个分隔项严格大于目标项的孩子；分隔项与目标相等时走
    /// 左侧，保证重复二元组的落点稳定且唯一。删除/存在性判断必须走这条路径，
    /// 因为同一键的不同行号可能分布在不同叶页。
    pub fn locate_leaf_entry(&self, entry: Entry) -> PageId {
        let mut id = self.root;
        while let Page::Internal(internal) = &self.pages[&id] {
            let idx = internal
                .separators
                .partition_point(|separator| *separator <= entry);
            id = internal.children[idx];
        }
        id
    }

    /// 沿内部页只按键下降：进入第一个“最小键 >= key”的孩子，返回其叶页。
    ///
    /// 这是重复键场景下该键的最左落点叶页（同键记录可以继续沿叶链向后跨页）。
    /// 这里必须用严格小于：当分隔项的键恰好等于 `key` 时，右孩子虽然也含该键，
    /// 但左孩子可能同样含该键（重复键跨页），最左落点在左侧子树。与按完整
    /// 二元组下降（[`BPlusTree::locate_leaf_entry`]，相等走右侧）正好不同。
    fn locate_leaf_by_key(&self, key: Key) -> PageId {
        let mut id = self.root;
        while let Page::Internal(internal) = &self.pages[&id] {
            let idx = internal
                .separators
                .partition_point(|separator| separator.key < key);
            id = internal.children[idx];
        }
        id
    }

    /// 沿内部页按键下降，定位 `key` 所属区间的最左叶页页号（重复键的最左落点）。
    pub fn locate_leaf(&self, key: Key) -> PageId {
        self.locate_leaf_by_key(key)
    }

    /// 判断完整二元组是否存在。
    pub fn contains(&self, entry: Entry) -> bool {
        let leaf_id = self.locate_leaf_entry(entry);
        self.pages[&leaf_id]
            .as_leaf()
            .unwrap()
            .entries
            .contains(&entry)
    }

    /// 沿树定位后再沿叶链收集同一键的全部行号，返回值按 `row_id` 升序。
    ///
    /// 重复键可以跨叶页，因此定位到首个落点叶后需要顺着 `next` 继续扫描，
    /// 直到遇到更大的键。
    pub fn find(&self, key: Key) -> Vec<RowId> {
        let mut out = Vec::new();
        let mut id = self.locate_leaf(key);
        loop {
            let leaf = self.pages[&id].as_leaf().unwrap();
            for entry in &leaf.entries {
                if entry.key == key {
                    out.push(entry.row_id);
                }
                if entry.key > key {
                    // 当前叶页已越过目标键，后续叶页只会更大，可以整体结束。
                    return out;
                }
            }
            match leaf.next {
                Some(next_id) => id = next_id,
                None => return out,
            }
        }
    }

    /// 正向扫描左闭右开区间 `[start_inclusive, end_exclusive)` 内的全部索引项，
    /// 返回结果按 `(key, row_id)` 升序。区间为空（起点 >= 终点）时返回空向量。
    pub fn scan(&self, start_inclusive: Key, end_exclusive: Key) -> Vec<Entry> {
        let mut out = Vec::new();
        if start_inclusive >= end_exclusive {
            return out;
        }
        // 起点是键区间下界，只按键下降到不晚于起点的最左叶页。
        let mut id = self.locate_leaf(start_inclusive);
        loop {
            let leaf = self.pages[&id].as_leaf().unwrap();
            for entry in &leaf.entries {
                if entry.key < start_inclusive {
                    continue;
                }
                if entry.key >= end_exclusive {
                    return out;
                }
                out.push(*entry);
            }
            match leaf.next {
                Some(next_id) => id = next_id,
                None => break,
            }
        }
        out
    }

    /// 反向扫描同一个左闭右开区间 `[start_inclusive, end_exclusive)`，
    /// 返回结果按 `(key, row_id)` 降序，等价于正向扫描结果整体反转。
    pub fn scan_rev(&self, start_inclusive: Key, end_exclusive: Key) -> Vec<Entry> {
        let mut out = Vec::new();
        if start_inclusive >= end_exclusive || self.is_empty() {
            return out;
        }
        // - 若落点叶首项键已 >= end，区间尾部在它的左兄弟；
        // - 否则（落点叶内部存在 < end 的项），落点叶本身就是区间尾部所在叶。
        let landed = self.locate_leaf_by_key(end_exclusive);
        let first_key = self.pages[&landed].as_leaf().unwrap().entries[0].key;
        let mut id = if first_key >= end_exclusive {
            match self.pages[&landed].as_leaf().unwrap().prev {
                Some(prev_id) => prev_id,
                None => return out,
            }
        } else {
            landed
        };
        loop {
            let leaf = self.pages[&id].as_leaf().unwrap();
            for entry in leaf.entries.iter().rev() {
                if entry.key >= end_exclusive {
                    continue;
                }
                if entry.key < start_inclusive {
                    return out;
                }
                out.push(*entry);
            }
            match leaf.prev {
                Some(prev_id) => id = prev_id,
                None => break,
            }
        }
        out
    }

    // ------------------------------------------------------------------
    // 只读叶链
    // ------------------------------------------------------------------

    /// 叶链头页（最左叶页）页号。
    pub fn leaf_head(&self) -> PageId {
        let mut id = self.root;
        while let Page::Internal(internal) = &self.pages[&id] {
            id = internal.children[0];
        }
        id
    }

    /// 沿 `next` 指针列出全部叶页页号（升序）。
    pub fn leaf_chain(&self) -> Vec<PageId> {
        let mut out = Vec::new();
        let mut id = Some(self.leaf_head());
        while let Some(current) = id {
            out.push(current);
            id = self.pages[&current].as_leaf().unwrap().next;
        }
        out
    }

    fn alloc_page_id(&mut self) -> PageId {
        let id = self.next_id;
        self.next_id += 1;
        id
    }
}

#[cfg(test)]
mod unit_tests {
    use super::*;

    #[test]
    fn empty_tree_basics() {
        let tree = BPlusTree::new();
        assert_eq!(tree.len(), 0);
        assert!(tree.is_empty());
        assert_eq!(tree.height(), 1);
        assert!(tree.scan(i64::MIN, i64::MAX).is_empty());
        assert!(tree.scan_rev(i64::MIN, i64::MAX).is_empty());
        assert!(tree.find(1).is_empty());
        assert!(!tree.contains(Entry::new(1, 1)));
        assert_eq!(tree.leaf_chain(), vec![tree.root_id()]);
    }

    #[test]
    fn insert_dedup_and_lookup() {
        let mut tree = BPlusTree::new();
        assert!(tree.insert(Entry::new(5, 2)));
        assert!(tree.insert(Entry::new(5, 1)));
        assert!(tree.insert(Entry::new(3, 9)));
        assert!(!tree.insert(Entry::new(5, 1)));
        assert_eq!(tree.len(), 3);
        assert!(tree.contains(Entry::new(5, 1)));
        assert!(!tree.contains(Entry::new(5, 99)));
        assert_eq!(tree.find(5), vec![1, 2]);
        assert_eq!(
            tree.scan(0, 6),
            vec![Entry::new(3, 9), Entry::new(5, 1), Entry::new(5, 2)]
        );
    }

    #[test]
    fn delete_nonexistent_is_noop() {
        let mut tree = BPlusTree::new();
        tree.insert(Entry::new(1, 1));
        assert!(!tree.delete(Entry::new(1, 2)));
        assert!(!tree.delete(Entry::new(2, 1)));
        assert_eq!(tree.len(), 1);
    }
}
