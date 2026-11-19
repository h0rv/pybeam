import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from failure_workload import counts, mark, wait_file


class WorkloadTests(unittest.TestCase):
    def test_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(
                counts(Path(temporary)),
                {"once": 0, "twice": 0, "more_than_twice": 0, "missing": 20, "total_writes": 0},
            )

    def test_completions_and_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for task_id in range(20):
                mark(directory, task_id)
            mark(directory, 0)
            mark(directory, 1)
            mark(directory, 1)
            self.assertEqual(
                counts(directory),
                {"once": 18, "twice": 1, "more_than_twice": 1, "missing": 0, "total_writes": 23},
            )
            wait_file(directory / "0")

    def test_missing_marker_times_out(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch("failure_workload.time.monotonic", side_effect=[0.0, 31.0]),
            self.assertRaisesRegex(TimeoutError, "Actor did not enter"),
        ):
            wait_file(Path(temporary) / "missing")

    def test_marker_requires_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, self.assertRaises(FileNotFoundError):
            mark(Path(temporary) / "missing", 0)


if __name__ == "__main__":
    unittest.main()
