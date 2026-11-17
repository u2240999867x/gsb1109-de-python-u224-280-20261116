# Copyright (c) 2018 The Pooch Developers.
# Distributed under the terms of the BSD 3-Clause License.
# SPDX-License-Identifier: BSD-3-Clause
#
# This code is part of the Fatiando a Terra project (https://www.fatiando.org)
#

"""
Test the core class and factory function.
"""

import hashlib
import io
import os
import re
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
import requests

import pooch

# Import the core module so that we can monkeypatch some functions
from .. import core
from ..core import Pooch, create, download_action, retrieve, stream_download
from ..downloaders import FTPDownloader, HTTPDownloader
from ..hashes import file_hash, hash_matches
from ..registry import FileNotInRegistryError, RetractedFileError
from ..utils import get_logger, os_cache, temporary_file
from .utils import (
    capture_log,
    check_large_data,
    check_tiny_data,
    data_over_ftp,
    mirror_directory,
    pooch_test_dataverse_url,
    pooch_test_figshare_url,
    pooch_test_registry,
    pooch_test_url,
    pooch_test_zenodo_url,
    pooch_test_zenodo_with_slash_url,
)

DATA_DIR = str(Path(__file__).parent / "data")
REGISTRY = pooch_test_registry()
BASEURL = pooch_test_url()
FIGSHAREURL = pooch_test_figshare_url()
ZENODOURL = pooch_test_zenodo_url()
ZENODOURL_W_SLASH = pooch_test_zenodo_with_slash_url()
DATAVERSEURL = pooch_test_dataverse_url()
REGISTRY_CORRUPTED = {
    # The same data file but I changed the hash manually to a wrong one
    "tiny-data.txt": "098h0894dba14b12085eacb204284b97e362f4f3e5a5807693cc90ef415c1b2d"
}


@pytest.fixture
def data_dir_mirror(tmp_path):
    """
    Mirror the test data folder on a temporary directory. Needed to avoid
    permission errors when pooch is installed on a non-writable path.
    """
    return mirror_directory(DATA_DIR, tmp_path)


@pytest.mark.network
def test_retrieve():
    "Try downloading some data with retrieve"
    with TemporaryDirectory() as local_store:
        data_file = "tiny-data.txt"
        url = BASEURL + data_file
        # Check that the logs say that the file is being downloaded
        with capture_log() as log_file:
            fname = retrieve(url, known_hash=None, path=local_store)
            logs = log_file.getvalue()
            assert logs.split()[0] == "Downloading"
            assert "SHA256 hash of downloaded file:" in logs
            assert REGISTRY[data_file] in logs
        # Check that the downloaded file has the right content
        assert data_file == fname[-len(data_file) :]
        check_tiny_data(fname)
        assert file_hash(fname) == REGISTRY[data_file]
        # Check that no logging happens when not downloading
        with capture_log() as log_file:
            fname = retrieve(url, known_hash=None, path=local_store)
            assert log_file.getvalue() == ""
        with capture_log() as log_file:
            fname = retrieve(url, known_hash=REGISTRY[data_file], path=local_store)
            assert log_file.getvalue() == ""


@pytest.mark.network
def test_retrieve_fname():
    "Try downloading some data with retrieve and setting the file name"
    with TemporaryDirectory() as local_store:
        data_file = "tiny-data.txt"
        url = BASEURL + data_file
        # Check that the logs say that the file is being downloaded
        with capture_log() as log_file:
            fname = retrieve(url, known_hash=None, path=local_store, fname=data_file)
            logs = log_file.getvalue()
            assert logs.split()[0] == "Downloading"
            assert "SHA256 hash of downloaded file:" in logs
            assert REGISTRY[data_file] in logs
        # Check that the downloaded file has the right name and content
        assert data_file == os.path.split(fname)[1]
        check_tiny_data(fname)
        assert file_hash(fname) == REGISTRY[data_file]


@pytest.mark.network
def test_retrieve_default_path():
    "Try downloading some data with retrieve to the default cache location"
    data_file = "tiny-data.txt"
    url = BASEURL + data_file
    expected_location = os_cache("pooch") / data_file
    try:
        # Check that the logs say that the file is being downloaded
        with capture_log() as log_file:
            fname = retrieve(url, known_hash=None, fname=data_file)
            logs = log_file.getvalue()
            assert logs.split()[0] == "Downloading"
            assert str(os_cache("pooch").resolve()) in logs
            assert "SHA256 hash of downloaded file" in logs
            assert REGISTRY[data_file] in logs
        # Check that the downloaded file has the right content
        assert fname == str(expected_location.resolve())
        check_tiny_data(fname)
        assert file_hash(fname) == REGISTRY[data_file]
    finally:
        if os.path.exists(str(expected_location)):
            os.remove(str(expected_location))


def test_pooch_local(data_dir_mirror):
    "Setup a pooch that already has the local data and test the fetch."
    pup = Pooch(path=data_dir_mirror, base_url="some bogus URL", registry=REGISTRY)
    true = str(data_dir_mirror / "tiny-data.txt")
    fname = pup.fetch("tiny-data.txt")
    assert true == fname
    check_tiny_data(fname)


@pytest.mark.network
@pytest.mark.parametrize(
    "url",
    [
        BASEURL,
        pytest.param(FIGSHAREURL, marks=pytest.mark.figshare),
        ZENODOURL,
        DATAVERSEURL,
    ],
    ids=["https", "figshare", "zenodo", "dataverse"],
)
def test_pooch_custom_url(url):
    "Have pooch download the file from URL that is not base_url"
    with TemporaryDirectory() as local_store:
        path = Path(local_store)
        urls = {"tiny-data.txt": url + "tiny-data.txt"}
        # Setup a pooch in a temp dir
        pup = Pooch(path=path, base_url="", registry=REGISTRY, urls=urls)
        # Check that the logs say that the file is being downloaded
        with capture_log() as log_file:
            fname = pup.fetch("tiny-data.txt")
            logs = log_file.getvalue()
            assert logs.split()[0] == "Downloading"
            assert logs.split()[-1] == f"'{path}'."
        check_tiny_data(fname)
        # Check that no logging happens when there are no events
        with capture_log() as log_file:
            fname = pup.fetch("tiny-data.txt")
            assert log_file.getvalue() == ""


