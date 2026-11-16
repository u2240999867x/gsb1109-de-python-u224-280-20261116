# Collection coverage report: acme-router-X1-firmware-4.2

Policy: illustrative-compliance-policy
Locked MIB fact source: compiled modules ASN1, ASN1-ENUMERATION, ASN1-REFINEMENT, SNMP-FRAMEWORK-MIB, SNMP-TARGET-MIB, SNMP-USER-BASED-SM-MIB, SNMPv2-CONF, SNMPv2-MIB, SNMPv2-SMI, SNMPv2-TC

## Summary

Objects in scope: 44 | satisfied: 21 | partial: 5 | not satisfiable: 1 | waived: 17
De-duplicated polling entries: 26

## Requirement classes

### Mandatory groups

Mandatory groups come from un-conditional GROUP clauses of the claimed MODULE-COMPLIANCE. Every member object must be pollable at at least read-only; a single unsupported member fails the group.

- `SNMP-FRAMEWORK-MIB::snmpEngineGroup` -> 4 objects; device supported=True
- `SNMPv2-MIB::systemGroup` -> 11 objects; device supported=True
- `SNMPv2-MIB::snmpGroup` -> 6 objects; device supported=True

### Conditional optional groups

Conditional groups only bind the device when their stated condition is active. The device declaration must answer each condition explicitly; an inactive condition is recorded as a traceable waiver, not as coverage.

- `SNMP-TARGET-MIB::snmpTargetResponseGroup` condition: device generates SNMP messages for which a response is expected (retries/timeouts apply); device condition_active=True, supported=True (notification originator sends Inform requests with retry/timeout configuration)
- `SNMP-TARGET-MIB::snmpTargetCommandResponderGroup` condition: device includes an SNMP command responder application; device condition_active=True, supported=False (command responder context counters are not instrumented in firmware 4.2)
- `SNMP-USER-BASED-SM-MIB::usmMIBBasicGroup` condition: device implements the SNMPv3 User-based Security Model (USM); device condition_active=False, supported=False (SNMPv2c-only product; USM is not implemented)

### Minimum access levels (MIN-ACCESS)

Each compliance requirement carries a minimum access level. The default for a read-only collection is read-only; explicit MIN-ACCESS clauses can raise it (e.g. read-write, read-create). The object's effective requirement is the highest minimum across all groups/clauses that require it. The locked MIB MAX-ACCESS, adjusted by device access refinements, must meet that minimum.

Access ordering: not-accessible < accessible-for-notify < read-only < read-write < read-create

- `SNMPv2-MIB::sysContact` effective minimum: read-write; device offers: read-write
- `SNMPv2-MIB::snmpEnableAuthenTraps` effective minimum: read-write; device offers: read-only
- `SNMP-TARGET-MIB::snmpTargetAddrTimeout` effective minimum: read-create; device offers: read-only

## Polling set (de-duplicated)

Scalars use GET on the `.0` instance. Table columns use a subtree walk (GETNEXT/GETBulk): a column OID is a schema node, so one GET against it returns noSuchInstance and can never prove the table is fully populated. Each entry lists every declaration that requires it, while the object is polled only once.

- [GET] `SNMPv2-MIB::sysDescr.0` (1.3.6.1.2.1.1.1.0) verdict=satisfied min-access=read-only device=read-only
  basis: policy-group:SNMPv2-MIB::systemGroup
- [GET] `SNMPv2-MIB::sysObjectID.0` (1.3.6.1.2.1.1.2.0) verdict=satisfied min-access=read-only device=read-only
  basis: policy-group:SNMPv2-MIB::systemGroup
- [GET] `SNMPv2-MIB::sysUpTime.0` (1.3.6.1.2.1.1.3.0) verdict=satisfied min-access=read-only device=read-only
  basis: delivery-metric, policy-group:SNMPv2-MIB::systemGroup
- [GET] `SNMPv2-MIB::sysContact.0` (1.3.6.1.2.1.1.4.0) verdict=satisfied min-access=read-write device=read-write
  basis: RFC3418 snmpBasicComplianceRev2 OBJECT clause (MIN-ACCESS read-write), delivery-metric, policy-group:SNMPv2-MIB::systemGroup
