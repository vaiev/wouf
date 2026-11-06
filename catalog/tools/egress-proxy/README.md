# Egress proxy

A small forward proxy for bot containers: a bot reaches only the host names on an allowlist, and only at public
addresses. It is stage 1 of the egress design (`docs/specs/EGRESS-AND-KEY-PROXY.md`): the allowlist on `:3128`.
The key proxy (`:8080`) is not part of it, because the bots run on subscription logins (see the last section).

One file, `egress_proxy.py`, standard library only, Python 3.9+. It opens no TLS: the bot's TLS goes end to end.

## Run

```sh
python3 egress_proxy.py --bind '*' --port 3128 --allow-file allow.txt --log /run/egress/egress.log
```

The bot then gets `HTTPS_PROXY=http://<proxy>:3128` and `HTTP_PROXY=http://<proxy>:3128`. `--bind` defaults to
`127.0.0.1`; `'*'` listens on every IPv4 interface, which in a proxy container is what the bot's network reaches.
On start it prints `egress-proxy: listening on <bind>:<port>, <n> allowlist entries` to standard error.

| Option | Default | What it does |
| --- | --- | --- |
| `--allow-file FILE` | none | allowlist file, may repeat |
| `--allow HOST` | none | one allowlist entry, may repeat; added to the files |
| `--log FILE` | standard error | the request log, appended |
| `--bind ADDR`, `--port N` | `127.0.0.1`, `3128` | where it listens |
| `--max-conns N` | 64 | connections at once; more get `503` |
| `--idle-timeout S` | 60 | seconds without a byte before a connection is closed; also the limit for the whole request head |
| `--connect-timeout S` | 10 | seconds to connect upstream |
| `--max-line N` | 8192 | bytes in the request line and in each header line |
| `--max-header N` | 32768 | bytes in all header lines (and at most 100 headers) |

It writes nothing but its log file: it can run with a read-only root and `--log` on a mounted run folder.

`--unix PATH` listens on a Unix socket at `PATH` instead of TCP (`--bind` and `--port` are then unused). The socket
is created with mode `0600`; a path that already exists, a stale socket included, stops the start with exit code 2.
The allowlist, the refusals and the log are the same as on TCP. The socket file is removed when the proxy stops.

`--relay PATH` runs a relay instead of a proxy: it listens on `--bind`/`--port` and pipes each connection's bytes
both ways to the Unix socket at `PATH`, so a sandbox without a network, with that socket bind-mounted in, reaches the
proxy on a loopback port. It parses nothing and logs nothing; `--max-conns` (more are closed at once),
`--idle-timeout` and `--connect-timeout` apply, and the proxy behind it does every check.

## Allowlist file

One entry per line; `#` starts a comment; blank lines are skipped; case and a trailing dot do not matter.

```text
# exact host
pypi.org
# every name under a domain, not the domain itself
.pythonhosted.org
```

- `pypi.org` matches `pypi.org` only: not `files.pypi.org`, not `evil-pypi.org`.
- `.pypi.org` matches `files.pypi.org` and `a.b.pypi.org`, not `pypi.org` itself; list both if both are needed.
- An entry that is not a host name or `.suffix` (an address, a port, a `*` pattern, a URL) stops the start with
  exit code 2, so a typo never widens the list.
- The list is read once at start, from the files and options only, never from a request. An empty list refuses
  everything.

## What passes and what is refused

A request passes only if every check holds, in this order. Each refusal is answered with the status shown and logged
with the reason.

| Request | Refused when | Status, log reason |
| --- | --- | --- |
| `CONNECT host:443` | the host is an IP literal in any form (dotted, IPv6, integer, octal, hex) | `403 ip-literal` |
| | the port is not exactly `443` | `403 port` |
| | the host is not on the allowlist | `403 not-allowed` |
| | any address the name resolves to is not public: private, loopback, link-local (the `169.254` metadata address), CGNAT (`100.64/10`), multicast, broadcast, unspecified, reserved, documentation, IPv6 unique-local and site-local, IPv4-mapped, NAT64, 6to4 and Teredo forms of these | `403 address-…` |
| | the name does not resolve, or no address answers | `502 resolve`, `502 upstream` |
| `GET` / `HEAD http://host/…` | the same host rules, with port `80` instead of `443` | as above |
| | the request has a body, user info in the URL, or is not an absolute `http://` URL | `400` |
| any other method | always (`POST`, `PUT`, `DELETE`, `OPTIONS`, …) | `405 method` |
| any request | a request line or header over the limits, more than 100 headers | `414`, `431` |
| | malformed header name or value | `400 bad-header` |
| | no full request head within the idle timeout | `408 timeout` |
| | garbage, a bad version, a non-ASCII request | `400`, `505` |
| | the proxy already holds `--max-conns` connections | `503 busy` |

The name is resolved once, all its answers are checked, and the proxy connects to exactly the checked address, so a
second lookup cannot rebind the name to an inner address. Plain HTTP is sent upstream as one request with
`Connection: close`, without `Proxy-*` and hop-by-hop headers and with `Host` set from the URL; bytes the client
sends after it are not forwarded.

## Log

One line per connection, written when it ends:

```text
2026-11-02T10:15:04Z host=pypi.org port=443 result=allowed reason=ok bytes_out=2210 bytes_in=81234
2026-11-02T10:15:05Z host=evil-pypi.org port=443 result=refused reason=not-allowed bytes_out=0 bytes_in=0
```

Time is UTC; `bytes_out` is from the bot, `bytes_in` to it. No path, query, header or body is logged; a host that
is not a plain host name is logged as `-`, so a request cannot write its own log line.

## Tests

```sh
python3 -m unittest discover -s catalog/tools/egress-proxy
```

They use loopback only: names resolve through a fake resolver, and the proxy's connect goes to a local server
while the test records which checked address it was asked for.

## What it does not do

- It does not stop a leak through an allowed host (a gist on an allowed code host); keep the lists short.
- Over `CONNECT` it sees only the host and port, not the method or path; there is no TLS inspection.
- It does not make the networks or the container (`team run`), and it holds no keys.

## What a subscription-login CLI still needs

A CLI logged in with a subscription keeps its token in the bot's home, so the token stays in the container; the
proxy limits where it can go, not who holds it. For such a CLI to work behind the proxy:

- **Proxy support:** the CLI must honour `HTTPS_PROXY` for every call, and `NO_PROXY` must not name outside hosts.
  A CLI that ignores the variable fails closed on the internal network of the design, which has no other route.
- **Its hosts on the list:** the model API host and the login or token-refresh host of that CLI, for example
  `api.anthropic.com` for Claude Code. The exact hosts per CLI are unchecked here: start the CLI once with an empty
  list and read the refused host names in the log, then add only what a run needs.
- **Refused extras:** update checks, telemetry and error reporting hosts may be refused; the CLI should run without
  them, which is the same trial.
- **Keys:** the token is not moved out of the container until the key proxy (stage 2) can hold it, which needs a
  test per CLI.
