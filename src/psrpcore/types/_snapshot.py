# -*- coding: utf-8 -*-
# Copyright: (c) 2021, Jordan Borean (@jborean93) <jborean93@gmail.com>
# MIT License (see LICENSE or https://opensource.org/licenses/MIT)

"""Structural snapshot of a remote PSObject.

A remote object carries two independent kinds of property declarations:

* Adapted properties - the native members of the underlying .NET type,
  serialized under ``<Props>``.
* Extended properties - the members added on top by the PowerShell Extended
  Type System, serialized under ``<MS>``.

A property with the same name may exist in both groups and the two are not the
same thing. Reading a property can also fail at runtime; a failed read is not
the same as the property being absent.

This module provides a read only "warehousing" entrypoint,
:func:`snapshot_object`, that captures both declarations, the full type name
chain and the effective value of every property under a supplied
:class:`PropertyQueryPolicy`. The original object is never mutated and values
that are themselves objects are kept as references rather than copied. A
snapshot can be re-exported as a fresh projection through
:meth:`PSObjectSnapshot.to_projection` without touching the source object.
"""

import typing

from psrpcore.types._base import PSNoteProperty, PSObject, PSPropertyInfo

#: When a property read fails record the failure and use ``None`` as the
#: effective value so the property declaration is still represented.
ON_READ_ERROR_NIL = "nil"

#: When a property read fails record the failure and leave the effective value
#: unset; the declaration is retained but no value is produced for it.
ON_READ_ERROR_OMIT = "omit"

#: When a property read fails propagate the exception to the caller.
ON_READ_ERROR_RAISE = "raise"

_VALID_ON_READ_ERROR = {ON_READ_ERROR_NIL, ON_READ_ERROR_OMIT, ON_READ_ERROR_RAISE}


class PropertyQueryPolicy:
    """Policy that governs how property values are read for a snapshot.

    The policy only affects how the *effective value* of a declaration is
    computed; both the adapted and the extended property declarations are
    always captured even when reading their values is disabled or fails.

    Args:
        read_adapted: Whether to read the effective value of adapted
            properties. When ``False`` the declarations are still stored but no
            value is read.
        read_extended: Whether to read the effective value of extended
            properties. When ``False`` the declarations are still stored but no
            value is read.
        on_read_error: How to handle a property whose getter raises. One of
            :data:`ON_READ_ERROR_NIL`, :data:`ON_READ_ERROR_OMIT` or
            :data:`ON_READ_ERROR_RAISE`.
    """

    def __init__(
        self,
        *,
        read_adapted: bool = True,
        read_extended: bool = True,
        on_read_error: str = ON_READ_ERROR_NIL,
    ) -> None:
        if on_read_error not in _VALID_ON_READ_ERROR:
            raise ValueError(
                f"Invalid on_read_error '{on_read_error}', expect one of: {', '.join(sorted(_VALID_ON_READ_ERROR))}"
            )

        self.read_adapted = read_adapted
        self.read_extended = read_extended
        self.on_read_error = on_read_error


class PropertyReadFailure:
    """Records a property value that could not be read.

    A failure is kept alongside the declaration so the consumer can tell the
    difference between a property that does not exist and one that existed but
    raised when its value was requested.

    Args:
        name: The property name.
        group: Whether it was an ``adapted`` or ``extended`` property.
        error: The exception raised while reading the value.
    """

    def __init__(self, name: str, group: str, error: BaseException) -> None:
        self.name = name
        self.group = group
        self.error = error

    def __repr__(self) -> str:
        return f"PropertyReadFailure(name={self.name!r}, group={self.group!r}, error={self.error!r})"


class PropertySnapshot:
    """A single property declaration and its effective value.

    Args:
        declaration: The original :class:`PSPropertyInfo` metadata, kept as a
            reference.
        group: Whether this is an ``adapted`` or ``extended`` property.
        has_value: Whether an effective value is available. This is ``False``
            when reading was disabled by the policy or a failed read was
            omitted, in which case ``effective_value`` is always ``None``.
        effective_value: The value under the policy, or ``None`` when no value
            is available. Object values are stored as references.
        failure: The :class:`PropertyReadFailure` when the read failed,
            otherwise ``None``.
    """

    def __init__(
        self,
        declaration: PSPropertyInfo,
        group: str,
        has_value: bool,
        effective_value: typing.Any = None,
        failure: typing.Optional[PropertyReadFailure] = None,
    ) -> None:
        self.declaration = declaration
        self.group = group
        self.has_value = has_value
        self.effective_value = effective_value
        self.failure = failure

    @property
    def name(self) -> str:
        """The declared property name."""
        return self.declaration.name

    @property
    def readable(self) -> bool:
        """Whether the value was read without error."""
        return self.failure is None and self.has_value

    def __repr__(self) -> str:
        return (
            f"PropertySnapshot(name={self.name!r}, group={self.group!r}, has_value={self.has_value!r}, "
            f"effective_value={self.effective_value!r})"
        )


