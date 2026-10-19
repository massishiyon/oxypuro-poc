from __future__ import annotations

PHYSICAL_LIMITS: dict[str, float] = {
    "pm25": 1000,
    "pm10": 2000,
    "no2": 2000,
    "o3": 1000,
    "so2": 2000,
    "co": 100000,
}

NEGATIVE_TOLERANCE: float = -2