- [GET] `SNMPv2-MIB::sysName.0` (1.3.6.1.2.1.1.5.0) verdict=satisfied min-access=read-only device=read-write
  basis: delivery-metric, policy-group:SNMPv2-MIB::systemGroup
- [GET] `SNMPv2-MIB::sysLocation.0` (1.3.6.1.2.1.1.6.0) verdict=satisfied min-access=read-only device=read-write
  basis: policy-group:SNMPv2-MIB::systemGroup
- [GET] `SNMPv2-MIB::sysServices.0` (1.3.6.1.2.1.1.7.0) verdict=satisfied min-access=read-only device=read-only
  basis: policy-group:SNMPv2-MIB::systemGroup
- [GET] `SNMPv2-MIB::sysORLastChange.0` (1.3.6.1.2.1.1.8.0) verdict=satisfied min-access=read-only device=read-only
  basis: policy-group:SNMPv2-MIB::systemGroup
- [WALK] `SNMPv2-MIB::sysORID` (1.3.6.1.2.1.1.9.1.2) verdict=satisfied min-access=read-only device=read-only
  basis: delivery-metric, policy-group:SNMPv2-MIB::systemGroup
  index encoded in instance OID suffix: SNMPv2-MIB::sysORIndex
- [WALK] `SNMPv2-MIB::sysORDescr` (1.3.6.1.2.1.1.9.1.3) verdict=satisfied min-access=read-only device=read-only
  basis: policy-group:SNMPv2-MIB::systemGroup
  index encoded in instance OID suffix: SNMPv2-MIB::sysORIndex
- [WALK] `SNMPv2-MIB::sysORUpTime` (1.3.6.1.2.1.1.9.1.4) verdict=satisfied min-access=read-only device=read-only
  basis: policy-group:SNMPv2-MIB::systemGroup
  index encoded in instance OID suffix: SNMPv2-MIB::sysORIndex
- [GET] `SNMPv2-MIB::snmpInPkts.0` (1.3.6.1.2.1.11.1.0) verdict=satisfied min-access=read-only device=read-only
  basis: policy-group:SNMPv2-MIB::snmpGroup
- [GET] `SNMPv2-MIB::snmpInBadVersions.0` (1.3.6.1.2.1.11.3.0) verdict=satisfied min-access=read-only device=read-only
  basis: policy-group:SNMPv2-MIB::snmpGroup
- [GET] `SNMPv2-MIB::snmpInASNParseErrs.0` (1.3.6.1.2.1.11.6.0) verdict=satisfied min-access=read-only device=read-only
  basis: policy-group:SNMPv2-MIB::snmpGroup
- [GET] `SNMPv2-MIB::snmpEnableAuthenTraps.0` (1.3.6.1.2.1.11.30.0) verdict=partial min-access=read-write device=read-only
  basis: RFC3418 snmpBasicComplianceRev2 OBJECT clause (MIN-ACCESS read-write), delivery-metric, policy-group:SNMPv2-MIB::snmpGroup
- [GET] `SNMPv2-MIB::snmpSilentDrops.0` (1.3.6.1.2.1.11.31.0) verdict=satisfied min-access=read-only device=read-only
  basis: policy-group:SNMPv2-MIB::snmpGroup
- [GET] `SNMPv2-MIB::snmpProxyDrops.0` (1.3.6.1.2.1.11.32.0) verdict=satisfied min-access=read-only device=read-only
  basis: policy-group:SNMPv2-MIB::snmpGroup
- [GET] `SNMP-FRAMEWORK-MIB::snmpEngineID.0` (1.3.6.1.6.3.10.2.1.1.0) verdict=satisfied min-access=read-only device=read-only
  basis: delivery-metric, policy-group:SNMP-FRAMEWORK-MIB::snmpEngineGroup
- [GET] `SNMP-FRAMEWORK-MIB::snmpEngineBoots.0` (1.3.6.1.6.3.10.2.1.2.0) verdict=satisfied min-access=read-only device=read-only
  basis: delivery-metric, policy-group:SNMP-FRAMEWORK-MIB::snmpEngineGroup
