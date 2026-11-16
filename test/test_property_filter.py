from dbus_next import (PropertyFilter, Condition, PropertyRef, FilterVerdict, Variant,
                       RuleValidationError)
from dbus_next.signature import SignatureTree

import pytest

IFACE = 'org.example.Device'


def ref(name, signatures):
    return PropertyRef(IFACE, name, signatures)


def key(name):
    return (IFACE, name)


# ---------------------------------------------------------------------------
# rule and field-type validation
# ---------------------------------------------------------------------------


def test_invalid_signature_is_rejected():
    with pytest.raises(RuleValidationError):
        PropertyRef(IFACE, 'Bad', ['{'])


def test_signature_must_be_a_single_complete_type():
    with pytest.raises(RuleValidationError):
        PropertyRef(IFACE, 'Bad', ['ss'])


def test_at_least_one_signature_required():
    with pytest.raises(RuleValidationError):
        PropertyRef(IFACE, 'Bad', [])


def test_unknown_operator_rejected():
    with pytest.raises(RuleValidationError):
        Condition(ref('Online', ['b']), '~=', True)


def test_none_operand_rejected():
    with pytest.raises(RuleValidationError):
        Condition(ref('Online', ['b']), '==', None)


def test_bool_operand_rejected_for_integer_types():
    with pytest.raises(RuleValidationError):
        Condition(ref('Count', ['i']), '==', True)


def test_integer_operand_must_fit_declared_width():
    Condition(ref('Small', ['q']), '==', 0xffff)
    with pytest.raises(RuleValidationError):
        Condition(ref('Small', ['q']), '==', 0x10000)
    with pytest.raises(RuleValidationError):
        Condition(ref('Signed', ['n']), '==', -0x8001)


def test_boolean_only_supports_equality():
    Condition(ref('Online', ['b']), '!=', True)
    with pytest.raises(RuleValidationError):
        Condition(ref('Online', ['b']), '<', True)


def test_operand_valid_if_it_fits_any_allowed_signature():
    # 100000 fits x (int64) but not i (int32 fits too actually); use a value
    # beyond uint32 to prove per-signature checking.
    Condition(ref('Big', ['i', 'x']), '==', 0x100000000)
    with pytest.raises(RuleValidationError):
        Condition(ref('Small', ['y', 'q']), '==', 0x10000)


def test_object_path_operand_validated():
    Condition(ref('Peer', ['o']), '==', '/org/example')
    with pytest.raises(RuleValidationError):
        Condition(ref('Peer', ['o']), '==', 'not-a-path')


def test_struct_operand_must_match_member_count_and_types():
    Condition(ref('Point', ['(ii)']), '==', [1, 2])
    with pytest.raises(RuleValidationError):
        Condition(ref('Point', ['(ii)']), '==', [1])
    with pytest.raises(RuleValidationError):
        Condition(ref('Point', ['(ii)']), '==', [1, True])


def test_variant_operand_must_itself_be_variant():
    Condition(ref('Any', ['v']), '==', Variant('i', 1))
    with pytest.raises(RuleValidationError):
        Condition(ref('Any', ['v']), '==', 1)


def test_conflicting_signature_contracts_on_same_property_rejected():
    leaf_a = Condition(ref('Count', ['i']), '==', 1)
    leaf_b = Condition(ref('Count', ['x']), '==', 1)
    with pytest.raises(RuleValidationError):
        PropertyFilter.all_of([leaf_a, leaf_b])


def test_introspection_validation_rejects_unknown_property():
    index = {IFACE: {'Other': SignatureTree('b').types[0]}}
    rule = PropertyFilter.all_of([Condition(ref('Online', ['b']), '==', True)])
    with pytest.raises(RuleValidationError):
        rule.with_introspection(index)


def test_introspection_validation_rejects_disallowed_signature():
    index = {IFACE: {'Count': SignatureTree('x').types[0]}}
    rule = PropertyFilter.all_of([Condition(ref('Count', ['i']), '==', 1)])
    with pytest.raises(RuleValidationError):
        rule.with_introspection(index)


def test_introspection_validation_accepts_matching_contract():
    index = {IFACE: {'Count': SignatureTree('x').types[0]}}
    rule = PropertyFilter.all_of([Condition(ref('Count', ['i', 'x']), '==', 1)])
    validated = rule.with_introspection(index)
    assert validated.evaluate({key('Count'): 1}).verdict is FilterVerdict.MATCH


