Service-line normalization
==========================

A billed service line carries a quantity and a **Unit or Basis for
Measurement Code** (X12 data element 355, e.g. SV103 on a professional claim
or SV204 on an institutional claim). :mod:`pyx12.serviceline` turns those
raw values into a normalized, dimension-safe detail line and gives the exact
conversion or proration basis used to get there.

Qualifiers and dimensions
-------------------------

The qualifier alone determines the billing dimension; nothing is inferred
from a charge amount or unit rate.

====  ==========  ===============  =====================================
Code  Dimension   Name             Allowed conversions
====  ==========  ===============  =====================================
UN    count       Unit             none — counts are never converted
MJ    time        Minute           MJ <-> HR within the time dimension
HR    time        Hour             MJ <-> HR within the time dimension
DA    day         Day              none — days are never converted
====  ==========  ===============  =====================================

``UN``, ``MJ`` and ``DA`` are the qualifiers admitted by the 837 service-line
segments; ``HR`` is the generic DE 355 hour code and provides the explicit
same-dimension target for minute-to-hour conversion.

Conversions stay inside one dimension: minute-to-hour multiplies by
``60/3600``, hour-to-minute by ``3600/60``. Converting across dimensions
(count -> time, time -> day, ...) is refused with an error rather than
guessed, so quantities with different units are never mixed.

Normalizing a line
------------------

.. code-block:: python

    from pyx12.serviceline import normalize_service_line

    line = normalize_service_line(
        "claim-123:line-1",
        qualifier="MJ",
        quantity="90",
        service_date="20260115",
        target_qualifier="HR",
    )
    line.normalized_quantity  # Decimal('1.5')
    line.normalized_unit      # 'HR'
    line.conversion.rule      # 'time-unit conversion: multiply by 60/3600'

Dates use the X12 eight-digit form (``YYYYMMDD``) or a ``datetime.date``.
A line with only ``service_date`` is a single-day line; supplying
``end_date`` makes it an inclusive closed range.

Cross-day proration
-------------------

A line spanning a date range is split across the inclusive calendar days.
The split is even, and any indivisible remainder is carried on the first
day so the daily pieces always sum back to the original quantity exactly:

.. code-block:: python

    line = normalize_service_line(
        "claim-123:line-2",
        qualifier="MJ",
        quantity="10",
        service_date="20260101",
        end_date="20260103",
    )
    line.calendar_days                       # 3
    [d.quantity for d in line.daily_lines]   # [Decimal('4'), Decimal('3'), Decimal('3')]

The inclusive day count and remainder rule are recorded on every
``ProratedLine``. Pass ``prorate=False`` to keep the whole quantity on the
range without a daily split.

Incomplete input is reported, not guessed
-----------------------------------------

Normalization never reverse-engineers a missing piece from price:

- a missing or blank qualifier -> ``missing_unit``,
- an unrecognized qualifier -> ``unknown_qualifier``,
- a non-numeric, negative or non-finite quantity -> ``invalid_quantity``,
- a malformed or impossible date, or an end date without a start ->
  ``incomplete_date``,
- an end date before the start -> ``date_order``,
- a target qualifier outside the source dimension ->
  ``cross_dimension_conversion``.

A line with any error carries no normalized quantity and no daily lines, so
downstream code can branch on the ``errors`` tuple instead of catching
exceptions. Batch a group of lines with
:func:`pyx12.serviceline.normalize_service_lines`.
