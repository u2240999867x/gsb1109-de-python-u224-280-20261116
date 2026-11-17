.. _retention:

Retention Planning
==================

:mod:`dateparser.retention` turns a declarative retention policy into auditable
reclaim windows. It is meant for data-retention bookkeeping, such as deciding
when collected originals and the artifacts derived from them may be reclaimed,
and it is separate from date parsing.

Policies are supplied by the caller, as are the business calendars. The planner
never guesses a holiday, a weekend or a settlement date.

.. code-block:: python

    from dateparser.retention import plan_from_dict

    retention_input = {
        "calendar": {
            "id": "apac-ops-2026",
            "timezone": "Asia/Shanghai",
            "holidays": {"2026-10-01": "National Day"},
            "working_days": {"2026-10-10": "adjusted working Saturday"},
        },
        "policy": {
            "rules": [
                {"id": "R1", "raw_text": "次月月底", "kind": "next_month_end"},
                {"id": "R2", "raw_text": "采集后30天", "kind": "calendar_days", "days": 30},
                {
                    "id": "R3",
                    "raw_text": "结清后第三个工作日",
                    "kind": "settlement_business_days",
                    "days": 3,
                },
            ]
        },
        "materials": [
            {
                "id": "orig-1001",
                "project": "retail",
                "collected_at": "2026-09-29T14:20:00+08:00",
                "settlement_at": "2026-11-13T17:40:00+08:00",
            }
        ],
    }

    plan = plan_from_dict(retention_input, as_of="2026-11-18T00:00:00+08:00")
    plan.reclaimable()
    plan.materials["orig-1001"].reclaim_from

Rule semantics
--------------

``next_month_end``
    The window closes at the end of the month after the collection month. The
    last day is kept as a calendar date, so the window ends at the first instant
    of the following day in the calendar timezone. A material may give an
    explicit ``collection_period`` (``YYYY-MM``) when the collection month is
    known independently of the timestamp; this does not change ``collected_at``.

``calendar_days``
    The window closes ``days`` calendar days after collection, keeping the local
    wall-clock time. A daylight-saving change may move the UTC offset, so these
    windows are not ``days * 24`` hours. The business calendar is not consulted.

``settlement_business_days``
    The window closes ``days`` business days after settlement, counted on the
    supplied business calendar (working weekdays, holidays and extra working
    days). When a material has no settlement anchor the window stays
    ``pending``: there is no fallback to the collection timestamp. The plan lists
    the counted business days and every closed day it skipped.

Derived materials and shared originals
--------------------------------------

A material lists the materials it was derived from in ``depends_on``. A derived
artifact keeps its original alive, so an original's effective window is the
latest of its own requirements, the requirements of every project it is
explicitly shared with, and the effective window of every dependent material.

An original is a shared original when it is used by more than one project,
either through ``shared_with`` or because a derived material from another
project depends on it. A shared original only enters ``plan.reclaimable()`` once
every effective requirement has been released. If any requirement is pending,
the original is reported as pending rather than reclaimable.

Status values
-------------

``reclaimable``
    Every requirement is resolved and the latest window has closed by ``as_of``.
``held``
    Every requirement is resolved but at least one window is still open.
``pending``
    At least one requirement is unresolved, typically because a settlement
    anchor is missing.

Policy corrections
------------------

A correction is planned, not applied. :func:`plan_correction` compares the
current policy with a corrected one and reports the affected materials, their
old and new windows, and which rule governs each result. Collection timestamps
are never rewritten: a correction that tries to change ``collected_at`` or
``settlement_at`` is rejected and reported in ``rejected_changes``.

.. code-block:: python

    from dateparser.retention import correction_from_dict

    report = correction_from_dict(
        retention_input,
        {"rules": [{"id": "R3", "kind": "settlement_business_days", "days": 4}]},
        as_of="2026-11-18T00:00:00+08:00",
    )
    report.affected_materials()

Command line
------------

The same plans can be produced from JSON files::

    $ python -m dateparser.retention plan --input input.json --output plan.json
    $ python -m dateparser.retention correct --input input.json --changes fix.json

``--as-of`` and ``--generated-at`` accept ISO 8601 timestamps with a UTC offset.
Malformed input exits with status ``2`` and a message on standard error.
