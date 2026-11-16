#
# This file is part of pysnmp software.
#
# Copyright (c) 2005-2020, Ilya Etingof <etingof@gmail.com>
# License: https://www.pysnmp.com/pysnmp/license.html
#
"""SNMPv3 collection over proxies with layered identity accounting.

An SNMPv3 request crosses several identities at once:

* the *transport endpoint* a packet is sent to,
* the *authentication engine* (:RFC:`3414#section-4` authoritative engine)
  that signs and time-windows the message, and
* the *management context* (``contextEngineId``/``contextName``,
  :RFC:`3411#section-3.3`) that owns the managed objects.

Behind a proxy these three do not have to agree.  This module keeps them
apart, so that a context learned through one route is never attributed to
another, a report re-opens only the discovery it actually invalidates, and a
sample is only recorded when the route, engine and context used to fetch it
are all backed by evidence.

The main entry point is :class:`V3Collector`.  It wraps the asynchronous
command generator and records, for every request, the identity that was
actually used on the wire.  The resulting :class:`CollectionSample` objects
can be replayed through :class:`CollectionReplay` after the live SNMP engine
is gone.
"""

import json

from pysnmp import debug
from pysnmp.hlapi.v3arch.asyncio.cmdgen import get_cmd
from pysnmp.hlapi.v3arch.asyncio.context import ContextData
from pysnmp.hlapi.varbinds import CommandGeneratorVarBinds
from pysnmp.proto import api, rfc1902

__all__ = [
    "CollectionIdentity",
    "CollectionRequest",
    "CollectionSample",
    "CollectionReplay",
    "ReportKind",
    "V3Collector",
]

pMod = api.PROTOCOL_MODULES[api.SNMP_VERSION_2C]  # noqa: N816
VB_PROCESSOR = CommandGeneratorVarBinds()

# USM statistics reports that signal the discovery evidence is stale
UNKNOWN_ENGINE_ID_OID = (1, 3, 6, 1, 6, 3, 15, 1, 1, 4, 0)
NOT_IN_TIME_WINDOW_OID = (1, 3, 6, 1, 6, 3, 15, 1, 1, 2, 0)
UNKNOWN_USER_NAME_OID = (1, 3, 6, 1, 6, 3, 15, 1, 1, 3, 0)
UNSUPPORTED_SEC_LEVEL_OID = (1, 3, 6, 1, 6, 3, 15, 1, 1, 1, 0)
WRONG_DIGEST_OID = (1, 3, 6, 1, 6, 3, 15, 1, 1, 5, 0)
DECRYPTION_ERROR_OID = (1, 3, 6, 1, 6, 3, 15, 1, 1, 6, 0)


class ReportKind:
    """Classification of SNMPv3 Report-PDU evidence."""

    UNKNOWN_ENGINE_ID = "unknownEngineID"
    NOT_IN_TIME_WINDOW = "notInTimeWindow"
    UNKNOWN_USER_NAME = "unknownUserName"
    UNSUPPORTED_SEC_LEVEL = "unsupportedSecurityLevel"
    WRONG_DIGEST = "wrongDigest"
    DECRYPTION_ERROR = "decryptionError"
    OTHER = "other"


REPORT_KINDS = {
    UNKNOWN_ENGINE_ID_OID: ReportKind.UNKNOWN_ENGINE_ID,
    NOT_IN_TIME_WINDOW_OID: ReportKind.NOT_IN_TIME_WINDOW,
    UNKNOWN_USER_NAME_OID: ReportKind.UNKNOWN_USER_NAME,
    UNSUPPORTED_SEC_LEVEL_OID: ReportKind.UNSUPPORTED_SEC_LEVEL,
    WRONG_DIGEST_OID: ReportKind.WRONG_DIGEST,
    DECRYPTION_ERROR_OID: ReportKind.DECRYPTION_ERROR,
}

# Reports that say the engine identity or its boot generation moved and that
# the next request must be preceded by (re-)discovery.
REDISCOVERY_REPORTS = frozenset({
    ReportKind.UNKNOWN_ENGINE_ID,
    ReportKind.NOT_IN_TIME_WINDOW,
})


