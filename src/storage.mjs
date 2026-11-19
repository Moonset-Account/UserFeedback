// Reference in-memory storage driver.
//
// A "storage driver" is the external world behind ctx.read/ctx.write. It only
// needs two async methods: read(resource) and write(resource, value). Values
// cross the boundary as structured clones so that fixture code cannot alias
// stored references and accidentally make two sides look equal.

export class MapStorage {
  #map = new Map();
  #writeCount = 0;
  #readCount = 0;

  constructor(initial = {}) {
    for (const [resource, value] of Object.entries(initial)) {
      this.#map.set(resource, structuredClone(value));
    }
  }

  async read(resource) {
    this.#readCount += 1;
    if (!this.#map.has(resource)) return undefined;
    return structuredClone(this.#map.get(resource));
  }

  async write(resource, value) {
    this.#writeCount += 1;
    this.#map.set(resource, structuredClone(value));
    return { accepted: true, layer: 'storage', resource };
  }

  /** Deep snapshot used by tests to prove candidate writes never land here. */
  snapshot() {
    const out = {};
    for (const [k, v] of this.#map) out[k] = structuredClone(v);
    return out;
  }

  get stats() {
    return { reads: this.#readCount, writes: this.#writeCount };
  }
}
