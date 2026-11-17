# Copyright (c) 2018 The Pooch Developers.
# Distributed under the terms of the BSD 3-Clause License.
# SPDX-License-Identifier: BSD-3-Clause
#
# This code is part of the Fatiando a Terra project (https://www.fatiando.org)
#
"""
Test the processing manifests that cache processor outputs.
"""

import os
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from .. import Decompress, Pooch, Unzip, file_hash, retrieve
from ..processing import (
    MANIFEST_PREFIX,
    PROCESSING_FORMAT_VERSION,
    ProcessingManifest,
    manifest_path,
    processor_identity,
)
from .utils import capture_log, check_tiny_data, pooch_test_registry

DATA_DIR = str(Path(__file__).parent / "data")
REGISTRY = pooch_test_registry()


def _local_downloader(source):
    "Downloader that copies a local test file instead of using the network."

    def downloader(url, output, pooch):  # noqa: ARG001
        shutil.copyfile(source, output)

    return downloader


def _manifests(directory):
    return list(Path(directory).glob(f"*{MANIFEST_PREFIX}*.json"))


def test_manifest_records_source_processor_and_artifacts():
    "A complete manifest must register the source, processor and products"
    with TemporaryDirectory() as local_store:
        source = Path(DATA_DIR) / "store.zip"
        pup = Pooch(path=Path(local_store), base_url="", registry=REGISTRY)
        processor = Unzip()
        fnames = pup.fetch(
            "store.zip",
            processor=processor,
            downloader=_local_downloader(source),
        )

        manifest_file = manifest_path(
            str(Path(local_store) / "store.zip"),
            processor.processing_identity(),
        )
        manifest = ProcessingManifest.read(manifest_file)
        assert manifest is not None
        assert manifest.status == "complete"
        assert manifest.error is None
        assert manifest.source["file_name"] == "store.zip"
        assert manifest.source["hash_algorithm"] == "sha256"
        assert manifest.source["hash"] == REGISTRY["store.zip"]
        assert manifest.processor["type"].endswith("Unzip")
        assert manifest.artifacts == [os.path.abspath(name) for name in fnames]
        assert all(Path(artifact).exists() for artifact in manifest.artifacts)
        assert manifest.build_result() == fnames


def test_complete_manifest_is_reused_without_processing_again():
    "A second fetch with the same configuration must hit the manifest cache"
    with TemporaryDirectory() as local_store:
        source = Path(DATA_DIR) / "store.zip"
        pup = Pooch(path=Path(local_store), base_url="", registry=REGISTRY)
        first = pup.fetch(
            "store.zip",
            processor=Unzip(),
            downloader=_local_downloader(source),
        )
        with capture_log() as log_file:
            second = pup.fetch(
                "store.zip",
                processor=Unzip(),
                downloader=_local_downloader(source),
            )
        assert second == first
        # No extraction log means the processor was not executed again.
        assert log_file.getvalue() == ""


def test_different_parameters_get_different_manifests_and_products():
    "Derived files of different processor parameters must never be shared"
    with TemporaryDirectory() as local_store:
        source = Path(DATA_DIR) / "store.zip"
        pup = Pooch(path=Path(local_store), base_url="", registry=REGISTRY)

        all_files = pup.fetch(
            "store.zip",
            processor=Unzip(),
            downloader=_local_downloader(source),
        )
        one_file = pup.fetch(
            "store.zip",
            processor=Unzip(members=["store/tiny-data.txt"]),
            downloader=_local_downloader(source),
        )

        assert len(all_files) != len(one_file)
        assert set(one_file).issubset(set(all_files))
        identities = [
            Unzip().processing_identity(),
            Unzip(members=["store/tiny-data.txt"]).processing_identity(),
        ]
        assert identities[0] != identities[1]
        manifest_files = [
            manifest_path(str(Path(local_store) / "store.zip"), identity)
            for identity in identities
        ]
        assert manifest_files[0] != manifest_files[1]
        assert all(Path(name).exists() for name in manifest_files)
        assert len(_manifests(local_store)) == 2
        # Each manifest only lists the products of its own configuration.
        all_manifest = ProcessingManifest.read(manifest_files[0])
        one_manifest = ProcessingManifest.read(manifest_files[1])
        assert len(all_manifest.artifacts) == len(all_files)
        assert one_manifest.artifacts == [os.path.abspath(name) for name in one_file]


