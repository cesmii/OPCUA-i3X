"""ObjectInstanceMetadata carries dataType + dataTypeName."""

from i3xua.i3x.types import ObjectInstanceMetadata


def test_metadata_accepts_data_type_fields() -> None:
    m = ObjectInstanceMetadata.model_validate(
        {
            "typeNamespaceUri": "http://example.org/UA/",
            "sourceTypeId": "i=2368",
            "dataType": "i=11",
            "dataTypeName": "Double",
        }
    )
    assert m.dataType == "i=11"
    assert m.dataTypeName == "Double"


def test_metadata_data_type_fields_default_none_and_excluded_when_dumped() -> None:
    m = ObjectInstanceMetadata.model_validate({"sourceTypeId": "i=58"})
    dump = m.model_dump(exclude_none=True)
    assert "dataType" not in dump
    assert "dataTypeName" not in dump
