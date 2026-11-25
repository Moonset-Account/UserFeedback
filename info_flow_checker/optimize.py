"""Semantics-preserving constant propagation / folding for Boolean circuits."""

from __future__ import annotations

from .circuit import AND, CONST, MUX, NOT, OR, Circuit, CircuitBuilder
from .errors import CircuitValidationError

Resolved = bool | str


def propagate_constants(circuit: Circuit) -> Circuit:
    """Fold constants and apply local Boolean identities.

    The returned circuit preserves input names, output names/order, and all
    observable Boolean behavior.  Shared non-constant subgraphs remain shared:
    each original node is simplified once and referenced by id.
    """

    builder = CircuitBuilder(f"{circuit.name}__constant_propagated")
    for name in circuit.inputs:
        builder.input(name)

    constant_ids: dict[bool, str] = {}

    def constant_ref(value: bool) -> str:
        value = bool(value)
        if value not in constant_ids:
            # Reserve an id outside the readable ids normally chosen by users.
            base = f"__ifc_const_{'true' if value else 'false'}"
            candidate = base
            suffix = 1
            existing_ids = set(circuit.nodes) | set(builder._nodes)
            while candidate in existing_ids:
                candidate = f"{base}_{suffix}"
                suffix += 1
            builder.const(value, node_id=candidate)
            constant_ids[value] = candidate
        return constant_ids[value]

    mapping: dict[str, Resolved] = {name: name for name in circuit.inputs}

    for node_id in circuit.order:
        node = circuit.nodes[node_id]

        if node.op == "input":
            continue
        if node.op == CONST:
            if node.value is None:  # validated circuits prevent this
                raise CircuitValidationError(f"constant {node_id!r} has no value")
            # Map the wire to its Python value so downstream folds see the
            # propagated constant rather than only a constant node id.
            mapping[node_id] = node.value
            continue

        resolved_args = [mapping[arg] for arg in node.args]

        if node.op == AND:
            result: Resolved | None = None
            non_constant: list[str] = []
            for argument in resolved_args:
                if argument is False:
                    result = False
                    break
                if argument is True:
                    continue
                non_constant.append(argument)  # type: ignore[arg-type]
            if result is False:
                mapping[node_id] = False
                continue

            unique = list(dict.fromkeys(non_constant))
            if not unique:
                mapping[node_id] = True
            elif len(unique) == 1:
                mapping[node_id] = unique[0]
            else:
                mapping[node_id] = builder.and_(*unique, node_id=node_id)

        elif node.op == OR:
            result = None
            non_constant = []
            for argument in resolved_args:
                if argument is True:
                    result = True
                    break
                if argument is False:
                    continue
                non_constant.append(argument)  # type: ignore[arg-type]
            if result is True:
                mapping[node_id] = True
                continue

            unique = list(dict.fromkeys(non_constant))
            if not unique:
                mapping[node_id] = False
            elif len(unique) == 1:
                mapping[node_id] = unique[0]
            else:
                mapping[node_id] = builder.or_(*unique, node_id=node_id)

        elif node.op == NOT:
            argument = resolved_args[0]
            if isinstance(argument, bool):
                mapping[node_id] = not argument
                continue

            prior = builder._nodes.get(argument)
            if prior is not None and prior.op == NOT:
                # Double negation elimination.
                mapping[node_id] = prior.args[0]
            else:
                mapping[node_id] = builder.not_(argument, node_id=node_id)

        elif node.op == MUX:
            selector, true_branch, false_branch = resolved_args

            if isinstance(selector, bool):
                mapping[node_id] = true_branch if selector else false_branch
                continue

            if _same_resolved(true_branch, false_branch):
                mapping[node_id] = true_branch
                continue

            if true_branch is True and false_branch is False:
                mapping[node_id] = selector
                continue
            if true_branch is False and false_branch is True:
                mapping[node_id] = builder.not_(selector, node_id=node_id)
                continue

            selector_id = _as_node_ref(selector, constant_ref)
            true_id = _as_node_ref(true_branch, constant_ref)
            false_id = _as_node_ref(false_branch, constant_ref)
            mapping[node_id] = builder.mux(
                selector_id, true_id, false_id, node_id=node_id
            )
        else:  # pragma: no cover
            raise CircuitValidationError(f"unknown op {node.op!r}")

    output_refs: list[tuple[str, str]] = []
    for output in circuit.outputs:
        result = mapping[output.node]
        output_refs.append((output.name, _as_node_ref(result, constant_ref)))

    for name, node_ref in output_refs:
        builder.output(name, node_ref)
    built = builder.build()

    # Dead-code elimination after folding.  Inputs remain part of the circuit
    # interface even when no output currently uses them.
    reachable: set[str] = set()
    stack = [out.node for out in built.outputs] + list(built.inputs)
    while stack:
        node_id = stack.pop()
        if node_id in reachable:
            continue
        reachable.add(node_id)
        stack.extend(built.nodes[node_id].args)

    pruned_nodes = {
        node_id: built.nodes[node_id]
        for node_id in built.order
        if node_id in reachable
    }
    return Circuit.from_parts(
        inputs=built.inputs,
        nodes=pruned_nodes,
        outputs=built.outputs,
        name=built.name,
    )


def _same_resolved(left: Resolved, right: Resolved) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    return left == right


def _as_node_ref(value: Resolved, constant_ref) -> str:
    if isinstance(value, bool):
        return constant_ref(value)
    return value
