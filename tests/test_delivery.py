import datetime
import unittest

from imap_tools.delivery import analyze_delivery, analyze_message
from imap_tools.message import MailMessage

UTC = datetime.timezone.utc
SKEW = datetime.timedelta(minutes=2)


def dt(hour, minute=0, second=0):
    return datetime.datetime(2026, 11, 19, hour, minute, second, tzinfo=UTC)


def rfc(dt_value):
    return dt_value.strftime('%a, %d %b %Y %H:%M:%S +0000')


def received(by_host, dt_value, from_host='mail.sender.example'):
    return 'from {} by {} with ESMTPS id abc123; {}'.format(from_host, by_host, rfc(dt_value))


class AnalyzeDeliveryTest(unittest.TestCase):

    def test_time_sources_stored_separately(self):
        report = analyze_delivery(
            received_headers=[received('mx.example.com', dt(10, 5))],
            date_header=rfc(dt(10, 0)),
            internal_date=dt(10, 6),
            trusted_relays=['mx.example.com'],
            max_clock_skew=SKEW,
        )
        self.assertEqual(report.date_header, dt(10, 0))
        self.assertEqual(report.internal_date, dt(10, 6))
        self.assertEqual(report.hops[0].timestamp, dt(10, 5))

    def test_delivery_window_uses_only_trusted_observations(self):
        report = analyze_delivery(
            received_headers=[
                received('mx.example.com', dt(10, 5)),
                received('evil.sender.example', dt(1, 0)),  # forged, outside boundary
            ],
            internal_date=dt(10, 6),
            trusted_relays=['.example.com'],
            max_clock_skew=SKEW,
        )
        self.assertTrue(report.hops[0].trusted)
        self.assertFalse(report.hops[1].trusted)  # not promoted to trusted observation
        self.assertEqual(report.delivery_window, (dt(10, 3), dt(10, 8)))  # +- skew, forged hop ignored

    def test_forged_hop_cannot_extend_trusted_boundary(self):
        # a trusted-looking 'by' host below an untrusted hop stays untrusted
        report = analyze_delivery(
            received_headers=[
                received('mx.example.com', dt(10, 5)),
                received('unknown.relay.example.org', dt(10, 4)),
                received('mx.example.com', dt(10, 3)),  # forged claim, below boundary
            ],
            trusted_relays=['mx.example.com'],
            max_clock_skew=SKEW,
        )
        self.assertEqual([h.trusted for h in report.hops], [True, False, False])

    def test_hop_delay_range_with_skew(self):
        report = analyze_delivery(
            received_headers=[
                received('mx2.example.com', dt(10, 10)),
                received('mx1.example.com', dt(10, 4)),
            ],
            trusted_relays=['.example.com'],
            max_clock_skew=SKEW,
        )
        delay = report.hop_delays[0]
        self.assertEqual(delay.earlier_hop, 1)
        self.assertEqual(delay.later_hop, 0)
        self.assertEqual(delay.min_delay, datetime.timedelta(minutes=2))   # 6min - 2*skew
        self.assertEqual(delay.max_delay, datetime.timedelta(minutes=10))  # 6min + 2*skew
        self.assertTrue(delay.order_certain)
        self.assertEqual(report.conflicts, [])

    def test_missing_hop_timestamp_gives_unknown_delay_not_zero(self):
        report = analyze_delivery(
            received_headers=[
                received('mx2.example.com', dt(10, 10)),
                'from mail.sender.example by mx1.example.com with ESMTPS id abc123',  # no date part
            ],
            trusted_relays=['.example.com'],
            max_clock_skew=SKEW,
        )
        delay = report.hop_delays[0]
        self.assertIsNone(delay.min_delay)
        self.assertIsNone(delay.max_delay)
        self.assertFalse(delay.order_certain)

    def test_untrusted_hop_gives_unknown_delay(self):
        report = analyze_delivery(
            received_headers=[
                received('mx.example.com', dt(10, 10)),
                received('mail.sender.example', dt(10, 4)),
            ],
            trusted_relays=['mx.example.com'],
            max_clock_skew=SKEW,
        )
        self.assertIsNone(report.hop_delays[0].min_delay)

    def test_overlapping_ranges_do_not_fabricate_order(self):
        report = analyze_delivery(
            received_headers=[
                received('mx2.example.com', dt(10, 4, 30)),
                received('mx1.example.com', dt(10, 4)),
            ],
            trusted_relays=['.example.com'],
            max_clock_skew=SKEW,  # 30s diff < 2*skew -> order not provable
        )
        self.assertFalse(report.hop_delays[0].order_certain)
        self.assertEqual(report.conflicts, [])  # overlap is not a conflict

    def test_path_not_resorted_and_contradiction_reported(self):
        report = analyze_delivery(
            received_headers=[
                received('mx2.example.com', dt(10, 0)),   # claims earlier than the previous hop
                received('mx1.example.com', dt(10, 10)),
            ],
            trusted_relays=['.example.com'],
            max_clock_skew=SKEW,
        )
        self.assertEqual([h.index for h in report.hops], [0, 1])  # header order preserved
        self.assertEqual(len(report.conflicts), 1)
        self.assertEqual(report.conflicts[0].first, 'Received[1]')
        self.assertEqual(report.conflicts[0].second, 'Received[0]')

    def test_date_header_conflict(self):
        report = analyze_delivery(
            received_headers=[received('mx.example.com', dt(10, 5))],
            date_header=rfc(dt(11, 0)),  # sent after receipt - impossible
            trusted_relays=['mx.example.com'],
            max_clock_skew=SKEW,
        )
        self.assertEqual(len(report.conflicts), 1)
        self.assertEqual(report.conflicts[0].first, 'Date')

    def test_internal_date_conflict(self):
        report = analyze_delivery(
            received_headers=[received('mx.example.com', dt(10, 5))],
            internal_date=dt(9, 0),  # stored before final hop - impossible
            trusted_relays=['mx.example.com'],
            max_clock_skew=SKEW,
        )
        self.assertEqual(len(report.conflicts), 1)
        self.assertEqual(report.conflicts[0].second, 'INTERNALDATE')

    def test_no_trusted_observations_gives_no_window(self):
        report = analyze_delivery(
            received_headers=[received('mail.sender.example', dt(10, 0))],
            date_header=rfc(dt(10, 0)),
            trusted_relays=['mx.example.com'],
        )
        self.assertIsNone(report.delivery_window)

    def test_negative_skew_rejected(self):
        with self.assertRaises(ValueError):
            analyze_delivery([], max_clock_skew=datetime.timedelta(seconds=-1))

    def test_analyze_message(self):
        raw = (
            'Received: from mail.sender.example by mx.example.com with ESMTPS id a1; {}\r\n'
            'Date: {}\r\n'
            'From: a@b.c\r\n'
            '\r\n'
            'body'
        ).format(rfc(dt(10, 5)), rfc(dt(10, 0))).encode()
        message = MailMessage.from_bytes(raw)
        report = analyze_message(
            message, internal_date=dt(10, 6), trusted_relays=['mx.example.com'], max_clock_skew=SKEW)
        self.assertEqual(len(report.hops), 1)
        self.assertEqual(report.delivery_window, (dt(10, 3), dt(10, 8)))


if __name__ == '__main__':
    unittest.main()
