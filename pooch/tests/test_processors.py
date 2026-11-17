# Copyright (c) 2018 The Pooch Developers.
# Distributed under the terms of the BSD 3-Clause License.
# SPDX-License-Identifier: BSD-3-Clause
#
# This code is part of the Fatiando a Terra project (https://www.fatiando.org)
#
"""
Test the processor hooks
"""

import io
import os
import re
import tarfile
import uuid
import warnings
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from .. import Pooch
from ..processors import CONFLICT_STRATEGIES, Decompress, Untar, Unzip
from .utils import capture_log, check_tiny_data, pooch_test_registry, pooch_test_url

REGISTRY = pooch_test_registry()
BASEURL = pooch_test_url()
DATA_DIR = Path(__file__).parent / "data"


@pytest.mark.network
@pytest.mark.parametrize(
    ("method", "ext", "name"),
    [
        ("auto", "xz", None),
        ("lzma", "xz", None),
        ("xz", "xz", None),
        ("bzip2", "bz2", None),
        ("gzip", "gz", None),
        ("gzip", "gz", "different-name.txt"),
    ],
    ids=["auto", "lzma", "xz", "bz2", "gz", "name"],
)
def test_decompress(method, ext, name):
    "Check that decompression after download works for all formats"
    processor = Decompress(method=method, name=name)
    with TemporaryDirectory() as local_store:
        path = Path(local_store)
        if name is None:
            true_path = str(path / ".".join(["tiny-data.txt", ext, "decomp"]))
        else:
            true_path = str(path / name)
        # Setup a pooch in a temp dir
        pup = Pooch(path=path, base_url=BASEURL, registry=REGISTRY)
        # Check the logs when downloading and from the processor
        with capture_log() as log_file:
            fname = pup.fetch("tiny-data.txt." + ext, processor=processor)
            logs = log_file.getvalue()
            lines = logs.splitlines()
            assert len(lines) == 2
            assert lines[0].split()[0] == "Downloading"
            assert lines[-1].startswith("Decompressing")
            assert method in lines[-1]
        assert fname == true_path
        check_tiny_data(fname)
        # Check that processor doesn't execute when not downloading
        with capture_log() as log_file:
            fname = pup.fetch("tiny-data.txt." + ext, processor=processor)
            assert log_file.getvalue() == ""
        assert fname == true_path
        check_tiny_data(fname)


@pytest.mark.network
def test_decompress_fails():
    "Should fail if method='auto' and no extension is given in the file name"
    with TemporaryDirectory() as local_store:
        path = Path(local_store)
        pup = Pooch(path=path, base_url=BASEURL, registry=REGISTRY)

        # Invalid extension
        msg = re.escape("Unrecognized file extension '.txt'")
        with pytest.raises(ValueError, match=msg) as exception:
            pup.fetch("tiny-data.txt", processor=Decompress(method="auto"))
        assert "pooch.Unzip/Untar" not in exception.value.args[0]

        # Should also fail for a bad method name
        msg = re.escape("Invalid compression method 'bla'")
        with pytest.raises(ValueError, match=msg) as exception:
            pup.fetch("tiny-data.txt", processor=Decompress(method="bla"))
        assert "pooch.Unzip/Untar" not in exception.value.args[0]

        # Point people to Untar and Unzip
        msg = re.escape("Invalid compression method 'zip'")
        with pytest.raises(ValueError, match=msg) as exception:
            pup.fetch("tiny-data.txt", processor=Decompress(method="zip"))
        assert "pooch.Unzip/Untar" in exception.value.args[0]
        msg = re.escape("Unrecognized file extension '.zip'")
        with pytest.raises(ValueError, match=msg) as exception:
            pup.fetch("store.zip", processor=Decompress(method="auto"))
        assert "pooch.Unzip/Untar" in exception.value.args[0]


