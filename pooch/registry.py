# Copyright (c) 2018 The Pooch Developers.
# Distributed under the terms of the BSD 3-Clause License.
# SPDX-License-Identifier: BSD-3-Clause
#
# This code is part of the Fatiando a Terra project (https://www.fatiando.org)
#
"""
Immutable registry snapshots and registry file parsing.

The registry is treated as a versioned document.  A
:class:`~pooch.registry.RegistrySnapshot` is a complete, self-identifying copy
of that document.  Fetches made through :class:`pooch.Pooch` resolve their
file against a single snapshot, so a registry refresh can never change the
meaning of a fetch that is already in progress.
"""

import hashlib
import json
import shlex
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

RETRACTION_KEYWORDS = frozenset(
    {
        "delete",
        "deleted",
        "drop",
        "dropped",
        "remove",
        "removed",
        "retract",
        "retracted",
        "revoke",
        "revoked",
        "tombstone",
        "withdraw",
        "withdrawn",
    }
)


class RegistryError(ValueError):
    """
    Base class for errors about a file's status in a registry.
    """


class FileNotInRegistryError(RegistryError):
    """
    Raised when a file is not declared by the registry version in use.

    This means only that the registry does not declare the file.  It is
    deliberately different from :class:`RetractedFileError`, which means that
    the registry explicitly declares the file as withdrawn.
    """


class RetractedFileError(RegistryError):
    """
    Raised when a file has been explicitly retracted from the registry.

    This is deliberately different from a plain
    :class:`FileNotInRegistryError` raised for a file that is merely absent
    from the registry.  Absence means that the registry does not declare the
    file.  A retraction means that the registry declares that the file must
    not be used anymore.
    """


@dataclass(frozen=True)
class RegistryDirective:
    """
    A single directive read from a registry file.

    Parameters
    ----------
    kind : str
        Either ``"entry"`` or ``"retraction"``.
    name : str
        The name of the file.
    checksum : str or None
        The checksum for entries.  ``None`` for retractions.
    url : str or None
        The custom URL for entries, if one was provided.
    reason : str
        The reason attached to a retraction, if one was provided.
    """

    kind: str
    name: str
    checksum: Optional[str] = None
    url: Optional[str] = None
    reason: str = ""


@dataclass(frozen=True)
class RegistrySnapshot:
    """
    A complete, immutable view of a registry.

    Parameters
    ----------
    version : str
        Human-readable version identifier.  This is either a label supplied
        by the caller or the content digest.
    digest : str
        Content digest that identifies the snapshot independently of its
        source path, comments, or line ordering.
    registry : dict
        Mapping of file names to checksums.
    urls : dict
        Mapping of file names to custom download URLs.
    retracted : frozenset
        Names that were explicitly retracted.
    retraction_reasons : dict
        Mapping of retracted names to the reason given in the registry file.
    base_url : str
        Base URL captured with the snapshot.
    source : str or None
        Where the snapshot was loaded from, when known.
    label : str or None
        The caller-supplied version label, when one was given.
    """

    version: str
    digest: str
    registry: dict
    urls: dict
    retracted: frozenset
    retraction_reasons: dict
    base_url: str
    source: Optional[str] = None
    label: Optional[str] = None

    @property
    def files(self):
        "List of file names declared by this snapshot."
        return list(self.registry)

    @property
    def retracted_files(self):
        "List of file names explicitly retracted by this snapshot."
        return sorted(self.retracted)

    @classmethod
    def from_state(
        cls,
        registry,
        urls,
        retracted,
        retraction_reasons,
        base_url,
        version=None,
        source=None,
    ):
        """Build an immutable snapshot from a registry state."""
        declared = {
            str(name): str(checksum).lower() for name, checksum in registry.items()
        }
        declared_urls = {
            str(name): str(url) for name, url in urls.items() if str(name) in declared
        }
        declared_retracted = {
            str(name): str(reason) for name, reason in retracted.items()
        }
        digest = registry_digest(
            declared,
            declared_urls,
            declared_retracted,
            retraction_reasons,
            base_url=base_url,
        )
        label = None if version is None else str(version)
        return cls(
            version=label if label is not None else digest,
            digest=digest,
            registry=declared,
            urls=declared_urls,
            retracted=frozenset(declared_retracted),
            retraction_reasons=dict(retraction_reasons),
            base_url=base_url,
            source=source,
            label=label,
        )

    def as_dict(self):
        "Return a JSON-serializable representation of the snapshot."
        return {
            "version": self.version,
            "digest": self.digest,
            "registry": dict(self.registry),
            "urls": dict(self.urls),
            "retracted": sorted(self.retracted),
            "retraction_reasons": dict(self.retraction_reasons),
            "base_url": self.base_url,
            "source": self.source,
            "label": self.label,
        }

    @classmethod
    def from_dict(cls, data):
        "Rebuild a snapshot from :meth:`as_dict` output."
        return cls(
            version=data["version"],
            digest=data["digest"],
            registry=dict(data.get("registry", {})),
            urls=dict(data.get("urls", {})),
            retracted=frozenset(data.get("retracted", [])),
            retraction_reasons=dict(data.get("retraction_reasons", {})),
            base_url=data.get("base_url", ""),
            source=data.get("source"),
            label=data.get("label"),
        )

    def is_retracted(self, fname):
        "Return True if *fname* was explicitly retracted in this snapshot."
        return fname in self.retracted

    def reason(self, fname):
        "Return the reason for a retraction, or an empty string."
        return self.retraction_reasons.get(fname, "")

    def get_url(self, fname):
        "Return the URL that this snapshot uses to download *fname*."
        if fname in self.retracted:
            message = (
                f"File '{fname}' was explicitly retracted from registry "
                f"version '{self.version}'."
            )
            reason = self.retraction_reasons.get(fname, "")
            if reason:
                message += f" Reason: {reason}"
            raise RetractedFileError(message)
        if fname not in self.registry:
            message = (
                f"File '{fname}' is not in the registry (version '{self.version}')."
            )
            raise FileNotInRegistryError(message)
        return self.urls.get(fname, "".join([self.base_url, fname]))


