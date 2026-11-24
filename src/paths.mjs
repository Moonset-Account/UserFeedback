const INT_RE = /^(0|[1-9]\d*)$/;

export function parsePath(path) {
  if (Array.isArray(path)) return path.map((seg) => String(seg));
  if (path == null) return [];
  if (typeof path !== 'string') {
    throw new TypeError('path must be a JSON Pointer string or an array');
  }
  if (path === '') return [];
  if (!path.startsWith('/')) {
    // Convenience dotted/bracketed form. Canonical documentation uses JSON Pointer.
    const parts = [];
    for (const m of path.matchAll(/[^.[\]]+|\[(?:\d+|"[^"]*"|'[^']*')\]/g)) {
      let token = m[0];
      if (token.startsWith('[')) token = token.slice(1, -1).replace(/^['"]|['"]$/g, '');
      parts.push(token);
    }
    return parts;
  }
  return path.slice(1).split('/').map((part) => part.replace(/~1/g, '/').replace(/~0/g, '~'));
}

export function formatPath(segments = []) {
  const segs = parsePath(segments);
  if (segs.length === 0) return '';
  return `/${segs.map((s) => String(s).replace(/~/g, '~0').replace(/\//g, '~1')).join('/')}`;
}

export function pathStartsWith(maybeChild, ancestor) {
  const a = parsePath(ancestor);
  const c = parsePath(maybeChild);
  if (c.length < a.length) return false;
  return a.every((seg, i) => seg === c[i]);
}

export function pathIsDescendant(maybeChild, ancestor) {
  const a = parsePath(ancestor);
  const c = parsePath(maybeChild);
  return c.length > a.length && pathStartsWith(c, a);
}

export function asArrayIndex(segment, length = Number.MAX_SAFE_INTEGER) {
  if (typeof segment === 'number' && Number.isInteger(segment)) {
    return segment >= 0 && segment <= length ? segment : null;
  }
  const s = String(segment);
  if (s === '-' ) return length;
  if (!INT_RE.test(s)) return null;
  const n = Number(s);
  return n <= length ? n : null;
}

export function isPlainObject(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return false;
  const proto = Object.getPrototypeOf(value);
  return proto === Object.prototype || proto === null;
}
