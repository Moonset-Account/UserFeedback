// Address family ordering:
//   1. IPv6 is tried first.
//   2. While both families still have candidates, families alternate (the
//      address picked after an IPv6 one is IPv4, and vice versa).
//   3. Once one family is exhausted, the remaining addresses of the other
//      family are consumed in their original order.
// Addresses inside each family keep the order supplied by the caller.

export class AddressSelector {
  #v6;
  #v4;
  #i6 = 0;
  #i4 = 0;
  #lastFamily; // family of the previously returned address

  constructor(ipv6 = [], ipv4 = []) {
    this.#v6 = [...ipv6];
    this.#v4 = [...ipv4];
  }

  // Whether at least one address has not been handed out yet.
  hasNext() {
    return this.#i6 < this.#v6.length || this.#i4 < this.#v4.length;
  }

  // Number of addresses not handed out yet.
  get remaining() {
    return this.#v6.length - this.#i6 + (this.#v4.length - this.#i4);
  }

  // Returns { address, family, index } where `index` is the position inside
  // the family-specific input array, or null when every address was consumed.
  next() {
    const v6Left = this.#i6 < this.#v6.length;
    const v4Left = this.#i4 < this.#v4.length;

    if (!v6Left && !v4Left) return null;

    // Both families have candidates: start with IPv6, then alternate.
    if (v6Left && v4Left) {
      if (this.#lastFamily === undefined || this.#lastFamily === 'ipv4') {
        return this.#take('ipv6');
      }
      return this.#take('ipv4');
    }

    // One family is exhausted: drain the other in input order.
    if (v6Left) return this.#take('ipv6');
    return this.#take('ipv4');
  }

  #take(family) {
    if (family === 'ipv6') {
      const index = this.#i6;
      const address = this.#v6[this.#i6++];
      this.#lastFamily = 'ipv6';
      return { address, family: 'ipv6', index };
    }
    const index = this.#i4;
    const address = this.#v4[this.#i4++];
    this.#lastFamily = 'ipv4';
    return { address, family: 'ipv4', index };
  }
}
