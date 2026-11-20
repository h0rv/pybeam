import json
import tempfile
import unittest
from pathlib import Path

from chart import METRICS, read_results, render


class ChartTests(unittest.TestCase):
    def test_results_and_render(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "results.jsonl"
            source.write_text(
                "\n".join(
                    json.dumps(
                        {
                            "framework": framework,
                            **dict.fromkeys((key for key, _ in METRICS), 1.0),
                        }
                    )
                    for framework in ("pybeam", "ray")
                )
            )
            samples = read_results(source)
            self.assertEqual(len(samples), 2)
            output = Path(directory) / "chart.png"
            render(samples, output)
            self.assertTrue(output.read_bytes().startswith(b"\x89PNG"))

    def test_empty_results(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "results.jsonl"
            source.write_text("")
            with self.assertRaisesRegex(ValueError, "both frameworks"):
                read_results(source)

    def test_invalid_results(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "results.jsonl"
            for value in (
                [],
                {"framework": "other"},
                {"framework": "ray"},
                {"framework": "ray", "cold_s": -1},
                {"framework": "ray", "cold_s": True},
            ):
                source.write_text(json.dumps(value))
                with self.subTest(value=value), self.assertRaises(ValueError):
                    read_results(source)


if __name__ == "__main__":
    unittest.main()