def test_different_decompress_names_do_not_share_products():
    "Two decompression outputs must coexist with their own manifests"
    with TemporaryDirectory() as local_store:
        source = Path(DATA_DIR) / "tiny-data.txt.gz"
        pup = Pooch(path=Path(local_store), base_url="", registry=REGISTRY)

        default = pup.fetch(
            "tiny-data.txt.gz",
            processor=Decompress(),
            downloader=_local_downloader(source),
        )
        named = pup.fetch(
            "tiny-data.txt.gz",
            processor=Decompress(name="other-name.txt"),
            downloader=_local_downloader(source),
        )

        assert default != named
        assert Path(default).exists()
        assert Path(named).exists()
        check_tiny_data(default)
        check_tiny_data(named)
        assert len(_manifests(local_store)) == 2


class FlakyUnzip(Unzip):
    "Unzip processor that fails on its first invocation and works afterwards."

    attempts = 0

    def _extract_file(self, fname, extract_dir):
        FlakyUnzip.attempts += 1
        if FlakyUnzip.attempts == 1:
            os.makedirs(extract_dir, exist_ok=True)
            Path(extract_dir, "partial.txt").write_text("partial")
            message = "boom"
            raise RuntimeError(message)
        super()._extract_file(fname, extract_dir)


def test_failed_processing_is_not_a_cache_hit_and_can_be_retried():
    "Half products must be discarded and the source kept for reprocessing"
    with TemporaryDirectory() as local_store:
        source = Path(DATA_DIR) / "store.zip"
        source_path = Path(local_store) / "store.zip"
        pup = Pooch(path=Path(local_store), base_url="", registry=REGISTRY)

        failing = FlakyUnzip(extract_dir="flaky-out")
        with pytest.raises(RuntimeError, match="boom"):
            pup.fetch(
                "store.zip",
                processor=failing,
                downloader=_local_downloader(source),
            )

        # The source is untouched and the partial output is gone.
        assert source_path.exists()
        assert not (Path(local_store) / "flaky-out").exists()

        manifest_file = manifest_path(str(source_path), failing.processing_identity())
        failed = ProcessingManifest.read(manifest_file)
        assert failed is not None
        assert failed.status == "failed"
        assert "boom" in (failed.error or "")
        assert failed.artifacts == []

        # A new instance with the same identity must not reuse the failure.
        retry = FlakyUnzip(extract_dir="flaky-out")
        assert retry.processing_identity() == failing.processing_identity()
        fnames = pup.fetch(
            "store.zip",
            processor=retry,
            downloader=_local_downloader(source),
        )
        assert fnames
        assert all(Path(name).exists() for name in fnames)
        completed = ProcessingManifest.read(manifest_file)
        assert completed.status == "complete"
        assert completed.artifacts == [os.path.abspath(name) for name in fnames]


def test_missing_artifact_forces_reprocessing():
    "A product deleted from disk invalidates the manifest cache hit"
    with TemporaryDirectory() as local_store:
        source = Path(DATA_DIR) / "store.zip"
        pup = Pooch(path=Path(local_store), base_url="", registry=REGISTRY)
        processor = Unzip(members=["store/tiny-data.txt"])
        fnames = pup.fetch(
            "store.zip",
            processor=processor,
            downloader=_local_downloader(source),
        )
        assert len(fnames) == 1
        os.remove(fnames[0])

        with capture_log() as log_file:
            recovered = pup.fetch(
                "store.zip",
                processor=Unzip(members=["store/tiny-data.txt"]),
                downloader=_local_downloader(source),
            )
        assert recovered == fnames
        assert Path(fnames[0]).exists()
        assert "Extracting" in log_file.getvalue()


def test_changed_source_invalidates_manifest_and_replaces_products():
    "Updating the source must reprocess and remove stale derived files"
    with TemporaryDirectory() as local_store:
        old_source = Path(DATA_DIR) / "store.zip"
        new_source = Path(DATA_DIR) / "tiny-data.zip"
        # Use a private registry copy so the module-level one is not mutated.
        pup = Pooch(path=Path(local_store), base_url="", registry=dict(REGISTRY))
        processor = Unzip()

        pup.fetch(
            "store.zip",
            processor=processor,
            downloader=_local_downloader(old_source),
        )
        extracted_dir = Path(local_store) / "store.zip.unzip"
        assert (extracted_dir / "store").is_dir()

        new_hash = file_hash(str(new_source))
        pup.registry["store.zip"] = new_hash
        fnames = pup.fetch(
            "store.zip",
            processor=Unzip(),
            downloader=_local_downloader(new_source),
        )

        # Files from the old archive version are gone, only the new products
        # remain and the manifest points at the new source.
        assert not (extracted_dir / "store").exists()
        assert len(fnames) == 1
        manifest = ProcessingManifest.read(
            manifest_path(
                str(Path(local_store) / "store.zip"),
                processor.processing_identity(),
            )
        )
        assert manifest.source["hash"] == new_hash
        assert manifest.artifacts == [os.path.abspath(name) for name in fnames]


