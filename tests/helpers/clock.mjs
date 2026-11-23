// A small injected clock so retry windows and caller deadlines are exact.

export function createClock(start = 1_000_000) {
  let current = start;
  return {
    now: () => current,
    set(value) {
      current = value;
      return current;
    },
    advance(delta) {
      current += delta;
      return current;
    },
  };
}
