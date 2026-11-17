# Copyright (c) 2018 The Pooch Developers.
# Distributed under the terms of the BSD 3-Clause License.
# SPDX-License-Identifier: BSD-3-Clause
#
# This code is part of the Fatiando a Terra project (https://www.fatiando.org)
#
"""
Test downloading a single file from multiple mirrors.
"""

import threading
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from .. import mirrors
from ..hashes import file_hash
from ..mirrors import (
    QUARANTINE_DIR,
    MirrorDownloadCancelled,
    MirrorDownloadError,
    retrieve_from_mirrors,
    source_allowed,
)
from .utils import capture_log, check_tiny_data

DATA_DIR = Path(__file__).parent / "data"
TINY_DATA = DATA_DIR / "tiny-data.txt"
KNOWN_HASH = file_hash(str(TINY_DATA))

MIRROR_A = "https://mirror-a.example.org/data/tiny-data.txt"
MIRROR_B = "https://mirror-b.example.org/data/tiny-data.txt"
MIRROR_C = "https://mirror-c.example.org/data/tiny-data.txt"


class FakeDownloader:
    """
    Serve preset content for each URL and record the requested URLs.
    """

    def __init__(self, contents):
        # Maps a URL to the content to serve (str/bytes) or an exception to
        # raise when the URL is requested.
        self.contents = contents
        self.requested = []

    def __call__(self, url, output_file, pooch):  # noqa: ARG002
        self.requested.append(url)
        content = self.contents[url]
        if isinstance(content, BaseException):
            raise content
        if isinstance(content, str):
            content = content.encode()
        with open(output_file, "wb") as output:
            output.write(content)


@pytest.fixture(autouse=True)
def no_network_redirects(monkeypatch):
    """
    Replace redirect resolution with an identity function by default.

    Tests that need redirects monkeypatch ``mirrors.resolve_final_url``
    themselves.
    """
    monkeypatch.setattr(
        mirrors,
        "resolve_final_url",
        lambda url, timeout=None: url,  # noqa: ARG005
    )


def tiny_data_content():
    "The content of the tiny-data.txt test file."
    return TINY_DATA.read_bytes()


def test_source_allowed():
    "Check the allowed sources matching rules"
    url = "https://data.example.org/mirror/tiny-data.txt"
    assert source_allowed(url, None)
    assert source_allowed(url, [])
    assert source_allowed(url, ["https://data.example.org"])
    assert source_allowed(url, ["data.example.org"])
    assert source_allowed(url, ["https"])
    assert source_allowed(url, ["https://data.example.org/mirror"])
    assert source_allowed(url, ["http://other.org", "data.example.org"])
    assert not source_allowed(url, ["http://data.example.org"])
    assert not source_allowed(url, ["other.example.org"])
    assert not source_allowed(url, ["ftp"])
    assert not source_allowed(url, ["https://data.example.org/other"])


def test_mirrors_first_mirror_succeeds():
    "The first working mirror provides the file and later ones aren't used"
    with TemporaryDirectory() as local_store:
        downloader = FakeDownloader({MIRROR_A: tiny_data_content()})
        with capture_log() as log_file:
            full_path, attempts = retrieve_from_mirrors(
                [MIRROR_A, MIRROR_B],
                known_hash=KNOWN_HASH,
                fname="tiny-data.txt",
                path=local_store,
                downloader=downloader,
            )
        assert downloader.requested == [MIRROR_A]
        assert full_path == str(Path(local_store).resolve() / "tiny-data.txt")
        check_tiny_data(full_path)
        assert len(attempts) == 1
        assert attempts[0].status == "success"
        assert attempts[0].url == MIRROR_A
        assert attempts[0].final_url == MIRROR_A
        # The attempt and the final address are logged
        logs = log_file.getvalue()
        assert "Attempting mirror 1/2" in logs
        assert f"final address '{MIRROR_A}'" in logs


