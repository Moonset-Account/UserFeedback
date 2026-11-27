"""Integer convolution reconstruction from modular residue blocks.

The reconstructor merges convolution results submitted by independent
workers, where every worker computed the convolution modulo its own
integer.  Moduli are *not* required to be pairwise coprime: merging uses
the generalized Chinese Remainder Theorem, and the information carried by
each new modulus is measured through prime-power factors so that a factor
already supplied by another modulus is never counted twice.

A coefficient at output position ``k`` can only be published when the
effective modulus (the lcm of all accepted moduli, restricted to the
factors observed at that position) is strictly larger than ``2 * B_k``.
Under that condition exactly one signed integer in ``[-B_k, B_k]`` is
consistent with the accumulated congruences.
"""

from .reconstruct import (
    BlockReport,
    ConflictRecord,
    CongruenceConflict,
    ConvolutionReconstructor,
    PositionState,
    coefficient_bound,
    coefficient_bounds,
    direct_convolution,
    factorize,
    merge_congruence,
    prime_power_lcm,
    reconstruct_convolution,
    signed_residue,
    term_count,
)

__all__ = [
    "BlockReport",
    "ConflictRecord",
    "CongruenceConflict",
    "ConvolutionReconstructor",
    "PositionState",
    "coefficient_bound",
    "coefficient_bounds",
    "direct_convolution",
    "factorize",
    "merge_congruence",
    "prime_power_lcm",
    "reconstruct_convolution",
    "signed_residue",
    "term_count",
]