- [GET] `SNMP-FRAMEWORK-MIB::snmpEngineTime.0` (1.3.6.1.6.3.10.2.1.3.0) verdict=satisfied min-access=read-only device=read-only
  basis: delivery-metric, policy-group:SNMP-FRAMEWORK-MIB::snmpEngineGroup
- [GET] `SNMP-FRAMEWORK-MIB::snmpEngineMaxMessageSize.0` (1.3.6.1.6.3.10.2.1.4.0) verdict=satisfied min-access=read-only device=read-only
  basis: policy-group:SNMP-FRAMEWORK-MIB::snmpEngineGroup
- [WALK] `SNMP-TARGET-MIB::snmpTargetAddrTimeout` (1.3.6.1.6.3.12.1.2.1.4) verdict=partial min-access=read-create device=read-only
  basis: delivery-metric, illustrative MIN-ACCESS clause attached to the response-app condition, policy-group:SNMP-TARGET-MIB::snmpTargetResponseGroup
  index encoded in instance OID suffix: SNMP-TARGET-MIB::snmpTargetAddrName
- [WALK] `SNMP-TARGET-MIB::snmpTargetAddrRetryCount` (1.3.6.1.6.3.12.1.2.1.5) verdict=satisfied min-access=read-only device=read-create
  basis: policy-group:SNMP-TARGET-MIB::snmpTargetResponseGroup
  index encoded in instance OID suffix: SNMP-TARGET-MIB::snmpTargetAddrName
- [GET] `SNMP-TARGET-MIB::snmpUnavailableContexts.0` (1.3.6.1.6.3.12.1.4.0) verdict=partial min-access=read-only device=read-only
  basis: policy-group:SNMP-TARGET-MIB::snmpTargetCommandResponderGroup
- [GET] `SNMP-TARGET-MIB::snmpUnknownContexts.0` (1.3.6.1.6.3.12.1.5.0) verdict=partial min-access=read-only device=read-only
  basis: delivery-metric, policy-group:SNMP-TARGET-MIB::snmpTargetCommandResponderGroup
- [GET] `SNMP-USER-BASED-SM-MIB::usmStatsUnknownUserNames.0` (1.3.6.1.6.3.15.1.1.3.0) verdict=partial min-access=read-only device=read-only
  basis: delivery-metric, policy-group:SNMP-USER-BASED-SM-MIB::usmMIBBasicGroup

## Requirements only partially satisfiable

Partially satisfied means an active requirement is weakened (unsupported group, access deficit or no pollable instance). Waived objects carry only inactive conditional requirements and are excluded from the polling set by design.

- `SNMPv2-MIB::snmpEnableAuthenTraps` (partially satisfied):
  - RFC3418 snmpBasicComplianceRev2 OBJECT clause (MIN-ACCESS read-write): device offers read-only, requirement needs read-write
- `SNMP-TARGET-MIB::snmpTargetAddrName` (not satisfiable):
  - SNMP-TARGET-MIB::snmpTargetAddrName is column with access not-accessible; it has no pollable instance (index columns such as snmpTargetAddrName only appear encoded inside row instance OIDs)
- `SNMP-TARGET-MIB::snmpTargetAddrTimeout` (partially satisfied):
  - illustrative MIN-ACCESS clause attached to the response-app condition: device offers read-only, requirement needs read-create
- `SNMP-TARGET-MIB::snmpUnavailableContexts` (partially satisfied):
  - policy-group:SNMP-TARGET-MIB::snmpTargetCommandResponderGroup: command responder context counters are not instrumented in firmware 4.2
- `SNMP-TARGET-MIB::snmpUnknownContexts` (partially satisfied):
  - policy-group:SNMP-TARGET-MIB::snmpTargetCommandResponderGroup: command responder context counters are not instrumented in firmware 4.2
  - delivery-metric: required metric has no active GROUP/OBJECT declaration on this device; instance existence cannot be proven from the declaration and must be verified at runtime
