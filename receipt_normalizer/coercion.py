"""Input coercion helpers.

The normalizer's preferred API uses the dataclasses in :mod:`model`, but local
fixtures and decoded JSON often use dictionaries.  These helpers accept both
without executing or importing untrusted input.
"""

from __future__ import annotations

from typing import Any, Mapping

from .model import ActionRecord, ImageSwitch


def _first_key(data: Mapping[str, Any], *names: str) -> tuple[bool, Any]:
    for name in names:
        if name in data and data[name] is not None:
            return True, data[name]
    return False, None


def _missing(label: str, *names: str) -> ValueError:
    return ValueError(f"{label} is missing required field; expected one of: {', '.join(names)}")


def coerce_action_record(value: Any) -> ActionRecord:
    if isinstance(value, ActionRecord):
        return value

    if isinstance(value, Mapping):
        found, process_instance = _first_key(
            value,
            "process_instance",
            "process_instance_id",
            "process",
            "instance",
            "proc",
        )
        if not found:
            raise _missing("ActionRecord", "process_instance", "process", "instance")

        found, source_seq = _first_key(
            value,
            "source_seq",
            "source_sequence",
            "seq",
            "sequence",
            "position",
            "audit_serial",
            "serial",
        )
        if not found:
            raise _missing("ActionRecord", "source_seq", "seq", "sequence")

        params = value.get("params", value.get("args", ()))
        action_code = value.get("action_code", value.get("code", value.get("entry")))
        return ActionRecord(
            process_instance=process_instance,
            source_seq=source_seq,
            params=params if params is not None else (),
            action_code=action_code,
        )

    if isinstance(value, (tuple, list)):
        if len(value) == 3:
            process_instance, source_seq, params = value
            return ActionRecord(process_instance, source_seq, params)
        if len(value) == 4:
            first, second, third, fourth = value
            # Accept both (pid, seq, code, params) and the dataclass order
            # (pid, seq, params, code), using container shape when possible.
            if _looks_like_params(fourth) and not _looks_like_params(third):
                return ActionRecord(first, second, fourth, third)
            return ActionRecord(first, second, third, fourth)

    raise TypeError(
        "action record must be an ActionRecord, mapping, or "
        "(process_instance, source_seq, params[/action_code]) tuple"
    )


def coerce_image_switch(value: Any) -> ImageSwitch:
    if isinstance(value, ImageSwitch):
        return value

    if isinstance(value, Mapping):
        found, process_instance = _first_key(
            value,
            "process_instance",
            "process_instance_id",
            "process",
            "instance",
            "proc",
        )
        if not found:
            raise _missing("ImageSwitch", "process_instance", "process", "instance")

        found, source_seq = _first_key(
            value,
            "source_seq",
            "source_sequence",
            "at_seq",
            "seq",
            "sequence",
            "effective_seq",
            "position",
        )
        if not found:
            raise _missing("ImageSwitch", "source_seq", "at_seq", "seq")

        found_image, image = _first_key(
            value,
            "image",
            "to_image",
            "new_image",
            "image_path",
            "exe",
            "executable",
        )
        abi = _first_key(value, "abi", "active_abi", "to_abi", "new_abi")[1]
        order = value.get("order", value.get("tie_order", 0))
        attributes = value.get("attributes", {})
        return ImageSwitch(
            process_instance=process_instance,
            source_seq=source_seq,
            image=image if found_image else None,
            abi=abi,
            order=order,
            attributes=attributes if isinstance(attributes, Mapping) else {"value": attributes},
        )

    if isinstance(value, (tuple, list)) and len(value) in (3, 4, 5):
        process_instance, source_seq, image = value[0], value[1], value[2]
        abi = value[3] if len(value) >= 4 else None
        order = value[4] if len(value) == 5 else 0
        return ImageSwitch(process_instance, source_seq, image, abi, order)

    raise TypeError("image switch must be an ImageSwitch, mapping, or (pi, seq, image, abi) tuple")


def _looks_like_params(value: Any) -> bool:
    return isinstance(value, (Mapping, list, tuple)) and not isinstance(value, (str, bytes, bytearray))
