import math
import statistics

from benchmarks.contract import Statistics


def percentile(values: tuple[float, ...], fraction: float) -> float:
    """Nearest-rank percentile, with no interpolation or synthetic tail samples."""
    if not values or not 0 < fraction <= 1:
        raise ValueError("Percentiles require observations and a fraction in (0, 1]")
    return sorted(values)[math.ceil(len(values) * fraction) - 1]


def describe(values: tuple[float, ...]) -> Statistics:
    if not values:
        raise ValueError("Statistics require observations")
    return Statistics(
        count=len(values),
        mean=statistics.mean(values),
        stdev=statistics.stdev(values) if len(values) > 1 else 0.0,
        minimum=min(values),
        maximum=max(values),
        p50=percentile(values, 0.50),
        p95=percentile(values, 0.95),
        p99=percentile(values, 0.99),
    )
