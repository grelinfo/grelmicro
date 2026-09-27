"""The release publishes grelmicro-core exactly when it has to.

`grelmicro[jwt]` requires the crate version, and the release publishes that
version first when PyPI does not have it yet. These hold the decision to its
three refusals: a pin that names another version, a published version whose
source has since changed, and a version that is not above the latest one.
"""

import io
import tarfile
from pathlib import Path

import pytest

from tools import core_release

SOURCE = {
    "Cargo.toml": b'[package]\nname = "grelmicro-core"\nversion = "0.1.2"\n',
    "Cargo.lock": b"# lock\n",
    "pyproject.toml": b"[project]\n",
    "README.md": b"# grelmicro-core\n",
    "src/lib.rs": b"// core\n",
}


def sdist(files: dict[str, bytes], top: str = "grelmicro_core-0.1.2") -> bytes:
    """Pack files the way maturin lays out a source distribution."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content in {**files, "PKG-INFO": b"Version: x\n"}.items():
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def test_publishes_a_version_pypi_does_not_have() -> None:
    """A crate version above every published one is published first."""
    # Act
    publish = core_release.plan(
        version="0.1.2",
        required="0.1.2",
        released={"0.1.0"},
        released_source=None,
        source=SOURCE,
    )

    # Assert
    assert publish is True


def test_skips_a_published_version_whose_source_is_unchanged() -> None:
    """A release that reuses the published core does not publish it again.

    This is also what a re-run finds after the core published and grelmicro
    did not, so a failed grelmicro publish never burns the core version.
    """
    # Act
    publish = core_release.plan(
        version="0.1.2",
        required="0.1.2",
        released={"0.1.0", "0.1.2"},
        released_source=dict(SOURCE),
        source=SOURCE,
    )

    # Assert
    assert publish is False


def test_resumes_a_published_version_without_its_sdist() -> None:
    """A version whose upload stopped before the sdist is published again.

    The sdist uploads last, so a version without one is an interrupted
    publish, and the publish step skips the files already there.
    """
    # Act
    publish = core_release.plan(
        version="0.1.2",
        required="0.1.2",
        released={"0.1.0", "0.1.2"},
        released_source=None,
        source=SOURCE,
    )

    # Assert
    assert publish is True


def test_refuses_a_pin_that_names_another_version() -> None:
    """The jwt extra requires the crate version the suite was tested with."""
    # Act / Assert
    with pytest.raises(core_release.ReleaseError, match=r">=0\.1\.2"):
        core_release.plan(
            version="0.1.2",
            required="0.1.0",
            released={"0.1.0"},
            released_source=None,
            source=SOURCE,
        )


@pytest.mark.parametrize(
    ("released_source", "changed"),
    [
        ({**SOURCE, "src/lib.rs": b"// older\n"}, "src/lib.rs"),
        ({k: v for k, v in SOURCE.items() if k != "src/lib.rs"}, "src/lib.rs"),
        ({**SOURCE, "src/gone.rs": b"// removed since\n"}, "src/gone.rs"),
    ],
    ids=["edited", "added", "removed"],
)
def test_refuses_a_published_version_whose_source_changed(
    released_source: dict[str, bytes], changed: str
) -> None:
    """A change to a published crate needs a new version.

    Otherwise grelmicro would be tested against the changed crate and ship
    requiring the published one, which is older.
    """
    # Act / Assert
    with pytest.raises(core_release.ReleaseError, match=changed):
        core_release.plan(
            version="0.1.2",
            required="0.1.2",
            released={"0.1.2"},
            released_source=released_source,
            source=SOURCE,
        )


@pytest.mark.parametrize("latest", ["0.1.4", "0.1.10", "1.0.0"])
def test_refuses_a_version_not_above_the_latest(latest: str) -> None:
    """A new version sorts above every published one, compared numerically."""
    # Act / Assert
    with pytest.raises(core_release.ReleaseError, match=latest):
        core_release.plan(
            version="0.1.3",
            required="0.1.3",
            released={"0.1.0", latest},
            released_source=None,
            source=SOURCE,
        )


def test_refuses_a_version_that_is_not_plain_semver() -> None:
    """Only X.Y.Z versions are ordered, so anything else is refused."""
    # Act / Assert
    with pytest.raises(core_release.ReleaseError, match=r"0\.2\.0-rc1"):
        core_release.plan(
            version="0.2.0-rc1",
            required="0.2.0-rc1",
            released={"0.1.0"},
            released_source=None,
            source=SOURCE,
        )


def test_sdist_files_drop_the_top_directory_and_metadata() -> None:
    """An sdist reads as the crate files, keyed like the checkout."""
    # Act
    files = core_release.sdist_files(sdist(SOURCE))

    # Assert
    assert files == SOURCE


def test_source_files_skip_build_output(tmp_path: Path) -> None:
    """The crate reads as what the sdist ships, not the local build tree."""
    # Arrange
    for name, content in SOURCE.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes(content)
    (tmp_path / "target" / "release").mkdir(parents=True)
    (tmp_path / "target" / "release" / "core.so").write_bytes(b"\0")
    (tmp_path / ".DS_Store").write_bytes(b"\0")

    # Act
    files = core_release.source_files(tmp_path)

    # Assert
    assert files == SOURCE


def test_the_checkout_pins_the_crate_version() -> None:
    """The jwt extra in this checkout requires the crate it builds.

    The release refuses the pair otherwise, and this finds it before then.
    """
    # Act
    version = core_release.crate_version(core_release.CRATE / "Cargo.toml")
    required = core_release.required_core(core_release.PYPROJECT)

    # Assert
    assert required == version


def test_required_core_refuses_an_extra_without_a_minimum(
    tmp_path: Path,
) -> None:
    """The extra names its minimum as `grelmicro-core>=X.Y.Z`."""
    # Arrange
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project.optional-dependencies]\njwt = ["grelmicro-core"]\n',
        encoding="utf-8",
    )

    # Act / Assert
    with pytest.raises(core_release.ReleaseError, match="grelmicro-core>="):
        core_release.required_core(pyproject)


@pytest.fixture
def crate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the tool at a throwaway crate and pyproject pinning 0.1.2."""
    crate = tmp_path / "crate"
    for name, content in SOURCE.items():
        (crate / name).parent.mkdir(parents=True, exist_ok=True)
        (crate / name).write_bytes(content)
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project.optional-dependencies]\njwt = ["grelmicro-core>=0.1.2"]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(core_release, "CRATE", crate)
    monkeypatch.setattr(core_release, "PYPROJECT", pyproject)
    return crate