@pytest.mark.network
@pytest.mark.parametrize(
    "url",
    [
        BASEURL,
        pytest.param(FIGSHAREURL, marks=pytest.mark.figshare),
        ZENODOURL,
        DATAVERSEURL,
    ],
    ids=["https", "figshare", "zenodo", "dataverse"],
)
def test_pooch_download(url):
    "Setup a pooch that has no local data and needs to download"
    with TemporaryDirectory() as local_store:
        path = Path(local_store)
        true_path = str(path / "tiny-data.txt")
        # Setup a pooch in a temp dir
        pup = Pooch(path=path, base_url=url, registry=REGISTRY)
        # Check that the logs say that the file is being downloaded
        with capture_log() as log_file:
            fname = pup.fetch("tiny-data.txt")
            logs = log_file.getvalue()
            assert logs.split()[0] == "Downloading"
            assert logs.split()[-1] == f"'{path}'."
        # Check that the downloaded file has the right content
        assert true_path == fname
        check_tiny_data(fname)
        assert file_hash(fname) == REGISTRY["tiny-data.txt"]
        # Check that no logging happens when not downloading
        with capture_log() as log_file:
            fname = pup.fetch("tiny-data.txt")
            assert log_file.getvalue() == ""


class FakeHashMatches:
    "Create a fake version of hash_matches that fails n times"

    def __init__(self, nfailures):
        self.nfailures = nfailures
        self.failed = 0

    def hash_matches(self, *args, **kwargs):
        "Fail n times before finally passing"
        if self.failed < self.nfailures:
            self.failed += 1
            # Give it an invalid hash to force a failure
            return hash_matches(args[0], "bla", **kwargs)
        return hash_matches(*args, **kwargs)


@pytest.mark.network
def test_pooch_download_retry_off_by_default(monkeypatch):
    "Check that retrying the download is off by default"
    with TemporaryDirectory() as local_store:
        monkeypatch.setattr(core, "hash_matches", FakeHashMatches(3).hash_matches)
        # Setup a pooch without download retrying
        path = Path(local_store)
        pup = Pooch(path=path, base_url=BASEURL, registry=REGISTRY)
        # Make sure it fails with no retries
        msg = "does not match the known hash"
        with pytest.raises(ValueError, match=msg), capture_log() as log_file:
            pup.fetch("tiny-data.txt")
        # Check that the log doesn't have the download retry message
        logs = log_file.getvalue().strip().split("\n")
        assert len(logs) == 1
        assert logs[0].startswith("Downloading")
        assert logs[0].endswith(f"'{path}'.")


class FakeSleep:
    "Create a fake version of sleep that logs the specified times"

    def __init__(self):
        self.times = []

    def sleep(self, secs):
        "Store the time and doesn't sleep"
        self.times.append(secs)


@pytest.mark.network
def test_pooch_download_retry(monkeypatch):
    "Check that retrying the download works if the hash is different"
    with TemporaryDirectory() as local_store:
        monkeypatch.setattr(core, "hash_matches", FakeHashMatches(11).hash_matches)
        fakesleep = FakeSleep()
        monkeypatch.setattr(core.time, "sleep", fakesleep.sleep)
        # Setup a pooch with download retrying
        path = Path(local_store)
        true_path = str(path / "tiny-data.txt")
        retries = 11
        pup = Pooch(
            path=path, base_url=BASEURL, registry=REGISTRY, retry_if_failed=retries
        )
        # Check that the logs say that the download failed n times
        with capture_log() as log_file:
            fname = pup.fetch("tiny-data.txt")
            logs = log_file.getvalue().strip().split("\n")
            assert len(logs) == 1 + retries
            assert logs[0].startswith("Downloading")
            assert logs[0].endswith(f"'{path}'.")
            for i, line in zip(range(retries, 0, -1), logs[1:]):
                assert "Failed to download" in line
                plural = "s" if i > 1 else ""
                assert f"download again {i} more time{plural}." in line
            # Check that the sleep time increases but stops at 10s
            assert fakesleep.times == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 10]
        # Check that the downloaded file has the right content
        assert true_path == fname
        check_tiny_data(fname)
        assert file_hash(fname) == REGISTRY["tiny-data.txt"]


@pytest.mark.network
def test_pooch_download_retry_fails_eventually(monkeypatch):
    "Check that retrying the download fails after the set amount of retries"
    with TemporaryDirectory() as local_store:
        monkeypatch.setattr(core, "hash_matches", FakeHashMatches(3).hash_matches)
        # Setup a pooch with insufficient retry attempts
        path = Path(local_store)
        pup = Pooch(path=path, base_url=BASEURL, registry=REGISTRY, retry_if_failed=1)
        # Make sure it fails with no retries
        msg = "does not match the known hash"
        # Check that the logs say that the download failed n times
        with pytest.raises(ValueError, match=msg), capture_log() as log_file:
            pup.fetch("tiny-data.txt")
        logs = log_file.getvalue().strip().split("\n")
        assert len(logs) == 2
        assert logs[0].startswith("Downloading")
        assert logs[0].endswith(f"'{path}'.")
        assert "Failed to download" in logs[1]
        assert "download again 1 more time." in logs[1]


@pytest.mark.network
def test_pooch_logging_level():
    "Setup a pooch and check that no logging happens when the level is raised"
    with TemporaryDirectory() as local_store:
        path = Path(local_store)
        urls = {"tiny-data.txt": BASEURL + "tiny-data.txt"}
        # Setup a pooch in a temp dir
        pup = Pooch(path=path, base_url="", registry=REGISTRY, urls=urls)
        # Capture only critical logging events
        with capture_log("CRITICAL") as log_file:
            fname = pup.fetch("tiny-data.txt")
            assert log_file.getvalue() == ""
        check_tiny_data(fname)