def registry_digest(registry, urls, retracted, retraction_reasons, base_url=""):
    """
    Calculate the content digest of a registry state.

    The digest is independent of the order of lines in a registry file and of
    comments, but it changes when a checksum, URL, base URL, retraction, or
    retraction reason changes.  It can therefore be used to give old content a
    stable, traceable identity without pretending that it is a newer registry.
    """
    declared = {str(name): str(checksum).lower() for name, checksum in registry.items()}
    payload = {
        "schema": "pooch-registry-v1",
        "base_url": str(base_url),
        "files": sorted([name, checksum] for name, checksum in declared.items()),
        "urls": sorted(
            [str(name), str(url)] for name, url in urls.items() if str(name) in declared
        ),
        "retracted": sorted(
            [str(name), str(retraction_reasons.get(name, ""))] for name in retracted
        ),
    }
    serialized = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    checksum = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return f"sha256:{checksum}"


def _retraction_from_tokens(tokens):
    """
    Detect whether tokens represent a retraction and return (name, reason).

    Several explicit forms are accepted so that registry files can be written
    in a way that is natural for the project that owns them:

    * ``!name`` or ``! name``
    * ``- name``
    * ``name retracted`` (and the other retraction keywords)
    * ``retracted name`` (and the other retraction keywords)
    """
    if not tokens:
        return None
    first = tokens[0]
    if first.startswith("!") and len(first) > 1:
        return first[1:], " ".join(tokens[1:])
    keyword = first.rstrip(":").lower()
    if first in {"!", "-"}:
        if len(tokens) < 2:
            return "", ""
        return tokens[1], " ".join(tokens[2:])
    if keyword in RETRACTION_KEYWORDS:
        if len(tokens) < 2:
            return "", ""
        return tokens[1], " ".join(tokens[2:])
    if len(tokens) >= 2:
        second = tokens[1]
        if second in {"!", "-"}:
            return tokens[0], " ".join(tokens[2:])
        if second.rstrip(":").lower() in RETRACTION_KEYWORDS:
            return tokens[0], " ".join(tokens[2:])
    # Also accept a trailing marker, as in ``name <hash> retracted reason``.
    for position, token in enumerate(tokens[1:], start=1):
        if token.rstrip(":").lower() in RETRACTION_KEYWORDS or token == "!":
            reason = " ".join(tokens[position + 1 :])
            return tokens[0], reason
    return None


