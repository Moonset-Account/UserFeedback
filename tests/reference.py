"""Independent reference implementation used only by the tests.

Nothing here imports ``ticket_check``.  It is a deliberately plain, dense
GF(2) Gaussian elimination so that the production analyser is checked against
something written from a different direction, not against itself.
"""


def solve(n_cells, equations):
    """Solve a GF(2) system.

    ``equations`` is an iterable of ``(cell_iterable, parity)``.
    Returns ``(consistent, rank, particular, nullspace_basis)`` where
    ``particular`` is one solution bit mask and ``nullspace_basis`` a list of
    bit masks spanning the differences between solutions.
    """
    rows = []
    for cells, parity in equations:
        mask = 0
        for cell in cells:
            if not 0 <= cell < n_cells:
                raise ValueError(f"cell {cell} outside 0..{n_cells - 1}")
            mask ^= 1 << cell
        rows.append((mask, parity))

    pivot_rows = {}  # leading bit -> (row mask, rhs)
    for mask, parity in rows:
        while mask:
            bit = mask.bit_length() - 1
            if bit in pivot_rows:
                other_mask, other_parity = pivot_rows[bit]
                mask ^= other_mask
                parity ^= other_parity
            else:
                pivot_rows[bit] = (mask, parity)
                break
        else:
            # Reduced to zero without becoming a pivot: a combination of the
            # earlier rows.  It is only acceptable when its rhs also vanished.
            if parity:
                return False, None, None, None

    pivot_bits = sorted(pivot_rows)
    # Each stored row contains no bits above its leading bit, so solving in
    # ascending bit order lets every row use already-resolved lower bits.
    particular = 0
    for bit in pivot_bits:
        mask, parity = pivot_rows[bit]
        rest = mask & ~(1 << bit)
        value = parity ^ ((rest & particular).bit_count() & 1)
        if value:
            particular |= 1 << bit

    free_bits = [bit for bit in range(n_cells) if bit not in pivot_rows]
    basis = []
    for free in free_bits:
        vector = 1 << free
        for bit in pivot_bits:
            mask, _ = pivot_rows[bit]
            rest = mask & ~(1 << bit)
            if (rest & vector).bit_count() & 1:
                vector |= 1 << bit
        basis.append(vector)

    return True, len(pivot_rows), particular, basis


def forced_and_free(n_cells, equations, observed_cells):
    """Return (``{cell: forced value}``, ``[cells still free]``).

    A cell is forced when its unit vector has zero coordinates in every
    nullspace basis vector.  Observed cells are excluded from both results.
    """
    consistent, _, particular, basis = solve(n_cells, equations)
    if not consistent:
        return None, None
    forced = {}
    still_free = []
    for cell in range(n_cells):
        if cell in observed_cells:
            continue
        coordinates = [(vector >> cell) & 1 for vector in basis]
        if any(coordinates):
            still_free.append(cell)
        else:
            forced[cell] = (particular >> cell) & 1
    return forced, still_free


def is_satisfiable(n_cells, items):
    """Whether a set of certificate items can all hold at once."""
    equations = [(item.cells, item.value) for item in items]
    consistent, *_ = solve(n_cells, equations)
    return consistent
