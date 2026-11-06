# Limits

What WOUF does not do, said plainly. Read this before you give the team real data or real keys.

## Rules are instructions, not enforcement

The rules, roles and skills are text the models are asked to follow. A model can misread or ignore them. Only the sandbox, the network setting in container and `bwrap` mode, what comes back from a bot and the checks in code such as the gate and the exact-head approval are enforced by programs. Merging, publishing and branch protection stay with you and your repository host.

## Host mode has no sandbox

`mode = host` runs the bots directly on your computer, as your user. They can reach whatever your user can reach, including the network: `network = none` does not take it away in this mode. The gate command also runs on your computer, in a throwaway clone, not in a sandbox. It is meant only for trying things out.

## Bubblewrap is weaker than a container

`mode = bwrap` has no system call filter; the run says so at every start. CPU, memory and process limits apply only where a systemd user scope works on your computer. Without one, a bot can use all of the computer's power, and the run only warns about it. The tests check the command that sets these limits, not that systemd enforces them. Tools that need administrator rights are not installed for the bots.

## The default network is open

The default is `network = bridge`. It is not filtered: a bot can reach the internet, your local network and, on a cloud VM, the provider's metadata service. Block those on the engine's host if that matters, or use `network = egress`.

With `egress`:

- An allowed host can still carry data out, for example a package registry that takes uploads. For encrypted connections the proxy sees only the host name, not what is sent.
- Keys still reach the bot as with `bridge`; there is no key proxy yet. The CLI's model API and login hosts must be open.
- The default host lists for each CLI are unverified until you try them: run the CLI once, read the refused hosts in `egress.log`, and set the lists to what it really needs.
- The proxy refuses private and loopback addresses, so it cannot reach a model server on your own computer or in a private network.
- In container mode it needs Podman with aardvark-dns 1.11.0 or newer, or a Docker version with the fix for a known name-resolution leak; older ones are refused.

## Secret masking has gaps

Masking keys in a bot's output prevents accidents. It is not a barrier against a bot that wants to leak a key it was given. Known gaps:

- binary files are not scanned for key patterns; only registered values are replaced in their bytes
- a registered value split across lines, or spanning more lines than the stream buffer holds, may pass in streamed output
- a value containing `/` is not masked in a path
- your own files are checked only where they are text

The key store keeps keys out of the repository, the chat and the bots' storage, but a process running as your own user could still read it.

## The lead reads what bots write

Bot output reaches the lead framed as data, not instructions. The lead is still a model reading text a bot wrote.

## What the tests cover

The container flow is tested with Docker and a stand-in agent CLI. The tests that need a real container engine or a working bubblewrap are skipped on a computer without them, so a green run there proves less. The commands for the real agent CLIs and the example connectors follow their documentation; check them against your versions.

## Version 0.1.0

Version 0.1.0, dated 2026-11-06, is the first release. This guide describes `main`; the changelog says what 0.1.0 contains.
