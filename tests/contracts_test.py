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
"""Test the named column contracts for extended data."""

import fastkml as kml
from fastkml.contracts import ColumnContract
from fastkml.contracts import ColumnValue
from fastkml.enums import DataType
from tests.base import Lxml
from tests.base import StdLibrary

KML_NS = "http://www.opengis.net/kml/2.2"
GX_NS = "http://www.google.com/kml/ext/2.2"


def _document(xml: str) -> kml.Document:
    parsed = kml.KML.from_string(xml, ns=None)
    document = parsed.features[0]
    assert isinstance(document, kml.Document)
    return document


TYPED_DOCUMENT = f"""<kml xmlns="{KML_NS}">
<Document>
  <Schema name="Weather" id="weather-schema">
    <SimpleField name="temperature" type="double"/>
    <SimpleField name="station" type="string"/>
  </Schema>
  <Placemark>
    <ExtendedData>
      <SchemaData schemaUrl="#weather-schema">
        <SimpleData name="temperature">21.5</SimpleData>
        <SimpleData name="station">ALPHA</SimpleData>
      </SchemaData>
    </ExtendedData>
  </Placemark>
</Document>
</kml>"""

UNTYPED_DOCUMENT = f"""<kml xmlns="{KML_NS}">
<Document>
  <Placemark>
    <ExtendedData>
      <Data name="temperature">
        <displayName>Temperature</displayName>
        <value>warm</value>
      </Data>
    </ExtendedData>
  </Placemark>
</Document>
</kml>"""