def _as_bytes(value):
    """Normalise an identity value to octets for comparison."""
    if value is None:
        return None
    if isinstance(value, bytes):
        return value
    try:
        return bytes(value)
    except (TypeError, ValueError):
        return value


def _hex(value):
    """Render an identity value as lower-case hex, or ``None``."""
    value = _as_bytes(value)
    if value is None:
        return None
    return "0x" + value.hex()


def _pretty(value):
    if value is None:
        return ""
    try:
        return value.prettyPrint()
    except AttributeError:
        return str(value)


def _extract_oid(objectType):
    """Return an OID from an ObjectType/ObjectIdentity, if it is resolvable.

    OIDs may still be symbolic at this point (MIB resolution happens inside
    the command generator), so a missing OID is not an error: the response
    varbinds provide the authoritative OID list.
    """
    identity = objectType
    if not hasattr(identity, "get_oid"):
        try:
            identity = objectType[0]
        except Exception:
            return None
    try:
        return tuple(identity.get_oid())
    except Exception:
        pass
    try:
        return tuple(rfc1902.ObjectName(identity))
    except Exception:
        return None


class CollectionIdentity:
    """The three identity layers of one request.

    Parameters
    ----------
    transportDomain : tuple
        Transport domain, e.g. ``UdpTransportTarget.TRANSPORT_DOMAIN``.
    transportAddress : tuple
        Remote transport address.
    securityEngineId : bytes
        Authoritative engine that authenticates the message.
    contextEngineId : bytes
        Engine that owns the managed context.
    contextName : bytes
        Context name inside *contextEngineId*.
    engineBoots, engineTime : int
        Last confirmed boot generation and uptime for *securityEngineId*.
    """

    __slots__ = (
        "transportDomain",
        "transportAddress",
        "securityEngineId",
        "contextEngineId",
        "contextName",
        "engineBoots",
        "engineTime",
        "verified",
        "source",
    )

    def __init__(
        self,
        transportDomain,
        transportAddress,
        securityEngineId=None,
        contextEngineId=None,
        contextName=b"",
        engineBoots=None,
        engineTime=None,
        verified=False,
        source=None,
    ):
        self.transportDomain = tuple(transportDomain)
        self.transportAddress = tuple(transportAddress)
        self.securityEngineId = _as_bytes(securityEngineId)
        self.contextEngineId = _as_bytes(contextEngineId)
        self.contextName = _as_bytes(contextName) or b""
        self.engineBoots = engineBoots
        self.engineTime = engineTime
        self.verified = verified
        self.source = source

    @property
    def route(self):
        """Return the ``(transportDomain, transportAddress)`` route."""
        return self.transportDomain, self.transportAddress

    @property
    def is_pending(self):
        """Return True when discovery has not confirmed this identity."""
        return (
            not self.verified
            or self.securityEngineId is None
            or self.contextEngineId is None
        )

    def clone(self, **kwargs):
        """Return a copy with selected fields replaced."""
        values = {
            "transportDomain": self.transportDomain,
            "transportAddress": self.transportAddress,
            "securityEngineId": self.securityEngineId,
            "contextEngineId": self.contextEngineId,
            "contextName": self.contextName,
            "engineBoots": self.engineBoots,
            "engineTime": self.engineTime,
            "verified": self.verified,
            "source": self.source,
        }
        values.update(kwargs)
        return self.__class__(**values)

    def to_dict(self):
        """Return a JSON-friendly representation."""
        return {
            "transportDomain": list(self.transportDomain),
            "transportAddress": list(self.transportAddress),
            "securityEngineId": _hex(self.securityEngineId),
            "contextEngineId": _hex(self.contextEngineId),
            "contextName": _hex(self.contextName),
            "contextNameText": _pretty(self.contextName) or "",
            "engineBoots": self.engineBoots,
            "engineTime": self.engineTime,
            "verified": self.verified,
            "source": self.source,
        }

    def __eq__(self, other):
        if not isinstance(other, CollectionIdentity):
            return NotImplemented
        return self.to_dict() == other.to_dict()

    def __hash__(self):
        raise TypeError("%s is not hashable" % self.__class__.__name__)

    def __repr__(self):
        return (
            "CollectionIdentity(route={!r}, securityEngineId={!r}, "
            "contextEngineId={!r}, contextName={!r}, engineBoots={!r}, "
            "verified={!r})".format(
                self.route,
                _hex(self.securityEngineId),
                _hex(self.contextEngineId),
                _hex(self.contextName),
                self.engineBoots,
                self.verified,
            )
        )


