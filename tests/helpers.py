"""Shared topology builders for the unittest acceptance suite."""

from vlancheck.model import Link, PortConfig
from vlancheck.topology import Topology


def make_port(device, name, native_vlan, tagged, egress, description=""):
    """Concise PortConfig constructor.

    ``tagged`` is a list of accepted ingress tag ids.
    ``egress`` maps local vlan id -> "untag" or ("tag", id).
    """
    normalized_egress = {}
    for vid, action in egress.items():
        if action == "untag":
            normalized_egress[vid] = ("untag",)
        elif isinstance(action, list):
            normalized_egress[vid] = tuple(action)
        else:
            normalized_egress[vid] = action
    return PortConfig(
        device=device,
        name=name,
        native_vlan=native_vlan,
        tagged_ingress_vlans=frozenset(tagged),
        egress=normalized_egress,
        description=description,
    )


def make_link(a_device, a_port, b_device, b_port):
    return Link(a=(a_device, a_port), b=(b_device, b_port))


def make_topology(ports, links, pairs=()):
    return Topology(ports=ports, links=links, domain_pairs=pairs)
