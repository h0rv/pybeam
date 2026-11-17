from typing import Never

import pytest

from examples.otp_host import library


def test_summarize__returns_python_library_results() -> None:
    result = library.summarize(library.Batch(values=(2.0, 4.0, 6.0, 8.0)))
    assert result.count == 4
    assert result.mean == 5.0
    assert result.deviation == pytest.approx(5.0**0.5)
    assert result.minimum == 2.0
    assert result.maximum == 8.0


@pytest.mark.parametrize(
    "payload",
    [
        "null",
        '{"values":[]}',
        '{"values":[true]}',
        '{"values":["2.0"]}',
        '{"values":[NaN]}',
        '{"values":[1e101]}',
        '{"values":[1.0],"unexpected":1}',
        '{"values":[' + ",".join("1.0" for _ in range(513)) + "]}",
    ],
)
async def test_analyze__rejects_invalid_input_before_native_resources(
    payload: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_group(*, max_children: int, max_pending: int, max_bytes: int) -> Never:
        raise AssertionError("Invalid input must not open an actor group")

    monkeypatch.setattr(library.pybeam, "ActorGroup", unexpected_group)
    with pytest.raises(ValueError):
        await library.analyze(payload)


@pytest.mark.parametrize("payload", [" " * 65_537, b" " * 65_537, "雪" * 30_000])
async def test_analyze__bounds_encoded_input_before_native_resources(
    payload: str | bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_group(*, max_children: int, max_pending: int, max_bytes: int) -> Never:
        raise AssertionError("Oversized input must not open an actor group")

    monkeypatch.setattr(library.pybeam, "ActorGroup", unexpected_group)
    with pytest.raises(ValueError, match="65536 bytes"):
        await library.analyze(payload)