@pytest.mark.network
@pytest.mark.parametrize(
    "target_path", [None, "some_custom_path"], ids=["default_path", "custom_path"]
)
@pytest.mark.parametrize(
    ("archive", "members"),
    [
        ("tiny-data", ["tiny-data.txt"]),
        ("store", None),
        ("store", ["store/tiny-data.txt"]),
        ("store", ["store/subdir/tiny-data.txt"]),
        ("store", ["store/subdir"]),
        ("store", ["store/tiny-data.txt", "store/subdir"]),
    ],
    ids=[
        "single_file",
        "archive_all",
        "archive_file",
        "archive_subdir_file",
        "archive_subdir",
        "archive_multiple",
    ],
)
@pytest.mark.parametrize(
    ("processor_class", "extension"),
    [(Unzip, ".zip"), (Untar, ".tar.gz")],
    ids=["Unzip", "Untar"],
)
def test_unpacking(processor_class, extension, target_path, archive, members):
    "Tests the behaviour of processors for unpacking archives (Untar, Unzip)"
    processor = processor_class(members=members, extract_dir=target_path)
    if target_path is None:
        target_path = archive + extension + processor.suffix
    with TemporaryDirectory() as path:
        path = Path(path)  # noqa: PLW2901
        true_paths, expected_log = _unpacking_expected_paths_and_logs(
            archive, members, path / target_path, processor_class.__name__
        )
        # Setup a pooch in a temp dir
        pup = Pooch(path=path, base_url=BASEURL, registry=REGISTRY)
        # Capture logs and check for the right processor message
        with capture_log() as log_file:
            fnames = pup.fetch(archive + extension, processor=processor)
            assert set(fnames) == true_paths
            _check_logs(log_file, expected_log)
        for fname in fnames:
            check_tiny_data(fname)
        # Check that processor doesn't execute when not downloading
        with capture_log() as log_file:
            fnames = pup.fetch(archive + extension, processor=processor)
            assert set(fnames) == true_paths
            _check_logs(log_file, [])
        for fname in fnames:
            check_tiny_data(fname)


@pytest.mark.network
@pytest.mark.parametrize(
    ("processor_class", "extension"),
    [(Unzip, ".zip"), (Untar, ".tar.gz")],
)
def test_multiple_unpacking(processor_class, extension):
    "Test that multiple subsequent calls to a processor yield correct results"

    with TemporaryDirectory() as local_store:
        pup = Pooch(path=Path(local_store), base_url=BASEURL, registry=REGISTRY)

        # Do a first fetch with the one member only
        processor1 = processor_class(members=["store/tiny-data.txt"])
        filenames1 = pup.fetch("store" + extension, processor=processor1)
        assert len(filenames1) == 1
        check_tiny_data(filenames1[0])

        # Do a second fetch with the other member
        processor2 = processor_class(
            members=["store/tiny-data.txt", "store/subdir/tiny-data.txt"]
        )
        filenames2 = pup.fetch("store" + extension, processor=processor2)
        assert len(filenames2) == 2
        check_tiny_data(filenames2[0])
        check_tiny_data(filenames2[1])

        # Do a third fetch, again with one member and assert
        # that only this member was returned
        filenames3 = pup.fetch("store" + extension, processor=processor1)
        assert len(filenames3) == 1
        check_tiny_data(filenames3[0])


@pytest.mark.network
@pytest.mark.parametrize(
    ("processor_class", "extension"),
    [(Unzip, ".zip"), (Untar, ".tar.gz")],
)
def test_unpack_members_with_leading_dot(processor_class, extension):
    "Test that unpack members can also be specifed both with a leading ./"

    with TemporaryDirectory() as local_store:
        pup = Pooch(path=Path(local_store), base_url=BASEURL, registry=REGISTRY)

        # Do a first fetch with the one member only
        processor1 = processor_class(members=["./store/tiny-data.txt"])
        filenames1 = pup.fetch("store" + extension, processor=processor1)
        assert len(filenames1) == 1
        check_tiny_data(filenames1[0])


def _check_logs(log_file, expected_lines):
    """
    Assert that the lines in the log match the expected ones.
    """
    lines = log_file.getvalue().splitlines()
    assert len(lines) == len(expected_lines)
    for line, expected_line in zip(lines, expected_lines):
        assert line.startswith(expected_line)


def _unpacking_expected_paths_and_logs(archive, members, path, name):
    """
    Generate the appropriate expected paths and log message depending on the
    parameters for the test.
    """
    log_lines = ["Downloading"]
    if archive == "tiny-data":
        true_paths = {str(path / "tiny-data.txt")}
        log_lines.append("Extracting 'tiny-data.txt'")
    elif archive == "store" and members is None:
        true_paths = {
            str(path / "store" / "tiny-data.txt"),
            str(path / "store" / "subdir" / "tiny-data.txt"),
        }
        log_lines.append(f"{name}{name[-1]}ing contents")
    elif archive == "store" and members is not None:
        true_paths = []
        for member in members:
            true_path = path / Path(*member.split("/"))
            if not str(true_path).endswith("tiny-data.txt"):
                true_path = true_path / "tiny-data.txt"
            true_paths.append(str(true_path))
            log_lines.append(f"Extracting '{member}'")
        true_paths = set(true_paths)
    return true_paths, log_lines


