"""Load topology configurations from plain dictionaries (e.g. JSON).

Configuration shape::

    {
      "ports": [
        {
          "device": "SW1",
          "port": "ge0/1",
          "native_vlan": 10,
          "tagged_ingress_vlans": [20],
          "egress": {"10": "untag", "20": {"tag": 200}},
          "description": "optional note"
        }
      ],
      "links": [
        ["SW1", "ge0/1", "SW2", "ge0/24"],
        {"a": ["SW1", "ge0/2"], "b": ["SW2", "ge0/23"]}
      ],
      "domain_pairs": [
        [["SW1", 10], ["SW2", 10]]
      ]
    }
"""

from __future__ import annotations

import json
from typing import Any, Dict

from .model import ConfigError, Link, PortConfig
from .topology import Topology


def port_ref_from_string(text: str) -> tuple:
    """Parse ``"SW1/ge0/1"`` into ``("SW1", "ge0/1")`` (split on first /)."""
    if not isinstance(text, str) or "/" not in text:
        raise ConfigError(f"port reference must look like 'device/port', got {text!r}")
    device, port = text.split("/", 1)
    if not device or not port:
        raise ConfigError(f"port reference must look like 'device/port', got {text!r}")
    return (device, port)


def device_ref_from_string(text: str) -> str:
    if not isinstance(text, str) or not text:
        raise ConfigError(f"device reference must be a non-empty string, got {text!r}")
    return text


def _load_port(entry: Dict[str, Any]) -> PortConfig:
    if not isinstance(entry, dict):
        raise ConfigError(f"port entry must be an object, got {entry!r}")
    try:
        device = entry["device"]
        name = entry["port"]
        native = entry["native_vlan"]
    except KeyError as exc:
        raise ConfigError(f"port entry missing field {exc}: {entry!r}") from None

    tagged = entry.get("tagged_ingress_vlans", [])
    if not isinstance(tagged, (list, tuple, set, frozenset)):
        raise ConfigError("tagged_ingress_vlans must be a list of VLAN ids")

    egress_raw = entry.get("egress")
    if not isinstance(egress_raw, dict):
        raise ConfigError(
            f"port {device}/{name} needs an 'egress' mapping "
            "{vlan_id: 'untag' | {'tag': vlan_id}}"
        )
    egress: Dict[int, Any] = {}
    for key, action in egress_raw.items():
        try:
            vid = int(key)
        except (TypeError, ValueError):
            raise ConfigError(
                f"port {device}/{name}: egress key must be a VLAN id, got {key!r}"
            ) from None
        egress[vid] = action

    return PortConfig(
        device=str(device),
        name=str(name),
        native_vlan=native,
        tagged_ingress_vlans=frozenset(tagged),
        egress=egress,
        description=str(entry.get("description", "")),
    )


def _load_link(entry: Any) -> Link:
    if isinstance(entry, dict):
        if "a" not in entry or "b" not in entry:
            raise ConfigError(f"link object needs 'a' and 'b': {entry!r}")
        return Link(a=tuple(entry["a"]), b=tuple(entry["b"]))
    if isinstance(entry, (list, tuple)) and len(entry) == 4:
        return Link(
            a=(str(entry[0]), str(entry[1])),
            b=(str(entry[2]), str(entry[3])),
        )
    raise ConfigError(
        "link must be [deviceA, portA, deviceB, portB] or "
        f"{{'a': [...], 'b': [...]}}, got {entry!r}"
    )


def load_config(config: Any) -> Topology:
    """Build a :class:`Topology` from a dictionary or a JSON string/path."""
    if isinstance(config, str):
        stripped = config.lstrip()
        if stripped.startswith("{"):
            try:
                config = json.loads(config)
            except json.JSONDecodeError as exc:
                raise ConfigError(f"invalid JSON configuration: {exc}") from None
        else:
            with open(config, "r", encoding="utf-8") as handle:
                try:
                    config = json.load(handle)
                except json.JSONDecodeError as exc:
                    raise ConfigError(f"invalid JSON in {config}: {exc}") from None

    if not isinstance(config, dict):
        raise ConfigError("top-level configuration must be an object")

    port_entries = config.get("ports")
    if not isinstance(port_entries, list) or not port_entries:
        raise ConfigError("configuration needs a non-empty 'ports' list")
    ports = [_load_port(entry) for entry in port_entries]

    links = [_load_link(entry) for entry in config.get("links", [])]
    pairs = config.get("domain_pairs", [])
    if not isinstance(pairs, list):
        raise ConfigError("domain_pairs must be a list of domain groups")

    return Topology(ports=ports, links=links, domain_pairs=pairs)
