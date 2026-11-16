# Copyright (C) 2026 Christian Ledermann
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

"""Test style resolution and per-attribute provenance."""

import pytest

from fastkml import containers
from fastkml import features
from fastkml import kml
from fastkml import links
from fastkml import style_catalog
from fastkml import styles
from fastkml.enums import PairKey
from tests.base import Lxml
from tests.base import StdLibrary


def _style_map_with_normal_and_highlight() -> tuple[
    styles.Style,
    styles.Style,
    styles.StyleMap,
]:
    normal = styles.Style(
        id="normal-style",
        styles=[
            styles.IconStyle(color="ff0000ff", scale=1.0),
            styles.LineStyle(color="ff111111", width=2.0),
        ],
    )
    highlight = styles.Style(
        id="highlight-style",
        styles=[
            styles.IconStyle(color="ff00ff00"),
            styles.LineStyle(width=4.0),
        ],
    )
    style_map = styles.StyleMap(
        id="state-map",
        pairs=[
            styles.Pair(
                key=PairKey.normal,
                style=styles.StyleUrl(url="#normal-style"),
            ),
            styles.Pair(
                key=PairKey.highlight,
                style=styles.StyleUrl(url="#highlight-style"),
            ),
        ],
    )
    return normal, highlight, style_map


class TestStdLibrary(StdLibrary):
    """Test with the standard library."""

    def test_resolves_normal_and_highlight(self) -> None:
        normal, highlight, style_map = _style_map_with_normal_and_highlight()
        document = containers.Document(
            id="doc",
            styles=[normal, highlight, style_map],
        )
        placemark = features.Placemark(
            id="pm",
            style_url=styles.StyleUrl(url="#state-map"),
        )
        document.append(placemark)

        catalog = style_catalog.StyleCatalog(document)

        assert catalog[placemark].normal["IconStyle.color"] == "ff0000ff"
        assert catalog[placemark].normal["LineStyle.width"] == 2.0
        assert catalog[placemark].highlight["IconStyle.color"] == "ff00ff00"
        assert catalog[placemark].highlight["LineStyle.width"] == 4.0

    def test_normal_and_highlight_keep_separate_provenance(self) -> None:
        normal, highlight, style_map = _style_map_with_normal_and_highlight()
        document = containers.Document(
            id="doc",
            styles=[normal, highlight, style_map],
        )
        placemark = features.Placemark(
            id="pm",
            style_url=styles.StyleUrl(url="#state-map"),
        )
        document.append(placemark)

        entry = style_catalog.StyleCatalog(document)[placemark]

        normal_source = entry.normal.source("IconStyle", "color")
        highlight_source = entry.highlight.source("IconStyle", "color")
        assert normal_source is not None
        assert highlight_source is not None
        assert normal_source.style_id == "normal-style"
        assert highlight_source.style_id == "highlight-style"
        assert normal_source.mode is PairKey.normal
        assert highlight_source.mode is PairKey.highlight
        assert normal_source.style_map_id == "state-map"
        assert highlight_source.style_map_id == "state-map"

    def test_inline_style_overrides_per_attribute(self) -> None:
        normal, highlight, style_map = _style_map_with_normal_and_highlight()
        document = containers.Document(
            id="doc",
            styles=[normal, highlight, style_map],
        )
        placemark = features.Placemark(
            id="pm",
            style_url=styles.StyleUrl(url="#state-map"),
            styles=[
                styles.Style(
                    styles=[styles.IconStyle(scale=3.0)],
                ),
            ],
        )
        document.append(placemark)

        entry = style_catalog.StyleCatalog(document)[placemark]

        assert entry.normal["IconStyle.color"] == "ff0000ff"
        assert entry.normal["IconStyle.scale"] == 3.0
        assert entry.normal.source("IconStyle", "color").kind == "shared"
        assert entry.normal.source("IconStyle", "scale").kind == "inline"
        assert entry.normal.source("IconStyle", "color").style_id == "normal-style"
        # The inline override applies to both states without changing where the
        # inherited highlight values came from.
        assert entry.highlight["IconStyle.scale"] == 3.0
        assert entry.highlight["IconStyle.color"] == "ff00ff00"
        assert entry.highlight.source("IconStyle", "color").kind == "shared"

    def test_inline_style_without_style_url(self) -> None:
        placemark = features.Placemark(
            name="inline only",
            styles=[
                styles.Style(
                    styles=[
                        styles.PolyStyle(color="7f0000ff", fill=False),
                    ],
                ),
            ],
        )

        entry = style_catalog.StyleCatalog(placemark)[placemark]

        assert entry.normal["PolyStyle.color"] == "7f0000ff"
        assert entry.normal["PolyStyle.fill"] is False
        assert entry.normal.source("PolyStyle", "color").kind == "inline"
        # An inline Style is mode-independent: it applies to both states.
        assert entry.highlight["PolyStyle.color"] == "7f0000ff"
        assert entry.highlight.source("PolyStyle", "color").kind == "inline"

    def test_nested_style_map(self) -> None:
        document = containers.Document(
            id="doc",
            styles=[
                styles.Style(
                    id="deep",
                    styles=[styles.LabelStyle(color="ffabcdef", scale=1.5)],
                ),
                styles.StyleMap(
                    id="outer",
                    pairs=[
                        styles.Pair(
                            key=PairKey.normal,
                            style=styles.StyleMap(
                                id="inner",
                                pairs=[
                                    styles.Pair(
                                        key=PairKey.normal,
                                        style=styles.StyleUrl(url="#deep"),
                                    ),
                                ],
                            ),
                        ),
                    ],
                ),
            ],
        )
        placemark = features.Placemark(
            id="pm",
            style_url=styles.StyleUrl(url="#outer"),
        )
        document.append(placemark)

        entry = style_catalog.StyleCatalog(document)[placemark]

        assert entry.normal["LabelStyle.color"] == "ffabcdef"
        source = entry.normal.source("LabelStyle", "color")
        assert source is not None
        assert source.kind == "shared"
        assert source.style_id == "deep"
        assert source.style_map_id == "inner"
        assert source.url == "#deep"
        assert source.chain == (
            "#pm",
            "#outer",
            "normal pair",
            "#inner",
            "normal pair",
            "#deep",
        )

    def test_nested_style_map_selected_by_outer_mode(self) -> None:
        document = containers.Document(
            id="doc",
            styles=[
                styles.Style(
                    id="norm",
                    styles=[styles.IconStyle(color="ff111111")],
                ),
                styles.Style(
                    id="high",
                    styles=[styles.IconStyle(color="ff222222")],
                ),
                styles.StyleMap(
                    id="states",
                    pairs=[
                        styles.Pair(
                            key=PairKey.normal,
                            style=styles.StyleUrl(url="#norm"),
                        ),
                        styles.Pair(
                            key=PairKey.highlight,
                            style=styles.StyleUrl(url="#high"),
                        ),
                    ],
                ),
            ],
        )
        placemark = features.Placemark(
            id="pm",
            style_url=styles.StyleUrl(url="#states"),
        )
        document.append(placemark)

        entry = style_catalog.StyleCatalog(document)[placemark]

        assert entry.normal["IconStyle.color"] == "ff111111"
        assert entry.highlight["IconStyle.color"] == "ff222222"

    def test_style_map_without_highlight_falls_back_to_normal(self) -> None:
        document = containers.Document(
            id="doc",
            styles=[
                styles.Style(
                    id="only-normal",
                    styles=[styles.IconStyle(color="ff333333")],
                ),
                styles.StyleMap(
                    id="states",
                    pairs=[
                        styles.Pair(
                            key=PairKey.normal,
                            style=styles.StyleUrl(url="#only-normal"),
                        ),
                    ],
                ),
            ],
        )
        placemark = features.Placemark(
            id="pm",
            style_url=styles.StyleUrl(url="#states"),
        )
        document.append(placemark)

        entry = style_catalog.StyleCatalog(document)[placemark]

        assert entry.highlight["IconStyle.color"] == "ff333333"
        assert entry.highlight.fallback_used
        assert entry.highlight.source("IconStyle", "color").mode is PairKey.normal

    def test_nested_icon_attributes_have_provenance(self) -> None:
        document = containers.Document(
            id="doc",
            styles=[
                styles.Style(
                    id="icons",
                    styles=[
                        styles.IconStyle(
                            scale=2.0,
                            icon=links.Icon(href="https://example.com/a.png"),
                            hot_spot=styles.HotSpot(x=0.5, y=0.5),
                        ),
                    ],
                ),
            ],
        )
        placemark = features.Placemark(
            id="pm",
            style_url=styles.StyleUrl(url="#icons"),
        )
        document.append(placemark)

        entry = style_catalog.StyleCatalog(document)[placemark]

        assert entry.normal["IconStyle.icon.href"] == "https://example.com/a.png"
        assert entry.normal["IconStyle.hot_spot.x"] == 0.5
        assert entry.normal.source("IconStyle", "icon.href").style_id == "icons"
        style = entry.normal.to_style()
        icon_style = style.styles[0]
        assert icon_style.icon.href == "https://example.com/a.png"
        assert icon_style.hot_spot.x == 0.5

    def test_cycles_reported_separately(self) -> None:
        document = containers.Document(
            id="doc",
            styles=[
                styles.Style(
                    id="leaf",
                    styles=[styles.IconStyle(color="ff444444")],
                ),
                styles.StyleMap(
                    id="a",
                    pairs=[
                        styles.Pair(
                            key=PairKey.normal,
                            style=styles.StyleUrl(url="#b"),
                        ),
                    ],
                ),
                styles.StyleMap(
                    id="b",
                    pairs=[
                        styles.Pair(
                            key=PairKey.normal,
                            style=styles.StyleUrl(url="#a"),
                        ),
                    ],
                ),
            ],
        )
        placemark = features.Placemark(
            id="pm",
            style_url=styles.StyleUrl(url="#a"),
        )
        document.append(placemark)

        catalog = style_catalog.StyleCatalog(document)

        assert catalog.cycles
        assert catalog.missing == []
        cycle = catalog.cycles[0]
        assert isinstance(cycle, style_catalog.StyleCycle)
        assert cycle.feature_id == "pm"
        assert cycle.document_id == "doc"
        assert cycle.chain[-1] == "#a"
        assert "cycle" in cycle.message.lower()
        assert catalog[placemark].normal.attributes == {}

    def test_missing_target_reported_separately(self) -> None:
        document = containers.Document(id="doc")
        placemark = features.Placemark(
            id="pm",
            style_url=styles.StyleUrl(url="#does-not-exist"),
            styles=[styles.Style(styles=[styles.IconStyle(color="ff555555")])],
        )
        document.append(placemark)

        catalog = style_catalog.StyleCatalog(document)

        assert catalog.cycles == []
        assert len(catalog.missing) == 1
        missing = catalog.missing[0]
        assert isinstance(missing, style_catalog.MissingStyleTarget)
        assert missing.url == "#does-not-exist"
        assert missing.target == "does-not-exist"
        assert missing.feature_id == "pm"
        # Inline values still resolve after the broken shared reference.
        assert catalog[placemark].normal["IconStyle.color"] == "ff555555"

    def test_missing_nested_pair_target(self) -> None:
        document = containers.Document(
            id="doc",
            styles=[
                styles.StyleMap(
                    id="states",
                    pairs=[
                        styles.Pair(
                            key=PairKey.normal,
                            style=styles.StyleUrl(url="#absent"),
                        ),
                    ],
                ),
            ],
        )
        placemark = features.Placemark(
            id="pm",
            style_url=styles.StyleUrl(url="#states"),
        )
        document.append(placemark)

        catalog = style_catalog.StyleCatalog(document)

        assert len(catalog.missing) == 1
        assert catalog.missing[0].source_style_map_id == "states"
        assert catalog.missing[0].url == "#absent"

    def test_same_style_id_in_different_documents_is_not_merged(self) -> None:
        first = containers.Document(
            id="doc-a",
            styles=[
                styles.Style(
                    id="shared",
                    styles=[styles.IconStyle(color="ff0000aa")],
                ),
            ],
        )
        first_placemark = features.Placemark(
            id="pm-a",
            style_url=styles.StyleUrl(url="#shared"),
        )
        first.append(first_placemark)

        second = containers.Document(
            id="doc-b",
            styles=[
                styles.Style(
                    id="shared",
                    styles=[styles.IconStyle(color="ff00bb00")],
                ),
            ],
        )
        second_placemark = features.Placemark(
            id="pm-b",
            style_url=styles.StyleUrl(url="#shared"),
        )
        second.append(second_placemark)

        catalog = style_catalog.StyleCatalog(kml.KML(features=[first, second]))

        assert catalog[first_placemark].normal["IconStyle.color"] == "ff0000aa"
        assert catalog[second_placemark].normal["IconStyle.color"] == "ff00bb00"
        first_source = catalog[first_placemark].normal.source("IconStyle", "color")
        second_source = catalog[second_placemark].normal.source("IconStyle", "color")
        assert first_source.document_id == "doc-a"
        assert second_source.document_id == "doc-b"

    def test_style_in_other_document_is_not_visible(self) -> None:
        first = containers.Document(
            id="doc-a",
            styles=[
                styles.Style(
                    id="shared",
                    styles=[styles.IconStyle(color="ff0000aa")],
                ),
            ],
        )
        second = containers.Document(id="doc-b")
        placemark = features.Placemark(
            id="pm-b",
            style_url=styles.StyleUrl(url="#shared"),
        )
        second.append(placemark)

        catalog = style_catalog.StyleCatalog(kml.KML(features=[first, second]))

        assert catalog[placemark].normal.attributes == {}
        assert len(catalog.missing) == 1
        assert catalog.missing[0].document_id == "doc-b"

    def test_highlight_resolution_does_not_mutate_originals(self) -> None:
        normal, highlight, style_map = _style_map_with_normal_and_highlight()
        document = containers.Document(
            id="doc",
            styles=[normal, highlight, style_map],
        )
        placemark = features.Placemark(
            id="pm",
            style_url=styles.StyleUrl(url="#state-map"),
        )
        document.append(placemark)

        catalog = style_catalog.StyleCatalog(document)
        before_normal = normal.styles[0].color
        before_highlight = highlight.styles[0].color
        before_placemark_styles = list(placemark.styles)

        _ = catalog[placemark].highlight
        _ = catalog.highlight_style(placemark)

        assert normal.styles[0].color == before_normal
        assert highlight.styles[0].color == before_highlight
        assert placemark.styles == before_placemark_styles
        assert catalog[placemark].normal["IconStyle.color"] == before_normal
        assert catalog[placemark].highlight["IconStyle.color"] == before_highlight

    def test_materialised_style_is_detached(self) -> None:
        normal, _, style_map = _style_map_with_normal_and_highlight()
        document = containers.Document(
            id="doc",
            styles=[normal, style_map],
        )
        placemark = features.Placemark(
            id="pm",
            style_url=styles.StyleUrl(url="#state-map"),
        )
        document.append(placemark)

        catalog = style_catalog.StyleCatalog(document)
        materialised = catalog.normal_style(placemark)
        materialised.styles[0].color = "ffffffff"

        assert catalog[placemark].normal["IconStyle.color"] == "ff0000ff"
        assert normal.styles[0].color == "ff0000ff"
        assert placemark.styles == []

    def test_rebuild_after_edit_sees_new_values(self) -> None:
        normal, _, style_map = _style_map_with_normal_and_highlight()
        document = containers.Document(
            id="doc",
            styles=[normal, style_map],
        )
        placemark = features.Placemark(
            id="pm",
            style_url=styles.StyleUrl(url="#state-map"),
        )
        document.append(placemark)

        first = style_catalog.StyleCatalog(document)
        normal.styles[0].color = "ff999999"
        second = style_catalog.StyleCatalog(document)

        assert first[placemark].normal["IconStyle.color"] == "ff0000ff"
        assert second[placemark].normal["IconStyle.color"] == "ff999999"

    def test_catalog_is_iterable_and_indexable(self) -> None:
        document = containers.Document(id="doc")
        first = features.Placemark(id="one")
        second = features.Placemark(id="two")
        document.append(first)
        document.append(second)

        catalog = style_catalog.StyleCatalog(document)

        assert len(catalog) == 2
        assert [entry.feature_id for entry in catalog] == ["one", "two"]
        assert catalog["two"].feature is second
        assert catalog[second].feature is second
        assert catalog[0].feature is first
        with pytest.raises(KeyError):
            _ = catalog["unknown"]

    def test_document_and_kml_helpers(self) -> None:
        normal, _, style_map = _style_map_with_normal_and_highlight()
        document = containers.Document(
            id="doc",
            styles=[normal, style_map],
        )
        placemark = features.Placemark(
            id="pm",
            style_url=styles.StyleUrl(url="#state-map"),
        )
        document.append(placemark)

        document_catalog = document.style_catalog()
        kml_catalog = kml.KML(features=[document]).style_catalog()

        assert document_catalog[placemark].normal["IconStyle.color"] == "ff0000ff"
        assert kml_catalog[placemark].normal["IconStyle.color"] == "ff0000ff"
        assert document.get_style_catalog()[placemark].feature is placemark

    def test_resolve_style_function(self) -> None:
        class KMLTree:
            def __init__(self, features: list[features.Placemark]) -> None:
                self.features = features

        placemark = features.Placemark(
            styles=[styles.Style(styles=[styles.IconStyle(color="ff777777")])],
        )
        document = containers.Document(features=[placemark])

        resolved = style_catalog.resolve_style(
            kml.KML(features=[document]),
            placemark,
            PairKey.normal,
        )

        assert resolved["IconStyle.color"] == "ff777777"
        assert resolved.source_for("IconStyle.color").kind == "inline"

    def test_unknown_mode_rejected(self) -> None:
        document = containers.Document(id="doc")
        placemark = features.Placemark(id="pm")
        document.append(placemark)
        catalog = style_catalog.StyleCatalog(document)

        with pytest.raises(ValueError, match="Unknown style mode"):
            catalog.resolve(placemark, "hover")

    def test_external_reference_is_reported_without_fetching(self) -> None:
        document = containers.Document(id="doc")
        placemark = features.Placemark(
            id="pm",
            style_url=styles.StyleUrl(url="https://example.com/other.kml#shared"),
        )
        document.append(placemark)

        catalog = style_catalog.StyleCatalog(document)

        assert catalog.missing == []
        assert catalog.cycles == []
        assert len(catalog.external) == 1
        assert isinstance(catalog.external[0], style_catalog.ExternalStyleReference)
        assert catalog.external[0].target == "shared"
        assert catalog[placemark].normal.attributes == {}

    def test_inline_style_map_defines_both_states(self) -> None:
        placemark = features.Placemark(
            id="pm",
            styles=[
                styles.StyleMap(
                    pairs=[
                        styles.Pair(
                            key=PairKey.normal,
                            style=styles.Style(
                                styles=[styles.IconStyle(color="ff010101")],
                            ),
                        ),
                        styles.Pair(
                            key=PairKey.highlight,
                            style=styles.Style(
                                styles=[styles.IconStyle(color="ff020202")],
                            ),
                        ),
                    ],
                ),
                styles.Style(styles=[styles.LineStyle(width=3.0)]),
            ],
        )
        document = containers.Document(id="doc")
        document.append(placemark)

        entry = style_catalog.StyleCatalog(document)[placemark]

        assert entry.normal["IconStyle.color"] == "ff010101"
        assert entry.highlight["IconStyle.color"] == "ff020202"
        assert entry.normal["LineStyle.width"] == 3.0
        assert entry.highlight["LineStyle.width"] == 3.0
        assert entry.normal.source("IconStyle", "color").kind == "inline"
        assert entry.highlight.source("IconStyle", "color").mode is PairKey.highlight

    def test_inline_style_overrides_inline_style_map(self) -> None:
        placemark = features.Placemark(
            id="pm",
            styles=[
                styles.StyleMap(
                    pairs=[
                        styles.Pair(
                            key=PairKey.normal,
                            style=styles.Style(
                                styles=[styles.IconStyle(color="ff111111", scale=2.0)],
                            ),
                        ),
                    ],
                ),
                styles.Style(styles=[styles.IconStyle(scale=5.0)]),
            ],
        )
        document = containers.Document(id="doc")
        document.append(placemark)

        entry = style_catalog.StyleCatalog(document)[placemark]

        assert entry.normal["IconStyle.color"] == "ff111111"
        assert entry.normal["IconStyle.scale"] == 5.0
        assert entry.normal.source("IconStyle", "scale").kind == "inline"
        # The fallback reused the normal pair for highlight; the inline Style
        # still overrides on top of it.
        assert entry.highlight["IconStyle.color"] == "ff111111"
        assert entry.highlight["IconStyle.scale"] == 5.0
        assert entry.highlight.fallback_used

    def test_materialised_style_serializes(self) -> None:
        document = containers.Document(
            id="doc",
            styles=[
                styles.Style(
                    id="normal",
                    styles=[
                        styles.IconStyle(
                            color="ff0000ff",
                            icon=links.Icon(href="https://example.com/a.png"),
                        ),
                    ],
                ),
                styles.Style(
                    id="highlight",
                    styles=[
                        styles.IconStyle(
                            color="ff00ff00",
                            icon=links.Icon(href="https://example.com/b.png"),
                        ),
                        styles.LineStyle(width=4.0),
                    ],
                ),
                styles.StyleMap(
                    id="state-map",
                    pairs=[
                        styles.Pair(
                            key=PairKey.normal,
                            style=styles.StyleUrl(url="#normal"),
                        ),
                        styles.Pair(
                            key=PairKey.highlight,
                            style=styles.StyleUrl(url="#highlight"),
                        ),
                    ],
                ),
            ],
        )
        placemark = features.Placemark(
            id="pm",
            style_url=styles.StyleUrl(url="#state-map"),
        )
        document.append(placemark)

        style = style_catalog.StyleCatalog(document).highlight_style(placemark)
        serialized = style.to_string()

        assert "<kml:IconStyle>" in serialized
        assert "<kml:color>ff00ff00</kml:color>" in serialized
        assert "https://example.com/b.png" in serialized
        assert "<kml:LineStyle>" in serialized
        assert "<kml:width>4.0</kml:width>" in serialized
        # Materialising must not mutate the source declarations.
        assert placemark.styles == []
        assert document.styles[0].styles[0].color == "ff0000ff"

    def test_entry_mode_helpers(self) -> None:
        normal, highlight, style_map = _style_map_with_normal_and_highlight()
        document = containers.Document(
            id="doc",
            styles=[normal, highlight, style_map],
        )
        placemark = features.Placemark(
            id="pm",
            style_url=styles.StyleUrl(url="#state-map"),
        )
        document.append(placemark)

        entry = style_catalog.StyleCatalog(document)[placemark]

        assert entry["normal"] is entry.normal
        assert entry[PairKey.highlight] is entry.highlight
        assert entry.resolve("highlight") is entry.highlight
        assert entry.normal_attributes["IconStyle.color"] == "ff0000ff"
        assert entry.highlight_attributes["IconStyle.color"] == "ff00ff00"
        assert entry.normal_sources["IconStyle.color"].style_id == "normal-style"
        assert entry.highlight_sources["IconStyle.color"].style_id == "highlight-style"
        assert entry.sources_for(PairKey.highlight)["IconStyle.color"].mode is (
            PairKey.highlight
        )

    def test_issue_serialization(self) -> None:
        document = containers.Document(id="doc")
        placemark = features.Placemark(
            id="pm",
            name="Broken",
            style_url=styles.StyleUrl(url="#absent"),
        )
        document.append(placemark)

        catalog = style_catalog.StyleCatalog(document)
        data = catalog.to_dict()

        assert data["cycles"] == []
        assert data["missing"][0]["kind"] == "missing"
        assert data["missing"][0]["feature_id"] == "pm"
        assert data["missing"][0]["feature_name"] == "Broken"
        assert data["missing"][0]["chain"] == ["#pm", "#absent"]
        assert data["features"][-1]["normal"]["declarations"] == {}


class TestLxml(Lxml, TestStdLibrary):
    """Test with lxml."""
