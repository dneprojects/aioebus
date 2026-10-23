"""Evaluating ebusd message conditions against scan/register data (Phase 4).

Conditions gate firmware- or model-specific messages. On the reference configs
they are inline prefixes referencing the device scan -- `id_sw` / `id_hw` from
the 0704 identification -- or another register's value, with these forms:

    id_sw>=200        id_sw<=140        id_sw>=407        id_sw<202
    id_sw=350-400     (inclusive numeric range)
    id_hw=6701        (equality)
    Ehp_heatpumptype=3            (another register's value)
    Scan_id_product='0010002315';'0010002316'   (value is one of a list)

`evaluate` returns True, False, or None when the operand's value is unknown
(e.g. a register we have not read). `evaluate_all` combines a message's
conditions: any False wins; else any unknown yields unknown; else True.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_COND = re.compile(r"^([A-Za-z_][\w.]*)(>=|<=|=|<|>)(.+)$")


@dataclass
class ConditionContext:
    id_sw: int | None = None
    id_hw: int | None = None
    values: dict[str, object] = field(default_factory=dict)  # other operands

    def operand(self, name: str):
        if name == "id_sw":
            return self.id_sw
        if name == "id_hw":
            return self.id_hw
        return self.values.get(name)


def _as_int(value) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def evaluate(cond: str, ctx: ConditionContext) -> bool | None:
    m = _COND.match(cond.strip())
    if not m:
        return None  # bare name or unsupported form -> cannot decide
    name, op, rhs = m.group(1), m.group(2), m.group(3).strip()
    value = ctx.operand(name)
    if value is None:
        return None  # operand unknown

    if op == "=":
        if "'" in rhs or ";" in rhs:  # value-in-list of (quoted) strings
            options = [p.strip().strip("'") for p in rhs.split(";") if p.strip()]
            return str(value) in options
        lo_hi = rhs.split("-")
        if len(lo_hi) == 2 and all(p.isdigit() for p in lo_hi):  # inclusive range
            v = _as_int(value)
            return v is not None and int(lo_hi[0]) <= v <= int(lo_hi[1])
        if rhs.isdigit():
            v = _as_int(value)
            return v is not None and v == int(rhs)
        return str(value) == rhs

    v, r = _as_int(value), _as_int(rhs)
    if v is None or r is None:
        return None
    if op == ">=":
        return v >= r
    if op == "<=":
        return v <= r
    if op == ">":
        return v > r
    return v < r  # "<"


def evaluate_all(conditions, ctx: ConditionContext) -> bool | None:
    """Combine a message's conditions: any False -> False, else any unknown ->
    None, else True. No conditions -> True."""
    unknown = False
    for cond in conditions or ():
        result = evaluate(cond, ctx)
        if result is False:
            return False
        if result is None:
            unknown = True
    return None if unknown else True


def context_from_scan(scan) -> ConditionContext:
    """Build a context from a :class:`ScanResult` (id_sw/id_hw)."""
    return ConditionContext(id_sw=scan.sw_num, id_hw=scan.hw_num)
