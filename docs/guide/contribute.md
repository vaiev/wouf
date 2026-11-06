# Contribute

The pack is small on purpose: every file is something an agent loads, so every line costs context. A good change makes the pack clearer, shorter or safer. The repository is public at [github.com/vaiev/wouf](https://github.com/vaiev/wouf).

## Ways to help

- **Report a real failure.** A run where an agent followed the pack and still did the wrong thing helps most. Open an issue with the role, the skill, what happened and what you expected. Real failures drive the quality gate; imagined ones do not.
- **Share an example bot or connector** you use, with private details removed.
- **Fix contradictions** between files, and runner commands in `.agents/team/config.ini` that no longer match an agent CLI.
- **Report a security issue privately**, as described below, never in a public issue.

## Rules for changes

- Test first: add or change a test in `tests/`, see it fail, then change the pack.
- A new skill copies `.agents/skills/_template.md`, keeps its six sections in order and is named in `.agents/roles/lead.md`. Prefer extending a skill over adding one.
- Stay generic: no company names, host names, IP addresses outside the documentation ranges, account names or keys.
- Route models by tier; provider and model names belong in `.agents/team/config.ini`.
- Write plainly: short sentences, facts and trade-offs, no buzzwords.

## Checks

```bash
python3 -m unittest discover -s tests -v
```

The container tests run when Docker or Podman is reachable. CI also runs markdownlint, an offline link check and actionlint.

## Review

Work on a branch and open a pull request against `main`. Describe the behaviour change and the test you ran. The project's rule is that a pull request merges only after review, and its own changes follow the same method this guide describes: an independent review that approves the exact commit, green CI and the owner's yes. This is a working rule for people and bots, not something the pack enforces.

You can help by reviewing too: read a pull request against what it claims, run the tests yourself, and say what is missing, risky or could be simpler.

## Security issues

Use GitHub private vulnerability reporting for the repository. Until that is enabled, contact the organisation owners at [github.com/vaiev](https://github.com/vaiev). Do not include a private key, password, token, host address, or a log that might contain one. If a secret was already pasted somewhere public, revoke it first.

## License

Contributions are licensed under the Apache License, Version 2.0.
