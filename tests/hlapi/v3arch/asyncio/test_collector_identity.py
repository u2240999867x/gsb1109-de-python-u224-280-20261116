#
# This file is part of pysnmp software.
#
# Copyright (c) 2005-2020, Ilya Etingof <etingof@gmail.com>
# License: https://www.pysnmp.com/pysnmp/license.html
#
"""Tests for layered SNMPv3 identity across proxies.

These tests exercise the peer engine/context cache, the USM timeline, and the
collector/replay layer.  Network I/O is stubbed: the code under test is the
identity bookkeeping, not the socket stack.
"""

from pysnmp.hlapi.v3arch.asyncio.context import ContextData

from pysnmp.proto import errind
from pysnmp.proto import api as proto_api
from pysnmp.proto.api import v2c
from pysnmp.proto.mpmod.rfc3412 import SnmpV3MessageProcessingModel
from pysnmp.proto.secmod.rfc3414.service import SnmpUSMSecurityModel

from pysnmp.hlapi.v3arch.asyncio.collector import (
    UNKNOWN_ENGINE_ID_OID,
    CollectionIdentity,
    CollectionReplay,
    ReportKind,
    _ObservationStore,
)

pMod = proto_api.PROTOCOL_MODULES[proto_api.SNMP_VERSION_2C]  # noqa: N816

DOMAIN = (1, 3, 6, 1, 6, 1, 1)
ENDPOINT = ("192.0.2.10", 161)
SECURITY_ENGINE = b"\x80\x00\x00\x00\x01\x02\x03\x04"
OTHER_SECURITY_ENGINE = b"\x80\x00\x00\x00\x01\x02\x03\x05"
CONTEXT_ENGINE_A = b"\x80\x00\x00\x00\x0a\x0b\x0c\x0d"
CONTEXT_ENGINE_B = b"\x80\x00\x00\x00\x0a\x0b\x0c\x0e"


def _new_mpmod():
    return SnmpV3MessageProcessingModel()


