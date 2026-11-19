from binascii import unhexlify
from decimal import Decimal
from fractions import Fraction

import pytest

import cbor2
from cbor2 import CBORTag
from cbor2._types import CBORDecodeValueError
from cbor2.fixedpoint import (
    BigFloat,
    ColumnReport,
    DecimalFraction,
    NoCommonRepresentationError,
    RawRational,
    loads_exact,
    plan_fixed_point_column,
)


def plan(values, **kwargs):
    kwargs.setdefault("bases", [10])
    kwargs.setdefault("min_scale", 0)
    kwargs.setdefault("max_scale", 6)
    kwargs.setdefault("bit_widths", [8, 16, 32, 64])
    report = plan_fixed_point_column(values, **kwargs)
    assert isinstance(report, ColumnReport)
    return report


class TestCommonPlan:
    def test_integers_and_decimals(self):
        report = plan([12, Decimal("1.5"), -7])
        assert report.ok
        assert report.plan.base == 10
        assert report.plan.scale == 1
        assert report.plan.bit_width == 8  # mantissas 120, 15, -70 fit in int8
        assert report.plan.mantissas() == [120, 15, -70]
        assert report.plan.verify()

    def test_scale_is_minimal(self):
        report = plan([Decimal("1.5")], max_scale=5)
        assert report.plan.scale == 1

    def test_zero_values_any_scale(self):
        report = plan([0, Decimal("0.00"), Fraction(0, 7), 5])
        assert report.ok
        assert report.plan.mantissas() == [0, 0, 0, 5]
        assert report.plan.scale == 0

    def test_negative_scale_allows_shrinking(self):
        report = plan([1000], bases=[10], min_scale=-3, max_scale=3, bit_widths=[2, 8])
        assert report.ok
        assert report.plan.scale == -3
        assert report.plan.mantissas() == [1]
        assert report.plan.bit_width == 2

    def test_minimal_width_beats_base_order(self):
        # base 2 is listed first, but base 10 can shrink 1000 = 10**3 to a
        # 2-bit mantissa while base 2 can only reach 125 (8 bits)
        report = plan([1000], bases=[2, 10], min_scale=-3, max_scale=3, bit_widths=[2, 8, 16])
        assert report.ok
        assert report.plan.bit_width == 2
        assert report.plan.base == 10
        assert report.plan.mantissas() == [1]

    def test_base_order_breaks_ties(self):
        first = plan([3], bases=[2, 10], bit_widths=[3, 8])
        second = plan([3], bases=[10, 2], bit_widths=[3, 8])
        assert first.plan.bit_width == second.plan.bit_width == 3
        assert first.plan.base == 2
        assert second.plan.base == 10

    def test_rationals_share_a_scale(self):
        report = plan([Fraction(1, 2), Fraction(1, 5), 3])
        assert report.ok
        assert report.plan.base == 10
        assert report.plan.scale == 1
        assert report.plan.mantissas() == [5, 2, 30]
        assert report.plan.verify()

    def test_bigfloat_binary_scale(self):
        report = plan([BigFloat(3, -2)], bases=[2])
        assert report.ok
        assert report.plan.scale == 2
        assert report.plan.mantissas() == [3]

    def test_bigfloat_decimal_base(self):
        report = plan([BigFloat(3, -2)], bases=[10])
        assert report.ok
        assert report.plan.scale == 2
        assert report.plan.mantissas() == [75]  # 0.75 = 75 * 10**-2

    def test_value_of_round_trip(self):
        originals = [Decimal("2.5"), Fraction(1, 4), 3]
        report = plan(originals)
        assert report.ok
        for entry, original in zip(report.plan.entries, originals):
            assert report.plan.value_of(entry) == Fraction(original)

    def test_entries_preserve_original_structure(self):
        report = plan([Decimal("1.5"), BigFloat(3, -2), Fraction(2, 5), 7], bases=[10])
        kinds = [entry.kind for entry in report.plan.entries]
        assert kinds == ["decimal_fraction", "bigfloat", "rational", "integer"]
        decimal_entry, bigfloat_entry, rational_entry, _ = report.plan.entries
        assert decimal_entry.exponent == -1
        assert bigfloat_entry.exponent == -2
        assert (rational_entry.numerator, rational_entry.denominator) == (2, 5)
        assert report.plan.entries[0].original == Decimal("1.5")


