# -*- coding: utf-8 -*-
# Copyright: (c) 2021, Jordan Borean (@jborean93) <jborean93@gmail.com>
# MIT License (see LICENSE or https://opensource.org/licenses/MIT)

import xml.etree.ElementTree as ElementTree

import pytest

from psrpcore.types import (
    ON_READ_ERROR_NIL,
    ON_READ_ERROR_OMIT,
    ON_READ_ERROR_RAISE,
    PSCustomObject,
    PSNoteProperty,
    PSObject,
    PropertyQueryPolicy,
    PSObjectSnapshot,
    PSScriptProperty,
    PSString,
    PSType,
    snapshot_object,
)

from ..conftest import deserialize, serialize


def _boom_getter(obj: PSObject) -> None:
    raise RuntimeError("getter failed")


@PSType(
    type_names=["Snapshot.Widget"],
    adapted_properties=[
        PSNoteProperty("Name", ps_type=PSString),
        PSNoteProperty("OnlyAdapted"),
    ],
    extended_properties=[
        PSNoteProperty("Name"),
        PSNoteProperty("OnlyExtended"),
    ],
)
class ShadowWidget(PSObject):
    pass


@PSType(
    type_names=["Snapshot.Boom"],
    adapted_properties=[
        PSNoteProperty("Good", value="good"),
        PSScriptProperty("Boom", _boom_getter),
    ],
    extended_properties=[PSNoteProperty("Extra", value="extra")],
)
class BoomWidget(PSObject):
    pass


def test_snapshot_captures_both_groups_and_type_names():
    obj = ShadowWidget()
    obj.PSObject.adapted_properties[0].set_value("adapted", obj)
    obj.PSObject.extended_properties[0].set_value("extended", obj)

    snap = snapshot_object(obj)

    assert isinstance(snap, PSObjectSnapshot)
    assert snap.source is obj
    # The type name chain is copied, mutating the snapshot must not alter the source.
    assert snap.type_names == ["Snapshot.Widget", "System.Object"]
    snap.type_names.append("Other.Type")
    assert obj.PSTypeNames == ["Snapshot.Widget", "System.Object"]

    assert [p.name for p in snap.adapted_properties] == ["Name", "OnlyAdapted"]
    assert [p.name for p in snap.extended_properties] == ["Name", "OnlyExtended"]

    # Same name in both groups are two independent declarations and values.
    adapted_name = snap.adapted_properties[0]
    extended_name = snap.extended_properties[0]
    assert adapted_name is not extended_name
    assert adapted_name.effective_value == "adapted"
    assert extended_name.effective_value == "extended"


def test_snapshot_failed_read_is_not_missing_default_nil():
    snap = snapshot_object(BoomWidget())

    boom = next(p for p in snap.adapted_properties if p.name == "Boom")
    assert boom is not None  # The declaration is retained.
    assert boom.failure is not None
    assert isinstance(boom.failure.error, RuntimeError)
    assert boom.has_value is True
    assert boom.effective_value is None
    assert boom.readable is False

    good = next(p for p in snap.adapted_properties if p.name == "Good")
    assert good.readable is True
    assert good.effective_value == "good"


def test_snapshot_failed_read_omit_policy():
    snap = snapshot_object(BoomWidget(), PropertyQueryPolicy(on_read_error=ON_READ_ERROR_OMIT))

    boom = next(p for p in snap.adapted_properties if p.name == "Boom")
    assert boom.failure is not None
    assert boom.has_value is False
    assert boom.effective_value is None
    # The declaration still exists even though no value was produced.
    assert [p.name for p in snap.adapted_properties] == ["Good", "Boom"]


def test_snapshot_failed_read_raise_policy():
    with pytest.raises(RuntimeError, match="getter failed"):
        snapshot_object(BoomWidget(), PropertyQueryPolicy(on_read_error=ON_READ_ERROR_RAISE))


def test_snapshot_invalid_policy():
    with pytest.raises(ValueError, match="Invalid on_read_error"):
        PropertyQueryPolicy(on_read_error="bogus")


def test_snapshot_disabled_read_keeps_declarations():
    snap = snapshot_object(BoomWidget(), PropertyQueryPolicy(read_adapted=False))

    assert [p.name for p in snap.adapted_properties] == ["Good", "Boom"]
    assert all(not p.has_value for p in snap.adapted_properties)

    extra = next(p for p in snap.extended_properties if p.name == "Extra")
    assert extra.readable is True
    assert extra.effective_value == "extra"


def test_snapshot_keeps_shared_object_reference():
    shared = PSCustomObject(Inner="shared")
    parent = PSCustomObject(First=shared, Second=shared)

    snap = snapshot_object(parent)
    first = next(p for p in snap.extended_properties if p.name == "First")
    second = next(p for p in snap.extended_properties if p.name == "Second")

    assert first.effective_value is shared
    assert second.effective_value is shared


