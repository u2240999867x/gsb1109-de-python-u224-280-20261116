from __future__ import annotations

import threading
from collections import namedtuple
from collections.abc import Iterable, Iterator, Mapping, Sequence
from functools import total_ordering
from reprlib import recursive_repr
from typing import Any, TypeVar, overload

KT = TypeVar("KT")
VT_co = TypeVar("VT_co", covariant=True)

thread_locals = threading.local()


class CBORError(Exception):
    """Base class for errors that occur during CBOR encoding or decoding."""


class CBOREncodeError(CBORError):
    """Raised for exceptions occurring during CBOR encoding."""


class CBOREncodeTypeError(CBOREncodeError, TypeError):
    """Raised when attempting to encode a type that cannot be serialized."""


class CBOREncodeValueError(CBOREncodeError, ValueError):
    """Raised when the CBOR encoder encounters an invalid value."""


class CBORDecodeError(CBORError):
    """Raised for exceptions occurring during CBOR decoding."""


class CBORDecodeValueError(CBORDecodeError, ValueError):
    """Raised when the CBOR stream being decoded contains an invalid value."""


class CBORDecodeEOF(CBORDecodeError, EOFError):
    """Raised when decoding unexpectedly reaches EOF."""


@total_ordering
class CBORTag:
    """
    Represents a CBOR semantic tag.

    :param int tag: tag number
    :param value: encapsulated value (any object)
    """

    __slots__ = "tag", "value"

    def __init__(self, tag: str | int, value: Any) -> None:
        if not isinstance(tag, int) or tag not in range(2**64):
            raise TypeError("CBORTag tags must be positive integers less than 2**64")
        self.tag = tag
        self.value = value

    def __eq__(self, other: object) -> bool:
        if isinstance(other, CBORTag):
            return (self.tag, self.value) == (other.tag, other.value)

        return NotImplemented

    def __le__(self, other: object) -> bool:
        if isinstance(other, CBORTag):
            return (self.tag, self.value) <= (other.tag, other.value)

        return NotImplemented

    @recursive_repr()
    def __repr__(self) -> str:
        return f"CBORTag({self.tag}, {self.value!r})"

    def __hash__(self) -> int:
        self_id = id(self)
        try:
            running_hashes = thread_locals.running_hashes
        except AttributeError:
            running_hashes = thread_locals.running_hashes = set()

        if self_id in running_hashes:
            raise RuntimeError(
                "This CBORTag is not hashable because it contains a reference to itself"
            )

        running_hashes.add(self_id)
        try:
            return hash((self.tag, self.value))
        finally:
            running_hashes.remove(self_id)
            if not running_hashes:
                del thread_locals.running_hashes


class CBORSimpleValue(namedtuple("CBORSimpleValue", ["value"])):
    """
    Represents a CBOR "simple value".

    :param int value: the value (0-255)
    """

    __slots__ = ()

    value: int

    def __hash__(self) -> int:
        return hash(self.value)

    def __new__(cls, value: int) -> CBORSimpleValue:
        if value < 0 or value > 255 or 23 < value < 32:
            raise TypeError("simple value out of range (0..23, 32..255)")

        return super().__new__(cls, value)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, int):
            return self.value == other
        elif isinstance(other, CBORSimpleValue):
            return self.value == other.value

        return NotImplemented

    def __ne__(self, other: object) -> bool:
        if isinstance(other, int):
            return self.value != other
        elif isinstance(other, CBORSimpleValue):
            return self.value != other.value

        return NotImplemented

    def __lt__(self, other: object) -> bool:
        if isinstance(other, int):
            return self.value < other
        elif isinstance(other, CBORSimpleValue):
            return self.value < other.value

        return NotImplemented

    def __le__(self, other: object) -> bool:
        if isinstance(other, int):
            return self.value <= other
        elif isinstance(other, CBORSimpleValue):
            return self.value <= other.value

        return NotImplemented

    def __ge__(self, other: object) -> bool:
        if isinstance(other, int):
            return self.value >= other
        elif isinstance(other, CBORSimpleValue):
            return self.value >= other.value

        return NotImplemented

    def __gt__(self, other: object) -> bool:
        if isinstance(other, int):
            return self.value > other
        elif isinstance(other, CBORSimpleValue):
            return self.value > other.value

        return NotImplemented


