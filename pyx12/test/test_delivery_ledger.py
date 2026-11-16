import unittest

from pyx12.delivery_ledger import (
    ACCEPTED,
    AMBIGUOUS,
    CONFLICT,
    REJECTED,
    UNKNOWN,
    UNMATCHED,
    AckReport,
    DeliveryLedger,
    GroupConclusion,
    GroupKey,
    GroupTotals,
    OutboundGroup,
    TransactionKey,
    TransactionVerdict,
    parse_ack_report,
)

SENDER = "ORIGINATOR"
RECEIVER = "RESPONDER"


def make_key(control_number, sender=SENDER, receiver=RECEIVER, functional_id="HC"):
    return GroupKey(
        sender_id=sender,
        receiver_id=receiver,
        functional_id=functional_id,
        control_number=control_number,
    )


def make_txn(key, control_number, transaction_set_id="837"):
    return TransactionKey(
        group=key, transaction_set_id=transaction_set_id, control_number=control_number
    )


def make_group(group_id, key, txn_controls):
    return OutboundGroup(
        group_id=group_id,
        key=key,
        transactions=frozenset(make_txn(key, control) for control in txn_controls),
    )


def make_report(report_id, key, verdicts=(), code="A", totals=None):
    return AckReport(
        report_id=report_id,
        conclusions=(
            GroupConclusion(
                group=key, code=code, totals=totals or GroupTotals(), report_id=report_id
            ),
        ),
        verdicts=tuple(
            TransactionVerdict(transaction=txn, verdict=verdict, report_id=report_id)
            for txn, verdict in verdicts
        ),
        raw=f"raw:{report_id}",
    )


def _isa(ack_sender, ack_receiver):
    return (
        "ISA*00*          *00*          "
        f"*ZZ*{ack_sender:<15}*ZZ*{ack_receiver:<15}"
        "*261101*1200*U*00401*000000001*0*T*:~"
    )


def make_997(ack_sender, ack_receiver, functional_id, group_control, txns, ak9):
    segments = ["ST*997*0001", f"AK1*{functional_id}*{group_control}"]
    for ts_id, ts_control, ack_code in txns:
        segments.append(f"AK2*{ts_id}*{ts_control}")
        segments.append(f"AK5*{ack_code}")
    segments.append("AK9*" + "*".join(str(part) for part in ak9))
    segments.append(f"SE*{len(segments) + 1}*0001")
    body = "~".join(segments) + "~"
    return (
        _isa(ack_sender, ack_receiver)
        + f"GS*FA*{ack_sender}*{ack_receiver}*20261101*1200*1*X*004010~"
        + body
        + "GE*1*1~IEA*1*000000001~"
    )


def make_999(ack_sender, ack_receiver, functional_id, group_control, txns, ak9):
    segments = ["ST*999*0001", f"AK1*{functional_id}*{group_control}"]
    for ts_id, ts_control, ack_code in txns:
        segments.append(f"AK2*{ts_id}*{ts_control}")
        segments.append(f"IK5*{ack_code}")
    segments.append("AK9*" + "*".join(str(part) for part in ak9))
    segments.append(f"SE*{len(segments) + 1}*0001")
    body = "~".join(segments) + "~"
    return (
        _isa(ack_sender, ack_receiver)
        + f"GS*FA*{ack_sender}*{ack_receiver}*20261101*1200*1*X*005010~"
        + body
        + "GE*1*1~IEA*1*000000001~"
    )


class TestTransactionStatus(unittest.TestCase):
    def setUp(self):
        self.key = make_key("100")
        self.t1 = make_txn(self.key, "0001")
        self.t2 = make_txn(self.key, "0002")
        self.ledger = DeliveryLedger()
        self.ledger.register_group(make_group("g1", self.key, ("0001", "0002")))

    def test_accepted_and_rejected(self):
        self.ledger.add_report(
            make_report("r1", self.key, ((self.t1, ACCEPTED), (self.t2, REJECTED)))
        )
        self.assertEqual(self.ledger.transaction_status(self.t1), ACCEPTED)
        self.assertEqual(self.ledger.transaction_status(self.t2), REJECTED)

    def test_no_receipt_is_unknown(self):
        self.assertEqual(self.ledger.transaction_status(self.t1), UNKNOWN)

    def test_duplicate_reports_count_one_delivery(self):
        report = make_report("r1", self.key, ((self.t1, ACCEPTED),))
        self.ledger.add_report(report)
        self.ledger.add_report(report)
        self.ledger.add_report(make_report("r2", self.key, ((self.t1, ACCEPTED),)))
        statuses = self.ledger.transaction_statuses()
        self.assertEqual(statuses[self.t1], ACCEPTED)
        self.assertEqual(list(statuses.values()).count(ACCEPTED), 1)

    def test_conflicting_verdicts_give_conflict(self):
        self.ledger.add_report(make_report("r1", self.key, ((self.t1, ACCEPTED),)))
        self.ledger.add_report(make_report("r2", self.key, ((self.t1, REJECTED),)))
        self.assertEqual(self.ledger.transaction_status(self.t1), CONFLICT)

    def test_conflict_is_not_erased_by_later_receipts(self):
        self.ledger.add_report(make_report("r1", self.key, ((self.t1, ACCEPTED),)))
        self.ledger.add_report(make_report("r2", self.key, ((self.t1, REJECTED),)))
        self.ledger.add_report(make_report("r3", self.key, ((self.t1, ACCEPTED),)))
        self.assertEqual(self.ledger.transaction_status(self.t1), CONFLICT)