def parse_registry(lines: Iterable[str], source: str) -> list:
    """
    Parse registry lines into a list of directives.

    Parsing is complete before any caller-visible state is changed, which is
    what makes registry refreshes atomic.  A file that is omitted is never
    turned into a retraction: retractions must be written explicitly.

    Parameters
    ----------
    lines : iterable of str or bytes
        Lines of a registry file.
    source : str
        Name of the source, used in error messages.

    Returns
    -------
    directives : list of RegistryDirective
        The parsed directives, in file order.
    """
    directives = []
    for linenum, line in enumerate(lines):
        if isinstance(line, bytes):
            line = line.decode("utf-8")  # noqa: PLW2901
        line = line.strip()  # noqa: PLW2901
        if not line or line.startswith("#"):
            continue

        try:
            tokens = shlex.split(line)
        except ValueError as error:
            msg = (
                f"Invalid entry in Pooch registry file '{source}': "
                f"could not parse line {linenum + 1}. Offending entry: '{line}'"
            )
            raise OSError(msg) from error
        retraction = _retraction_from_tokens(tokens)
        if retraction is not None:
            name, reason = retraction
            if not name:
                msg = (
                    f"Invalid entry in Pooch registry file '{source}': "
                    f"retraction in line {linenum + 1} is missing a file name. "
                    f"Offending entry: '{line}'"
                )
                raise OSError(msg)
            directives.append(
                RegistryDirective(kind="retraction", name=name, reason=reason)
            )
            continue

        if len(tokens) not in [0, 2, 3]:
            msg = (
                f"Invalid entry in Pooch registry file '{source}': "
                f"expected 2 or 3 elements in line {linenum + 1} but got "
                f"{len(tokens)}. Offending entry: '{line}'"
            )
            raise OSError(msg)
        if tokens:
            file_name = tokens[0]
            file_checksum = tokens[1].lower()
            file_url = tokens[2] if len(tokens) == 3 else None
            if file_checksum in RETRACTION_KEYWORDS:
                directives.append(RegistryDirective(kind="retraction", name=file_name))
                continue
            directives.append(
                RegistryDirective(
                    kind="entry",
                    name=file_name,
                    checksum=file_checksum,
                    url=file_url,
                )
            )
    return directives


def apply_directives(
    registry,
    urls,
    retracted,
    retraction_reasons,
    directives,
    replace=False,
):
    """
    Apply parsed directives to a registry state.

    When *replace* is True, the new state starts empty (apart from any URLs
    supplied by the caller).  When it is False, directives are merged into the
    existing state.  In both cases the input mappings are not modified.

    Parameters
    ----------
    registry, urls, retracted, retraction_reasons : dict
        The existing registry state.
    directives : list of RegistryDirective
        Directives to apply, in order.
    replace : bool
        If True, build a new state from the directives alone.

    Returns
    -------
    registry, urls, retracted, retraction_reasons : tuple of dict
        The complete new state.
    """
    new_registry = {} if replace else dict(registry)
    new_urls = dict(urls)
    new_retracted = {} if replace else dict(retracted)
    new_reasons = {} if replace else dict(retraction_reasons)

    for directive in directives:
        if directive.kind == "entry":
            new_registry[directive.name] = directive.checksum
            new_retracted.pop(directive.name, None)
            new_reasons.pop(directive.name, None)
            if directive.url is not None:
                new_urls[directive.name] = directive.url
        else:
            new_registry.pop(directive.name, None)
            new_urls.pop(directive.name, None)
            new_retracted[directive.name] = directive.reason
            new_reasons[directive.name] = directive.reason
    if replace:
        # A complete registry only keeps custom URLs for files it declares.
        # URLs for omitted files belong to the old version, not the new one.
        new_urls = {name: url for name, url in new_urls.items() if name in new_registry}
    return new_registry, new_urls, new_retracted, new_reasons


@dataclass(frozen=True)
class ProvenanceRecord:
    """
    A preserved copy of content that used to live at a registry file name.

    Parameters
    ----------
    fname : str
        The registry file name that the content belonged to.
    path : Path
        Path to the preserved content.
    algorithm : str
        Hash algorithm used to identify the content.
    digest : str
        Hex digest of the preserved content.
    registry_version : str
        Registry snapshot that was current when the content was superseded.
    known_hash : str
        Registry checksum that was in use when the content was superseded.
    captured_at : str
        ISO-8601 timestamp of when the content was preserved.
    status : str
        Why the declaration changed: ``"superseded"`` for a checksum change,
        ``"retracted"`` for an explicit retraction, or ``"undeclared"`` when
        the new registry simply omits the file.
    """

    fname: str
    path: Path
    algorithm: str
    digest: str
    registry_version: str
    known_hash: str
    captured_at: str
    status: str = "superseded"