class TestBlockers:
    def test_denominator_prime_not_in_base(self):
        report = plan([Fraction(1, 3), 2], bases=[2, 10], bit_widths=[8, 16])
        assert not report.ok
        assert report.plan is None
        (blocker,) = report.blockers
        assert blocker.category == "no_common_scale"
        assert blocker.index == 0
        assert blocker.kind == "rational"
        assert len(blocker.base_failures) == 2
        for failure in blocker.base_failures:
            assert "3" in failure.reason

    def test_no_rounding_to_fit(self):
        # 0.1 = 1/10 has an infinite binary expansion; it must not be rounded
        report = plan([Decimal("0.1")], bases=[2])
        assert not report.ok
        (blocker,) = report.blockers
        assert blocker.category == "no_common_scale"
        assert "5" in blocker.base_failures[0].reason

    def test_scale_range_exceeded(self):
        report = plan([Decimal("0.001")], bases=[10], max_scale=2)
        assert not report.ok
        (blocker,) = report.blockers
        assert blocker.category == "no_common_scale"
        assert "scale >= 3" in blocker.base_failures[0].reason

    def test_out_of_range(self):
        report = plan([2**70], bases=[10], bit_widths=[8, 64])
        assert not report.ok
        (blocker,) = report.blockers
        assert blocker.category == "out_of_range"
        assert "72" in blocker.base_failures[0].reason

    def test_illegal_denominator_is_separate(self):
        report = plan([RawRational(1, 0), 10**30, 5], bases=[10], bit_widths=[8])
        assert not report.ok
        categories = {blocker.category for blocker in report.blockers}
        assert categories == {"illegal_denominator", "out_of_range"}
        illegal, overflow = report.blockers
        assert illegal.index == 0
        assert "zero denominator" in illegal.reason
        assert overflow.index == 1

    def test_non_integer_denominator_is_illegal(self):
        report = plan([RawRational(1, 2.5)], bases=[10])
        assert not report.ok
        assert report.blockers[0].category == "illegal_denominator"

    def test_negative_denominator_is_normalized(self):
        report = plan([RawRational(3, -4)], bases=[10])
        assert report.ok
        assert report.plan.mantissas() == [-75]
        assert report.plan.scale == 2

    def test_non_finite_decimal(self):
        report = plan([Decimal("NaN"), Decimal("Infinity"), 1], bases=[10])
        assert not report.ok
        assert {b.category for b in report.blockers} == {"non_finite"}
        assert [b.index for b in report.blockers] == [0, 1]

    def test_conflicting_bases_list_all_blockers(self):
        # 1/5 only works in base 10, 1/2**... only in base 2: no common base
        report = plan([Fraction(1, 5), BigFloat(1, -3)], bases=[2, 3], bit_widths=[16])
        assert not report.ok
        assert len(report.blockers) == 2
        assert all(b.category == "no_common_scale" for b in report.blockers)

    def test_unwrap_raises_with_blockers(self):
        report = plan([Fraction(1, 3)], bases=[10])
        with pytest.raises(NoCommonRepresentationError) as exc:
            report.unwrap()
        assert exc.value.blockers == report.blockers
        assert "illegal" not in str(exc.value)


class TestHugeExponents:
    def test_huge_positive_exponent_out_of_range_fast(self):
        # 1 * 2**(10**9) cannot be materialized; the decision is purely
        # exponent arithmetic
        report = plan([BigFloat(1, 10**9), 5], bases=[2], bit_widths=[8, 64])
        assert not report.ok
        (blocker,) = report.blockers
        assert blocker.category == "out_of_range"
        assert blocker.index == 0

    def test_huge_negative_exponent_fits_at_huge_scale(self):
        report = plan(
            [DecimalFraction(1, -(10**9))],
            bases=[10],
            max_scale=10**9,
            bit_widths=[2, 8],
        )
        assert report.ok
        assert report.plan.scale == 10**9
        assert report.plan.mantissas() == [1]
        assert report.plan.bit_width == 2

    def test_huge_negative_exponent_blocked_by_scale_range(self):
        report = plan([DecimalFraction(1, -(10**9))], bases=[10], max_scale=100)
        assert not report.ok
        (blocker,) = report.blockers
        assert blocker.category == "no_common_scale"
        assert "1000000000" in blocker.base_failures[0].reason

    def test_huge_decimal_exponent_preserved(self):
        # Decimal itself cannot even hold this exponent
        value = DecimalFraction(7, 10**30)
        report = plan([value], bases=[10], bit_widths=[8, 16])
        assert not report.ok
        (blocker,) = report.blockers
        assert blocker.category == "out_of_range"
        assert blocker.value is value


