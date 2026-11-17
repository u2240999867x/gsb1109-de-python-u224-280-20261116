# Copyright (c) 2018 The Pooch Developers.
# Distributed under the terms of the BSD 3-Clause License.
# SPDX-License-Identifier: BSD-3-Clause
#
# This code is part of the Fatiando a Terra project (https://www.fatiando.org)
#
"""
Processing manifests for the files produced by processors.

When a processor unpacks or converts a downloaded (source) file, the derived
files are cached next to the source. A *processing manifest* records the
information that is needed to decide whether the cached derived files can
safely be reused:

* the verification information of the source file (name, hash algorithm,
  hash and size),
* the identity of the processor, meaning its type together with the
  parameters it was called with, and
* the list of files (artifacts) that the processor produced.

One manifest is written next to the source file for each processor
configuration. The same source can therefore be processed with different
parameters without the derived files ever being mixed up. If processing
fails, the partial outputs are removed and a "failed" manifest is written so
that a half-finished result can never be mistaken for a valid cache hit. The
source file itself is never modified or deleted, so it remains available to
be processed again.
"""

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, cast, runtime_checkable

from .hashes import file_hash, hash_algorithm
from .typing import Action, PathType, Processor
from .utils import get_logger

PROCESSING_FORMAT_VERSION = 1
MANIFEST_PREFIX = ".processing-"
MANIFEST_SUFFIX = ".json"


@runtime_checkable
class ManifestProcessor(Protocol):
    """
    Processor that participates in manifest-based caching.

    A processor opts in by implementing the three ``processing_*`` methods.
    """

    def processing_identity(self) -> dict[str, Any]:
        """Stable identity of the processor configuration."""

    def processing_artifacts(self, result: Any) -> list[PathType]:
        """Paths of the files present in the processor result."""

    def processing_discard(self, fname: str) -> None:
        """Remove the outputs of a failed processing attempt."""


