# Catalog

Optional examples that a team may copy. The catalog is off by default and outside the pack: nothing in `.agents/` loads it, and the team works the same without it. Copy what you need, read it, adapt it.

## Skills (`skills/`)

Playbooks for running a small sovereign platform with coding agents. They use placeholders (`<project>`, `<zone>`, documentation IP ranges), never real values.

| Skill | Use when |
| --- | --- |
| [openstack-cloud](skills/openstack-cloud/SKILL.md) | Servers, networks, volumes or object storage on an OpenStack cloud with OpenTofu |
| [knot-dnssec](skills/knot-dnssec/SKILL.md) | Own authoritative name servers with Knot DNS and automatic DNSSEC |
| [dns-cutover](skills/dns-cutover/SKILL.md) | Moving a zone to new name servers without SERVFAIL (glue, DS, NS order) |
| [postgres-dual-backup](skills/postgres-dual-backup/SKILL.md) | pgBackRest PITR plus age-encrypted dumps, pruning, restore drill |
| [sops-secrets](skills/sops-secrets/SKILL.md) | Any work that touches secrets: store, SOPS + age, wrappers, rotation |
| [project-starter](skills/project-starter/SKILL.md) | Starting a project: agent setup, stack evaluation, setup order |

To use one, copy its folder into the team's skills: `cp -r catalog/skills/<name> .agents/team/skills/`.

## Tools (`tools/`)

| Tool | What it is |
| --- | --- |
| [companion](tools/companion/README.md) | A read-only cockpit for a team folder (phase gates, task waves, bots, federation, log), live over server-sent events, and a single-file showcase export |
| [hub](tools/hub/README.md) | A knowledge hub: SQLite full-text search over notes, decisions and documents, plus work claims and handoffs between agents |
| [control-mcp](tools/control-mcp/README.md) | A template MCP server that lets an agent run only allow-listed actions, with approvals and an audit log |
| [local-runner](tools/local-runner/README.md) | Check a local model server and generate a Codex runner section |
| [egress-proxy](tools/egress-proxy/README.md) | A forward proxy that lets a bot container reach only the host names on an allowlist, at public addresses; `team run` starts it for `network = egress` |

To use one, copy its folder anywhere (for example `cp -r catalog/tools/hub ~/tools/`) and follow its README. hub, control-mcp and egress-proxy are standalone, and companion needs the WOUF pack beside the team folder (for its secret patterns); all need only Python 3.9+ (control-mcp reads TOML: Python 3.11+, or `pip install tomli`; its server also needs the MCP SDK from its `requirements.txt`); optional extras are named in their READMEs.

## Tests

`python3 -m unittest discover -s tests` from the repository root checks the catalog (`tests/test_catalog.py`) and runs the tool tests. Tests that need an optional package are skipped with the reason.
