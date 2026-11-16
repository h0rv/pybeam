import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from examples.distributed import profile


@dataclass(frozen=True)
class PreparedTarget:
    sdk: Path
    file: Path
    private_bytes: bytes
    calls: list[str]


@pytest.fixture
def prepared_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PreparedTarget:
    selected = os.environ.get("PYBEAM_TEST_CANONICAL_PROFILE")
    native = os.environ.get("PYBEAM_TEST_PREPARED_NATIVE")
    if selected is None or native is None:
        pytest.skip("Requires explicit read-only prepared profile and Native inputs; no VM is run")
    candidate = Path(selected).resolve(strict=True)
    native_source = Path(native).resolve(strict=True)
    parsed = profile.PreparedProfile.model_validate_json(candidate.read_bytes())
    sdk = tmp_path / "sdk"
    for name in parsed.sdk_files:
        source = profile.contained(profile.ROOT, Path(name))
        destination = profile.contained(sdk, Path(name))
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    shutil.copytree(candidate.parent / "runtime", sdk / "src/pybeam/runtime/_assets")
    target = sdk / "build/distribution-ready-profile.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(candidate.read_bytes())
    private_bytes = (candidate.parent / "historical-private-profile.json").read_bytes()
    calls: list[str] = []
    real_output = subprocess.check_output
    real_verify = profile.verify_files

    def output(arguments: list[str], *, text: bool, timeout: int) -> str:
        assert text and timeout == 5
        assert arguments[1:4] == ["-C", str(parsed.native_source), "rev-parse"]
        reference = arguments[4]
        assert reference in {"HEAD", "HEAD:c_src"}
        calls.append(reference)
        return real_output(
            [arguments[0], "-C", str(native_source), "rev-parse", reference],
            text=True,
            timeout=timeout,
        )

    def verify(root: Path, files: dict[str, str]) -> None:
        if root == parsed.native_source:
            calls.append("native-payload")
            real_verify(native_source, files)
        else:
            real_verify(root, files)

    # Simulate postpromotion path placement only. Real Git, bytes, hashes and pins are checked.
    monkeypatch.setattr(profile, "ROOT", sdk)
    monkeypatch.setattr(profile, "PROFILE", target)
    monkeypatch.setattr(profile.subprocess, "check_output", output)
    monkeypatch.setattr(profile, "verify_files", verify)
    return PreparedTarget(sdk, target, private_bytes, calls)


def test_canonical_profile__complete_prepared_target_passes_unchanged_guard(
    prepared_target: PreparedTarget,
) -> None:
    assert profile.digest(prepared_target.file) == profile.PROFILE_SHA256
    profile.verify_prepared()
    assert prepared_target.calls == ["HEAD", "HEAD:c_src", "native-payload"]


@pytest.mark.parametrize("invalid", ["private", "tampered"])
def test_canonical_profile__stale_or_tampered_profile_stops_before_native_checks(
    prepared_target: PreparedTarget, invalid: str
) -> None:
    payload = (
        prepared_target.private_bytes
        if invalid == "private"
        else prepared_target.file.read_bytes() + b" "
    )
    prepared_target.file.write_bytes(payload)
    with pytest.raises(ValueError, match="profile pin differs"):
        profile.verify_prepared()
    assert prepared_target.calls == []


def test_canonical_profile__changed_showcase_template_is_rejected(
    prepared_target: PreparedTarget,
) -> None:
    template = prepared_target.sdk / "examples/showcase/job.html"
    template.write_bytes(template.read_bytes() + b"tampered")
    with pytest.raises(ValueError, match="source or asset bytes differ"):
        profile.verify_prepared()


def test_canonical_profile__changed_bundled_helper_is_rejected(
    prepared_target: PreparedTarget,
) -> None:
    helpers = tuple((prepared_target.sdk / "src/pybeam/runtime/_assets").rglob("_fd.py"))
    assert len(helpers) == 1
    helpers[0].write_bytes(b"tampered helper")
    with pytest.raises(ValueError, match="source or asset bytes differ"):
        profile.verify_prepared()
