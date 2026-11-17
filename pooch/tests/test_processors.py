# Copyright (c) 2018 The Pooch Developers.
# Distributed under the terms of the BSD 3-Clause License.
# SPDX-License-Identifier: BSD-3-Clause
#
# This code is part of the Fatiando a Terra project (https://www.fatiando.org)
#
"""
Test the processor hooks
"""

import re
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from .. import Pooch
from .. import processors
from ..processors import Decompress, Untar, Unzip
from .utils import capture_log, check_tiny_data, pooch_test_registry, pooch_test_url

REGISTRY = pooch_test_registry()
BASEURL = pooch_test_url()


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


def _copy_archive(tmp_path, name):
    """
    Copy one of the test archives into a temporary folder.
    """
    archive = tmp_path / name
    shutil.copy(Path(__file__).parent / "data" / name, archive)
    return str(archive)


def _tree(root):
    """
    List every file below root as a path relative to it.
    """
    root = Path(root)
    return sorted(
        str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()
    )


@pytest.mark.parametrize(
    ("processor_class", "extension"),
    [(Unzip, ".zip"), (Untar, ".tar.gz")],
    ids=["Unzip", "Untar"],
)
def test_unpacking_failure_removes_own_products(tmp_path, processor_class, extension):
    """
    A failed extraction must not leave a partial tree or unrelated files behind.
    """
    archive = _copy_archive(tmp_path, "store" + extension)
    extract_dir = Path(archive + processor_class().suffix)
    extract_dir.mkdir()
    external = extract_dir / "keep.txt"
    external.write_text("keep me", encoding="utf-8")
    processor = processor_class()

    def fail_after_writing(fname, destination):
        (Path(destination) / "partial.txt").write_text("partial", encoding="utf-8")
        raise RuntimeError("extraction failed")

    processor._extract_file = fail_after_writing
    with pytest.raises(RuntimeError, match="extraction failed"):
        processor(archive, "download", None)

    assert _tree(extract_dir) == ["keep.txt"]
    assert external.read_text(encoding="utf-8") == "keep me"
    # No journals or staging folders are left behind next to the cache.
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "store" + extension,
        "store" + extension + processor.suffix,
    ]


@pytest.mark.parametrize(
    ("processor_class", "extension"),
    [(Unzip, ".zip"), (Untar, ".tar.gz")],
    ids=["Unzip", "Untar"],
)
def test_unpacking_failure_restores_overwritten_files(
    tmp_path, processor_class, extension
):
    """
    A failed update must restore files it overwrote with new content.
    """
    archive = _copy_archive(tmp_path, "store" + extension)
    extract_dir = Path(archive + processor_class().suffix)
    existing = extract_dir / "store" / "tiny-data.txt"
    existing.parent.mkdir(parents=True)
    existing.write_text("old content", encoding="utf-8")
    processor = processor_class()

    original_publish = processors._ExtractionTransaction.publish

    def publish_then_fail(transaction):
        original_publish(transaction)
        raise RuntimeError("publication failed")

    processors._ExtractionTransaction.publish = publish_then_fail
    try:
        with pytest.raises(RuntimeError, match="publication failed"):
            processor(archive, "update", None)
    finally:
        processors._ExtractionTransaction.publish = original_publish

    assert existing.read_text(encoding="utf-8") == "old content"
    assert _tree(extract_dir) == ["store/tiny-data.txt"]


@pytest.mark.parametrize(
    ("processor_class", "extension"),
    [(Unzip, ".zip"), (Untar, ".tar.gz")],
    ids=["Unzip", "Untar"],
)
def test_unpacking_preexisting_file_not_claimed(tmp_path, processor_class, extension):
    """
    A same-named file that was there before the call is not one of its products.
    """
    archive = _copy_archive(tmp_path, "store" + extension)
    extract_dir = Path(archive + processor_class().suffix)
    existing = extract_dir / "store" / "tiny-data.txt"
    existing.parent.mkdir(parents=True)
    existing.write_text("someone else's data", encoding="utf-8")

    processor = processor_class(members=["store/tiny-data.txt"])

    def fail_before_publishing(fname, destination):
        raise RuntimeError("extraction failed")

    processor._extract_file = fail_before_publishing
    with pytest.raises(RuntimeError, match="extraction failed"):
        processor(archive, "download", None)

    # This call never published anything, so the pre-existing file must remain.
    content = existing.read_text(encoding="utf-8")
    assert content == "someone else's data"
    assert _tree(extract_dir) == ["store/tiny-data.txt"]