class CollectionRequest:
    """One GET request and the identity it is to be sent with."""

    __slots__ = ("requestId", "oid", "identity", "authData")

    def __init__(self, oid, identity, authData=None, requestId=None):
        self.oid = tuple(oid)
        self.identity = identity
        self.authData = authData
        self.requestId = requestId

    def to_dict(self):
        """Return a JSON-friendly representation for replay."""
        return {
            "requestId": self.requestId,
            "oid": list(self.oid),
            "identity": self.identity.to_dict(),
        }


class CollectionSample:
    """Value fetched for one OID, attributed to the identity that fetched it.

    A pending sample (:attr:`pending` is True) means that the request failed or
    that the evidence backing its identity was missing.  Pending samples carry
    no value and no attribution, so they can never be mistaken for
    observations of a context.
    """

    __slots__ = (
        "requestId",
        "oid",
        "value",
        "identity",
        "pending",
        "reason",
        "reportKind",
        "errorIndication",
    )

    def __init__(
        self,
        requestId,
        oid,
        value=None,
        identity=None,
        pending=False,
        reason=None,
        reportKind=None,
        errorIndication=None,
    ):
        self.requestId = requestId
        self.oid = tuple(oid)
        self.value = value
        self.identity = identity
        self.pending = pending
        self.reason = reason
        self.reportKind = reportKind
        self.errorIndication = errorIndication

    @property
    def attributed(self):
        """Return True when this sample may be stored as an observation."""
        return (
            not self.pending
            and self.identity is not None
            and not self.identity.is_pending
        )

    def to_dict(self):
        """Return a JSON-friendly representation for replay."""
        return {
            "requestId": self.requestId,
            "oid": list(self.oid),
            "value": _pretty(self.value),
            "pending": self.pending,
            "reason": self.reason,
            "reportKind": self.reportKind,
            "errorIndication": str(self.errorIndication)
            if self.errorIndication is not None
            else None,
            "identity": self.identity and self.identity.to_dict(),
        }

    def __repr__(self):
        return "CollectionSample(requestId={!r}, oid={!r}, value={!r}, pending={!r})".format(
            self.requestId, self.oid, _pretty(self.value), self.pending
        )


class _ObservationStore:
    """Verified observations, keyed by route and context identity.

    Each ``(route, securityEngineId, contextEngineId, contextName)`` tuple owns
    an independent entry.  In particular, two contexts sharing one transport
    endpoint never alias, and re-discovering one of them does not clear the
    others.
    """

    def __init__(self):
        self._entries = {}
        self._stale = set()

    @staticmethod
    def _key(identity):
        return (
            identity.route,
            _as_bytes(identity.securityEngineId),
            _as_bytes(identity.contextEngineId),
            _as_bytes(identity.contextName),
        )

    def record(self, sample):
        """Store a sample if it carries verified attribution."""
        if not sample.attributed:
            return False
        key = self._key(sample.identity)
        self._entries.setdefault(key, {})[sample.oid] = sample.value
        self._stale.discard(key)
        return True

    def mark_stale(self, identity):
        """Mark one context's observations as no longer fresh.

        The values stay available for audit, but they no longer authorize a
        new request and must not be attributed to one.  Other contexts on the
        same endpoint are unaffected.
        """
        self._stale.add(self._key(identity))

    def mark_route_stale(self, route):
        """Mark every context behind *route* as no longer fresh.

        Used when the authoritative engine identity or its boot generation
        moved: the route-wide freshness evidence is void, yet the values stay
        in the store for audit and are not deleted.
        """
        route = tuple(route)
        for key in self._entries:
            if key[0] == route:
                self._stale.add(key)

    def is_stale(self, identity):
        """Return True when *identity*'s observations lost freshness."""
        return self._key(identity) in self._stale

    def get(self, identity, oid):
        """Return a fresh stored value for *identity* and *oid*, if any."""
        if self.is_stale(identity):
            return None
        return self._entries.get(self._key(identity), {}).get(tuple(oid))

    def observations_for(self, identity):
        """Return all observations belonging to *identity*."""
        return dict(self._entries.get(self._key(identity), {}))

    def observations_for_route(self, route):
        """Return all observations made over *route*, across its contexts."""
        result = {}
        for key, values in self._entries.items():
            if key[0] == tuple(route):
                result[key] = dict(values)
        return result

    def invalidate(self, identity):
        """Drop the observations of one context identity only."""
        key = self._key(identity)
        self._entries.pop(key, None)
        self._stale.discard(key)

    def invalidate_route(self, route):
        """Drop all observations made over one route."""
        route = tuple(route)
        for key in list(self._entries):
            if key[0] == route:
                del self._entries[key]
                self._stale.discard(key)

    def to_journal(self):
        """Return all verified observations in a replayable form."""
        result = []
        for key, values in self._entries.items():
            route, securityEngineId, contextEngineId, contextName = key
            result.append({
                "identity": CollectionIdentity(
                    route[0],
                    route[1],
                    securityEngineId=securityEngineId,
                    contextEngineId=contextEngineId,
                    contextName=contextName,
                    verified=True,
                ).to_dict(),
                "oids": [list(oid) for oid in values],
                "stale": key in self._stale,
            })
        return result

    def clear(self):
        """Drop every stored observation."""
        self._entries.clear()
        self._stale.clear()

    def __len__(self):
        return sum(len(values) for values in self._entries.values())


