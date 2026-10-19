from __future__ import annotations

from oxypuro.core.constants import NEGATIVE_TOLERANCE, PHYSICAL_LIMITS
from oxypuro.core.models import Measurement
from oxypuro.core.units import KNOWN_PARAMETERS


def check(m: Measurement) -> tuple[str, ...]:
    flags: list[str] = []
    if m.parameter not in KNOWN_PARAMETERS:
        flags.append("unknown_parameter")
    if m.unit_status == "unknown":
        flags.append("unknown_unit")
    if m.lat is None or m.lon is None:
        flags.append("missing_coordinates")

    v = m.value
    if v is not None:
        if v < NEGATIVE_TOLERANCE:
            flags.append("negative")
        elif v < 0:
            flags.append("small_negative")
        limit = PHYSICAL_LIMITS.get(m.parameter)
        if limit is not None and v > limit:
            flags.append("implausible_high")
    return tuple(flags)