@pytest.mark.parametrize(
    ("processor_class", "extension"),
    [(Unzip, ".zip"), (Untar, ".tar.gz")],
    ids=["Unzip", "Untar"],
)
def test_unpacking_replays_interrupted_call(tmp_path, processor_class, extension):
    """
    An interrupted extraction is rolled back before a later call proceeds.
    """
    archive = _copy_archive(tmp_path, "store" + extension)
    extract_dir = archive + processor_class().suffix
    processor = processor_class()

    transaction = processors._ExtractionTransaction(archive, extract_dir, "download")
    transaction.prepare()
    processor._extract_file(archive, transaction.data_dir)
    transaction.add_staged_files()
    transaction.publish()
    # Simulate a crash: the call stops before committing its transaction.
    transaction._forget()

    filenames = processor(archive, "fetch", None)
    assert len(filenames) == 2
    for fname in filenames:
        check_tiny_data(fname)
    assert _tree(extract_dir) == [
        "store/subdir/tiny-data.txt",
        "store/tiny-data.txt",
    ]


def test_unpacking_replay_keeps_data_published_by_another_call(tmp_path):
    """
    A replayed rollback must not delete a file another call has published.

    This simulates a rollback that failed part-way: the crashed call recorded
    an artifact, another call replaced it with new content, and only then is
    the crashed call's rollback retried. The newer file must survive.
    """
    archive = _copy_archive(tmp_path, "store.zip")
    extract_dir = archive + ".unzip"
    processor = Unzip()

    # An abandoned transaction that published one of its files, then crashed.
    abandoned = processors._ExtractionTransaction(archive, extract_dir, "download")
    abandoned.prepare()
    processor._extract_file(archive, abandoned.data_dir)
    abandoned.add_staged_files()
    abandoned.publish()
    journal_dir = Path(abandoned.journal_dir)
    plan_path = abandoned.plan_path
    plan = abandoned._plan()
    abandoned._forget()

    # Another call publishes valid data at the same paths.
    processor(archive, "fetch", None)
    published = {
        fname: Path(fname).read_bytes()
        for fname in Path(extract_dir).rglob("*.txt")
    }
    assert published

    # Retrying the old transaction's rollback leaves the new data untouched.
    processors._rollback_transaction(plan_path, plan)
    for fname, content in published.items():
        assert fname.read_bytes() == content
    assert not journal_dir.exists()


@pytest.mark.parametrize(
    ("processor_class", "extension"),
    [(Unzip, ".zip"), (Untar, ".tar.gz")],
    ids=["Unzip", "Untar"],
)
def test_unpacking_committed_cleanup_failure_keeps_data(
    tmp_path, processor_class, extension
):
    """
    Failing to delete the records of a committed call must not undo its data.
    """
    archive = _copy_archive(tmp_path, "store" + extension)
    extract_dir = archive + processor_class().suffix
    processor = processor_class()

    original_cleanup = processors._cleanup_committed_transaction

    def fail_cleanup(plan_path, plan):
        raise OSError("cannot clean up")

    processors._cleanup_committed_transaction = fail_cleanup
    try:
        filenames = processor(archive, "download", None)
    finally:
        processors._cleanup_committed_transaction = original_cleanup

    assert len(filenames) == 2
    for fname in filenames:
        check_tiny_data(fname)

    # The next call notices the committed record and only removes leftovers.
    assert len(processor(archive, "fetch", None)) == 2
    for fname in filenames:
        check_tiny_data(fname)
