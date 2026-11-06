# Egress allowlist and key proxy for the bot containers (T-0029, design only)

Status: proposal, 2026-10-29, written by a peer lead. No code; the owner decides before any is written.

## Problem

A bot container today runs with `[environment] network = bridge`, so it can reach every host on the internet. The
keys of its run (the model API key, connector keys) come in on standard input and live as environment variables
for that run (DESIGN.md, "Keys"). So a bot that is talked into it by text it reads, a dependency's install
script, or a tool it calls can send a key, the repository or its brief to any host, and fetch and run anything.

Masking hides keys in what comes back, but not in what goes out. The idea is taken from GitHub Agentic Workflows'
firewall [S1]: a Squid proxy filters the agent's traffic by a domain allowlist, and an API proxy sidecar "holds
LLM API keys so they never reach the agent process".

## Goals

- **G1:** a bot container holds no real key. The model call works through a proxy that adds the key.
- **G2:** a bot reaches only the hosts on its task's allowlist. All else fails, raw IPs and DNS included.
- **G3:** it runs rootless with Podman on one rented host, with no root daemon and no host firewall changes the owner
  must keep in step.
- **G4:** the gate keeps `gate_network = none`; a team that does not opt in runs as today (`bridge` or `none`).
- **Non-goals:** stopping a leak through an allowed host (a gist on github.com); TLS inspection; ingress.

## Options

| | A: open bridge (today) | B: no network + key socket | C: internal network + proxy container | D: host firewall by uid |
|---|---|---|---|---|
| How | `--network bridge` | `--network none`; a Unix socket from the host mounted in, a small forwarder in the image | per run, an `--internal` bridge network for the bot and a proxy container on it and on a second bridge network | nftables `meta skuid` rules for the rootless user, IP sets from DNS |
| Keys in the container | yes | no | no | yes |
| Host allowlist | no | none possible (no network at all) | yes, by name (CONNECT host, SNI) | by IP, which drifts with CDNs |
| Package installs, `git fetch` | yes | no | yes, through the proxy, if allowed | yes |
| Rootless Podman | yes | yes | yes, with netavark and aardvark-dns ≥ 1.11.0 (see 1. below) | needs root for nft rules |
| Moving parts | 0 | forwarder in the image, socket mount | one proxy process per run | firewall state on the host |
| Fits Docker too | yes | yes | only Moby ≥ 26.0.0, 25.0.5 or 23.0.11 (CVE-2024-29018 [S4]) | no |

**B** is the strongest for model-only bots but breaks every bot that installs or fetches. **D** keeps the keys
in the container and needs root. **C** meets G1 to G4.

## Recommendation: C, one proxy per run

```text
 bot container ──(bridge wouf-<run>-in, --internal)──► proxy container ──(bridge wouf-<run>-out)──► allowed hosts
   HTTPS_PROXY=http://proxy:3128                              :3128 CONNECT, allowlist by host name
   ANTHROPIC_BASE_URL=http://proxy:8080/anthropic             :8080 reverse proxy, adds the real key
   ANTHROPIC_API_KEY=dummy-<run>                              keys only here, from the run's stdin
```

1. **Networks.** `team run` creates two named bridge networks, `wouf-<run>-in` with `--internal` and
   `wouf-<run>-out` without it. The bot joins only `-in`, which has no gateway. On an internal network
   "aardvark-dns will only resolve container names", and other queries get `NXDOMAIN` [S2]. That holds from
   aardvark-dns 1.11.0 ("Do not allow 'internal' networks to access DNS" [S3]); older versions forward unknown
   names to the host's resolvers, which carries data out in DNS queries. Docker had the same hole on `--internal`
   networks until the fix for CVE-2024-29018 [S4]. So `network = egress` needs those versions.
2. **Proxy container.** The same team image runs a small standard-library Python proxy (in `catalog/tools/`,
   about 300 lines), started with `--network wouf-<run>-in --network wouf-<run>-out` [S5], and removed with the
   run, under `HARDEN` and a read-only root.
   - **Not pasta mode:** Podman refuses named networks with any mode but bridge [S8], so no container joins
     `pasta` here.
   - **Rootless:** rootless bridge networks reach the host through Podman's rootless network namespace. Which tool
     links it (pasta or slirp4netns) is unchecked. The topology smoke test below is the first trial step.
   - **`:3128`** answers HTTP `CONNECT host:443` only for hosts on the run's allowlist, matched exactly or as a
     `.suffix` entry. It refuses IP literals and other ports, and every name that resolves to a private,
     loopback, link-local or metadata address (checked after resolving, against DNS rebinding). No TLS is
     opened: the bot's TLS goes end to end.
   - **Plain HTTP** (`HTTP_PROXY`): only `GET` and `HEAD` to an allowed host; every other method is refused.
   - **`:8080`** is a reverse proxy for the model APIs the team's runners use (`/anthropic/` →
     `api.anthropic.com`, `/openai/` → `api.openai.com`, …). It replaces the dummy key with the real one,
     which it got on its own standard input the way bots get keys today. It allows only `POST` and `GET` on
     the API's documented path prefixes, and it can stop a run at a token or request budget.
   - **Log:** one line per request (host, path prefix, status, bytes, time) in the run folder, which `team result`
     shows, masked with the real keys. The bot's `WOUF_MASK` list then holds only its dummy keys.