@pytest.mark.network
def test_pooch_update():
    "Setup a pooch that already has the local data but the file is outdated"
    with TemporaryDirectory() as local_store:
        path = Path(local_store)
        # Create a dummy version of tiny-data.txt that is different from the
        # one in the remote storage
        true_path = str(path / "tiny-data.txt")
        with open(true_path, "w", encoding="utf-8") as fin:
            fin.write("different data")
        # Setup a pooch in a temp dir
        pup = Pooch(path=path, base_url=BASEURL, registry=REGISTRY)
        # Check that the logs say that the file is being updated
        with capture_log() as log_file:
            fname = pup.fetch("tiny-data.txt")
            logs = log_file.getvalue()
            assert logs.split()[0] == "Updating"
            assert logs.split()[-1] == f"'{path}'."
        # Check that the updated file has the right content
        assert true_path == fname
        check_tiny_data(fname)
        assert file_hash(fname) == REGISTRY["tiny-data.txt"]
        # Check that no logging happens when not downloading
        with capture_log() as log_file:
            fname = pup.fetch("tiny-data.txt")
            assert log_file.getvalue() == ""


def test_pooch_update_disallowed():
    "Test that disallowing updates works."
    with TemporaryDirectory() as local_store:
        path = Path(local_store)
        # Create a dummy version of tiny-data.txt that is different from the
        # one in the remote storage
        true_path = str(path / "tiny-data.txt")
        with open(true_path, "w", encoding="utf-8") as fin:
            fin.write("different data")
        # Setup a pooch in a temp dir
        pup = Pooch(
            path=path,
            base_url=BASEURL,
            registry=REGISTRY,
            allow_updates=False,
        )
        msg = "but updates are disallowed"
        with pytest.raises(ValueError, match=msg):
            pup.fetch("tiny-data.txt")


def test_pooch_update_disallowed_environment():
    "Test that disallowing updates works through an environment variable."
    variable_name = "MYPROJECT_DISALLOW_UPDATES"
    try:
        os.environ[variable_name] = "False"
        with TemporaryDirectory() as local_store:
            path = Path(local_store)
            # Create a dummy version of tiny-data.txt that is different from
            # the one in the remote storage
            true_path = str(path / "tiny-data.txt")
            with open(true_path, "w", encoding="utf-8") as fin:
                fin.write("different data")
            # Setup a pooch in a temp dir
            pup = create(
                path=path,
                base_url=BASEURL,
                registry=REGISTRY,
                allow_updates=variable_name,
            )
            msg = "but updates are disallowed"
            with pytest.raises(ValueError, match=msg):
                pup.fetch("tiny-data.txt")
    finally:
        os.environ.pop(variable_name)


def test_pooch_create_base_url_no_trailing_slash():
    """
    Test if pooch.create appends a trailing slash to the base url if missing
    """
    base_url = "https://mybase.url"
    pup = create(base_url=base_url, registry=None, path=DATA_DIR)
    assert pup.base_url == base_url + "/"


@pytest.mark.network
def test_pooch_corrupted(data_dir_mirror):
    "Raise an exception if the file hash doesn't match the registry"
    # Test the case where the file wasn't in the directory
    with TemporaryDirectory() as local_store:
        path = os.path.abspath(local_store)
        pup = Pooch(path=path, base_url=BASEURL, registry=REGISTRY_CORRUPTED)
        with capture_log() as log_file:
            msg = "(tiny-data.txt)"
            with pytest.raises(ValueError, match=msg):
                pup.fetch("tiny-data.txt")
            logs = log_file.getvalue()
            assert logs.split()[0] == "Downloading"
            assert logs.split()[-1] == f"'{path}'."
    # and the case where the file exists but hash doesn't match
    pup = Pooch(path=data_dir_mirror, base_url=BASEURL, registry=REGISTRY_CORRUPTED)
    with capture_log() as log_file:
        msg = "(tiny-data.txt)"
        with pytest.raises(ValueError, match=msg):
            pup.fetch("tiny-data.txt")
        logs = log_file.getvalue()
        assert logs.split()[0] == "Updating"
        assert logs.split()[-1] == f"'{data_dir_mirror}'."


def test_pooch_file_not_in_registry():
    "Should raise an exception if the file is not in the registry."
    pup = Pooch(
        path="it shouldn't matter", base_url="this shouldn't either", registry=REGISTRY
    )
    msg = "is not in the registry"
    with pytest.raises(ValueError, match=msg):
        pup.fetch("this-file-does-not-exit.csv")


def test_pooch_load_registry():
    "Loading the registry from a file should work"
    pup = Pooch(path="", base_url="")
    pup.load_registry(os.path.join(DATA_DIR, "registry.txt"))
    assert pup.registry == REGISTRY
    assert pup.registry_files.sort() == list(REGISTRY).sort()


def test_pooch_load_registry_comments():
    "Loading the registry from a file and strip line comments"
    pup = Pooch(path="", base_url="")
    pup.load_registry(os.path.join(DATA_DIR, "registry_comments.txt"))
    assert pup.registry == REGISTRY
    assert pup.registry_files.sort() == list(REGISTRY).sort()


def test_pooch_load_registry_fileobj():
    "Loading the registry from a file object"
    path = os.path.join(DATA_DIR, "registry.txt")

    # Binary mode
    pup = Pooch(path="", base_url="")
    with open(path, "rb") as fin:
        pup.load_registry(fin)
    assert pup.registry == REGISTRY
    assert pup.registry_files.sort() == list(REGISTRY).sort()

    # Text mode
    pup = Pooch(path="", base_url="")
    with open(path, "r", encoding="utf-8") as fin:
        pup.load_registry(fin)
    assert pup.registry == REGISTRY
    assert pup.registry_files.sort() == list(REGISTRY).sort()


def test_pooch_load_registry_custom_url():
    "Load the registry from a file with a custom URL inserted"
    pup = Pooch(path="", base_url="")
    pup.load_registry(os.path.join(DATA_DIR, "registry-custom-url.txt"))
    assert pup.registry == REGISTRY
    assert pup.urls == {"tiny-data.txt": "https://some-site/tiny-data.txt"}


def test_pooch_load_registry_invalid_line():
    "Should raise an exception when a line doesn't have two elements"
    pup = Pooch(path="", base_url="", registry={})
    msg = "Invalid entry in Pooch registry file"
    with pytest.raises(OSError, match=msg):
        pup.load_registry(os.path.join(DATA_DIR, "registry-invalid.txt"))