def test_plan_command_prints_step_outputs(
    crate: Path,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`plan` prints the lines a workflow step appends to its outputs."""
    # Arrange
    monkeypatch.setattr(
        core_release, "fetch", lambda _version: ({"0.1.0"}, None)
    )

    # Act
    code = core_release.main(["plan"])

    # Assert
    assert code == 0
    assert capsys.readouterr().out == "version=0.1.2\npublish=true\n"


def test_plan_command_fails_with_the_reason(
    crate: Path,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A refused plan exits non-zero and says what to change."""
    # Arrange
    changed = sdist({**SOURCE, "src/lib.rs": b"// older\n"})
    monkeypatch.setattr(
        core_release, "fetch", lambda _version: ({"0.1.2"}, changed)
    )

    # Act
    code = core_release.main(["plan"])

    # Assert
    assert code == 1
    assert "src/lib.rs" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("published", "expected"),
    [
        (({"0.1.2"}, sdist(SOURCE)), 0),
        (({"0.1.2"}, sdist({**SOURCE, "src/lib.rs": b"// older\n"})), 1),
        (({"0.1.0"}, None), 1),
    ],
    ids=["unchanged", "changed", "unpublished"],
)
def test_released_command_answers_whether_pypi_has_this_crate(
    crate: Path,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
    published: tuple[set[str], bytes | None],
    expected: int,
) -> None:
    """`released` exits 0 only when PyPI holds exactly this crate."""
    # Arrange
    monkeypatch.setattr(core_release, "fetch", lambda _version: published)

    # Act
    code = core_release.main(["released"])

    # Assert
    assert code == expected
