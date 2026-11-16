#
# This file is part of pysnmp software.
#
# Copyright (c) 2005-2020, Ilya Etingof <etingof@gmail.com>
# License: https://www.pysnmp.com/pysnmp/license.html
#
from __future__ import annotations

import sys
import time
from typing import TYPE_CHECKING

from pyasn1.codec.ber import decoder, eoo
from pyasn1.type import constraint, namedtype, univ
from pysnmp import debug
from pysnmp.proto import api, errind, error, rfc1905, rfc3411
from pysnmp.proto.mpmod.base import AbstractMessageProcessingModel
from pysnmp.proto.secmod.base import AbstractSecurityModel

if TYPE_CHECKING:
    from pysnmp.entity.engine import SnmpEngine


# API to rfc1905 protocol objects
pMod = api.PROTOCOL_MODULES[api.SNMP_VERSION_2C]  # noqa: N816


# SNMPv3 message format


class ScopedPDU(univ.Sequence):
    """Scoped PDU syntax."""

    componentType = namedtype.NamedTypes(  # noqa: N815
        namedtype.NamedType("contextEngineId", univ.OctetString()),
        namedtype.NamedType("contextName", univ.OctetString()),
        namedtype.NamedType("data", rfc1905.PDUs()),
    )


class ScopedPduData(univ.Choice):
    """Scoped PDU data syntax."""

    componentType = namedtype.NamedTypes(  # noqa: N815
        namedtype.NamedType("plaintext", ScopedPDU()),
        namedtype.NamedType("encryptedPDU", univ.OctetString()),
    )


class HeaderData(univ.Sequence):
    """SNMPv3 message header data syntax."""

    componentType = namedtype.NamedTypes(  # noqa: N815
        namedtype.NamedType(
            "msgID",
            univ.Integer().subtype(
                subtypeSpec=constraint.ValueRangeConstraint(0, 2147483647)
            ),
        ),
        namedtype.NamedType(
            "msgMaxSize",
            univ.Integer().subtype(
                subtypeSpec=constraint.ValueRangeConstraint(484, 2147483647)
            ),
        ),
        namedtype.NamedType(
            "msgFlags",
            univ.OctetString().subtype(
                subtypeSpec=constraint.ValueSizeConstraint(1, 1)
            ),
        ),
        # NOTE (etingof): constrain SNMPv3 message to only USM+ security models
        # because SNMPv1/v2c seems incompatible in pysnmp implementation, not sure
        # if it's intended by the SNMP standard at all...
        namedtype.NamedType(
            "msgSecurityModel",
            univ.Integer().subtype(
                subtypeSpec=constraint.ValueRangeConstraint(3, 2147483647)
            ),
        ),
    )


class SNMPv3Message(univ.Sequence):
    """SNMPv3 message syntax."""

    componentType = namedtype.NamedTypes(  # noqa: N815
        namedtype.NamedType(
            "msgVersion",
            univ.Integer().subtype(
                subtypeSpec=constraint.ValueRangeConstraint(0, 2147483647)
            ),
        ),
        namedtype.NamedType("msgGlobalData", HeaderData()),
        namedtype.NamedType("msgSecurityParameters", univ.OctetString()),
        namedtype.NamedType("msgData", ScopedPduData()),
    )


# XXX move somewhere?
_snmpErrors = {  # noqa: N816
    (1, 3, 6, 1, 6, 3, 15, 1, 1, 1, 0): errind.unsupportedSecurityLevel,
    (1, 3, 6, 1, 6, 3, 15, 1, 1, 2, 0): errind.notInTimeWindow,
    (1, 3, 6, 1, 6, 3, 15, 1, 1, 3, 0): errind.unknownUserName,
    (1, 3, 6, 1, 6, 3, 15, 1, 1, 4, 0): errind.unknownEngineID,
    (1, 3, 6, 1, 6, 3, 15, 1, 1, 5, 0): errind.wrongDigest,
    (1, 3, 6, 1, 6, 3, 15, 1, 1, 6, 0): errind.decryptionError,
}