- `SNMP-USER-BASED-SM-MIB::usmStatsUnsupportedSecLevels` (waived (inactive condition)):
  - policy-group:SNMP-USER-BASED-SM-MIB::usmMIBBasicGroup: condition inactive (device implements the SNMPv3 User-based Security Model (USM))
- `SNMP-USER-BASED-SM-MIB::usmStatsNotInTimeWindows` (waived (inactive condition)):
  - policy-group:SNMP-USER-BASED-SM-MIB::usmMIBBasicGroup: condition inactive (device implements the SNMPv3 User-based Security Model (USM))
- `SNMP-USER-BASED-SM-MIB::usmStatsUnknownUserNames` (partially satisfied):
  - policy-group:SNMP-USER-BASED-SM-MIB::usmMIBBasicGroup: condition inactive (device implements the SNMPv3 User-based Security Model (USM))
  - delivery-metric: required metric has no active GROUP/OBJECT declaration on this device; instance existence cannot be proven from the declaration and must be verified at runtime
- `SNMP-USER-BASED-SM-MIB::usmStatsUnknownEngineIDs` (waived (inactive condition)):
  - policy-group:SNMP-USER-BASED-SM-MIB::usmMIBBasicGroup: condition inactive (device implements the SNMPv3 User-based Security Model (USM))
- `SNMP-USER-BASED-SM-MIB::usmStatsWrongDigests` (waived (inactive condition)):
  - policy-group:SNMP-USER-BASED-SM-MIB::usmMIBBasicGroup: condition inactive (device implements the SNMPv3 User-based Security Model (USM))
- `SNMP-USER-BASED-SM-MIB::usmStatsDecryptionErrors` (waived (inactive condition)):
  - policy-group:SNMP-USER-BASED-SM-MIB::usmMIBBasicGroup: condition inactive (device implements the SNMPv3 User-based Security Model (USM))
- `SNMP-USER-BASED-SM-MIB::usmUserSpinLock` (waived (inactive condition)):
  - policy-group:SNMP-USER-BASED-SM-MIB::usmMIBBasicGroup: condition inactive (device implements the SNMPv3 User-based Security Model (USM))
- `SNMP-USER-BASED-SM-MIB::usmUserSecurityName` (waived (inactive condition)):
  - policy-group:SNMP-USER-BASED-SM-MIB::usmMIBBasicGroup: condition inactive (device implements the SNMPv3 User-based Security Model (USM))
- `SNMP-USER-BASED-SM-MIB::usmUserCloneFrom` (waived (inactive condition)):
  - policy-group:SNMP-USER-BASED-SM-MIB::usmMIBBasicGroup: condition inactive (device implements the SNMPv3 User-based Security Model (USM))
- `SNMP-USER-BASED-SM-MIB::usmUserAuthProtocol` (waived (inactive condition)):
  - policy-group:SNMP-USER-BASED-SM-MIB::usmMIBBasicGroup: condition inactive (device implements the SNMPv3 User-based Security Model (USM))
- `SNMP-USER-BASED-SM-MIB::usmUserAuthKeyChange` (waived (inactive condition)):
  - policy-group:SNMP-USER-BASED-SM-MIB::usmMIBBasicGroup: condition inactive (device implements the SNMPv3 User-based Security Model (USM))
- `SNMP-USER-BASED-SM-MIB::usmUserOwnAuthKeyChange` (waived (inactive condition)):
  - policy-group:SNMP-USER-BASED-SM-MIB::usmMIBBasicGroup: condition inactive (device implements the SNMPv3 User-based Security Model (USM))
- `SNMP-USER-BASED-SM-MIB::usmUserPrivProtocol` (waived (inactive condition)):
  - policy-group:SNMP-USER-BASED-SM-MIB::usmMIBBasicGroup: condition inactive (device implements the SNMPv3 User-based Security Model (USM))
- `SNMP-USER-BASED-SM-MIB::usmUserPrivKeyChange` (waived (inactive condition)):
  - policy-group:SNMP-USER-BASED-SM-MIB::usmMIBBasicGroup: condition inactive (device implements the SNMPv3 User-based Security Model (USM))
