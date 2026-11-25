"""A small acyclic Boolean circuit model and builder.

Supported gates are AND, OR, NOT, and MUX.  A MUX node uses the argument
order ``(condition, value_when_true, value_when_false)``.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from .errors import CircuitValidationError

INPUT = "input"
CONST = "const"
AND = "and"
OR = "or"
NOT = "not"
MUX = "mux"

GATE_OPS = frozenset({AND, OR, NOT, MUX})
ALL_OPS = frozenset({INPUT, CONST, AND, OR, NOT, MUX})

_OP_ALIASES = {
    "input": INPUT,
    "in": INPUT,
    "const": CONST,
    "constant": CONST,
    "bool": CONST,
    "and": AND,
    "&": AND,
    "∧": AND,
    "or": OR,
    "|": OR,
    "∨": OR,
    "not": NOT,
    "!": NOT,
    "~": NOT,
    "¬": NOT,
    "mux": MUX,
    "ite": MUX,
    "if": MUX,
    "select": MUX,
}


@dataclass(frozen=True)
class Node:
    """One circuit node.

    ``value`` is only used for :data:`CONST` nodes.  MUX arguments are ordered
    as ``[condition, when_true, when_false]``.
    """

    id: str
    op: str
    args: tuple[str, ...] = ()
    value: bool | None = None


@dataclass(frozen=True)
class Output:
    """A named externally observable circuit output."""

    name: str
    node: str


@dataclass(frozen=True)
class Circuit:
    """Validated acyclic Boolean circuit."""

    inputs: tuple[str, ...]
    nodes: Mapping[str, Node]
    outputs: tuple[Output, ...]
    name: str = "circuit"
    order: tuple[str, ...] = field(default=(), compare=False)

    def __post_init__(self) -> None:
        nodes = {str(k): v for k, v in dict(self.nodes).items()}
        outputs = tuple(
            item if isinstance(item, Output) else Output(str(item[0]), str(item[1]))
            for item in self.outputs
        )
        order = self.order or topological_order(self.inputs, nodes, outputs)
        object.__setattr__(self, "inputs", tuple(self.inputs))
        object.__setattr__(self, "nodes", MappingProxyType(nodes))
        object.__setattr__(self, "outputs", outputs)
        object.__setattr__(self, "order", tuple(order))

    @classmethod
    def from_parts(
        cls,
        *,
        inputs: Sequence[str],
        nodes: Mapping[str, Node] | Sequence[Node],
        outputs: Sequence[Output | tuple[str, str]],
        name: str = "circuit",
    ) -> "Circuit":
        """Create and validate a circuit from Python parts."""

        input_tuple = tuple(inputs)
        if isinstance(nodes, Mapping):
            node_map = dict(nodes)
        else:
            node_map = {node.id: node for node in nodes}

        normalized: dict[str, Node] = {}
        for key, node in node_map.items():
            if not isinstance(node, Node):
                raise CircuitValidationError(
                    f"node {key!r} must be a Node instance, got {type(node).__name__}"
                )
            if key != node.id:
                raise CircuitValidationError(
                    f"node map key {key!r} does not match node id {node.id!r}"
                )
            value = node.value
            if node.op == CONST and value is not None and not isinstance(value, bool):
                if isinstance(value, int) and value in (0, 1):
                    value = bool(value)
                else:
                    raise CircuitValidationError(
                        f"constant node {node.id!r} has non-Boolean value {value!r}"
                    )
            normalized[node.id] = Node(
                id=node.id,
                op=node.op,
                args=tuple(node.args),
                value=value,
            )

        output_tuple = tuple(
            out if isinstance(out, Output) else Output(str(out[0]), str(out[1]))
            for out in outputs
        )
        order = topological_order(input_tuple, normalized, output_tuple)
        return cls(
            inputs=input_tuple,
            nodes=normalized,
            outputs=output_tuple,
            name=name,
            order=order,
        )

    @property
    def output_names(self) -> tuple[str, ...]:
        return tuple(out.name for out in self.outputs)

    def output_map(self) -> dict[str, str]:
        return {out.name: out.node for out in self.outputs}

    def output_node(self, name: str) -> str:
        for out in self.outputs:
            if out.name == name:
                return out.node
        raise KeyError(f"unknown output {name!r}; known outputs: {self.output_names!r}")


class CircuitBuilder:
    """Mutable builder that assigns deterministic default node ids."""

    def __init__(self, name: str = "circuit") -> None:
        self.name = name
        self._inputs: list[str] = []
        self._nodes: dict[str, Node] = {}
        self._outputs: list[Output] = []
        self._sequence = 0

    def _fresh_id(self, prefix: str) -> str:
        while True:
            candidate = f"{prefix}_{self._sequence}"
            self._sequence += 1
            if candidate not in self._nodes:
                return candidate

    def _insert(
        self,
        op: str,
        args: Sequence[str],
        node_id: str | None,
        value: bool | None = None,
    ) -> str:
        if node_id is None:
            node_id = self._fresh_id(op)
        if not isinstance(node_id, str) or not node_id:
            raise CircuitValidationError(f"node id must be a non-empty string, got {node_id!r}")
        if node_id in self._nodes:
            raise CircuitValidationError(f"duplicate node id {node_id!r}")
        arg_tuple = tuple(args)
        for arg in arg_tuple:
            if arg not in self._nodes:
                raise CircuitValidationError(
                    f"node {node_id!r} refers to undefined node {arg!r}"
                )
        self._nodes[node_id] = Node(node_id, op, arg_tuple, value)
        return node_id

    def input(self, name: str, *, node_id: str | None = None) -> str:
        if not isinstance(name, str) or not name:
            raise CircuitValidationError(f"input name must be a non-empty string, got {name!r}")
        nid = node_id or name
        if nid in self._nodes or name in self._inputs:
            raise CircuitValidationError(f"duplicate input/node name {name!r}")
        self._inputs.append(name)
        # Inputs have no dependencies; bypass the reference check by inserting
        # directly rather than calling _insert().
        self._nodes[nid] = Node(nid, INPUT, (), None)
        if nid != name:
            # The common and documented form uses the input name as node id.
            # Keeping an alias would complicate assignment lookup, so reject
            # divergence for now.
            raise CircuitValidationError("an input node id must equal its input name")
        return nid

    def const(self, value: bool | int, *, node_id: str | None = None) -> str:
        if not isinstance(value, bool):
            if isinstance(value, int) and value in (0, 1):
                value = bool(value)
            else:
                raise CircuitValidationError(f"constant value must be Boolean, got {value!r}")
        return self._insert(CONST, (), node_id, bool(value))

    def and_(self, *args: str, node_id: str | None = None) -> str:
        if len(args) < 1:
            raise CircuitValidationError("AND requires at least one argument")
        return self._insert(AND, args, node_id)

    def or_(self, *args: str, node_id: str | None = None) -> str:
        if len(args) < 1:
            raise CircuitValidationError("OR requires at least one argument")
        return self._insert(OR, args, node_id)

    def not_(self, arg: str, *, node_id: str | None = None) -> str:
        return self._insert(NOT, (arg,), node_id)

    def mux(
        self,
        condition: str,
        when_true: str,
        when_false: str,
        *,
        node_id: str | None = None,
    ) -> str:
        return self._insert(MUX, (condition, when_true, when_false), node_id)

    def output(self, name: str, node_id: str) -> None:
        if not isinstance(name, str) or not name:
            raise CircuitValidationError(f"output name must be non-empty, got {name!r}")
        if any(out.name == name for out in self._outputs):
            raise CircuitValidationError(f"duplicate output name {name!r}")
        if node_id not in self._nodes:
            raise CircuitValidationError(f"output {name!r} refers to unknown node {node_id!r}")
        self._outputs.append(Output(name, node_id))

    def build(self) -> Circuit:
        return Circuit.from_parts(
            inputs=self._inputs,
            nodes=self._nodes,
            outputs=self._outputs,
            name=self.name,
        )


def normalize_op(op: Any) -> str:
    if not isinstance(op, str):
        raise CircuitValidationError(f"operation must be a string, got {op!r}")
    normalized = _OP_ALIASES.get(op)
    if normalized is None:
        normalized = _OP_ALIASES.get(op.lower())
    if normalized is None:
        raise CircuitValidationError(f"unsupported operation {op!r}")
    return normalized


def topological_order(
    inputs: Sequence[str],
    nodes: Mapping[str, Node],
    outputs: Sequence[Output | tuple[str, str]],
) -> tuple[str, ...]:
    """Validate the graph and return a deterministic topological order."""

    if not isinstance(nodes, Mapping):
        raise CircuitValidationError("nodes must be a mapping of id to Node")

    seen_inputs: set[str] = set()
    input_tuple = tuple(inputs)
    for name in input_tuple:
        if not isinstance(name, str) or not name:
            raise CircuitValidationError(f"input names must be non-empty strings: {name!r}")
        if name in seen_inputs:
            raise CircuitValidationError(f"duplicate input {name!r}")
        seen_inputs.add(name)
        if name not in nodes:
            raise CircuitValidationError(f"input {name!r} has no corresponding node")
        if nodes[name].op != INPUT:
            raise CircuitValidationError(f"input {name!r} must use the input operation")

    indegree: dict[str, int] = {}
    children: dict[str, list[str]] = {nid: [] for nid in nodes}

    for nid, node in nodes.items():
        if not isinstance(node, Node):
            raise CircuitValidationError(f"node {nid!r} is not a Node")
        if node.id != nid:
            raise CircuitValidationError(f"node id {node.id!r} differs from mapping key {nid!r}")
        if node.op not in ALL_OPS:
            raise CircuitValidationError(f"node {nid!r} has unsupported op {node.op!r}")

        args = tuple(node.args)
        if node.op in (INPUT, CONST):
            if args:
                raise CircuitValidationError(f"{node.op} node {nid!r} cannot have arguments")
        elif node.op == NOT:
            if len(args) != 1:
                raise CircuitValidationError(f"NOT node {nid!r} requires exactly one argument")
        elif node.op == MUX:
            if len(args) != 3:
                raise CircuitValidationError(f"MUX node {nid!r} requires exactly three arguments")
        elif node.op in (AND, OR):
            if len(args) < 1:
                raise CircuitValidationError(f"{node.op.upper()} node {nid!r} needs an argument")

        if node.op == CONST and not isinstance(node.value, bool):
            raise CircuitValidationError(f"constant node {nid!r} requires a Boolean value")

        unique_deps: list[str] = []
        seen_deps: set[str] = set()
        for arg in args:
            if not isinstance(arg, str) or arg not in nodes:
                raise CircuitValidationError(
                    f"node {nid!r} refers to undefined node {arg!r}"
                )
            if arg == nid:
                raise CircuitValidationError(f"node {nid!r} directly depends on itself")
            if arg not in seen_deps:
                seen_deps.add(arg)
                unique_deps.append(arg)
                children[arg].append(nid)
        indegree[nid] = len(unique_deps)

    output_names: set[str] = set()
    for item in outputs:
        if isinstance(item, Output):
            name, node_id = item.name, item.node
        else:
            name, node_id = item
        if not isinstance(name, str) or not name:
            raise CircuitValidationError(f"output name must be non-empty: {name!r}")
        if name in output_names:
            raise CircuitValidationError(f"duplicate output name {name!r}")
        output_names.add(name)
        if node_id not in nodes:
            raise CircuitValidationError(f"output {name!r} refers to unknown node {node_id!r}")

    ready = deque(nid for nid in nodes if indegree[nid] == 0)
    order: list[str] = []
    while ready:
        nid = ready.popleft()
        order.append(nid)
        for child in children[nid]:
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)

    if len(order) != len(nodes):
        unresolved = [nid for nid in nodes if indegree[nid] > 0]
        raise CircuitValidationError(
            "circuit must be acyclic; unresolved/cyclic nodes: " + ", ".join(unresolved[:10])
        )
    return tuple(order)


def circuit_from_dict(data: Mapping[str, Any] | Circuit) -> Circuit:
    """Build a :class:`Circuit` from a JSON-compatible dictionary.

    Minimal format::

        {
          "name": "example",
          "inputs": ["p", "s"],
          "nodes": [
            {"id": "g", "op": "and", "args": ["p", "s"]}
          ],
          "outputs": {"decision": "g"}
        }
    """

    if isinstance(data, Circuit):
        return data
    if not isinstance(data, Mapping):
        raise TypeError(f"circuit definition must be a mapping, got {type(data).__name__}")

    builder = CircuitBuilder(str(data.get("name", "circuit")))

    raw_inputs = data.get("inputs", ())
    if isinstance(raw_inputs, str):
        raw_inputs = [raw_inputs]
    for item in raw_inputs:
        if isinstance(item, str):
            name = item
        elif isinstance(item, Mapping):
            name = item.get("name") or item.get("id")
        else:
            raise CircuitValidationError(f"invalid input descriptor {item!r}")
        builder.input(str(name))

    raw_nodes = data.get("nodes", ())
    if isinstance(raw_nodes, Mapping):
        node_iter = [dict(value, id=key) if isinstance(value, Mapping) else value
                     for key, value in raw_nodes.items()]
    elif isinstance(raw_nodes, Sequence) and not isinstance(raw_nodes, (str, bytes)):
        node_iter = raw_nodes
    else:
        raise CircuitValidationError("nodes must be a list or mapping")

    for item in node_iter:
        if isinstance(item, Node):
            raw = item
            nid = raw.id
            op = normalize_op(raw.op)
            args = list(raw.args)
            value = raw.value
        elif isinstance(item, Mapping):
            nid = item.get("id") or item.get("name")
            if nid is None:
                raise CircuitValidationError(f"node is missing id: {item!r}")
            op = normalize_op(item.get("op", item.get("operation")))
            args = item.get("args", item.get("inputs", item.get("arguments", ())))
            if isinstance(args, str):
                args = [args]
            elif not isinstance(args, Sequence):
                raise CircuitValidationError(f"node {nid!r} args must be a sequence")
            value = item.get("value", item.get("v"))
            if op == NOT and not args and "arg" in item:
                args = [item["arg"]]
            if op == MUX and not args:
                args = [item[key] for key in ("condition", "when_true", "when_false")
                        if key in item]
        elif isinstance(item, Sequence) and len(item) >= 2:
            # Compact form: [id, op, args, value?]
            nid, op = item[0], normalize_op(item[1])
            args = item[2] if len(item) >= 3 else ()
            if isinstance(args, str):
                args = [args]
            value = item[3] if len(item) >= 4 else None
        else:
            raise CircuitValidationError(f"invalid node descriptor {item!r}")

        nid = str(nid)
        if op == INPUT:
            if nid not in builder._nodes:
                builder.input(nid)
        elif op == CONST:
            builder.const(True if value is None else value, node_id=nid)
        elif op == AND:
            builder.and_(*[str(a) for a in args], node_id=nid)
        elif op == OR:
            builder.or_(*[str(a) for a in args], node_id=nid)
        elif op == NOT:
            builder.not_(str(args[0]), node_id=nid)
        elif op == MUX:
            if len(args) != 3:
                raise CircuitValidationError(f"MUX node {nid!r} needs three args")
            builder.mux(str(args[0]), str(args[1]), str(args[2]), node_id=nid)

    raw_outputs = data.get("outputs", ())
    if isinstance(raw_outputs, Mapping):
        output_items = list(raw_outputs.items())
    elif isinstance(raw_outputs, Sequence) and not isinstance(raw_outputs, (str, bytes)):
        output_items = []
        for item in raw_outputs:
            if isinstance(item, str):
                output_items.append((item, item))
            elif isinstance(item, Mapping):
                name = item.get("name")
                node_ref = item.get("node", item.get("from", item.get("id")))
                if name is None or node_ref is None:
                    raise CircuitValidationError(f"invalid output descriptor {item!r}")
                output_items.append((name, node_ref))
            else:
                raise CircuitValidationError(f"invalid output descriptor {item!r}")
    else:
        raise CircuitValidationError("outputs must be a mapping or list")

    for name, node_ref in output_items:
        if isinstance(node_ref, Mapping):
            node_ref = node_ref.get("node", node_ref.get("from", node_ref.get("id")))
        builder.output(str(name), str(node_ref))

    return builder.build()


def circuit_to_dict(circuit: Circuit) -> dict[str, Any]:
    """Serialize a circuit to a JSON-compatible dictionary."""

    return {
        "name": circuit.name,
        "inputs": list(circuit.inputs),
        "nodes": [
            {
                "id": node.id,
                "op": node.op,
                "args": list(node.args),
                **({"value": node.value} if node.op == CONST else {}),
            }
            for node_id in circuit.order
            for node in [circuit.nodes[node_id]]
        ],
        "outputs": {out.name: out.node for out in circuit.outputs},
    }
