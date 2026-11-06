---
name: postgres-dual-backup
description: >-
  Use when setting up, checking or restoring PostgreSQL backups with two
  independent lines: pgBackRest point-in-time recovery to S3 plus nightly
  age-encrypted pg_dump files, including pruning and the restore drill.
---
# PostgreSQL: two independent backup lines

Placeholders: buckets `<prefix>-backups-pitr` and `<prefix>-backups-dumps` (versioned, private), `<age-recipient>` (public key; the private key stays offline with the owner).

## Why two lines

- **Line 1, pgBackRest (PITR):** full + differential backups and the WAL archive, encrypted by pgBackRest itself (`repo1-cipher-type=aes-256-cbc`, passphrase in the encrypted secret store). Restores any point in time. **Losing the passphrase loses the repository**: store it in two places.
- **Line 2, logical dumps:** `pg_dump -Fc` per database plus `pg_dumpall --globals-only`, piped through `age -r <age-recipient>`, uploaded per database and timestamp. Independent of pgBackRest's format and keys; the private key never touches the server.

## Schedule (example)

| Job | When (UTC) | What |
|---|---|---|
| pgBackRest full / diff | Sun full, Mon–Sat diff, 01:30 | `pgbackrest --stanza=main backup --type=full\|diff` |
| WAL archive | continuous, `archive_timeout` 5 min | `archive-async=y` |
| dumps | 02:30 | one object per database, `<db>/<UTC>.dump.age` |
| prune | 03:30 | drop non-current versions older than 14 days and orphaned delete markers; keep current dumps 14 days but **always the newest 3 per database** |

Run everything as `postgres` under systemd timers; write a small status JSON per job so monitoring can alert on age and failure.

## Credentials

- One S3 user for the backup jobs; humans never put their keys on the database host.
- Secrets reach the host as a bundle created on the workstation (lengths printed, never values), copied by `scp`, installed with `install -m 0600`, then `shred -u` on both ends.

## Restore drill (quarterly and before larger changes)

1. Line 1: on a throw-away node, restore read-only from the repository to a canary timestamp (`--type=time --target=<ts>`), check that rows written before the timestamp exist and rows after it do not. Prove the production repository was not touched (no new timeline history file; `pgbackrest check` still fine).
2. Line 2: decrypt one dump on the workstation into a pipe and restore it on the throw-away node into a fresh database (`age -d -i <offline key> | pg_restore -d <db>`); print only counts.
3. Record the restore time; remove the throw-away node (owner action).

## Checks

- `pgbackrest --stanza=main info` shows the expected full/diff sets; `pgbackrest check` passes.
- The newest dump per database is younger than 26 hours; its size is plausible.
- `systemctl --failed` is empty on the database host.