class TestExactDecoder:
    def test_decimal_fraction_tag(self):
        value = loads_exact(unhexlify("c48221196ab3"))
        assert value == DecimalFraction(27315, -2)

    def test_bigfloat_tag(self):
        value = loads_exact(unhexlify("c5822003"))
        assert value == BigFloat(3, -1)

    def test_bigfloat_huge_exponent_not_expanded(self):
        # tag 5, [2**64, 1]: the default decoder would try to compute 2**2**64
        payload = unhexlify("c582c24901000000000000000001")
        value = loads_exact(payload)
        assert value == BigFloat(1, 2**64)

    def test_rational_tag(self):
        assert loads_exact(unhexlify("d81e820205")) == Fraction(2, 5)

    def test_rational_zero_denominator_preserved(self):
        assert loads_exact(unhexlify("d81e820100")) == RawRational(1, 0)

    def test_rational_invalid_payload(self):
        with pytest.raises(CBORDecodeValueError, match="not a tuple"):
            loads_exact(unhexlify("d81e01"))

    def test_nested_values_use_exact_decoder(self):
        value = loads_exact(unhexlify("82c582200301"))
        assert value == [BigFloat(3, -1), 1]

    def test_plain_values_pass_through(self):
        assert loads_exact(unhexlify("820203")) == [2, 3]

    def test_decoded_column_plans_losslessly(self):
        payload = unhexlify("83c48221196ab3c5822003d81e820102")
        column = loads_exact(payload)
        report = plan(column, bases=[10], bit_widths=[16, 32])
        assert report.ok
        assert report.plan.scale == 2
        assert report.plan.mantissas() == [27315, 150, 50]
        assert report.plan.verify()


class TestCBORTagInputs:
    def test_tags_accepted_directly(self):
        values = [
            CBORTag(4, (-2, 27251)),
            CBORTag(5, (-1, 3)),
            CBORTag(30, (1, 2)),
            CBORTag(2, b"\x01\x00"),
        ]
        report = plan(values, bases=[10], bit_widths=[8, 16])
        assert report.ok
        assert report.plan.scale == 2
        assert report.plan.mantissas() == [27251, 150, 50, 25600]
        assert report.plan.bit_width == 16
        kinds = [entry.kind for entry in report.plan.entries]
        assert kinds == ["decimal_fraction", "bigfloat", "rational", "integer"]

    def test_tag30_zero_denominator_is_illegal(self):
        report = plan([CBORTag(30, (1, 0))], bases=[10])
        assert not report.ok
        assert report.blockers[0].category == "illegal_denominator"


class TestValidation:
    def test_empty_bases(self):
        with pytest.raises(ValueError, match="base"):
            plan([1], bases=[])

    def test_base_too_small(self):
        with pytest.raises(ValueError, match=">= 2"):
            plan([1], bases=[1])

    def test_empty_widths(self):
        with pytest.raises(ValueError, match="bit width"):
            plan([1], bit_widths=[])

    def test_inverted_scale_range(self):
        with pytest.raises(ValueError, match="min_scale"):
            plan([1], min_scale=3, max_scale=1)

    def test_unsupported_type(self):
        with pytest.raises(TypeError, match="unsupported type"):
            plan(["nope"])

    def test_unsupported_tag(self):
        with pytest.raises(TypeError, match="unsupported CBOR tag"):
            plan([CBORTag(99, 1)])


def test_cbor2_package_still_imports():
    assert cbor2.loads(b"\x02") == 2
