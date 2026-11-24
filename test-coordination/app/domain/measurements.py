"""Typed measurement comparison (RA-059).

The spec warns RTO and RPO must not be compared as untyped numbers. Each
measurement type has a dimension, a canonical unit and a comparison direction.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class MeasurementRule:
    dimension: str          # "duration" | "ratio"
    lower_is_better: bool


RULES: dict[str, MeasurementRule] = {
    "RTO": MeasurementRule("duration", lower_is_better=True),
    "RPO": MeasurementRule("duration", lower_is_better=True),
    "Availability": MeasurementRule("ratio", lower_is_better=False),
}

# Factors to the canonical unit for each dimension (seconds; percentage).
UNITS: dict[str, tuple[str, Decimal]] = {
    "Seconds": ("duration", Decimal(1)),
    "Minutes": ("duration", Decimal(60)),
    "Hours": ("duration", Decimal(3600)),
    "Percentage": ("ratio", Decimal(1)),
}


class MeasurementError(ValueError):
    pass


def to_canonical(value: Decimal, unit: str, dimension: str) -> Decimal:
    if unit not in UNITS:
        raise MeasurementError(f"Unknown unit '{unit}'")
    dim, factor = UNITS[unit]
    if dim != dimension:
        raise MeasurementError(f"Unit '{unit}' is a {dim} unit; expected a {dimension} unit")
    return value * factor


def evaluate(measurement_type: str, target: Decimal, target_unit: str,
             actual: Decimal, actual_unit: str) -> tuple[bool, Decimal, Decimal]:
    """Return (met, target_canonical, actual_canonical)."""
    rule = RULES.get(measurement_type)
    if rule is None:
        raise MeasurementError(
            f"No comparison rule for '{measurement_type}'; record it as 'Other' without a pass/fail"
        )
    t = to_canonical(target, target_unit, rule.dimension)
    a = to_canonical(actual, actual_unit, rule.dimension)
    if t < 0 or a < 0:
        raise MeasurementError("Measurements must be non-negative")
    if rule.dimension == "ratio" and (t > 100 or a > 100):
        raise MeasurementError("Percentage must be between 0 and 100")
    met = a <= t if rule.lower_is_better else a >= t
    return met, t, a
