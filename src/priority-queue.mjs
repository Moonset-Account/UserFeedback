// 最小二叉堆。比较键相同时按入队序号 FIFO，保证模拟确定性。
export class MinHeap {
  #heap = [];
  #seq = 0;

  get size() {
    return this.#heap.length;
  }

  push(key, payload) {
    const node = { key, seq: this.#seq++, payload };
    this.#heap.push(node);
    this.#siftUp(this.#heap.length - 1);
  }

  pop() {
    const heap = this.#heap;
    if (heap.length === 0) return undefined;
    const top = heap[0];
    const last = heap.pop();
    if (heap.length > 0) {
      heap[0] = last;
      this.#siftDown(0);
    }
    return top.payload;
  }

  peekKey() {
    return this.#heap.length === 0 ? null : this.#heap[0].key;
  }

  #siftUp(index) {
    const heap = this.#heap;
    const node = heap[index];
    while (index > 0) {
      const parent = (index - 1) >> 1;
      if (!this.#less(node, heap[parent])) break;
      heap[index] = heap[parent];
      index = parent;
    }
    heap[index] = node;
  }

  #siftDown(index) {
    const heap = this.#heap;
    const size = heap.length;
    const node = heap[index];
    while (true) {
      const left = index * 2 + 1;
      const right = left + 1;
      let smallest = index;
      if (left < size && this.#less(heap[left], node)) smallest = left;
      if (right < size && this.#less(heap[right], smallest === index ? node : heap[left])) {
        smallest = right;
      }
      if (smallest === index) break;
      heap[index] = heap[smallest];
      index = smallest;
    }
    heap[index] = node;
  }

  #less(a, b) {
    return a.key < b.key || (a.key === b.key && a.seq < b.seq);
  }
}
