---
name: sops-secrets
description: >-
  Use whenever an agent works with secrets: where they live, how SOPS + age
  encrypts the ones a repository needs, wrapper scripts that read them at
  call time, and the rules that keep secrets out of transcripts.
---
# Secret hygiene with SOPS + age and wrappers

## Where secrets live

- **Outside the repository:** API keys, SSH keys and the age private key sit in a secret store directory (`<secret-store>`, mode 0700) that only wrapper scripts read. Agents never read, print, copy or move files there.
- **In the repository, encrypted:** values a deployment needs (database passwords, backup passphrases, S3 keys) live in `secrets/*.enc.yaml`, encrypted with SOPS for an age recipient (`.sops.yaml`: `creation_rules: - path_regex: secrets/.*\.enc\.yaml$  age: <age-recipient>`).
- **On hosts:** files with mode 0600 or 0640 owned by the service; never in container images, never in environment dumps or logs.

## Wrappers

- `scripts/secret.sh <file> <key>` validates both names and runs `sops decrypt --extract '["<key>"]' secrets/<file>.enc.yaml`; its output goes straight into the consuming process (a pipe or a 0600 temp file), never to the terminal.
- Provider APIs (cloud, DNS, Git hosting) go through guarded wrappers that read the token at call time, allow GET freely, require a written approval for mutations, refuse deletes, log mutations without bodies, and redact responses.
- Generate secrets inside the wrapper (`openssl rand -base64 48 | sops ...`); verify them by length or fingerprint, never by value.

## Rules for agents

- Never print a secret, a token or an environment dump; inspect names only (`env | cut -d= -f1`). Every tool output ends up in a transcript.
- Never write a secret into a file that git tracks unencrypted, into `.git/config`, or into a command line that shows up in `ps` or shell history.
- Secret bundles for a host: create, `scp`, `install`, then `shred -u` on both ends.
- Tests for secret handling use invented names and generated keys, never real ones.
- If a secret leaked into a transcript, log or commit: rotate it first, then clean up.

## Rotation

- Keep a deadline for every expiring credential (token, application credential, certificate) a few days before it expires.
- Rotate by adding the new value, switching consumers, verifying, then removing the old one (a human removes).