def test_pooch_load_registry_with_spaces():
    "Should check that spaces in filenames are allowed in registry files"
    pup = Pooch(path="", base_url="")
    pup.load_registry(os.path.join(DATA_DIR, "registry-spaces.txt"))
    assert "file with spaces.txt" in pup.registry
    assert "other with spaces.txt" in pup.registry


@pytest.mark.network
def test_check_availability():
    "Should correctly check availability of existing and non existing files"
    # Check available remote file
    pup = Pooch(path=DATA_DIR, base_url=BASEURL, registry=REGISTRY)
    assert pup.is_available("tiny-data.txt")
    # Check non available remote file
    pup = Pooch(path=DATA_DIR, base_url=BASEURL + "wrong-url/", registry=REGISTRY)
    assert not pup.is_available("tiny-data.txt")
    # Wrong file name
    registry = {"not-a-real-data-file.txt": "notarealhash"}
    registry.update(REGISTRY)
    pup = Pooch(path=DATA_DIR, base_url=BASEURL, registry=registry)
    assert not pup.is_available("not-a-real-data-file.txt")


def test_check_availability_on_ftp(ftpserver):
    "Should correctly check availability of existing and non existing files"
    with data_over_ftp(ftpserver, "tiny-data.txt") as url:
        # Check available remote file on FTP server
        pup = Pooch(
            path=DATA_DIR,
            base_url=url.replace("tiny-data.txt", ""),
            registry={
                "tiny-data.txt": (
                    "baee0894dba14b12085eacb204284b97e362f4f3e5a5807693cc90ef415c1b2d"
                ),
                "doesnot_exist.zip": "jdjdjdjdflld",
            },
        )
        downloader = FTPDownloader(port=ftpserver.server_port)
        assert pup.is_available("tiny-data.txt", downloader=downloader)
        # Check non available remote file
        assert not pup.is_available("doesnot_exist.zip", downloader=downloader)


def test_check_availability_invalid_downloader():
    "Should raise an exception if the downloader doesn't support this"

    def downloader(url, output, pooch):  # noqa: ARG001
        "A downloader that doesn't support check_only"
        return

    pup = Pooch(path=DATA_DIR, base_url=BASEURL, registry=REGISTRY)
    msg = "does not support availability checks."
    with pytest.raises(NotImplementedError, match=msg):
        pup.is_available("tiny-data.txt", downloader=downloader)


@pytest.mark.network
def test_fetch_with_downloader(capsys):
    "Setup a downloader function for fetch"

    def download(url, output_file, pup):
        "Download through HTTP and warn that we're doing it"
        get_logger().info("downloader executed")
        HTTPDownloader()(url, output_file, pup)

    with TemporaryDirectory() as local_store:
        path = Path(local_store)
        # Setup a pooch in a temp dir
        pup = Pooch(path=path, base_url=BASEURL, registry=REGISTRY)
        # Check that the logs say that the file is being downloaded
        with capture_log() as log_file:
            fname = pup.fetch("large-data.txt", downloader=download)
            logs = log_file.getvalue()
            lines = logs.splitlines()
            assert len(lines) == 2
            assert lines[0].split()[0] == "Downloading"
            assert lines[1] == "downloader executed"
        # Read stderr and make sure no progress bar was printed by default
        assert not capsys.readouterr().err
        # Check that the downloaded file has the right content
        check_large_data(fname)
        # Check that no logging happens when not downloading
        with capture_log() as log_file:
            fname = pup.fetch("large-data.txt")
            assert log_file.getvalue() == ""


def test_invalid_hash_alg(data_dir_mirror):
    "Test an invalid hashing algorithm"
    pup = Pooch(
        path=data_dir_mirror, base_url=BASEURL, registry={"tiny-data.txt": "blah:1234"}
    )
    msg = re.escape("Algorithm 'blah' not available to the pooch library")
    with pytest.raises(ValueError, match=msg):
        pup.fetch("tiny-data.txt")


def test_alternative_hashing_algorithms(data_dir_mirror):
    "Test different hashing algorithms using local data"
    fname = str(data_dir_mirror / "tiny-data.txt")
    check_tiny_data(fname)
    with open(fname, "rb") as fin:
        data = fin.read()
    for alg in ("sha512", "md5"):
        hasher = hashlib.new(alg)
        hasher.update(data)
        registry = {"tiny-data.txt": f"{alg}:{hasher.hexdigest()}"}
        pup = Pooch(path=data_dir_mirror, base_url="some bogus URL", registry=registry)
        assert fname == pup.fetch("tiny-data.txt")
        check_tiny_data(fname)


def test_download_action():
    "Test that the right action is performed based on file existing"
    action, verb = download_action(
        Path("this_file_does_not_exist.txt"), known_hash=None
    )
    assert action == "download"
    assert verb == "Downloading"

    with temporary_file() as tmp:
        action, verb = download_action(Path(tmp), known_hash="not the correct hash")
    assert action == "update"
    assert verb == "Updating"

    with temporary_file() as tmp:
        with open(tmp, "w", encoding="utf-8") as output:
            output.write("some data")
        action, verb = download_action(Path(tmp), known_hash=file_hash(tmp))
    assert action == "fetch"
    assert verb == "Fetching"


@pytest.mark.network
@pytest.mark.parametrize("fname", ["tiny-data.txt", "subdir/tiny-data.txt"])
def test_stream_download(fname):
    "Check that downloading a file over HTTP works as expected"
    # Use the data in store/ because the subdir is in there for some reason
    url = BASEURL + "store/" + fname
    known_hash = REGISTRY[fname]
    downloader = HTTPDownloader()
    with TemporaryDirectory() as local_store:
        destination = Path(local_store) / fname
        assert not destination.exists()
        stream_download(url, destination, known_hash, downloader, pooch=None)
        assert destination.exists()
        check_tiny_data(str(destination))


@pytest.mark.network
@pytest.mark.parametrize(
    "url",
    [pytest.param(FIGSHAREURL, marks=pytest.mark.figshare), ZENODOURL, DATAVERSEURL],
    ids=["figshare", "zenodo", "dataverse"],
)
def test_load_registry_from_doi(url):
    """Check that the registry is correctly populated from the API"""
    with TemporaryDirectory() as local_store:
        path = os.path.abspath(local_store)
        pup = Pooch(path=path, base_url=url)
        pup.load_registry_from_doi()

        # Check the existence of all files in the registry
        assert len(pup.registry) == 2
        assert "tiny-data.txt" in pup.registry
        assert "store.zip" in pup.registry

        # Ensure that all files have correct checksums by fetching them
        for filename in pup.registry:
            pup.fetch(filename)