- `SNMP-USER-BASED-SM-MIB::usmUserOwnPrivKeyChange` (waived (inactive condition)):
  - policy-group:SNMP-USER-BASED-SM-MIB::usmMIBBasicGroup: condition inactive (device implements the SNMPv3 User-based Security Model (USM))
- `SNMP-USER-BASED-SM-MIB::usmUserPublic` (waived (inactive condition)):
  - policy-group:SNMP-USER-BASED-SM-MIB::usmMIBBasicGroup: condition inactive (device implements the SNMPv3 User-based Security Model (USM))
- `SNMP-USER-BASED-SM-MIB::usmUserStorageType` (waived (inactive condition)):
  - policy-group:SNMP-USER-BASED-SM-MIB::usmMIBBasicGroup: condition inactive (device implements the SNMPv3 User-based Security Model (USM))
- `SNMP-USER-BASED-SM-MIB::usmUserStatus` (waived (inactive condition)):
  - policy-group:SNMP-USER-BASED-SM-MIB::usmMIBBasicGroup: condition inactive (device implements the SNMPv3 User-based Security Model (USM))

## Evidence trace

Every verdict below is derived only from the locked MIB definition, the compliance policy and the device declaration. When an object is required by several groups, each group keeps its own evidence row while the object still appears once in the polling set.

- `SNMPv2-MIB::sysDescr` [scalar] => satisfied
  - group `policy-group:SNMPv2-MIB::systemGroup` requirement=mandatory, min-access=read-only -> ok
- `SNMPv2-MIB::sysObjectID` [scalar] => satisfied
  - group `policy-group:SNMPv2-MIB::systemGroup` requirement=mandatory, min-access=read-only -> ok
- `SNMPv2-MIB::sysUpTime` [scalar] => satisfied
  - group `policy-group:SNMPv2-MIB::systemGroup` requirement=mandatory, min-access=read-only -> ok
  - required-metric `delivery-metric` requirement=mandatory, min-access=read-only -> ok
- `SNMPv2-MIB::sysContact` [scalar] => satisfied
  - group `policy-group:SNMPv2-MIB::systemGroup` requirement=mandatory, min-access=read-only -> ok
  - compliance-object `RFC3418 snmpBasicComplianceRev2 OBJECT clause (MIN-ACCESS read-write)` requirement=mandatory, min-access=read-write -> ok
  - required-metric `delivery-metric` requirement=mandatory, min-access=read-only -> ok
- `SNMPv2-MIB::sysName` [scalar] => satisfied
  - group `policy-group:SNMPv2-MIB::systemGroup` requirement=mandatory, min-access=read-only -> ok
  - required-metric `delivery-metric` requirement=mandatory, min-access=read-only -> ok
- `SNMPv2-MIB::sysLocation` [scalar] => satisfied
  - group `policy-group:SNMPv2-MIB::systemGroup` requirement=mandatory, min-access=read-only -> ok
- `SNMPv2-MIB::sysServices` [scalar] => satisfied
  - group `policy-group:SNMPv2-MIB::systemGroup` requirement=mandatory, min-access=read-only -> ok
- `SNMPv2-MIB::sysORLastChange` [scalar] => satisfied
  - group `policy-group:SNMPv2-MIB::systemGroup` requirement=mandatory, min-access=read-only -> ok
- `SNMPv2-MIB::sysORID` [column] => satisfied
  - group `policy-group:SNMPv2-MIB::systemGroup` requirement=mandatory, min-access=read-only -> ok
  - required-metric `delivery-metric` requirement=mandatory, min-access=read-only -> ok
- `SNMPv2-MIB::sysORDescr` [column] => satisfied
  - group `policy-group:SNMPv2-MIB::systemGroup` requirement=mandatory, min-access=read-only -> ok
- `SNMPv2-MIB::sysORUpTime` [column] => satisfied
  - group `policy-group:SNMPv2-MIB::systemGroup` requirement=mandatory, min-access=read-only -> ok
- `SNMPv2-MIB::snmpInPkts` [scalar] => satisfied
  - group `policy-group:SNMPv2-MIB::snmpGroup` requirement=mandatory, min-access=read-only -> ok