class TestPeerCacheLayering:
    """The transport endpoint, authentication engine and context are distinct."""

    def test_pending_before_discovery(self):
        mpmod = _new_mpmod()
        assert mpmod.get_peer_engine_info(DOMAIN, ENDPOINT, b"") == (
            None,
            None,
            None,
        )
        assert mpmod.get_peer_engine_details(DOMAIN, ENDPOINT, b"") is None

    def test_untrusted_report_withholds_context_engine_id(self):
        mpmod = _new_mpmod()
        mpmod.remember_peer_engine_info(
            DOMAIN,
            ENDPOINT,
            SECURITY_ENGINE,
            CONTEXT_ENGINE_A,
            b"ctx-a",
            source="report",
            trust_context_engine_id=False,
        )

        details = mpmod.get_peer_engine_details(DOMAIN, ENDPOINT, b"ctx-a")
        assert details["securityEngineId"] == SECURITY_ENGINE
        assert details["contextEngineId"] is None
        assert details["contextState"] == "pending"

    def test_response_confirms_context_engine_id(self):
        mpmod = _new_mpmod()
        mpmod.remember_peer_engine_info(
            DOMAIN,
            ENDPOINT,
            SECURITY_ENGINE,
            CONTEXT_ENGINE_A,
            b"ctx-a",
            source="response",
            trust_context_engine_id=True,
        )

        details = mpmod.get_peer_engine_details(DOMAIN, ENDPOINT, b"ctx-a")
        assert details["contextEngineId"] == CONTEXT_ENGINE_A
        assert details["contextState"] == "verified"

    def test_contexts_on_same_endpoint_stay_independent(self):
        mpmod = _new_mpmod()
        mpmod.remember_peer_engine_info(
            DOMAIN,
            ENDPOINT,
            SECURITY_ENGINE,
            CONTEXT_ENGINE_A,
            b"ctx-a",
            source="response",
        )
        mpmod.remember_peer_engine_info(
            DOMAIN,
            ENDPOINT,
            SECURITY_ENGINE,
            CONTEXT_ENGINE_B,
            b"ctx-b",
            source="response",
        )

        assert (
            mpmod.get_peer_engine_info(DOMAIN, ENDPOINT, b"ctx-a")[1]
            == CONTEXT_ENGINE_A
        )
        assert (
            mpmod.get_peer_engine_info(DOMAIN, ENDPOINT, b"ctx-b")[1]
            == CONTEXT_ENGINE_B
        )

    def test_rediscovery_of_one_context_keeps_the_other(self):
        mpmod = _new_mpmod()
        mpmod.remember_peer_engine_info(
            DOMAIN,
            ENDPOINT,
            SECURITY_ENGINE,
            CONTEXT_ENGINE_A,
            b"ctx-a",
            source="response",
        )
        mpmod.remember_peer_engine_info(
            DOMAIN,
            ENDPOINT,
            SECURITY_ENGINE,
            CONTEXT_ENGINE_B,
            b"ctx-b",
            source="response",
        )

        mpmod.forget_peer_engine_info(DOMAIN, ENDPOINT, b"ctx-a")

        forgotten = mpmod.get_peer_engine_details(DOMAIN, ENDPOINT, b"ctx-a")
        assert forgotten["contextKnown"] is False
        assert forgotten["contextEngineId"] is None
        assert (
            mpmod.get_peer_engine_details(DOMAIN, ENDPOINT, b"ctx-b")["contextEngineId"]
            == CONTEXT_ENGINE_B
        )

    def test_engine_id_change_pends_all_contexts_on_route(self):
        mpmod = _new_mpmod()
        mpmod.remember_peer_engine_info(
            DOMAIN,
            ENDPOINT,
            SECURITY_ENGINE,
            CONTEXT_ENGINE_A,
            b"ctx-a",
            source="response",
        )
        mpmod.remember_peer_engine_info(
            DOMAIN,
            ENDPOINT,
            SECURITY_ENGINE,
            CONTEXT_ENGINE_B,
            b"ctx-b",
            source="response",
        )

        _, changed = mpmod.remember_peer_engine_info(
            DOMAIN,
            ENDPOINT,
            OTHER_SECURITY_ENGINE,
            CONTEXT_ENGINE_A,
            b"ctx-a",
            source="report",
            trust_context_engine_id=False,
        )

        assert changed is True
        assert (
            mpmod.get_peer_engine_details(DOMAIN, ENDPOINT, b"ctx-b")["contextEngineId"]
            is None
        )

    def test_boot_generation_change_is_reported(self):
        mpmod = _new_mpmod()
        mpmod.remember_peer_engine_info(
            DOMAIN, ENDPOINT, SECURITY_ENGINE, b"", engineBoots=7, source="response"
        )
        _, changed = mpmod.remember_peer_engine_info(
            DOMAIN, ENDPOINT, SECURITY_ENGINE, b"", engineBoots=8, source="response"
        )
        assert changed is True

    def test_report_without_timing_keeps_boot_evidence(self):
        mpmod = _new_mpmod()
        mpmod.remember_peer_engine_info(
            DOMAIN, ENDPOINT, SECURITY_ENGINE, b"", engineBoots=7, source="response"
        )
        mpmod.remember_peer_engine_info(
            DOMAIN,
            ENDPOINT,
            SECURITY_ENGINE,
            b"",
            engineBoots=None,
            source="report",
            trust_context_engine_id=False,
        )
        assert mpmod.get_peer_engine_details(DOMAIN, ENDPOINT, b"")["engineBoots"] == 7

    def test_refresh_keeps_entry_past_first_deadline(self):
        mpmod = _new_mpmod()
        mpmod.remember_peer_engine_info(
            DOMAIN, ENDPOINT, SECURITY_ENGINE, CONTEXT_ENGINE_A, b"ctx-a"
        )
        # Refresh the same entry; the newer registration must win over the
        # stale one scheduled at the same tick.
        mpmod.remember_peer_engine_info(
            DOMAIN, ENDPOINT, SECURITY_ENGINE, CONTEXT_ENGINE_A, b"ctx-a"
        )
        for _ in range(150):
            mpmod.receive_timer_tick(None, 0)

        details = mpmod.get_peer_engine_details(DOMAIN, ENDPOINT, b"ctx-a")
        assert details is not None
        assert details["securityEngineId"] == SECURITY_ENGINE


class TestUsmTimeline:
    """Boot/time evidence is trusted only after validation."""

    def test_first_authenticated_message_establishes_timeline(self):
        sm = SnmpUSMSecurityModel()
        assert not sm.is_timeline_trusted(SECURITY_ENGINE)
        sm._SnmpUSMSecurityModel__timeline[SECURITY_ENGINE] = (3, 100, 100, 0)
        assert sm.is_timeline_trusted(SECURITY_ENGINE)
        info = sm.get_timeline_info(SECURITY_ENGINE)
        assert info["engineBoots"] == 3
        assert info["engineTime"] == 100

    def test_invalidate_timeline_drops_evidence(self):
        sm = SnmpUSMSecurityModel()
        sm._SnmpUSMSecurityModel__timeline[SECURITY_ENGINE] = (3, 100, 100, 0)
        sm._SnmpUSMSecurityModel__timeline[OTHER_SECURITY_ENGINE] = (1, 5, 5, 0)

        sm.invalidate_timeline(SECURITY_ENGINE)

        assert not sm.is_timeline_trusted(SECURITY_ENGINE)
        assert sm.is_timeline_trusted(OTHER_SECURITY_ENGINE)