@pytest.mark.network
def test_load_registry_from_doi_zenodo_with_slash():
    """
    Check that the registry is correctly populated from the Zenodo API when
    the filename contains a slash
    """
    url = ZENODOURL_W_SLASH
    with TemporaryDirectory() as local_store:
        path = os.path.abspath(local_store)
        pup = Pooch(path=path, base_url=url)
        pup.load_registry_from_doi()

        # Check the existence of all files in the registry
        assert len(pup.registry) == 1
        assert "santisoler/pooch-test-data-v1.zip" in pup.registry

        # Ensure that all files have correct checksums by fetching them
        for filename in pup.registry:
            pup.fetch(filename)


def test_wrong_load_registry_from_doi():
    """Check that non-DOI URLs produce an error"""

    pup = Pooch(path="", base_url=BASEURL)

    msg = "only implemented for DOIs"
    with pytest.raises(ValueError, match=msg):
        pup.load_registry_from_doi()


def test_fetch_uses_one_registry_snapshot_while_refreshing(tmp_path):
    """
    A fetch must keep using the snapshot it started with.

    The downloader refreshes the registry before downloading. If the fetch
    re-read the live registry after that point, it would use the new hash and
    fail. It must instead finish against the version it started with.
    """
    path = tmp_path / "cache"
    path.mkdir()
    source = Path(DATA_DIR) / "tiny-data.txt"
    local = path / "tiny-data.txt"
    local.write_text("stale content", encoding="utf-8")
    old_hash = file_hash(str(source))
    new_hash = hashlib.sha256(b"a completely different file").hexdigest()
    pup = Pooch(
        path=path,
        base_url="some bogus URL",
        registry={"tiny-data.txt": old_hash},
        registry_version="2026.11.1",
    )

    def downloader(url, output_file, pooch):  # noqa: ARG001
        pooch.load_registry(
            io.StringIO(f"tiny-data.txt {new_hash}\n"), version="2026.11.2"
        )
        shutil.copyfile(str(source), output_file)

    fname = pup.fetch("tiny-data.txt", downloader=downloader)
    assert fname == str(path / "tiny-data.txt")
    check_tiny_data(fname)
    assert pup.registry_version == "2026.11.2"
    assert pup.registry["tiny-data.txt"] == new_hash


def test_fetches_use_fixed_snapshots_between_refreshes(tmp_path):
    "Two fetches without a refresh must both use the same registry version."
    path = tmp_path / "cache"
    path.mkdir()
    source = Path(DATA_DIR) / "tiny-data.txt"
    local = path / "tiny-data.txt"
    local.write_text("v1 content", encoding="utf-8")
    hash_v1 = file_hash(str(source))
    pup = Pooch(
        path=path,
        base_url="some bogus URL",
        registry={"tiny-data.txt": hash_v1},
        registry_version="v1",
    )
    seen = []

    def downloader(url, output_file, pooch):  # noqa: ARG001
        seen.append((pooch.registry_version, pooch.registry["tiny-data.txt"]))
        shutil.copyfile(str(source), output_file)

    pup.fetch("tiny-data.txt", downloader=downloader)
    # The contents already match the snapshot now, so the second fetch must
    # be a no-op that still reports the same fixed version.
    pup.fetch("tiny-data.txt", downloader=downloader)
    assert seen == [("v1", hash_v1)]
    assert pup.registry_version == "v1"


def test_failed_registry_refresh_keeps_old_registry(tmp_path):
    "A registry file with a bad line must not publish any of its entries."
    before = Pooch(
        path=tmp_path,
        base_url="",
        registry={"tiny-data.txt": REGISTRY["tiny-data.txt"]},
        registry_version="old",
    )
    with pytest.raises(OSError, match="Invalid entry in Pooch registry file"):
        before.load_registry(
            io.StringIO(
                "new-file.txt " + REGISTRY["large-data.txt"] + "\n"
                "broken line with too many elements here\n"
            ),
            version="broken",
        )
    assert before.registry == {"tiny-data.txt": REGISTRY["tiny-data.txt"]}
    assert before.registry_version == "old"
    assert "new-file.txt" not in before.registry
    assert [snapshot.version for snapshot in before.registry_history] == ["old"]


def test_refresh_registry_is_atomic(tmp_path):
    "A refresh whose source fails must leave the current registry untouched."
    pup = Pooch(
        path=tmp_path,
        base_url="",
        registry={"tiny-data.txt": REGISTRY["tiny-data.txt"]},
        registry_version="v1",
    )

    def failing_source(fileobj):
        fileobj.write(b"new-file.txt " + REGISTRY["large-data.txt"].encode() + b"\n")
        fileobj.flush()
        message = "connection dropped"
        raise RuntimeError(message)

    with pytest.raises(RuntimeError, match="connection dropped"):
        pup.refresh_registry(failing_source, version="v2")
    assert pup.registry == {"tiny-data.txt": REGISTRY["tiny-data.txt"]}
    assert pup.registry_version == "v1"


def test_omitted_file_is_not_a_retraction(tmp_path):
    "A file missing from a new registry is undeclared, not retracted."
    pup = Pooch(path=tmp_path, base_url="", registry=REGISTRY, registry_version="v1")
    pup.load_registry(
        io.StringIO(f"tiny-data.txt {REGISTRY['tiny-data.txt']}\n"),
        version="v2",
    )
    assert "large-data.txt" not in pup.registry
    assert "large-data.txt" not in pup.registry_snapshot.retracted
    with pytest.raises(ValueError, match="is not in the registry"):
        pup.fetch("large-data.txt")
    with pytest.raises(ValueError, match="is not in the registry"):
        pup.get_url("large-data.txt")
    with pytest.raises(ValueError, match="is not in the registry"):
        pup._assert_file_in_registry("large-data.txt")