class TestGroupReconciliation(unittest.TestCase):
    def setUp(self):
        self.key = make_key("100")
        self.txns = tuple(make_txn(self.key, f"000{i}") for i in (1, 2, 3))
        self.ledger = DeliveryLedger()
        self.ledger.register_group(make_group("g1", self.key, ("0001", "0002", "0003")))

    def test_group_level_only_confirms_scope_but_not_transactions(self):
        self.ledger.add_report(make_report("r1", self.key, code="A", totals=GroupTotals(3, 3, 3)))
        recon = self.ledger.reconcile_group("g1")
        self.assertTrue(recon.confirmed)
        self.assertEqual(set(recon.statuses.values()), {UNKNOWN})

    def test_unlisted_transactions_are_not_split_by_difference(self):
        verdicts = ((self.txns[0], ACCEPTED), (self.txns[1], ACCEPTED))
        self.ledger.add_report(make_report("r1", self.key, verdicts, totals=GroupTotals(3, 3, 3)))
        recon = self.ledger.reconcile_group("g1")
        self.assertEqual(recon.statuses[self.txns[2]], UNKNOWN)
        self.assertTrue(all(check.consistent for check in recon.totals_checks))

    def test_totals_contradicting_details_are_flagged(self):
        verdicts = ((self.txns[0], REJECTED), (self.txns[1], REJECTED))
        self.ledger.add_report(make_report("r1", self.key, verdicts, totals=GroupTotals(3, 3, 3)))
        recon = self.ledger.reconcile_group("g1")
        accepted_checks = [c for c in recon.totals_checks if "accepted" in c.detail]
        self.assertTrue(any(not c.consistent for c in accepted_checks))

    def test_included_count_mismatch_is_flagged(self):
        self.ledger.add_report(
            make_report("r1", self.key, totals=GroupTotals(included=2, received=2))
        )
        recon = self.ledger.reconcile_group("g1")
        self.assertTrue(any(not c.consistent for c in recon.totals_checks))

    def test_group_code_conflict_is_flagged_and_kept(self):
        self.ledger.add_report(make_report("r1", self.key, code="A"))
        self.ledger.add_report(make_report("r2", self.key, code="R"))
        recon = self.ledger.reconcile_group("g1")
        self.assertTrue(recon.group_conflict)
        self.assertEqual(recon.codes, frozenset({"A", "R"}))

    def test_out_of_scope_detail_is_recorded_not_linked(self):
        foreign = make_txn(self.key, "0009")
        self.ledger.add_report(make_report("r1", self.key, ((foreign, ACCEPTED),)))
        recon = self.ledger.reconcile_group("g1")
        self.assertEqual([v.transaction for v in recon.out_of_scope], [foreign])
        self.assertEqual(set(recon.statuses.values()), {UNKNOWN})


class TestCorrelation(unittest.TestCase):
    def test_control_number_reuse_across_sender_domains(self):
        key_a = make_key("100", sender="ALPHA", receiver="BETA")
        key_b = make_key("100", sender="GAMMA", receiver="DELTA")
        txn_a = make_txn(key_a, "0001")
        txn_b = make_txn(key_b, "0001")
        ledger = DeliveryLedger()
        ledger.register_group(make_group("ga", key_a, ("0001",)))
        ledger.register_group(make_group("gb", key_b, ("0001",)))
        ledger.add_report(make_report("ra", key_a, ((txn_a, ACCEPTED),)))
        ledger.add_report(make_report("rb", key_b, ((txn_b, REJECTED),)))
        self.assertEqual(ledger.transaction_status(txn_a), ACCEPTED)
        self.assertEqual(ledger.transaction_status(txn_b), REJECTED)
        self.assertEqual(ledger.candidates(), [])

    def test_ambiguous_group_stays_candidate(self):
        key = make_key("100")
        txn = make_txn(key, "0001")
        ledger = DeliveryLedger()
        ledger.register_group(make_group("g1", key, ("0001",)))
        ledger.register_group(make_group("g2", key, ("0001",)))
        ledger.add_report(make_report("r1", key, ((txn, ACCEPTED),)))
        candidates = ledger.candidates()
        self.assertEqual([c.reason for c in candidates], [AMBIGUOUS])
        self.assertEqual(ledger.transaction_status(txn), UNKNOWN)

    def test_unmatched_report_stays_candidate(self):
        key = make_key("100")
        ledger = DeliveryLedger()
        ledger.register_group(make_group("g1", key, ("0001",)))
        foreign_key = make_key("200")
        ledger.add_report(make_report("r1", foreign_key, code="A"))
        candidates = ledger.candidates()
        self.assertEqual([c.reason for c in candidates], [UNMATCHED])
        self.assertFalse(ledger.reconcile_group("g1").confirmed)

    def test_candidate_links_once_group_is_registered(self):
        key = make_key("100")
        txn = make_txn(key, "0001")
        ledger = DeliveryLedger()
        ledger.add_report(make_report("r1", key, ((txn, ACCEPTED),)))
        self.assertEqual(ledger.transaction_status(txn), UNKNOWN)
        ledger.register_group(make_group("g1", key, ("0001",)))
        self.assertEqual(ledger.transaction_status(txn), ACCEPTED)
        self.assertEqual(ledger.candidates(), [])