# ---------------------------------------------------------------------------
# three-valued boolean evaluation
# ---------------------------------------------------------------------------


def test_unread_property_is_need_read_with_exact_evidence():
    rule = PropertyFilter.all_of([Condition(ref('Online', ['b']), '==', True)])
    result = rule.evaluate()
    assert result.verdict is FilterVerdict.NEED_READ
    assert result.missing == [key('Online')]


def test_leaf_match_and_exclude():
    rule = PropertyFilter.all_of([Condition(ref('Online', ['b']), '==', True)])
    assert rule.evaluate({key('Online'): True}).verdict is FilterVerdict.MATCH
    assert rule.evaluate({key('Online'): False}).verdict is FilterVerdict.EXCLUDE


def test_and_requires_all_known_and_holding():
    rule = PropertyFilter.all_of([
        Condition(ref('Online', ['b']), '==', True),
        Condition(ref('Count', ['i']), '>=', 3),
    ])
    result = rule.evaluate({key('Online'): True})
    assert result.verdict is FilterVerdict.NEED_READ
    assert result.missing == [key('Count')]

    result = rule.evaluate({
        key('Online'): True,
        key('Count'): 5,
    })
    assert result.verdict is FilterVerdict.MATCH


def test_and_short_circuits_to_exclude_without_requesting_other_reads():
    rule = PropertyFilter.all_of([
        Condition(ref('Online', ['b']), '==', True),
        Condition(ref('Count', ['i']), '>=', 3),
    ])
    # Online is false: the exclusion is settled; Count must not be requested.
    result = rule.evaluate({key('Online'): False})
    assert result.verdict is FilterVerdict.EXCLUDE
    assert result.missing == []


def test_or_short_circuits_to_match_without_requesting_other_reads():
    rule = PropertyFilter.any_of([
        Condition(ref('Online', ['b']), '==', True),
        Condition(ref('Count', ['i']), '>=', 3),
    ])
    result = rule.evaluate({key('Online'): True})
    assert result.verdict is FilterVerdict.MATCH
    assert result.missing == []


def test_pending_branch_does_not_demand_reads_after_verdict_settled():
    # First branch is pending, second settles the verdict: the pending
    # property is irrelevant and must not appear in the missing evidence.
    rule = PropertyFilter.any_of([
        Condition(ref('Online', ['b']), '==', True),
        Condition(ref('Count', ['i']), '>=', 3),
    ])
    result = rule.evaluate({key('Count'): 5})
    assert result.verdict is FilterVerdict.MATCH
    assert result.missing == []

    rule = PropertyFilter.all_of([
        Condition(ref('Online', ['b']), '==', True),
        Condition(ref('Count', ['i']), '>=', 3),
    ])
    result = rule.evaluate({key('Count'): 0})
    assert result.verdict is FilterVerdict.EXCLUDE
    assert result.missing == []


def test_not_negates_all_three_verdicts():
    inner = Condition(ref('Online', ['b']), '==', True)
    rule = PropertyFilter.not_(inner)
    assert rule.evaluate().verdict is FilterVerdict.NEED_READ
    assert rule.evaluate({key('Online'): False}).verdict is FilterVerdict.MATCH
    assert rule.evaluate({key('Online'): True}).verdict is FilterVerdict.EXCLUDE


def test_nested_boolean_expression():
    rule = PropertyFilter.all_of([
        PropertyFilter.any_of([
            Condition(ref('Online', ['b']), '==', True),
            Condition(ref('Ready', ['b']), '==', True),
        ]),
        PropertyFilter.not_(Condition(ref('Blocked', ['b']), '==', True)),
    ])
    result = rule.evaluate({
        key('Online'): False,
        key('Ready'): True,
        key('Blocked'): False,
    })
    assert result.verdict is FilterVerdict.MATCH

    result = rule.evaluate({key('Online'): False})
    assert result.verdict is FilterVerdict.NEED_READ
    assert set(result.missing) == {key('Ready'), key('Blocked')}


# ---------------------------------------------------------------------------
# invalidated properties: stale cache is never substituted
# ---------------------------------------------------------------------------


def test_invalidated_property_forces_reread_even_with_stale_value():
    rule = PropertyFilter.all_of([Condition(ref('Online', ['b']), '==', True)])
    result = rule.evaluate({key('Online'): True}, invalidated=[key('Online')])
    assert result.verdict is FilterVerdict.NEED_READ
    assert result.missing == [key('Online')]