def test_explicit_retraction_is_not_an_omission(tmp_path):
    "An explicit retraction must raise a distinct error from an omission."
    pup = Pooch(path=tmp_path, base_url="", registry=REGISTRY, registry_version="v1")
    pup.load_registry(
        io.StringIO(
            f"tiny-data.txt {REGISTRY['tiny-data.txt']}\n"
            "large-data.txt retracted replaced by a revised calibration\n"
        ),
        version="v2",
    )
    assert "large-data.txt" not in pup.registry
    assert "large-data.txt" in pup.registry_snapshot.retracted
    with pytest.raises(RetractedFileError, match="explicitly retracted"):
        pup.fetch("large-data.txt")
    with pytest.raises(RetractedFileError) as error:
        pup.get_url("large-data.txt")
    assert "revised calibration" in str(error.value)
    with pytest.raises(RetractedFileError, match="explicitly retracted"):
        pup._assert_file_in_registry("large-data.txt")
    assert issubclass(RetractedFileError, ValueError)


@pytest.mark.parametrize(
    "line",
    [
        "large-data.txt retracted because",
        "!large-data.txt because",
        "! large-data.txt because",
        "- large-data.txt because",
        "withdrawn large-data.txt because",
        f"large-data.txt {REGISTRY['large-data.txt']} retracted because",
    ],
)
def test_retraction_syntaxes(tmp_path, line):
    "All supported explicit retraction forms must parse as retractions."
    pup = Pooch(path=tmp_path, base_url="", registry=REGISTRY)
    pup.load_registry(io.StringIO(line + "\n"))
    assert "large-data.txt" in pup.registry_snapshot.retracted
    assert pup.registry_snapshot.reason("large-data.txt") == "because"


def test_retracted_file_can_be_revived(tmp_path):
    "A later entry for a retracted file must declare it again."
    pup = Pooch(path=tmp_path, base_url="", registry=REGISTRY)
    pup.load_registry(io.StringIO("large-data.txt retracted mistaken identity\n"))
    assert "large-data.txt" in pup.registry_snapshot.retracted
    pup.load_registry(io.StringIO(f"large-data.txt {REGISTRY['large-data.txt']}\n"))
    assert "large-data.txt" in pup.registry
    assert "large-data.txt" not in pup.registry_snapshot.retracted


def test_registry_replacement_removes_old_entries(tmp_path):
    "A replacing refresh publishes a complete, not merged, registry."
    pup = Pooch(
        path=tmp_path,
        base_url="",
        registry={"a.txt": "sha256:aa", "b.txt": "sha256:bb"},
        urls={"a.txt": "https://example.com/a.txt"},
        registry_version="v1",
    )
    pup.load_registry(io.StringIO("c.txt sha256:cc\n"), version="v2")
    assert pup.registry == {"c.txt": "sha256:cc"}
    assert pup.urls == {}


def test_registry_digest_is_content_addressed(tmp_path):
    "The same registry content must have the same digest regardless of order."
    first = Pooch(
        path=tmp_path,
        base_url="",
        registry={"a.txt": "sha256:aa", "b.txt": "sha256:bb"},
    )
    second = Pooch(
        path=tmp_path,
        base_url="",
        registry={"b.txt": "sha256:bb", "a.txt": "sha256:aa"},
    )
    assert first.registry_digest == second.registry_digest
    first.load_registry(io.StringIO("a.txt sha256:changed\n"))
    assert first.registry_digest != second.registry_digest


def test_old_registry_version_remains_fetchable(tmp_path):
    "A prior snapshot must stay usable and keep its own checksum and URL."
    path = tmp_path / "cache"
    path.mkdir()
    source = Path(DATA_DIR) / "tiny-data.txt"
    shutil.copyfile(str(source), str(path / "tiny-data.txt"))
    hash_v1 = file_hash(str(source))
    pup = Pooch(
        path=path,
        base_url="",
        urls={"tiny-data.txt": "https://old.example.com/tiny-data.txt"},
        registry={"tiny-data.txt": hash_v1},
        registry_version="v1",
    )
    pup.load_registry(
        io.StringIO(f"tiny-data.txt {REGISTRY['large-data.txt']}\n"),
        version="v2",
    )
    preserved = pup.fetch_version("tiny-data.txt", "v1")
    assert Path(preserved).read_text(encoding="utf-8") == source.read_text(
        encoding="utf-8"
    )
    assert Path(preserved) != path / "tiny-data.txt"
    old = pup.registry_at("v1")
    assert old.registry["tiny-data.txt"] == hash_v1
    assert old.get_url("tiny-data.txt") == "https://old.example.com/tiny-data.txt"


def test_pinned_fetch_can_read_retracted_snapshot(tmp_path):
    "Pinning an earlier version must keep working after a retraction."
    path = tmp_path / "cache"
    path.mkdir()
    source = Path(DATA_DIR) / "tiny-data.txt"
    shutil.copyfile(str(source), str(path / "tiny-data.txt"))
    hash_v1 = file_hash(str(source))
    pup = Pooch(
        path=path,
        base_url="",
        registry={"tiny-data.txt": hash_v1},
        registry_version="v1",
    )
    pup.retract("tiny-data.txt", reason="bad calibration")
    preserved = pup.fetch_version("tiny-data.txt", "v1")
    assert Path(preserved).read_text(encoding="utf-8") == source.read_text(
        encoding="utf-8"
    )
    with pytest.raises(RetractedFileError, match="bad calibration"):
        pup.fetch("tiny-data.txt")


def test_superseded_content_is_preserved_with_provenance(tmp_path):
    "Changing a checksum must keep the old content under a stable identity."
    path = tmp_path / "cache"
    path.mkdir()
    old_content = b"old version of the data\n"
    local = path / "tiny-data.txt"
    local.write_bytes(old_content)
    old_hash = file_hash(str(local))
    omitted_content = b"content that will be left undeclared\n"
    omitted = path / "other.txt"
    omitted.write_bytes(omitted_content)
    omitted_hash = file_hash(str(omitted))
    pup = Pooch(
        path=path,
        base_url="",
        registry={"tiny-data.txt": old_hash, "other.txt": omitted_hash},
        registry_version="v1",
    )
    pup.load_registry(
        io.StringIO(f"tiny-data.txt {REGISTRY['tiny-data.txt']}\n"),
        version="v2",
    )
    records = pup.provenance
    by_file = {record.fname: record for record in records}
    record = by_file["tiny-data.txt"]
    assert record.registry_version == "v1"
    assert record.status == "superseded"
    assert record.known_hash == old_hash
    assert record.path.read_bytes() == old_content
    assert record.path.name == old_hash
    index = path / ".pooch-history" / "index.json"
    assert index.exists()
    index_text = index.read_text(encoding="utf-8")
    assert '"registry_version": "v1"' in index_text
    assert '"status": "superseded"' in index_text
    assert by_file["other.txt"].status == "undeclared"
    assert by_file["other.txt"].path.read_bytes() == omitted_content