class CBORMap(Sequence[tuple[KT, VT_co]]):
    """
    An ordered, lossless representation of a CBOR map.

    Unlike :class:`dict`, this type keeps every member in the order it appeared on
    the wire. Keys and values are stored as decoded objects, so key types (including
    integers, booleans and complex values) are preserved. Duplicate keys are retained
    as separate members.

    The members can be iterated as ``(key, value)`` pairs, or accessed with
    :meth:`keys`, :meth:`values`, :meth:`items` and :meth:`get`.

    :param pairs: an iterable of ``(key, value)`` pairs
    """

    __slots__ = ("_pairs",)

    def __init__(self, pairs: Iterable[tuple[KT, VT_co]] = ()) -> None:
        self._pairs: list[tuple[KT, VT_co]] = list(pairs)

    @overload
    def __getitem__(self, index: int) -> tuple[KT, VT_co]: ...

    @overload
    def __getitem__(self, index: slice) -> list[tuple[KT, VT_co]]: ...

    def __getitem__(self, index: int | slice) -> tuple[KT, VT_co] | list[tuple[KT, VT_co]]:
        return self._pairs[index]

    def __len__(self) -> int:
        return len(self._pairs)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, CBORMap):
            return self._pairs == other._pairs

        return NotImplemented

    def __hash__(self) -> int:
        try:
            return hash(tuple(self._pairs))
        except TypeError:
            raise TypeError(f"unhashable {self.__class__.__name__}") from None

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self._pairs!r})"

    def _append(self, pair: tuple[KT, VT_co]) -> None:
        """Append a member to the underlying ordered storage (internal use)."""
        self._pairs.append(pair)

    @property
    def pairs(self) -> list[tuple[KT, VT_co]]:
        """The members of the map as a list of ``(key, value)`` pairs, in order."""
        return list(self._pairs)

    def keys(self) -> list[KT]:
        return [key for key, _ in self._pairs]

    def values(self) -> list[VT_co]:
        return [value for _, value in self._pairs]

    def items(self) -> list[tuple[KT, VT_co]]:
        return list(self._pairs)

    def get(self, key: KT, default: Any = None) -> Any:
        for member_key, value in self._pairs:
            if member_key is key or member_key == key:
                return value

        return default

    def as_dict(self, *, key_to_str: bool = False) -> dict[Any, Any]:
        """
        Convert the map to a regular :class:`dict`.

        By default the keys and values are copied unchanged, and the last occurrence
        of a duplicated key wins. This is intentionally lossy; use the map itself
        when duplicates or key types matter.

        :param key_to_str:
            if ``True``, convert keys to strings using
            :func:`cbor2.cbor_key_to_str`; any resulting collisions raise
            :exc:`CBORMapKeyCollisionError`
        :raises CBORMapKeyCollisionError:
            if ``key_to_str`` is ``True`` and two distinct original keys would map to
            the same string

        """
        if key_to_str:
            converted = cbor_map_to_str_keys(self)
            assert isinstance(converted, dict)
            return converted

        result: dict[Any, Any] = {}
        for key, value in self._pairs:
            result[key] = value

        return result


class CBORMapKeyCollisionError(CBORError, ValueError):
    """
    Raised when converting CBOR map keys to strings produces a collision.

    :ivar collisions:
        a list of ``(string key, [original keys])`` tuples, in first occurrence order
    """

    def __init__(self, collisions: list[tuple[str, list[Any]]]) -> None:
        self.collisions = collisions
        details = ", ".join(
            f"{string_key!r} <- {original_keys!r}" for string_key, original_keys in collisions
        )
        super().__init__(f"map key collision after string conversion: {details}")


def _check_key_collisions(converted: Iterable[tuple[str, Any]]) -> None:
    grouped: dict[str, list[Any]] = {}
    for string_key, original_key in converted:
        grouped.setdefault(string_key, []).append(original_key)

    collisions = [
        (string_key, original_keys)
        for string_key, original_keys in grouped.items()
        if len(original_keys) > 1
    ]
    if collisions:
        raise CBORMapKeyCollisionError(collisions)


def cbor_key_to_str(key: Any) -> str:
    """
    Convert a decoded CBOR map key to a string without silently losing its type.

    Text strings are returned unchanged, byte strings are decoded as UTF-8 with
    backslash escapes, simple values are rendered as ``cbor_simple:N``, and other
    keys are rendered using :func:`repr` so that distinct key types remain
    distinguishable.

    :param key: the decoded CBOR key
    :return: the string representation of the key

    """
    if isinstance(key, str):
        return key
    elif isinstance(key, bytes):
        return key.decode("utf-8", errors="backslashreplace")
    elif isinstance(key, bytearray):
        return bytes(key).decode("utf-8", errors="backslashreplace")
    elif isinstance(key, CBORSimpleValue):
        return f"cbor_simple:{key.value:d}"

    return repr(key)


