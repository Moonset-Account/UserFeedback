"""Threshold secret reconstruction over GF(65537) with error correction.

Each byte of the secret is shared independently with a polynomial of degree
< t whose constant term is that byte (Shamir per byte).  Reconstruction
tolerates up to ``e`` erroneous shares per the declared error budget by
decoding each byte column as a Reed-Solomon codeword (Berlekamp-Welch).

Security properties:
  * On any failure no partial secret is returned (``secret is None``).
  * Diagnostics only ever contain share numbers (``Share.index``), never
    share values, coordinates of other batches, or secret material.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

PRIME = 65537          # field modulus (2**16 + 1, prime)
MAX_SHARES = 31        # at most 31 shares per batch
MAX_LENGTH = 4096      # at most 4096 secret bytes

OK = "ok"
INSUFFICIENT = "insufficient_evidence"
INCONSISTENT = "inconsistent"


# ---------------------------------------------------------------------------
# Public data types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BatchDescriptor:
    """Trusted description of a share batch."""
    batch_id: str
    threshold: int               # t: polynomial degree < t
    x_coords: Tuple[int, ...]    # all expected share x-coordinates
    length: int                  # original secret length in bytes
    sha256: str                  # hex SHA-256 of the original bytes
    prime: int = PRIME


@dataclass(frozen=True)
class Share:
    """One received share: per-column y values at coordinate ``x``."""
    index: int                   # share number (only thing diagnostics report)
    batch_id: str
    x: int
    ys: Tuple[int, ...]          # one GF(65537) element per byte column


@dataclass(frozen=True)
class RecoveryResult:
    status: str                  # OK / INSUFFICIENT / INCONSISTENT
    reason: str                  # machine-checkable reason code
    secret: Optional[bytes]      # None unless status == OK
    error_positions: Tuple[int, ...]   # corrected share numbers (OK only)
    diagnostics: Tuple[int, ...]       # share numbers relevant to a failure


# ---------------------------------------------------------------------------
# GF(65537) polynomial helpers (coefficients low -> high)
# ---------------------------------------------------------------------------

def _poly_eval(coeffs: Sequence[int], x: int) -> int:
    acc = 0
    for c in reversed(coeffs):
        acc = (acc * x + c) % PRIME
    return acc


def _poly_trim(p: Sequence[int]) -> List[int]:
    p = [c % PRIME for c in p]
    while len(p) > 1 and p[-1] == 0:
        p.pop()
    return p


def _poly_divmod(num: Sequence[int], den: Sequence[int]) -> Tuple[List[int], List[int]]:
    num = _poly_trim(num)
    den = _poly_trim(den)
    if den == [0]:
        raise ZeroDivisionError("polynomial division by zero")
    if len(num) < len(den):
        return [0], num
    q = [0] * (len(num) - len(den) + 1)
    rem = num[:]
    inv_lead = pow(den[-1], -1, PRIME)
    for k in range(len(q) - 1, -1, -1):
        c = rem[len(den) - 1 + k] * inv_lead % PRIME
        q[k] = c
        if c:
            for j in range(len(den)):
                rem[j + k] = (rem[j + k] - c * den[j]) % PRIME
    return _poly_trim(q), _poly_trim(rem)


def _solve_linear(rows: Sequence[Sequence[int]], rhs: Sequence[int],
                  n_unknowns: int) -> Optional[List[int]]:
    """Solve A*x = b over GF(PRIME); rows may exceed unknowns.

    Returns the unique solution, or None if the system is underdetermined
    or inconsistent.
    """
    m = len(rows)
    aug = [[(v % PRIME) for v in rows[i]] + [rhs[i] % PRIME] for i in range(m)]
    pivot_row = 0
    for col in range(n_unknowns):
        piv = -1
        for r in range(pivot_row, m):
            if aug[r][col] != 0:
                piv = r
                break
        if piv == -1:
            return None  # no pivot for this column -> underdetermined
        aug[pivot_row], aug[piv] = aug[piv], aug[pivot_row]
        prow = aug[pivot_row]
        inv_lead = pow(prow[col], -1, PRIME)
        for r in range(m):
            if r == pivot_row:
                continue
            factor = aug[r][col] * inv_lead % PRIME
            if factor:
                fr = aug[r]
                aug[r] = [(a - factor * b) % PRIME for a, b in zip(fr, prow)]
        pivot_row += 1
    # remaining rows must be 0 | 0
    for r in range(pivot_row, m):
        if aug[r][n_unknowns] != 0:
            return None
    sol = [0] * n_unknowns
    for c in range(n_unknowns):
        lead = aug[c][c]
        if lead == 0:
            return None
        sol[c] = aug[c][n_unknowns] * pow(lead, -1, PRIME) % PRIME
    return sol


# ---------------------------------------------------------------------------
# Reed-Solomon decoding of one byte column
# ---------------------------------------------------------------------------

def _erasure_attempt(xs, ys, t, e, exclude, xpow):
    """Cheap decode: interpolate from points outside ``exclude`` and verify.

    Any polynomial of degree < t agreeing with all but <= e received points
    is the unique correct decode (n >= t + 2e), so accepting it is safe.
    """
    n = len(xs)
    good = [i for i in range(n) if i not in exclude]
    if len(good) < t:
        return None
    use = good[:t]
    rows = [xpow[i][:t] for i in use]
    rhs = [ys[i] for i in use]
    sol = _solve_linear(rows, rhs, t)
    if sol is None:
        return None
    errs = [i for i in range(n) if _poly_eval(sol, xs[i]) != ys[i]]
    if len(errs) > e:
        return None
    return _poly_trim(sol), errs


def _bw_attempt(xs, ys, t, e_act, xpow):
    """Berlekamp-Welch decode assuming exactly ``e_act`` errors."""
    n = len(xs)
    n_unknowns = t + 2 * e_act
    rows = []
    rhs = []
    for i in range(n):
        xp = xpow[i]
        y = ys[i]
        row = list(xp[:t + e_act])                       # N coefficients
        row += [(-y * xp[k]) % PRIME for k in range(e_act)]  # E coefficients
        rows.append(row)
        rhs.append(y * xp[e_act] % PRIME)                # b_e = 1 fixed
    sol = _solve_linear(rows, rhs, n_unknowns)
    if sol is None:
        return None
    num = sol[:t + e_act]
    den = sol[t + e_act:] + [1]
    try:
        p, rem = _poly_divmod(num, den)
    except ZeroDivisionError:
        return None
    if rem != [0]:
        return None
    if len(p) > t:
        return None
    errs = [i for i in range(n) if _poly_eval(p, xs[i]) != ys[i]]
    if len(errs) > e_act:
        return None
    return p, errs


def _bw_decode(xs, ys, t, e, xpow):
    """Full decode: try decreasing error counts from the declared budget."""
    n = len(xs)
    max_err = min(e, (n - t) // 2)
    for e_act in range(max_err, -1, -1):
        res = _bw_attempt(xs, ys, t, e_act, xpow)
        if res is not None:
            return res
    return None


# ---------------------------------------------------------------------------
# Share generation (for local fixtures / rehearsals)
# ---------------------------------------------------------------------------

def generate_shares(secret: bytes, threshold: int, x_coords: Sequence[int],
                    batch_id: str, seed: int, start_index: int = 1) -> List[Share]:
    """Deterministically share ``secret`` byte-wise over GF(65537)."""
    if not (1 <= len(secret) <= MAX_LENGTH):
        raise ValueError("secret length out of range")
    if threshold < 1:
        raise ValueError("threshold must be >= 1")
    if not x_coords or len(x_coords) > MAX_SHARES:
        raise ValueError("x_coords count out of range")
    if len(set(x_coords)) != len(x_coords):
        raise ValueError("x_coords must be distinct")
    if any(not (0 <= x < PRIME) for x in x_coords):
        raise ValueError("x_coords out of field range")
    rng = random.Random(seed)
    polys = [[b] + [rng.randrange(PRIME) for _ in range(threshold - 1)]
             for b in secret]
    shares = []
    for k, x in enumerate(x_coords):
        ys = tuple(_poly_eval(p, x) for p in polys)
        shares.append(Share(index=start_index + k, batch_id=batch_id,
                            x=x, ys=ys))
    return shares


def make_descriptor(batch_id: str, threshold: int, x_coords: Sequence[int],
                    secret: bytes) -> BatchDescriptor:
    return BatchDescriptor(batch_id=batch_id, threshold=threshold,
                           x_coords=tuple(x_coords), length=len(secret),
                           sha256=hashlib.sha256(secret).hexdigest())


# ---------------------------------------------------------------------------
# Reconstruction
# ---------------------------------------------------------------------------

def _result(status, reason, diagnostics=(), secret=None, error_positions=()):
    return RecoveryResult(status=status, reason=reason, secret=secret,
                          error_positions=tuple(error_positions),
                          diagnostics=tuple(diagnostics))


def _inconsistent(reason, diagnostics=()):
    return _result(INCONSISTENT, reason, diagnostics)


def _well_formed(share: Share, length: int) -> bool:
    return (isinstance(share.index, int)
            and isinstance(share.x, int) and 0 <= share.x < PRIME
            and len(share.ys) == length
            and all(isinstance(y, int) and 0 <= y < PRIME for y in share.ys))


def recover(descriptor: BatchDescriptor, shares: Sequence[Share],
            max_errors: int) -> RecoveryResult:
    """Reconstruct the secret from ``shares`` under batch ``descriptor``.

    ``max_errors`` is the caller-declared upper bound e on erroneous share
    positions.  Never returns partial secret material on failure.
    """
    d = descriptor
    # --- trusted descriptor validation -----------------------------------
    if d.prime != PRIME:
        return _inconsistent("invalid_descriptor")
    if not isinstance(d.threshold, int) or d.threshold < 1:
        return _inconsistent("invalid_descriptor")
    if not (1 <= d.length <= MAX_LENGTH):
        return _inconsistent("invalid_descriptor")
    if not d.x_coords or len(d.x_coords) > MAX_SHARES:
        return _inconsistent("invalid_descriptor")
    if len(set(d.x_coords)) != len(d.x_coords):
        return _inconsistent("invalid_descriptor")
    if any(not isinstance(x, int) or not (0 <= x < PRIME) for x in d.x_coords):
        return _inconsistent("invalid_descriptor")
    if (not isinstance(d.sha256, str) or len(d.sha256) != 64
            or any(c not in "0123456789abcdefABCDEF" for c in d.sha256)):
        return _inconsistent("invalid_descriptor")
    if not isinstance(max_errors, int) or max_errors < 0:
        return _inconsistent("invalid_error_budget")

    shares = list(shares)
    if len(shares) > MAX_SHARES:
        return _inconsistent("too_many_shares")
    if not shares:
        return _result(INSUFFICIENT, "insufficient_distinct_coordinates")

    # --- per-share validation --------------------------------------------
    bad = [s.index for s in shares if not _well_formed(s, d.length)]
    if bad:
        return _inconsistent("invalid_share_format", sorted(bad))

    # batch mixing must be reported, never silently interpolated away
    bad = sorted(s.index for s in shares if s.batch_id != d.batch_id)
    if bad:
        return _inconsistent("batch_id_mismatch", bad)

    known_x = set(d.x_coords)
    bad = sorted(s.index for s in shares if s.x not in known_x)
    if bad:
        return _inconsistent("unknown_x_coordinate", bad)

    # --- duplicate coordinates: same value dedupes, conflict reports -----
    by_x = {}
    conflict = []
    for s in shares:
        first = by_x.get(s.x)
        if first is None:
            by_x[s.x] = s
        elif first.ys != s.ys:
            conflict.extend((first.index, s.index))
        # identical duplicate: keep first, drop the copy
    if conflict:
        return _inconsistent("conflicting_shares", sorted(set(conflict)))

    unique = list(by_x.values())
    need = d.threshold + 2 * max_errors
    if len(unique) < need:
        return _result(INSUFFICIENT, "insufficient_distinct_coordinates",
                       diagnostics=sorted(s.index for s in unique))

    # --- per-column Reed-Solomon decoding ---------------------------------
    xs = [s.x for s in unique]
    share_no = [s.index for s in unique]
    n = len(unique)
    max_err = min(max_errors, (n - d.threshold) // 2)
    xpow = [[pow(x, j, PRIME) for j in range(d.threshold + max_err + 1)]
            for x in xs]

    constants: List[int] = []
    union_errors = set()
    suspect: List[int] = []
    for col in range(d.length):
        ys = [s.ys[col] for s in unique]
        dec = _erasure_attempt(xs, ys, d.threshold, max_err, suspect, xpow)
        if dec is None:
            dec = _bw_decode(xs, ys, d.threshold, max_err, xpow)
        if dec is None:
            return _result(INSUFFICIENT,
                           "no_reconstruction_within_error_bound",
                           diagnostics=sorted(share_no))
        poly, errs = dec
        constants.append(poly[0] if poly else 0)
        if errs:
            union_errors.update(errs)
            suspect = sorted(union_errors)

    # --- post-checks: error budget, byte range, digest --------------------
    if len(union_errors) > max_errors:
        return _inconsistent("error_budget_exceeded",
                             sorted(share_no[i] for i in union_errors))
    if any(c > 255 for c in constants):
        return _inconsistent("constant_term_out_of_byte_range",
                             sorted(share_no[i] for i in union_errors))
    secret = bytes(constants)
    if hashlib.sha256(secret).hexdigest() != d.sha256.lower():
        return _inconsistent("digest_mismatch",
                             sorted(share_no[i] for i in union_errors))
    return _result(OK, "ok", secret=secret,
                   error_positions=sorted(share_no[i] for i in union_errors))