class PSObjectSnapshot:
    """A read only structural snapshot of a :class:`PSObject`.

    The two property groups are stored independently so an adapted and an
    extended property sharing a name are never collapsed into one. The source
    object is retained as a reference and object valued properties keep their
    original references; nothing is deep copied and the source is never
    mutated.

    Args:
        source: The PSObject the snapshot was built from.
        type_names: A copy of the source type name chain.
        adapted_properties: The adapted property snapshots.
        extended_properties: The extended property snapshots.
        to_string: The explicit ``<ToString>`` value of the source, if any.
    """

    def __init__(
        self,
        source: PSObject,
        type_names: typing.List[str],
        adapted_properties: typing.List[PropertySnapshot],
        extended_properties: typing.List[PropertySnapshot],
        to_string: typing.Optional[str] = None,
    ) -> None:
        self.source = source
        self.type_names = type_names
        self.adapted_properties = adapted_properties
        self.extended_properties = extended_properties
        self.to_string = to_string

    def to_projection(self) -> PSObject:
        """Build a fresh, re-exportable projection of the snapshot.

        A new :class:`PSObject` is created with the adapted property values in
        the adapted group (``<Props>``) and the extended property values in the
        extended group (``<MS>``). Object valued properties keep referencing the
        same shared objects. The source object and its metadata are not
        modified.

        Only properties that have an effective value (including a failed read
        handled by the nil policy) are projected; declarations whose reads were
        disabled or omitted are skipped but remain available on the snapshot.

        Returns:
            PSObject: A new PSObject that serializes with the two groups kept
            separate.
        """
        projection = PSObject()
        meta = projection.PSObject
        meta.type_names = list(self.type_names)

        if self.to_string is not None:
            meta.to_string = self.to_string

        meta.adapted_properties = [
            PSNoteProperty(snapshot.name, snapshot.effective_value)
            for snapshot in self.adapted_properties
            if snapshot.has_value
        ]
        meta.extended_properties = [
            PSNoteProperty(snapshot.name, snapshot.effective_value)
            for snapshot in self.extended_properties
            if snapshot.has_value
        ]

        return projection


def snapshot_object(
    value: PSObject,
    policy: typing.Optional[PropertyQueryPolicy] = None,
) -> PSObjectSnapshot:
    """Capture a structural snapshot of a remote PSObject.

    This is the warehousing entrypoint. It records the adapted and extended
    property declarations separately, copies the type name chain and resolves
    the effective value of every property according to ``policy``.

    A property whose getter raises is governed by the policy but its
    declaration is always retained, so a read failure is never confused with a
    missing property. The source object is not mutated and any object returned
    by a property is stored as a reference rather than copied.

    Args:
        value: The PSObject to snapshot.
        policy: The query policy controlling value reads. A default policy is
            used when omitted.

    Returns:
        PSObjectSnapshot: The structural snapshot.
    """
    if not isinstance(value, PSObject):
        raise TypeError(f"Can only snapshot a PSObject, got {type(value).__name__}")

    policy = policy or PropertyQueryPolicy()
    meta = value.PSObject

    adapted = _capture_group(value, meta.adapted_properties, "adapted", policy.read_adapted, policy)
    extended = _capture_group(value, meta.extended_properties, "extended", policy.read_extended, policy)

    return PSObjectSnapshot(
        source=value,
        type_names=list(meta.type_names),
        adapted_properties=adapted,
        extended_properties=extended,
        to_string=meta.to_string,
    )


def _capture_group(
    value: PSObject,
    declarations: typing.List[PSPropertyInfo],
    group: str,
    should_read: bool,
    policy: PropertyQueryPolicy,
) -> typing.List[PropertySnapshot]:
    snapshots: typing.List[PropertySnapshot] = []

    for declaration in declarations:
        if not should_read:
            # Keep the declaration but do not touch the getter.
            snapshots.append(PropertySnapshot(declaration, group, has_value=False))
            continue

        try:
            effective_value = declaration.get_value(value)
        except Exception as e:
            failure = PropertyReadFailure(declaration.name, group, e)

            if policy.on_read_error == ON_READ_ERROR_RAISE:
                raise

            if policy.on_read_error == ON_READ_ERROR_OMIT:
                snapshots.append(PropertySnapshot(declaration, group, has_value=False, failure=failure))
            else:
                snapshots.append(
                    PropertySnapshot(declaration, group, has_value=True, effective_value=None, failure=failure)
                )
        else:
            snapshots.append(PropertySnapshot(declaration, group, has_value=True, effective_value=effective_value))

    return snapshots
