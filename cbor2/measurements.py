"""Reconcile compressed sensor measurements encoded with custom CBOR tags.

The collector emits two kinds of custom tags:

* an *absolute* record that pins one sample of one sensor generation to a
  value;
* an *affine relation* that states ``y = a * x + b`` for two samples, with
  ``a`` and ``b`` carried as exact rational numbers.

This module turns an unordered stream of such records into confirmed values,
still-undetermined relations and explicit conflict records.  Every value and
coefficient is kept as a :class:`fractions.Fraction`; nothing is ever routed
through binary floating point arithmetic.

A sample is identified by ``(sensor, generation, sample)``.  The generation is
part of the identity, so samples from different restarts are never connected.

The public entry points are :func:`parse_records` and :func:`reconcile` (with
the aliases :func:`resolve`, :func:`reconstruct`, :func:`decode_measurements`
and :func:`reconcile_cbor`).  Raw CBOR bytes, CBOR sequences, decoded lists,
file objects and mapping envelopes are all accepted.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from io import BytesIO
from typing import Any, Hashable

from ._types import CBORDecodeError, CBORTag

try:  # the C extension uses a distinct CBORTag class
    from _cbor2 import CBORTag as _C_CBORTag
except ImportError:  # pragma: no cover - depends on build configuration
    _C_CBORTag = None

_TAG_TYPES: tuple[type, ...] = (CBORTag,) if _C_CBORTag is None else (CBORTag, _C_CBORTag)


def _is_tag(value: Any) -> bool:
    return isinstance(value, _TAG_TYPES)

__all__ = [
    "TAG_ABSOLUTE",
    "TAG_ANCHOR",
    "TAG_AFFINE",
    "TAG_RELATION",
    "SampleId",
    "Sample",
    "Anchor",
    "AbsoluteAnchor",
    "AffineRelation",
    "Relation",
    "ParsedRecords",
    "Evidence",
    "Conflict",
    "InvalidRecord",
    "UndeterminedRelation",
    "AffineExpression",
    "Component",
    "ReconciliationResult",
    "MeasurementFormatError",
    "exact_fraction",
    "parse_records",
    "reconcile",
    "resolve",
    "reconstruct",
    "reconcile_measurements",
    "decode_measurements",
    "reconcile_cbor",
    "load_measurements",
    "measurement_tag_hook",
]

#: Tag numbers used by the collector when no number is supplied explicitly.
TAG_ABSOLUTE = TAG_ANCHOR = 47000
TAG_AFFINE = TAG_RELATION = 47001

_SENSOR_KEYS = ("sensor", "sensor_id", "sensorId", "device", "device_id", "node", "channel")
_GENERATION_KEYS = (
    "generation",
    "generation_id",
    "generationId",
    "gen",
    "boot",
    "boot_id",
    "bootId",
    "restart",
    "restart_id",
    "restartId",
    "session",
    "epoch",
)
_SAMPLE_KEYS = (
    "sample",
    "sample_id",
    "sampleId",
    "sample_no",
    "sample_number",
    "sampleNumber",
    "index",
    "seq",
    "n",
)
_VALUE_KEYS = ("value", "measurement", "reading", "absolute", "abs", "observed", "val")
_X_KEYS = ("x", "src", "source", "from", "left", "lhs", "sample_x", "first", "input")
_Y_KEYS = (
    "y",
    "dst",
    "dest",
    "destination",
    "target",
    "to",
    "right",
    "rhs",
    "sample_y",
    "second",
    "output",
)
_A_KEYS = ("a", "slope", "scale", "m", "multiplier", "factor", "gain")
_B_KEYS = ("b", "intercept", "offset", "c", "shift", "bias")
_RECORD_KEYS = ("records", "measurements", "items", "data", "entries")
_ANCHOR_KEYS = ("anchors", "absolutes", "absolute", "values")
_RELATION_KEYS = ("relations", "affine", "affines", "edges", "links")

_MISSING = object()


class MeasurementFormatError(ValueError):
    """Raised when a measurement record cannot be interpreted."""


class SampleId(tuple):
    """Identity of one sample.

    The value behaves like ``(sensor, generation, sample)`` and exposes
    descriptive attribute names too.  ``generation`` is part of the identity,
    which is what prevents a restart from joining unrelated series.
    """

    __slots__ = ()

    def __new__(cls, sensor: Hashable, generation: Hashable, sample: Hashable) -> SampleId:
        return tuple.__new__(cls, (sensor, generation, sample))

    sensor = property(lambda self: self[0])
    generation = property(lambda self: self[1])
    sample = property(lambda self: self[2])
    sensor_id = property(lambda self: self[0])
    generation_id = property(lambda self: self[1])
    sample_no = property(lambda self: self[2])
    sample_number = property(lambda self: self[2])
    boot = property(lambda self: self[1])
    boot_id = property(lambda self: self[1])

    def __repr__(self) -> str:
        return f"SampleId(sensor={self[0]!r}, generation={self[1]!r}, sample={self[2]!r})"


Sample = SampleId


def _freeze(value: Any) -> Hashable:
    if isinstance(value, SampleId):
        return value
    if isinstance(value, Mapping):
        return tuple(sorted((_freeze(key), _freeze(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(item) for item in value)
    if isinstance(value, bytearray):
        return bytes(value)
    try:
        hash(value)
    except TypeError:
        return repr(value)
    return value


def _sort_key(value: Any) -> tuple[str, str]:
    return (type(value).__name__, repr(value))


def _sample_sort_key(sample: SampleId) -> tuple[Any, Any, Any]:
    return (_sort_key(sample[0]), _sort_key(sample[1]), _sort_key(sample[2]))


def _fraction_text(value: Fraction) -> str:
    return f"{value.numerator}/{value.denominator}"


def exact_fraction(value: Any, *, what: str = "number") -> Fraction:
    """Convert a decoded CBOR value to an exact :class:`Fraction`.

    Integers, rationals, decimals, numeric strings and two-element
    numerator/denominator pairs are accepted.  A binary float is converted
    from its exact binary value with :meth:`Fraction.from_float`; no floating
    point arithmetic is performed on coefficients.
    """

    if isinstance(value, Fraction):
        return value
    if isinstance(value, bool) or value is None:
        raise MeasurementFormatError(f"{what} must be an exact rational number")
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise MeasurementFormatError(f"{what} must be finite")
        return Fraction(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise MeasurementFormatError(f"{what} must be finite")
        return Fraction.from_float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise MeasurementFormatError(f"{what} is empty")
        try:
            return Fraction(text)
        except (ValueError, ZeroDivisionError):
            try:
                number = Decimal(text)
            except Exception:
                raise MeasurementFormatError(
                    f"{what} is not a rational number: {value!r}"
                ) from None
            if not number.is_finite():
                raise MeasurementFormatError(f"{what} must be finite")
            return Fraction(number)
    if _is_tag(value):
        payload = value.value
        if value.tag == 30:
            if not isinstance(payload, (list, tuple)) or len(payload) != 2:
                raise MeasurementFormatError(f"{what} has a malformed rational payload")
            numerator = exact_fraction(payload[0], what=f"{what} numerator")
            denominator = exact_fraction(payload[1], what=f"{what} denominator")
            if denominator == 0:
                raise MeasurementFormatError(f"{what} has a zero denominator")
            return numerator / denominator
        if value.tag == 4:
            if not isinstance(payload, (list, tuple)) or len(payload) != 2:
                raise MeasurementFormatError(f"{what} has a malformed decimal payload")
            exponent = exact_fraction(payload[0], what=f"{what} exponent")
            mantissa = exact_fraction(payload[1], what=f"{what} mantissa")
            if exponent.denominator != 1:
                raise MeasurementFormatError(f"{what} decimal exponent is not an integer")
            return mantissa * (Fraction(10) ** int(exponent))
        if value.tag == 5:
            if not isinstance(payload, (list, tuple)) or len(payload) != 2:
                raise MeasurementFormatError(f"{what} has a malformed bigfloat payload")
            exponent = exact_fraction(payload[0], what=f"{what} exponent")
            mantissa = exact_fraction(payload[1], what=f"{what} mantissa")
            if exponent.denominator != 1:
                raise MeasurementFormatError(f"{what} bigfloat exponent is not an integer")
            return mantissa * (Fraction(2) ** int(exponent))
        raise MeasurementFormatError(f"{what} has an unsupported tag {value.tag}")
    if isinstance(value, (list, tuple)) and len(value) == 2:
        numerator = exact_fraction(value[0], what=f"{what} numerator")
        denominator = exact_fraction(value[1], what=f"{what} denominator")
        if denominator == 0:
            raise MeasurementFormatError(f"{what} has a zero denominator")
        return numerator / denominator
    if isinstance(value, Mapping):
        for numerator_keys, denominator_keys in (
            (("n", "num", "numerator"), ("d", "den", "denominator")),
            (("value",), ("scale",)),
        ):
            numerator = _get_any(value, numerator_keys)
            denominator = _get_any(value, denominator_keys)
            if numerator is not _MISSING and denominator is not _MISSING:
                numerator_value = exact_fraction(numerator, what=f"{what} numerator")
                denominator_value = exact_fraction(denominator, what=f"{what} denominator")
                if denominator_value == 0:
                    raise MeasurementFormatError(f"{what} has a zero denominator")
                return numerator_value / denominator_value
    raise MeasurementFormatError(f"{what} is not an exact rational number: {value!r}")


@dataclass(frozen=True)
class Anchor:
    """An absolute observation of one sample."""

    sample: SampleId
    value: Fraction
    source: Any = None
    record: Any = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.sample, SampleId):
            object.__setattr__(self, "sample", SampleId(*self.sample))
        object.__setattr__(self, "value", exact_fraction(self.value, what="anchor value"))

    @property
    def absolute(self) -> Fraction:
        return self.value


AbsoluteAnchor = Anchor


@dataclass(frozen=True)
class AffineRelation:
    """The relation ``y = a * x + b`` with exact coefficients."""

    x: SampleId
    y: SampleId
    a: Fraction
    b: Fraction
    source: Any = None
    record: Any = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.x, SampleId):
            object.__setattr__(self, "x", SampleId(*self.x))
        if not isinstance(self.y, SampleId):
            object.__setattr__(self, "y", SampleId(*self.y))
        object.__setattr__(self, "a", exact_fraction(self.a, what="relation slope"))
        object.__setattr__(self, "b", exact_fraction(self.b, what="relation intercept"))

    @property
    def slope(self) -> Fraction:
        return self.a

    @property
    def intercept(self) -> Fraction:
        return self.b

    @property
    def lhs(self) -> SampleId:
        return self.x

    @property
    def rhs(self) -> SampleId:
        return self.y

    @property
    def source_sample(self) -> SampleId:
        return self.x

    @property
    def target_sample(self) -> SampleId:
        return self.y

    def apply(self, value: Fraction) -> Fraction:
        return self.a * exact_fraction(value) + self.b

    def reverse(self) -> AffineRelation | None:
        if self.a == 0:
            return None
        return AffineRelation(
            self.y,
            self.x,
            Fraction(1) / self.a,
            -self.b / self.a,
            source=self.source,
            record=self.record,
        )

    def equation(self) -> tuple[dict[SampleId, Fraction], Fraction]:
        coefficients: dict[SampleId, Fraction] = {}
        if self.x == self.y:
            coefficient = 1 - self.a
            if coefficient:
                coefficients[self.x] = coefficient
        else:
            if self.a:
                coefficients[self.x] = -self.a
            coefficients[self.y] = Fraction(1)
        return coefficients, self.b

    def describe(self) -> str:
        return (
            f"{_sample_text(self.y)} = {_fraction_text(self.a)} * "
            f"{_sample_text(self.x)} + {_fraction_text(self.b)}"
        )


Relation = AffineRelation


@dataclass(frozen=True)
class Evidence:
    """One observation used in a derivation."""

    kind: str
    source: Any
    sample: SampleId | None = None
    detail: str = ""
    coefficient: Fraction = Fraction(1)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "kind": self.kind,
            "source": _jsonable(self.source),
            "coefficient": _fraction_text(self.coefficient),
        }
        if self.sample is not None:
            result["sample"] = _sample_dict(self.sample)
        if self.detail:
            result["detail"] = self.detail
        return result


@dataclass(frozen=True)
class Conflict:
    """An inconsistency or a rejected observation."""

    kind: str
    message: str
    samples: tuple[SampleId, ...] = ()
    sources: tuple[Any, ...] = ()
    values: tuple[Fraction, ...] = ()
    basis: tuple[Evidence, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "message": self.message,
            "samples": [_sample_dict(sample) for sample in self.samples],
            "sources": [_jsonable(source) for source in self.sources],
            "values": [_fraction_text(value) for value in self.values],
            "basis": [evidence.to_dict() for evidence in self.basis],
        }


@dataclass(frozen=True)
class InvalidRecord:
    """A record that could not be interpreted or was rejected."""

    reason: str
    source: Any = None
    record: Any = field(default=None, compare=False, repr=False)
    conflict: Conflict | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "source": _jsonable(self.source),
            "record": _jsonable(self.record),
            "conflict": self.conflict.to_dict() if self.conflict else None,
        }


@dataclass(frozen=True)
class UndeterminedRelation:
    """A relation that survives but does not fix all its samples.

    ``samples`` and ``coefficients`` describe the canonical equation
    ``sum(coefficients[i] * samples[i]) == rhs``.
    """

    samples: tuple[SampleId, ...]
    coefficients: tuple[Fraction, ...]
    rhs: Fraction
    relation: AffineRelation | None = None
    basis: tuple[Evidence, ...] = ()

    @property
    def variables(self) -> tuple[SampleId, ...]:
        return self.samples

    @property
    def constant(self) -> Fraction:
        return self.rhs

    def evaluate(self, values: Mapping[SampleId, Fraction]) -> Fraction:
        return sum(
            (
                coefficient * values[sample]
                for sample, coefficient in zip(self.samples, self.coefficients)
            ),
            Fraction(0),
        ) - self.rhs

    def to_dict(self) -> dict[str, Any]:
        return {
            "samples": [_sample_dict(sample) for sample in self.samples],
            "coefficients": [_fraction_text(coefficient) for coefficient in self.coefficients],
            "rhs": _fraction_text(self.rhs),
            "relation": _relation_dict(self.relation) if self.relation else None,
            "basis": [evidence.to_dict() for evidence in self.basis],
        }


@dataclass(frozen=True)
class AffineExpression:
    """An unresolved sample expressed through free samples."""

    sample: SampleId
    constant: Fraction
    terms: tuple[tuple[SampleId, Fraction], ...]

    @property
    def is_constant(self) -> bool:
        return not self.terms

    def evaluate(self, values: Mapping[SampleId, Fraction]) -> Fraction:
        return self.constant + sum(
            (coefficient * values[sample] for sample, coefficient in self.terms), Fraction(0)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample": _sample_dict(self.sample),
            "constant": _fraction_text(self.constant),
            "terms": [
                {"sample": _sample_dict(sample), "coefficient": _fraction_text(coefficient)}
                for sample, coefficient in self.terms
            ],
        }


@dataclass(frozen=True)
class Component:
    """One independent connected component of the reconciled graph."""

    samples: tuple[SampleId, ...]
    generation: Hashable | None
    confirmed: tuple[SampleId, ...]
    undetermined: tuple[SampleId, ...]
    conflicts: tuple[Conflict, ...] = ()

    @property
    def generations(self) -> tuple[Hashable, ...]:
        return tuple(sorted({sample[1] for sample in self.samples}, key=_sort_key))

    def to_dict(self) -> dict[str, Any]:
        return {
            "generation": _jsonable(self.generation),
            "generations": [_jsonable(generation) for generation in self.generations],
            "samples": [_sample_dict(sample) for sample in self.samples],
            "confirmed": [_sample_dict(sample) for sample in self.confirmed],
            "undetermined": [_sample_dict(sample) for sample in self.undetermined],
            "conflicts": [conflict.to_dict() for conflict in self.conflicts],
        }


@dataclass(frozen=True)
class ReconciliationResult:
    """The outcome of :func:`reconcile`."""

    confirmed: dict[SampleId, Fraction]
    evidence: dict[SampleId, tuple[Evidence, ...]]
    undetermined: tuple[UndeterminedRelation, ...]
    expressions: dict[SampleId, AffineExpression]
    conflicts: tuple[Conflict, ...]
    relations: tuple[AffineRelation, ...]
    observations: tuple[Anchor | AffineRelation, ...]
    rejected: tuple[InvalidRecord, ...]
    components: tuple[Component, ...]
    duplicate_relations: int = 0
    duplicate_anchors: int = 0
    conflicted: tuple[SampleId, ...] = ()

    @property
    def values(self) -> dict[SampleId, Fraction]:
        return self.confirmed

    @property
    def confirmed_values(self) -> dict[SampleId, Fraction]:
        return self.confirmed

    @property
    def unresolved(self) -> tuple[UndeterminedRelation, ...]:
        return self.undetermined

    @property
    def unresolved_relations(self) -> tuple[UndeterminedRelation, ...]:
        return self.undetermined

    @property
    def basis(self) -> dict[SampleId, tuple[Evidence, ...]]:
        return self.evidence

    @property
    def invalid(self) -> tuple[InvalidRecord, ...]:
        return self.rejected

    @property
    def conflicted_samples(self) -> tuple[SampleId, ...]:
        return self.conflicted

    @property
    def accepted_relations(self) -> tuple[AffineRelation, ...]:
        return self.relations

    def value(self, sample: SampleId | tuple[Any, Any, Any]) -> Fraction | None:
        if not isinstance(sample, SampleId):
            sample = SampleId(*sample)
        return self.confirmed.get(sample)

    def to_dict(self) -> dict[str, Any]:
        return {
            "confirmed": [
                {
                    "sample": _sample_dict(sample),
                    "value": _fraction_text(value),
                    "basis": [evidence.to_dict() for evidence in self.evidence.get(sample, ())],
                }
                for sample, value in sorted(
                    self.confirmed.items(), key=lambda item: _sample_sort_key(item[0])
                )
            ],
            "undetermined": [relation.to_dict() for relation in self.undetermined],
            "expressions": [
                expression.to_dict()
                for _, expression in sorted(
                    self.expressions.items(), key=lambda item: _sample_sort_key(item[0])
                )
            ],
            "conflicts": [conflict.to_dict() for conflict in self.conflicts],
            "invalid": [record.to_dict() for record in self.rejected],
            "relations": [_relation_dict(relation) for relation in self.relations],
            "components": [component.to_dict() for component in self.components],
            "duplicate_relations": self.duplicate_relations,
            "duplicate_anchors": self.duplicate_anchors,
            "conflicted": [_sample_dict(sample) for sample in self.conflicted],
        }

    def to_json(self, *, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


def _sample_text(sample: SampleId) -> str:
    return f"({sample[0]!r}, {sample[1]!r}, {sample[2]!r})"


def _sample_dict(sample: SampleId) -> dict[str, Any]:
    return {
        "sensor": _jsonable(sample[0]),
        "generation": _jsonable(sample[1]),
        "sample": _jsonable(sample[2]),
    }


def _relation_dict(relation: AffineRelation | None) -> dict[str, Any] | None:
    if relation is None:
        return None
    return {
        "x": _sample_dict(relation.x),
        "y": _sample_dict(relation.y),
        "a": _fraction_text(relation.a),
        "b": _fraction_text(relation.b),
        "source": _jsonable(relation.source),
    }


def _jsonable(value: Any) -> Any:
    if isinstance(value, Fraction):
        return _fraction_text(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, SampleId):
        return _sample_dict(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [_jsonable(item) for item in sorted(value, key=_sort_key)]
    if isinstance(value, bytes):
        return value.hex()
    if _is_tag(value):
        return {"tag": value.tag, "value": _jsonable(value.value)}
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    return repr(value)


def _get_any(mapping: Mapping[Any, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    for key in keys:
        for candidate, value in mapping.items():
            if isinstance(candidate, str) and candidate.lower() == key.lower():
                return value
    return _MISSING


def _has_any(mapping: Mapping[Any, Any], keys: Sequence[str]) -> bool:
    return _get_any(mapping, keys) is not _MISSING


def _unwrap_tags(value: Any) -> Any:
    while _is_tag(value) and value.tag not in (30, 4, 5):
        value = value.value
    return value


def _tag_numbers(value: Any) -> tuple[int, ...]:
    numbers: list[int] = []
    while _is_tag(value):
        numbers.append(value.tag)
        value = value.value
    return tuple(numbers)


def _context_from_mapping(value: Mapping[Any, Any], parent: Mapping[str, Any]) -> dict[str, Any]:
    context = dict(parent)
    sensor = _get_any(value, _SENSOR_KEYS)
    generation = _get_any(value, _GENERATION_KEYS)
    if sensor is not _MISSING:
        context["sensor"] = _freeze(sensor)
    if generation is not _MISSING:
        context["generation"] = _freeze(generation)
    return context


def _parse_sample(value: Any, context: Mapping[str, Any], *, what: str = "sample") -> SampleId:
    value = _unwrap_tags(value)
    if isinstance(value, SampleId):
        return value
    if isinstance(value, Mapping):
        sensor = _get_any(value, _SENSOR_KEYS)
        generation = _get_any(value, _GENERATION_KEYS)
        sample = _get_any(value, _SAMPLE_KEYS)
        if sample is _MISSING:
            for key in ("id", "key", "label"):
                if key in value:
                    sample = value[key]
                    break
        if sensor is _MISSING:
            sensor = context.get("sensor", _MISSING)
        if generation is _MISSING:
            generation = context.get("generation", _MISSING)
        if sample is _MISSING or sensor is _MISSING or generation is _MISSING:
            raise MeasurementFormatError(f"{what} is missing sensor, generation or sample number")
        return SampleId(_freeze(sensor), _freeze(generation), _freeze(sample))
    if isinstance(value, (list, tuple)):
        if len(value) == 3:
            sensor, generation, sample = value
            return SampleId(_freeze(sensor), _freeze(generation), _freeze(sample))
        if len(value) == 2:
            if "sensor" in context:
                generation, sample = value
                return SampleId(_freeze(context["sensor"]), _freeze(generation), _freeze(sample))
            if "generation" in context:
                sensor, sample = value
                return SampleId(_freeze(sensor), _freeze(context["generation"]), _freeze(sample))
            raise MeasurementFormatError(f"{what} needs a sensor or generation context")
        if len(value) == 1:
            value = value[0]
        else:
            raise MeasurementFormatError(f"{what} must have two or three components")
    if isinstance(value, str) and "sensor" not in context and "generation" not in context:
        for separator in ("::", "|", "#", "/", ":"):
            parts = value.split(separator)
            if len(parts) == 3:
                return SampleId(_freeze(parts[0]), _freeze(parts[1]), _freeze(parts[2]))
        raise MeasurementFormatError(f"{what} must contain sensor, generation and sample labels")
    if "sensor" not in context or "generation" not in context:
        raise MeasurementFormatError(f"{what} needs sensor and generation context")
    return SampleId(_freeze(context["sensor"]), _freeze(context["generation"]), _freeze(value))


def _endpoint_like(value: Any) -> bool:
    value = _unwrap_tags(value)
    if isinstance(value, (SampleId, Mapping)):
        return True
    if isinstance(value, (list, tuple)) and len(value) in (2, 3):
        return True
    if isinstance(value, str):
        return any(len(value.split(separator)) == 3 for separator in ("::", "|", "#", "/", ":"))
    return False


def _parse_anchor(value: Any, context: Mapping[str, Any], source: Any) -> Anchor:
    payload = _unwrap_tags(value)
    if isinstance(payload, Anchor):
        return payload
    if isinstance(payload, Mapping):
        anchor_value = _get_any(payload, _VALUE_KEYS)
        if anchor_value is _MISSING:
            raise MeasurementFormatError("absolute record has no value")
        sample_value = _get_any(payload, _SAMPLE_KEYS)
        if sample_value is _MISSING:
            sample_value = payload
        sample_context = _context_from_mapping(payload, context)
        sample = _parse_sample(sample_value, sample_context, what="absolute sample")
        return Anchor(
            sample,
            exact_fraction(anchor_value, what="anchor value"),
            source=source,
            record=payload,
        )
    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes, bytearray)):
        if len(payload) == 2:
            sample = _parse_sample(payload[0], context, what="absolute sample")
            return Anchor(
                sample, exact_fraction(payload[1], what="anchor value"), source, payload
            )
        if len(payload) == 4:
            sensor, generation, sample_number, anchor_value = payload
            sample = SampleId(_freeze(sensor), _freeze(generation), _freeze(sample_number))
            return Anchor(
                sample, exact_fraction(anchor_value, what="anchor value"), source, payload
            )
        if len(payload) == 5:
            sensor, generation, sample_number, numerator, denominator = payload
            sample = SampleId(_freeze(sensor), _freeze(generation), _freeze(sample_number))
            return Anchor(
                sample,
                exact_fraction([numerator, denominator], what="anchor value"),
                source,
                payload,
            )
        if len(payload) == 3 and "generation" in context:
            if _scalar_like(payload[0]) and _scalar_like(payload[1]):
                sensor, sample_number, anchor_value = payload
                sample = SampleId(
                    _freeze(sensor), _freeze(context["generation"]), _freeze(sample_number)
                )
            else:
                sample = _parse_sample(payload[0], context, what="absolute sample")
                anchor_value = payload[1] if len(payload) == 2 else payload[2]
            return Anchor(
                sample, exact_fraction(anchor_value, what="anchor value"), source, payload
            )
        if len(payload) == 3 and "sensor" in context and "generation" not in context:
            sample = _parse_sample(payload[:2], context, what="absolute sample")
            return Anchor(
                sample, exact_fraction(payload[2], what="anchor value"), source, payload
            )
    raise MeasurementFormatError("unrecognized absolute record")


def _parse_relation(value: Any, context: Mapping[str, Any], source: Any) -> AffineRelation:
    payload = _unwrap_tags(value)
    if isinstance(payload, AffineRelation):
        return payload
    if isinstance(payload, Mapping):
        x_value = _get_any(payload, _X_KEYS)
        y_value = _get_any(payload, _Y_KEYS)
        a_value = _get_any(payload, _A_KEYS)
        b_value = _get_any(payload, _B_KEYS)
        if (x_value is _MISSING or y_value is _MISSING) and "samples" in payload:
            samples = payload["samples"]
            if isinstance(samples, Sequence) and len(samples) == 2:
                x_value, y_value = samples
        if (a_value is _MISSING or b_value is _MISSING) and "coefficients" in payload:
            coefficients = payload["coefficients"]
            if isinstance(coefficients, Sequence) and len(coefficients) == 2:
                a_value, b_value = coefficients
        if (
            x_value is _MISSING
            or y_value is _MISSING
            or a_value is _MISSING
            or b_value is _MISSING
        ):
            raise MeasurementFormatError("affine record is missing x, y, a or b")
        relation_context = _context_from_mapping(payload, context)
        return AffineRelation(
            _parse_sample(x_value, relation_context, what="relation x"),
            _parse_sample(y_value, relation_context, what="relation y"),
            exact_fraction(a_value, what="relation slope"),
            exact_fraction(b_value, what="relation intercept"),
            source=source,
            record=payload,
        )
    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes, bytearray)):
        if len(payload) == 8:
            if _endpoint_like(payload[0]) and _endpoint_like(payload[3]):
                x = SampleId(_freeze(payload[0]), _freeze(payload[1]), _freeze(payload[2]))
                y = SampleId(_freeze(payload[3]), _freeze(payload[4]), _freeze(payload[5]))
                a_value, b_value = payload[6], payload[7]
            elif payload[0] == payload[3] and payload[1] == payload[4]:
                # sensor, generation, x, sensor, generation, y, a, b
                x = SampleId(_freeze(payload[0]), _freeze(payload[1]), _freeze(payload[2]))
                y = SampleId(_freeze(payload[3]), _freeze(payload[4]), _freeze(payload[5]))
                a_value, b_value = payload[6], payload[7]
            else:
                raise MeasurementFormatError(
                    "eight-field affine record needs endpoints or a shared sensor/generation"
                )
        elif len(payload) == 6:
            if _endpoint_like(payload[0]) and _endpoint_like(payload[1]):
                x = _parse_sample(payload[0], context, what="relation x")
                y = _parse_sample(payload[1], context, what="relation y")
                a_value = (payload[2], payload[3])
                b_value = (payload[4], payload[5])
            else:
                sensor, generation, x_number, y_number, a_value, b_value = payload
                x = SampleId(_freeze(sensor), _freeze(generation), _freeze(x_number))
                y = SampleId(_freeze(sensor), _freeze(generation), _freeze(y_number))
        elif len(payload) == 5:
            sensor, generation, x_number, y_number, coefficient_pair = payload
            x = SampleId(_freeze(sensor), _freeze(generation), _freeze(x_number))
            y = SampleId(_freeze(sensor), _freeze(generation), _freeze(y_number))
            a_value, b_value = coefficient_pair
        elif len(payload) == 4:
            x_value, y_value, a_value, b_value = payload
            x = _parse_sample(x_value, context, what="relation x")
            y = _parse_sample(y_value, context, what="relation y")
        elif len(payload) == 3 and isinstance(payload[2], Mapping):
            x = _parse_sample(payload[0], context, what="relation x")
            y = _parse_sample(payload[1], context, what="relation y")
            a_value = _get_any(payload[2], _A_KEYS)
            b_value = _get_any(payload[2], _B_KEYS)
            if a_value is _MISSING or b_value is _MISSING:
                raise MeasurementFormatError("affine coefficient map is missing a or b")
        elif len(payload) == 3 and isinstance(payload[2], Sequence) and len(payload[2]) == 2:
            x = _parse_sample(payload[0], context, what="relation x")
            y = _parse_sample(payload[1], context, what="relation y")
            a_value, b_value = payload[2]
        elif len(payload) == 3 and isinstance(payload[1], Sequence) and len(payload[1]) == 2:
            x = _parse_sample(payload[0], context, what="relation x")
            y = _parse_sample(payload[2], context, what="relation y")
            a_value, b_value = payload[1]
        else:
            raise MeasurementFormatError("unrecognized affine record")
        return AffineRelation(
            x,
            y,
            exact_fraction(a_value, what="relation slope"),
            exact_fraction(b_value, what="relation intercept"),
            source=source,
            record=payload,
        )
    raise MeasurementFormatError("unrecognized affine record")


def _scalar_like(value: Any) -> bool:
    value = _unwrap_tags(value)
    if isinstance(value, bool) or value is None:
        return False
    return isinstance(value, (int, float, Fraction, Decimal, str)) or _is_tag(value)


def _looks_like_anchor(payload: Any) -> bool:
    payload = _unwrap_tags(payload)
    if isinstance(payload, Anchor):
        return True
    if isinstance(payload, Mapping):
        return _has_any(payload, _VALUE_KEYS) and not _has_any(payload, _A_KEYS)
    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes, bytearray)):
        if len(payload) == 2:
            return _endpoint_like(payload[0]) and _scalar_like(payload[1])
        if len(payload) == 5:
            return _scalar_like(payload[0]) and _scalar_like(payload[1])
        if len(payload) == 4:
            return not (_endpoint_like(payload[0]) and _endpoint_like(payload[1]))
    return False


def _looks_like_relation(payload: Any) -> bool:
    payload = _unwrap_tags(payload)
    if isinstance(payload, AffineRelation):
        return True
    if isinstance(payload, Mapping):
        if (
            _has_any(payload, _X_KEYS)
            and _has_any(payload, _Y_KEYS)
            and _has_any(payload, _A_KEYS)
            and _has_any(payload, _B_KEYS)
        ):
            return True
        if "samples" in payload and (
            "coefficients" in payload or ("a" in payload and "b" in payload)
        ):
            return True
        return False
    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes, bytearray)):
        if len(payload) == 8:
            return True
        if len(payload) == 6:
            if _endpoint_like(payload[0]) and _endpoint_like(payload[1]):
                return (
                    isinstance(payload[2], (list, tuple))
                    and len(payload[2]) == 2
                    and isinstance(payload[3], (list, tuple))
                    and len(payload[3]) == 2
                )
            return _scalar_like(payload[0]) and _scalar_like(payload[1]) and _scalar_like(
                payload[4]
            ) and _scalar_like(payload[5])
        if len(payload) == 5:
            return (
                _scalar_like(payload[0])
                and _scalar_like(payload[1])
                and isinstance(payload[4], (list, tuple))
                and len(payload[4]) == 2
            )
        if len(payload) == 4:
            return _endpoint_like(payload[0]) and _endpoint_like(payload[1])
        if len(payload) == 3:
            return isinstance(payload[2], Mapping) or (
                isinstance(payload[1], Sequence)
                and not isinstance(payload[1], (str, bytes, bytearray))
                and len(payload[1]) == 2
            ) or (
                isinstance(payload[2], Sequence)
                and not isinstance(payload[2], (str, bytes, bytearray))
                and len(payload[2]) == 2
            )
    return False


def _is_record_like(value: Any) -> bool:
    if isinstance(value, (Anchor, AffineRelation)) or _is_tag(value):
        return True
    if isinstance(value, Mapping):
        return _looks_like_anchor(value) or _looks_like_relation(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return _looks_like_anchor(value) or _looks_like_relation(value)
    return False


def _is_single_record(value: Any) -> bool:
    if isinstance(value, (Anchor, AffineRelation)) or _is_tag(value):
        return True
    if isinstance(value, Mapping):
        return (
            _looks_like_anchor(value)
            or _looks_like_relation(value)
            or any(key in value for key in (*_RECORD_KEYS, *_ANCHOR_KEYS, *_RELATION_KEYS))
            or "generations" in value
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if len(value) > 1 and all(_is_record_like(item) for item in value):
            return False
        return _looks_like_anchor(value) or _looks_like_relation(value)
    return False


def _decode_stream(data: bytes) -> list[Any]:
    from ._decoder import CBORDecoder

    decoder = CBORDecoder(BytesIO(data))
    records: list[Any] = []
    while True:
        try:
            records.append(decoder.decode())
        except EOFError:
            return records


def _iter_input(
    value: Any, context: Mapping[str, Any] | None = None
) -> Iterator[tuple[Any, dict[str, Any]]]:
    context = dict(context or {})
    if isinstance(value, (bytes, bytearray, memoryview)):
        for record in _decode_stream(bytes(value)):
            yield from _iter_input(record, context)
        return
    if hasattr(value, "read"):
        data = value.read()
        if isinstance(data, str):
            data = data.encode()
        for record in _decode_stream(data):
            yield from _iter_input(record, context)
        return
    if isinstance(value, Mapping):
        new_context = _context_from_mapping(value, context)
        if _looks_like_anchor(value) or _looks_like_relation(value):
            yield value, new_context
            return
        found_envelope = False
        for key in (*_RECORD_KEYS, *_ANCHOR_KEYS, *_RELATION_KEYS):
            if key in value:
                found_envelope = True
                yield from _iter_input(value[key], new_context)
        if found_envelope:
            return
        if "generations" in value:
            sensor = new_context.get("sensor", _MISSING)
            if sensor is _MISSING:
                raise MeasurementFormatError("generation group has no sensor")
            generations = value["generations"]
            if isinstance(generations, Mapping):
                for generation, samples in generations.items():
                    group_context = dict(new_context)
                    group_context["sensor"] = sensor
                    group_context["generation"] = _freeze(generation)
                    yield from _iter_input(samples, group_context)
                return
        if "samples" in value:
            sensor = new_context.get("sensor", _MISSING)
            generation = new_context.get("generation", _MISSING)
            if sensor is _MISSING or generation is _MISSING:
                raise MeasurementFormatError("sample group has no sensor or generation")
            samples = value["samples"]
            if isinstance(samples, Mapping):
                for sample, sample_value in samples.items():
                    yield (
                        Anchor(
                            SampleId(_freeze(sensor), _freeze(generation), _freeze(sample)),
                            exact_fraction(sample_value),
                        ),
                        dict(new_context),
                    )
                return
        yield value, new_context
        return
    if isinstance(value, (list, tuple)) and not _is_single_record(value):
        for item in value:
            yield from _iter_input(item, context)
        return
    yield value, context


@dataclass(frozen=True)
class ParsedRecords:
    anchors: tuple[Anchor, ...]
    relations: tuple[AffineRelation, ...]
    rejected: tuple[InvalidRecord, ...]


def parse_records(
    records: Any = None,
    *,
    anchors: Iterable[Any] = (),
    relations: Iterable[Any] = (),
    strict: bool = False,
) -> ParsedRecords:
    """Parse records without solving them.

    ``records`` may be raw CBOR bytes, a CBOR sequence, a decoded list, a file
    object, a mapping envelope, or an iterable of already typed observations.
    ``anchors`` and ``relations`` supply the two record kinds separately.
    """

    parsed_anchors: list[Anchor] = []
    parsed_relations: list[AffineRelation] = []
    rejected: list[InvalidRecord] = []

    def add_anchor(item: Any, context: Mapping[str, Any], source: Any) -> bool:
        try:
            parsed_anchors.append(_parse_anchor(item, context, source))
        except (MeasurementFormatError, TypeError, ValueError) as exc:
            conflict = Conflict("malformed_anchor", str(exc), sources=(source,))
            rejected.append(InvalidRecord("malformed_anchor", source, item, conflict))
            if strict:
                raise MeasurementFormatError(str(exc)) from exc
            return False
        return True

    def add_relation(item: Any, context: Mapping[str, Any], source: Any) -> bool:
        try:
            parsed_relations.append(_parse_relation(item, context, source))
        except (MeasurementFormatError, TypeError, ValueError) as exc:
            conflict = Conflict("malformed_relation", str(exc), sources=(source,))
            rejected.append(InvalidRecord("malformed_relation", source, item, conflict))
            if strict:
                raise MeasurementFormatError(str(exc)) from exc
            return False
        return True

    index = 0
    if records is not None:
        for item, context in _iter_input(records):
            source = index
            index += 1
            if isinstance(item, Anchor):
                parsed_anchors.append(item)
                continue
            if isinstance(item, AffineRelation):
                parsed_relations.append(item)
                continue
            tags = _tag_numbers(item)
            payload = _unwrap_tags(item)
            absolute_tag = any(tag in (TAG_ABSOLUTE, TAG_ANCHOR) for tag in tags)
            affine_tag = any(tag in (TAG_AFFINE, TAG_RELATION) for tag in tags)
            if absolute_tag and not affine_tag:
                add_anchor(payload, context, source)
            elif affine_tag:
                add_relation(payload, context, source)
            elif _looks_like_relation(payload):
                add_relation(payload, context, source)
            elif _looks_like_anchor(payload):
                add_anchor(payload, context, source)
            else:
                conflict = Conflict(
                    "unrecognized_record",
                    "record is neither an absolute nor an affine record",
                    sources=(source,),
                )
                rejected.append(InvalidRecord("unrecognized_record", source, item, conflict))
                if strict:
                    raise MeasurementFormatError(
                        "record is neither an absolute nor an affine record"
                    )

    for anchor in anchors:
        if isinstance(anchor, Anchor):
            parsed_anchors.append(anchor)
        else:
            index += 1
            add_anchor(anchor, {}, index)

    for relation in relations:
        if isinstance(relation, AffineRelation):
            parsed_relations.append(relation)
        else:
            index += 1
            add_relation(relation, {}, index)

    return ParsedRecords(tuple(parsed_anchors), tuple(parsed_relations), tuple(rejected))


def _normalized_equation(
    coefficients: Mapping[SampleId, Fraction], rhs: Fraction
) -> tuple[tuple[tuple[SampleId, Fraction], ...], Fraction] | None:
    """Return a primitive integer form of ``sum(c*x) == rhs``.

    The canonical form makes ``2y = 4x + 6`` and ``y = 2x + 3`` identical, so
    repeated delivery is not counted as a second observation.
    """

    items = [(sample, coefficient) for sample, coefficient in coefficients.items() if coefficient]
    if not items:
        if rhs:
            raise MeasurementFormatError("relation reduces to a contradiction")
        return None
    items.sort(key=lambda item: _sample_sort_key(item[0]))
    denominator = 1
    for _, coefficient in items:
        denominator = math.lcm(denominator, coefficient.denominator)
    denominator = math.lcm(denominator, rhs.denominator)
    integers = [int(coefficient * denominator) for _, coefficient in items]
    rhs_integer = int(rhs * denominator)
    divisor = 0
    for value in (*integers, rhs_integer):
        divisor = math.gcd(divisor, abs(value))
    if divisor == 0:
        return None
    integers = [value // divisor for value in integers]
    rhs_integer //= divisor
    if integers[0] < 0 or (integers[0] == 0 and rhs_integer < 0):
        integers = [-value for value in integers]
        rhs_integer = -rhs_integer
    return tuple(
        (sample, Fraction(value)) for (sample, _), value in zip(items, integers)
    ), Fraction(rhs_integer)


def _relation_key(relation: AffineRelation) -> tuple[Any, ...] | None:
    coefficients, rhs = relation.equation()
    return _normalized_equation(coefficients, rhs)


class _Row:
    __slots__ = ("coefficients", "rhs", "provenance")

    def __init__(
        self,
        coefficients: dict[SampleId, Fraction],
        rhs: Fraction,
        provenance: dict[int, Fraction],
    ) -> None:
        self.coefficients = coefficients
        self.rhs = rhs
        self.provenance = provenance

    @property
    def pivot(self) -> SampleId:
        return min(self.coefficients, key=_sample_sort_key)

    def copy(self) -> _Row:
        return _Row(dict(self.coefficients), self.rhs, dict(self.provenance))

    def add_scaled(self, other: _Row, factor: Fraction) -> _Row:
        coefficients = dict(self.coefficients)
        for sample, coefficient in other.coefficients.items():
            value = coefficients.get(sample, Fraction(0)) - factor * coefficient
            if value:
                coefficients[sample] = value
            elif sample in coefficients:
                del coefficients[sample]
        rhs = self.rhs - factor * other.rhs
        provenance = dict(self.provenance)
        for key, value in other.provenance.items():
            new_value = provenance.get(key, Fraction(0)) + factor * value
            if new_value:
                provenance[key] = new_value
            elif key in provenance:
                del provenance[key]
        return _Row(coefficients, rhs, provenance)

    def normalize(self) -> _Row:
        pivot_coefficient = self.coefficients[self.pivot]
        if pivot_coefficient == 1:
            return self
        coefficients = {
            sample: coefficient / pivot_coefficient
            for sample, coefficient in self.coefficients.items()
        }
        provenance = {
            key: value / pivot_coefficient for key, value in self.provenance.items() if value
        }
        return _Row(coefficients, self.rhs / pivot_coefficient, provenance)


class _System:
    """A linear system over Fractions, kept in reduced row echelon form."""

    def __init__(self) -> None:
        self.rows: list[_Row] = []

    @staticmethod
    def _eliminate(rows: list[_Row]) -> tuple[list[_Row], _Row | None]:
        """Reduce ``rows`` to RREF and return any contradiction row."""

        matrix: list[_Row] = []
        for row in rows:
            if not row.coefficients:
                if row.rhs:
                    return [], row
                continue
            matrix.append(row.copy())
        variables = sorted(
            {sample for row in matrix for sample in row.coefficients},
            key=_sample_sort_key,
        )
        next_row = 0
        for variable in variables:
            pivot_at = next(
                (
                    index
                    for index in range(next_row, len(matrix))
                    if variable in matrix[index].coefficients
                ),
                None,
            )
            if pivot_at is None:
                continue
            pivot_row = matrix.pop(pivot_at)
            pivot_row = pivot_row.normalize()
            matrix.insert(next_row, pivot_row)
            for index, row in enumerate(matrix):
                if index == next_row:
                    continue
                coefficient = row.coefficients.get(variable)
                if coefficient:
                    matrix[index] = row.add_scaled(pivot_row, coefficient)
            next_row += 1

        result: list[_Row] = []
        for row in matrix:
            if not row.coefficients:
                if row.rhs:
                    return [], row
                continue
            result.append(row.normalize())
        return result, None

    def add(self, equation: _Row) -> tuple[bool, _Row | None]:
        reduced, witness = self._eliminate([*self.rows, equation])
        if witness is not None:
            return False, witness
        self.rows = reduced
        return True, None

    def fixed(self) -> dict[SampleId, Fraction]:
        """Return every sample whose value is forced by the system."""

        return {
            row.pivot: row.rhs / row.coefficients[row.pivot]
            for row in self.rows
            if len(row.coefficients) == 1
        }

    def expressions(self) -> dict[SampleId, AffineExpression]:
        expressions: dict[SampleId, AffineExpression] = {}
        pivots = {row.pivot for row in self.rows}
        free = sorted(
            {
                sample
                for row in self.rows
                for sample in row.coefficients
                if sample not in pivots
            },
            key=_sample_sort_key,
        )
        for row in self.rows:
            pivot = row.pivot
            pivot_coefficient = row.coefficients[pivot]
            terms: list[tuple[SampleId, Fraction]] = []
            for sample, coefficient in sorted(
                row.coefficients.items(), key=lambda item: _sample_sort_key(item[0])
            ):
                if sample != pivot:
                    terms.append((sample, -coefficient / pivot_coefficient))
            expressions[pivot] = AffineExpression(
                pivot, row.rhs / pivot_coefficient, tuple(terms)
            )
        for sample in free:
            expressions[sample] = AffineExpression(sample, Fraction(0), ((sample, Fraction(1)),))
        return expressions


def _evidence_from_provenance(
    provenance: Mapping[int, Fraction],
    observations: Mapping[int, Anchor | AffineRelation],
) -> tuple[Evidence, ...]:
    evidence: list[Evidence] = []
    for index, coefficient in sorted(provenance.items()):
        if not coefficient:
            continue
        observation = observations[index]
        if isinstance(observation, Anchor):
            evidence.append(
                Evidence(
                    "anchor",
                    observation.source,
                    sample=observation.sample,
                    detail=f"absolute value {_fraction_text(observation.value)}",
                    coefficient=coefficient,
                )
            )
        else:
            evidence.append(
                Evidence(
                    "relation",
                    observation.source,
                    detail=observation.describe(),
                    coefficient=coefficient,
                )
            )
    return tuple(evidence)


def _union_find(samples: Iterable[SampleId]) -> dict[SampleId, SampleId]:
    return {sample: sample for sample in samples}


def _find(parents: dict[SampleId, SampleId], sample: SampleId) -> SampleId:
    root = sample
    while parents[root] != root:
        root = parents[root]
    while parents[sample] != root:
        parents[sample], sample = root, parents[sample]
    return root


def _union(parents: dict[SampleId, SampleId], left: SampleId, right: SampleId) -> None:
    left_root = _find(parents, left)
    right_root = _find(parents, right)
    if left_root != right_root:
        parents[right_root] = left_root


def reconcile(
    records: Any = None,
    *,
    anchors: Iterable[Any] = (),
    relations: Iterable[Any] = (),
    strict: bool = False,
    allow_cross_generation: bool = False,
) -> ReconciliationResult:
    """Reconcile an unordered set of compressed measurements.

    :param records:
        raw CBOR bytes, a file object, a decoded sequence, a mapping with
        ``anchors``/``relations`` keys, or an iterable of records
    :param anchors:
        explicitly supplied absolute observations
    :param relations:
        explicitly supplied affine relations
    :param strict:
        raise :class:`MeasurementFormatError` instead of recording malformed
        input
    :param allow_cross_generation:
        if true, relations may connect samples from different generations.
        The default is false, matching the collector rule that a restart
        starts a new series.

    The result exposes ``confirmed`` (exact values keyed by :class:`SampleId`),
    ``evidence`` (the observations behind each confirmed value),
    ``undetermined`` (canonical surviving relations), ``expressions`` (each
    unresolved sample in terms of free samples), ``conflicts``, ``relations``,
    ``components`` and ``to_dict()``/``to_json()``.
    """

    parsed = parse_records(records, anchors=anchors, relations=relations, strict=strict)
    rejected: list[InvalidRecord] = list(parsed.rejected)
    conflicts: list[Conflict] = []
    observations: dict[int, Anchor | AffineRelation] = {}
    observation_index = 0

    # Absolute observations are authoritative.  Identical repeats are one
    # observation; contradictory anchors for the same sample are all rejected
    # so that they cannot mask a genuinely undetermined value.  Relations
    # touching a conflicted sample are also rejected: none of the candidate
    # anchor values can be singled out, so nothing may be propagated through
    # that sample.
    conflicted_samples: set[SampleId] = set()
    anchor_groups: dict[SampleId, list[Anchor]] = defaultdict(list)
    for anchor in parsed.anchors:
        anchor_groups[anchor.sample].append(anchor)

    unique_anchors: list[Anchor] = []
    duplicate_anchors = 0
    for sample in sorted(anchor_groups, key=_sample_sort_key):
        group = anchor_groups[sample]
        by_value: dict[Fraction, list[Anchor]] = defaultdict(list)
        for anchor in group:
            by_value[anchor.value].append(anchor)
        if len(by_value) > 1:
            conflicted_samples.add(sample)
            values = tuple(
                sorted(by_value, key=lambda value: (value.numerator, value.denominator))
            )
            conflicts.append(
                Conflict(
                    "anchor_conflict",
                    f"independent absolute anchors disagree for {_sample_text(sample)}",
                    samples=(sample,),
                    sources=tuple(anchor.source for anchor in group),
                    values=values,
                    basis=tuple(
                        Evidence(
                            "anchor",
                            anchor.source,
                            sample=sample,
                            detail=f"absolute value {_fraction_text(anchor.value)}",
                        )
                        for anchor in group
                    ),
                )
            )
            continue
        unique_anchors.append(group[0])
        duplicate_anchors += len(group) - 1

    # Identical affine equations are one observation.  Normalising the
    # equation also catches the same relation written in reverse form.
    relation_groups: dict[tuple[Any, ...], list[AffineRelation]] = defaultdict(list)
    for relation in parsed.relations:
        if relation.x in conflicted_samples or relation.y in conflicted_samples:
            conflict = Conflict(
                "anchor_conflict",
                "relation touches a sample whose absolute anchors disagree",
                samples=(
                    (relation.x,)
                    if relation.x in conflicted_samples
                    else ()
                )
                + ((relation.y,) if relation.y in conflicted_samples else ()),
                sources=(relation.source,),
                basis=(Evidence("relation", relation.source, detail=relation.describe()),),
            )
            conflicts.append(conflict)
            rejected.append(
                InvalidRecord("anchor_conflict", relation.source, relation.record, conflict)
            )
            continue
        if not allow_cross_generation and relation.x[1] != relation.y[1]:
            conflict = Conflict(
                "cross_generation",
                "relation connects samples from different generations",
                samples=(relation.x, relation.y),
                sources=(relation.source,),
                basis=(Evidence("relation", relation.source, detail=relation.describe()),),
            )
            conflicts.append(conflict)
            rejected.append(
                InvalidRecord("cross_generation", relation.source, relation.record, conflict)
            )
            continue
        try:
            key = _relation_key(relation)
        except MeasurementFormatError as exc:
            conflict = Conflict(
                "bad_relation",
                str(exc),
                samples=(relation.x, relation.y),
                sources=(relation.source,),
            )
            conflicts.append(conflict)
            rejected.append(
                InvalidRecord("bad_relation", relation.source, relation.record, conflict)
            )
            if strict:
                raise
            continue
        if key is not None:
            relation_groups[key].append(relation)

    unique_relations: list[AffineRelation] = []
    duplicate_relations = 0
    for group in relation_groups.values():
        # Keep first-delivery order: if a later relation contradicts values
        # already fixed by earlier observations, the later one is the one
        # rejected.  Sorting by canonical key would make that choice arbitrary.
        unique_relations.append(group[0])
        duplicate_relations += len(group) - 1

    system = _System()

    def register(observation: Anchor | AffineRelation) -> int:
        nonlocal observation_index
        index = observation_index
        observation_index += 1
        observations[index] = observation
        return index

    for anchor in sorted(unique_anchors, key=lambda anchor: _sample_sort_key(anchor.sample)):
        index = register(anchor)
        ok, witness = system.add(
            _Row({anchor.sample: Fraction(1)}, anchor.value, {index: Fraction(1)})
        )
        if not ok:
            conflicts.append(
                Conflict(
                    "anchor_conflict",
                    "absolute anchor contradicts previously accepted observations",
                    samples=(anchor.sample,),
                    sources=(anchor.source,),
                    values=(anchor.value,),
                    basis=_evidence_from_provenance(
                        witness.provenance if witness else {}, observations
                    ),
                )
            )

    accepted_relations: list[AffineRelation] = []
    for relation in unique_relations:
        index = register(relation)
        coefficients, rhs = relation.equation()
        ok, witness = system.add(_Row(dict(coefficients), rhs, {index: Fraction(1)}))
        if ok:
            accepted_relations.append(relation)
        else:
            provenance = dict(witness.provenance) if witness else {}
            if provenance.get(index, Fraction(0)) < 0:
                provenance = {key: -value for key, value in provenance.items()}
            basis = list(_evidence_from_provenance(provenance, observations))
            conflict = Conflict(
                "relation_conflict",
                "affine relation contradicts the other observations",
                samples=(relation.x, relation.y),
                sources=(relation.source,),
                basis=tuple(basis),
            )
            conflicts.append(conflict)
            rejected.append(
                InvalidRecord("relation_conflict", relation.source, relation.record, conflict)
            )
            if strict:
                raise MeasurementFormatError("affine relation contradicts the other observations")

    fixed = system.fixed()
    expressions = system.expressions()

    evidence: dict[SampleId, tuple[Evidence, ...]] = {}
    provenance_by_sample: dict[SampleId, dict[int, Fraction]] = {
        next(iter(row.coefficients)): row.provenance
        for row in system.rows
        if len(row.coefficients) == 1
    }
    for sample in fixed:
        evidence[sample] = _evidence_from_provenance(
            provenance_by_sample.get(sample, {}), observations
        )

    # Canonical surviving constraints: each RREF row with free variables
    # expresses one pivot variable through free variables.  Rank-dependent
    # edges therefore appear once, not once per delivered record.
    undetermined: list[UndeterminedRelation] = []
    for row in system.rows:
        if len(row.coefficients) <= 1:
            continue
        pivot = row.pivot
        items = [(pivot, Fraction(1))]
        for sample, coefficient in sorted(
            row.coefficients.items(), key=lambda item: _sample_sort_key(item[0])
        ):
            if sample != pivot:
                items.append((sample, coefficient))
        undetermined.append(
            UndeterminedRelation(
                tuple(sample for sample, _ in items),
                tuple(coefficient for _, coefficient in items),
                row.rhs,
                basis=_evidence_from_provenance(row.provenance, observations),
            )
        )

    # Build components from accepted observations only.  Rejected edges do
    # not erase or merge the components they touched.
    parents = _union_find(
        [sample for sample in fixed]
        + [sample for relation in accepted_relations for sample in (relation.x, relation.y)]
    )
    for anchor in unique_anchors:
        parents.setdefault(anchor.sample, anchor.sample)
    for relation in accepted_relations:
        _union(parents, relation.x, relation.y)
    groups: dict[SampleId, list[SampleId]] = defaultdict(list)
    for sample in parents:
        groups[_find(parents, sample)].append(sample)

    components: list[Component] = []
    for root in sorted(groups, key=_sample_sort_key):
        samples = tuple(sorted(groups[root], key=_sample_sort_key))
        generations = {sample[1] for sample in samples}
        generation = next(iter(generations)) if len(generations) == 1 else None
        component_confirmed = tuple(sample for sample in samples if sample in fixed)
        component_undetermined = tuple(sample for sample in samples if sample not in fixed)
        component_conflicts = tuple(
            conflict
            for conflict in conflicts
            if conflict.samples and all(sample in samples for sample in conflict.samples)
        )
        components.append(
            Component(
                samples,
                generation,
                component_confirmed,
                component_undetermined,
                component_conflicts,
            )
        )

    return ReconciliationResult(
        confirmed={sample: fixed[sample] for sample in sorted(fixed, key=_sample_sort_key)},
        evidence={sample: evidence[sample] for sample in sorted(evidence, key=_sample_sort_key)},
        undetermined=tuple(undetermined),
        expressions={
            sample: expression
            for sample, expression in sorted(
                expressions.items(), key=lambda item: _sample_sort_key(item[0])
            )
        },
        conflicts=tuple(conflicts),
        relations=tuple(accepted_relations),
        observations=tuple(observations[index] for index in sorted(observations)),
        rejected=tuple(rejected),
        components=tuple(components),
        duplicate_relations=duplicate_relations,
        duplicate_anchors=duplicate_anchors,
        conflicted=tuple(sorted(conflicted_samples, key=_sample_sort_key)),
    )


resolve = reconcile
reconstruct = reconcile
reconcile_measurements = reconcile


def decode_measurements(
    data: Any,
    *,
    strict: bool = False,
    allow_cross_generation: bool = False,
) -> ReconciliationResult:
    """Decode CBOR bytes and immediately reconcile the contained records."""

    return reconcile(
        data, strict=strict, allow_cross_generation=allow_cross_generation
    )


reconcile_cbor = decode_measurements
load_measurements = decode_measurements


def measurement_tag_hook(decoder: Any, tag: CBORTag) -> Any:
    """A decoder ``tag_hook`` that preserves custom measurement tags.

    Returning the :class:`CBORTag` keeps its number and payload so that
    :func:`parse_records` can classify the record by structure.
    """

    return tag


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m cbor2.measurements",
        description="Reconcile compressed CBOR sensor measurements",
    )
    parser.add_argument("infile", nargs="?", default="-", help="CBOR input file (default: stdin)")
    parser.add_argument("-o", "--outfile", default="-", help="JSON output file (default: stdout)")
    parser.add_argument("--pretty", action="store_true", help="indent the JSON output")
    parser.add_argument(
        "--allow-cross-generation",
        action="store_true",
        help="allow relations between different restart generations",
    )
    options = parser.parse_args(argv)

    if options.infile == "-":
        data = sys.stdin.buffer.read()
    else:
        with open(options.infile, "rb") as fp:
            data = fp.read()

    try:
        result = decode_measurements(
            data, allow_cross_generation=options.allow_cross_generation
        )
    except CBORDecodeError as exc:
        parser.error(str(exc))
        return 2

    text = result.to_json(indent=4 if options.pretty else None) + "\n"
    if options.outfile == "-":
        sys.stdout.write(text)
    else:
        with open(options.outfile, "w", encoding="utf-8") as fp:
            fp.write(text)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
