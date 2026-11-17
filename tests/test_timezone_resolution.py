from datetime import datetime, timedelta
from typing import Any

import pytest
from pytz import UTC
from pytz import timezone as pytz_timezone

from dateparser import parse
from dateparser.conf import Settings, SettingValidationError
from dateparser.date import DateDataParser
from dateparser.timezone_resolution import (
    AmbiguousTimeError,
    LocalTimeCandidate,
    TimezoneConflictError,
    TimezoneDeclaration,
    TimezoneResolution,
    declaration_from_offset,
    declaration_from_token,
    localize,
    offset_name,
)


def parse_data(date_string: str, **settings: Any):
    return DateDataParser(settings=settings).get_date_data(date_string)


class TestDeclaration:
    def test_offset_declaration_keeps_its_text(self) -> None:
        declaration = declaration_from_token("UTC\\-05:00", timedelta(hours=-5))
        assert declaration == TimezoneDeclaration(
            value="UTC-05:00",
            kind="offset",
            source="date",
            offset=timedelta(hours=-5),
            name="UTC\\-05:00",
        )

    def test_abbreviation_declaration_keeps_its_text(self) -> None:
        declaration = declaration_from_token("CEST", timedelta(hours=2))
        assert declaration.value == "CEST"
        assert declaration.kind == "abbreviation"
        assert declaration.offset == timedelta(hours=2)

    def test_result_keeps_date_timezone_and_output(self) -> None:
        data = parse_data(
            "12 Feb 2015 10:56 PM EST",
            TIMEZONE="UTC",
            TO_TIMEZONE="Europe/Berlin",
            RETURN_AS_TIMEZONE_AWARE=True,
        )
        timezone = data.timezone
        assert timezone is not None
        assert timezone.declared == "EST"
        assert timezone.declaration is not None
        assert timezone.declaration.source == "date"
        assert timezone.applied_name == "EST"
        assert timezone.output_name == "Europe/Berlin"
        assert data.date_obj == datetime(2015, 2, 13, 4, 56, tzinfo=UTC).replace(
            tzinfo=data.date_obj.tzinfo
        )
        assert data.date_obj.utcoffset() == timedelta(hours=1)

    def test_original_local_date_survives_conversion(self) -> None:
        data = parse_data(
            "31 Dec 2014 11:30 PM EST",
            TIMEZONE="UTC",
            TO_TIMEZONE="Asia/Tokyo",
            RETURN_AS_TIMEZONE_AWARE=True,
        )
        assert data.date_obj == datetime(
            2015, 1, 1, 13, 30, tzinfo=data.date_obj.tzinfo
        )
        assert data.timezone is not None
        assert data.timezone.original_local_date == datetime(2014, 12, 31).date()
        assert data.timezone.original_local_datetime == datetime(2014, 12, 31, 23, 30)

    def test_date_without_timezone_has_no_declaration(self) -> None:
        data = parse_data("12 Feb 2015 10:56 PM")
        assert data.timezone is not None
        assert data.timezone.declared is None
        assert data.timezone.local == datetime(2015, 2, 12, 22, 56)

    def test_offset_name_formatting(self) -> None:
        assert offset_name(timedelta(hours=-5)) == "-05:00"
        assert offset_name(timedelta(hours=5, minutes=30)) == "+05:30"


