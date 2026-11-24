//! 撤根后立即复用槽的端到端走查（真实功能，非预置输出）。
//!
//! 运行：`cargo run --offline --example drop_reuse`

use persistent_miniheap::medium::PersistentMedia;
use persistent_miniheap::{HeapError, PersistentHeap};

fn main() {
    let media = PersistentMedia::cold_boot(persistent_miniheap::heap::TOTAL_LINES);
    let mut heap = PersistentHeap::format(media);

    // 1) 在根 2 上发布一个对象，读者拿到跨恢复句柄。
    let first = heap.publish(2, &[111, 222]).unwrap();
    println!("published first object: {first:?}");
    println!("read through first handle: {:?}", heap.read(first).unwrap());

    // 2) 撤根：根项清空并经屏障持久，紧接着回收，槽立刻回到空闲表。
    heap.drop_root(2).unwrap();
    println!("root 2 after drop: {:?}", heap.root_handle(2).unwrap());
    println!("free table after drop: {:?}", heap.free_table());

    // 3) 立即复用同一槽发布新对象。
    let second = heap.publish(2, &[333]).unwrap();
    println!("reused slot for second object: {second:?}");
    assert_eq!(second.slot, first.slot);

    // 4) 旧句柄因代次落后而失效，绝不会读到复用后的新对象。
    match heap.read(first) {
        Err(HeapError::BadSlot { slot }) => {
            println!("stale handle correctly rejected on slot {slot}");
        }
        other => panic!("expected stale-handle error, got {other:?}"),
    }
    println!("read through second handle: {:?}", heap.read(second).unwrap());

    // 5) 独立遍历审计：可达集合与空闲集合互斥且覆盖全部槽。
    let audit = heap.audit();
    println!(
        "audit: disjoint={} covers_all={} headers_ok={}",
        audit.disjoint, audit.covers_all_slots, audit.headers_ok
    );
    assert!(audit.disjoint && audit.covers_all_slots && audit.headers_ok);
}