@dataclass
class ProcessingManifest:
    """
    Record of a single processing run of one source file.

    Parameters
    ----------
    source : dict
        Fingerprint of the verified source file.
    processor : dict
        Identity (type and parameters) of the processor.
    artifacts : list of str
        Absolute paths of the files produced by the processor.
    result_kind : str
        How the return value of the processor is rebuilt from the artifacts.
        Either ``"path"`` (a single file path) or ``"list"`` (a list of
        paths).
    status : str
        ``"complete"`` if the processing finished successfully or
        ``"failed"`` otherwise.
    error : str or None
        Message of the error that caused the processing to fail, if any.
    format_version : int
        Version of the manifest file format.

    """

    source: dict[str, Any]
    processor: dict[str, Any]
    artifacts: list[str] = field(default_factory=list)
    result_kind: str = "path"
    status: str = "complete"
    error: Optional[str] = None
    format_version: int = PROCESSING_FORMAT_VERSION

    def to_dict(self) -> dict[str, Any]:
        "Serialize the manifest to a JSON-friendly dictionary."
        return {
            "format_version": self.format_version,
            "status": self.status,
            "error": self.error,
            "source": self.source,
            "processor": self.processor,
            "result_kind": self.result_kind,
            "artifacts": list(self.artifacts),
        }

    def write(self, path: PathType) -> None:
        """
        Write the manifest to *path* atomically.

        The manifest is first written to a temporary file in the same
        directory and then moved into place so that a crash can never leave a
        half-written manifest behind.
        """
        directory = os.path.dirname(os.path.abspath(os.fspath(path)))
        os.makedirs(directory, exist_ok=True)
        descriptor, tmp_name = tempfile.mkstemp(
            prefix=".pooch-processing-", dir=directory
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(self.to_dict(), handle, indent=2, sort_keys=True)
            os.replace(tmp_name, os.fspath(path))
        except BaseException:
            if os.path.exists(tmp_name):
                os.remove(tmp_name)
            raise

    @classmethod
    def read(cls, path: PathType) -> Optional["ProcessingManifest"]:
        """
        Read a manifest from *path*.

        Return ``None`` if the file doesn't exist or if it can't be parsed.
        A corrupted manifest is never used as a cache hit.
        """
        try:
            with open(os.fspath(path), encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return None
        except (json.JSONDecodeError, OSError):
            return None
        if not isinstance(data, dict):
            return None
        return cls(
            source=data.get("source", {}),
            processor=data.get("processor", {}),
            artifacts=list(data.get("artifacts", [])),
            result_kind=data.get("result_kind", "path"),
            status=data.get("status", "complete"),
            error=data.get("error"),
            format_version=data.get("format_version", PROCESSING_FORMAT_VERSION),
        )

    def is_complete_for(self, source: dict[str, Any], identity: dict[str, Any]) -> bool:
        """
        Check whether the manifest represents a usable cache hit.

        The manifest is usable only if the processing completed, the manifest
        format is still current, the source file is still the exact same
        verified file, the processor identity (type and parameters) is
        unchanged and every recorded artifact still exists on disk.
        """
        return (
            self.format_version == PROCESSING_FORMAT_VERSION
            and self.status == "complete"
            and self.error is None
            and self.processor == identity
            and self.source.get("file_name") == source.get("file_name")
            and self.source.get("hash_algorithm") == source.get("hash_algorithm")
            and self.source.get("hash") == source.get("hash")
            and all(os.path.exists(artifact) for artifact in self.artifacts)
        )

    def build_result(self) -> Any:
        "Rebuild the value originally returned by the processor."
        if self.result_kind == "list":
            return list(self.artifacts)
        if self.artifacts:
            return self.artifacts[0]
        return None


def source_fingerprint(fname: PathType, known_hash: Optional[str]) -> dict[str, Any]:
    """
    Compute the fingerprint of the verified source file *fname*.

    When *known_hash* is provided, the file has already been verified against
    it (either during the current download or before returning a cached
    file), so its algorithm and hash are reused instead of hashing the file a
    second time. When it is ``None``, the SHA256 hash is computed.
    """
    algorithm = hash_algorithm(known_hash)
    if known_hash is None:
        source_hash = file_hash(str(fname), alg=algorithm)
    else:
        source_hash = known_hash.split(":", 1)[-1].lower()
    stat = os.stat(os.fspath(fname))
    return {
        "file_name": os.path.basename(os.fspath(fname)),
        "hash_algorithm": algorithm,
        "hash": source_hash,
        "size": stat.st_size,
    }


def manifest_path(fname: PathType, identity: dict[str, Any]) -> str:
    """
    Path of the manifest for *fname* and the given processor *identity*.

    Two calls with the same source file and processor identity share one
    manifest (and can reuse each other's outputs), while different processor
    parameters get different manifests and therefore never share derived
    files.
    """
    payload = json.dumps(identity, sort_keys=True, default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"{os.fspath(fname)}{MANIFEST_PREFIX}{digest}{MANIFEST_SUFFIX}"


def processor_identity(processor: Processor) -> Optional[dict[str, Any]]:
    """
    Get the identity of a processor if it supports processing manifests.

    Processors opt in by providing a ``processing_identity`` method that
    returns a dictionary with at least a ``"type"`` key. Plain processor
    functions return ``None`` and keep working exactly as before.
    """
    if not isinstance(processor, ManifestProcessor):
        return None
    identity = processor.processing_identity()
    if not isinstance(identity, dict) or "type" not in identity:
        return None
    return identity


def apply_processor(
    processor: Processor,
    fname: str,
    action: Action,
    pooch: Any,
    known_hash: Optional[str],
) -> Any:
    """
    Run *processor* on the source file *fname* with manifest-based caching.

    If the processor doesn't support manifests, it is called directly without
    any change to its behaviour. Otherwise, a complete manifest matching the
    current source and processor identity short-circuits the processing and
    the recorded artifacts are returned instead. On failure, the partial
    outputs are discarded and a failed manifest is written so that the next
    call processes the source again from scratch.
    """
    identity = processor_identity(processor)
    if identity is None:
        return processor(fname, action, pooch)
    manifest_processor = cast(ManifestProcessor, processor)

    path = manifest_path(fname, identity)
    source = source_fingerprint(fname, known_hash)
    existing = ProcessingManifest.read(path)
    if existing is not None and existing.is_complete_for(source, identity):
        return existing.build_result()

    try:
        result = processor(fname, action, pooch)
    except Exception as error:
        _discard_outputs(manifest_processor, fname)
        ProcessingManifest(
            source=source,
            processor=identity,
            artifacts=[],
            result_kind="unknown",
            status="failed",
            error=f"{type(error).__name__}: {error}",
        ).write(path)
        raise

    artifacts = [
        os.path.abspath(os.fspath(artifact))
        for artifact in manifest_processor.processing_artifacts(result)
        if os.path.abspath(os.fspath(artifact)) != os.path.abspath(fname)
    ]
    result_kind = "list" if isinstance(result, (list, tuple)) else "path"
    ProcessingManifest(
        source=source,
        processor=identity,
        artifacts=artifacts,
        result_kind=result_kind,
    ).write(path)
    return result


def _discard_outputs(processor: ManifestProcessor, fname: str) -> None:
    "Remove partial outputs left behind by a failed processing attempt."
    try:
        processor.processing_discard(fname)
    except OSError as error:
        get_logger().info(
            "Could not remove partial processed files for '%s': %s",
            os.path.basename(fname),
            error,
        )
