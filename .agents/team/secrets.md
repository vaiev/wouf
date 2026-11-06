# Secrets

Names, where they are stored and who may use them. Never values. Values live in `.agents/bin/secret`'s store on the computer where the lead runs (macOS keychain, Linux Secret Service, `pass` with `WOUF_SECRET_BACKEND=pass`, or a 0600 file outside the repository). How to ask for and use them: `.agents/skills/secrets.md`.

| Name | What it is for | Used by | Scope | Stored on | Rotate by |
|---|---|---|---|---|---|
| | | | | | |

- **Scope**: the least access that does the job, e.g. "read-only, repo X".
- **Stored on**: the computer where the lead runs. Bots get a secret only for one run, as an environment variable; it is never stored in their sandbox.
- **Rotate by**: a date, or `on leak`.