@pytest.mark.network
@pytest.mark.parametrize(
    ("processor_class", "extension"),
    [(Unzip, ".zip"), (Untar, ".tar.gz")],
)
def test_unpacking_members_then_no_members(processor_class, extension):
    """
    Test that calling with valid members then without them works.
    https://github.com/fatiando/pooch/issues/364
    """
    with TemporaryDirectory() as local_store:
        pup = Pooch(path=Path(local_store), base_url=BASEURL, registry=REGISTRY)

        # Do a first fetch with an existing member
        processor1 = processor_class(members=["store/tiny-data.txt"])
        filenames1 = pup.fetch("store" + extension, processor=processor1)
        assert len(filenames1) == 1

        # Do a second fetch with no members
        processor2 = processor_class()
        filenames2 = pup.fetch("store" + extension, processor=processor2)
        assert len(filenames2) > 1


@pytest.mark.network
@pytest.mark.parametrize(
    ("processor_class", "extension"),
    [(Unzip, ".zip"), (Untar, ".tar.gz")],
)
def test_unpacking_wrong_members_then_no_members(processor_class, extension):
    """
    Test that calling with invalid members then without them works.
    https://github.com/fatiando/pooch/issues/364
    """
    with TemporaryDirectory() as local_store:
        pup = Pooch(path=Path(local_store), base_url=BASEURL, registry=REGISTRY)

        # Do a first fetch with incorrect member
        processor1 = processor_class(members=["not-a-valid-file.csv"])
        filenames1 = pup.fetch("store" + extension, processor=processor1)
        assert len(filenames1) == 0

        # Do a second fetch with no members
        processor2 = processor_class()
        filenames2 = pup.fetch("store" + extension, processor=processor2)
        assert len(filenames2) > 0


# ----------------------------------------------------------------------------
# Offline tests for root-constrained extraction and explicit conflict handling
# ----------------------------------------------------------------------------