class ReportEvent:
    """A classified report received for one request."""

    __slots__ = ("requestId", "kind", "oid", "value", "identity", "raw")

    def __init__(self, requestId, kind, oid, value, identity, raw=None):
        self.requestId = requestId
        self.kind = kind
        self.oid = tuple(oid)
        self.value = value
        self.identity = identity
        self.raw = raw

    def __repr__(self):
        return "ReportEvent(requestId={!r}, kind={!r}, oid={!r})".format(
            self.requestId, self.kind, self.oid
        )


class CollectionReplay:
    """Replay recorded samples and verify the identity used for each request.

    The replay never touches the network: it reads the journal produced by
    :meth:`V3Collector.journal` and answers three questions for every entry:
    which route was intended, which engine/context identity was actually
    used, and whether that identity had enough evidence to be attributed.
    Entries without evidence are reported as pending discovery instead of being
    silently dropped or attributed to the current default.
    """

    def __init__(self, journal):
        if isinstance(journal, str):
            journal = json.loads(journal)
        self.journal = journal

    @classmethod
    def from_json(cls, payload):
        """Build a replay from a JSON document or string."""
        return cls(payload)

    def identities(self):
        """Return the identities of every recorded request, by request ID."""
        return {
            entry.get("requestId"): self._identity_from_dict(entry.get("identity"))
            for entry in self.journal.get("requests", [])
        }

    def samples(self):
        """Return the recorded sample mappings."""
        return list(self.journal.get("samples", []))

    def to_json(self):
        """Serialise the journal this replay was built from."""
        return json.dumps(self.journal, indent=2, sort_keys=True)

    def requests(self):
        """Return the reconstructed :class:`CollectionRequest` list."""
        requests = []
        for entry in self.journal.get("requests", []):
            identity = self._identity_from_dict(entry.get("identity"))
            requests.append(
                CollectionRequest(
                    entry.get("oid", ()),
                    identity,
                    requestId=entry.get("requestId"),
                )
            )
        return requests

    @staticmethod
    def _identity_from_dict(data):
        if not data:
            return None
        return CollectionIdentity(
            data.get("transportDomain", ()),
            data.get("transportAddress", ()),
            securityEngineId=_from_hex(data.get("securityEngineId")),
            contextEngineId=_from_hex(data.get("contextEngineId")),
            contextName=_from_hex(data.get("contextName")) or b"",
            engineBoots=data.get("engineBoots"),
            engineTime=data.get("engineTime"),
            verified=data.get("verified", False),
            source=data.get("source"),
        )

    def request_identity(self, requestId):
        """Return the identity recorded for *requestId*, or ``None``."""
        for entry in self.journal.get("requests", []):
            if entry.get("requestId") == requestId:
                return self._identity_from_dict(entry.get("identity"))
        return None

    def pending(self):
        """Return ``(requestId, oid, detail)`` for entries needing discovery."""
        return [
            (requestId, oid, detail)
            for requestId, oid, _identity, state, detail in self.verify()
            if state == "pending-discovery"
        ]

    def routing(self):
        """Return per-request routing facts captured in the journal.

        This is the audit view asked of a replay: what route, which engine,
        which context and which boot generation each request actually used.
        """
        result = []
        for entry in self.journal.get("requests", []):
            identity = self._identity_from_dict(entry.get("identity"))
            result.append({
                "requestId": entry.get("requestId"),
                "oid": list(entry.get("oid", ())),
                "route": identity and identity.to_dict() or None,
            })
        return result

    def verify(self):
        """Yield ``(requestId, oid, identity, state, detail)`` per request.

        *state* is either ``"verified"`` (the route, engine and context were
        all recorded and confirmed) or ``"pending-discovery"``.  Pending
        discovery covers both "never confirmed" and "the journal lacks the
        evidence to tell", so a caller who cannot verify an entry is told to
        run discovery rather than being handed a guess.
        """
        samples = {
            entry.get("requestId"): entry for entry in self.journal.get("samples", [])
        }
        for entry in self.journal.get("requests", []):
            requestId = entry.get("requestId")
            oid = tuple(entry.get("oid", ()))
            identity = self._identity_from_dict(entry.get("identity"))
            sample = samples.get(requestId)

            if identity is None or identity.securityEngineId is None:
                yield (
                    requestId,
                    oid,
                    identity,
                    "pending-discovery",
                    "journal does not record an authoritative engine",
                )
                continue

            if identity.is_pending:
                yield (
                    requestId,
                    oid,
                    identity,
                    "pending-discovery",
                    "engine/context identity was not confirmed",
                )
                continue

            if sample is None:
                yield (
                    requestId,
                    oid,
                    identity,
                    "pending-discovery",
                    "journal does not record the outcome of this request",
                )
                continue

            if sample.get("pending"):
                yield (
                    requestId,
                    oid,
                    identity,
                    "pending-discovery",
                    sample.get("reason") or "request did not produce a sample",
                )
                continue

            yield (requestId, oid, identity, "verified", None)