- `SNMPv2-MIB::snmpInBadVersions` [scalar] => satisfied
  - group `policy-group:SNMPv2-MIB::snmpGroup` requirement=mandatory, min-access=read-only -> ok
- `SNMPv2-MIB::snmpInASNParseErrs` [scalar] => satisfied
  - group `policy-group:SNMPv2-MIB::snmpGroup` requirement=mandatory, min-access=read-only -> ok
- `SNMPv2-MIB::snmpEnableAuthenTraps` [scalar] => partial
  - group `policy-group:SNMPv2-MIB::snmpGroup` requirement=mandatory, min-access=read-only -> ok
  - compliance-object `RFC3418 snmpBasicComplianceRev2 OBJECT clause (MIN-ACCESS read-write)` requirement=mandatory, min-access=read-write -> access-deficit (device offers read-only, requirement needs read-write)
  - required-metric `delivery-metric` requirement=mandatory, min-access=read-only -> ok
- `SNMPv2-MIB::snmpSilentDrops` [scalar] => satisfied
  - group `policy-group:SNMPv2-MIB::snmpGroup` requirement=mandatory, min-access=read-only -> ok
- `SNMPv2-MIB::snmpProxyDrops` [scalar] => satisfied
  - group `policy-group:SNMPv2-MIB::snmpGroup` requirement=mandatory, min-access=read-only -> ok
- `SNMP-FRAMEWORK-MIB::snmpEngineID` [scalar] => satisfied
  - group `policy-group:SNMP-FRAMEWORK-MIB::snmpEngineGroup` requirement=mandatory, min-access=read-only -> ok
  - required-metric `delivery-metric` requirement=mandatory, min-access=read-only -> ok
- `SNMP-FRAMEWORK-MIB::snmpEngineBoots` [scalar] => satisfied
  - group `policy-group:SNMP-FRAMEWORK-MIB::snmpEngineGroup` requirement=mandatory, min-access=read-only -> ok
  - required-metric `delivery-metric` requirement=mandatory, min-access=read-only -> ok
- `SNMP-FRAMEWORK-MIB::snmpEngineTime` [scalar] => satisfied
  - group `policy-group:SNMP-FRAMEWORK-MIB::snmpEngineGroup` requirement=mandatory, min-access=read-only -> ok
  - required-metric `delivery-metric` requirement=mandatory, min-access=read-only -> ok
- `SNMP-FRAMEWORK-MIB::snmpEngineMaxMessageSize` [scalar] => satisfied
  - group `policy-group:SNMP-FRAMEWORK-MIB::snmpEngineGroup` requirement=mandatory, min-access=read-only -> ok
- `SNMP-TARGET-MIB::snmpTargetAddrName` [column] => unsatisfied
  - required-metric `delivery-metric` requirement=mandatory, min-access=read-only -> undeclared (required metric has no active GROUP/OBJECT declaration on this device; instance existence cannot be proven from the declaration and must be verified at runtime)
- `SNMP-TARGET-MIB::snmpTargetAddrTimeout` [column] => partial
  - group `policy-group:SNMP-TARGET-MIB::snmpTargetResponseGroup` requirement=conditional, min-access=read-only, condition=device generates SNMP messages for which a response is expected (retries/timeouts apply) active=True -> ok
  - compliance-object `illustrative MIN-ACCESS clause attached to the response-app condition` requirement=conditional, min-access=read-create, condition=device generates SNMP messages for which a response is expected (retries/timeouts apply) active=True -> access-deficit (device offers read-only, requirement needs read-create)
  - required-metric `delivery-metric` requirement=mandatory, min-access=read-only -> ok
- `SNMP-TARGET-MIB::snmpTargetAddrRetryCount` [column] => satisfied
  - group `policy-group:SNMP-TARGET-MIB::snmpTargetResponseGroup` requirement=conditional, min-access=read-only, condition=device generates SNMP messages for which a response is expected (retries/timeouts apply) active=True -> ok