class TestConflict:
    def test_conflict_is_reported(self) -> None:
        data = parse_data(
            "12 Feb 2015 10:56 PM EST",
            TIMEZONE="Europe/London",
            RETURN_AS_TIMEZONE_AWARE=True,
        )
        timezone = data.timezone
        assert timezone is not None
        assert timezone.conflict is not None
        assert "EST" in timezone.conflict
        assert "Europe/London" in timezone.conflict

    def test_default_policy_keeps_date_declaration(self) -> None:
        data = parse_data(
            "12 Feb 2015 10:56 PM EST",
            TIMEZONE="Europe/London",
            RETURN_AS_TIMEZONE_AWARE=True,
        )
        assert data.timezone is not None
        assert data.timezone.declared == "EST"
        assert data.timezone.applied_name == "EST"
        assert data.date_obj is not None
        assert data.date_obj.utcoffset() == timedelta(0)
        assert data.date_obj.hour == 3
        assert data.date_obj.minute == 56

    def test_settings_policy_uses_source_timezone(self) -> None:
        data = parse_data(
            "12 Feb 2015 10:56 PM EST",
            TIMEZONE="Europe/London",
            TIMEZONE_CONFLICT="settings",
            RETURN_AS_TIMEZONE_AWARE=True,
        )
        assert data.timezone is not None
        assert data.timezone.applied_name == "Europe/London"
        assert data.date_obj is not None
        assert data.date_obj.utcoffset() == timedelta(0)
        assert data.date_obj.hour == 22
        assert data.date_obj.minute == 56

    def test_error_policy_raises(self) -> None:
        with pytest.raises(TimezoneConflictError, match="EST"):
            parse(
                "12 Feb 2015 10:56 PM EST",
                settings={
                    "TIMEZONE": "Europe/London",
                    "TIMEZONE_CONFLICT": "error",
                },
            )

    def test_source_timezone_declares_the_source_zone(self) -> None:
        data = parse_data(
            "12 Feb 2015 10:56 PM",
            SOURCE_TIMEZONE="America/New_York",
            TIMEZONE="UTC",
            RETURN_AS_TIMEZONE_AWARE=True,
        )
        assert data.timezone is not None
        assert data.timezone.declared == "America/New_York"
        assert data.timezone.declaration is not None
        assert data.timezone.declaration.source == "settings"
        assert data.date_obj == datetime(
            2015, 2, 13, 3, 56, tzinfo=pytz_timezone("UTC")
        )

    def test_agreeing_declarations_do_not_conflict(self) -> None:
        data = parse_data(
            "12 Feb 2015 10:56 PM -0500",
            SOURCE_TIMEZONE="America/New_York",
            RETURN_AS_TIMEZONE_AWARE=True,
        )
        assert data.timezone is not None
        assert data.timezone.conflict is None


class TestDst:
    def test_ambiguous_time_is_refused_by_default(self) -> None:
        with pytest.raises(AmbiguousTimeError) as error:
            parse(
                "3 Nov 2024 1:30 AM",
                settings={
                    "TIMEZONE": "America/New_York",
                    "RETURN_AS_TIMEZONE_AWARE": True,
                },
            )
        assert error.value.status == "ambiguous"
        assert len(error.value.candidates) == 2
        assert {candidate.offset for candidate in error.value.candidates} == {
            timedelta(hours=-4),
            timedelta(hours=-5),
        }

    def test_ambiguous_time_earlier(self) -> None:
        data = parse_data(
            "3 Nov 2024 1:30 AM",
            TIMEZONE="America/New_York",
            AMBIGUOUS_TIME="earlier",
            RETURN_AS_TIMEZONE_AWARE=True,
        )
        assert data.date_obj is not None
        assert data.date_obj.utcoffset() == timedelta(hours=-4)
        assert data.timezone is not None
        assert data.timezone.is_ambiguous
        assert [c.utc for c in data.timezone.candidates] == sorted(
            c.utc for c in data.timezone.candidates
        )

    def test_ambiguous_time_later(self) -> None:
        data = parse_data(
            "3 Nov 2024 1:30 AM",
            TIMEZONE="America/New_York",
            AMBIGUOUS_TIME="later",
            RETURN_AS_TIMEZONE_AWARE=True,
        )
        assert data.date_obj is not None
        assert data.date_obj.utcoffset() == timedelta(hours=-5)
        assert data.timezone is not None
        assert data.timezone.is_ambiguous

    def test_nonexistent_time_is_refused_by_default(self) -> None:
        with pytest.raises(AmbiguousTimeError) as error:
            parse(
                "10 Mar 2024 2:30 AM",
                settings={
                    "TIMEZONE": "America/New_York",
                    "RETURN_AS_TIMEZONE_AWARE": True,
                },
            )
        assert error.value.status == "nonexistent"

    def test_nonexistent_time_choice_is_recorded(self) -> None:
        data = parse_data(
            "10 Mar 2024 2:30 AM",
            TIMEZONE="America/New_York",
            AMBIGUOUS_TIME="later",
            RETURN_AS_TIMEZONE_AWARE=True,
        )
        assert data.timezone is not None
        assert data.timezone.is_nonexistent
        assert data.date_obj is not None
        assert data.date_obj.utcoffset() == timedelta(hours=-4)

    def test_localize_returns_candidates_in_order(self) -> None:
        zone = pytz_timezone("Europe/Berlin")
        chosen, candidates, status = localize(
            datetime(2024, 10, 27, 2, 30), zone, "earlier"
        )
        assert status == "ambiguous"
        assert chosen.replace(tzinfo=None) == datetime(2024, 10, 27, 2, 30)
        assert chosen.utcoffset() == timedelta(hours=2)
        assert [candidate.offset for candidate in candidates] == [
            timedelta(hours=2),
            timedelta(hours=1),
        ]
        assert all(
            isinstance(candidate, LocalTimeCandidate) for candidate in candidates
        )

    def test_zone_name_dst_offset_is_followed(self) -> None:
        summer = parse_data(
            "1 Jul 2024 12:00",
            SOURCE_TIMEZONE="America/New_York",
            TIMEZONE="UTC",
            RETURN_AS_TIMEZONE_AWARE=True,
        )
        winter = parse_data(
            "1 Jan 2024 12:00",
            SOURCE_TIMEZONE="America/New_York",
            TIMEZONE="UTC",
            RETURN_AS_TIMEZONE_AWARE=True,
        )
        assert summer.date_obj is not None
        assert winter.date_obj is not None
        assert summer.date_obj.hour == 16
        assert winter.date_obj.hour == 17