def test_plain_processor_function_keeps_working_without_manifest():
    "Processors without the manifest protocol must behave exactly as before"
    with TemporaryDirectory() as local_store:
        source = Path(DATA_DIR) / "tiny-data.txt"

        def plain_processor(fname, action, pooch):  # noqa: ARG001
            product = fname + ".plain"
            Path(product).write_text("processed", encoding="utf-8")
            return product

        assert processor_identity(plain_processor) is None
        pup = Pooch(path=Path(local_store), base_url="", registry=REGISTRY)
        result = pup.fetch(
            "tiny-data.txt",
            processor=plain_processor,
            downloader=_local_downloader(source),
        )
        assert result == str(Path(local_store) / "tiny-data.txt.plain")
        assert _manifests(local_store) == []


def test_retrieve_uses_processing_manifest():
    "The standalone retrieve entry point must use the same manifest cache"
    with TemporaryDirectory() as local_store:
        source = Path(DATA_DIR) / "tiny-data.txt.gz"
        known_hash = REGISTRY["tiny-data.txt.gz"]
        common = {
            "url": "https://example.com/tiny-data.txt.gz",
            "known_hash": known_hash,
            "path": local_store,
            "fname": "tiny-data.txt.gz",
            "processor": Decompress(),
            "downloader": _local_downloader(source),
        }
        first = retrieve(**common)
        second = retrieve(**common)
        assert first == second
        check_tiny_data(first)
        assert len(_manifests(local_store)) == 1


def test_corrupted_manifest_is_ignored():
    "An unreadable manifest must never be treated as a valid cache hit"
    with TemporaryDirectory() as local_store:
        source = Path(DATA_DIR) / "store.zip"
        pup = Pooch(path=Path(local_store), base_url="", registry=REGISTRY)
        processor = Unzip()
        fnames = pup.fetch(
            "store.zip",
            processor=processor,
            downloader=_local_downloader(source),
        )

        manifest_file = Path(
            manifest_path(
                str(Path(local_store) / "store.zip"),
                processor.processing_identity(),
            )
        )
        manifest_file.write_text("not valid json", encoding="utf-8")
        assert ProcessingManifest.read(str(manifest_file)) is None

        recovered = pup.fetch(
            "store.zip",
            processor=Unzip(),
            downloader=_local_downloader(source),
        )
        assert recovered == fnames
        # A fresh, valid manifest is written in place of the corrupted one.
        rewritten = ProcessingManifest.read(str(manifest_file))
        assert rewritten is not None
        assert rewritten.status == "complete"
        assert rewritten.artifacts == [os.path.abspath(name) for name in fnames]


def test_extractor_identity_covers_parameters():
    "The identity must change when members or extract_dir change"
    assert (
        Unzip().processing_identity()
        != Unzip(members=["store/tiny-data.txt"]).processing_identity()
    )
    assert (
        Unzip(extract_dir="a").processing_identity()
        != Unzip(extract_dir="b").processing_identity()
    )
    identity = Unzip(members=["b", "a"]).processing_identity()
    # Member order must not matter for the identity.
    assert identity == Unzip(members=["a", "b"]).processing_identity()
    assert isinstance(identity, dict)
    assert identity["type"].endswith("Unzip")


def test_outdated_manifest_format_is_not_reused():
    "A manifest written by a newer format must be treated as a cache miss"
    with TemporaryDirectory() as local_store:
        source = Path(DATA_DIR) / "store.zip"
        pup = Pooch(path=Path(local_store), base_url="", registry=REGISTRY)
        processor = Unzip()
        fnames = pup.fetch(
            "store.zip",
            processor=processor,
            downloader=_local_downloader(source),
        )

        manifest_file = manifest_path(
            str(Path(local_store) / "store.zip"),
            processor.processing_identity(),
        )
        manifest = ProcessingManifest.read(manifest_file)
        assert manifest is not None
        manifest.format_version = PROCESSING_FORMAT_VERSION + 1
        manifest.write(manifest_file)

        recovered = pup.fetch(
            "store.zip",
            processor=Unzip(),
            downloader=_local_downloader(source),
        )
        assert recovered == fnames
        # The stale manifest is replaced by one in the current format.
        rewritten = ProcessingManifest.read(manifest_file)
        assert rewritten is not None
        assert rewritten.format_version == PROCESSING_FORMAT_VERSION
        assert rewritten.status == "complete"