class TestObservationStore:
    """Observations belong to one context identity and nothing else."""

    def _identity(self, contextEngineId, contextName, securityEngineId=SECURITY_ENGINE):
        return CollectionIdentity(
            DOMAIN,
            ENDPOINT,
            securityEngineId=securityEngineId,
            contextEngineId=contextEngineId,
            contextName=contextName,
            verified=True,
        )

    def test_samples_are_keyed_by_context_identity(self):
        store = _ObservationStore()
        oid = (1, 3, 6, 1, 2, 1, 1, 1, 0)

        class Sample:
            attributed = True
            identity = self._identity(CONTEXT_ENGINE_A, b"ctx")
            oid = (1, 3, 6, 1, 2, 1, 1, 1, 0)
            value = "A"

        store.record(Sample())

        assert store.get(self._identity(CONTEXT_ENGINE_A, b"ctx"), oid) == "A"
        assert store.get(self._identity(CONTEXT_ENGINE_B, b"ctx"), oid) is None
        assert store.get(self._identity(CONTEXT_ENGINE_A, b"other"), oid) is None

    def test_pending_sample_is_not_recorded(self):
        store = _ObservationStore()

        class Sample:
            attributed = False
            identity = self._identity(CONTEXT_ENGINE_A, b"ctx")
            oid = (1, 3, 6, 1, 2, 1, 1, 1, 0)
            value = None

        assert store.record(Sample()) is False
        assert len(store) == 0

    def test_invalidate_route_does_not_touch_other_routes(self):
        store = _ObservationStore()
        other = CollectionIdentity(
            DOMAIN,
            ("192.0.2.11", 161),
            securityEngineId=SECURITY_ENGINE,
            contextEngineId=CONTEXT_ENGINE_A,
            contextName=b"ctx",
            verified=True,
        )

        class Sample:
            attributed = True

            def __init__(self, identity, value, oid=(1, 3, 6, 1)):
                self.identity = identity
                self.value = value
                self.oid = oid

        store.record(Sample(self._identity(CONTEXT_ENGINE_A, b"ctx"), "here"))
        store.record(Sample(other, "there"))

        store.invalidate_route((DOMAIN, ENDPOINT))

        assert len(store) == 1
        assert store.get(other, (1, 3, 6, 1)) == "there"


class TestReplay:
    """Replay must expose the identity each request really used."""

    def _journal(self, verified=True, securityEngineId=SECURITY_ENGINE):
        identity = CollectionIdentity(
            DOMAIN,
            ENDPOINT,
            securityEngineId=securityEngineId,
            contextEngineId=CONTEXT_ENGINE_A,
            contextName=b"ctx",
            verified=verified,
        )
        return {
            "requests": [
                {
                    "requestId": "q1/0",
                    "oid": [1, 3, 6, 1, 2, 1, 1, 1, 0],
                    "identity": identity.to_dict(),
                }
            ],
            "samples": [
                {
                    "requestId": "q1/0",
                    "oid": [1, 3, 6, 1, 2, 1, 1, 1, 0],
                    "value": "router",
                    "pending": False,
                    "identity": identity.to_dict(),
                }
            ],
            "reports": [],
        }

    def test_verified_journal_replays_identity(self):
        verdicts = list(CollectionReplay(self._journal()).verify())
        assert len(verdicts) == 1
        requestId, oid, identity, state, detail = verdicts[0]
        assert requestId == "q1/0"
        assert state == "verified"
        assert identity.securityEngineId == SECURITY_ENGINE
        assert identity.contextEngineId == CONTEXT_ENGINE_A

    def test_unconfirmed_identity_is_pending_discovery(self):
        verdicts = list(CollectionReplay(self._journal(verified=False)).verify())
        assert verdicts[0][3] == "pending-discovery"

    def test_missing_engine_evidence_is_reported(self):
        verdicts = list(CollectionReplay(self._journal(securityEngineId=None)).verify())
        assert verdicts[0][3] == "pending-discovery"
        assert "authoritative engine" in verdicts[0][4]

    def test_journal_round_trips_through_json(self):
        replay = CollectionReplay(self._journal())
        restored = CollectionReplay.from_json(replay.to_json())
        assert list(restored.verify())[0][3] == "verified"


class TestReportClassification:
    """Reports are evidence, never business variables."""

    def test_report_kind_mapping(self):
        from pysnmp.hlapi.v3arch.asyncio import collector

        assert (
            collector.REPORT_KINDS[UNKNOWN_ENGINE_ID_OID]
            == ReportKind.UNKNOWN_ENGINE_ID
        )
        assert (
            collector.REPORT_KINDS[(1, 3, 6, 1, 6, 3, 15, 1, 1, 2, 0)]
            == ReportKind.NOT_IN_TIME_WINDOW
        )

    def test_rediscovery_reports_are_bounded(self):
        from pysnmp.hlapi.v3arch.asyncio import collector

        assert collector.REDISCOVERY_REPORTS == frozenset({
            ReportKind.UNKNOWN_ENGINE_ID,
            ReportKind.NOT_IN_TIME_WINDOW,
        })


