from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, overload

from .conf import Settings, apply_settings
from .timezone_parser import pop_tz_declaration
from .timezone_resolution import resolve_wall_time, timezone_for_declaration
from .utils import strip_braces

if TYPE_CHECKING:
    from .timezone_resolution import TimezoneResolution


class DateParser:
    def parse(
        self,
        date_string: str,
        parse_method: Callable[
            ...,
            tuple[datetime, str | None] | tuple[datetime, str | None, tuple[str, ...]],
        ],
        settings: Settings | dict[str, Any] | None = None,
        date_order: str | None = None,
    ) -> tuple[datetime, str | None]:
        date_obj, period, _ = self._parse_with_parts(
            date_string, parse_method, settings=settings, date_order=date_order
        )
        return date_obj, period

    @overload
    def _parse_with_parts(
        self,
        date_string: str,
        parse_method: Callable[
            ...,
            tuple[datetime, str | None] | tuple[datetime, str | None, tuple[str, ...]],
        ],
        settings: Settings | dict[str, Any] | None = None,
        date_order: str | None = None,
        return_timezone: Literal[False] = False,
    ) -> tuple[datetime, str | None, tuple[str, ...]]: ...

    @overload
    def _parse_with_parts(
        self,
        date_string: str,
        parse_method: Callable[
            ...,
            tuple[datetime, str | None] | tuple[datetime, str | None, tuple[str, ...]],
        ],
        settings: Settings | dict[str, Any] | None = None,
        date_order: str | None = None,
        *,
        return_timezone: Literal[True],
    ) -> tuple[datetime, str | None, tuple[str, ...], "TimezoneResolution | None"]: ...

    @apply_settings
    def _parse_with_parts(
        self,
        date_string: str,
        parse_method: Callable[
            ...,
            tuple[datetime, str | None] | tuple[datetime, str | None, tuple[str, ...]],
        ],
        settings: Settings | dict[str, Any] | None = None,
        date_order: str | None = None,
        return_timezone: bool = False,
    ) -> tuple[Any, ...]:
        assert isinstance(settings, Settings)
        date_string = str(date_string)

        if not date_string.strip():
            raise ValueError("Empty string")

        date_string = strip_braces(date_string)
        date_string, declaration = pop_tz_declaration(date_string, settings=settings)

        result = parse_method(
            date_string,
            settings=settings,
            tz=timezone_for_declaration(declaration),
            date_order=date_order,
        )
        date_obj, period = result[:2]
        parts = result[2] if len(result) == 3 else ()

        date_obj, timezone = resolve_wall_time(date_obj, settings, declaration)

        if not settings.RETURN_AS_TIMEZONE_AWARE or (
            settings.RETURN_AS_TIMEZONE_AWARE
            and "default" == settings.RETURN_AS_TIMEZONE_AWARE
            and not declaration
        ):
            date_obj = date_obj.replace(tzinfo=None)

        if return_timezone:
            return date_obj, period, parts, timezone
        return date_obj, period, parts


date_parser = DateParser()
