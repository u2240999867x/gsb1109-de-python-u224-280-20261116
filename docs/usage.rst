.. _using-datedataparser:


Using DateDataParser
--------------------

:func:`dateparser.parse` uses a default parser which tries to detect language
every time it is called and is not the most efficient way while parsing dates
from the same source.

:class:`DateDataParser <dateparser.date.DateDataParser>` provides an alternate and efficient way
to control language detection behavior.

See :ref:`performance` for ways to make parsing faster.

:class:`dateparser.date.DateDataParser` can also be initialized with known languages:

    >>> ddp = DateDataParser(languages=['de', 'nl'])
    >>> ddp.get_date_data('vr jan 24, 2014 12:49')
    DateData(date_obj=datetime.datetime(2014, 1, 24, 12, 49), period='day', locale='nl', parts=('year', 'month', 'day', 'time'))
    >>> ddp.get_date_data('18.10.14 um 22:56 Uhr')
    DateData(date_obj=datetime.datetime(2014, 10, 18, 22, 56), period='day', locale='de', parts=('year', 'month', 'day', 'time'))
    >>> ddp.get_date_data('11 July 2012')
    DateData(date_obj=None, period='day', locale=None, parts=())

.. _batch-parsing:

Parsing a batch of dates
------------------------

A batch of dates may come from one source that writes every date in the same
language and date order, without saying which.  :func:`dateparser.parse_batch`
infers that convention from the languages that are allowed and from rows whose
dates are already known:

    >>> from datetime import date
    >>> from dateparser import parse_batch
    >>> result = parse_batch(
    ...     ['05/06/2021', '06.03.2021'],
    ...     allowed_languages=['en', 'de', 'fr'],
    ...     verified=[('5 März 2021', date(2021, 3, 5))],
    ... )
    >>> result.conventions
    (Convention(language='de', date_order='DMY'),)
    >>> [row.date for row in result.rows]
    [datetime.datetime(2021, 6, 5, 0, 0), datetime.datetime(2021, 3, 6, 0, 0)]

Each ``(language, date order)`` pair that reads every verified row as its known
date explains the batch.  A row gets a definite ``date`` only when all the
explanations read it the same way; otherwise ``date`` is ``None`` and
``candidates`` lists the possible dates together with the conventions that
support each one.  An ambiguous numeric date does not settle the date order by
being the first row in the file:

    >>> result = parse_batch(
    ...     ['05/03/2021'],
    ...     allowed_languages=['en', 'de'],
    ...     date_orders=['DMY', 'MDY'],
    ... )
    >>> result.rows[0].date is None
    True
    >>> [(c.date.date(), c.languages) for c in result.rows[0].candidates]
    [(datetime.date(2021, 3, 5), ('de', 'en')), (datetime.date(2021, 5, 3), ('de', 'en'))]

When no convention explains all the verified rows, the rows that have to be
left out are reported as :attr:`BatchResult.conflicts`, instead of reading each
row in a different language:

    >>> result = parse_batch(
    ...     [],
    ...     allowed_languages=['de', 'fr'],
    ...     verified=[('5 März 2021', date(2021, 3, 5)),
    ...               ('5 mars 2021', date(2021, 3, 5))],
    ... )
    >>> result.ok
    False
    >>> len(result.conflicts)
    2

Pass ``languages`` instead of ``allowed_languages`` when the language is
already known.  Such a batch is not inferred: it is read with the configured
languages and their date order, and ``languages`` are never widened to make a
row fit.

:class:`~dateparser.batch.BatchParser` can be reused for several batches with
the same configuration.  It holds no state between calls, and because each
batch copies its settings, two configurations can run in parallel without
affecting each other.