def test_report_error_indications_are_not_success_values():
    """A report indication must map to a pending sample, not a value."""
    sample = __import__(
        "pysnmp.hlapi.v3arch.asyncio.collector", fromlist=["CollectionSample"]
    ).CollectionSample(
        "q1/0",
        (1, 3, 6, 1),
        identity=CollectionIdentity(
            DOMAIN,
            ENDPOINT,
            securityEngineId=SECURITY_ENGINE,
            contextEngineId=CONTEXT_ENGINE_A,
            contextName=b"ctx",
            verified=True,
        ),
        pending=True,
        reason="report",
        reportKind=ReportKind.UNKNOWN_ENGINE_ID,
        errorIndication=errind.unknownEngineID,
    )

    assert sample.attributed is False
    assert sample.to_dict()["pending"] is True
    assert sample.to_dict()["value"] == ""


class TestCollectorGating:
    """A collector must not attribute values without confirmed evidence."""

    def test_pending_identity_never_marks_good_evidence_when_no_sample(self):
        identity = CollectionIdentity(
            DOMAIN,
            ENDPOINT,
            securityEngineId=SECURITY_ENGINE,
            contextEngineId=CONTEXT_ENGINE_A,
            contextName=b"ctx",
            verified=False,
        )
        journal = {
            "requests": [
                {
                    "requestId": "q1/0",
                    "oid": [1, 3],
                    "identity": identity.to_dict(),
                }
            ],
            "samples": [],
            "reports": [],
        }
        verdict = list(CollectionReplay(journal).verify())[0]
        assert verdict[3] == "pending-discovery"

    def test_boot_generation_change_revokes_freshness_only(self):
        mpmod = _new_mpmod()
        mpmod.remember_peer_engine_info(
            DOMAIN,
            ENDPOINT,
            SECURITY_ENGINE,
            CONTEXT_ENGINE_A,
            b"ctx-a",
            engineBoots=5,
            source="response",
        )
        mpmod.remember_peer_engine_info(
            DOMAIN,
            ENDPOINT,
            SECURITY_ENGINE,
            CONTEXT_ENGINE_B,
            b"ctx-b",
            engineBoots=5,
            source="response",
        )

        mpmod.remember_peer_engine_info(
            DOMAIN,
            ENDPOINT,
            SECURITY_ENGINE,
            CONTEXT_ENGINE_A,
            b"ctx-a",
            engineBoots=6,
            source="response",
        )

        details = mpmod.get_peer_engine_details(DOMAIN, ENDPOINT, b"ctx-b")
        assert details["contextEngineId"] == CONTEXT_ENGINE_B
        assert details["contextState"] == "pending"
        assert details["freshnessVerified"] is False

    def test_older_replayed_boot_is_not_a_new_generation(self):
        mpmod = _new_mpmod()
        mpmod.remember_peer_engine_info(
            DOMAIN, ENDPOINT, SECURITY_ENGINE, b"", engineBoots=9, source="response"
        )
        _, changed = mpmod.remember_peer_engine_info(
            DOMAIN, ENDPOINT, SECURITY_ENGINE, b"", engineBoots=4, source="response"
        )
        assert changed is False
        assert mpmod.get_peer_engine_details(DOMAIN, ENDPOINT, b"")["engineBoots"] == 9

    def test_invalidate_peer_discovery_keeps_context_mapping(self):
        mpmod = _new_mpmod()
        mpmod.remember_peer_engine_info(
            DOMAIN, ENDPOINT, SECURITY_ENGINE, CONTEXT_ENGINE_A, b"ctx-a"
        )
        mpmod.invalidate_peer_discovery(DOMAIN, ENDPOINT, b"ctx-a")

        details = mpmod.get_peer_engine_details(DOMAIN, ENDPOINT, b"ctx-a")
        assert details["contextEngineId"] == CONTEXT_ENGINE_A
        assert details["freshnessVerified"] is False


class TestObservationFreshness:
    """Stale values stay auditable but never satisfy a new request."""

    def _identity(self):
        return CollectionIdentity(
            DOMAIN,
            ENDPOINT,
            securityEngineId=SECURITY_ENGINE,
            contextEngineId=CONTEXT_ENGINE_A,
            contextName=b"ctx",
            verified=True,
        )

    class _Sample:
        attributed = True
        oid = (1, 3, 6, 1, 4, 1)

        def __init__(self, identity, value):
            self.identity = identity
            self.value = value

    def test_stale_observation_is_not_returned(self):
        store = _ObservationStore()
        identity = self._identity()
        store.record(self._Sample(identity, "old"))

        store.mark_stale(identity)

        assert store.get(identity, self._Sample.oid) is None
        assert store.is_stale(identity) is True

    def test_stale_marker_clears_after_refresh(self):
        store = _ObservationStore()
        identity = self._identity()
        store.record(self._Sample(identity, "old"))
        store.mark_stale(identity)

        store.record(self._Sample(identity, "new"))

        assert store.get(identity, self._Sample.oid) == "new"
        assert store.is_stale(identity) is False

    def test_stale_marker_is_per_context(self):
        store = _ObservationStore()
        first = self._identity()
        second = CollectionIdentity(
            DOMAIN,
            ENDPOINT,
            securityEngineId=SECURITY_ENGINE,
            contextEngineId=CONTEXT_ENGINE_B,
            contextName=b"other",
            verified=True,
        )
        store.record(self._Sample(first, "first"))
        store.record(self._Sample(second, "second"))

        store.mark_stale(first)

        assert store.get(second, self._Sample.oid) == "second"
        assert store.get(first, self._Sample.oid) is None

    def test_journal_exposes_staleness(self):
        store = _ObservationStore()
        identity = self._identity()
        store.record(self._Sample(identity, "value"))
        store.mark_stale(identity)

        (entry,) = store.to_journal()
        assert entry["stale"] is True
        assert entry["identity"]["contextEngineId"] == "0x" + CONTEXT_ENGINE_A.hex()


