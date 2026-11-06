---
name: dns-cutover
description: >-
  Use when moving a zone to new authoritative name servers without SERVFAIL,
  especially when DNSSEC is on at the old or the new side: gates, order of
  glue / DS / NS steps, waiting times, verification and rollback.
---
# DNS cut-over without downtime (DNSSEC-safe)

Every registrar step (glue, NS, DS) runs only with the owner's go **at that moment**. One step, verify, wait, next step.

## Gates before the first registrar step

1. New servers answer the same signed data on every public address (serial, DNSKEY, RRSIGs).
2. The signing keys are backed up off the host and the backup decrypts (`knot-dnssec`).
3. Record content equals the old provider's, TTLs included (`dig` every name against old and new servers). Re-check on the day: records changed at the old provider since the copy must be copied again.
4. You know how the old provider behaves when DNSSEC is switched off there (some stop signing at once while the DS is still at the parent → bogus answers until the DS TTL expires). Accept that window or ask the provider first.
5. Name server host names resolve worldwide before glue is needed: publish their A records in the **currently active** zone a day ahead.

## Order for a zone that is signed today

1. **Glue** (only in the zone that carries the name server names): register host objects at the registry. Harmless while the zone still uses the old servers.
2. **DS off** at the registrar. Verify at the parent's own servers: `dig +norecurse DS <zone> @<parent-ns>` is empty. Wait **2 × DS TTL**.
3. **NS** to the new servers. Verify at the parent (`dig +norecurse @<parent-ns> <zone> NS`, glue in the additional section), then through public resolvers (serial = new primary), web and mail checks. Wait **2 × NS TTL**.
4. **DS on** with the new key (`keymgr <zone> ds`). Verify the DS at the parent, `ad` flag at two independent validating resolvers, equal serial and RRSIGs on both new servers.

**Never NS and DS in one step.** Resolvers that still cache the old NS would get unsigned or differently signed answers while a DS exists: the zone turns bogus for up to the NS TTL.

An unsigned zone skips step 2. Count waits from the moment the change is visible at the parent's servers, not from the registrar's "saved".

## Mail domains

Move mail-free zones first, the zone with the owner's mailbox last. After the NS step: send and receive a test mail; check that DKIM selectors still resolve (a `_domainkey` delegated to the mail provider must stay reachable); check provider redirect services that depended on the old DNS host.

## Rollback

- Before a DS is set: switch NS back at the registrar; recovery takes the NS TTL.
- After a DS is set and validation fails (`SERVFAIL`, no `ad`): remove the DS first (recovery ≤ DS TTL), then investigate. Never touch keys while a DS points to them.
- Keep every old zone untouched for at least 7 days after its cut-over: it is the rollback.

## Verification commands

```bash
dig +norecurse @<parent-ns> <zone> NS
dig +norecurse @<parent-ns> <zone> DS
dig @<validating-resolver> <zone> SOA +dnssec        # ad flag, serial
dig +norecurse +dnssec @ns1.<zone> <zone> SOA         # same serial on ns1 and ns2
```