def _write_zip(path, members):
    """
    Create a zip archive with the given ``(name, content)`` members.

    Duplicate names are allowed so the conflict strategies can be exercised.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with zipfile.ZipFile(path, "w") as archive:
            for name, content in members:
                archive.writestr(name, content)


def _write_tar(path, members):
    """
    Create an uncompressed tar archive with the given ``(name, content)``
    members. Duplicate names are allowed.
    """
    with tarfile.TarFile(path, "w") as archive:
        for name, content in members:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))


@pytest.fixture
def archive_factory(tmp_path):
    """Return functions that build zip/tar archives inside a temp directory."""

    def build_zip(members, name="archive.zip"):
        path = tmp_path / name
        _write_zip(path, members)
        return path

    def build_tar(members, name="archive.tar"):
        path = tmp_path / name
        _write_tar(path, members)
        return path

    return build_zip, build_tar


@pytest.mark.parametrize(
    ("processor_class", "build_index"),
    [(Unzip, 0), (Untar, 1)],
    ids=["Unzip", "Untar"],
)
def test_extraction_blocks_directory_traversal(
    archive_factory, tmp_path, processor_class, build_index
):
    "Members escaping the extraction root must raise instead of writing files"
    build_archive = archive_factory[build_index]
    archive = build_archive(
        [
            ("safe.txt", b"safe\n"),
            ("../escape.txt", b"evil\n"),
            ("sub/../../escape2.txt", b"evil2\n"),
        ]
    )
    extract_dir = tmp_path / "output"
    processor = processor_class(extract_dir=str(extract_dir))
    with pytest.raises(ValueError, match="resolves outside of the extraction"):
        processor(str(archive), "download", None)
    assert not (tmp_path / "escape.txt").exists()
    assert not (tmp_path / "escape2.txt").exists()


@pytest.mark.parametrize(
    ("processor_class", "build_index"),
    [(Unzip, 0), (Untar, 1)],
    ids=["Unzip", "Untar"],
)
def test_extraction_blocks_backslash_traversal(
    archive_factory, tmp_path, processor_class, build_index
):
    "Backslash-based traversal must be normalized and blocked on every platform"
    build_archive = archive_factory[build_index]
    archive = build_archive([("..\\escape.txt", b"evil\n")])
    extract_dir = tmp_path / "output"
    processor = processor_class(extract_dir=str(extract_dir))
    with pytest.raises(ValueError, match="resolves outside of the extraction"):
        processor(str(archive), "download", None)
    assert not (tmp_path / "escape.txt").exists()


@pytest.mark.parametrize(
    ("processor_class", "build_index"),
    [(Unzip, 0), (Untar, 1)],
    ids=["Unzip", "Untar"],
)
def test_absolute_members_are_rebased_onto_root(
    archive_factory, tmp_path, processor_class, build_index
):
    "Absolute member names must be written inside the extraction directory"
    build_archive = archive_factory[build_index]
    member_name = f"/pooch-test-{uuid.uuid4().hex}.txt"
    archive = build_archive([(member_name, b"inside\n")])
    extract_dir = tmp_path / "output"
    processor = processor_class(extract_dir=str(extract_dir))
    fnames = processor(str(archive), "download", None)
    expected_name = os.path.basename(member_name)
    assert fnames == [str(extract_dir / expected_name)]
    assert (extract_dir / expected_name).read_bytes() == b"inside\n"
    assert not Path(member_name).exists()


@pytest.mark.parametrize(
    ("processor_class", "build_index"),
    [(Unzip, 0), (Untar, 1)],
    ids=["Unzip", "Untar"],
)
def test_duplicate_members_fail_by_default(
    archive_factory, tmp_path, processor_class, build_index
):
    "Duplicate members must raise unless an explicit conflict strategy is set"
    build_archive = archive_factory[build_index]
    archive = build_archive(
        [("dup.txt", b"first\n"), ("dup.txt", b"second\n")]
    )
    extract_dir = tmp_path / "output"
    processor = processor_class(extract_dir=str(extract_dir))
    with pytest.raises(ValueError, match="Duplicate archive member"):
        processor(str(archive), "download", None)


@pytest.mark.parametrize(
    ("processor_class", "build_index"),
    [(Unzip, 0), (Untar, 1)],
    ids=["Unzip", "Untar"],
)
@pytest.mark.parametrize(
    ("strategy", "expected"),
    [("overwrite", b"second\n"), ("skip", b"first\n")],
    ids=["overwrite-keeps-last", "skip-keeps-first"],
)
def test_duplicate_member_conflict_strategies(
    archive_factory, tmp_path, processor_class, build_index, strategy, expected
):
    "The explicit conflict strategy decides which duplicate payload survives"
    build_archive = archive_factory[build_index]
    archive = build_archive(
        [("dup.txt", b"first\n"), ("dup.txt", b"second\n")]
    )
    extract_dir = tmp_path / f"output-{strategy}"
    processor = processor_class(
        extract_dir=str(extract_dir), on_conflict=strategy
    )
    fnames = processor(str(archive), "download", None)
    assert fnames == [str(extract_dir / "dup.txt")]
    assert (extract_dir / "dup.txt").read_bytes() == expected


@pytest.mark.parametrize(
    ("processor_class", "build_index"),
    [(Unzip, 0), (Untar, 1)],
    ids=["Unzip", "Untar"],
)
def test_members_normalizing_to_same_path_conflict(
    archive_factory, tmp_path, processor_class, build_index
):
    "Distinct names that normalize to one path are treated as duplicates"
    build_archive = archive_factory[build_index]
    archive = build_archive(
        [("a/data.txt", b"first\n"), ("./a/data.txt", b"second\n")]
    )
    extract_dir = tmp_path / "output"
    with pytest.raises(ValueError, match="Duplicate archive member"):
        processor_class(extract_dir=str(extract_dir))(
            str(archive), "download", None
        )
    surviving = tmp_path / "output-skip"
    processor_class(extract_dir=str(surviving), on_conflict="skip")(
        str(archive), "download", None
    )
    assert (surviving / "a" / "data.txt").read_bytes() == b"first\n"


def test_invalid_conflict_strategy():
    "Only documented strategies are accepted"
    for processor_class in (Unzip, Untar):
        with pytest.raises(ValueError, match="Invalid conflict strategy"):
            processor_class(on_conflict="replace-everything")
    assert CONFLICT_STRATEGIES == ("error", "overwrite", "skip")


@pytest.mark.parametrize(
    ("processor_class", "archive_name"),
    [(Unzip, "store.zip"), (Untar, "store.tar.gz")],
    ids=["Unzip", "Untar"],
)
def test_shipped_archives_unpack_offline(tmp_path, processor_class, archive_name):
    "The safe extraction path handles the archives shipped with the test suite"
    archive = DATA_DIR / archive_name
    work_archive = tmp_path / archive_name
    work_archive.write_bytes(archive.read_bytes())

    processor = processor_class()
    fnames = processor(str(work_archive), "download", None)
    assert len(fnames) == 2
    for fname in fnames:
        check_tiny_data(fname)

    # Member filtering, including a directory member and a leading ./ prefix
    for members in (
        ["store/tiny-data.txt"],
        ["store/subdir"],
        ["./store/subdir/tiny-data.txt"],
    ):
        member_processor = processor_class(members=members)
        member_files = member_processor(str(work_archive), "download", None)
        assert member_files
        for fname in member_files:
            check_tiny_data(fname)

    # Re-running with action='fetch' must not re-extract and returns the files
    cached = processor(str(work_archive), "fetch", None)
    assert sorted(cached) == sorted(fnames)