def test_snapshot_does_not_mutate_source():
    obj = BoomWidget()
    before_adapted = [type(p).__name__ for p in obj.PSObject.adapted_properties]
    before_extended = [type(p).__name__ for p in obj.PSObject.extended_properties]

    snapshot_object(obj)

    assert [type(p).__name__ for p in obj.PSObject.adapted_properties] == before_adapted
    assert [type(p).__name__ for p in obj.PSObject.extended_properties] == before_extended


def test_snapshot_rejects_non_psobject():
    with pytest.raises(TypeError, match="Can only snapshot a PSObject"):
        snapshot_object(object())  # type: ignore[arg-type]


def test_serializer_failed_property_read_emits_nil_in_original_group():
    element = serialize(BoomWidget())
    actual = ElementTree.tostring(element, encoding="unicode")

    # The failed adapted property stays in Props as Nil rather than aborting serialization.
    assert '<Props><S N="Good">good</S><Nil N="Boom" /></Props>' in actual
    assert '<MS><S N="Extra">extra</S></MS>' in actual


def test_projection_reexports_groups_and_does_not_mutate_source():
    obj = ShadowWidget()
    obj.PSObject.adapted_properties[0].set_value("adapted", obj)
    obj.PSObject.extended_properties[0].set_value("extended", obj)

    snap = snapshot_object(obj)
    projection = snap.to_projection()

    assert isinstance(projection, PSObject)
    assert projection is not obj
    # Source metadata is untouched by building a projection.
    assert obj.PSTypeNames == ["Snapshot.Widget", "System.Object"]
    assert [p.name for p in obj.PSObject.adapted_properties] == ["Name", "OnlyAdapted"]
    assert [p.name for p in obj.PSObject.extended_properties] == ["Name", "OnlyExtended"]

    actual = ElementTree.tostring(serialize(projection), encoding="unicode")
    assert "<T>Snapshot.Widget</T>" in actual
    assert '<Props><S N="Name">adapted</S><Nil N="OnlyAdapted" /></Props>' in actual
    assert '<MS><S N="Name">extended</S><Nil N="OnlyExtended" /></MS>' in actual


def test_projection_preserves_shared_reference():
    shared = PSCustomObject(Inner="shared")
    parent = PSCustomObject(First=shared, Second=shared)

    projection = snapshot_object(parent).to_projection()

    assert projection.First is shared
    assert projection.Second is shared


def test_projection_skips_omitted_values_but_keeps_nil_failures():
    omit_snap = snapshot_object(BoomWidget(), PropertyQueryPolicy(on_read_error=ON_READ_ERROR_OMIT))
    omit_projection = omit_snap.to_projection()
    omit_xml = ElementTree.tostring(serialize(omit_projection), encoding="unicode")
    assert 'N="Boom"' not in omit_xml
    assert '<S N="Good">good</S>' in omit_xml

    nil_snap = snapshot_object(BoomWidget(), PropertyQueryPolicy(on_read_error=ON_READ_ERROR_NIL))
    nil_projection = nil_snap.to_projection()
    nil_xml = ElementTree.tostring(serialize(nil_projection), encoding="unicode")
    assert '<Nil N="Boom" />' in nil_xml


def test_unknown_deserialized_type_skips_local_constructor_and_keeps_groups():
    constructed = []

    @PSType(type_names=["Snapshot.LocalOnly"], rehydrate=False)
    class LocalOnly(PSObject):
        def __init__(self, *args, **kwargs):
            constructed.append("init")
            super().__init__(*args, **kwargs)

    clixml = (
        '<Obj RefId="0"><TN RefId="0"><T>Snapshot.LocalOnly</T><T>System.Object</T></TN>'
        '<Props><S N="Native">n</S></Props><MS><S N="Extra">x</S></MS></Obj>'
    )

    value = deserialize(ElementTree.fromstring(clixml))

    assert type(value) is PSObject
    assert constructed == []
    assert value.PSTypeNames == ["Deserialized.Snapshot.LocalOnly", "Deserialized.System.Object"]

    snap = snapshot_object(value)
    assert [p.name for p in snap.adapted_properties] == ["Native"]
    assert [p.name for p in snap.extended_properties] == ["Extra"]

    # Re-export keeps the two declarations in their original groups.
    reexport = ElementTree.tostring(serialize(snap.to_projection()), encoding="unicode")
    assert '<Props><S N="Native">n</S></Props>' in reexport
    assert '<MS><S N="Extra">x</S></MS>' in reexport