def test_invalidation_then_fresh_read_decides_normally():
    rule = PropertyFilter.all_of([Condition(ref('Online', ['b']), '==', True)])
    result = rule.evaluate({key('Online'): False}, invalidated=[key('Online')])
    assert result.verdict is FilterVerdict.NEED_READ
    # A later batch without the invalidation marker trusts the fresh value.
    result = rule.evaluate({key('Online'): True})
    assert result.verdict is FilterVerdict.MATCH


# ---------------------------------------------------------------------------
# empty collections: element types are never guessed from elements
# ---------------------------------------------------------------------------


def test_empty_array_equality_is_decidable_from_declared_type_only():
    rule = PropertyFilter.all_of([Condition(ref('Tags', ['as']), '==', [])])
    index = {IFACE: {'Tags': SignatureTree('as').types[0]}}
    result = rule.with_introspection(index).evaluate({key('Tags'): []})
    assert result.verdict is FilterVerdict.MATCH
    assert result.missing == []


def test_empty_array_never_infers_element_signature():
    # The same empty value under two different element contracts is decided by
    # the declared signature, never by inspecting (absent) elements.
    for sig in ('ai', 'as', 'a{sv}'):
        operand = {} if sig == 'a{sv}' else []
        rule = PropertyFilter.all_of([Condition(ref('Empty', [sig]), '==', operand)])
        result = rule.evaluate({key('Empty'): operand})
        assert result.verdict is FilterVerdict.MATCH


def test_non_empty_array_comparison():
    rule = PropertyFilter.all_of([Condition(ref('Tags', ['as']), '==', ['a', 'b'])])
    assert rule.evaluate({key('Tags'): ['a', 'b']}).verdict is FilterVerdict.MATCH
    assert rule.evaluate({key('Tags'): ['a']}).verdict is FilterVerdict.EXCLUDE


def test_byte_array_comparison():
    rule = PropertyFilter.all_of([Condition(ref('Blob', ['ay']), '==', b'\x01\x02')])
    assert rule.evaluate({key('Blob'): bytearray(b'\x01\x02')}).verdict is FilterVerdict.MATCH


def test_dict_ordering_operators_are_rejected():
    with pytest.raises(RuleValidationError):
        Condition(ref('Props', ['a{ss}']), '<', {})


# ---------------------------------------------------------------------------
# integer widths and Variant interpretation
# ---------------------------------------------------------------------------


def test_cross_width_integer_comparison_is_numeric():
    # Property is declared uint16; comparison against a plain int operand is a
    # numeric comparison once the operand is known to fit the declared width.
    rule = PropertyFilter.all_of([Condition(ref('Small', ['q']), '>', 256)])
    index = {IFACE: {'Small': SignatureTree('q').types[0]}}
    result = rule.with_introspection(index).evaluate({key('Small'): 257})
    assert result.verdict is FilterVerdict.MATCH


def test_value_outside_declared_integer_range_excludes():
    rule = PropertyFilter.all_of([Condition(ref('Small', ['q']), '==', 1)])
    result = rule.evaluate({key('Small'): 0x10000})
    assert result.verdict is FilterVerdict.EXCLUDE


def test_variant_wrong_inner_signature_is_definite_exclusion_with_evidence():
    rule = PropertyFilter.all_of([Condition(ref('Count', ['i']), '<', 10)])
    result = rule.evaluate({key('Count'): Variant('s', 'hello')})
    assert result.verdict is FilterVerdict.EXCLUDE
    assert result.missing == []
    assert result.signature_mismatches == {key('Count'): ['s']}


def test_variant_matching_inner_signature_is_compared_by_inner_value():
    rule = PropertyFilter.all_of([Condition(ref('Count', ['i', 'x']), '<', 10)])
    assert rule.evaluate({key('Count'): Variant('i', 5)}).verdict is FilterVerdict.MATCH
    assert rule.evaluate({key('Count'): Variant('x', 50)}).verdict is FilterVerdict.EXCLUDE


def test_ambiguous_contract_without_introspection_requires_disambiguation():
    rule = PropertyFilter.all_of([Condition(ref('Count', ['i', 'x']), '<', 10)])
    with pytest.raises(RuleValidationError):
        rule.evaluate({key('Count'): 5})
    # A Variant carries its own signature, so it remains decidable.
    result = rule.evaluate({key('Count'): Variant('x', 5)})
    assert result.verdict is FilterVerdict.MATCH