def _from_hex(value):
    if value is None:
        return None
    if value.startswith("0x"):
        return bytes.fromhex(value[2:])
    return value.encode("latin-1")


def journal_sample(journal, requestId):
    """Return the recorded sample for *requestId* as a mapping, if any."""
    for entry in journal.get("samples", []):
        if entry.get("requestId") == requestId:
            return entry
    return None


class _ObserverRecorder:
    """Capture what the SNMPv3 stack actually used for an outgoing request."""

    def __init__(self, collector):
        self.collector = collector

    def __call__(self, snmpEngine, execpoint, variables, cbCtx):
        if execpoint == "rfc3412.prepareOutgoingMessage":
            self.collector._record_outgoing(variables)
        elif execpoint == "rfc3412.prepareDataElements:internal":
            self.collector._record_incoming_report(variables)


class V3Collector:
    """Collect SNMPv3 variables while tracking layered identity.

    Parameters
    ----------
    snmpEngine : :py:class:`~pysnmp.entity.engine.SnmpEngine`
        Engine used for the requests.  It must be the same engine passed to
        :func:`~pysnmp.hlapi.v3arch.asyncio.get_cmd`.
    """

    def __init__(self, snmpEngine=None, replay=None):
        self.snmpEngine = snmpEngine
        self.replay = replay
        self.observations = _ObservationStore()
        self._journal = {"requests": [], "samples": [], "reports": []}
        self._lastOutgoing = {}
        self._attemptReports = []
        self._supersededReports = []
        self._observer = None
        self._requestCounter = 0
        self._currentRequestId = None

    # -- lifecycle ---------------------------------------------------------

    def attach(self, snmpEngine=None):
        """Attach the collector's observer to an SNMP engine."""
        if snmpEngine is not None:
            self.snmpEngine = snmpEngine
        if self.snmpEngine is None:
            raise ValueError("snmpEngine is required")
        if self._observer is None:
            self._observer = _ObserverRecorder(self)
            self.snmpEngine.observer.register_observer(
                self._observer,
                "rfc3412.prepareOutgoingMessage",
                "rfc3412.prepareDataElements:internal",
            )
        return self

    def detach(self):
        """Remove the collector's observer from the SNMP engine."""
        if self._observer is not None and self.snmpEngine is not None:
            self.snmpEngine.observer.unregister_observer(self._observer)
            self._observer = None

    def __enter__(self):
        return self.attach()

    def __exit__(self, *exc_info):
        self.detach()

    # -- identity helpers --------------------------------------------------

    def route_identity(self, transportTarget, contextData=None):
        """Return the identity currently known for a route/context.

        The returned identity is marked ``verified`` only when the route's
        authoritative engine, its boot/time freshness and this context's
        ``contextEngineId`` were all confirmed.  Otherwise it is the
        *pending discovery* state and the caller must not attach a sample to
        it, even if a known mapping is still present for addressing.
        """
        contextData = contextData or ContextData()
        if self.snmpEngine is None:
            raise ValueError("snmpEngine is required")
        domain, address = transportTarget.get_transport_info()
        details = None
        try:
            mpmod = self.snmpEngine.message_processing_subsystems[3]
            details = mpmod.get_peer_engine_details(
                domain, address, contextData.contextName
            )
        except (AttributeError, KeyError):  # pragma: no cover - defensive
            details = None

        if details is None:
            return CollectionIdentity(
                domain,
                address,
                contextEngineId=_as_bytes(contextData.contextEngineId),
                contextName=contextData.contextName,
                verified=False,
                source="pending-discovery",
            )

        contextEngineId = details.get("contextEngineId")
        if contextData.contextEngineId is not None:
            # An explicitly supplied contextEngineId is what the request will
            # actually carry, so it, not the cached mapping, is the identity
            # to attribute the sample to.
            contextEngineId = _as_bytes(contextData.contextEngineId)

        verified = (
            details.get("securityEngineId") is not None
            and details.get("contextState") == "verified"
            and details.get("freshnessVerified", False)
            and contextEngineId is not None
        )

        return CollectionIdentity(
            domain,
            address,
            securityEngineId=details.get("securityEngineId"),
            contextEngineId=contextEngineId,
            contextName=details.get("contextName") or contextData.contextName,
            engineBoots=details.get("engineBoots"),
            engineTime=details.get("engineTime"),
            verified=verified,
            source=details.get("source"),
        )

    def _record_outgoing(self, variables):
        # Every outgoing message starts a new attempt.  Reports seen before
        # this point triggered the retry/discovery that produced this
        # attempt.  They still matter for the freshness of earlier samples,
        # but they must not decide the final outcome (a successful
        # rediscovery is not a failure).
        if self._attemptReports:
            self._supersededReports.extend(self._attemptReports)
            self._attemptReports = []
        identity = CollectionIdentity(
            variables.get("transportDomain", ()),
            variables.get("transportAddress", ()),
            securityEngineId=variables.get("securityEngineId"),
            contextEngineId=variables.get("contextEngineId"),
            contextName=variables.get("contextName") or b"",
            engineBoots=variables.get("engineBoots"),
            engineTime=variables.get("engineTime"),
            verified=(
                variables.get("securityEngineId") is not None
                and variables.get("contextState") == "verified"
                and variables.get("freshnessVerified", False)
            ),
            source="outgoing",
        )
        self._lastOutgoing = identity

    def _record_incoming_report(self, variables):
        pdu = variables.get("pdu")
        if pdu is None:
            return
        varBinds = pMod.apiPDU.get_varbinds(pdu)
        oid = varBinds and tuple(varBinds[0][0]) or ()
        value = varBinds and varBinds[0][1] or None
        kind = REPORT_KINDS.get(oid, ReportKind.OTHER)
        identity = CollectionIdentity(
            variables.get("transportDomain", ()),
            variables.get("transportAddress", ()),
            securityEngineId=variables.get("securityEngineId"),
            contextEngineId=variables.get("contextEngineId"),
            contextName=variables.get("contextName") or b"",
            verified=False,
            source="report",
        )
        event = ReportEvent(self._currentRequestId, kind, oid, value, identity, raw=pdu)
        self._attemptReports.append(event)
        self._journal["reports"].append({
            "requestId": self._currentRequestId,
            "kind": kind,
            "oid": list(oid),
            "value": _pretty(value),
            "identity": identity.to_dict(),
        })
        debug.logger & debug.FLAG_APP and debug.logger(
            "V3Collector: report {} for request {}".format(kind, self._currentRequestId)
        )

    # -- requests ----------------------------------------------------------

    async def get(
        self,
        transportTarget,
        authData,
        contextData,
        *objectTypes,
        requestId=None,
        **options,
    ):
        """Fetch OIDs, recording which identity was used for each sample."""
        if self.snmpEngine is None:
            raise ValueError("snmpEngine is required")
        objectTypes = tuple(objectTypes)
        if not objectTypes:
            return []

        if requestId is None:
            self._requestCounter += 1
            requestId = "q%s" % self._requestCounter

        self._currentRequestId = requestId
        self._lastOutgoing = {}
        self._attemptReports = []
        self._supersededReports = []

        planned = self.route_identity(transportTarget, contextData)

        # Resolve symbolic OIDs before dispatch.  The command generator would
        # resolve them anyway, but keeping the resolved objects here means the
        # journal can name the exact OID even when the request fails before a
        # response arrives.
        resolvedObjectTypes = VB_PROCESSOR.make_varbinds(
            self.snmpEngine.cache, objectTypes
        )

        try:
            (
                errorIndication,
                errorStatus,
                errorIndex,
                varBinds,
            ) = await get_cmd(
                self.snmpEngine,
                authData,
                transportTarget,
                contextData,
                *resolvedObjectTypes,
                **options,
            )
        except Exception as exc:  # keep the evidence trail on failure
            identity = self._lastOutgoing or planned
            self._journal["requests"].append({
                "requestId": requestId,
                "oid": [list(_extract_oid(resolvedObjectTypes[0]) or ())],
                "identity": identity.to_dict(),
            })
            sample = CollectionSample(
                requestId,
                _extract_oid(resolvedObjectTypes[0]) or (),
                identity=identity,
                pending=True,
                reason="exception",
                errorIndication=exc,
            )
            self._journal["samples"].append(sample.to_dict())
            return [sample]

        # The observer saw the message that was actually built: it is the
        # authoritative statement of the route/engine/context used on the
        # wire.  Fall back to the pre-request snapshot only if no message
        # was ever dispatched (e.g. a dispatch-time exception).
        if self._lastOutgoing and self._lastOutgoing.securityEngineId is not None:
            identity = self._lastOutgoing
        else:
            identity = planned

        # The observer runs before the response is parsed, so its verdict
        # cannot include confirmation carried by this very response.  If the
        # cache now confirms exactly the identity that was used on the wire,
        # adopt that confirmation: the request that established the mapping
        # is the one whose value belongs to it.
        confirmed = self.route_identity(transportTarget, contextData)
        if (
            confirmed.verified
            and confirmed.route == identity.route
            and confirmed.securityEngineId == identity.securityEngineId
            and confirmed.contextEngineId == identity.contextEngineId
            and confirmed.contextName == identity.contextName
        ):
            identity = identity.clone(
                verified=True,
                engineBoots=confirmed.engineBoots,
                engineTime=confirmed.engineTime,
            )
        requestEntries = []
        samples = []

        # Reports tell us about discovery, not about business variables.
        # Reports from superseded attempts describe a real identity change,
        # so values gathered before the retry are stale; but they must not
        # roll back the discovery the successful retry just completed.
        # Reports from the final attempt had no successful follow-up, so
        # they also re-open discovery for the next request.
        supersededKinds = [event.kind for event in self._supersededReports]
        reportKinds = [event.kind for event in self._attemptReports]

        if errorIndication is None:
            self._apply_reports(identity, supersededKinds, invalidate_discovery=False)
        self._apply_reports(identity, reportKinds, invalidate_discovery=True)

        responseVarBinds = list(varBinds or ())
        for index, objectType in enumerate(resolvedObjectTypes):
            requestedOid = _extract_oid(objectType)
            if index < len(responseVarBinds):
                responseOid, value = responseVarBinds[index]
                oid = tuple(responseOid)
                if requestedOid is None:
                    requestedOid = oid
            else:
                oid = requestedOid or ()
                value = None

            if requestedOid is not None and oid != requestedOid:
                # A well-behaved GET response echoes the requested OID.  If
                # it does not, keep the sample attached to what we asked for
                # rather than silently attributing the value to another OID.
                oid = requestedOid
                value = None

            requestEntries.append({
                "requestId": "%s/%d" % (requestId, index),
                "oid": list(oid),
                "identity": identity.to_dict(),
            })

            if errorIndication is not None:
                sample = CollectionSample(
                    "%s/%d" % (requestId, index),
                    oid,
                    identity=identity,
                    pending=True,
                    reason="report" if reportKinds else "error",
                    reportKind=reportKinds and reportKinds[0] or None,
                    errorIndication=errorIndication,
                )
            elif errorStatus:
                sample = CollectionSample(
                    "%s/%d" % (requestId, index),
                    oid,
                    identity=identity,
                    pending=True,
                    reason="errorStatus",
                    errorIndication=errorStatus,
                )
            else:
                sample = CollectionSample(
                    "%s/%d" % (requestId, index),
                    oid,
                    value=value,
                    identity=identity,
                    pending=value is None,
                    reason=value is None and "no value" or None,
                )

            if sample.attributed:
                self.observations.record(sample)
            samples.append(sample)
            self._journal["samples"].append(sample.to_dict())

        self._journal["requests"].extend(requestEntries)
        return samples

    def _apply_reports(self, identity, reportKinds, invalidate_discovery=True):
        """Invalidate only the evidence a report actually targets.

        ``unknownEngineID`` means the authoritative engine identity moved, so
        every context behind that route loses freshness (the values are kept
        for audit, not deleted).  ``notInTimeWindow`` means only the timeline
        of the context that received it moved, so only that context is
        re-opened; other contexts sharing the endpoint stay verified.
        Credential reports (unknown user, wrong digest, decryption error,
        unsupported security level) do not invalidate identity at all.
        """
        if not reportKinds or self.snmpEngine is None:
            return

        route = identity.route
        if not any(kind in REDISCOVERY_REPORTS for kind in reportKinds):
            return

        # ``notInTimeWindow`` reports carry the authoritative engine's
        # corrected timeline, which USM has already validated and installed.
        # Dropping it would make every retry fail again; only an unknown
        # engine means the old timeline belongs to a different engine.
        invalidate_timeline = ReportKind.UNKNOWN_ENGINE_ID in reportKinds

        # Old freshness evidence no longer authorizes new requests.  Keep the
        # values for audit (never delete them) but mark them stale: an engine
        # change is route-wide, while a boot/time report concerns only the
        # context that received it.
        if invalidate_timeline:
            self.observations.mark_route_stale(route)
        else:
            self.observations.mark_stale(identity)

        if not invalidate_discovery:
            return

        try:
            mpmod = self.snmpEngine.message_processing_subsystems[3]
            mpmod.invalidate_peer_discovery(
                route[0],
                route[1],
                identity.contextName,
                snmpEngine=self.snmpEngine,
                invalidate_timeline=invalidate_timeline,
            )
        except (AttributeError, KeyError):  # pragma: no cover - defensive
            pass

    # -- replay ------------------------------------------------------------

    def journal(self):
        """Return the recorded request/sample/report journal.

        Every request entry records the actual route, authoritative engine,
        context and boot generation used on the wire.  Samples are listed
        separately so a pending or failed request stays visible rather than
        disappearing.  Reports are evidence about discovery, not business
        variables, and are never part of the observations.
        """
        return {
            "requests": list(self._journal["requests"]),
            "samples": list(self._journal["samples"]),
            "reports": list(self._journal["reports"]),
            "observations": self.observations.to_journal(),
        }

    def replay_recorder(self):
        """Return a replay object bound to the current journal."""
        return CollectionReplay(self.journal())
