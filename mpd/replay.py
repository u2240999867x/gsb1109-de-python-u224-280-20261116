"""Replay recorded MPD responses against several server versions.

The harness exists for checking one condition end to end without a live
server.  A fixture directory may contain:

* ``*.json`` files holding a list of song dictionaries (or ``{"songs": [...]}``)
* ``*.jsonl`` files with one song dictionary per line
* ``*.mpd`` / ``*.txt`` files with MPD protocol blocks (``key: value`` lines,
  blank line between records)

Records are merged by ``file``.  Repeated tag keys become multiple values, so
multi-valued tag behaviour can be replayed accurately.

``ReplayClient`` implements the subset of the MPD command language the planner
uses: ``find`` and ``search`` (both legacy pairs and filter expressions) and
``listallinfo``.  ``replay`` runs the same condition against every requested
version and reports whether the final confirmed records agree.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from mpd.filters import (
    Filter,
    FilterResult,
    as_filter,
    normalize_filter,
    parse_filter,
    select_songs,
)

DEFAULT_VERSIONS = ("0.20.0", "0.21.0")

PathLike = Union[str, "Path"]


def load_songs(
    source: Union[PathLike, Iterable[Mapping[str, Any]]]
) -> List[Dict[str, Any]]:
    """Load and merge records from a fixture directory or an iterable."""

    if isinstance(source, (str, Path)):
        return _load_directory(Path(source))
    return _merge_records(source)


def _load_directory(directory: Path) -> List[Dict[str, Any]]:
    if not directory.exists():
        raise FileNotFoundError(str(directory))
    records: List[Mapping[str, Any]] = []
    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix == ".json":
            records.extend(_load_json(path))
        elif suffix in (".jsonl", ".ndjson"):
            records.extend(_load_jsonl(path))
        elif suffix in (".mpd", ".txt"):
            records.extend(_load_protocol(path))
    if not records:
        raise ValueError("no song records found in {}".format(directory))
    return _merge_records(records)


def _load_json(path: Path) -> List[Mapping[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, Mapping) and "songs" in data:
        data = data["songs"]
    if not isinstance(data, list):
        raise ValueError("{} does not contain a song list".format(path))
    return data


def _load_jsonl(path: Path) -> List[Mapping[str, Any]]:
    records: List[Mapping[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as error:
            raise ValueError("{}:{}: {}".format(path, number, error))
    return records


def _load_protocol(path: Path) -> List[Mapping[str, Any]]:
    records: List[Mapping[str, Any]] = []
    current: Dict[str, Any] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            if current:
                records.append(current)
                current = {}
            continue
        if line in ("OK", "list_OK") or line.startswith("ACK "):
            continue
        if ": " not in line:
            raise ValueError("{}: expected 'key: value', got {!r}".format(path, line))
        key, value = line.split(": ", 1)
        key = key.lower()
        if key in current:
            existing = current[key]
            if not isinstance(existing, list):
                existing = [existing]
            existing.append(value)
            current[key] = existing
        else:
            current[key] = value
    if current:
        records.append(current)
    return records


def _merge_records(records: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for record in records:
        if not isinstance(record, Mapping):
            raise TypeError("song record must be a mapping: {!r}".format(record))
        if "file" not in record:
            continue
        key = str(record["file"])
        if key not in merged:
            merged[key] = {}
            order.append(key)
        target = merged[key]
        for name, value in record.items():
            name = str(name).lower()
            values = _values(value)
            if name not in target:
                target[name] = values[0] if len(values) == 1 else list(values)
                continue
            existing = target[name]
            if not isinstance(existing, list):
                existing = [existing]
                target[name] = existing
            for item in values:
                if item not in existing:
                    existing.append(item)
    return [merged[key] for key in order]


def _values(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set, frozenset)):
        return [str(item) for item in value if item is not None]
    return [str(value)]


@dataclass
class ReplayClient:
    """An in-memory MPD server with a selectable protocol version."""

    songs: Sequence[Mapping[str, Any]]
    version: str
    calls: List[str] = field(default_factory=list)

    @property
    def mpd_version(self) -> str:
        return self.version

    def find(self, *args: Any) -> List[Mapping[str, Any]]:
        self.calls.append("find " + _render_args(args))
        if _is_expression(args):
            return self._expression(args[0], ignore_case=False)
        return self._legacy(args, exact=True)

    def search(self, *args: Any) -> List[Mapping[str, Any]]:
        self.calls.append("search " + _render_args(args))
        if _is_expression(args):
            return self._expression(args[0], ignore_case=True)
        return self._legacy(args, exact=False)

    def listallinfo(self, *args: Any) -> List[Mapping[str, Any]]:
        self.calls.append("listallinfo")
        return list(self.songs)

    def _expression(
        self, expression: str, ignore_case: bool
    ) -> List[Mapping[str, Any]]:
        condition = parse_filter(expression, ignore_case=ignore_case)
        return [song for song in self.songs if condition.evaluate(song) is True]

    def _legacy(self, args: Sequence[Any], exact: bool) -> List[Mapping[str, Any]]:
        pairs = list(zip(args[0::2], args[1::2]))
        return [song for song in self.songs if _pairs_match(song, pairs, exact)]


def _render_args(args: Sequence[Any]) -> str:
    return " ".join(repr(arg) for arg in args)


def _is_expression(args: Sequence[Any]) -> bool:
    return len(args) == 1 and str(args[0]).lstrip().startswith("(")


def _pairs_match(
    song: Mapping[str, Any], pairs: Sequence[Tuple[Any, Any]], exact: bool
) -> bool:
    for tag, needle in pairs:
        values = _tag_values(song, str(tag))
        if values is None:
            return False
        if exact:
            if not any(value == needle for value in values):
                return False
        else:
            if not any(str(needle).lower() in value.lower() for value in values):
                return False
    return True


def _tag_values(song: Mapping[str, Any], tag: str) -> Optional[List[str]]:
    key = tag.lower()
    if key == "any":
        values: List[str] = []
        for name, value in song.items():
            if name in ("file", "directory", "playlist"):
                continue
            values.extend(_values(value))
        return values
    if key not in song:
        return None
    return _values(song[key])


@dataclass
class ReplayReport:
    """Comparison of one condition across several MPD versions."""

    condition: Filter
    results: Dict[str, FilterResult]

    @property
    def versions(self) -> List[str]:
        return list(self.results)

    def confirmed(self, version: str) -> List[str]:
        return sorted(self.results[version].confirmed_files)

    @property
    def equivalent(self) -> bool:
        confirmed = [self.confirmed(version) for version in self.results]
        return all(records == confirmed[0] for records in confirmed[1:])

    @property
    def differences(self) -> Dict[str, Dict[str, List[str]]]:
        if not self.results:
            return {}
        reference_version = self.versions[0]
        reference = set(self.confirmed(reference_version))
        differences: Dict[str, Dict[str, List[str]]] = {}
        for version in self.versions[1:]:
            current = set(self.confirmed(version))
            if current != reference:
                differences[version] = {
                    "missing": sorted(reference - current),
                    "extra": sorted(current - reference),
                }
        return differences

    @property
    def unsupported(self) -> Dict[str, List[str]]:
        return {
            version: list(result.unsupported)
            for version, result in self.results.items()
            if not result.supported
        }

    @property
    def undetermined(self) -> Dict[str, List[str]]:
        return {
            version: sorted(result.undetermined_files)
            for version, result in self.results.items()
            if result.undetermined_files
        }

    def as_dict(self) -> Dict[str, Any]:
        return {
            "condition": self.condition.to_mpd(),
            "equivalent": self.equivalent,
            "versions": {
                version: result.as_dict()
                for version, result in self.results.items()
            },
            "differences": self.differences,
            "unsupported": self.unsupported,
            "undetermined": self.undetermined,
        }


def replay(
    condition: Union[Filter, str, Mapping[str, Any], Sequence[Any]],
    source: Union[
        PathLike,
        Iterable[Mapping[str, Any]],
        Mapping[str, Union[PathLike, Iterable[Mapping[str, Any]]]],
    ],
    versions: Sequence[str] = DEFAULT_VERSIONS,
    *,
    require_server_only: bool = False,
) -> ReplayReport:
    """Run *condition* against fixtures for each server *version*.

    *source* is a fixture directory, an iterable of records, or a mapping of
    version to fixture source when each server should replay its own directory.
    The confirmed records should agree across versions.  A version whose
    server language cannot express the condition still participates: its plan
    uses a candidate superset plus local verification, and
    :attr:`ReplayReport.unsupported` records the remaining guarantees.
    """

    parsed = normalize_filter(as_filter(condition))
    per_version = _per_version_sources(source)
    if per_version is not None and versions == DEFAULT_VERSIONS:
        versions = sorted(per_version)
    shared = None
    if per_version is None:
        shared = load_songs(source)  # type: ignore[arg-type]
    results: Dict[str, FilterResult] = {}
    for version in versions:
        if per_version is not None:
            if version not in per_version:
                raise KeyError("no fixture source for MPD {}".format(version))
            songs = load_songs(per_version[version])
        else:
            songs = shared
        client = ReplayClient(songs or [], version)
        results[version] = select_songs(
            client, parsed, require_server_only=require_server_only
        )
    return ReplayReport(condition=parsed, results=results)


def _per_version_sources(
    source: Any,
) -> Optional[Dict[str, Any]]:
    if not isinstance(source, Mapping):
        return None
    # A song record mapping is distinguishable from a version mapping by its
    # keys: version maps use MPD-style version strings.
    if all(isinstance(key, str) and key[:1].isdigit() for key in source):
        return dict(source)
    return None


# Convenience aliases.
replay_directory = replay
replay_fixtures = replay
run_replay = replay
load_records = load_songs