def cbor_map_to_str_keys(value: Any, preserve_maps: bool = False) -> Any:
    """
    Recursively convert the keys of CBOR maps (and their nested containers) to strings.

    When two distinct original keys in the same map convert to the same string, a
    :exc:`CBORMapKeyCollisionError` is raised listing both the string key and every
    original key that collided. No value is dropped in favour of a "last one wins"
    result.

    Cyclic structures are supported and preserve their internal references.

    :param value: a decoded CBOR value
    :param preserve_maps:
        if ``True``, :class:`CBORMap` instances are converted to new :class:`CBORMap`
        instances instead of dictionaries, preserving member order and duplicates
    :return: the value with map keys converted to strings
    :raises CBORMapKeyCollisionError:
        if distinct original keys in the same map convert to the same string

    """
    return _cbor_map_to_str_keys(value, preserve_maps, {})


def _cbor_map_to_str_keys_with_alias(value: Any, alias: Any, preserve_maps: bool) -> Any:
    """
    Convert keys like :func:`cbor_map_to_str_keys`, redirecting references to
    ``alias`` to the resulting object so cyclic maps stay cyclic.
    """
    target: Any = CBORMap() if preserve_maps else {}
    memo = {id(alias): target}
    converted = _cbor_map_to_str_keys(value, preserve_maps, memo)
    if isinstance(target, CBORMap):
        target._pairs.extend(converted._pairs)
    else:
        target.update(converted)

    return target


def _cbor_map_to_str_keys(value: Any, preserve_maps: bool, memo: dict[int, Any]) -> Any:
    if isinstance(value, CBORMap):
        cached = memo.get(id(value))
        if cached is not None:
            return cached

        converted = [(cbor_key_to_str(key), key) for key, _ in value]
        _check_key_collisions(converted)
        if preserve_maps:
            result: Any = CBORMap()
            memo[id(value)] = result
            for (string_key, _), (_, member_value) in zip(converted, value):
                result._append(
                    (string_key, _cbor_map_to_str_keys(member_value, preserve_maps, memo))
                )
            return result

        result = {}
        memo[id(value)] = result
        for (string_key, _), (_, member_value) in zip(converted, value):
            result[string_key] = _cbor_map_to_str_keys(member_value, preserve_maps, memo)
        return result
    elif isinstance(value, Mapping):
        cached = memo.get(id(value))
        if cached is not None:
            return cached

        converted = [(cbor_key_to_str(key), key) for key in value]
        _check_key_collisions(converted)
        result = {}
        memo[id(value)] = result
        for string_key, original_key in converted:
            result[string_key] = _cbor_map_to_str_keys(value[original_key], preserve_maps, memo)
        return result
    elif isinstance(value, list):
        cached = memo.get(id(value))
        if cached is not None:
            return cached

        result = []
        memo[id(value)] = result
        result.extend(_cbor_map_to_str_keys(item, preserve_maps, memo) for item in value)
        return result
    elif isinstance(value, tuple):
        return tuple(_cbor_map_to_str_keys(item, preserve_maps, memo) for item in value)
    elif isinstance(value, (set, frozenset)):
        return type(value)(_cbor_map_to_str_keys(item, preserve_maps, memo) for item in value)

    return value


class FrozenDict(Mapping[KT, VT_co]):
    """
    A hashable, immutable mapping type.

    The arguments to ``FrozenDict`` are processed just like those to ``dict``.
    """

    def __init__(self, *args: Mapping[KT, VT_co] | Iterable[tuple[KT, VT_co]]) -> None:
        self._d: dict[KT, VT_co] = dict(*args)
        self._hash: int | None = None

    def __iter__(self) -> Iterator[KT]:
        return iter(self._d)

    def __len__(self) -> int:
        return len(self._d)

    def __getitem__(self, key: KT) -> VT_co:
        return self._d[key]

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self._d})"

    def __hash__(self) -> int:
        if self._hash is None:
            self._hash = hash((frozenset(self), frozenset(self.values())))

        return self._hash


class UndefinedType:
    __slots__ = ()

    def __new__(cls: type[UndefinedType]) -> UndefinedType:
        try:
            return undefined
        except NameError:
            return super().__new__(cls)

    def __repr__(self) -> str:
        return "undefined"

    def __bool__(self) -> bool:
        return False


class BreakMarkerType:
    __slots__ = ()

    def __new__(cls: type[BreakMarkerType]) -> BreakMarkerType:
        try:
            return break_marker
        except NameError:
            return super().__new__(cls)

    def __repr__(self) -> str:
        return "break_marker"

    def __bool__(self) -> bool:
        return True


#: Represents the "undefined" value.
undefined = UndefinedType()
break_marker = BreakMarkerType()