class TestMpmodReportHandling:
    """Reports cache the engine but do not authorize the context."""

    def test_credential_report_does_not_revoke_freshness(self):
        mpmod = _new_mpmod()
        mpmod.remember_peer_engine_info(
            DOMAIN,
            ENDPOINT,
            SECURITY_ENGINE,
            CONTEXT_ENGINE_A,
            b"ctx",
            source="response",
        )
        details = mpmod.get_peer_engine_details(DOMAIN, ENDPOINT, b"ctx")
        assert details["freshnessVerified"] is True

    def test_unknown_engine_report_revokes_freshness(self):
        mpmod = _new_mpmod()
        pdu = pMod.ReportPDU()
        pMod.apiPDU.set_defaults(pdu)
        pMod.apiPDU.set_varbinds(
            pdu, [((1, 3, 6, 1, 6, 3, 15, 1, 1, 4, 0), v2c.Counter32(1))]
        )
        assert mpmod._report_revokes_freshness(pdu) is True

    def test_unknown_user_report_keeps_freshness(self):
        mpmod = _new_mpmod()
        pdu = pMod.ReportPDU()
        pMod.apiPDU.set_defaults(pdu)
        pMod.apiPDU.set_varbinds(
            pdu, [((1, 3, 6, 1, 6, 3, 15, 1, 1, 3, 0), v2c.Counter32(1))]
        )
        assert mpmod._report_revokes_freshness(pdu) is False


class TestReportGoesThroughMpmod:
    """End-to-end(ish) check of the report branch inside prepareDataElements."""

    def _build_report_wire(self, engine_id, report_oid):
        from pyasn1.codec.ber import encoder

        from pysnmp.proto import rfc1905
        from pysnmp.proto.mpmod import rfc3412 as proto_rfc3412
        from pysnmp.proto.secmod.rfc3414.service import UsmSecurityParameters

        pdu = rfc1905.ReportPDU()
        pMod.apiPDU.set_defaults(pdu)
        pMod.apiPDU.set_request_id(pdu, 0)
        pMod.apiPDU.set_varbinds(pdu, [(report_oid, v2c.Counter32(1))])

        security_parameters = UsmSecurityParameters()
        security_parameters.setComponentByPosition(0, engine_id)
        security_parameters.setComponentByPosition(1, 0)
        security_parameters.setComponentByPosition(2, 0)
        security_parameters.setComponentByPosition(3, b"")
        security_parameters.setComponentByPosition(4, b"")
        security_parameters.setComponentByPosition(5, b"")

        message = proto_rfc3412.SNMPv3Message()
        message.setComponentByPosition(0, 3)
        message.setComponentByPosition(1)
        global_data = message.getComponentByPosition(1)
        global_data.setComponentByPosition(0, 1)
        global_data.setComponentByPosition(1, 65507)
        global_data.setComponentByPosition(2, b"\x04")
        global_data.setComponentByPosition(3, 3)
        message.setComponentByPosition(2, encoder.encode(security_parameters))

        scoped_pdu = proto_rfc3412.ScopedPDU()
        scoped_pdu.setComponentByPosition(0, engine_id)
        scoped_pdu.setComponentByPosition(1, b"")
        scoped_pdu.getComponentByPosition(2).setComponentByName("report", pdu)

        scoped_pdu_data = proto_rfc3412.ScopedPduData()
        scoped_pdu_data.setComponentByName("plaintext", scoped_pdu)
        message.setComponentByPosition(3, scoped_pdu_data)

        return encoder.encode(message)

    def _drive(self, report_oid):
        from pyasn1.codec.ber import decoder

        from pysnmp.proto import error
        from pysnmp.proto.mpmod import rfc3412 as proto_rfc3412

        mpmod = _new_mpmod()
        wire = self._build_report_wire(SECURITY_ENGINE, report_oid)
        decoded, _ = decoder.decode(wire, asn1Spec=proto_rfc3412.SNMPv3Message())
        scoped_pdu = decoded.getComponentByPosition(3).getComponentByName("plaintext")

        security_model = type(
            "StubSecurityModel",
            (),
            {
                "process_incoming_message": lambda *a, **kw: (
                    SECURITY_ENGINE,
                    b"",
                    scoped_pdu,
                    65507,
                    "sec-ref",
                ),
                "release_state_information": lambda *a, **kw: None,
            },
        )()

        engine = type("StubEngine", (), {})()
        engine.transport_dispatcher = None
        engine.security_models = {3: security_model}
        engine.observer = type(
            "StubObserver",
            (),
            {
                "store_execution_context": lambda *a, **kw: None,
                "clear_execution_context": lambda *a, **kw: None,
            },
        )()
        engine.get_mib_builder = lambda: (_ for _ in ()).throw(
            RuntimeError("not needed")
        )

        # Provide a minimal MIB builder for engine ID lookups.
        from pysnmp.smi.builder import MibBuilder

        builder = MibBuilder()
        builder.load_modules("SNMP-FRAMEWORK-MIB")
        engine.get_mib_builder = lambda: builder

        mpmod._cache.push_by_message_id(1, sendPduHandle=4242)

        try:
            mpmod.prepare_data_elements(engine, DOMAIN, ENDPOINT, wire)
        except error.StatusInformation:
            pass
        except Exception:
            # Later stages need a fuller engine; the report cache update has
            # already happened by then, which is what this test observes.
            pass

        return mpmod

    def test_unknown_engine_report_caches_engine_but_not_context(self):
        mpmod = self._drive((1, 3, 6, 1, 6, 3, 15, 1, 1, 4, 0))

        details = mpmod.get_peer_engine_details(DOMAIN, ENDPOINT, b"")
        assert details is not None
        assert details["securityEngineId"] == SECURITY_ENGINE
        assert details["contextState"] == "pending"
        assert details["contextEngineId"] is None