- `SNMP-TARGET-MIB::snmpUnavailableContexts` [scalar] => partial
  - group `policy-group:SNMP-TARGET-MIB::snmpTargetCommandResponderGroup` requirement=conditional, min-access=read-only, condition=device includes an SNMP command responder application active=True -> group-unsupported (command responder context counters are not instrumented in firmware 4.2)
- `SNMP-TARGET-MIB::snmpUnknownContexts` [scalar] => partial
  - group `policy-group:SNMP-TARGET-MIB::snmpTargetCommandResponderGroup` requirement=conditional, min-access=read-only, condition=device includes an SNMP command responder application active=True -> group-unsupported (command responder context counters are not instrumented in firmware 4.2)
  - required-metric `delivery-metric` requirement=mandatory, min-access=read-only -> undeclared (required metric has no active GROUP/OBJECT declaration on this device; instance existence cannot be proven from the declaration and must be verified at runtime)
- `SNMP-USER-BASED-SM-MIB::usmStatsUnsupportedSecLevels` [scalar] => waived
  - group `policy-group:SNMP-USER-BASED-SM-MIB::usmMIBBasicGroup` requirement=conditional, min-access=read-only, condition=device implements the SNMPv3 User-based Security Model (USM) active=False -> condition-inactive (device implements the SNMPv3 User-based Security Model (USM))
- `SNMP-USER-BASED-SM-MIB::usmStatsNotInTimeWindows` [scalar] => waived
  - group `policy-group:SNMP-USER-BASED-SM-MIB::usmMIBBasicGroup` requirement=conditional, min-access=read-only, condition=device implements the SNMPv3 User-based Security Model (USM) active=False -> condition-inactive (device implements the SNMPv3 User-based Security Model (USM))
- `SNMP-USER-BASED-SM-MIB::usmStatsUnknownUserNames` [scalar] => partial
  - group `policy-group:SNMP-USER-BASED-SM-MIB::usmMIBBasicGroup` requirement=conditional, min-access=read-only, condition=device implements the SNMPv3 User-based Security Model (USM) active=False -> condition-inactive (device implements the SNMPv3 User-based Security Model (USM))
  - required-metric `delivery-metric` requirement=mandatory, min-access=read-only -> undeclared (required metric has no active GROUP/OBJECT declaration on this device; instance existence cannot be proven from the declaration and must be verified at runtime)
- `SNMP-USER-BASED-SM-MIB::usmStatsUnknownEngineIDs` [scalar] => waived
  - group `policy-group:SNMP-USER-BASED-SM-MIB::usmMIBBasicGroup` requirement=conditional, min-access=read-only, condition=device implements the SNMPv3 User-based Security Model (USM) active=False -> condition-inactive (device implements the SNMPv3 User-based Security Model (USM))
- `SNMP-USER-BASED-SM-MIB::usmStatsWrongDigests` [scalar] => waived
  - group `policy-group:SNMP-USER-BASED-SM-MIB::usmMIBBasicGroup` requirement=conditional, min-access=read-only, condition=device implements the SNMPv3 User-based Security Model (USM) active=False -> condition-inactive (device implements the SNMPv3 User-based Security Model (USM))
- `SNMP-USER-BASED-SM-MIB::usmStatsDecryptionErrors` [scalar] => waived
  - group `policy-group:SNMP-USER-BASED-SM-MIB::usmMIBBasicGroup` requirement=conditional, min-access=read-only, condition=device implements the SNMPv3 User-based Security Model (USM) active=False -> condition-inactive (device implements the SNMPv3 User-based Security Model (USM))
- `SNMP-USER-BASED-SM-MIB::usmUserSpinLock` [scalar] => waived
  - group `policy-group:SNMP-USER-BASED-SM-MIB::usmMIBBasicGroup` requirement=conditional, min-access=read-only, condition=device implements the SNMPv3 User-based Security Model (USM) active=False -> condition-inactive (device implements the SNMPv3 User-based Security Model (USM))
- `SNMP-USER-BASED-SM-MIB::usmUserSecurityName` [column] => waived
  - group `policy-group:SNMP-USER-BASED-SM-MIB::usmMIBBasicGroup` requirement=conditional, min-access=read-only, condition=device implements the SNMPv3 User-based Security Model (USM) active=False -> condition-inactive (device implements the SNMPv3 User-based Security Model (USM))
