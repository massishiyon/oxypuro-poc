from __future__ import annotations

import unicodedata

KNOWN_PARAMETERS = frozenset({"pm25", "pm10", "no2", "o3", "so2", "co", "no", "nox", "bc"})
PARTICLES = frozenset({"pm25", "pm10", "bc"})

MOLAR_MASS = {
    "no2": 46.0055,
    "o3": 47.9982,
    "so2": 64.066,
    "co": 28.010,
    "no": 30.006,
}
MOLAR_VOLUME_L = 24.055

CANONICAL_UNIT = "µg/m³"
_NORMALISED_UGM3 = "μg/m3"


def normalise_parameter(s: str) -> str:
    return s.strip().lower().replace(".", "").replace("_", "")


def normalise_unit_string(s: str) -> str:
    out = unicodedata.normalize("NFKC", s).lower().replace(" ", "")
    if out.startswith("ug"):
        out = "μg" + out[2:]
    return out


def to_canonical(parameter: str, value: float, unit: str) -> tuple[float | None, str | None, str]:
    p = normalise_parameter(parameter)
    u = normalise_unit_string(unit)

    if u == _NORMALISED_UGM3:
        return value, CANONICAL_UNIT, "ok"
    if u == "mg/m3":
        return value * 1000.0, CANONICAL_UNIT, "converted"
    if u in ("ppb", "ppm") and p in MOLAR_MASS:
        factor = MOLAR_MASS[p] / MOLAR_VOLUME_L
        if u == "ppm":
            factor *= 1000.0
        return value * factor, CANONICAL_UNIT, "converted"
    return None, None, "unknown"