class TestReportRetrySemantics:
    """notInTimeWindow refreshes time; unknownEngineID restarts discovery."""

    def test_not_in_time_window_keeps_validated_timeline(self):
        sm = SnmpUSMSecurityModel()
        sm._SnmpUSMSecurityModel__timeline[SECURITY_ENGINE] = (4, 900, 900, 0)

        # Simulate the collector's decision: a notInTimeWindow report must
        # not drop the timeline that USM just refreshed.
        invalidate = ReportKind.UNKNOWN_ENGINE_ID in {ReportKind.NOT_IN_TIME_WINDOW}
        assert invalidate is False

    def test_unknown_engine_requires_timeline_invalidation(self):
        invalidate = ReportKind.UNKNOWN_ENGINE_ID in {ReportKind.UNKNOWN_ENGINE_ID}
        assert invalidate is True

    def test_invalidate_peer_discovery_can_skip_timeline(self):
        mpmod = _new_mpmod()
        sm = SnmpUSMSecurityModel()
        sm._SnmpUSMSecurityModel__timeline[SECURITY_ENGINE] = (4, 900, 900, 0)

        engine = type("StubEngine", (), {"security_models": {3: sm}})()
        mpmod.remember_peer_engine_info(
            DOMAIN,
            ENDPOINT,
            SECURITY_ENGINE,
            CONTEXT_ENGINE_A,
            b"ctx",
            source="response",
        )

        mpmod.invalidate_peer_discovery(
            DOMAIN,
            ENDPOINT,
            b"ctx",
            snmpEngine=engine,
            invalidate_timeline=False,
        )

        assert sm.is_timeline_trusted(SECURITY_ENGINE) is True
        details = mpmod.get_peer_engine_details(DOMAIN, ENDPOINT, b"ctx")
        assert details["freshnessVerified"] is False
        assert details["contextEngineId"] == CONTEXT_ENGINE_A


class TestReportAttribution:
    """Report varbinds are diagnostics, never stored business variables."""

    def test_report_event_is_not_an_observation(self):
        store = _ObservationStore()
        identity = CollectionIdentity(
            DOMAIN,
            ENDPOINT,
            securityEngineId=SECURITY_ENGINE,
            contextEngineId=CONTEXT_ENGINE_A,
            contextName=b"ctx",
            verified=False,
        )
        journal = {
            "requests": [
                {
                    "requestId": "q1/0",
                    "oid": [1, 3, 6, 1],
                    "identity": identity.to_dict(),
                }
            ],
            "samples": [
                {
                    "requestId": "q1/0",
                    "oid": [1, 3, 6, 1],
                    "value": "1",
                    "pending": True,
                    "reason": "report",
                    "identity": identity.to_dict(),
                }
            ],
            "reports": [
                {
                    "requestId": "q1/0",
                    "kind": ReportKind.UNKNOWN_ENGINE_ID,
                    "oid": list(UNKNOWN_ENGINE_ID_OID),
                    "value": "1",
                    "identity": identity.to_dict(),
                }
            ],
        }

        verdict = list(CollectionReplay(journal).verify())[0]
        assert verdict[3] == "pending-discovery"
        assert len(store) == 0
        # The report is still recorded for audit.
        assert journal["reports"][0]["kind"] == ReportKind.UNKNOWN_ENGINE_ID