class TestOtherParsers:
    def test_custom_format_offset_is_traceable(self) -> None:
        data = DateDataParser(
            settings={"TO_TIMEZONE": "UTC", "RETURN_AS_TIMEZONE_AWARE": True}
        ).get_date_data("26 Jan 99 16:32:21 +0100", ["%d %b %y %H:%M:%S %z"])
        assert data.timezone is not None
        assert data.timezone.declared == "UTC+01:00"
        assert data.timezone.original_local_datetime == datetime(
            1999, 1, 26, 16, 32, 21
        )
        assert data.date_obj == datetime(1999, 1, 26, 15, 32, 21, tzinfo=UTC)

    def test_relative_time_keeps_source_declaration(self) -> None:
        data = parse_data(
            "2 hours ago EST",
            TIMEZONE="UTC",
            RETURN_AS_TIMEZONE_AWARE=True,
        )
        assert data.timezone is not None
        assert data.timezone.declared == "EST"
        assert data.timezone.applied_offset == timedelta(hours=-5)

    def test_relative_time_with_fixed_timezone_has_resolution(self) -> None:
        data = parse_data(
            "2 hours ago",
            TIMEZONE="Asia/Karachi",
            RETURN_AS_TIMEZONE_AWARE=True,
        )
        assert data.timezone is not None
        assert data.timezone.declared == "Asia/Karachi"


class TestSettings:
    @pytest.mark.parametrize(
        ("settings", "message"),
        [
            ({"TIMEZONE_CONFLICT": "neither"}, '"neither" is not a valid value'),
            ({"AMBIGUOUS_TIME": "guess"}, '"guess" is not a valid value'),
        ],
    )
    def test_invalid_policy_is_rejected(
        self, settings: dict[str, Any], message: str
    ) -> None:
        with pytest.raises(SettingValidationError, match=message):
            DateDataParser(settings=settings)

    def test_policies_are_in_defaults(self) -> None:
        settings = Settings()
        assert settings.TIMEZONE_CONFLICT == "date"
        assert settings.AMBIGUOUS_TIME == "error"
        assert settings.SOURCE_TIMEZONE is False

    def test_resolution_type_is_public(self) -> None:
        assert TimezoneResolution().declared is None
        assert declaration_from_offset(timedelta(0)).value == "UTC+00:00"

    def test_ambiguous_time_is_still_a_value_error(self) -> None:
        # Existing code catches ValueError around parsing; the new policy
        # errors remain part of that family.
        assert issubclass(AmbiguousTimeError, ValueError)
        assert issubclass(TimezoneConflictError, ValueError)

    def test_utc_offset_token_keeps_its_offset_not_the_zone_lmt(self) -> None:
        data = parse_data("12 Feb 2015 10:56 PM EST", RETURN_AS_TIMEZONE_AWARE=True)
        assert data.date_obj is not None
        assert data.date_obj.utcoffset() == timedelta(hours=-5)