3. **Bot environment.** The bot gets `HTTPS_PROXY`/`HTTP_PROXY`, the model `*_BASE_URL` variables and a dummy
   key per runner. Claude Code reads `ANTHROPIC_BASE_URL` [S6]. Codex documents `openai_base_url` in its user
   config [S7]; whether it still reads `OPENAI_BASE_URL` is unchecked. npm, pip and git honour `HTTPS_PROXY`. A
   runner that cannot take a base URL stays on option A until it can, and `team doctor` names it.
4. **Allowlist.**
   - **Base list:** `[egress] allow =` in `config.ini`. It starts empty: only the model APIs through `:8080`.
   - **Per task:** an optional `Egress:` line in the brief, for example `Egress: pypi.org, registry.npmjs.org`.
   - **Who adds a host:** a host on the brief line that is not in the base list needs the lead's yes for that
     task, recorded as a `decision.recorded` event.
   - **`team flow`:** passes the list to every step of the task. Review and decide get only the model API.
5. **Opt in.** `[environment] network = egress` turns this on; `bridge` and `none` keep today's behaviour.
   - **`mode = host`:** there is no container, so `team run` refuses `network = egress` there.
   - **Engine:** the network and the proxy are made on the engine the bot runs on, a remote `docker_host`
     (`ssh://`) included. The real keys then reach that host as the bot's keys do today.
   - **Gate:** `gate_network` is unchanged; set to `bridge` for installs, it stays open. An egress gate is later.

## Staging stays offline

`team run` copies the inputs, the pack and a bundle of the repository (`in/repo.bundle`) into the container
through the engine, before it starts (`teamlib/run.py`, `cp … :/inbox`); the bot fetches from that local file. The
results come back as `out/result.bundle` through the same engine copy (`collect`). None of this uses the
container's network, so egress changes nothing there; review and decide get the bundle and the model API only. A
`git fetch` from a remote inside the container is different: it goes through `:3128`, to a listed host only.

## What stays open in C

- **Allowed hosts still carry data:** a bot can still leak through an allowed host, for example a package
  registry that also accepts uploads. Keep the lists short and use read-only mirrors where they exist. Over
  CONNECT, methods are not visible without TLS inspection, which is a non-goal.
- **The key proxy spends:** through `:8080` the bot uses the real key for any call under the allowed path
  prefixes, file uploads included. The budget stop is the only limit there.
- **Subscription logins:** a CLI logged in with a subscription keeps its OAuth token in `/bot/home`. G1 then holds
  only with API keys, or with the proxy holding that token too, which needs testing per CLI before it is promised.
- **Cost per run:** a second container start and two network creates and removes, measured on the target host first.

## Rollout

1. **Proxy tool** in `catalog/tools/egress-proxy/`, with tests: allowlist matching, refused IP literals and
   private resolutions, key injection, no key in the logs, budget stop. It runs with nothing but Python.
2. **`team run`:** `network = egress` creates the networks and the proxy, passes the keys to the proxy instead of
   the bot, and removes both after the run, also after a timeout or crash (labels and `team doctor --clean`).
3. **Brief and flow:** the `Egress:` line, the lead's yes, and `flow` passing the list on.
4. **Trial on a rented Podman host:** the topology smoke test first, then one worker and one review bot for a week against
   `bridge` (tokens, time, failures). Then decide on making it the default for new teams.

## Tests that would prove it

- **Topology smoke test:** the two networks and a proxy on both start rootless on the target Podman; the bot
  on `-in` reaches `proxy` by name and nothing else.
- **Refusals:** in the bot, `curl https://example.org` and `curl https://203.0.113.7` fail, `getent hosts
  example.org` finds nothing, and a host on the list works through the proxy.
- **No real key:** `env` in the bot holds no real key, and a model call still succeeds.
- **Proxy guards:** a listed name that resolves to loopback or the metadata address (link-local) is refused, and
  so is a request to `:8080` with another path prefix.
- **Staged repository:** a worker run under egress gets its repo from `repo.bundle`, and its commits come back.
- **Clean-up:** after a crash or timeout, both networks and the proxy are gone.
- **Opt in:** with `bridge`, every existing run test passes unchanged.

## Sources

Read 2026-10-29/30. Unchecked: the gh-aw design beyond its README, and every claim not marked [S…].

- [S1] gh-aw-firewall README: <https://github.com/github/gh-aw-firewall>
- [S2] podman-network-create(1), `--internal`: <https://docs.podman.io/en/latest/markdown/podman-network-create.1.html>
- [S3] aardvark-dns v1.11.0 release notes: <https://github.com/containers/aardvark-dns/releases/tag/v1.11.0>
- [S4] Moby advisory GHSA-mq39-4gv4-mvpx (CVE-2024-29018, fixed in 26.0.0-rc3, 25.0.5, 23.0.11):
  <https://github.com/moby/moby/security/advisories/GHSA-mq39-4gv4-mvpx>
- [S5] podman-run(1), `--network` given more than once: <https://docs.podman.io/en/latest/markdown/podman-run.1.html>
- [S6] Claude Code environment variables, `ANTHROPIC_BASE_URL`: <https://code.claude.com/docs/en/env-vars>
- [S7] Codex config reference, `openai_base_url`: <https://learn.chatgpt.com/docs/config-file/config-reference>
- [S8] Podman v5.0.0 `container_validate.go`, "networks … can only be used with Bridge mode networking":
  <https://github.com/containers/podman/blob/v5.0.0/pkg/specgen/container_validate.go>

## Decisions for the owner

1. Is option C right, or is B enough at first (model-only bots, no installs)?
2. Which runners must work behind the key proxy first (Claude Code, Codex)? Must subscription logins work?
3. Should a host in a brief's `Egress:` line need the lead's yes, or the owner's?