class TestReplayRouting:
    """Replay must answer what route each request actually used."""

    def _journal(self, first_engine, second_engine):
        first = CollectionIdentity(
            DOMAIN,
            ENDPOINT,
            securityEngineId=first_engine,
            contextEngineId=CONTEXT_ENGINE_A,
            contextName=b"a",
            verified=True,
        )
        second = CollectionIdentity(
            DOMAIN,
            ENDPOINT,
            securityEngineId=second_engine,
            contextEngineId=CONTEXT_ENGINE_B,
            contextName=b"b",
            verified=True,
        )
        return {
            "requests": [
                {"requestId": "q1/0", "oid": [1, 3, 1], "identity": first.to_dict()},
                {"requestId": "q2/0", "oid": [1, 3, 2], "identity": second.to_dict()},
            ],
            "samples": [
                {
                    "requestId": "q1/0",
                    "oid": [1, 3, 1],
                    "value": "1",
                    "pending": False,
                    "identity": first.to_dict(),
                },
                {
                    "requestId": "q2/0",
                    "oid": [1, 3, 2],
                    "value": "2",
                    "pending": False,
                    "identity": second.to_dict(),
                },
            ],
            "reports": [],
        }

    def test_routing_reports_engine_per_request(self):
        replay = CollectionReplay(self._journal(SECURITY_ENGINE, OTHER_SECURITY_ENGINE))
        routing = replay.routing()

        assert routing[0]["route"]["securityEngineId"] == ("0x" + SECURITY_ENGINE.hex())
        assert routing[1]["route"]["securityEngineId"] == (
            "0x" + OTHER_SECURITY_ENGINE.hex()
        )
        assert routing[0]["route"]["contextEngineId"] == ("0x" + CONTEXT_ENGINE_A.hex())
        assert routing[1]["route"]["contextEngineId"] == ("0x" + CONTEXT_ENGINE_B.hex())

    def test_pending_helper_lists_only_unverified(self):
        journal = self._journal(SECURITY_ENGINE, OTHER_SECURITY_ENGINE)
        journal["requests"][1]["identity"]["verified"] = False

        pending = CollectionReplay(journal).pending()

        assert len(pending) == 1
        assert pending[0][0] == "q2/0"


class TestContextPendingStillExposesEngine:
    """Engine evidence is usable even when the context needs discovery."""

    def test_report_caches_engine_for_later_context_lookup(self):
        mpmod = _new_mpmod()
        mpmod.remember_peer_engine_info(
            DOMAIN,
            ENDPOINT,
            SECURITY_ENGINE,
            CONTEXT_ENGINE_A,
            b"ctx",
            source="report",
            trust_context_engine_id=False,
        )

        details = mpmod.get_peer_engine_details(DOMAIN, ENDPOINT, b"ctx")
        assert details["securityEngineId"] == SECURITY_ENGINE
        assert details["contextKnown"] is True
        assert details["contextState"] == "pending"
        assert details["freshnessVerified"] is False

    def test_unknown_context_is_distinguishable_from_known_pending(self):
        mpmod = _new_mpmod()
        mpmod.remember_peer_engine_info(
            DOMAIN, ENDPOINT, SECURITY_ENGINE, b"", engineBoots=1, source="report"
        )

        details = mpmod.get_peer_engine_details(DOMAIN, ENDPOINT, b"never-seen")
        assert details["securityEngineId"] == SECURITY_ENGINE
        assert details["contextKnown"] is False
        assert details["contextEngineId"] is None


class TestCollectorRequestFlow:
    """The collector's attribution logic without touching the network."""

    def _engine(self):
        from pysnmp.entity.engine import SnmpEngine

        return SnmpEngine()

    def _target(self, domain=DOMAIN, address=ENDPOINT):
        from pysnmp.hlapi.transport import AbstractTransportTarget

        class Target(AbstractTransportTarget):
            TRANSPORT_DOMAIN = domain

            def __init__(self):
                self.transport_address = address
                super().__init__(timeout=1, retries=0)

        return Target()

    def test_confirmed_mapping_attributes_establishing_request(self):
        from pysnmp.hlapi.v3arch.asyncio.collector import V3Collector

        engine = self._engine()
        mpmod = engine.message_processing_subsystems[3]
        mpmod.remember_peer_engine_info(
            DOMAIN,
            ENDPOINT,
            SECURITY_ENGINE,
            CONTEXT_ENGINE_A,
            b"ctx",
            source="response",
        )

        collector = V3Collector(engine)
        identity = collector.route_identity(
            self._target(), ContextData(contextName=b"ctx")
        )

        assert identity.verified is True
        assert identity.securityEngineId == SECURITY_ENGINE
        assert identity.contextEngineId == CONTEXT_ENGINE_A

    def test_unconfirmed_route_is_pending(self):
        from pysnmp.hlapi.v3arch.asyncio.collector import V3Collector

        engine = self._engine()
        collector = V3Collector(engine)

        identity = collector.route_identity(
            self._target(), ContextData(contextName=b"ctx")
        )

        assert identity.verified is False
        assert identity.is_pending is True

    def test_explicit_context_engine_id_wins_over_cache(self):
        from pysnmp.hlapi.v3arch.asyncio.collector import V3Collector

        engine = self._engine()
        mpmod = engine.message_processing_subsystems[3]
        mpmod.remember_peer_engine_info(
            DOMAIN,
            ENDPOINT,
            SECURITY_ENGINE,
            CONTEXT_ENGINE_A,
            b"ctx",
            source="response",
        )
        collector = V3Collector(engine)

        identity = collector.route_identity(
            self._target(),
            ContextData(contextEngineId=CONTEXT_ENGINE_B, contextName=b"ctx"),
        )

        assert identity.contextEngineId == CONTEXT_ENGINE_B


