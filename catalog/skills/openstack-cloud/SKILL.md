---
name: openstack-cloud
description: >-
  Use when an agent runs servers, networks, volumes or object storage on an
  OpenStack public cloud (for example a Swiss provider) with OpenTofu: wrapper
  and credentials, quotas, flavors and resizes, cost tracking, object storage
  quirks, and what an agent must never do there.
---
# OpenStack cloud with OpenTofu

Placeholders: `<project>` (your OpenStack project), `<region>`, `<prefix>` (resource name prefix), `<secret-store>` (directory outside the repository, readable only by wrappers).

## Access through one wrapper

- One script (`scripts/os.sh`) pins the cloud config file from `<secret-store>`, the project and the region, then runs `openstack "$@"`. Agents call the wrapper, never `openstack` with another config, and never with `--debug` (it prints tokens).
- Use an **application credential** scoped to the one project, with an expiry date you track as a deadline. It cannot create EC2/S3 credentials ("delegated tokens cannot access credentials directly"): a human creates those once and stores them encrypted (`sops-secrets`).
- If the organisation has several projects, the credential sees only yours; still leave quota headroom for the others.
- Smoke test: `scripts/os.sh token issue -c expires`.

## Infrastructure as code

- Everything that exists is in OpenTofu; the CLI is for reading. Run OpenTofu through a wrapper too (`infra/tofu.sh plan|apply|test`): it supplies the state passphrase and provider credentials at call time.
- Encrypt the state (OpenTofu state encryption with a passphrase from the secret store) before it is committed or uploaded.
- `prevent_destroy = true` on every stateful resource (volumes, buckets, DNS zones). Deletions are done by a human.
- `tofu test` with `mock_provider "openstack" {}` covers names, fixed IPs, security-group rules, cloud-init content and capacity — before any plan against the real cloud.
- **Quota as a precondition:** compute the sum of vCPU/RAM from flavor names (`a<vcpu>-ram<gb>-disk<gb>` style) plus machines outside the main topology, and fail the plan above the quota. A test shows the plan fails one flavor too large.
- Apply only the plan you just read: `plan -out`, check adds/changes/deletes, `apply` that file.

## Flavors and resizes

- A flavor change is an **in-place resize with a reboot** (plan shows `~1`), never a replace. Announce the downtime.
- The root disk can only grow: keep the same disk size across profiles, or the resize fails. Pin the flavor's disk size in a test.
- A change of `user_data` (cloud-init) **replaces** the server. Put later host changes (swap, packages) into idempotent scripts run over SSH, or add `ignore_changes` deliberately.
- Image updates: `ignore_changes = [image_id]` so a newer image does not rebuild servers.
- Stopped servers still cost the reserved part; real savings mean snapshot + delete (a human decision).

## Costs

- Rating via CloudKitty (`python-cloudkittyclient` in the same venv): `rating summary get -g type` (month by type) and `rating dataframes get -b <hour>Z -e <hour+1>Z` (per resource). Rated hours lag a few hours; HTTP 404/500 for an hour means "not rated yet".
- Derive a price model per vCPU and per GB RAM from two measured flavors, then check it against a third. Project to the end of the credit period weekly; propose a smaller profile as soon as the projection exceeds the budget.

## Object storage (Swift + S3 API)

- S3 is usually **path-style** and needs SigV4 even when the region name looks odd (some providers expect `us-east-1`). Unsigned requests may answer with a misleading error.
- No bucket lifecycle or object lock on many providers: expire objects per object (`X-Delete-After`) and prune versions with your own job.
- Versioning on backup buckets; buckets private; one S3 user per writer (backup jobs never use a human's key).
- A guarded client: GET/HEAD free, PUT only with an approval and a log line, DELETE refused, object bodies never printed.

## Never

- Touch another project of the organisation.
- Print or copy credentials; run `ec2 credentials create` as an agent.
- Delete servers, volumes, buckets or zones; run a destroy.
- Open a security group wider than the spec says (egress is explicit too).
