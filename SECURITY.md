# Security policy

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x | Yes |
| Older `main` commits | No |

Version 0.1.0, dated 2026-11-06, is the first release (`.agents/VERSION`, `CHANGELOG.md`). A fix comes as a new 0.1.x release, which `team update` installs.

## Reporting a vulnerability

Report it privately, through GitHub private vulnerability reporting on the repository: <https://github.com/vaiev/wouf/security/advisories/new>. This is the only channel. Do not open a public issue or pull request with the report.

Say what you did, what happened and what you expected, with the version (`team --version`) and the mode (`container`, `bwrap` or `host`). Do not include a private key, password, token, host address, or a log that might contain one of those. If a secret was already pasted somewhere public, revoke it before reporting.

## What counts as a vulnerability

A way around a boundary that the code is meant to hold, in particular:

- **Sandbox escape:** a bot reads or writes outside its own storage and task, or sees the host, the project folder or another bot, in `container` or `bwrap` mode; or a result copied back from a bot writes outside the folder it is copied to, or brings a link with it.
- **Key leak:** a key reaches a container's configuration, a file, a log, a receipt or the output unmasked, by a path that is not one of the known gaps listed in `README.md` under "Limits"; or `federate` accepts an envelope that is not signed by a trusted peer's key.
- **Review bypass:** a flow ends `ready`, or `team merge propose` accepts a task, without the approvals of the exact head commit that its class requires (in a flow, also a green gate on that head); or a federated envelope starts a run or becomes this team's decision.
- A template or skill that tells an agent to send, publish, delete, spend or merge without the approval the rules require, or a contradiction between files that an agent could resolve towards the less safe reading.

## What does not count

The limits the project documents in `README.md` under "Limits" (in more detail in `docs/guide/limits.md`) are known, not vulnerabilities. Among them:

- An agent ignoring the rules. The rules are instructions to the models; enforcement comes from the sandbox, the checks in code, and the permissions and branch protection of your repository host.
- Anything a bot does in `host` mode, which has no sandbox.
- What an open network allows: the default `network = bridge` is not filtered, and with `egress` an allowed host can still carry data out.
- The known gaps of secret masking, and a process running as your own user reading the secret store.
- `mode = bwrap` being weaker than a container, as `docs/DESIGN.md` §2 describes.