def test_mirrors_fallback_quarantines_divergent_content():
    "A mirror serving same-name different content is quarantined, not erased"
    with TemporaryDirectory() as local_store:
        downloader = FakeDownloader(
            {MIRROR_A: "divergent content", MIRROR_B: tiny_data_content()}
        )
        full_path, attempts = retrieve_from_mirrors(
            [MIRROR_A, MIRROR_B],
            known_hash=KNOWN_HASH,
            fname="tiny-data.txt",
            path=local_store,
            downloader=downloader,
        )
        # Both mirrors were tried and the good one provided the file
        assert downloader.requested == [MIRROR_A, MIRROR_B]
        check_tiny_data(full_path)
        assert [a.status for a in attempts] == ["hash_mismatch", "success"]
        # The divergent content was quarantined and not erased by the later
        # successful download
        quarantined = attempts[0].quarantined_to
        assert quarantined is not None
        assert Path(quarantined).parent == Path(local_store) / QUARANTINE_DIR
        assert Path(quarantined).exists()
        with open(quarantined, encoding="utf-8") as f:
            assert f.read() == "divergent content"
        # The final file has the verified content
        assert file_hash(full_path) == KNOWN_HASH


def test_mirrors_disallowed_source_is_skipped():
    "Mirrors outside the allowed sources are not contacted"
    with TemporaryDirectory() as local_store:
        downloader = FakeDownloader({MIRROR_B: tiny_data_content()})
        full_path, attempts = retrieve_from_mirrors(
            [MIRROR_A, MIRROR_B],
            known_hash=KNOWN_HASH,
            fname="tiny-data.txt",
            path=local_store,
            allowed_sources=["mirror-b.example.org"],
            downloader=downloader,
        )
        assert downloader.requested == [MIRROR_B]
        check_tiny_data(full_path)
        assert [a.status for a in attempts] == ["source_not_allowed", "success"]
        assert attempts[0].final_url is None


def test_mirrors_redirect_records_final_address(monkeypatch):
    "Redirects are followed, checked, and the final address is recorded"
    final = "https://cdn.example.org/tiny-data.txt"
    monkeypatch.setattr(
        mirrors,
        "resolve_final_url",
        lambda url, timeout=None: {MIRROR_A: final}.get(url, url),  # noqa: ARG005
    )
    with TemporaryDirectory() as local_store:
        downloader = FakeDownloader({final: tiny_data_content()})
        with capture_log() as log_file:
            full_path, attempts = retrieve_from_mirrors(
                [MIRROR_A],
                known_hash=KNOWN_HASH,
                fname="tiny-data.txt",
                path=local_store,
                allowed_sources=["mirror-a.example.org", "cdn.example.org"],
                downloader=downloader,
            )
        # The download used the final (redirected) address
        assert downloader.requested == [final]
        check_tiny_data(full_path)
        assert attempts[0].status == "success"
        assert attempts[0].url == MIRROR_A
        assert attempts[0].final_url == final
        assert f"redirected to final address '{final}'" in log_file.getvalue()


def test_mirrors_redirect_to_disallowed_source(monkeypatch):
    "A redirect to a source outside the allowed ones rejects the mirror"
    monkeypatch.setattr(
        mirrors,
        "resolve_final_url",
        lambda url, timeout=None: {  # noqa: ARG005
            MIRROR_A: "https://evil.example.org/tiny-data.txt"
        }.get(url, url),
    )
    with TemporaryDirectory() as local_store:
        downloader = FakeDownloader({MIRROR_B: tiny_data_content()})
        full_path, attempts = retrieve_from_mirrors(
            [MIRROR_A, MIRROR_B],
            known_hash=KNOWN_HASH,
            fname="tiny-data.txt",
            path=local_store,
            allowed_sources=["mirror-a.example.org", "mirror-b.example.org"],
            downloader=downloader,
        )
        # The evil redirect target was never requested
        assert downloader.requested == [MIRROR_B]
        check_tiny_data(full_path)
        assert [a.status for a in attempts] == ["source_not_allowed", "success"]
        assert attempts[0].final_url == "https://evil.example.org/tiny-data.txt"


