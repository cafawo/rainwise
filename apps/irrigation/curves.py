from __future__ import annotations

import math


DEFAULT_MIN_MM = 0.0
DEFAULT_MAX_MM = 7.0
DEFAULT_G = 0.1852
DEFAULT_M = 25.6653

KNOWN_POINTS = [
    {"x": 30, "y": 5},
    {"x": 25, "y": 3},
    {"x": 20, "y": 2},
    {"x": 15, "y": 1},
]


def daily_water_required(
    temp_c: float,
    min_mm: float = DEFAULT_MIN_MM,
    max_mm: float = DEFAULT_MAX_MM,
    g: float = DEFAULT_G,
    m: float = DEFAULT_M,
) -> float:
    return min_mm + (max_mm - min_mm) / (1 + math.exp(-g * (temp_c - m)))


def generate_curve_points(
    min_temp: int,
    max_temp: int,
    step: int,
    *,
    min_mm: float,
    max_mm: float,
    g: float,
    m: float,
) -> list[dict[str, float]]:
    points: list[dict[str, float]] = []
    temp = min_temp
    while temp <= max_temp:
        points.append(
            {
                "x": float(temp),
                "y": round(daily_water_required(temp, min_mm, max_mm, g, m), 3),
            }
        )
        temp += step
    return points


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    if quantile <= 0:
        return min(values)
    if quantile >= 1:
        return max(values)

    sorted_values = sorted(values)
    rank = quantile * (len(sorted_values) - 1)
    lower = int(math.floor(rank))
    upper = int(math.ceil(rank))
    if lower == upper:
        return sorted_values[lower]
    weight = rank - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight
