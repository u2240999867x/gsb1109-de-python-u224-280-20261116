# emacs: -*- mode: python-mode; py-indent-offset: 4; indent-tabs-mode: nil -*-
# vi: set ft=python sts=4 ts=4 sw=4 et:
### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
#
#   See COPYING file distributed along with the NiBabel package for the
#   copyright and license terms.
#
### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
"""Tests for NIfTI statistic image value contracts"""

import numpy as np
import pytest

from .. import Nifti1Image, Nifti2Image
from .. import statcontracts as sc
from ..analyze import AnalyzeImage


def _image(values, intent=None, params=(), shape=None, allow_unknown=False):
    data = np.asarray(values, dtype=np.float64)
    if shape is not None:
        data = data.reshape(shape)
    img = Nifti1Image(data, np.eye(4))
    if intent is not None:
        img.header.set_intent(intent, params, allow_unknown=allow_unknown)
    return img


# ---------------------------------------------------------------------------
# Contract construction
# ---------------------------------------------------------------------------


def test_contract_intent_to_statistic_mapping():
    # Only the intent code decides the statistic; the value range never does.
    cases = {
        't test': ('t', (10,)),
        'f test': ('F', (2, 10)),
        'z score': ('z', ()),
        'chi2': ('chi2', (5,)),
        'p value': ('p', ()),
        'log p value': ('log_p', ()),
        'log10 p value': ('log10_p', ()),
    }
    for intent, (statistic, params) in cases.items():
        contract = sc.build_contract(_image([1.0], intent, params))
        assert contract.statistic == statistic
        assert contract.intent_name == ''
        assert contract.is_valid()


def test_contract_plain_intensity_image():
    img = _image(np.arange(8.0), shape=(2, 2, 2))
    contract = sc.build_contract(img, name='anatomy')
    assert contract.name == 'anatomy'
    assert contract.intent_code == 0
    assert contract.statistic is None
    assert contract.scalar_intent is False
    assert contract.is_valid()
    assert contract.convertible_to == ()
    assert contract.value_range == (0.0, 7.0)


def test_contract_records_parameters_and_availability():
    img = _image([1.0], 't test', (10,))
    contract = sc.build_contract(img)
    p1, p2, p3 = contract.parameters
    assert p1.name == 'intent_p1'
    assert p1.value == 10.0
    assert p1.available is True
    assert 'DOF' in p1.description
    assert p2.available is False and p3.available is False


def test_contract_t_map_without_dof_is_not_significance_ready():
    # A t map missing its degrees of freedom must not be presented as having
    # a known significance probability.
    img = _image([2.3], 't test')
    contract = sc.build_contract(img)
    assert contract.statistic == 't'
    assert contract.is_valid() is False
    assert 'missing_parameters' in contract.issue_codes
    assert contract.convertible_to == ()
    result = sc.apply_conversion(img, sc.ConversionRule('p value'))
    assert result.accepted is False
    assert result.reasons == ('missing_parameters',)
    assert result.data is None


def test_contract_invalid_and_missing_f_dofs():
    missing = sc.build_contract(_image([1.0], 'f test'))
    assert missing.is_valid() is False
    assert missing.issue_codes.count('missing_parameters') == 2

    negative = sc.build_contract(_image([1.0], 'f test', (2, -4)))
    assert negative.is_valid() is False
    assert 'invalid_parameters' in negative.issue_codes


def test_contract_unknown_intent_code_is_reported_not_guessed():
    # Values in the "typical z range" must not lead to a z interpretation.
    img = _image([-1.96, 0.0, 1.96], 9999, allow_unknown=True)
    contract = sc.build_contract(img)
    assert contract.statistic is None
    assert contract.intent_label == 'unknown code 9999'
    assert contract.issue_codes == ('unknown_intent',)
    assert contract.is_valid() is False
    assert contract.convertible_to == ()
    result = sc.apply_conversion(img, sc.ConversionRule('p value'))
    assert result.accepted is False
    assert 'unknown_intent' in result.reasons


def test_contract_scalar_intent_with_multiple_components_conflicts():
    img = _image(np.zeros(24), 't test', (10,), shape=(2, 2, 2, 3))
    contract = sc.build_contract(img)
    assert contract.n_components == 3
    assert contract.scalar_intent is True
    assert contract.issue_codes == ('scalar_intent_with_components',)
    assert contract.is_valid() is False


def test_contract_vector_intent_allows_multiple_components():
    img = _image(np.zeros(24), 'vector', shape=(2, 2, 2, 3))
    contract = sc.build_contract(img)
    assert contract.n_components == 3
    assert contract.scalar_intent is False
    assert contract.is_valid()


def test_contract_value_range_checks_are_warnings():
    f_contract = sc.build_contract(_image([-1.0, 2.0], 'f test', (2, 10)))
    assert 'out_of_range' in f_contract.issue_codes
    # Range violations do not block conversions on their own.
    assert f_contract.is_valid()

    p_contract = sc.build_contract(_image([0.5, 1.2], 'p value'))
    assert 'out_of_range' in p_contract.issue_codes
    assert p_contract.value_range == (0.5, 1.2)


def test_contract_non_finite_values():
    nan_img = _image([1.0, np.nan], 'z score')
    nan_contract = sc.build_contract(nan_img)
    assert nan_contract.has_nan is True
    assert nan_contract.finite is False
    assert 'non_finite' in nan_contract.issue_codes
    assert nan_contract.is_valid()  # warning only, not an error
    result = sc.apply_conversion(nan_img, sc.ConversionRule('p value'))
    assert result.accepted is False
    assert result.reasons == ('non_finite',)

    inf_img = _image([1.0, np.inf], 'z score')
    assert sc.build_contract(inf_img).has_inf is True


def test_contract_identity_distinguishes_statistics_and_parameters():
    t_img = _image([1.0], 't test', (10,))
    other_dof = _image([1.0], 't test', (20,))
    z_img = _image([1.0], 'z score')
    t_identity = sc.contract_identity(sc.build_contract(t_img))
    assert t_identity != sc.contract_identity(sc.build_contract(other_dof))
    assert t_identity != sc.contract_identity(sc.build_contract(z_img))
    # Same intent, same parameters, same component count -> same identity even
    # when the caller gives the images identical names.
    t_named = sc.build_contract(t_img, name='result')
    t_dup = sc.build_contract(_image([2.0], 't test', (10,)), name='result')
    assert sc.contract_identity(t_named) == sc.contract_identity(t_dup)


def test_contract_rejects_non_nifti_images():
    with pytest.raises(TypeError, match='NIfTI'):
        sc.build_contract(AnalyzeImage(np.zeros((2, 2, 2)), np.eye(4)))


def test_contract_supports_nifti2():
    img = Nifti2Image(np.ones((2, 2, 2)), np.eye(4))
    img.header.set_intent('f test', (2, 10))
    contract = sc.build_contract(img)
    assert contract.statistic == 'F'
    assert contract.parameters[0].value == 2.0
    assert contract.is_valid()


# ---------------------------------------------------------------------------
# Distribution math
# ---------------------------------------------------------------------------


def test_betainc_known_values():
    x = np.array([0.0, 0.5, 1.0])
    # I_x(2, 3): 3x^2 - 2x^3 -> I_0.5 = 0.6875
    np.testing.assert_allclose(sc._betainc(2.0, 3.0, x), [0.0, 0.6875, 1.0], atol=1e-12)


def test_t_survival_known_critical_values():
    # Two-tailed p for t(10) = 2.228 is 0.05; t = 0 gives p = 1.
    p = sc._t_sf(np.array([0.0, 2.22813885]), 10.0)
    np.testing.assert_allclose(p, [1.0, 0.05], atol=1e-8)


def test_f_survival_known_critical_values():
    # F(2, 10) = 4.1028 -> p = 0.05; F = 0 -> p = 1.
    p = sc._f_sf(np.array([0.0, 4.10282102]), 2.0, 10.0)
    np.testing.assert_allclose(p, [1.0, 0.05], atol=1e-8)


def test_z_two_sided_p():
    p = sc._norm_two_sided_p(np.array([0.0, 1.95996398, -1.95996398]))
    np.testing.assert_allclose(p, [1.0, 0.05, 0.05], atol=1e-8)


# ---------------------------------------------------------------------------
# Conversions
# ---------------------------------------------------------------------------


def test_t_map_conversion_uses_dof_and_keeps_original():
    data = np.array([0.0, 2.22813885, -2.22813885], dtype=np.float64)
    img = _image(data, 't test', (10,))
    result = sc.apply_conversion(img, sc.ConversionRule('p value'))
    assert result.accepted
    assert result.data.dtype == np.float64
    np.testing.assert_allclose(result.data, [1.0, 0.05, 0.05], atol=1e-8)
    # Original voxel values are retained untouched.
    np.testing.assert_array_equal(result.source_data, data)
    np.testing.assert_array_equal(np.asanyarray(img.dataobj), data)


def test_f_and_z_map_conversions():
    f_img = _image([4.10282102], 'f test', (2, 10))
    f_result = sc.apply_conversion(f_img, sc.ConversionRule('p value'))
    assert f_result.accepted
    np.testing.assert_allclose(f_result.data, [0.05], atol=1e-8)

    z_img = _image([1.95996398], 'z score')
    for column, expected in (
        ('p value', 0.05),
        ('log p value', np.log(0.05)),
        ('log10 p value', np.log10(0.05)),
    ):
        result = sc.apply_conversion(
            z_img,
            sc.ConversionRule(column, value_range=(-50.0, 1.0)),
        )
        assert result.accepted
        np.testing.assert_allclose(result.data, [expected], atol=1e-8)


def test_probability_map_conversions():
    p_img = _image([0.05, 0.5], 'p value')
    logp = sc.apply_conversion(p_img, sc.ConversionRule('log p value', value_range=(-50.0, 0.0)))
    assert logp.accepted
    np.testing.assert_allclose(logp.data, np.log([0.05, 0.5]), atol=1e-12)

    logp_img = _image([np.log(0.05)], 'log p value')
    back = sc.apply_conversion(logp_img, sc.ConversionRule('p value'))
    assert back.accepted
    np.testing.assert_allclose(back.data, [0.05], atol=1e-12)

    log10p_img = _image([-2.0], 'log10 p value')
    back10 = sc.apply_conversion(log10p_img, sc.ConversionRule('p value'))
    assert back10.accepted
    np.testing.assert_allclose(back10.data, [0.01], atol=1e-12)


def test_conversion_precision_and_tolerance():
    img = _image([2.22813885], 't test', (10,))
    tight = sc.apply_conversion(
        img, sc.ConversionRule('p value', precision='float32', tolerance=1e-12)
    )
    # float32 rounding of ~0.05 exceeds a 1e-12 tolerance.
    assert tight.accepted is False
    assert tight.reasons == ('tolerance_exceeded',)
    assert tight.data is None
    assert tight.max_error > 1e-12

    relaxed = sc.apply_conversion(
        img, sc.ConversionRule('p value', precision='float32', tolerance=1e-6)
    )
    assert relaxed.accepted
    assert relaxed.data.dtype == np.float32
    assert relaxed.max_error <= 1e-6


def test_conversion_rejects_values_outside_target_range():
    # A custom rule declaring an output range that cannot hold the p values.
    img = _image([5.0], 'z score')
    result = sc.apply_conversion(img, sc.ConversionRule('p value', value_range=(0.5, 1.0)))
    assert result.accepted is False
    assert result.reasons == ('out_of_target_range',)


def test_conversion_without_registered_rule_is_rejected():
    img = _image([1.0], 'chi2', (5,))
    result = sc.apply_conversion(img, sc.ConversionRule('p value'))
    assert result.accepted is False
    assert result.reasons == ('no_converter',)


def test_custom_converter_on_rule():
    img = _image([1.0, 2.0], 'z score')
    rule = sc.ConversionRule(
        'doubled',
        converter=lambda values: values * 2.0,
        value_range=(0.0, 4.0),
    )
    result = sc.apply_conversion(img, rule)
    assert result.accepted
    np.testing.assert_array_equal(result.data, [2.0, 4.0])


def test_register_converter_decorator_restores_registry():
    img = _image([1.0], 'chi2', (5,))
    key = ('chi2', 'p value')
    saved = sc.CONVERTERS.get(key)
    try:

        @sc.register_converter('chi2', 'p value')
        def chi2_sf(values):
            return np.exp(-np.asarray(values) / 2.0)

        assert sc.CONVERTERS[key] is chi2_sf
        result = sc.apply_conversion(img, sc.ConversionRule('p value'))
        assert result.accepted
        np.testing.assert_allclose(result.data, [np.exp(-0.5)], atol=1e-12)
    finally:
        if saved is None:
            sc.CONVERTERS.pop(key, None)
        else:
            sc.CONVERTERS[key] = saved


def test_source_identity_mismatch_raises():
    t_img = _image([1.0], 't test', (10,))
    f_img = _image([1.0], 'f test', (2, 10))
    identity = sc.contract_identity(sc.build_contract(t_img))
    with pytest.raises(ValueError, match='identity'):
        sc.apply_conversion(f_img, sc.ConversionRule('p value', source_identity=identity))


def test_available_targets_reflect_contract_state():
    assert sc.build_contract(_image([1.0], 'z score')).convertible_to == (
        'log p value',
        'log10 p value',
        'p value',
    )
    assert sc.build_contract(_image([1.0], 't test', (10,))).convertible_to == (
        'log p value',
        'log10 p value',
        'p value',
    )
    assert sc.build_contract(_image([1.0], 't test')).convertible_to == ()


def test_rejected_result_raises_on_demand():
    img = _image([1.0], 't test')
    result = sc.apply_conversion(img, sc.ConversionRule('p value'))
    with pytest.raises(ValueError, match='rejected'):
        result.raise_if_rejected()


# ---------------------------------------------------------------------------
# Rule and target column validation
# ---------------------------------------------------------------------------


def test_rule_and_target_validation():
    with pytest.raises(ValueError, match='floating point'):
        sc.TargetColumn('x', 'int32', (0.0, 1.0), 1e-9)
    with pytest.raises(ValueError, match='Tolerance'):
        sc.ConversionRule('x', tolerance=-1.0)
    with pytest.raises(ValueError, match='range'):
        sc.ConversionRule('x', value_range=(1.0, 0.0))
    rule = sc.ConversionRule('p value', precision='float32')
    assert rule.precision == np.dtype('float32')
