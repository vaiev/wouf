# Local runner

This tool helps a WOUF team use a model server on the user's computer without a cloud subscription. It needs Python 3.9 or newer and only the standard library. It checks OpenAI-compatible servers such as Ollama, LM Studio, llama.cpp server and vLLM. It does not install a server or model.

```sh
python3 catalog/tools/local-runner/local_runner.py check http://localhost:11434
python3 catalog/tools/local-runner/local_runner.py smoke http://localhost:11434 --model MODEL
python3 catalog/tools/local-runner/local_runner.py block http://localhost:11434 --model MODEL --light MODEL
```

`check` lists models from `/v1/models`, falling back to Ollama's `/api/tags`. If the server needs a key, set it in an environment variable and add `--key-env VARIABLE` to `check`; no key is sent by default. `smoke` sends one short chat completion and reports the answer length and elapsed time. `block` prints a `[runner.local]` section; `--light` is optional and defaults to the strong model. `--cli codex` is optional and currently the only CLI choice.

## Ollama setup

1. Install Ollama for your computer and start its server.
2. In a terminal, pull a model that supports tool use, for example `ollama pull MODEL`, replacing `MODEL` with the model's name.
3. From the repository folder, run `python3 catalog/tools/local-runner/local_runner.py check http://localhost:11434`. Optionally run `smoke` as above, replacing `MODEL` with the name printed by `check`.
4. Run `python3 catalog/tools/local-runner/local_runner.py block http://localhost:11434 --model MODEL`, with that name. Copy the printed section into `team/config.ini`, replacing the existing `[runner.local]` section.
5. Run `team runners` to inspect the runner and select it for a bot in the team configuration.

The printed block uses Codex CLI's open-source mode, with `--oss --local-provider lmstudio` and `CODEX_OSS_BASE_URL` pointing to the supplied server. The flags were checked with `codex --help` and `codex exec --help` for **codex-cli 0.166.0**. Codex uses the Responses API as well as the models endpoint; a server that only supports chat completions can pass `smoke` but cannot run this CLI. Server and model tool-calling support are also needed. The container command uses Docker's `host.docker.internal`; replace it with `host.containers.internal` for Podman. The server must listen on an interface reachable from the container, and the host alias must resolve there; engine and host setup may be needed.

With `network = egress`, the container host name must be listed in `[egress] allow`. The current catalog egress proxy also rejects private and loopback addresses, so it cannot presently reach a server on this computer even with that allowlist entry. With `mode = bwrap` and `network = none`, a server outside the sandbox is not reachable.

The generated section contains the requested `auth = none`. The current pack interprets any nonempty `auth` as a secret name, including `none`; until the pack supports that value, change this line to `auth =` after pasting. The pack appends the chosen tier and its model using `model_flag`.

Local models can be weaker than cloud models. Tool use requires a model that supports it, and speed depends on the computer. This tool does not wire the runner into the default config or add a doctor check.

## Tests

```sh
python3 -m unittest discover -s catalog/tools/local-runner
```

The tests use a fake server on loopback; no real model is needed.