def test_retraction_preserves_local_content(tmp_path):
    "Retracting a file must preserve matching local content and metadata."
    path = tmp_path / "cache"
    path.mkdir()
    content = b"content that will be withdrawn\n"
    local = path / "data.txt"
    local.write_bytes(content)
    known_hash = file_hash(str(local))
    pup = Pooch(
        path=path,
        base_url="",
        registry={"data.txt": known_hash},
        registry_version="v1",
    )
    pup.retract("data.txt", reason="wrong instrument")
    assert pup.registry_snapshot.retracted == frozenset({"data.txt"})
    assert pup.registry_snapshot.reason("data.txt") == "wrong instrument"
    assert len(pup.provenance) == 1
    assert pup.provenance[0].status == "retracted"
    assert pup.provenance[0].path.read_bytes() == content
    with pytest.raises(RetractedFileError, match="wrong instrument"):
        pup.fetch("data.txt")


def test_refresh_registry_success(tmp_path):
    "A successful refresh must publish a complete new snapshot."
    pup = Pooch(
        path=tmp_path,
        base_url="",
        registry={"old.txt": "sha256:old"},
        registry_version="v1",
    )

    def source(fileobj):
        fileobj.write(f"tiny-data.txt {REGISTRY['tiny-data.txt']}\n".encode())

    snapshot = pup.refresh_registry(source, version="v2")
    assert snapshot.version == "v2"
    assert pup.registry == {"tiny-data.txt": REGISTRY["tiny-data.txt"]}
    assert "old.txt" not in pup.registry
    assert [item.version for item in pup.registry_history] == ["v1", "v2"]


def test_registry_version_defaults_to_digest(tmp_path):
    "Without an explicit label, the digest must be the version."
    pup = Pooch(path=tmp_path, base_url="", registry={"a.txt": "sha256:aa"})
    assert pup.registry_version == pup.registry_digest
    assert pup.registry_version.startswith("sha256:")


def test_load_registry_can_merge_for_compatibility(tmp_path):
    "Merging must remain available for callers that rely on it."
    pup = Pooch(path=tmp_path, base_url="", registry={"a.txt": "sha256:aa"})
    pup.load_registry(io.StringIO("b.txt sha256:bb\n"), replace=False)
    assert pup.registry == {"a.txt": "sha256:aa", "b.txt": "sha256:bb"}


def test_registry_error_types_are_distinct_but_backwards_compatible(tmp_path):
    "Absence and retraction must be distinguishable and still ValueErrors."
    pup = Pooch(
        path=tmp_path,
        base_url="",
        registry={"a.txt": "sha256:aa"},
        registry_version="v1",
    )
    pup.load_registry(io.StringIO("b.txt retracted gone\n"), version="v2")
    assert issubclass(FileNotInRegistryError, ValueError)
    assert issubclass(RetractedFileError, ValueError)
    assert not issubclass(RetractedFileError, FileNotInRegistryError)
    with pytest.raises(FileNotInRegistryError):
        pup.fetch("a.txt")
    with pytest.raises(RetractedFileError):
        pup.fetch("b.txt")
    assert type(pup.fetch.__self__) is Pooch


def test_api_errors_are_exposed_at_top_level():
    "The new registry errors and snapshot type must be importable from pooch."
    assert pooch.RegistryError is not None
    assert pooch.RegistrySnapshot is not None
    assert pooch.ProvenanceRecord is not None


def test_pooch_repr_contains_registry_version(tmp_path):
    "The repr must identify which registry version an instance is using."
    pup = Pooch(path=tmp_path, base_url="", registry={}, registry_version="v9")
    assert "registry_version='v9'" in repr(pup)


def test_provenance_is_not_created_for_unchanged_content(tmp_path):
    "Reloading the same registry must not create spurious history records."
    path = tmp_path / "cache"
    path.mkdir()
    content = b"stable content\n"
    local = path / "a.txt"
    local.write_bytes(content)
    known_hash = file_hash(str(local))
    pup = Pooch(
        path=path,
        base_url="",
        registry={"a.txt": known_hash},
        registry_version="v1",
    )
    pup.load_registry(io.StringIO(f"a.txt {known_hash}\n"), version="v2")
    assert pup.provenance == ()
    assert local.read_bytes() == content


def test_provenance_digest_is_shared_for_identical_content(tmp_path):
    "Identical old content stored under two names must share one blob."
    path = tmp_path / "cache"
    path.mkdir()
    content = b"same bytes\n"
    for name in ("a.txt", "b.txt"):
        (path / name).write_bytes(content)
    known_hash = file_hash(str(path / "a.txt"))
    pup = Pooch(
        path=path,
        base_url="",
        registry={"a.txt": known_hash, "b.txt": known_hash},
        registry_version="v1",
    )
    pup.load_registry(io.StringIO("c.txt sha256:cc\n"), version="v2")
    records = {record.fname: record for record in pup.provenance}
    assert records["a.txt"].path == records["b.txt"].path
    assert records["a.txt"].path.read_bytes() == content


def test_load_registry_accepts_binary_file_object(tmp_path):
    "Binary registry files must still be parsed and published atomically."
    pup = Pooch(path=tmp_path, base_url="", registry={"old.txt": "sha256:old"})
    data = io.BytesIO(b"new.txt sha256:new\n")
    snapshot = pup.load_registry(data, version="v2")
    assert snapshot.version == "v2"
    assert pup.registry == {"new.txt": "sha256:new"}


