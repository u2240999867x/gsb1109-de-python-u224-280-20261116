"""New revision of the GSB-COMPAT-MIB fixture.

Same module name and OIDs as the v1 fixture, but each column exercises a
different upgrade outcome. Both versions are executed by independent
MibBuilder instances, so none of these classes are shared at import time.
"""

from pyasn1.type.constraint import (
    ConstraintsExclusion,
    ConstraintsIntersection,
    ConstraintsUnion,
    PermittedAlphabetConstraint,
    SingleValueConstraint,
    ValueRangeConstraint,
    ValueSizeConstraint,
)

(OctetString, ObjectIdentifier) = mibBuilder.import_symbols(
    "ASN1", "OctetString", "ObjectIdentifier"
)

(NamedValues,) = mibBuilder.import_symbols("ASN1-ENUMERATION", "NamedValues")

(
    Bits,
    Gauge32,
    Integer32,
    MibTable,
    MibTableColumn,
    MibTableRow,
) = mibBuilder.import_symbols(
    "SNMPv2-SMI",
    "Bits",
    "Gauge32",
    "Integer32",
    "MibTable",
    "MibTableColumn",
    "MibTableRow",
)

_root = (1, 3, 6, 1, 4, 1, 99999, 77)
_compatTable = MibTable(_root + (1,))
_compatEntry = MibTableRow(_root + (1, 1))


class _StableEnum_Type(Integer32):
    subtypeSpec = Integer32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        SingleValueConstraint(1, 2),
    )
    namedValues = NamedValues(("true", 1), ("false", 2))


class _StableRange_Type(Integer32):
    subtypeSpec = Integer32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        ValueRangeConstraint(0, 100),
    )


class _NarrowedRange_Type(Integer32):
    subtypeSpec = Integer32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        ValueRangeConstraint(10, 90),
    )


class _WidenedRange_Type(Integer32):
    subtypeSpec = Integer32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        ValueRangeConstraint(0, 100),
    )


class _GaugeTag_Type(Gauge32):
    pass


class _PhaseEnum_Type(Integer32):
    subtypeSpec = Integer32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        SingleValueConstraint(0, 1),
    )
    namedValues = NamedValues(("init", 0), ("halt", 1))


class _StatusCode_Type(Integer32):
    subtypeSpec = Integer32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        SingleValueConstraint(1, 2, 3),
    )
    namedValues = NamedValues(("up", 1), ("down", 2), ("standby", 3))


class _RoleBits_Type(Bits):
    namedValues = NamedValues(("admin", 0), ("guest", 1))


class _FlagBits_Type(Bits):
    namedValues = NamedValues(("flagA", 0), ("flagB", 1), ("flagC", 2))


class _StringSize_Type(OctetString):
    subtypeSpec = OctetString.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        ValueSizeConstraint(0, 4),
    )


class _AlphabetWidened_Type(OctetString):
    subtypeSpec = OctetString.subtypeSpec
    subtypeSpec += ConstraintsIntersection(
        PermittedAlphabetConstraint(65, 66, 67, 68),
        ValueSizeConstraint(0, 4),
    )


class _AlphabetRestricted_Type(OctetString):
    subtypeSpec = OctetString.subtypeSpec
    subtypeSpec += ConstraintsIntersection(
        PermittedAlphabetConstraint(65, 66, 67),
        ValueSizeConstraint(0, 4),
    )


class _ExclusionType(Integer32):
    subtypeSpec = Integer32.subtypeSpec
    subtypeSpec += ValueRangeConstraint(0, 10)
    subtypeSpec += ConstraintsExclusion(SingleValueConstraint(7))


class _ContainedGapEquivalent_Type(Integer32):
    subtypeSpec = Integer32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        SingleValueConstraint(1, 2, 3),
    )


class _FamilyInteger_Type(Integer32):
    subtypeSpec = Integer32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        ValueRangeConstraint(0, 255),
    )


stableEnum = MibTableColumn(_compatEntry.name + (1,), _StableEnum_Type())
stableRange = MibTableColumn(_compatEntry.name + (2,), _StableRange_Type())
narrowedRange = MibTableColumn(_compatEntry.name + (3,), _NarrowedRange_Type())
widenedRange = MibTableColumn(_compatEntry.name + (4,), _WidenedRange_Type())
tagChanged = MibTableColumn(_compatEntry.name + (5,), _GaugeTag_Type())
phaseEnum = MibTableColumn(_compatEntry.name + (6,), _PhaseEnum_Type())
statusCode = MibTableColumn(_compatEntry.name + (7,), _StatusCode_Type())
roleBits = MibTableColumn(_compatEntry.name + (8,), _RoleBits_Type())
flagBits = MibTableColumn(_compatEntry.name + (9,), _FlagBits_Type())
stringSize = MibTableColumn(_compatEntry.name + (10,), _StringSize_Type())
alphabetWidened = MibTableColumn(_compatEntry.name + (11,), _AlphabetWidened_Type())
alphabetRestricted = MibTableColumn(
    _compatEntry.name + (12,), _AlphabetRestricted_Type()
)
exclusionSeven = MibTableColumn(_compatEntry.name + (13,), _ExclusionType())
containedGap = MibTableColumn(_compatEntry.name + (14,), _ContainedGapEquivalent_Type())
familyChanged = MibTableColumn(_compatEntry.name + (15,), _FamilyInteger_Type())

mibBuilder.export_symbols(
    "GSB-COMPAT-MIB",
    compatTable=_compatTable,
    compatEntry=_compatEntry,
    stableEnum=stableEnum,
    stableRange=stableRange,
    narrowedRange=narrowedRange,
    widenedRange=widenedRange,
    tagChanged=tagChanged,
    phaseEnum=phaseEnum,
    statusCode=statusCode,
    roleBits=roleBits,
    flagBits=flagBits,
    stringSize=stringSize,
    alphabetWidened=alphabetWidened,
    alphabetRestricted=alphabetRestricted,
    exclusionSeven=exclusionSeven,
    containedGap=containedGap,
    familyChanged=familyChanged,
)
