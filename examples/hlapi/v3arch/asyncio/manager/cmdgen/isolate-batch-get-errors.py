"""
SNMPv2c batch GET with error isolation
++++++++++++++++++++++++++++++++++++++

Send one SNMP GET request for several OIDs. When the agent rejects a single
binding, only the bindings the response left unresolved are queried again.
The response's own error-index decides which binding is set aside, so the
readable OIDs are still collected.

Functionally similar to:

| $ snmpget -v2c -c public demo.pysnmp.com SNMPv2-MIB::sysDescr.0 \\
|       SNMPv2-MIB::sysUpTime.0 1.3.6.1.4.1.60069.9.3 SNMPv2-MIB::sysName.0

"""  #
import asyncio
from pysnmp.hlapi.v3arch.asyncio import *


async def run():
    snmpEngine = SnmpEngine()
    result = await get_cmd_isolated(
        snmpEngine,
        CommunityData("public"),
        await UdpTransportTarget.create(("demo.pysnmp.com", 161)),
        ContextData(),
        ObjectType(ObjectIdentity("SNMPv2-MIB", "sysDescr", 0)),
        ObjectType(ObjectIdentity("SNMPv2-MIB", "sysUpTime", 0)),
        ObjectType(ObjectIdentity("1.3.6.1.4.1.60069.9.3")),
        ObjectType(ObjectIdentity("SNMPv2-MIB", "sysName", 0)),
        requestSize=16,
        onPlan=lambda plan: print("next request:", plan.positions, plan.reason),
    )

    for outcome in result.confirmed:
        print("confirmed", outcome.position, outcome.oid, "=", outcome.value)

    for outcome in result.absent:
        print("absent   ", outcome.position, outcome.oid, "-", outcome.reason)

    for outcome in result.unconfirmed:
        print("unknown  ", outcome.position, outcome.oid, "-", outcome.reason)

    for outcome in result.conflicts:
        print("conflict ", outcome.position, outcome.oid, "-", outcome.reason)


asyncio.run(run())