class TestOrderIndependence(unittest.TestCase):
    def test_arrival_order_does_not_change_results(self):
        key = make_key("100")
        other_key = make_key("200")
        t1 = make_txn(key, "0001")
        t2 = make_txn(key, "0002")
        reports = [
            make_report("r1", key, ((t1, ACCEPTED),), totals=GroupTotals(2, 2, 1)),
            make_report("r2", key, ((t1, REJECTED), (t2, ACCEPTED)), code="P"),
            make_report("r3", other_key, code="A"),
        ]

        def build(order):
            ledger = DeliveryLedger()
            for step in order:
                if step == "g1":
                    ledger.register_group(make_group("g1", key, ("0001", "0002")))
                elif step == "g2":
                    ledger.register_group(make_group("g2", other_key, ("0001",)))
                else:
                    ledger.add_report(reports[int(step[1]) - 1])
            return ledger

        forward = build(["g1", "g2", "r1", "r2", "r3"])
        reverse = build(["r3", "r2", "r1", "g2", "g1"])
        self.assertEqual(forward.transaction_statuses(), reverse.transaction_statuses())
        self.assertEqual(forward.reconcile(), reverse.reconcile())
        self.assertEqual(forward.candidates(), reverse.candidates())


class TestParseAckReport(unittest.TestCase):
    def test_parse_997(self):
        text = make_997(
            RECEIVER,
            SENDER,
            "HC",
            "100",
            (("837", "0001", "A"), ("837", "0002", "R")),
            ("P", 2, 2, 1),
        )
        report = parse_ack_report("r1", text)
        self.assertEqual(report.raw, text)
        self.assertEqual(len(report.conclusions), 1)
        conclusion = report.conclusions[0]
        self.assertEqual(conclusion.group, make_key("100"))
        self.assertEqual(conclusion.code, "P")
        self.assertEqual(conclusion.totals, GroupTotals(included=2, received=2, accepted=1))
        key = make_key("100")
        self.assertEqual(
            set(report.verdicts),
            {
                TransactionVerdict(make_txn(key, "0001"), ACCEPTED, "r1"),
                TransactionVerdict(make_txn(key, "0002"), REJECTED, "r1"),
            },
        )

    def test_parse_999(self):
        text = make_999(RECEIVER, SENDER, "HC", "100", (("837", "0001", "R"),), ("R", 1, 1, 0))
        report = parse_ack_report("r1", text)
        self.assertEqual(report.conclusions[0].code, "R")
        self.assertEqual(
            report.verdicts,
            (TransactionVerdict(make_txn(make_key("100"), "0001"), REJECTED, "r1"),),
        )

    def test_group_level_only_report_has_no_verdicts(self):
        text = make_997(RECEIVER, SENDER, "HC", "100", (), ("A", 2, 2, 2))
        report = parse_ack_report("r1", text)
        self.assertEqual(report.verdicts, ())
        self.assertEqual(report.conclusions[0].totals.accepted, 2)

    def test_unmapped_transaction_codes_give_no_verdict(self):
        text = make_997(RECEIVER, SENDER, "HC", "100", (("837", "0001", "X"),), ("E", 1, 1, 0))
        report = parse_ack_report("r1", text)
        self.assertEqual(report.verdicts, ())

    def test_parsed_report_feeds_ledger(self):
        key = make_key("100")
        txn = make_txn(key, "0001")
        ledger = DeliveryLedger()
        ledger.register_group(make_group("g1", key, ("0001",)))
        text = make_997(RECEIVER, SENDER, "HC", "100", (("837", "0001", "A"),), ("A", 1, 1, 1))
        ledger.add_report(parse_ack_report("r1", text))
        self.assertEqual(ledger.transaction_status(txn), ACCEPTED)
        recon = ledger.reconcile_group("g1")
        self.assertTrue(recon.confirmed)
        self.assertTrue(all(check.consistent for check in recon.totals_checks))


if __name__ == "__main__":
    unittest.main()
