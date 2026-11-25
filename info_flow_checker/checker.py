"""Exact information-flow checker with declassified summary outputs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .circuit import Circuit, circuit_from_dict
from .errors import PolicyValidationError
from .optimize import propagate_constants
from .policy import MAX_PUBLIC_BITS, MAX_SECRET_BITS, Policy
from .simulator import build_secret_masks, evaluate, simulate_secret_bitsets


@dataclass(frozen=True)
class FirstDifference:
    """The first external output bit on which a replayed pair differs."""

    index: int
    output: str
    value_a: bool
    value_b: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "output": self.output,
            "value_a": self.value_a,
            "value_b": self.value_b,
        }


@dataclass(frozen=True)
class Counterexample:
    """A deterministic, fully specified pair of replayable inputs."""

    public_index: int
    secret_index_a: int
    secret_index_b: int
    public_assignment: dict[str, bool]
    secret_assignment_a: dict[str, bool]
    secret_assignment_b: dict[str, bool]
    inputs_a: dict[str, bool]
    inputs_b: dict[str, bool]
    summary_a: dict[str, bool]
    summary_b: dict[str, bool]
    external_a: dict[str, bool]
    external_b: dict[str, bool]
    first_difference: FirstDifference

    @property
    def pair(self) -> tuple[dict[str, bool], dict[str, bool]]:
        return self.inputs_a, self.inputs_b

    def replay(self, circuit: Circuit | Mapping[str, Any]) -> "ReplayOutcome":
        return replay_counterexample(circuit, self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "public_index": self.public_index,
            "secret_index_a": self.secret_index_a,
            "secret_index_b": self.secret_index_b,
            "public_assignment": dict(self.public_assignment),
            "secret_assignment_a": dict(self.secret_assignment_a),
            "secret_assignment_b": dict(self.secret_assignment_b),
            "inputs_a": dict(self.inputs_a),
            "inputs_b": dict(self.inputs_b),
            "summary_a": dict(self.summary_a),
            "summary_b": dict(self.summary_b),
            "external_a": dict(self.external_a),
            "external_b": dict(self.external_b),
            "first_difference": self.first_difference.to_dict(),
        }


@dataclass(frozen=True)
class ReplayOutcome:
    """Result of re-evaluating a counterexample pair on a circuit."""

    outputs_a: dict[str, bool]
    outputs_b: dict[str, bool]
    same_public_inputs: bool
    same_approved_summary: bool
    external_distinguishable: bool
    first_difference: FirstDifference | None
    matches_recorded_pair: bool
    valid: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "outputs_a": dict(self.outputs_a),
            "outputs_b": dict(self.outputs_b),
            "same_public_inputs": self.same_public_inputs,
            "same_approved_summary": self.same_approved_summary,
            "external_distinguishable": self.external_distinguishable,
            "first_difference": (
                self.first_difference.to_dict() if self.first_difference else None
            ),
            "matches_recorded_pair": self.matches_recorded_pair,
            "valid": self.valid,
        }


@dataclass(frozen=True)
class SecurityReport:
    """Conclusion of an exhaustive check."""

    secure: bool
    circuit_name: str
    public_inputs: tuple[str, ...]
    secret_inputs: tuple[str, ...]
    summary_outputs: tuple[str, ...]
    external_outputs: tuple[str, ...]
    public_assignment_count: int
    secret_assignment_count: int
    public_assignments_checked: int
    leaking_public_assignment_count: int = 0
    constant_propagation_applied: bool = False
    counterexample: Counterexample | None = None

    def describe(self) -> str:
        scope = (
            f"checked {self.public_assignments_checked}/"
            f"{self.public_assignment_count} public assignments with "
            f"{self.secret_assignment_count} secret assignments each"
        )
        if self.secure:
            return f"SECURE: no secret pair is distinguishable; {scope}."
        assert self.counterexample is not None
        diff = self.counterexample.first_difference
        return (
            "LEAK: external outputs distinguish two secrets with identical "
            f"public inputs and approved summaries; first difference is "
            f"external bit {diff.index} ({diff.output!r}); {scope}."
        )

    def assert_secure(self) -> None:
        if not self.secure:
            raise AssertionError(self.describe())

    def to_dict(self) -> dict[str, Any]:
        return {
            "secure": self.secure,
            "status": "secure" if self.secure else "leak",
            "description": self.describe(),
            "circuit_name": self.circuit_name,
            "public_inputs": list(self.public_inputs),
            "secret_inputs": list(self.secret_inputs),
            "summary_outputs": list(self.summary_outputs),
            "external_outputs": list(self.external_outputs),
            "max_public_bits": MAX_PUBLIC_BITS,
            "max_secret_bits": MAX_SECRET_BITS,
            "public_assignment_count": self.public_assignment_count,
            "secret_assignment_count": self.secret_assignment_count,
            "public_assignments_checked": self.public_assignments_checked,
            "leaking_public_assignment_count": self.leaking_public_assignment_count,
            "constant_propagation_applied": self.constant_propagation_applied,
            "counterexample": (
                self.counterexample.to_dict() if self.counterexample is not None else None
            ),
        }


@dataclass(frozen=True)
class BoundPolicy:
    public_inputs: tuple[str, ...]
    secret_inputs: tuple[str, ...]
    summary_outputs: tuple[str, ...]
    external_outputs: tuple[str, ...] = field(default=())


def check_information_flow(
    circuit: Circuit | Mapping[str, Any],
    policy: Policy | Mapping[str, Any] | None = None,
    *,
    public_inputs: tuple[str, ...] | list[str] = (),
    secret_inputs: tuple[str, ...] | list[str] = (),
    summary_outputs: tuple[str, ...] | list[str] = (),
    external_outputs: tuple[str, ...] | list[str] | None = None,
    constant_propagation: bool = False,
) -> SecurityReport:
    """Exhaustively check non-interference modulo approved summaries.

    For every assignment to at most eight public inputs, all assignments to at
    most twelve secret inputs are represented in parallel with Python integer
    bitsets.  Secrets are grouped by their approved-summary value.  If two
    secrets in one summary group produce different external bits, the first
    deterministic pair is returned.
    """

    checked_circuit = circuit_from_dict(circuit)
    if policy is None:
        active_policy = Policy(
            public_inputs=public_inputs,
            secret_inputs=secret_inputs,
            summary_outputs=summary_outputs,
            external_outputs=external_outputs,
        )
    else:
        active_policy = Policy.from_mapping(policy)

    if constant_propagation:
        checked_circuit = propagate_constants(checked_circuit)

    bound = bind_policy(checked_circuit, active_policy)

    public_count = 1 << len(bound.public_inputs)
    secret_count = 1 << len(bound.secret_inputs)
    public_positions = {name: i for i, name in enumerate(bound.public_inputs)}
    output_nodes = checked_circuit.output_map()

    summary_nodes = [output_nodes[name] for name in bound.summary_outputs]
    external_nodes = [output_nodes[name] for name in bound.external_outputs]
    byte_length = (secret_count + 7) // 8
    secret_masks = build_secret_masks(bound.secret_inputs)

    first_counterexample: Counterexample | None = None
    leaking_public_count = 0

    for public_index in range(public_count):
        values, assignment_count, _ = simulate_secret_bitsets(
            checked_circuit,
            public_positions=public_positions,
            secret_names=bound.secret_inputs,
            public_index=public_index,
            secret_masks=secret_masks,
        )

        summary_bytes = [
            values[node_id].to_bytes(byte_length, byteorder="little")
            for node_id in summary_nodes
        ]
        external_bytes = [
            values[node_id].to_bytes(byte_length, byteorder="little")
            for node_id in external_nodes
        ]

        # summary code -> (first secret index in group, external code)
        groups: dict[int, tuple[int, int]] = {}
        public_pair: tuple[int, int, FirstDifference] | None = None

        for secret_index in range(assignment_count):
            byte_index = secret_index >> 3
            bit_mask = 1 << (secret_index & 7)

            summary_code = 0
            for position, packed in enumerate(summary_bytes):
                if packed[byte_index] & bit_mask:
                    summary_code |= 1 << position

            external_code = 0
            for position, packed in enumerate(external_bytes):
                if packed[byte_index] & bit_mask:
                    external_code |= 1 << position

            existing = groups.get(summary_code)
            if existing is None:
                groups[summary_code] = (secret_index, external_code)
                continue

            first_secret_index, first_external_code = existing
            if first_external_code == external_code:
                continue

            difference_mask = first_external_code ^ external_code
            difference_index = (difference_mask & -difference_mask).bit_length() - 1
            difference = FirstDifference(
                index=difference_index,
                output=bound.external_outputs[difference_index],
                value_a=bool((first_external_code >> difference_index) & 1),
                value_b=bool((external_code >> difference_index) & 1),
            )
            if public_pair is None:
                public_pair = (first_secret_index, secret_index, difference)

        if public_pair is not None:
            leaking_public_count += 1
            if first_counterexample is None:
                secret_a_index, secret_b_index, bit_difference = public_pair
                first_counterexample = _build_counterexample(
                    checked_circuit,
                    bound=bound,
                    public_index=public_index,
                    secret_a_index=secret_a_index,
                    secret_b_index=secret_b_index,
                    claimed_difference=bit_difference,
                )

    return SecurityReport(
        secure=first_counterexample is None,
        circuit_name=checked_circuit.name,
        public_inputs=bound.public_inputs,
        secret_inputs=bound.secret_inputs,
        summary_outputs=bound.summary_outputs,
        external_outputs=bound.external_outputs,
        public_assignment_count=public_count,
        secret_assignment_count=secret_count,
        public_assignments_checked=public_count,
        leaking_public_assignment_count=leaking_public_count,
        constant_propagation_applied=constant_propagation,
        counterexample=first_counterexample,
    )


def bind_policy(circuit: Circuit, policy: Policy) -> BoundPolicy:
    """Validate labels against a circuit and fill the default external list."""

    public = policy.public_inputs
    secret = policy.secret_inputs

    classified = set(public) | set(secret)
    input_set = set(circuit.inputs)
    missing_labels = input_set - classified
    unknown_labels = classified - input_set
    if missing_labels:
        raise PolicyValidationError(
            "every circuit input must be labeled public or secret; missing: "
            + ", ".join(sorted(missing_labels))
        )
    if unknown_labels:
        raise PolicyValidationError(
            "policy labels refer to unknown inputs: " + ", ".join(sorted(unknown_labels))
        )

    output_map = circuit.output_map()
    for kind, names in (
        ("summary_outputs", policy.summary_outputs),
        ("external_outputs", policy.external_outputs or ()),
    ):
        unknown = [name for name in names if name not in output_map]
        if unknown:
            raise PolicyValidationError(
                f"{kind} refers to unknown outputs {unknown!r}; "
                f"known outputs are {circuit.output_names!r}"
            )

    external = (
        circuit.output_names if policy.external_outputs is None else policy.external_outputs
    )
    return BoundPolicy(
        public_inputs=public,
        secret_inputs=secret,
        summary_outputs=policy.summary_outputs,
        external_outputs=external,
    )


def _decode_assignment(index: int, names: tuple[str, ...]) -> dict[str, bool]:
    # The first listed name is bit zero of the deterministic assignment index.
    return {name: bool((index >> position) & 1) for position, name in enumerate(names)}


def _first_difference(
    outputs_a: Mapping[str, bool],
    outputs_b: Mapping[str, bool],
    names: tuple[str, ...],
) -> FirstDifference | None:
    for index, name in enumerate(names):
        value_a = bool(outputs_a[name])
        value_b = bool(outputs_b[name])
        if value_a != value_b:
            return FirstDifference(index, name, value_a, value_b)
    return None


def _build_counterexample(
    circuit: Circuit,
    *,
    bound: BoundPolicy,
    public_index: int,
    secret_a_index: int,
    secret_b_index: int,
    claimed_difference: FirstDifference,
) -> Counterexample:
    public_assignment = _decode_assignment(public_index, bound.public_inputs)
    secret_a = _decode_assignment(secret_a_index, bound.secret_inputs)
    secret_b = _decode_assignment(secret_b_index, bound.secret_inputs)
    inputs_a = {**public_assignment, **secret_a}
    inputs_b = {**public_assignment, **secret_b}

    all_a = evaluate(circuit, inputs_a)
    all_b = evaluate(circuit, inputs_b)
    summary_a = {name: all_a[name] for name in bound.summary_outputs}
    summary_b = {name: all_b[name] for name in bound.summary_outputs}
    external_a = {name: all_a[name] for name in bound.external_outputs}
    external_b = {name: all_b[name] for name in bound.external_outputs}

    difference = _first_difference(external_a, external_b, bound.external_outputs)
    if difference is None:
        raise RuntimeError("internal checker error: bit-parallel mismatch did not replay")
    if difference != claimed_difference:
        raise RuntimeError(
            "internal checker error: replayed first difference "
            f"{difference!r} != simulated {claimed_difference!r}"
        )

    return Counterexample(
        public_index=public_index,
        secret_index_a=secret_a_index,
        secret_index_b=secret_b_index,
        public_assignment=public_assignment,
        secret_assignment_a=secret_a,
        secret_assignment_b=secret_b,
        inputs_a=inputs_a,
        inputs_b=inputs_b,
        summary_a=summary_a,
        summary_b=summary_b,
        external_a=external_a,
        external_b=external_b,
        first_difference=difference,
    )


def replay_counterexample(
    circuit: Circuit | Mapping[str, Any],
    counterexample: Counterexample,
) -> ReplayOutcome:
    """Re-evaluate both complete assignments and verify the recorded claim."""

    replayed_circuit = circuit_from_dict(circuit)
    outputs_a = evaluate(replayed_circuit, counterexample.inputs_a)
    outputs_b = evaluate(replayed_circuit, counterexample.inputs_b)

    public_names = tuple(counterexample.public_assignment)
    same_public = all(
        counterexample.inputs_a[name] == counterexample.inputs_b[name] for name in public_names
    )

    summary_names = tuple(counterexample.summary_a)
    same_summary = all(outputs_a[name] == outputs_b[name] for name in summary_names)
    recorded_summary_matches = all(
        outputs_a[name] == value for name, value in counterexample.summary_a.items()
    ) and all(
        outputs_b[name] == value for name, value in counterexample.summary_b.items()
    )

    external_names = tuple(counterexample.external_a)
    difference = _first_difference(outputs_a, outputs_b, external_names)

    recorded_values_match = all(
        outputs_a[name] == value for name, value in counterexample.external_a.items()
    ) and all(outputs_b[name] == value for name, value in counterexample.external_b.items())

    matches_recorded = (
        recorded_values_match
        and recorded_summary_matches
        and difference == counterexample.first_difference
    )
    valid = bool(
        same_public
        and same_summary
        and difference is not None
        and matches_recorded
    )

    return ReplayOutcome(
        outputs_a=outputs_a,
        outputs_b=outputs_b,
        same_public_inputs=same_public,
        same_approved_summary=same_summary,
        external_distinguishable=difference is not None,
        first_difference=difference,
        matches_recorded_pair=matches_recorded,
        valid=valid,
    )