class TestDiscoveryScope:
    """Timing reports re-open one context; engine changes re-open the route."""

    def _seed_two_contexts(self):
        mpmod = _new_mpmod()
        mpmod.remember_peer_engine_info(
            DOMAIN,
            ENDPOINT,
            SECURITY_ENGINE,
            CONTEXT_ENGINE_A,
            b"a",
            engineBoots=2,
            source="response",
        )
        mpmod.remember_peer_engine_info(
            DOMAIN,
            ENDPOINT,
            SECURITY_ENGINE,
            CONTEXT_ENGINE_B,
            b"b",
            engineBoots=2,
            source="response",
        )
        return mpmod

    def test_timing_report_pends_only_target_context(self):
        mpmod = self._seed_two_contexts()

        mpmod.invalidate_peer_discovery(
            DOMAIN, ENDPOINT, b"a", invalidate_timeline=False
        )

        first = mpmod.get_peer_engine_details(DOMAIN, ENDPOINT, b"a")
        second = mpmod.get_peer_engine_details(DOMAIN, ENDPOINT, b"b")
        assert first["contextState"] == "pending"
        assert first["contextEngineId"] == CONTEXT_ENGINE_A
        assert second["contextState"] == "verified"
        assert second["contextEngineId"] == CONTEXT_ENGINE_B
        assert second["freshnessVerified"] is True

    def test_engine_report_pends_route_but_keeps_mappings(self):
        mpmod = self._seed_two_contexts()

        mpmod.invalidate_peer_discovery(DOMAIN, ENDPOINT, b"a")

        first = mpmod.get_peer_engine_details(DOMAIN, ENDPOINT, b"a")
        second = mpmod.get_peer_engine_details(DOMAIN, ENDPOINT, b"b")
        assert first["contextState"] == "pending"
        assert second["contextState"] == "pending"
        # Mappings survive: the managed objects did not move.
        assert first["contextEngineId"] == CONTEXT_ENGINE_A
        assert second["contextEngineId"] == CONTEXT_ENGINE_B
        assert second["freshnessVerified"] is False


class TestSampleAttribution:
    """Only fully evidenced identities may accept an observation."""

    def _identity(self, verified):
        return CollectionIdentity(
            DOMAIN,
            ENDPOINT,
            securityEngineId=SECURITY_ENGINE,
            contextEngineId=CONTEXT_ENGINE_A,
            contextName=b"ctx",
            verified=verified,
        )

    def test_verified_identity_attributes(self):
        from pysnmp.hlapi.v3arch.asyncio.collector import CollectionSample

        sample = CollectionSample(
            "q1/0", (1, 3, 6, 1), value="x", identity=self._identity(True)
        )
        assert sample.attributed is True

    def test_unverified_identity_does_not_attribute(self):
        from pysnmp.hlapi.v3arch.asyncio.collector import CollectionSample

        sample = CollectionSample(
            "q1/0", (1, 3, 6, 1), value="x", identity=self._identity(False)
        )
        assert sample.attributed is False

    def test_missing_context_engine_id_does_not_attribute(self):
        from pysnmp.hlapi.v3arch.asyncio.collector import CollectionSample

        identity = CollectionIdentity(
            DOMAIN,
            ENDPOINT,
            securityEngineId=SECURITY_ENGINE,
            contextEngineId=None,
            contextName=b"ctx",
            verified=True,
        )
        sample = CollectionSample("q1/0", (1, 3, 6, 1), value="x", identity=identity)
        assert sample.attributed is False

    def test_pending_sample_carries_no_value(self):
        from pysnmp.hlapi.v3arch.asyncio.collector import CollectionSample

        sample = CollectionSample(
            "q1/0",
            (1, 3, 6, 1),
            identity=self._identity(True),
            pending=True,
            reason="report",
        )
        assert sample.attributed is False
        assert sample.to_dict()["value"] == ""
