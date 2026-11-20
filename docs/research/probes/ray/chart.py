import json
import math
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

import matplotlib.pyplot as plt

METRICS = (
    ("cold_s", "Cold start (s)"),
    ("spawn_us_each", "Spawn + first call (µs/actor)"),
    ("tree_rss_mib", "Post-spawn tree RSS (MiB)"),
    ("p50_us", "Sequential call p50 (µs)"),
    ("p99_us", "Sequential call p99 (µs)"),
    ("cpu_speedup", "CPU speedup (K x single / parallel)"),
)


@dataclass(frozen=True)
class Sample:
    framework: str
    metrics: dict[str, float]


def read_results(path: Path) -> list[Sample]:
    samples: list[Sample] = []
    for line in path.read_text().splitlines():
        value: object = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError("Expected a JSON object")
        framework: object = value.get("framework")
        if framework not in ("pybeam", "ray") or not isinstance(framework, str):
            raise ValueError("Unknown framework")
        metrics: dict[str, float] = {}
        for key, _ in METRICS:
            number: object = value.get(key)
            if type(number) not in (int, float) or not isinstance(number, int | float):
                raise ValueError("Expected numeric metric: " + key)
            if not math.isfinite(number) or number <= 0:
                raise ValueError("Expected positive finite metric: " + key)
            metrics[key] = float(number)
        samples.append(Sample(framework, metrics))
    if {sample.framework for sample in samples} != {"pybeam", "ray"}:
        raise ValueError("Need results for both frameworks")
    return samples


def render(samples: list[Sample], output: Path) -> None:
    plt.rcParams.update(
        {
            "text.color": "#888888",
            "axes.labelcolor": "#888888",
            "xtick.color": "#888888",
            "ytick.color": "#888888",
            "axes.edgecolor": "#888888",
            "font.size": 10,
        }
    )
    figure, axes = plt.subplots(2, 3, figsize=(12, 7), layout="constrained")
    for axis, (key, title) in zip(axes.flat, METRICS, strict=True):
        values = [
            statistics.median(
                sample.metrics[key] for sample in samples if sample.framework == framework
            )
            for framework in ("pybeam", "ray")
        ]
        axis.set_facecolor("none")
        bars = axis.bar(["PyBEAM", "Ray"], values, color=["#888888", "#3699b5"])
        axis.set_title(title, fontsize=11)
        axis.set_ylim(0, max(values) * 1.25)
        axis.bar_label(bars, labels=[format(value, ".3g") for value in values], padding=4)
        axis.spines[["top", "right"]].set_visible(False)
    figure.suptitle("Local actor probe · median of three fresh-process trials", fontsize=14)
    figure.text(
        0.5,
        -0.025,
        "RSS is summed, not proportional; actor counts differ. CPU actors are warm.",
        ha="center",
        fontsize=10,
    )
    figure.savefig(output, dpi=160, transparent=True, bbox_inches="tight")
    plt.close(figure)


if __name__ == "__main__":
    render(read_results(Path(sys.argv[1])), Path(sys.argv[2]))
