"""Old revision of the GSB-COMPAT-MIB fixture.

This module is written in the style pysmi produces: every base symbol is
pulled from the *mibBuilder* that executes the module, so loading the same
module name from another directory builds an independent set of classes.
"""

from pyasn1.type.constraint import (
    ConstraintsExclusion,
    ConstraintsIntersection,
    ConstraintsUnion,
    ContainedSubtypeConstraint,
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
    Counter32,
    Integer32,
    MibTable,
    MibTableColumn,
    MibTableRow,
) = mibBuilder.import_symbols(
    "SNMPv2-SMI",
    "Bits",
    "Counter32",
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
        ValueRangeConstraint(1, 100),
    )


class _WidenedRange_Type(Integer32):
    subtypeSpec = Integer32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        ValueRangeConstraint(1, 50),
    )


class _CounterTag_Type(Counter32):
    pass


class _PhaseEnum_Type(Integer32):
    subtypeSpec = Integer32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        SingleValueConstraint(0, 1),
    )
    namedValues = NamedValues(("init", 0), ("run", 1))


class _StatusCode_Type(Integer32):
    subtypeSpec = Integer32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        SingleValueConstraint(1, 2, 3),
    )
    namedValues = NamedValues(("up", 1), ("down", 2), ("testing", 3))


class _RoleBits_Type(Bits):
    namedValues = NamedValues(("admin", 0), ("user", 1))


class _FlagBits_Type(Bits):
    namedValues = NamedValues(("flagA", 0), ("flagB", 1), ("flagC", 2))


class _StringSize_Type(OctetString):
    subtypeSpec = OctetString.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        ValueSizeConstraint(0, 8),
    )


class _AlphabetWidened_Type(OctetString):
    subtypeSpec = OctetString.subtypeSpec
    subtypeSpec += ConstraintsIntersection(
        PermittedAlphabetConstraint(65, 66, 67),
        ValueSizeConstraint(0, 4),
    )


class _AlphabetRestricted_Type(OctetString):
    subtypeSpec = OctetString.subtypeSpec
    subtypeSpec += ConstraintsIntersection(
        PermittedAlphabetConstraint(65, 66, 67, 68),
        ValueSizeConstraint(0, 4),
    )


class _ExclusionType(Integer32):
    subtypeSpec = Integer32.subtypeSpec
    subtypeSpec += ValueRangeConstraint(0, 10)
    subtypeSpec += ConstraintsExclusion(SingleValueConstraint(7))


class _ContainedType(Integer32):
    subtypeSpec = Integer32.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        ContainedSubtypeConstraint(SingleValueConstraint(1, 2, 3)),
    )


class _FamilyString_Type(OctetString):
    subtypeSpec = OctetString.subtypeSpec
    subtypeSpec += ConstraintsUnion(
        ValueSizeConstraint(1, 1),
    )


stableEnum = MibTableColumn(_compatEntry.name + (1,), _StableEnum_Type())
stableRange = MibTableColumn(_compatEntry.name + (2,), _StableRange_Type())
narrowedRange = MibTableColumn(_compatEntry.name + (3,), _NarrowedRange_Type())
widenedRange = MibTableColumn(_compatEntry.name + (4,), _WidenedRange_Type())
tagChanged = MibTableColumn(_compatEntry.name + (5,), _CounterTag_Type())
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
containedGap = MibTableColumn(_compatEntry.name + (14,), _ContainedType())
familyChanged = MibTableColumn(_compatEntry.name + (15,), _FamilyString_Type())

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
