"""OPC UA standard DataType NodeId -> spec name.

Two layers:
  - Part 6 Table 2 encoding builtins (Boolean..Enumeration, NodeIds 1-29).
  - Part 3 / Part 5 well-known DataTypes that derive from a Part-6 builtin
    but carry distinct semantics on the wire (Duration, UtcTime, LocaleId,
    Decimal, ServerStatusDataType, Range, EUInformation, …). These show
    up frequently on the OPC Foundation Reference Server and similar.

Used by `core.mapping.to_object_instance` for `metadata.system.dataTypeName`
and by `adapters.asyncua.browse.collect_artificial_types` for naming
artificial types (Duration_Scalar_RW vs Custom_i290_Scalar_RW). Anything
not in this table falls through to the browse-time BrowseName resolver
(`_resolve_unresolved_datatype_names`) and finally to
`Custom_<sanitized-NodeId>` if even that misses.
"""

from __future__ import annotations

# Canonical form (no `ns=0;` prefix) per `core.neutral.canonicalize_node_id`.
BUILTIN_DATATYPE_NAMES: dict[str, str] = {
    # Part 6 §5 encoding builtins.
    "i=1": "Boolean",
    "i=2": "SByte",
    "i=3": "Byte",
    "i=4": "Int16",
    "i=5": "UInt16",
    "i=6": "Int32",
    "i=7": "UInt32",
    "i=8": "Int64",
    "i=9": "UInt64",
    "i=10": "Float",
    "i=11": "Double",
    "i=12": "String",
    "i=13": "DateTime",
    "i=14": "Guid",
    "i=15": "ByteString",
    "i=16": "XmlElement",
    "i=17": "NodeId",
    "i=18": "ExpandedNodeId",
    "i=19": "StatusCode",
    "i=20": "QualifiedName",
    "i=21": "LocalizedText",
    "i=22": "Structure",
    "i=23": "DataValue",
    "i=24": "BaseDataType",
    "i=25": "DiagnosticInfo",
    "i=26": "Number",
    "i=27": "Integer",
    "i=28": "UInteger",
    "i=29": "Enumeration",
    # Part 3 / Part 5 — well-known semantic aliases over Part-6 builtins.
    # The browse-time BrowseName resolver picks up the long tail (anything
    # observed on the live address space), so missing entries self-heal at
    # runtime; entries here are an optimization to skip the extra Read for
    # types we already know. Some labels below are best-effort spec lookups
    # — if a server publishes a different DisplayName for one of these
    # NodeIds, that's a sign to amend the table (server's BrowseName is
    # authoritative).
    "i=290": "Duration",
    "i=294": "UtcTime",
    "i=295": "LocaleId",
    "i=296": "Argument",
    "i=297": "StatusResult",
    "i=302": "MessageSecurityMode",
    "i=303": "UserTokenType",
    "i=307": "ApplicationType",
    "i=308": "ApplicationDescription",
    "i=311": "EndpointDescription",
    "i=315": "SecurityTokenRequestType",
    "i=338": "BuildInfo",
    "i=344": "SignedSoftwareCertificate",
    "i=388": "ServerStatusDataType",
    "i=393": "ServerDiagnosticsSummaryDataType",
    "i=399": "SamplingIntervalDiagnosticsDataType",
    "i=862": "ServerStatusDataType",
    "i=884": "Range",
    "i=887": "EUInformation",
    "i=894": "ServiceCounterDataType",
    "i=12188": "DateString",
    "i=12189": "TimeString",
    "i=12190": "DurationString",
    "i=12554": "OptionSet",
    "i=12755": "Union",
    "i=12756": "NormalizedString",
    "i=12877": "AudioDataType",
    "i=14647": "DataTypeSchemaHeader",
    "i=17861": "Decimal",
    "i=18947": "ContinuationPoint",
    "i=19084": "BitFieldMaskDataType",
    "i=19723": "DataSetMetaDataType",
    "i=19730": "DataSetReaderMessageDataType",
    "i=20998": "EnumValueType",
    "i=23751": "PortableQualifiedName",
    "i=24277": "RolePermissionType",
    # Image variants (Part 5 §12.20).
    "i=30": "Image",
    "i=2000": "ImageBMP",
    "i=2001": "ImageGIF",
    "i=2002": "ImageJPG",
    "i=2003": "ImagePNG",
}


def lookup_datatype_name(node_id: str) -> str | None:
    """Return the spec name for a built-in OPC UA DataType, or None."""
    return BUILTIN_DATATYPE_NAMES.get(node_id)


__all__ = ["BUILTIN_DATATYPE_NAMES", "lookup_datatype_name"]