def test_load_registry_rejects_empty_retraction(tmp_path):
    "A retraction without a file name must fail without changing state."
    pup = Pooch(
        path=tmp_path,
        base_url="",
        registry={"a.txt": "sha256:aa"},
        registry_version="v1",
    )
    with pytest.raises(OSError, match="missing a file name"):
        pup.load_registry(io.StringIO("!\n"), version="bad")
    assert pup.registry == {"a.txt": "sha256:aa"}
    assert pup.registry_version == "v1"


def test_pinned_version_download_uses_isolated_folder(tmp_path):
    "A pinned fetch that must download must not write over current content."
    path = tmp_path / "cache"
    path.mkdir()
    pup = Pooch(
        path=path,
        base_url="",
        urls={"tiny-data.txt": "https://example.invalid/tiny-data.txt"},
        registry={"tiny-data.txt": "sha256:" + "0" * 64},
        registry_version="v1",
    )
    pup.load_registry(io.StringIO(f"other.txt {REGISTRY['large-data.txt']}\n"))
    target = path / "tiny-data.txt"
    target.write_bytes(b"current version content")
    before = target.read_bytes()
    with pytest.raises(requests.exceptions.ConnectionError):
        pup.fetch("tiny-data.txt", registry_version="v1")
    assert target.read_bytes() == before
    version_folder = pup._version_cache_root(pup.registry_at("v1"))
    assert version_folder.name.startswith("sha256-")
    assert version_folder.parent.name == ".pooch-versions"


def test_fetch_rejects_unknown_registry_version(tmp_path):
    "Pinning a version that was never published must fail clearly."
    pup = Pooch(path=tmp_path, base_url="", registry={"a.txt": "sha256:aa"})
    with pytest.raises(KeyError, match="Unknown registry version"):
        pup.fetch("a.txt", registry_version="does-not-exist")


def test_registry_versions_listing_and_retraction_queries(tmp_path):
    "The public helpers must report history and retraction state."
    pup = Pooch(
        path=tmp_path,
        base_url="",
        registry={"a.txt": "sha256:aa"},
        registry_version="v1",
    )
    pup.retract("a.txt", reason="broke", version="v2")
    assert pup.registry_versions == ["v1", "v2"]
    assert pup.retracted_files == ["a.txt"]
    assert pup.is_retracted("a.txt")
    assert not pup.is_retracted("b.txt")


def test_provenance_survives_a_new_pooch_instance(tmp_path):
    "Preserved content must remain identifiable after a process restart."
    path = tmp_path / "cache"
    path.mkdir()
    first = path / "tiny-data.txt"
    first.write_bytes(b"first generation\n")
    first_hash = file_hash(str(first))
    pup = Pooch(
        path=path,
        base_url="",
        registry={"tiny-data.txt": first_hash},
        registry_version="v1",
    )
    pup.load_registry(
        io.StringIO(f"tiny-data.txt {REGISTRY['tiny-data.txt']}\n"), version="v2"
    )
    preserved = pup.provenance[0].path
    assert preserved.exists()

    restarted = Pooch(
        path=path,
        base_url="",
        registry={"tiny-data.txt": REGISTRY["tiny-data.txt"]},
        registry_version="v2",
    )
    assert len(restarted.provenance) == 1
    record = restarted.provenance[0]
    assert record.fname == "tiny-data.txt"
    assert record.status == "superseded"
    assert record.registry_version == "v1"
    assert record.path == preserved
    assert preserved.read_bytes() == b"first generation\n"


def test_is_available_distinguishes_retraction(tmp_path):
    "Availability checks must not report a retracted file as simply missing."
    pup = Pooch(
        path=tmp_path,
        base_url="",
        registry={"a.txt": "sha256:aa"},
        registry_version="v1",
    )
    pup.retract("a.txt", reason="poisoned")
    with pytest.raises(RetractedFileError, match="poisoned"):
        pup.is_available("a.txt")
    with pytest.raises(FileNotInRegistryError):
        pup.is_available("never-declared.txt")


def test_load_registry_from_doi_failure_is_atomic(tmp_path, monkeypatch):
    "A repository API failure must not publish a partial DOI registry."
    pup = Pooch(
        path=tmp_path,
        base_url="doi:10.0000/example",
        registry={"a.txt": "sha256:aa"},
        registry_version="v1",
    )

    class Repository:
        def populate_registry(self, pooch):
            pooch.registry["partial.txt"] = "sha256:bb"
            message = "API failed mid-listing"
            raise RuntimeError(message)

    def make_repository(*args, **kwargs):  # noqa: ARG001
        return Repository()

    class Downloader:
        def __init__(self):
            self.headers = {}
            self.timeout = 1
            self.kwargs = {}

    def make_downloader(*args, **kwargs):  # noqa: ARG001
        return Downloader()

    monkeypatch.setattr(core, "doi_to_repository", make_repository)
    monkeypatch.setattr(core, "choose_downloader", make_downloader)
    monkeypatch.setattr(core, "DOIDownloader", Downloader)
    with pytest.raises(RuntimeError, match="mid-listing"):
        pup.load_registry_from_doi()
    assert pup.registry == {"a.txt": "sha256:aa"}
    assert pup.registry_version == "v1"


def test_registry_versions_survive_a_new_pooch_instance(tmp_path):
    "A restart must be able to resolve and fetch a previously preserved version."
    path = tmp_path / "cache"
    path.mkdir()
    first = path / "tiny-data.txt"
    first.write_bytes(b"first generation\n")
    first_hash = file_hash(str(first))
    pup = Pooch(
        path=path,
        base_url="",
        registry={"tiny-data.txt": first_hash},
        registry_version="v1",
    )
    pup.load_registry(
        io.StringIO(f"tiny-data.txt {REGISTRY['tiny-data.txt']}\n"), version="v2"
    )
    preserved = pup.provenance[0].path

    restarted = Pooch(
        path=path,
        base_url="",
        registry={"tiny-data.txt": REGISTRY["tiny-data.txt"]},
        registry_version="v2",
    )
    assert restarted.registry_versions == ["v1", "v2"]
    old = restarted.registry_at("v1")
    assert old.registry["tiny-data.txt"] == first_hash
    fetched = restarted.fetch_version("tiny-data.txt", "v1")
    assert Path(fetched) == preserved
    assert Path(fetched).read_bytes() == b"first generation\n"
