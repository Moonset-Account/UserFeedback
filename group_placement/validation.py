"""Input validation for the group-placement module."""

from __future__ import annotations

from .models import Container, Node, Pod

MAX_PODS = 8
MAX_NODES = 6


class PlacementInputError(ValueError):
    """Raised when the Pod/Node input data is internally inconsistent."""


def _require_number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PlacementInputError(f"{field} must be a number, got {value!r}")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise PlacementInputError(f"{field} must be finite, got {value!r}")
    if number < 0:
        raise PlacementInputError(f"{field} must be non-negative, got {value!r}")
    return number


def validate(pods: list[Pod], nodes: list[Node]) -> None:
    """Validate structural and numeric constraints of a placement request."""

    if not isinstance(pods, (list, tuple)):
        raise PlacementInputError("pods must be a list or tuple")
    if not isinstance(nodes, (list, tuple)):
        raise PlacementInputError("nodes must be a list or tuple")
    if len(pods) > MAX_PODS:
        raise PlacementInputError(
            f"at most {MAX_PODS} pods per group, got {len(pods)}"
        )
    if len(nodes) > MAX_NODES:
        raise PlacementInputError(
            f"at most {MAX_NODES} nodes, got {len(nodes)}"
        )

    node_names: set[str] = set()
    for node in nodes:
        if not isinstance(node, Node):
            raise PlacementInputError("every node must be a Node instance")
        if node.name in node_names:
            raise PlacementInputError(f"duplicate node name: {node.name!r}")
        node_names.add(node.name)
        _require_number(node.cpu_available, f"node {node.name!r} cpu_available")
        _require_number(
            node.memory_available, f"node {node.name!r} memory_available"
        )

    pod_names: set[str] = set()
    for pod in pods:
        if not isinstance(pod, Pod):
            raise PlacementInputError("every pod must be a Pod instance")
        if pod.name in pod_names:
            raise PlacementInputError(f"duplicate pod name: {pod.name!r}")
        pod_names.add(pod.name)
        _require_number(pod.cpu_overhead, f"pod {pod.name!r} cpu_overhead")
        _require_number(
            pod.memory_overhead, f"pod {pod.name!r} memory_overhead"
        )

        container_names: set[str] = set()
        all_containers = list(pod.inits) + list(pod.apps)
        for container in all_containers:
            if not isinstance(container, Container):
                raise PlacementInputError(
                    f"pod {pod.name!r}: every container must be a Container"
                )
            if container.name in container_names:
                raise PlacementInputError(
                    f"pod {pod.name!r}: duplicate container name "
                    f"{container.name!r}"
                )
            container_names.add(container.name)
            _require_number(
                container.cpu,
                f"pod {pod.name!r} container {container.name!r} cpu",
            )
            _require_number(
                container.memory,
                f"pod {pod.name!r} container {container.name!r} memory",
            )
            if container.kind not in ("init", "app"):
                raise PlacementInputError(
                    f"pod {pod.name!r} container {container.name!r}: "
                    f"kind must be 'init' or 'app', got {container.kind!r}"
                )
            if container.kind == "app" and container.resident:
                raise PlacementInputError(
                    f"pod {pod.name!r} container {container.name!r}: "
                    "app containers cannot be marked resident"
                )

        for node_name in pod.eligible_nodes:
            if node_name not in node_names:
                raise PlacementInputError(
                    f"pod {pod.name!r}: eligible node {node_name!r} "
                    "does not exist"
                )