- `SNMP-USER-BASED-SM-MIB::usmUserCloneFrom` [column] => waived
  - group `policy-group:SNMP-USER-BASED-SM-MIB::usmMIBBasicGroup` requirement=conditional, min-access=read-only, condition=device implements the SNMPv3 User-based Security Model (USM) active=False -> condition-inactive (device implements the SNMPv3 User-based Security Model (USM))
- `SNMP-USER-BASED-SM-MIB::usmUserAuthProtocol` [column] => waived
  - group `policy-group:SNMP-USER-BASED-SM-MIB::usmMIBBasicGroup` requirement=conditional, min-access=read-only, condition=device implements the SNMPv3 User-based Security Model (USM) active=False -> condition-inactive (device implements the SNMPv3 User-based Security Model (USM))
- `SNMP-USER-BASED-SM-MIB::usmUserAuthKeyChange` [column] => waived
  - group `policy-group:SNMP-USER-BASED-SM-MIB::usmMIBBasicGroup` requirement=conditional, min-access=read-only, condition=device implements the SNMPv3 User-based Security Model (USM) active=False -> condition-inactive (device implements the SNMPv3 User-based Security Model (USM))
- `SNMP-USER-BASED-SM-MIB::usmUserOwnAuthKeyChange` [column] => waived
  - group `policy-group:SNMP-USER-BASED-SM-MIB::usmMIBBasicGroup` requirement=conditional, min-access=read-only, condition=device implements the SNMPv3 User-based Security Model (USM) active=False -> condition-inactive (device implements the SNMPv3 User-based Security Model (USM))
- `SNMP-USER-BASED-SM-MIB::usmUserPrivProtocol` [column] => waived
  - group `policy-group:SNMP-USER-BASED-SM-MIB::usmMIBBasicGroup` requirement=conditional, min-access=read-only, condition=device implements the SNMPv3 User-based Security Model (USM) active=False -> condition-inactive (device implements the SNMPv3 User-based Security Model (USM))
- `SNMP-USER-BASED-SM-MIB::usmUserPrivKeyChange` [column] => waived
  - group `policy-group:SNMP-USER-BASED-SM-MIB::usmMIBBasicGroup` requirement=conditional, min-access=read-only, condition=device implements the SNMPv3 User-based Security Model (USM) active=False -> condition-inactive (device implements the SNMPv3 User-based Security Model (USM))
- `SNMP-USER-BASED-SM-MIB::usmUserOwnPrivKeyChange` [column] => waived
  - group `policy-group:SNMP-USER-BASED-SM-MIB::usmMIBBasicGroup` requirement=conditional, min-access=read-only, condition=device implements the SNMPv3 User-based Security Model (USM) active=False -> condition-inactive (device implements the SNMPv3 User-based Security Model (USM))
- `SNMP-USER-BASED-SM-MIB::usmUserPublic` [column] => waived
  - group `policy-group:SNMP-USER-BASED-SM-MIB::usmMIBBasicGroup` requirement=conditional, min-access=read-only, condition=device implements the SNMPv3 User-based Security Model (USM) active=False -> condition-inactive (device implements the SNMPv3 User-based Security Model (USM))
- `SNMP-USER-BASED-SM-MIB::usmUserStorageType` [column] => waived
  - group `policy-group:SNMP-USER-BASED-SM-MIB::usmMIBBasicGroup` requirement=conditional, min-access=read-only, condition=device implements the SNMPv3 User-based Security Model (USM) active=False -> condition-inactive (device implements the SNMPv3 User-based Security Model (USM))
- `SNMP-USER-BASED-SM-MIB::usmUserStatus` [column] => waived
  - group `policy-group:SNMP-USER-BASED-SM-MIB::usmMIBBasicGroup` requirement=conditional, min-access=read-only, condition=device implements the SNMPv3 User-based Security Model (USM) active=False -> condition-inactive (device implements the SNMPv3 User-based Security Model (USM))

