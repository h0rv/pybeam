import argparse
import json
from collections import defaultdict
from html import escape
from pathlib import Path
from statistics import median


def record(path: Path) -> dict[str, object]:
    return parse(path.read_text())


def parse(text: str) -> dict[str, object]:
    value: object = json.loads(text)
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError("Expected a JSON object")
    return {str(key): item for key, item in value.items()}


def number(value: object) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError("Expected a measured number")
    return float(value)


def child(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("Expected a measurement object")
    return {str(key): item for key, item in value.items()}


def chart(title: str, unit: str, samples: dict[str, list[float]], path: Path) -> None:
    maximum = max(max(values) for values in samples.values()) * 1.1
    height = 120 + len(samples) * 36
    elements = [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 960 '
        + str(height)
        + '" role="img" aria-labelledby="title desc">',
        '<title id="title">' + escape(title) + "</title>",
        '<desc id="desc">Dots are medians; lines span minimum to maximum. '
        "Labels give median (min-max) and successful repeat count.</desc>",
        "<style>svg{background:#fff;color:#202124;font:16px sans-serif}"
        "text{fill:currentColor}.axis{stroke:#8b9097}.hornbeam{stroke:#0969da;fill:#0969da}"
        ".uvicorn{stroke:#a34800;fill:#a34800}"
        "@media(prefers-color-scheme:dark){svg{background:#151719;color:#edf0f2}"
        ".hornbeam{stroke:#75b7ff;fill:#75b7ff}.uvicorn{stroke:#ffb86c;fill:#ffb86c}}</style>",
        '<text x="16" y="28" font-size="21">' + escape(title) + "</text>",
        '<text x="16" y="54">' + escape(unit) + " · median (min-max); n = completed repeats</text>",
    ]
    for index, (label, values) in enumerate(samples.items()):
        y = 88 + index * 36
        low, mid, high = min(values), median(values), max(values)
        css = "hornbeam" if label.startswith("Hornbeam") else "uvicorn"
        x1, xm, x2 = (210 + value / maximum * 370 for value in (low, mid, high))
        elements.extend(
            [
                '<text x="16" y="' + str(y + 5) + '">' + escape(label) + "</text>",
                '<g class="'
                + css
                + '"><path stroke-width="3" d="M'
                + str(x1)
                + ","
                + str(y)
                + "H"
                + str(x2)
                + '"/><circle cx="'
                + str(xm)
                + '" cy="'
                + str(y)
                + '" r="4"/></g>',
                '<text x="610" y="'
                + str(y + 5)
                + '">'
                + format(mid, ".3f")
                + " ("
                + format(low, ".3f")
                + "-"
                + format(high, ".3f")
                + "); n="
                + str(len(values))
                + "</text>",
            ]
        )
    y = height - 25
    elements.extend(
        [
            '<path class="axis" d="M210,' + str(y - 18) + 'H580"/>',
            '<text x="210" y="' + str(y + 5) + '">0</text>',
            '<text x="540" y="' + str(y + 5) + '">' + format(maximum, ".1f") + "</text>",
            "</svg>",
        ]
    )
    path.write_text("\n".join(elements) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("raw", type=Path)
    args = parser.parse_args()
    raw = Path(args.raw)
    rows = [parse(line) for line in (raw / "results.jsonl").read_text().splitlines()]
    baseline = {
        (row["variant"], row["repeat"]): number(row["memory_bytes"])
        for row in rows
        if row["views"] == 0 and "error" not in row
    }
    metrics: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        if "error" in row:
            print("FAILED", row)
            continue
        variant, repeat, views = row["variant"], row["repeat"], int(number(row["views"]))
        label = ("Hornbeam" if variant == "hornbeam" else "Uvicorn") + " · " + str(views)
        name = "pyview-" + str(variant) + "-" + str(repeat) + "-" + str(views)
        http = record(raw / (name + ".http.json"))
        if http["errorDistribution"] != {} or http["statusCodeDistribution"] != {"200": 3000}:
            raise ValueError("HTTP trial did not deliver 3000 successful responses")
        latency = child(http["latencyPercentiles"])
        metrics["memory"][label].append(number(row["memory_bytes"]) / 1e6)
        for p in ("p50", "p99"):
            metrics["ping-" + p][label].append(number(latency[p]) * 1000)
        if views:
            if row["event_samples"] != 300 or row["pending_events"] != 0:
                raise ValueError("Event trial has missing replies")
            metrics["per-view"][label].append(
                (number(row["memory_bytes"]) - baseline[variant, repeat]) / views / 1000
            )
            for p in ("p50", "p99"):
                metrics["event-" + p][label].append(number(row["event_" + p + "_ms"]))
            metrics["ticks"][label].append(
                number(row["ticks"]) / number(row["expected_ticks"]) * 100
            )
    titles = {
        "memory": ("Container memory after settling", "MB"),
        "per-view": ("Incremental memory per view", "kB/view, relative to zero views"),
        "ping-p50": ("GET /ping p50 during background pushes", "ms, 100 requests/s"),
        "ping-p99": ("GET /ping p99 during background pushes", "ms, 100 requests/s"),
        "event-p50": ("LiveView event round trip p50", "ms"),
        "event-p99": ("LiveView event round trip p99", "ms"),
        "ticks": ("Ticks received / expected", "%"),
    }
    for key, (title, unit) in titles.items():
        ordered = dict(
            sorted(metrics[key].items(), key=lambda pair: (int(pair[0].split()[-1]), pair[0]))
        )
        chart(title, unit, ordered, Path(__file__).parent / ("pyview-" + key + ".svg"))
        print(
            key,
            {
                label: (median(values), min(values), max(values), len(values))
                for label, values in ordered.items()
            },
        )


if __name__ == "__main__":
    main()
