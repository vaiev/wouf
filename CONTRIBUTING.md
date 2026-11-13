# Contributing

Thanks for helping. The pack is small on purpose: every file is something an agent loads, so every line costs context. A good change makes the pack clearer, shorter or safer. The guide says the same in [docs/guide/contribute.md](docs/guide/contribute.md).

## What helps most

- **A real failure:** a run where an agent followed the pack and still did the wrong thing. Open an issue with the role, the skill, what happened and what you expected. Real failures drive the quality gate; imagined ones do not.
- **An example bot or connector** you use (`.agents/bots/`, `.agents/connectors/`), with private details removed.
- **Fixes for contradictions** between files, and for runner commands in `.agents/team/config.ini` that no longer match an agent CLI.
- **Reviews:** read a pull request against what it claims, run the tests yourself, and say what is missing, risky or could be simpler.

A security issue goes in private, never in a public issue: see [SECURITY.md](SECURITY.md).

## How a change goes in

The project changes itself with the method it describes: WOUF is built by an agent team under the owner's lead. The lead merges pull requests in the private development repository itself, under the owner's standing OK, once a reviewer approved the exact head commit and CI is green; the owner sets the direction and decides what goes public. Whether a person or a bot writes the change, it takes the same four steps:

1. **Brief.** One short text says what to change, which paths, and how to tell it is done.
2. **Worker.** The change is made on its own branch, test first, inside the paths of the brief.
3. **Review.** Someone who did not write the change reads it in a fresh context and approves the exact commit, or asks for changes. A new commit needs a new approval. CI must be green.
4. **The owner's standing OK.** The lead merges into `main` under the owner's standing OK, and the owner decides what goes public.

In WOUF, `team flow` runs steps 2 and 3 and ends `ready` only with an approval of the head commit; `team merge propose` then records it as a proposal and merges nothing: the pack itself never merges. In this project the lead does step 4 under the owner's standing OK. This is a working rule for people and bots: the pack does not enforce it; only the permissions and branch protection of the repository host can.

For a pull request from outside the team: work on a branch, open a pull request against `main`, and describe the behaviour change and the test you ran. It then goes through review and the owner's yes like any other change. The public history is rebuilt for each release, so the maintainer carries an accepted pull request into the next release.

## Rules for changes

- Test first: add or change a test in `tests/`, see it fail, then change the pack.
- A new skill copies `.agents/skills/_template.md`, keeps its six sections in order and is named in `.agents/roles/lead.md`. Prefer extending a skill over adding one.
- Stay generic: no company names, host names, IP addresses outside the documentation ranges, account names or keys.
- Route models by tier (`strong`, `mid`, `light`); provider and model names belong in `.agents/team/config.ini`, as aliases or empty for the CLI default. Model versions age; the tests reject them in Markdown.
- Write plainly: short sentences, facts and trade-offs, no buzzwords.

## Checks

```bash
python3 scripts/test-parallel.py
```

This runs the whole suite in parallel processes, as CI does. `python3 -m unittest discover -s tests -v` runs it in one process; `python3 -m unittest discover -s tests -p 'test_pack.py'` runs one module. The container tests run when Docker or Podman is reachable. CI also runs markdownlint, an offline link check and actionlint.

## License

By contributing, you agree that your contributions are licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE).