def test_mirrors_cancel_event_stops_fallback():
    "Once cancelled, no new fallback requests to other mirrors are started"
    cancel_event = threading.Event()
    requested = []

    def cancelling_downloader(url, output_file, pooch):  # noqa: ARG001
        requested.append(url)
        cancel_event.set()
        msg = "connection dropped"
        raise ConnectionError(msg)

    with TemporaryDirectory() as local_store:
        with pytest.raises(MirrorDownloadCancelled) as excinfo:
            retrieve_from_mirrors(
                [MIRROR_A, MIRROR_B, MIRROR_C],
                known_hash=KNOWN_HASH,
                fname="tiny-data.txt",
                path=local_store,
                downloader=cancelling_downloader,
                cancel_event=cancel_event,
            )
        # No new fallback requests were started after the cancellation
        assert requested == [MIRROR_A]
        assert [a.status for a in excinfo.value.attempts] == [
            "download_error",
            "cancelled",
        ]


def test_mirrors_keyboard_interrupt_stops_fallback():
    "A KeyboardInterrupt cancels the operation without trying more mirrors"
    downloader = FakeDownloader({MIRROR_A: KeyboardInterrupt()})
    with TemporaryDirectory() as local_store:
        with pytest.raises(MirrorDownloadCancelled) as excinfo:
            retrieve_from_mirrors(
                [MIRROR_A, MIRROR_B],
                known_hash=KNOWN_HASH,
                fname="tiny-data.txt",
                path=local_store,
                downloader=downloader,
            )
        # Only the cancelled mirror was contacted
        assert downloader.requested == [MIRROR_A]
        assert [a.status for a in excinfo.value.attempts] == ["cancelled"]


def test_mirrors_download_error_falls_back():
    "A failed download falls back to the next mirror"
    with TemporaryDirectory() as local_store:
        downloader = FakeDownloader(
            {MIRROR_A: ConnectionError("down"), MIRROR_B: tiny_data_content()}
        )
        full_path, attempts = retrieve_from_mirrors(
            [MIRROR_A, MIRROR_B],
            known_hash=KNOWN_HASH,
            fname="tiny-data.txt",
            path=local_store,
            downloader=downloader,
        )
        check_tiny_data(full_path)
        assert [a.status for a in attempts] == ["download_error", "success"]
        assert "down" in attempts[0].error


def test_mirrors_all_fail_raises_with_records():
    "Failing all mirrors raises an error that records every attempt"
    with TemporaryDirectory() as local_store:
        downloader = FakeDownloader(
            {MIRROR_A: "divergent A", MIRROR_B: ConnectionError("down")}
        )
        with pytest.raises(MirrorDownloadError) as excinfo:
            retrieve_from_mirrors(
                [MIRROR_A, MIRROR_B],
                known_hash=KNOWN_HASH,
                fname="tiny-data.txt",
                path=local_store,
                downloader=downloader,
            )
        attempts = excinfo.value.attempts
        assert [a.status for a in attempts] == ["hash_mismatch", "download_error"]
        # The divergent content is still quarantined even though everything
        # failed
        assert Path(attempts[0].quarantined_to).exists()
        message = str(excinfo.value)
        assert MIRROR_A in message
        assert MIRROR_B in message
        assert "quarantined" in message


def test_mirrors_cached_file_skips_mirrors():
    "A cached file that passes verification doesn't contact any mirror"
    with TemporaryDirectory() as local_store:
        path = Path(local_store)
        (path / "tiny-data.txt").write_bytes(tiny_data_content())
        downloader = FakeDownloader({})
        full_path, attempts = retrieve_from_mirrors(
            [MIRROR_A],
            known_hash=KNOWN_HASH,
            fname="tiny-data.txt",
            path=local_store,
            downloader=downloader,
        )
        assert downloader.requested == []
        assert attempts == []
        check_tiny_data(full_path)


def test_mirrors_no_urls_raises():
    "Passing no mirrors is an error"
    with pytest.raises(ValueError, match="at least one mirror"):
        retrieve_from_mirrors([], known_hash=KNOWN_HASH)
