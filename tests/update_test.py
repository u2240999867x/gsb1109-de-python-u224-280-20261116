# Copyright (C) 2025 Christian Ledermann
#
# This library is free software; you can redistribute it and/or modify it under
# the terms of the GNU Lesser General Public License as published by the Free
# Software Foundation; either version 2.1 of the License, or (at your option)
# any later version.
#
# This library is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU Lesser General Public License for more
# details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this library; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301 USA

"""Test applying ``Update`` operations to a loaded KML object tree."""

from fastkml import KML
from fastkml.containers import Document
from fastkml.containers import Folder
from fastkml.data import ExtendedData
from fastkml.data import Schema
from fastkml.data import SchemaData
from fastkml.data import SimpleData
from fastkml.enums import PairKey
from fastkml.features import Placemark
from fastkml.geometry import Coordinates
from fastkml.geometry import Point
from fastkml.network_link_control import Change
from fastkml.network_link_control import Create
from fastkml.network_link_control import Delete
from fastkml.network_link_control import NetworkLinkControl
from fastkml.network_link_control import Update
from fastkml.styles import Pair
from fastkml.styles import Style
from fastkml.styles import StyleMap
from fastkml.styles import StyleUrl
from fastkml.update import UpdateResult
from fastkml.update import apply_update
from fastkml.update import apply_updates
from tests.base import Lxml
from tests.base import StdLibrary

TARGET_HREF = "http://example.com/target.kml"


def _placemark(
    id: str,
    *,
    name: str | None = None,
    style_url: str | None = None,
) -> Placemark:
    return Placemark(
        id=id,
        name=name,
        style_url=StyleUrl(url=style_url) if style_url else None,
    )


def _document() -> Document:
    return Document(
        id="doc",
        styles=[
            Style(id="s1"),
            StyleMap(
                id="sm1",
                pairs=[Pair(key=PairKey.normal, style=StyleUrl(url="#s1"))],
            ),
        ],
        features=[
            Folder(
                id="f1",
                name="Folder One",
                features=[
                    _placemark("p1", name="One", style_url="#s1"),
                    Placemark(
                        id="p2",
                        name="Two",
                        kml_geometry=Point(
                            id="pt2",
                            kml_coordinates=Coordinates(coords=[(1.0, 2.0, 0.0)]),
                        ),
                    ),
                ],
            ),
        ],
    )


def _kml() -> KML:
    return KML(features=[_document()])


