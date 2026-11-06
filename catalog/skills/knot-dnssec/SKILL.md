---
name: knot-dnssec
description: >-
  Use when setting up or changing your own authoritative name servers with
  Knot DNS and automatic DNSSEC (primary signs, secondary transfers): zone
  files as code, signing policy, TSIG, checks, and the off-node key backup.
---
# Own name servers: Knot DNS with automatic DNSSEC

Placeholders: `ns1.<zone>` / `ns2.<zone>` on two public hosts (`192.0.2.10`, `198.51.100.20` here), second-network addresses `203.0.113.10` / `203.0.113.11`, `<age-recipient>` (public key of an offline age key).

## Layout as code

| Repository | Host | Role |
|---|---|---|
| `dns/zones/<zone>.zone` | primary `/etc/knot/zones/` | source of truth; Knot never rewrites it (`zonefile-sync: -1`, `zonefile-load: difference-no-serial`, `serial-policy: unixtime`) |
| `dns/knot-primary.conf`, `dns/knot-secondary.conf` | `/etc/knot/knot.conf` | listen on the private address + loopback (public address via NAT/floating IP), TSIG ACLs, `mod-rrl` |
| `dns/tsig.conf` (generated, encrypted in the repo) | both, `root:knot 0640` | one TSIG key for transfers and notifies |
| `dns/apply.sh` | staged on the host | check config, swap atomically, restart with rollback, reload zones, wait for signatures |

## Signing policy

- `algorithm: ecdsap256sha256` (13), automatic ZSK rollover (e.g. 90 days), **manual KSK** (a KSK change needs a DS change at the registrar: plan it), NSEC3 with 0 iterations and no salt (RFC 9276), no CDS/CDNSKEY until you decide to automate DS updates.
- Only the primary signs; the secondary serves the signed zone by AXFR/IXFR. Never sign on both.

## Checks after every change

1. `knotc conf-check`, `kzonecheck` on the new zone file before it goes live (package `knot-dnssecutils`).
2. After reload: wait until the zone has RRSIGs, then `knotc zone-check`.
3. Secondary in sync: same SOA serial and DNSKEY set on both public addresses:
   `dig +norecurse +dnssec +noall +answer @192.0.2.10 <zone> SOA` = same against `@198.51.100.20`.
4. Offline validation: `knotc zone-flush <zone> +outdir <dir>`, then `kzonecheck -d on` on the export.
5. AXFR from outside must fail; no recursion; `version.bind` refused.
6. DS for the registrar: `keymgr <zone> ds` (digest type 2). Recompute it from the public DNSKEY once to prove both match.

`knotc` and `keymgr` live in `/usr/sbin` and need root.

## Key backup off the host (do this before any DS goes to a registrar)

- `knotc -b zone-backup +backupdir <dir> +keysonly` → `tar` → `age -r <age-recipient>` **on the host**, delete the plaintext, upload only the ciphertext to versioned object storage.
- Name the object after a fingerprint of the key state (hash of `keymgr <zone> list` for all zones): a new object appears only when keys change, and old states stay.
- Test once that it decrypts — print counts, never key material (`age -d … | tar -t | grep -c '\.pem$'`).
- Restore on a rebuilt primary: stop Knot **before** the first sync, or it generates new keys and every DS breaks.

## Pitfalls

- Both name servers in one data centre fail together; keep SOA expire long and plan a third server elsewhere.
- Public port 53 attracts abuse: enable response rate limiting.
- A record changed at the old DNS provider during a migration must also go into the Knot zone file, or the copy is stale.
- Never change keys on the primary while a DS at the parent points to them.
