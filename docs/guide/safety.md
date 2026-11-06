# Safety

The rules in `.agents/` are instructions to the models. A model can misread or ignore them. What actually limits a bot is the place it runs in, the network it gets and what comes back from it. This part explains each, with its trade-offs.

## How `.agents/` is built

- `.agents/rules.md`, `.agents/roles/` and `.agents/skills/` are the shared pack: plain text every member reads.
- `.agents/team/` is your team: `config.ini` with the settings, `bots/` with your bots, and the board, decisions, schedule, knowledge and logs.
- `.agents/bin/` holds the helpers. They are standard-library Python, so the pack needs no package installs of its own.
- `.agents/bots/` and `.agents/connectors/` hold examples. They are off until you say yes, and a connector reaches only the bot that lists it.

Setup copies `.agents/` into your folder. Outside it, setup only touches your own secret store and installs software you agree to; it changes no global settings.

## Sandbox modes

`mode` in the `[environment]` section of `.agents/team/config.ini` says where bots run. A new team starts as `undeclared`, and the lead runs setup first.

Docker, Podman and bubblewrap runs stage inputs and a repository copy into a separate workspace; host mode runs without a sandbox and can access the host under the user's permissions.

### Container (recommended)

One image for the whole team holds the agent CLIs and tools. Each bot has its own storage, and its container exists only while it works on a task. The container gets the bot's storage as its only volume; the original project and home folders are not mounted as such. The lead copies the brief and inputs in and the results out.

Every container the helper creates drops all capabilities, cannot gain new privileges and has a limit on processes; a bot's container also has CPU and memory limits. A bot that works longer than the time limit is stopped.

This needs Docker or Podman, on your computer or on a VM or server reached through an SSH alias.

### Bubblewrap (`bwrap`)

For Linux without Docker or Podman. Each run gets a bubblewrap sandbox with its own namespaces, no capabilities and no host environment variables. It sees its own folders (its storage, inputs and outbox, and a small team prefix that stands in for the image) and, read-only, parts of your computer: the program folders such as `/usr`, `/bin` and `/lib`, and a fixed list of entries in `/etc`, such as certificates, time zone and name resolution settings. Nothing of your home folder or project is mounted. Anything kept in those program folders or `/etc` entries is readable by the bot, so keep no secrets there.

The message at every start says "only its own folders"; the read-only program folders and `/etc` entries above come on top.

It is weaker than a container. There is no system call filter, and the run says so at every start. CPU, memory and process limits depend on your computer: where a systemd user scope works, each run and the gate get the `memory` and `cpus` settings and a process limit; where it does not, the run warns that there are no such limits and goes ahead without them. `team doctor` tells you which case you have. Software that needs administrator rights is not installed for the bots; you install it.

### Host (no sandbox)

`host` runs the bots directly on your computer, in `.agents/team/env/<id>/`, with the agent CLI's own safe defaults. There is no sandbox. Use it only to try things out.

## Network and egress

`network` in the same section decides what a bot can reach in container and `bwrap` mode. In `host` mode it fences nothing: there is no sandbox, so a bot uses your computer's network whatever `network` says, `none` included.

- `bridge`, the default: open internet access, which cloud models need. It is not filtered: a bot can also reach your local network and, on a cloud VM, the provider's metadata service. Block those on the engine's host if that matters.
- `none`: no network in container and `bwrap` mode. This only fits local models inside the container. It does not take the network away in `host` mode.
- `egress`: the bot reaches outside hosts only through a proxy for each run, which lets through only an allowlist; the bot's own loopback is the one exception, see below. In container mode the bot sits on an internal network with no way out, and the proxy sits on both networks. In `bwrap` mode the bot has no network beyond its own loopback and reaches the proxy through a socket.

With `egress`, each run gets the base `allow` list plus the model and login hosts of its own CLI. A brief can add hosts for one task with an `Egress:` line; a host outside the base list needs the lead's recorded yes for that task first.

There is one exception to "only through the proxy": the bot reaches its own loopback (`localhost`, `127.0.0.1`, `::1`) directly, without the proxy. This exception is fixed in code; neither the config nor a brief can widen it. It is the loopback inside the bot's own container or sandbox, not your computer's: in `bwrap` mode a test checks that the bot cannot reach a service on the host's loopback, though that test runs only where bubblewrap can start a sandbox. Separately, the proxy refuses names that resolve to private, loopback or link-local addresses, so the bot cannot reach those through the proxy either. Older Docker and Podman versions with a known name-resolution leak are refused.

`bwrap` mode refuses `bridge`, and `host` mode refuses `egress`, because there is no sandbox to fence in.

## Keys

`.agents/bin/secret` keeps keys out of the repository, the chat and the bots' storage. You type a key yourself, in your own terminal, with `secret set NAME`; it has no command that prints a value back. It uses your system's keychain or Secret Service when available, else a file store with owner-only permissions that it refuses to keep inside a git repository.

A bot gets a key for one run, as a line on its standard input, never in an argument or environment variable of the container command. Keys found in what a bot hands back are masked as `***`.

Masking prevents accidents. It is not a barrier against a bot that wants to leak a key it was given. The limits part lists the known gaps.

## Bot output is data

What a bot prints and hands back reaches the lead framed as data from the bot, not as instructions, and the lead's role tells it so. Results come back as plain files and folders only: the helper skips and lists links instead of following them.

The lead is still a model reading text a bot wrote. The framing helps; it does not make the lead immune to what it reads.
