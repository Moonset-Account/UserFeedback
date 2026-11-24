"""Core data model and strict JSON parsing for the adaptive join planner.

The model is intentionally small and explicit: table subsets are bit masks,
scenario cardinalities are supplied by the caller, and all fees/memory
requirements are integers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

PYTHON_VERSION = (3, 11)


class PlannerError(ValueError):
    """Raised when a problem definition or strategy is invalid."""


# ---------------------------------------------------------------------------
# Primitive validation helpers
# ---------------------------------------------------------------------------


def is_plain_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def require_int(value: Any, what: str, *, minimum: int = 0) -> int:
    if not is_plain_int(value):
        raise PlannerError(f"{what} must be an integer, got {type(value).__name__}")
    if value < minimum:
        raise PlannerError(f"{what} must be >= {minimum}, got {value}")
    return value


def require_str(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise PlannerError(f"{what} must be a non-empty string")
    return value


def as_tuple_str(value: Any, what: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, (list, tuple)):
        raise PlannerError(f"{what} must be a string or list of strings")
    out: list[str] = []
    for i, item in enumerate(value):
        out.append(require_str(item, f"{what}[{i}]"))
    return tuple(out)


def opt_bool(value: Any, what: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise PlannerError(f"{what} must be a boolean")
    return value


def ordering_covers(ordering: tuple[str, ...], required: tuple[str, ...]) -> bool:
    """Return True if ``ordering`` has ``required`` as its leading prefix."""

    if not required:
        return True
    return tuple(ordering[: len(required)]) == tuple(required)


# ---------------------------------------------------------------------------
# Cost tables
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CostRule:
    cost: int | None = None
    memory: int | None = None
    min_left: int | None = None
    max_left: int | None = None
    min_right: int | None = None
    max_right: int | None = None
    min_output: int | None = None
    max_output: int | None = None
    label: str = ""

    def matches(self, left_rows: int, right_rows: int, output_rows: int) -> bool:
        values = (
            ("left", left_rows, self.min_left, self.max_left),
            ("right", right_rows, self.min_right, self.max_right),
            ("output", output_rows, self.min_output, self.max_output),
        )
        for _name, value, lo, hi in values:
            if lo is not None and value < lo:
                return False
            if hi is not None and value > hi:
                return False
        return True


@dataclass(frozen=True)
class CostPoint:
    cost: int
    memory: int


@dataclass(frozen=True)
class CostTable:
    """Integer join fee table with affine fallback and range/exact rules.

    The default fee is::

        fixed + per_left_row*l + per_right_row*r + per_output_row*o

    A matching rule may override ``cost`` and/or ``memory``.  Rules are tried
    in input order; the first matching rule wins.
    """

    name: str
    fixed: int = 0
    per_left_row: int = 0
    per_right_row: int = 0
    per_output_row: int = 0
    memory_fixed: int = 0
    memory_per_left_row: int = 0
    memory_per_right_row: int = 0
    memory_per_output_row: int = 0
    default_cost: int | None = None
    default_memory: int | None = None
    rules: tuple[CostRule, ...] = ()

    def lookup(self, left_rows: int, right_rows: int, output_rows: int) -> CostPoint:
        cost = (
            self.fixed
            + self.per_left_row * left_rows
            + self.per_right_row * right_rows
            + self.per_output_row * output_rows
        )
        memory = (
            self.memory_fixed
            + self.memory_per_left_row * left_rows
            + self.memory_per_right_row * right_rows
            + self.memory_per_output_row * output_rows
        )

        matched: CostRule | None = None
        for rule in self.rules:
            if rule.matches(left_rows, right_rows, output_rows):
                matched = rule
                break

        if matched is not None:
            if matched.cost is not None:
                cost = matched.cost
            if matched.memory is not None:
                memory = matched.memory
        else:
            if self.default_cost is not None:
                cost = self.default_cost
            if self.default_memory is not None:
                memory = self.default_memory

        if cost < 0 or memory < 0:
            raise PlannerError(f"{self.name} cost table produced negative cost/memory")
        return CostPoint(cost=cost, memory=memory)


@dataclass(frozen=True)
class RowCost:
    """Integer per-subset fee, used for materialization and explicit sorts."""

    fixed: int = 0
    per_row: int = 0
    memory_fixed: int = 0
    memory_per_row: int = 0

    def cost(self, rows: int) -> int:
        return self.fixed + self.per_row * rows

    def memory(self, rows: int) -> int:
        return self.memory_fixed + self.memory_per_row * rows


def _parse_cost_rule(data: dict[str, Any], path: str) -> CostRule:
    if not isinstance(data, dict):
        raise PlannerError(f"{path} must be an object")

    def opt_int(*keys: str) -> int | None:
        for key in keys:
            if key in data and data[key] is not None:
                return require_int(data[key], f"{path}.{key}")
        return None

    exact_l = opt_int("left_rows", "left")
    exact_r = opt_int("right_rows", "right")
    exact_o = opt_int("output_rows", "output")
    min_l = opt_int("min_left", "min_left_rows")
    max_l = opt_int("max_left", "max_left_rows")
    min_r = opt_int("min_right", "min_right_rows")
    max_r = opt_int("max_right", "max_right_rows")
    min_o = opt_int("min_output", "min_output_rows")
    max_o = opt_int("max_output", "max_output_rows")
    if exact_l is not None:
        min_l = max_l = exact_l
    if exact_r is not None:
        min_r = max_r = exact_r
    if exact_o is not None:
        min_o = max_o = exact_o

    cost = opt_int("cost", "fee")
    memory = opt_int("memory", "memory_cost")
    label = str(data.get("label", ""))
    return CostRule(
        cost=cost,
        memory=memory,
        min_left=min_l,
        max_left=max_l,
        min_right=min_r,
        max_right=max_r,
        min_output=min_o,
        max_output=max_o,
        label=label,
    )


def parse_cost_table(value: Any, name: str) -> CostTable:
    if value is None:
        # A missing algorithm is still represented by a prohibitively expensive
        # table?  Better: callers that want an algorithm must configure it.
        # We use a very large finite default so JSON remains concise; feasibility
        # and optimality remain exact for ordinary test inputs.
        return CostTable(name=name, default_cost=10**18, default_memory=0)
    if is_plain_int(value):
        return CostTable(name=name, default_cost=require_int(value, name))
    if isinstance(value, list):
        value = {"rules": value}
    if not isinstance(value, dict):
        raise PlannerError(f"{name} must be an integer, rule list, or cost-table object")

    def integer_field(key: str, default: int = 0) -> int:
        if key not in value or value[key] is None:
            return default
        return require_int(value[key], f"{name}.{key}")

    default_cost = value.get("default_cost", value.get("default", None))
    if default_cost is not None:
        default_cost = require_int(default_cost, f"{name}.default_cost")
    default_memory = value.get("default_memory", value.get("memory_limit_cost", None))
    if default_memory is not None:
        default_memory = require_int(default_memory, f"{name}.default_memory")

    rules_raw = value.get("rules", value.get("entries", ()))
    if not isinstance(rules_raw, (list, tuple)):
        raise PlannerError(f"{name}.rules must be a list")
    rules = tuple(_parse_cost_rule(r, f"{name}.rules[{i}]") for i, r in enumerate(rules_raw))

    return CostTable(
        name=name,
        fixed=integer_field("fixed"),
        per_left_row=integer_field("per_left_row", integer_field("left_per_row")),
        per_right_row=integer_field("per_right_row", integer_field("right_per_row")),
        per_output_row=integer_field("per_output_row", integer_field("output_per_row")),
        memory_fixed=integer_field("memory_fixed"),
        memory_per_left_row=integer_field("memory_per_left_row"),
        memory_per_right_row=integer_field("memory_per_right_row"),
        memory_per_output_row=integer_field("memory_per_output_row"),
        default_cost=default_cost,
        default_memory=default_memory,
        rules=rules,
    )


def parse_row_cost(value: Any, name: str, *, required: bool = False) -> RowCost | None:
    if value is None:
        if required:
            raise PlannerError(f"{name} is required")
        return None
    if is_plain_int(value):
        return RowCost(fixed=require_int(value, name))
    if not isinstance(value, dict):
        raise PlannerError(f"{name} must be an integer or object")
    return RowCost(
        fixed=require_int(value.get("fixed", 0), f"{name}.fixed"),
        per_row=require_int(value.get("per_row", value.get("cost_per_row", 0)), f"{name}.per_row"),
        memory_fixed=require_int(value.get("memory_fixed", 0), f"{name}.memory_fixed"),
        memory_per_row=require_int(
            value.get("memory_per_row", 0), f"{name}.memory_per_row"
        ),
    )


# ---------------------------------------------------------------------------
# Structural definitions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TableSpec:
    name: str
    ordering: tuple[str, ...] = ()
    materialized: bool = False
    memory_limit: int | None = None
    reuse_order: bool = True
    reuse_materialization: bool = True


@dataclass(frozen=True)
class Block:
    """A current frontier output covering a disjoint set of tables."""

    mask: int
    ordering: tuple[str, ...] = ()
    materialized: bool = False
    memory_limit: int | None = None
    reuse_order: bool = True


@dataclass(frozen=True)
class Edge:
    eid: str
    left_name: str
    right_name: str
    left_bit: int
    right_bit: int
    left_keys: tuple[str, ...]
    right_keys: tuple[str, ...]
    output_order: tuple[str, ...] = ()
    memory_limit: int | None = None

    @property
    def left_mask(self) -> int:
        return self.left_bit

    @property
    def right_mask(self) -> int:
        return self.right_bit


@dataclass(frozen=True)
class Scenario:
    sid: str
    rows: dict[int, int]


@dataclass(frozen=True)
class Problem:
    names: tuple[str, ...]
    index: dict[str, int]
    tables: tuple[TableSpec, ...]
    initial_blocks: tuple[Block, ...]
    edges: tuple[Edge, ...]
    scenarios: tuple[Scenario, ...]
    hash_cost: CostTable
    merge_cost: CostTable
    materialization_cost: RowCost
    sort_cost: RowCost | None
    default_memory_limit: int | None
    memory_limits: dict[int, int | None]
    initial_sunk: tuple[int, ...]
    reuse_existing_order: bool = True
    reuse_materialized_blocks: bool = True
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def n_tables(self) -> int:
        return len(self.names)

    @property
    def all_mask(self) -> int:
        return (1 << self.n_tables) - 1

    @property
    def scenario_ids(self) -> tuple[str, ...]:
        return tuple(s.sid for s in self.scenarios)

    def subset_key(self, mask: int) -> str:
        return "+".join(self.names[i] for i in range(self.n_tables) if mask & (1 << i))

    def parse_mask(self, key: Any, what: str = "subset") -> int:
        if is_plain_int(key):
            mask = int(key)
            if mask <= 0 or mask > self.all_mask:
                raise PlannerError(f"{what}: invalid subset mask {mask}")
            return mask
        if not isinstance(key, str) or not key.strip():
            raise PlannerError(f"{what} must be a non-empty subset key")
        text = key.strip()
        if text in self.index:
            return 1 << self.index[text]

        pieces: list[str]
        if any(sep in text for sep in ("+", ",", "|")):
            normalized = text.replace(",", "+").replace("|", "+")
            pieces = [p.strip() for p in normalized.split("+") if p.strip()]
        elif all(len(name) == 1 for name in self.names):
            pieces = list(text)
        else:
            pieces = [text]

        mask = 0
        for piece in pieces:
            if piece not in self.index:
                raise PlannerError(
                    f"{what}: unknown table {piece!r} in subset {key!r}; "
                    "use names joined by '+', e.g. 'A+B'"
                )
            bit = 1 << self.index[piece]
            if mask & bit:
                raise PlannerError(f"{what}: duplicate table {piece!r} in {key!r}")
            mask |= bit
        return mask

    def row_count(self, scenario_index: int, mask: int) -> int:
        rows = self.scenarios[scenario_index].rows
        if mask not in rows:
            raise PlannerError(
                f"scenario {self.scenarios[scenario_index].sid!r} lacks cardinality for "
                f"subset {self.subset_key(mask)!r}"
            )
        return rows[mask]

    def limit_for(self, mask: int, explicit: int | None = None) -> int | None:
        if explicit is not None:
            return explicit
        if mask in self.memory_limits:
            return self.memory_limits[mask]
        return self.default_memory_limit


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _as_dict(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise PlannerError("problem definition must be a JSON object")
    return data


def _get_section(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


def _parse_memory_limit(value: Any, path: str) -> int | None:
    if value is None:
        return None
    return require_int(value, path, minimum=0)


def parse_problem(data: dict[str, Any]) -> Problem:
    data = _as_dict(data)

    # Tables ----------------------------------------------------------------
    tables_raw = data.get("tables")
    if tables_raw is None:
        raise PlannerError("problem requires a 'tables' list (1..6 base tables)")
    if not isinstance(tables_raw, list) or not tables_raw:
        raise PlannerError("'tables' must be a non-empty list")
    if len(tables_raw) > 6:
        raise PlannerError(f"at most six base tables are supported, got {len(tables_raw)}")

    table_specs: list[TableSpec] = []
    names: list[str] = []
    for i, item in enumerate(tables_raw):
        if isinstance(item, str):
            name = require_str(item, f"tables[{i}]")
            ordering: tuple[str, ...] = ()
            materialized = False
            limit = None
            table_reuse_order = True
            table_reuse_materialization = True
        elif isinstance(item, dict):
            name = require_str(item.get("name", item.get("table")), f"tables[{i}].name")
            ordering = as_tuple_str(
                item.get("ordering", item.get("sorted_by", item.get("order"))),
                f"tables[{i}].ordering",
            )
            materialized = bool(item.get("materialized", item.get("is_materialized", False)))
            limit = _parse_memory_limit(
                item.get("memory_limit", item.get("memory")), f"tables[{i}].memory_limit"
            )
            table_reuse_order = opt_bool(
                item.get(
                    "reuse_order",
                    item.get("reuse_existing_order", item.get("can_reuse_order")),
                ),
                f"tables[{i}].reuse_order",
                True,
            )
            table_reuse_materialization = opt_bool(
                item.get(
                    "reuse_materialization",
                    item.get("reuse_materialized_block", item.get("can_reuse_materialized")),
                ),
                f"tables[{i}].reuse_materialization",
                True,
            )
        else:
            raise PlannerError(f"tables[{i}] must be a name or object")
        if name in names:
            raise PlannerError(f"duplicate table name {name!r}")
        names.append(name)
        table_specs.append(
            TableSpec(
                name,
                ordering,
                materialized,
                limit,
                reuse_order=table_reuse_order,
                reuse_materialization=table_reuse_materialization,
            )
        )

    index = {name: i for i, name in enumerate(names)}
    n = len(names)
    all_mask = (1 << n) - 1

    # Memory limits ----------------------------------------------------------
    default_limit = _parse_memory_limit(
        _get_section(data, "default_memory_limit", "memory_limit", "memory"),
        "default_memory_limit",
    )
    reuse_existing_order = opt_bool(
        _get_section(
            data,
            "reuse_existing_order",
            "reuse_order",
            "can_reuse_order",
            "reuse_sorting",
        ),
        "reuse_existing_order",
        True,
    )
    reuse_materialized_blocks = opt_bool(
        _get_section(
            data,
            "reuse_materialized_blocks",
            "reuse_materialization",
            "can_reuse_materialized",
            "can_reuse_materialization",
        ),
        "reuse_materialized_blocks",
        True,
    )
    memory_limits: dict[int, int | None] = {}
    limits_raw = data.get("memory_limits", {})
    if limits_raw is None:
        limits_raw = {}
    if not isinstance(limits_raw, dict):
        raise PlannerError("'memory_limits' must be an object mapping subset to integer")

    def parse_mask_static(key: Any) -> int:
        # Local parser before Problem exists.
        if is_plain_int(key):
            mask = int(key)
            if mask <= 0 or mask > all_mask:
                raise PlannerError(f"invalid subset mask {mask}")
            return mask
        text = str(key).strip()
        if text in index:
            return 1 << index[text]
        if any(sep in text for sep in ("+", ",", "|")):
            pieces = [p.strip() for p in text.replace(",", "+").replace("|", "+").split("+") if p.strip()]
        elif all(len(x) == 1 for x in names):
            pieces = list(text)
        else:
            pieces = [text]
        mask = 0
        for piece in pieces:
            if piece not in index:
                raise PlannerError(f"memory_limits key {key!r} references unknown table {piece!r}")
            mask |= 1 << index[piece]
        return mask

    for key, value in limits_raw.items():
        mask = parse_mask_static(key)
        memory_limits[mask] = _parse_memory_limit(value, f"memory_limits[{key!r}]")

    # Scenarios --------------------------------------------------------------
    scenarios_raw = data.get("scenarios")
    if not isinstance(scenarios_raw, list) or not scenarios_raw:
        raise PlannerError("'scenarios' must be a non-empty list")
    scenarios: list[Scenario] = []
    scenario_ids: set[str] = set()
    for i, item in enumerate(scenarios_raw):
        if not isinstance(item, dict):
            raise PlannerError(f"scenarios[{i}] must be an object")
        sid = str(item.get("id", item.get("name", f"s{i+1}")))
        if sid in scenario_ids:
            raise PlannerError(f"duplicate scenario id {sid!r}")
        scenario_ids.add(sid)
        rows_raw = item.get("rows", item.get("cardinalities", item.get("row_counts")))
        if not isinstance(rows_raw, dict) or not rows_raw:
            raise PlannerError(f"scenarios[{i}].rows must be a non-empty object")
        rows: dict[int, int] = {}
        for key, value in rows_raw.items():
            mask = parse_mask_static(str(key))
            rows[mask] = require_int(value, f"scenarios[{i}].rows[{key!r}]")
        scenarios.append(Scenario(sid, rows))

    k = len(scenarios)
    sunk = [0] * k

    def add_cost_value(target: list[int], value: Any, path: str) -> None:
        if value is None:
            return
        if is_plain_int(value):
            amount = require_int(value, path)
            for i in range(k):
                target[i] += amount
        elif isinstance(value, dict):
            for key, amount in value.items():
                if key not in [s.sid for s in scenarios]:
                    raise PlannerError(f"{path}: unknown scenario id {key!r}")
                idx = next(i for i, s in enumerate(scenarios) if s.sid == key)
                target[idx] += require_int(amount, f"{path}[{key!r}]")
        else:
            raise PlannerError(f"{path} must be an integer or scenario-id mapping")

    add_cost_value(sunk, data.get("initial_cost", data.get("prepaid_cost", data.get("sunk_cost"))), "initial_cost")

    # Initial/seed blocks ----------------------------------------------------
    seeds_raw = _get_section(data, "seed_blocks", "existing_blocks", "blocks", "inputs")
    initial_blocks: list[Block] = []
    covered = 0
    if seeds_raw is not None:
        if not isinstance(seeds_raw, list) or not seeds_raw:
            raise PlannerError("seed/existing blocks must be a non-empty list")
        for i, item in enumerate(seeds_raw):
            if isinstance(item, str):
                mask = parse_mask_static(item)
                ordering = ()
                materialized = False
                explicit_limit = None
                paid = None
                block_reuse_order = True
                block_reuse_materialization = True
            elif isinstance(item, dict):
                subset_value = item.get("tables", item.get("subset", item.get("relations")))
                if subset_value is None:
                    if "mask" in item:
                        mask = require_int(item["mask"], f"blocks[{i}].mask", minimum=1)
                        if mask > all_mask:
                            raise PlannerError(f"blocks[{i}].mask outside table bits")
                    else:
                        raise PlannerError(f"blocks[{i}] requires 'tables' or 'subset'")
                elif isinstance(subset_value, str):
                    mask = parse_mask_static(subset_value)
                elif isinstance(subset_value, list):
                    mask = 0
                    for t in subset_value:
                        tname = require_str(t, f"blocks[{i}].tables entry")
                        if tname not in index:
                            raise PlannerError(f"blocks[{i}] references unknown table {tname!r}")
                        mask |= 1 << index[tname]
                else:
                    raise PlannerError(f"blocks[{i}].tables must be string or list")
                ordering = as_tuple_str(
                    item.get("ordering", item.get("sorted_by", item.get("order"))),
                    f"blocks[{i}].ordering",
                )
                materialized = bool(item.get("materialized", item.get("is_materialized", False)))
                explicit_limit = _parse_memory_limit(
                    item.get("memory_limit", item.get("memory")), f"blocks[{i}].memory_limit"
                )
                paid = item.get("paid_cost", item.get("cost_paid"))
                block_reuse_order = opt_bool(
                    item.get(
                        "reuse_order",
                        item.get("reuse_existing_order", item.get("can_reuse_order")),
                    ),
                    f"blocks[{i}].reuse_order",
                    True,
                )
                block_reuse_materialization = opt_bool(
                    item.get(
                        "reuse_materialization",
                        item.get(
                            "reuse_materialized_block",
                            item.get("can_reuse_materialized"),
                        ),
                    ),
                    f"blocks[{i}].reuse_materialization",
                    True,
                )
            else:
                raise PlannerError(f"blocks[{i}] must be a string or object")

            if mask & covered:
                overlap = mask & covered
                raise PlannerError(
                    "seed/existing blocks overlap at bits "
                    f"{overlap}; a reusable starting frontier must be disjoint"
                )
            if mask & ~all_mask:
                raise PlannerError(f"blocks[{i}] contains unknown table bits")
            covered |= mask
            limit = explicit_limit
            if limit is None and mask in memory_limits:
                limit = memory_limits[mask]
            if limit is None:
                limit = default_limit
            if not reuse_materialized_blocks or not block_reuse_materialization:
                materialized = False
            reuse_order = reuse_existing_order and block_reuse_order
            initial_blocks.append(
                Block(
                    mask,
                    tuple(ordering),
                    materialized,
                    limit,
                    reuse_order=reuse_order,
                )
            )
            if isinstance(item, dict):
                add_cost_value(sunk, paid, f"blocks[{i}].paid_cost")

    # Add uncovered tables as singleton scans using table declarations.
    for i, t in enumerate(table_specs):
        bit = 1 << i
        if not (covered & bit):
            limit = t.memory_limit
            if limit is None and bit in memory_limits:
                limit = memory_limits[bit]
            if limit is None:
                limit = default_limit
            materialized = (
                t.materialized
                and reuse_materialized_blocks
                and t.reuse_materialization
            )
            initial_blocks.append(
                Block(
                    bit,
                    t.ordering,
                    materialized,
                    limit,
                    reuse_order=reuse_existing_order and t.reuse_order,
                )
            )
            covered |= bit

    if covered != all_mask:
        raise PlannerError("internal error: initial blocks do not cover all tables")
    initial_blocks.sort(key=lambda b: b.mask)

    # Edges ------------------------------------------------------------------
    edges_raw = data.get("edges")
    if not isinstance(edges_raw, list) or not edges_raw:
        raise PlannerError("'edges' must be a non-empty list of available join edges")
    edges: list[Edge] = []
    edge_ids: set[str] = set()
    for i, item in enumerate(edges_raw):
        if isinstance(item, (list, tuple)) and len(item) == 2:
            left_name = require_str(item[0], f"edges[{i}][0]")
            right_name = require_str(item[1], f"edges[{i}][1]")
            eid = f"e{i+1}:{left_name}-{right_name}"
            left_keys: tuple[str, ...] = ()
            right_keys: tuple[str, ...] = ()
            output_order: tuple[str, ...] = ()
            edge_limit = None
        elif isinstance(item, dict):
            left_name = require_str(item.get("left", item.get("left_table")), f"edges[{i}].left")
            right_name = require_str(item.get("right", item.get("right_table")), f"edges[{i}].right")
            eid = str(item.get("id", item.get("name", f"e{i+1}:{left_name}-{right_name}")))
            left_keys = as_tuple_str(
                item.get("left_keys", item.get("left_order", item.get("left_sorted_by"))),
                f"edges[{i}].left_keys",
            )
            right_keys = as_tuple_str(
                item.get("right_keys", item.get("right_order", item.get("right_sorted_by"))),
                f"edges[{i}].right_keys",
            )
            output_order = as_tuple_str(
                item.get("output_order", item.get("result_ordering")),
                f"edges[{i}].output_order",
            )
            edge_limit = _parse_memory_limit(
                item.get("memory_limit", item.get("join_memory_limit")),
                f"edges[{i}].memory_limit",
            )
        else:
            raise PlannerError(f"edges[{i}] must be a [left,right] pair or object")

        if left_name not in index or right_name not in index:
            raise PlannerError(f"edges[{i}] references unknown table {left_name!r}/{right_name!r}")
        if left_name == right_name:
            raise PlannerError(f"edges[{i}] cannot join a table to itself")
        if eid in edge_ids:
            raise PlannerError(f"duplicate edge id {eid!r}")
        edge_ids.add(eid)
        li = index[left_name]
        ri = index[right_name]
        if not output_order:
            output_order = left_keys
        edges.append(
            Edge(
                eid=eid,
                left_name=left_name,
                right_name=right_name,
                left_bit=1 << li,
                right_bit=1 << ri,
                left_keys=left_keys,
                right_keys=right_keys,
                output_order=output_order,
                memory_limit=edge_limit,
            )
        )

    # Cost model -------------------------------------------------------------
    cost_section = _get_section(data, "cost_model", "costs")
    if cost_section is None:
        cost_section = data
    if not isinstance(cost_section, dict):
        raise PlannerError("cost_model/costs must be an object")

    join_section = _get_section(cost_section, "join_costs", "joins", "join")
    if join_section is None:
        join_section = cost_section
    if not isinstance(join_section, dict):
        raise PlannerError("join cost section must be an object")

    hash_raw = _get_section(join_section, "hash", "hash_join", "hashjoin")
    merge_raw = _get_section(join_section, "merge", "merge_join", "sort_merge")
    hash_cost = parse_cost_table(hash_raw, "hash_cost")
    merge_cost = parse_cost_table(merge_raw, "merge_cost")

    mat_raw = _get_section(
        cost_section,
        "materialization_cost",
        "materialization",
        "probe_cost",
        "materialize",
    )
    materialization_cost = parse_row_cost(mat_raw, "materialization_cost") or RowCost()
    sort_raw = _get_section(cost_section, "sort_cost", "sort")
    sort_cost = parse_row_cost(sort_raw, "sort_cost")

    problem = Problem(
        names=tuple(names),
        index=index,
        tables=tuple(table_specs),
        initial_blocks=tuple(initial_blocks),
        edges=tuple(edges),
        scenarios=tuple(scenarios),
        hash_cost=hash_cost,
        merge_cost=merge_cost,
        materialization_cost=materialization_cost,
        sort_cost=sort_cost,
        default_memory_limit=default_limit,
        memory_limits=memory_limits,
        initial_sunk=tuple(sunk),
        reuse_existing_order=reuse_existing_order,
        reuse_materialized_blocks=reuse_materialized_blocks,
        raw=data,
    )

    # Early cardinality validation for singleton seeds/tables.  Other subsets
    # are checked when an action actually needs them.
    for si in range(len(scenarios)):
        for block in problem.initial_blocks:
            problem.row_count(si, block.mask)
    return problem


def load_problem(path: str | Path) -> Problem:
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise PlannerError(f"cannot read problem file {p}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PlannerError(f"invalid JSON in {p}: {exc}") from exc
    return parse_problem(data)