class TestStdLibrary(StdLibrary):
    """Test with the standard library."""

    def test_create_adds_children_to_target_container(self) -> None:
        kml = _kml()
        folder = kml.features[0].features[0]
        update = Update(
            target_href=TARGET_HREF,
            operations=[
                Create(
                    objects=[
                        Folder(
                            target_id="f1",
                            features=[_placemark("p3", name="Three")],
                        ),
                    ],
                ),
            ],
        )

        results = apply_update(kml, update)

        assert len(results) == 1
        result = results[0]
        assert result.operation == "create"
        assert result.target_id == "f1"
        assert result.applied is True
        assert result.reason is None
        assert [pm.id for pm in folder.features] == ["p1", "p2", "p3"]
        assert folder.features[2].name == "Three"

    def test_create_into_document(self) -> None:
        document = _document()
        update = Update(
            target_href=TARGET_HREF,
            operations=[
                Create(
                    objects=[
                        Document(
                            target_id="doc",
                            features=[_placemark("p9")],
                        ),
                    ],
                ),
            ],
        )

        results = apply_update(document, update)

        assert results[0].applied is True
        assert [feature.id for feature in document.features] == ["f1", "p9"]

    def test_create_unknown_target_does_not_create_stub(self) -> None:
        kml = _kml()
        update = Update(
            target_href=TARGET_HREF,
            operations=[
                Create(
                    objects=[
                        Folder(
                            target_id="missing",
                            features=[_placemark("p3")],
                        ),
                    ],
                ),
            ],
        )

        results = apply_update(kml, update)

        assert results[0].applied is False
        assert "missing" in results[0].reason
        folder = kml.features[0].features[0]
        assert [pm.id for pm in folder.features] == ["p1", "p2"]

    def test_create_target_that_is_not_a_container(self) -> None:
        kml = _kml()
        update = Update(
            target_href=TARGET_HREF,
            operations=[
                Create(
                    objects=[
                        Folder(
                            target_id="pt2",
                            features=[_placemark("p3")],
                        ),
                    ],
                ),
            ],
        )

        results = apply_update(kml, update)

        assert results[0].applied is False
        assert "not a container" in results[0].reason

    def test_create_without_target_id(self) -> None:
        kml = _kml()
        update = Update(
            target_href=TARGET_HREF,
            operations=[Create(objects=[Folder(features=[_placemark("p3")])])],
        )

        results = apply_update(kml, update)

        assert results[0].applied is False
        assert results[0].target_id == ""

    def test_create_replay_does_not_append_duplicate(self) -> None:
        kml = _kml()
        update = Update(
            target_href=TARGET_HREF,
            operations=[
                Create(
                    objects=[
                        Folder(
                            target_id="f1",
                            features=[_placemark("p3")],
                        ),
                    ],
                ),
            ],
        )

        first = apply_update(kml, update)
        second = apply_update(kml, update)

        assert first[0].applied is True
        assert second[0].applied is False
        assert "p3" in second[0].reason
        folder = kml.features[0].features[0]
        assert [pm.id for pm in folder.features] == ["p1", "p2", "p3"]

    def test_create_skips_duplicate_but_adds_new_sibling(self) -> None:
        kml = _kml()
        update = Update(
            target_href=TARGET_HREF,
            operations=[
                Create(
                    objects=[
                        Folder(
                            target_id="f1",
                            features=[_placemark("p1"), _placemark("p4")],
                        ),
                    ],
                ),
            ],
        )

        results = apply_update(kml, update)

        assert results[0].applied is True
        assert "p1" in results[0].reason
        folder = kml.features[0].features[0]
        assert [pm.id for pm in folder.features] == ["p1", "p2", "p4"]

    def test_change_is_sparse_and_preserves_other_fields(self) -> None:
        kml = _kml()
        update = Update(
            target_href=TARGET_HREF,
            operations=[
                Change(objects=[Placemark(target_id="p1", name="Renamed")]),
            ],
        )

        results = apply_update(kml, update)

        placemark = kml.features[0].features[0].features[0]
        assert results[0].applied is True
        assert results[0].object is placemark
        assert placemark.id == "p1"
        assert placemark.name == "Renamed"
        assert placemark.style_url.url == "#s1"

    def test_change_does_not_overwrite_id(self) -> None:
        kml = _kml()
        update = Update(
            target_href=TARGET_HREF,
            operations=[
                Change(
                    objects=[Placemark(target_id="p1", id="other", name="x")],
                ),
            ],
        )

        apply_update(kml, update)

        placemark = kml.features[0].features[0].features[0]
        assert placemark.id == "p1"
        assert placemark.name == "x"

    def test_change_replaces_nested_geometry_coordinates(self) -> None:
        kml = _kml()
        update = Update(
            target_href=TARGET_HREF,
            operations=[
                Change(
                    objects=[
                        Point(
                            target_id="pt2",
                            kml_coordinates=Coordinates(coords=[(9.0, 8.0, 0.0)]),
                        ),
                    ],
                ),
            ],
        )

        results = apply_update(kml, update)

        point = kml.features[0].features[0].features[1].kml_geometry
        assert results[0].applied is True
        assert point.kml_coordinates.coords == [(9.0, 8.0, 0.0)]

    def test_change_unknown_target_creates_nothing(self) -> None:
        kml = _kml()
        update = Update(
            target_href=TARGET_HREF,
            operations=[
                Change(objects=[Placemark(target_id="ghost", name="x")]),
            ],
        )

        results = apply_update(kml, update)

        assert results[0].applied is False
        assert "ghost" in results[0].reason
        assert results[0].object is not None
        assert all(
            placemark.id != "ghost"
            for placemark in kml.features[0].features[0].features
        )

    def test_change_with_wrong_element_type_is_rejected(self) -> None:
        kml = _kml()
        update = Update(
            target_href=TARGET_HREF,
            operations=[
                Change(objects=[Folder(target_id="p1", name="Wrong Type")]),
            ],
        )

        results = apply_update(kml, update)

        placemark = kml.features[0].features[0].features[0]
        assert results[0].applied is False
        assert "Placemark" in results[0].reason
        assert "Folder" in results[0].reason
        assert placemark.name == "One"

    def test_delete_removes_feature(self) -> None:
        kml = _kml()
        update = Update(
            target_href=TARGET_HREF,
            operations=[Delete(objects=[Placemark(target_id="p2")])],
        )

        results = apply_update(kml, update)

        assert results[0].applied is True
        assert results[0].target_id == "p2"
        folder = kml.features[0].features[0]
        assert [pm.id for pm in folder.features] == ["p1"]

    def test_delete_unknown_target_is_reported_not_created(self) -> None:
        kml = _kml()
        update = Update(
            target_href=TARGET_HREF,
            operations=[Delete(objects=[Placemark(target_id="ghost")])],
        )

        results = apply_update(kml, update)

        assert results[0].applied is False
        assert "ghost" in results[0].reason

    def test_delete_style_reports_dangling_feature_and_stylemap(self) -> None:
        kml = _kml()
        update = Update(
            target_href=TARGET_HREF,
            operations=[Delete(objects=[Style(target_id="s1")])],
        )

        results = apply_update(kml, update)

        assert results[0].applied is True
        assert results[0].affected_references == frozenset({"p1", "sm1"})
        assert [style.id for style in kml.features[0].styles] == ["sm1"]

    def test_delete_parent_reports_dangling_reference_outside_subtree(self) -> None:
        document = Document(
            id="doc",
            features=[
                Folder(id="f1", features=[Style(id="s1")]),
                _placemark("p9", style_url="#s1"),
            ],
        )
        update = Update(
            target_href=TARGET_HREF,
            operations=[Delete(objects=[Folder(target_id="f1")])],
        )

        results = apply_update(KML(features=[document]), update)

        assert results[0].applied is True
        assert results[0].affected_references == frozenset({"p9"})
        assert [feature.id for feature in document.features] == ["p9"]

    def test_delete_parent_ignores_references_inside_removed_subtree(self) -> None:
        kml = _kml()
        update = Update(
            target_href=TARGET_HREF,
            operations=[Delete(objects=[Folder(target_id="f1")])],
        )

        results = apply_update(kml, update)

        assert results[0].applied is True
        # p1's styleUrl points at s1, but both p1 and its folder are removed;
        # s1 itself stays in the document so nothing is left dangling.
        assert results[0].affected_references == frozenset()
        assert kml.features[0].features == []

    def test_delete_feature_with_internal_schema_data_reports_no_referrer(self) -> None:
        document = Document(
            id="doc",
            schemata=[Schema(id="sc1", name="TrailHeadType")],
            features=[
                Placemark(
                    id="p1",
                    extended_data=ExtendedData(
                        elements=[
                            SchemaData(
                                schema_url="#sc1",
                                data=[SimpleData(name="n", value="v")],
                            ),
                        ],
                    ),
                ),
            ],
        )
        update = Update(
            target_href=TARGET_HREF,
            operations=[Delete(objects=[Placemark(target_id="p1")])],
        )

        results = apply_update(KML(features=[document]), update)

        assert results[0].applied is True
        # The SchemaData referencing sc1 is removed together with the placemark,
        # so no surviving object is left dangling.
        assert results[0].affected_references == frozenset()
        assert document.features == []

    def test_operations_are_applied_in_document_order(self) -> None:
        kml = _kml()
        update = Update(
            target_href=TARGET_HREF,
            operations=[
                Delete(objects=[Placemark(target_id="p2")]),
                Create(
                    objects=[
                        Folder(
                            target_id="f1",
                            features=[_placemark("p3")],
                        ),
                    ],
                ),
                Change(objects=[Placemark(target_id="p1", name="Renamed")]),
            ],
        )

        results = apply_update(kml, update)

        assert [result.operation for result in results] == [
            "delete",
            "create",
            "change",
        ]
        folder = kml.features[0].features[0]
        assert [pm.id for pm in folder.features] == ["p1", "p3"]
        assert folder.features[0].name == "Renamed"

    def test_apply_updates_chains_updates_and_skips_empty_ones(self) -> None:
        kml = _kml()
        updates = [
            None,
            Update(),
            Update(
                target_href=TARGET_HREF,
                operations=[
                    Create(
                        objects=[
                            Folder(
                                target_id="f1",
                                features=[_placemark("p3")],
                            ),
                        ],
                    ),
                ],
            ),
            Update(
                target_href=TARGET_HREF,
                operations=[
                    Change(objects=[Placemark(target_id="p3", name="Three")]),
                ],
            ),
        ]

        results = apply_updates(kml, updates)

        assert len(results) == 2
        folder = kml.features[0].features[0]
        assert [pm.id for pm in folder.features] == ["p1", "p2", "p3"]
        assert folder.features[2].name == "Three"

    def test_apply_updates_can_delete_object_created_by_earlier_update(self) -> None:
        kml = _kml()
        create = Update(
            target_href=TARGET_HREF,
            operations=[
                Create(
                    objects=[
                        Folder(
                            target_id="f1",
                            features=[_placemark("p3")],
                        ),
                    ],
                ),
            ],
        )
        delete = Update(
            target_href=TARGET_HREF,
            operations=[Delete(objects=[Placemark(target_id="p3")])],
        )

        apply_updates(kml, [create, delete])

        folder = kml.features[0].features[0]
        assert [pm.id for pm in folder.features] == ["p1", "p2"]

    def test_update_result_is_truthy_only_when_applied(self) -> None:
        kml = _kml()
        update = Update(
            target_href=TARGET_HREF,
            operations=[
                Create(
                    objects=[
                        Folder(
                            target_id="missing",
                            features=[_placemark("p3")],
                        ),
                    ],
                ),
            ],
        )

        result = apply_update(kml, update)[0]

        assert isinstance(result, UpdateResult)
        assert bool(result) is False

    def test_result_records_the_source_target_href(self) -> None:
        kml = _kml()
        update = Update(
            target_href=TARGET_HREF,
            operations=[
                Change(objects=[Placemark(target_id="p1", name="Renamed")]),
            ],
        )

        results = apply_update(kml, update)

        assert results[0].target_href == TARGET_HREF

    def test_parsed_update_can_be_applied_to_object_tree(self) -> None:
        kml = _kml()
        nlc = NetworkLinkControl.from_string(
            f"""
            <kml:NetworkLinkControl xmlns:kml="http://www.opengis.net/kml/2.2">
              <kml:Update>
                <kml:targetHref>{TARGET_HREF}</kml:targetHref>
                <kml:Create>
                  <kml:Folder targetId="f1">
                    <kml:Placemark id="p3"><kml:name>Three</kml:name></kml:Placemark>
                  </kml:Folder>
                </kml:Create>
              </kml:Update>
            </kml:NetworkLinkControl>
            """,
        )

        results = apply_update(kml, nlc.update)

        assert results[0].applied is True
        folder = kml.features[0].features[0]
        assert [pm.id for pm in folder.features] == ["p1", "p2", "p3"]
        assert folder.features[2].name == "Three"


class TestLxml(Lxml, TestStdLibrary):
    """Test with lxml."""