class TestStdLibrary(StdLibrary):
    """Test with the standard library."""

    def test_untyped_column_contract(self) -> None:
        values = kml.column_values(_document(UNTYPED_DOCUMENT))

        assert len(values) == 1
        contract = values[0].contract
        assert contract == ColumnContract(
            name="temperature",
            namespace=KML_NS,
            schema_url=None,
            declared_type=None,
            array=False,
        )
        assert values[0].raw == "warm"
        assert values[0].value == "warm"
        assert values[0].error is None

    def test_typed_column_contract_keeps_schema_url_and_type(self) -> None:
        contracts = kml.column_contracts(_document(TYPED_DOCUMENT))

        temperature = next(c for c in contracts if c.name == "temperature")
        assert temperature.schema_url == "#weather-schema"
        assert temperature.declared_type is DataType.double
        assert temperature.namespace == KML_NS
        assert temperature.array is False

    def test_typed_value_is_coerced_and_raw_preserved(self) -> None:
        values = kml.column_values(_document(TYPED_DOCUMENT))

        temperature = next(v for v in values if v.contract.name == "temperature")
        assert temperature.raw == "21.5"
        assert temperature.value == 21.5
        assert isinstance(temperature.value, float)
        assert temperature.error is None

        station = next(v for v in values if v.contract.name == "station")
        assert station.raw == "ALPHA"
        assert station.value == "ALPHA"

    def test_same_name_in_two_documents_keeps_separate_contracts(self) -> None:
        typed = _document(TYPED_DOCUMENT)
        untyped = _document(UNTYPED_DOCUMENT)

        typed_contract = next(
            c for c in kml.column_contracts(typed) if c.name == "temperature"
        )
        untyped_contract = next(
            c for c in kml.column_contracts(untyped) if c.name == "temperature"
        )

        assert typed_contract.declared_type is DataType.double
        assert typed_contract.schema_url == "#weather-schema"
        assert untyped_contract.declared_type is None
        assert untyped_contract.schema_url is None
        assert typed_contract != untyped_contract

    def test_missing_schema_keeps_url_without_declared_type(self) -> None:
        xml = f"""<kml xmlns="{KML_NS}">
        <Document>
          <Placemark>
            <ExtendedData>
              <SchemaData schemaUrl="#weather-schema">
                <SimpleData name="temperature">9</SimpleData>
              </SchemaData>
            </ExtendedData>
          </Placemark>
        </Document>
        </kml>"""

        values = kml.column_values(_document(xml))

        assert len(values) == 1
        contract = values[0].contract
        assert contract.schema_url == "#weather-schema"
        assert contract.declared_type is None
        assert values[0].raw == "9"
        assert values[0].value == "9"
        assert values[0].error is None

    def test_missing_schema_does_not_borrow_other_document(self) -> None:
        with_schema = _document(TYPED_DOCUMENT)
        xml = f"""<kml xmlns="{KML_NS}">
        <Document>
          <Placemark>
            <ExtendedData>
              <SchemaData schemaUrl="#weather-schema">
                <SimpleData name="temperature">9</SimpleData>
              </SchemaData>
            </ExtendedData>
          </Placemark>
        </Document>
        </kml>"""
        without_schema = _document(xml)

        declared = next(
            c for c in kml.column_contracts(with_schema) if c.name == "temperature"
        )
        undeclared = next(
            c for c in kml.column_contracts(without_schema) if c.name == "temperature"
        )

        assert declared.declared_type is DataType.double
        assert undeclared.declared_type is None
        assert kml.column_values(without_schema)[0].value == "9"

    def test_failed_coercion_isolates_only_that_value(self) -> None:
        xml = f"""<kml xmlns="{KML_NS}">
        <Document>
          <Schema name="Weather" id="weather-schema">
            <SimpleField name="temperature" type="double"/>
          </Schema>
          <Placemark>
            <ExtendedData>
              <SchemaData schemaUrl="#weather-schema">
                <SimpleData name="temperature">not-a-number</SimpleData>
              </SchemaData>
            </ExtendedData>
          </Placemark>
          <Placemark>
            <ExtendedData>
              <SchemaData schemaUrl="#weather-schema">
                <SimpleData name="temperature">19.0</SimpleData>
              </SchemaData>
            </ExtendedData>
          </Placemark>
        </Document>
        </kml>"""

        values = kml.column_values(_document(xml))

        assert len(values) == 2
        bad, good = values
        assert isinstance(bad, ColumnValue)
        assert bad.raw == "not-a-number"
        assert bad.value is None
        assert bad.error is not None
        assert good.raw == "19.0"
        assert good.value == 19.0
        assert good.error is None

    def test_array_failure_isolates_only_that_element(self) -> None:
        xml = f"""<kml xmlns="{KML_NS}" xmlns:gx="{GX_NS}">
        <Document>
          <Schema id="weather-schema">
            <gx:SimpleArrayField name="readings" type="int"/>
          </Schema>
          <Placemark>
            <ExtendedData>
              <SchemaData schemaUrl="#weather-schema">
                <gx:SimpleArrayData name="readings">
                  <gx:value>1</gx:value>
                  <gx:value>oops</gx:value>
                  <gx:value>7</gx:value>
                </gx:SimpleArrayData>
              </SchemaData>
            </ExtendedData>
          </Placemark>
        </Document>
        </kml>"""

        values = kml.column_values(_document(xml))

        assert [value.raw for value in values] == ["1", "oops", "7"]
        assert [value.value for value in values] == [1, None, 7]
        assert values[0].error is None
        assert values[1].error is not None
        assert values[2].error is None
        assert all(value.contract.array is True for value in values)
        assert values[0].contract.namespace == GX_NS

    def test_unsigned_negative_value_is_rejected(self) -> None:
        xml = f"""<kml xmlns="{KML_NS}">
        <Document>
          <Schema id="s">
            <SimpleField name="count" type="uint"/>
          </Schema>
          <Placemark>
            <ExtendedData>
              <SchemaData schemaUrl="#s">
                <SimpleData name="count">-3</SimpleData>
              </SchemaData>
            </ExtendedData>
          </Placemark>
        </Document>
        </kml>"""

        value = kml.column_values(_document(xml))[0]

        assert value.raw == "-3"
        assert value.value is None
        assert "unsigned" in (value.error or "")

    def test_bool_values_are_coerced(self) -> None:
        xml = f"""<kml xmlns="{KML_NS}">
        <Document>
          <Schema id="s">
            <SimpleField name="on" type="bool"/>
          </Schema>
          <Placemark>
            <ExtendedData>
              <SchemaData schemaUrl="#s">
                <SimpleData name="on">1</SimpleData>
              </SchemaData>
            </ExtendedData>
          </Placemark>
        </Document>
        </kml>"""

        value = kml.column_values(_document(xml))[0]

        assert value.value is True
        assert value.error is None

    def test_contracts_are_deduplicated(self) -> None:
        xml = f"""<kml xmlns="{KML_NS}">
        <Document>
          <Schema id="s">
            <SimpleField name="temperature" type="double"/>
          </Schema>
          <Placemark>
            <ExtendedData>
              <SchemaData schemaUrl="#s">
                <SimpleData name="temperature">1.0</SimpleData>
              </SchemaData>
            </ExtendedData>
          </Placemark>
          <Placemark>
            <ExtendedData>
              <SchemaData schemaUrl="#s">
                <SimpleData name="temperature">2.0</SimpleData>
              </SchemaData>
            </ExtendedData>
          </Placemark>
        </Document>
        </kml>"""

        contracts = kml.column_contracts(_document(xml))

        assert len(contracts) == 1
        assert contracts[0].name == "temperature"

    def test_same_name_two_schema_urls_are_separate_contracts(self) -> None:
        xml = f"""<kml xmlns="{KML_NS}">
        <Document>
          <Schema id="a">
            <SimpleField name="temperature" type="double"/>
          </Schema>
          <Schema id="b">
            <SimpleField name="temperature" type="string"/>
          </Schema>
          <Placemark>
            <ExtendedData>
              <SchemaData schemaUrl="#a">
                <SimpleData name="temperature">1.5</SimpleData>
              </SchemaData>
              <SchemaData schemaUrl="#b">
                <SimpleData name="temperature">mild</SimpleData>
              </SchemaData>
            </ExtendedData>
          </Placemark>
        </Document>
        </kml>"""

        document = _document(xml)
        contracts = kml.column_contracts(document)
        values = kml.column_values(document)

        assert len(contracts) == 2
        assert {c.schema_url for c in contracts} == {"#a", "#b"}
        assert {c.declared_type for c in contracts} == {
            DataType.double,
            DataType.string,
        }
        assert [value.value for value in values] == [1.5, "mild"]

    def test_extended_data_in_nested_folders_is_found(self) -> None:
        xml = f"""<kml xmlns="{KML_NS}">
        <Document>
          <Folder>
            <Folder>
              <Placemark>
                <ExtendedData>
                  <Data name="temperature"><value>cold</value></Data>
                </ExtendedData>
              </Placemark>
            </Folder>
          </Folder>
        </Document>
        </kml>"""

        values = kml.column_values(_document(xml))

        assert len(values) == 1
        assert values[0].raw == "cold"


class TestLxml(Lxml, TestStdLibrary):
    """Test with lxml."""