class SnmpV3MessageProcessingModel(AbstractMessageProcessingModel):
    """Implement SNMPv3 message processing model."""

    MESSAGE_PROCESSING_MODEL_ID = univ.Integer(3)  # SNMPv3
    SNMP_MSG_SPEC = SNMPv3Message
    _empty_string = univ.OctetString("")
    _message_flags = {
        0: univ.OctetString("\x00"),
        1: univ.OctetString("\x01"),
        3: univ.OctetString("\x03"),
        4: univ.OctetString("\x04"),
        5: univ.OctetString("\x05"),
        7: univ.OctetString("\x07"),
    }

    def __init__(self):
        """Create SNMPv3 message processing model instance."""
        AbstractMessageProcessingModel.__init__(self)
        self.__scopedPDU = ScopedPDU()
        # Peer identity is kept in layers.  A transport endpoint speaks for
        # one authoritative SNMP engine, while the same endpoint can expose
        # many SNMP contexts, each owning a different contextEngineId.
        self.__engineIdCache = {}
        self.__contextCache = {}
        self.__engineIdCacheExpiry = {}
        self.__engineIdCacheExpQueue = {}
        self.__expirationTimer = 0

    @staticmethod
    def _route_key(transportDomain, transportAddress):
        """Build the key identifying one transport route."""
        return tuple(transportDomain), tuple(transportAddress)

    @staticmethod
    def _to_bytes(value):
        if value is None:
            return None
        if isinstance(value, bytes):
            return value
        try:
            return bytes(value)
        except (TypeError, ValueError):
            return value

    @staticmethod
    def _context_key(transportDomain, transportAddress, contextName):
        """Build the key identifying one context on a transport route."""
        return (
            SnmpV3MessageProcessingModel._route_key(transportDomain, transportAddress),
            SnmpV3MessageProcessingModel._to_bytes(contextName) or b"",
        )

    def _schedule_engine_cache_expiry(self, cacheKey, snmpEngine=None):
        """Register expiry of one cache entry, cancelling a stale one first."""
        resolution = 1.0
        if snmpEngine is not None and snmpEngine.transport_dispatcher is not None:
            resolution = snmpEngine.transport_dispatcher.get_timer_resolution()
        expireAt = int(self.__expirationTimer + 300 / resolution)

        staleExpireAt = self.__engineIdCacheExpiry.pop(cacheKey, None)
        if staleExpireAt is not None:
            staleQueue = self.__engineIdCacheExpQueue.get(staleExpireAt)
            if staleQueue is not None:
                if cacheKey in staleQueue:
                    staleQueue.remove(cacheKey)
                if not staleQueue:
                    del self.__engineIdCacheExpQueue[staleExpireAt]

        self.__engineIdCacheExpiry[cacheKey] = expireAt
        if expireAt not in self.__engineIdCacheExpQueue:
            self.__engineIdCacheExpQueue[expireAt] = []
        self.__engineIdCacheExpQueue[expireAt].append(cacheKey)

    def _mark_route_contexts_pending(self, routeKey, dropContextEngineId=True):
        """Mark all contexts on one route as no longer verified.

        This is only used for route-wide facts.  Re-discovering a single
        context deliberately leaves the other contexts on the same route
        untouched.  A boot-generation change keeps the context mapping (the
        managed objects did not move) but drops its freshness, so
        *dropContextEngineId* is False in that case.
        """
        for (cachedRouteKey, _), contextData in self.__contextCache.items():
            if cachedRouteKey == routeKey:
                contextData["state"] = "pending"
                contextData["engineId"] = None
                contextData["engineBoots"] = None
                if dropContextEngineId:
                    contextData["contextEngineId"] = None

    def get_peer_engine_info(self, transportDomain, transportAddress, contextName=b""):
        """Return cached peer SNMP engine data.

        The lookup is scoped to *contextName*.  Two contexts sharing a
        transport endpoint are therefore interpreted independently: learning
        the contextEngineId of one context never changes the view of another.
        """
        routeKey = self._route_key(transportDomain, transportAddress)
        engineData = self.__engineIdCache.get(routeKey)
        contextData = self.__contextCache.get(
            self._context_key(transportDomain, transportAddress, contextName)
        )

        securityEngineId = engineData and engineData.get("securityEngineId")
        contextEngineId = None
        if contextData is not None and contextData.get("state") == "verified":
            contextEngineId = contextData.get("contextEngineId")
        if contextEngineId is None:
            contextEngineId = securityEngineId

        if securityEngineId is None:
            return None, None, None
        return securityEngineId, contextEngineId, contextName

    def get_peer_engine_details(
        self, transportDomain, transportAddress, contextName=b""
    ):
        """Return cached peer engine data including boot generation.

        Unlike :meth:`get_peer_engine_info` this returns the full cache entry,
        including the last observed ``snmpEngineBoots``/``snmpEngineTime`` and
        the point in time when the information was confirmed.  A missing entry
        means discovery is still pending.
        """
        routeKey = self._route_key(transportDomain, transportAddress)
        engineData = self.__engineIdCache.get(routeKey)
        if engineData is None:
            return None

        contextKey = self._context_key(transportDomain, transportAddress, contextName)
        contextData = self.__contextCache.get(contextKey)

        details = dict(engineData)
        details["routeKey"] = routeKey
        details["contextKey"] = contextKey
        details["contextKnown"] = contextData is not None
        details["contextName"] = contextData and contextData.get("contextName")
        details["contextEngineId"] = contextData and contextData.get("contextEngineId")
        contextState = contextData and contextData.get("state")
        details["contextState"] = contextState
        contextBoots = contextData and contextData.get("engineBoots")
        engineBoots = engineData.get("engineBoots")
        details["freshnessVerified"] = bool(
            engineData.get("freshnessVerified")
            and contextState == "verified"
            and (
                engineBoots is None
                or contextBoots is None
                or contextBoots == engineBoots
            )
        )
        return details

    def remember_peer_engine_info(
        self,
        transportDomain,
        transportAddress,
        securityEngineId,
        contextEngineId=None,
        contextName=b"",
        engineBoots=None,
        engineTime=None,
        source="report",
        verified=True,
        trust_context_engine_id=True,
        snmpEngine=None,
        securityModel=3,
        freshnessVerified=None,
    ):
        """Remember (or refresh) confirmed peer engine/context identity.

        ``securityEngineId`` is the authoritative engine that authenticates
        messages on this route.  ``contextEngineId``/``contextName`` identify
        the management context, which a proxy may map onto a different SNMP
        engine.  The two are stored separately so that re-discovering one
        context does not disturb the others.

        Returns a tuple ``(previous, changed)`` where *changed* tells whether
        the authoritative engine identity or its boot generation moved.
        """
        routeKey = self._route_key(transportDomain, transportAddress)
        contextKey = self._context_key(transportDomain, transportAddress, contextName)

        previous = self.__engineIdCache.get(routeKey)
        existingContext = self.__contextCache.get(contextKey)

        changed = False
        engineChanged = False
        if previous is not None:
            if previous.get("securityEngineId") != self._to_bytes(securityEngineId):
                changed = True
                engineChanged = True
            elif (
                engineBoots is not None
                and previous.get("engineBoots") is not None
                and engineBoots > previous.get("engineBoots")
            ):
                # A boot generation only moves forward.  An older value
                # (e.g. a replayed report) is not a new generation.
                changed = True

        sameEngine = previous is not None and previous.get(
            "securityEngineId"
        ) == self._to_bytes(securityEngineId)
        if sameEngine:
            previousBoots = previous.get("engineBoots")
            if engineBoots is None:
                # A report without timing evidence must not erase the boot
                # generation confirmed earlier for the same engine.
                engineBoots = previousBoots
                engineTime = previous.get("engineTime")
            elif previousBoots is not None and engineBoots < previousBoots:
                # A boot generation only moves forward.  An older value (e.g.
                # a replayed report) is not a new generation and must not
                # replace the trusted one.
                engineBoots = previousBoots
                engineTime = previous.get("engineTime")

        self.__engineIdCache[routeKey] = {
            "securityEngineId": self._to_bytes(securityEngineId),
            "engineBoots": engineBoots,
            "engineTime": engineTime,
            "confirmedAt": time.time() if verified else None,
            "state": "verified" if verified else "pending",
            # Only a confirmed response (or timing evidence learned from a
            # validated message) authorizes reusing boot/time values.  A bare
            # report may identify the engine but must not authorize requests
            # unless the caller says the report did not revoke freshness.
            "freshnessVerified": (
                bool(verified and source == "response")
                if freshnessVerified is None
                else bool(freshnessVerified)
            ),
            "source": source,
        }
        self._schedule_engine_cache_expiry(("engine", routeKey), snmpEngine)

        if changed and previous is not None:
            # The authoritative engine or its boot generation moved.  No
            # context on this route may keep using the old freshness evidence.
            # If only the boot generation moved, the context mapping itself
            # is still valid and is preserved.
            self._mark_route_contexts_pending(
                routeKey, dropContextEngineId=engineChanged
            )
            existingContext = self.__contextCache.get(contextKey)
            # The authoritative engine itself moved: the old engine's
            # timeline is irrelevant.  A forward boot-generation change is
            # different -- USM has just validated the new values and they are
            # the current evidence, so the timeline must be kept.
            if engineChanged and snmpEngine is not None:
                smHandler = snmpEngine.security_models.get(securityModel)
                if smHandler is not None and hasattr(smHandler, "invalidate_timeline"):
                    smHandler.invalidate_timeline(
                        self._to_bytes(previous.get("securityEngineId"))
                    )

        if trust_context_engine_id:
            storedContextEngineId = contextEngineId
            contextState = "verified" if verified else "pending"
            confirmedAt = time.time() if verified else None
        elif existingContext is not None and not engineChanged:
            # A report (e.g. notInTimeWindows) does not re-confirm the context
            # identity.  Keep what an earlier response verified rather than
            # letting a report erase it, and leave other contexts alone.  A
            # boot-only change keeps the mapping too: the managed objects did
            # not move, only freshness lapsed.
            storedContextEngineId = existingContext.get("contextEngineId")
            contextState = existingContext.get("state", "pending")
            confirmedAt = existingContext.get("confirmedAt")
        else:
            # An engine discovery report echoes the request's contextEngineId,
            # which is not evidence of the managed object's context engine.
            storedContextEngineId = None
            contextState = "pending"
            confirmedAt = None

        self.__contextCache[contextKey] = {
            "contextEngineId": self._to_bytes(storedContextEngineId),
            "contextName": self._to_bytes(contextName) or b"",
            "engineId": self._to_bytes(securityEngineId),
            "engineBoots": engineBoots,
            "confirmedAt": confirmedAt,
            "state": contextState,
            "source": source,
        }
        self._schedule_engine_cache_expiry(("context", contextKey), snmpEngine)

        debug.logger & debug.FLAG_MP and debug.logger(
            "rememberPeerEngineInfo: engine {} context {} for {} {} "
            "context {!r} (changed {})".format(
                self.__engineIdCache[routeKey],
                self.__contextCache[contextKey],
                transportDomain,
                transportAddress,
                contextName,
                changed,
            )
        )

        return previous, changed

    def forget_peer_engine_info(
        self, transportDomain, transportAddress, contextName=None
    ):
        """Drop peer discovery evidence.

        With *contextName* given only that context's evidence is removed,
        leaving other contexts on the same route intact.  Without it the whole
        route (engine and all contexts) is forgotten.
        """
        routeKey = self._route_key(transportDomain, transportAddress)
        if contextName is None:
            self.__engineIdCache.pop(routeKey, None)
            for key in list(self.__contextCache):
                if key[0] == routeKey:
                    del self.__contextCache[key]
            return

        contextKey = self._context_key(transportDomain, transportAddress, contextName)
        self.__contextCache.pop(contextKey, None)

    def invalidate_peer_discovery(
        self,
        transportDomain,
        transportAddress,
        contextName=None,
        snmpEngine=None,
        securityModel=3,
        invalidate_timeline=True,
    ):
        """Revoke freshness evidence without discarding verified observations.

        The route keeps the engine ID it discovered, so the next request can
        be addressed to it, but the boot/time authorization is withdrawn.

        With *invalidate_timeline* (an authoritative engine identity change),
        every context on the route loses freshness and the USM timeline of
        the old engine is dropped.  Without it (a boot/time report) only the
        named context is re-opened; other contexts sharing the endpoint keep
        their verified state.  In both cases the ``contextEngineId`` mappings
        are preserved, because the managed objects did not move.
        """
        routeKey = self._route_key(transportDomain, transportAddress)

        if invalidate_timeline:
            # The authoritative engine identity is in question.  Withdraw
            # route-wide freshness, keep every context mapping (the managed
            # objects did not move) and force the next request through
            # discovery.
            engineData = self.__engineIdCache.get(routeKey)
            if engineData is not None:
                engineData["freshnessVerified"] = False
                engineData["state"] = "pending"
                if snmpEngine is not None:
                    smHandler = snmpEngine.security_models.get(securityModel)
                    if smHandler is not None and hasattr(
                        smHandler, "invalidate_timeline"
                    ):
                        smHandler.invalidate_timeline(
                            engineData.get("securityEngineId")
                        )
            self._mark_route_contexts_pending(routeKey, dropContextEngineId=False)
            return

        if contextName is None:
            return

        # A boot/time report concerns one context only.  Other contexts on
        # the same endpoint keep their verified state and observations.
        contextKey = self._context_key(transportDomain, transportAddress, contextName)
        contextData = self.__contextCache.get(contextKey)
        if contextData is not None and contextData.get("state") == "verified":
            contextData["state"] = "pending"

    # 7.1.1a
    def prepare_outgoing_message(
        self,
        snmpEngine: SnmpEngine,
        transportDomain,
        transportAddress,
        messageProcessingModel,
        securityModel,
        securityName,
        securityLevel,
        contextEngineId,
        contextName,
        pduVersion,
        pdu,
        expectResponse,
        sendPduHandle,
    ):
        """Prepare SNMP message for dispatch."""
        (snmpEngineID,) = snmpEngine.get_mib_builder().import_symbols(
            "__SNMP-FRAMEWORK-MIB", "snmpEngineID"
        )
        snmpEngineID = snmpEngineID.syntax

        # 7.1.1b
        msgID = self._cache.new_message_id()

        debug.logger & debug.FLAG_MP and debug.logger(
            "prepareOutgoingMessage: new msgID %s" % msgID
        )

        routeKey = self._route_key(transportDomain, transportAddress)
        peerSnmpEngineData = self.__engineIdCache.get(routeKey)
        peerContextData = self.__contextCache.get(
            self._context_key(transportDomain, transportAddress, contextName)
        )
        debug.logger & debug.FLAG_MP and debug.logger(
            "prepareOutgoingMessage: peer SNMP engine data {} context data {} "
            "for transport {}, address {}".format(
                peerSnmpEngineData,
                peerContextData,
                transportDomain,
                transportAddress,
            )
        )

        # 7.1.4
        # The context layer and the authentication engine layer are kept
        # apart.  A proxy may authenticate on behalf of one engine while the
        # managed objects live in a context owned by another engine.
        if contextEngineId is None:
            if (
                peerContextData is not None
                and peerContextData.get("state") == "verified"
                and peerContextData.get("contextEngineId")
            ):
                # Only a context confirmed by discovery may steer the
                # outgoing contextEngineId.  An unverified or lapsed mapping
                # is kept for audit, but it must not be used to address a new
                # request.
                contextEngineId = peerContextData["contextEngineId"]
            elif peerSnmpEngineData is not None:
                contextEngineId = peerSnmpEngineData["securityEngineId"]
            else:
                contextEngineId = snmpEngineID
        # 7.1.5
        if not contextName:
            contextName = self._empty_string

        debug.logger & debug.FLAG_MP and debug.logger(
            f"prepareOutgoingMessage: using contextEngineId {contextEngineId!r}, contextName {contextName!r}"
        )

        # 7.1.6
        scopedPDU = self.__scopedPDU
        scopedPDU.setComponentByPosition(0, contextEngineId)
        scopedPDU.setComponentByPosition(1, contextName)
        scopedPDU.setComponentByPosition(2)
        scopedPDU.getComponentByPosition(2).setComponentByType(
            pdu.tagSet,
            pdu,
            verifyConstraints=False,
            matchTags=False,
            matchConstraints=False,
        )

        # 7.1.7
        msg = self._snmpMsgSpec

        # 7.1.7a
        msg.setComponentByPosition(
            0,
            self.MESSAGE_PROCESSING_MODEL_ID,
            verifyConstraints=False,
            matchTags=False,
            matchConstraints=False,
        )
        headerData = msg.setComponentByPosition(1).getComponentByPosition(1)

        # 7.1.7b
        headerData.setComponentByPosition(
            0, msgID, verifyConstraints=False, matchTags=False, matchConstraints=False
        )

        (snmpEngineMaxMessageSize,) = snmpEngine.get_mib_builder().import_symbols(
            "__SNMP-FRAMEWORK-MIB", "snmpEngineMaxMessageSize"
        )

        # 7.1.7c
        # XXX need to coerce MIB value as it has incompatible constraints set
        headerData.setComponentByPosition(
            1,
            snmpEngineMaxMessageSize.syntax,
            verifyConstraints=False,
            matchTags=False,
            matchConstraints=False,
        )

        # 7.1.7d
        msgFlags = 0
        if securityLevel == 1:
            pass
        elif securityLevel == 2:
            msgFlags |= 0x01
        elif securityLevel == 3:
            msgFlags |= 0x03
        else:
            raise error.ProtocolError("Unknown securityLevel %s" % securityLevel)

        if pdu.tagSet in rfc3411.CONFIRMED_CLASS_PDUS:
            msgFlags |= 0x04

        headerData.setComponentByPosition(
            2,
            self._message_flags[msgFlags],
            verifyConstraints=False,
            matchTags=False,
            matchConstraints=False,
        )

        # 7.1.7e
        # XXX need to coerce MIB value as it has incompatible constraints set
        headerData.setComponentByPosition(3, int(securityModel))

        debug.logger & debug.FLAG_MP and debug.logger(
            f"prepareOutgoingMessage: {msg.prettyPrint()}"
        )

        if securityModel in snmpEngine.security_models:
            smHandler = snmpEngine.security_models[securityModel]
        else:
            raise error.StatusInformation(
                errorIndication=errind.unsupportedSecurityModel
            )

        # 7.1.9.a
        if pdu.tagSet in rfc3411.UNCONFIRMED_CLASS_PDUS:
            securityEngineId = snmpEngineID

        else:
            if peerSnmpEngineData is None:
                debug.logger & debug.FLAG_MP and debug.logger(
                    "prepareOutgoingMessage: peer SNMP engine is not known"
                )

                securityEngineId = None

            else:
                securityEngineId = peerSnmpEngineData["securityEngineId"]

        debug.logger & debug.FLAG_MP and debug.logger(
            "prepareOutgoingMessage: securityModel {!r}, securityEngineId {!r}, securityName {!r}, securityLevel {!r}".format(
                securityModel, securityEngineId, securityName, securityLevel
            )
        )

        # 7.1.9.b
        (securityParameters, wholeMsg) = smHandler.generate_request_message(
            snmpEngine,
            self.MESSAGE_PROCESSING_MODEL_ID,
            msg,
            snmpEngineMaxMessageSize.syntax,
            securityModel,
            securityEngineId,
            securityName,
            securityLevel,
            scopedPDU,
        )

        # Message size constraint verification
        if len(wholeMsg) > snmpEngineMaxMessageSize.syntax:
            raise error.StatusInformation(errorIndication=errind.tooBig)

        # 7.1.9.c
        if pdu.tagSet in rfc3411.CONFIRMED_CLASS_PDUS:
            # XXX rfc bug? why stateReference should be created?
            self._cache.push_by_message_id(
                msgID,
                sendPduHandle=sendPduHandle,
                msgID=msgID,
                snmpEngineID=snmpEngineID,
                securityModel=securityModel,
                securityName=securityName,
                securityLevel=securityLevel,
                contextEngineId=contextEngineId,
                contextName=contextName,
                transportDomain=transportDomain,
                transportAddress=transportAddress,
            )

        snmpEngine.observer.store_execution_context(
            snmpEngine,
            "rfc3412.prepareOutgoingMessage",
            dict(
                transportDomain=transportDomain,
                transportAddress=transportAddress,
                wholeMsg=wholeMsg,
                securityModel=securityModel,
                securityName=securityName,
                securityLevel=securityLevel,
                contextEngineId=contextEngineId,
                contextName=contextName,
                securityEngineId=securityEngineId,
                engineBoots=peerSnmpEngineData
                and peerSnmpEngineData.get("engineBoots"),
                engineTime=peerSnmpEngineData and peerSnmpEngineData.get("engineTime"),
                contextState=peerContextData
                and peerContextData.get("state")
                or "pending",
                freshnessVerified=bool(
                    peerSnmpEngineData and peerSnmpEngineData.get("freshnessVerified")
                ),
                pdu=pdu,
            ),
        )
        snmpEngine.observer.clear_execution_context(
            snmpEngine, "rfc3412.prepareOutgoingMessage"
        )

        return transportDomain, transportAddress, wholeMsg

    def prepare_response_message(
        self,
        snmpEngine: SnmpEngine,
        messageProcessingModel,
        securityModel,
        securityName,
        securityLevel,
        contextEngineId,
        contextName,
        pduVersion,
        pdu,
        maxSizeResponseScopedPDU,
        stateReference,
        statusInformation,
    ):
        """Prepare SNMP message for response."""
        (snmpEngineID,) = snmpEngine.get_mib_builder().import_symbols(  # type: ignore
            "__SNMP-FRAMEWORK-MIB", "snmpEngineID"
        )
        snmpEngineID = snmpEngineID.syntax

        # 7.1.2.b
        cachedParams = self._cache.pop_by_state_reference(stateReference)
        msgID = cachedParams["msgID"]
        contextEngineId = cachedParams["contextEngineId"]
        contextName = cachedParams["contextName"]
        securityModel = cachedParams["securityModel"]
        securityName = cachedParams["securityName"]
        securityLevel = cachedParams["securityLevel"]
        securityStateReference = cachedParams["securityStateReference"]
        reportableFlag = cachedParams["reportableFlag"]
        maxMessageSize = cachedParams["msgMaxSize"]
        transportDomain = cachedParams["transportDomain"]
        transportAddress = cachedParams["transportAddress"]

        debug.logger & debug.FLAG_MP and debug.logger(
            "prepareResponseMessage: stateReference %s" % stateReference
        )

        # 7.1.3
        if statusInformation is not None and "oid" in statusInformation:
            # 7.1.3a
            if pdu is None:
                pduType = None
            else:
                requestID = pdu.getComponentByPosition(0)
                pduType = pdu.tagSet

            # 7.1.3b
            if (
                pdu is None
                and not reportableFlag
                or pduType is not None
                and pduType not in rfc3411.CONFIRMED_CLASS_PDUS
            ):
                if securityModel in snmpEngine.security_models:
                    smHandler = snmpEngine.security_models[securityModel]
                    smHandler.release_state_information(securityStateReference)
                raise error.StatusInformation(errorIndication=errind.loopTerminated)

            # 7.1.3c
            reportPDU = rfc1905.ReportPDU()
            pMod.apiPDU.set_varbinds(
                reportPDU, ((statusInformation["oid"], statusInformation["val"]),)
            )
            pMod.apiPDU.set_error_status(reportPDU, 0)
            pMod.apiPDU.set_error_index(reportPDU, 0)
            if pdu is None:
                pMod.apiPDU.set_request_id(reportPDU, 0)
            else:
                # noinspection PyUnboundLocalVariable
                pMod.apiPDU.set_request_id(reportPDU, requestID)

            # 7.1.3d.1
            if "securityLevel" in statusInformation:
                securityLevel = statusInformation["securityLevel"]
            else:
                securityLevel = 1

            # 7.1.3d.2
            if "contextEngineId" in statusInformation:
                contextEngineId = statusInformation["contextEngineId"]
            else:
                contextEngineId = snmpEngineID

            # 7.1.3d.3
            if "contextName" in statusInformation:
                contextName = statusInformation["contextName"]
            else:
                contextName = ""

            # 7.1.3e
            pdu = reportPDU

            debug.logger & debug.FLAG_MP and debug.logger(
                "prepareResponseMessage: prepare report PDU for statusInformation %s"
                % statusInformation
            )
        # 7.1.4
        if not contextEngineId:
            contextEngineId = snmpEngineID  # XXX impl-dep manner

        # 7.1.5
        if not contextName:
            contextName = self._empty_string

        debug.logger & debug.FLAG_MP and debug.logger(
            f"prepareResponseMessage: using contextEngineId {contextEngineId!r}, contextName {contextName!r}"
        )

        # 7.1.6
        scopedPDU = self.__scopedPDU
        scopedPDU.setComponentByPosition(0, contextEngineId)
        scopedPDU.setComponentByPosition(1, contextName)
        scopedPDU.setComponentByPosition(2)
        scopedPDU.getComponentByPosition(2).setComponentByType(
            pdu.tagSet,
            pdu,
            verifyConstraints=False,
            matchTags=False,
            matchConstraints=False,
        )

        # 7.1.7
        msg = self._snmpMsgSpec

        # 7.1.7a
        msg.setComponentByPosition(
            0,
            self.MESSAGE_PROCESSING_MODEL_ID,
            verifyConstraints=False,
            matchTags=False,
            matchConstraints=False,
        )

        headerData = msg.setComponentByPosition(1).getComponentByPosition(1)

        # 7.1.7b
        headerData.setComponentByPosition(
            0, msgID, verifyConstraints=False, matchTags=False, matchConstraints=False
        )

        (snmpEngineMaxMessageSize,) = snmpEngine.get_mib_builder().import_symbols(
            "__SNMP-FRAMEWORK-MIB", "snmpEngineMaxMessageSize"
        )

        # 7.1.7c
        # XXX need to coerce MIB value as it has incompatible constraints set
        headerData.setComponentByPosition(
            1,
            snmpEngineMaxMessageSize.syntax,
            verifyConstraints=False,
            matchTags=False,
            matchConstraints=False,
        )

        # 7.1.7d
        msgFlags = 0
        if securityLevel == 1:
            pass
        elif securityLevel == 2:
            msgFlags |= 0x01
        elif securityLevel == 3:
            msgFlags |= 0x03
        else:
            raise error.ProtocolError("Unknown securityLevel %s" % securityLevel)

        if pdu.tagSet in rfc3411.CONFIRMED_CLASS_PDUS:  # XXX not needed?
            msgFlags |= 0x04

        headerData.setComponentByPosition(
            2,
            self._message_flags[msgFlags],
            verifyConstraints=False,
            matchTags=False,
            matchConstraints=False,
        )

        # 7.1.7e
        headerData.setComponentByPosition(
            3,
            securityModel,
            verifyConstraints=False,
            matchTags=False,
            matchConstraints=False,
        )

        debug.logger & debug.FLAG_MP and debug.logger(
            f"prepareResponseMessage: {msg.prettyPrint()}"
        )

        if securityModel in snmpEngine.security_models:
            smHandler = snmpEngine.security_models[securityModel]
        else:
            raise error.StatusInformation(
                errorIndication=errind.unsupportedSecurityModel
            )

        debug.logger & debug.FLAG_MP and debug.logger(
            "prepareResponseMessage: securityModel {!r}, securityEngineId {!r}, securityName {!r}, securityLevel {!r}".format(
                securityModel, snmpEngineID, securityName, securityLevel
            )
        )

        # 7.1.8a
        try:
            (securityParameters, wholeMsg) = smHandler.generate_response_message(
                snmpEngine,
                self.MESSAGE_PROCESSING_MODEL_ID,
                msg,
                snmpEngineMaxMessageSize.syntax,
                securityModel,
                snmpEngineID,
                securityName,
                securityLevel,
                scopedPDU,
                securityStateReference,
                statusInformation.get(
                    "errorIndication"
                ),  # Use get() to handle missing errorIndication
            )  # type: ignore
        except error.StatusInformation:
            # 7.1.8.b
            raise

        debug.logger & debug.FLAG_MP and debug.logger(
            "prepareResponseMessage: SM finished"
        )

        # Message size constraint verification
        if len(wholeMsg) > min(snmpEngineMaxMessageSize.syntax, maxMessageSize):
            raise error.StatusInformation(errorIndication=errind.tooBig)

        snmpEngine.observer.store_execution_context(
            snmpEngine,
            "rfc3412.prepareResponseMessage",
            dict(
                transportDomain=transportDomain,
                transportAddress=transportAddress,
                securityModel=securityModel,
                securityName=securityName,
                securityLevel=securityLevel,
                contextEngineId=contextEngineId,
                contextName=contextName,
                securityEngineId=snmpEngineID,
                pdu=pdu,
            ),
        )
        snmpEngine.observer.clear_execution_context(
            snmpEngine, "rfc3412.prepareResponseMessage"
        )

        return transportDomain, transportAddress, wholeMsg

    @staticmethod
    def _report_revokes_freshness(reportPdu):
        """Return True for reports that say the timeline moved.

        ``usmStatsUnknownEngineIDs`` and ``usmStatsNotInTimeWindows`` are the
        two reports that invalidate cached boot/time evidence.  Other reports
        (unknown user, wrong digest, decryption error, unsupported security
        level) identify credential problems and leave freshness alone.
        """
        varBinds = pMod.apiPDU.get_varbinds(reportPdu)
        if not varBinds:
            return False
        return tuple(varBinds[0][0]) in (
            (1, 3, 6, 1, 6, 3, 15, 1, 1, 2, 0),  # usmStatsNotInTimeWindows
            (1, 3, 6, 1, 6, 3, 15, 1, 1, 4, 0),  # usmStatsUnknownEngineIDs
        )

    @staticmethod
    def _trusted_engine_timing(snmpEngine, securityEngineId):
        """Return boot/time evidence validated by the USM timeline.

        Report PDUs carry USM statistics counters, not a boot generation.
        The authoritative boot/time values are those the security model has
        validated while processing authenticated messages, so read them from
        the USM timeline rather than from the report varbinds.
        """
        if snmpEngine is None or securityEngineId is None:
            return None, None
        securityModel = snmpEngine.security_models.get(3)
        if securityModel is None or not hasattr(securityModel, "get_timeline_info"):
            return None, None
        timeline = securityModel.get_timeline_info(securityEngineId)
        if not timeline:
            return None, None
        return timeline["engineBoots"], timeline["engineTime"]

    def _increase_engine_generation(self, routeKey, engineBoots, engineTime):
        """Advance the recorded boot generation only, never regress it."""
        entry = self.__engineIdCache.get(routeKey)
        if entry is None or engineBoots is None:
            return
        recordedBoots = entry.get("engineBoots")
        if recordedBoots is None or engineBoots > recordedBoots:
            entry["engineBoots"] = engineBoots
            entry["engineTime"] = engineTime

    # 7.2.1

    def prepare_data_elements(
        self, snmpEngine: SnmpEngine, transportDomain, transportAddress, wholeMsg
    ):
        """Prepare SNMP message data elements."""
        # 7.2.2
        msg, restOfwholeMsg = decoder.decode(wholeMsg, asn1Spec=self._snmpMsgSpec)

        debug.logger & debug.FLAG_MP and debug.logger(
            f"prepareDataElements: {msg.prettyPrint()}"
        )

        if eoo.endOfOctets.isSameTypeWith(msg):
            raise error.StatusInformation(errorIndication=errind.parseError)

        # 7.2.3
        headerData = msg.getComponentByPosition(1)
        msgVersion = messageProcessingModel = msg.getComponentByPosition(0)
        msgID = headerData.getComponentByPosition(0)
        (msgFlags,) = headerData.getComponentByPosition(2).asNumbers()
        maxMessageSize = headerData.getComponentByPosition(1)
        securityModel = headerData.getComponentByPosition(3)
        securityParameters = msg.getComponentByPosition(2)

        debug.logger & debug.FLAG_MP and debug.logger(
            "prepareDataElements: msg data msgVersion {} msgID {} securityModel {}".format(
                msgVersion, msgID, securityModel
            )
        )

        # 7.2.4
        if securityModel not in snmpEngine.security_models:
            (snmpUnknownSecurityModels,) = snmpEngine.get_mib_builder().import_symbols(
                "__SNMP-MPD-MIB", "snmpUnknownSecurityModels"
            )
            snmpUnknownSecurityModels.syntax += 1
            raise error.StatusInformation(
                errorIndication=errind.unsupportedSecurityModel
            )

        # 7.2.5
        if msgFlags & 0x03 == 0x00:
            securityLevel = 1  # noAuthNoPriv
        elif (msgFlags & 0x03) == 0x01:
            securityLevel = 2  # authNoPriv
        elif (msgFlags & 0x03) == 0x03:
            securityLevel = 3  # authPriv
        else:
            (snmpInvalidMsgs,) = snmpEngine.get_mib_builder().import_symbols(
                "__SNMP-MPD-MIB", "snmpInvalidMsgs"
            )
            snmpInvalidMsgs.syntax += 1
            raise error.StatusInformation(errorIndication=errind.invalidMsg)

        if msgFlags & 0x04:
            reportableFlag = 1
        else:
            reportableFlag = 0

        # 7.2.6
        smHandler: AbstractSecurityModel = snmpEngine.security_models[securityModel]
        try:
            (
                securityEngineId,
                securityName,
                scopedPDU,
                maxSizeResponseScopedPDU,
                securityStateReference,
            ) = smHandler.process_incoming_message(
                snmpEngine,
                messageProcessingModel,
                maxMessageSize,
                securityParameters,
                securityModel,
                securityLevel,
                wholeMsg,
                msg,
            )  # type: ignore
            debug.logger & debug.FLAG_MP and debug.logger(
                "prepareDataElements: SM succeeded"
            )

        except error.StatusInformation:
            statusInformation, origTraceback = sys.exc_info()[1:3]

            debug.logger & debug.FLAG_MP and debug.logger(
                "prepareDataElements: SM failed, statusInformation %s"
                % statusInformation
            )

            snmpEngine.observer.store_execution_context(
                snmpEngine,
                "rfc3412.prepareDataElements:sm-failure",
                dict(
                    transportDomain=transportDomain,
                    transportAddress=transportAddress,
                    securityModel=securityModel,
                    securityLevel=securityLevel,
                    securityParameters=securityParameters,
                    statusInformation=statusInformation,
                ),
            )
            snmpEngine.observer.clear_execution_context(
                snmpEngine, "rfc3412.prepareDataElements:sm-failure"
            )

            if "errorIndication" in statusInformation:
                # 7.2.6a
                if "oid" in statusInformation:
                    # 7.2.6a1
                    securityStateReference = statusInformation["securityStateReference"]
                    contextEngineId = statusInformation["contextEngineId"]
                    contextName = statusInformation["contextName"]
                    if "scopedPDU" in statusInformation:
                        scopedPDU = statusInformation["scopedPDU"]
                        pdu = scopedPDU.getComponentByPosition(2).getComponent()
                    else:
                        pdu = None
                    maxSizeResponseScopedPDU = statusInformation[
                        "maxSizeResponseScopedPDU"
                    ]
                    securityName = None  # XXX secmod cache used

                    # 7.2.6a2
                    stateReference = self._cache.new_state_reference()
                    self._cache.push_by_state_reference(
                        stateReference,
                        msgVersion=messageProcessingModel,
                        msgID=msgID,
                        contextEngineId=contextEngineId,
                        contextName=contextName,
                        securityModel=securityModel,
                        securityName=securityName,
                        securityLevel=securityLevel,
                        securityStateReference=securityStateReference,
                        reportableFlag=reportableFlag,
                        msgMaxSize=maxMessageSize,
                        maxSizeResponseScopedPDU=maxSizeResponseScopedPDU,
                        transportDomain=transportDomain,
                        transportAddress=transportAddress,
                    )

                    # 7.2.6a3
                    try:
                        snmpEngine.message_dispatcher.return_response_pdu(
                            snmpEngine,
                            3,
                            securityModel,
                            securityName,
                            securityLevel,
                            contextEngineId,
                            contextName,
                            1,
                            pdu,
                            maxSizeResponseScopedPDU,
                            stateReference,
                            statusInformation,
                        )
                    except error.StatusInformation:
                        pass

                    debug.logger & debug.FLAG_MP and debug.logger(
                        "prepareDataElements: error reported"
                    )

            # 7.2.6b
            try:
                raise statusInformation.with_traceback(origTraceback)
            finally:
                # Break cycle between locals and traceback object
                # (seems to be irrelevant on Py3 but just in case)
                del origTraceback
        else:
            # Sniff for engine/context identity in engine-to-engine messages.
            contextEngineId = scopedPDU[0]
            contextName = scopedPDU[1]
            pdus = scopedPDU[2]
            pdu = pdus.getComponent()
            if pdu.tagSet in rfc3411.INTERNAL_CLASS_PDUS:
                # A Report is received on this route.  The scopedPDU may echo
                # the contextEngineId of our request, so only the
                # authoritative (security) engine is trusted from here.  The
                # contextEngineId is confirmed by a response PDU, not a
                # Report.
                routeKey = self._route_key(transportDomain, transportAddress)
                reportEngineBoots, reportEngineTime = self._trusted_engine_timing(
                    snmpEngine, securityEngineId
                )
                previousEngineData = self.__engineIdCache.get(routeKey)
                reportRevokesFreshness = self._report_revokes_freshness(pdu)
                self.remember_peer_engine_info(
                    transportDomain,
                    transportAddress,
                    securityEngineId,
                    contextEngineId,
                    contextName,
                    engineBoots=reportEngineBoots,
                    engineTime=reportEngineTime,
                    source="report",
                    verified=True,
                    trust_context_engine_id=False,
                    snmpEngine=snmpEngine,
                    securityModel=securityModel,
                    freshnessVerified=bool(
                        previousEngineData
                        and previousEngineData.get("freshnessVerified")
                        and not reportRevokesFreshness
                    ),
                )
                debug.logger & debug.FLAG_MP and debug.logger(
                    "prepareDataElements: cache securityEngineId {!r} for {!r} {!r} "
                    "context {!r} (contextEngineId trust withheld)".format(
                        securityEngineId,
                        transportDomain,
                        transportAddress,
                        contextName,
                    )
                )
                if reportEngineBoots is not None:
                    self._increase_engine_generation(
                        routeKey, reportEngineBoots, reportEngineTime
                    )

        (snmpEngineID,) = snmpEngine.get_mib_builder().import_symbols(
            "__SNMP-FRAMEWORK-MIB", "snmpEngineID"
        )
        snmpEngineID = snmpEngineID.syntax

        # 7.2.7 XXX PDU would be parsed here?
        contextEngineId = scopedPDU[0]
        contextName = scopedPDU[1]
        pdu = scopedPDU[2]

        pdu = pdu.getComponent()  # PDUs

        # 7.2.8
        pduVersion = api.SNMP_VERSION_2C

        # 7.2.9
        pduType = pdu.tagSet

        # 7.2.10
        if (
            pduType in rfc3411.RESPONSE_CLASS_PDUS
            or pduType in rfc3411.INTERNAL_CLASS_PDUS
        ):
            # 7.2.10a
            try:
                cachedReqParams = self._cache.pop_by_message_id(msgID)
            except error.ProtocolError:
                smHandler.release_state_information(securityStateReference)
                raise error.StatusInformation(errorIndication=errind.dataMismatch)
            # 7.2.10b
            sendPduHandle = cachedReqParams["sendPduHandle"]
        else:
            sendPduHandle = None

        debug.logger & debug.FLAG_MP and debug.logger(
            f"prepareDataElements: using sendPduHandle {sendPduHandle} for msgID {msgID}"
        )

        # 7.2.11
        if pduType in rfc3411.INTERNAL_CLASS_PDUS:
            # 7.2.11a
            varBinds = pMod.apiPDU.get_varbinds(pdu)
            if varBinds:
                statusInformation = error.StatusInformation(
                    errorIndication=_snmpErrors.get(
                        varBinds[0][0],
                        errind.ReportPduReceived(varBinds[0][0].prettyPrint()),
                    ),
                    oid=varBinds[0][0],
                    val=varBinds[0][1],
                    sendPduHandle=sendPduHandle,
                )
            else:
                # A Report-PDU must carry exactly one varbind naming the error
                # (RFC 3412 §7.2.11a). Honour the malformed case rather than
                # raising a StatusInformation that has no errorIndication --
                # every consumer below assumes one is present. #241
                statusInformation = error.StatusInformation(
                    errorIndication=errind.ReportPduReceived(""),
                    sendPduHandle=sendPduHandle,
                )

            # 7.2.11b (incomplete implementation)

            snmpEngine.observer.store_execution_context(
                snmpEngine,
                "rfc3412.prepareDataElements:internal",
                dict(
                    transportDomain=transportDomain,
                    transportAddress=transportAddress,
                    securityModel=securityModel,
                    securityName=securityName,
                    securityLevel=securityLevel,
                    contextEngineId=contextEngineId,
                    contextName=contextName,
                    securityEngineId=securityEngineId,
                    pdu=pdu,
                ),
            )
            snmpEngine.observer.clear_execution_context(
                snmpEngine, "rfc3412.prepareDataElements:internal"
            )

            # 7.2.11c
            smHandler.release_state_information(securityStateReference)

            # 7.2.11d
            # no-op

            # 7.2.11e XXX may need to pass Reports up to app in some cases...
            raise statusInformation

        statusInformation = None  # no errors ahead

        # 7.2.12
        if pduType in rfc3411.RESPONSE_CLASS_PDUS:
            # 7.2.12a -> no-op

            # 7.2.12b
            # noinspection PyUnboundLocalVariable
            if (
                securityModel != cachedReqParams["securityModel"]
                or securityName != cachedReqParams["securityName"]
                or securityLevel != cachedReqParams["securityLevel"]
                or contextEngineId != cachedReqParams["contextEngineId"]
                or contextName != cachedReqParams["contextName"]
            ):
                smHandler.release_state_information(securityStateReference)
                raise error.StatusInformation(errorIndication=errind.dataMismatch)

            # A response is the authoritative confirmation of the context
            # behind the route.  Record it per context: the security engine
            # authenticating this route and the engine owning the managed
            # objects may legitimately differ when a proxy is in between.
            responseBoots, responseTime = self._trusted_engine_timing(
                snmpEngine, securityEngineId
            )
            self.remember_peer_engine_info(
                transportDomain,
                transportAddress,
                securityEngineId,
                contextEngineId,
                contextName,
                engineBoots=responseBoots,
                engineTime=responseTime,
                source="response",
                verified=True,
                trust_context_engine_id=True,
                snmpEngine=snmpEngine,
                securityModel=securityModel,
            )

            snmpEngine.observer.store_execution_context(
                snmpEngine,
                "rfc3412.prepareDataElements:response",
                dict(
                    transportDomain=transportDomain,
                    transportAddress=transportAddress,
                    securityModel=securityModel,
                    securityName=securityName,
                    securityLevel=securityLevel,
                    contextEngineId=contextEngineId,
                    contextName=contextName,
                    securityEngineId=securityEngineId,
                    pdu=pdu,
                ),
            )
            snmpEngine.observer.clear_execution_context(
                snmpEngine, "rfc3412.prepareDataElements:response"
            )

            # 7.2.12c
            smHandler.release_state_information(securityStateReference)
            stateReference = None

            # 7.2.12d
            return (
                messageProcessingModel,
                securityModel,
                securityName,
                securityLevel,
                contextEngineId,
                contextName,
                pduVersion,
                pdu,
                pduType,
                sendPduHandle,
                maxSizeResponseScopedPDU,
                statusInformation,
                stateReference,
            )

        # 7.2.13
        if pduType in rfc3411.CONFIRMED_CLASS_PDUS:
            # 7.2.13a
            if securityEngineId != snmpEngineID:
                smHandler.release_state_information(securityStateReference)
                raise error.StatusInformation(errorIndication=errind.engineIDMismatch)

            # 7.2.13b
            stateReference = self._cache.new_state_reference()
            self._cache.push_by_state_reference(
                stateReference,
                msgVersion=messageProcessingModel,
                msgID=msgID,
                contextEngineId=contextEngineId,
                contextName=contextName,
                securityModel=securityModel,
                securityName=securityName,
                securityLevel=securityLevel,
                securityStateReference=securityStateReference,
                reportableFlag=reportableFlag,
                msgMaxSize=maxMessageSize,
                maxSizeResponseScopedPDU=maxSizeResponseScopedPDU,
                transportDomain=transportDomain,
                transportAddress=transportAddress,
            )

            debug.logger & debug.FLAG_MP and debug.logger(
                "prepareDataElements: new stateReference %s" % stateReference
            )

            snmpEngine.observer.store_execution_context(
                snmpEngine,
                "rfc3412.prepareDataElements:confirmed",
                dict(
                    transportDomain=transportDomain,
                    transportAddress=transportAddress,
                    securityModel=securityModel,
                    securityName=securityName,
                    securityLevel=securityLevel,
                    contextEngineId=contextEngineId,
                    contextName=contextName,
                    securityEngineId=securityEngineId,
                    pdu=pdu,
                ),
            )
            snmpEngine.observer.clear_execution_context(
                snmpEngine, "rfc3412.prepareDataElements:confirmed"
            )

            # 7.2.13c
            return (
                messageProcessingModel,
                securityModel,
                securityName,
                securityLevel,
                contextEngineId,
                contextName,
                pduVersion,
                pdu,
                pduType,
                sendPduHandle,
                maxSizeResponseScopedPDU,
                statusInformation,
                stateReference,
            )

        # 7.2.14
        if pduType in rfc3411.UNCONFIRMED_CLASS_PDUS:
            # Pass new stateReference to let app browse request details
            stateReference = self._cache.new_state_reference()

            snmpEngine.observer.store_execution_context(
                snmpEngine,
                "rfc3412.prepareDataElements:unconfirmed",
                dict(
                    transportDomain=transportDomain,
                    transportAddress=transportAddress,
                    securityModel=securityModel,
                    securityName=securityName,
                    securityLevel=securityLevel,
                    contextEngineId=contextEngineId,
                    contextName=contextName,
                    securityEngineId=securityEngineId,
                    pdu=pdu,
                ),
            )
            snmpEngine.observer.clear_execution_context(
                snmpEngine, "rfc3412.prepareDataElements:unconfirmed"
            )

            # This is not specified explicitly in RFC
            smHandler.release_state_information(securityStateReference)

            return (
                messageProcessingModel,
                securityModel,
                securityName,
                securityLevel,
                contextEngineId,
                contextName,
                pduVersion,
                pdu,
                pduType,
                sendPduHandle,
                maxSizeResponseScopedPDU,
                statusInformation,
                stateReference,
            )

        smHandler.release_state_information(securityStateReference)
        raise error.StatusInformation(errorIndication=errind.unsupportedPDUtype)

    def __expire_engines_info(self):
        if self.__expirationTimer in self.__engineIdCacheExpQueue:
            for cacheKey in self.__engineIdCacheExpQueue[self.__expirationTimer]:
                # Skip stale registrations: the entry was refreshed since and
                # its expiry moved to a later tick.
                if self.__engineIdCacheExpiry.get(cacheKey) != self.__expirationTimer:
                    continue
                del self.__engineIdCacheExpiry[cacheKey]
                cacheKind, key = cacheKey
                if cacheKind == "engine":
                    self.__engineIdCache.pop(key, None)
                elif cacheKind == "context":
                    self.__contextCache.pop(key, None)
                debug.logger & debug.FLAG_MP and debug.logger(
                    f"__expireEnginesInfo: expiring {cacheKey!r}"
                )
            del self.__engineIdCacheExpQueue[self.__expirationTimer]
        self.__expirationTimer += 1

    def receive_timer_tick(self, snmpEngine: SnmpEngine, timeNow):
        """Process periodic timer tick."""
        self.__expire_engines_info()
        AbstractMessageProcessingModel.receive_timer_tick(self, snmpEngine, timeNow)