def test_double_comparison_coerces_numeric_operand():
    rule = PropertyFilter.all_of([Condition(ref('Level', ['d']), '>=', 1.5)])
    assert rule.evaluate({key('Level'): 2}).verdict is FilterVerdict.MATCH
    assert rule.evaluate({key('Level'): 1.0}).verdict is FilterVerdict.EXCLUDE


def test_malformed_value_for_declared_type_excludes():
    rule = PropertyFilter.all_of([Condition(ref('Online', ['b']), '==', True)])
    result = rule.evaluate({key('Online'): 'yes'})
    assert result.verdict is FilterVerdict.EXCLUDE


# ---------------------------------------------------------------------------
# dict construction and batch independence
# ---------------------------------------------------------------------------


def test_from_dict_builds_nested_expression():
    rule = PropertyFilter.from_dict({
        'all': [
            {
                'interface': IFACE,
                'name': 'Online',
                'signatures': ['b'],
                'op': '==',
                'operand': True,
            },
            {
                'not': {
                    'interface': IFACE,
                    'name': 'Count',
                    'signatures': ['i'],
                    'op': '<',
                    'operand': 1,
                }
            },
        ]
    })
    result = rule.evaluate({
        key('Online'): True,
        key('Count'): 5,
    })
    assert result.verdict is FilterVerdict.MATCH


def test_from_dict_rejects_malformed_expression():
    with pytest.raises(RuleValidationError):
        PropertyFilter.from_dict({'all': []})
    with pytest.raises(RuleValidationError):
        PropertyFilter.from_dict({
            'interface': IFACE,
            'name': 'Online',
            'signatures': ['b'],
            'op': '==',
        })


def test_rules_keep_independent_signature_contracts_across_batches():
    narrow = PropertyFilter.all_of([Condition(ref('Count', ['q']), '==', 5)])
    wide = PropertyFilter.all_of([Condition(ref('Count', ['x']), '==', 5)])

    first = narrow.evaluate({key('Count'): 5})
    second = wide.evaluate({key('Count'): 5})
    assert first.verdict is FilterVerdict.MATCH
    assert second.verdict is FilterVerdict.MATCH

    # Re-evaluating either rule repeatedly must not carry state over.
    for _ in range(3):
        assert narrow.evaluate().verdict is FilterVerdict.NEED_READ
        assert wide.evaluate({key('Count'): 5}).verdict is FilterVerdict.MATCH

    # The two contracts remain distinct objects on their respective rules.
    assert narrow.references[key('Count')].signatures == ('q',)
    assert wide.references[key('Count')].signatures == ('x',)


def test_nested_short_circuit_does_not_leak_irrelevant_evidence():
    # AND where the OR child is already matched by one branch; the OR's
    # pending sibling must not be requested once the whole AND is settled.
    rule = PropertyFilter.all_of([
        Condition(ref('Online', ['b']), '==', False),
        PropertyFilter.any_of([
            Condition(ref('Ready', ['b']), '==', True),
            Condition(ref('Pending', ['b']), '==', True),
        ]),
    ])
    # First term fails while the OR is entirely unresolved: the exclusion is
    # settled, so neither OR property may be requested.
    result = rule.evaluate({key('Online'): True})
    assert result.verdict is FilterVerdict.EXCLUDE
    assert result.missing == []

    # The OR itself matches on Ready; Pending stays unread but is irrelevant.
    rule = PropertyFilter.all_of([
        Condition(ref('Online', ['b']), '==', True),
        PropertyFilter.any_of([
            Condition(ref('Ready', ['b']), '==', True),
            Condition(ref('Pending', ['b']), '==', True),
        ]),
    ])
    result = rule.evaluate({key('Online'): True, key('Ready'): True})
    assert result.verdict is FilterVerdict.MATCH
    assert result.missing == []


def test_required_reads_lists_every_dependency_once():
    rule = PropertyFilter.all_of([
        Condition(ref('Online', ['b']), '==', True),
        PropertyFilter.any_of([
            Condition(ref('Count', ['i']), '>', 0),
            Condition(ref('Online', ['b']), '!=', False),
        ]),
    ])
    assert sorted(rule.required_reads()) == sorted([key('Online'), key('Count')])
