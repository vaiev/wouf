# Toolkit

Skills the team wrote or adopted, and connectors it turned on, with their status. The lifecycle is in `.agents/skills/toolkit.md`. A tool that is not `active` or `trial` here is not loaded.

| Name | Kind | Status | Used by | Purpose | Configured in | Secrets | Since | Last used |
|---|---|---|---|---|---|---|---|---|
| | | | | | | | | |

- **Kind**: `skill` (a file in `team/skills/` or the pack) or `mcp` (a server).
- **Status**: `proposed` → `trial` → `active` → `disabled` or `retired`.
  - `proposed`: written, not yet reviewed.
  - `trial`: reviewed, used for named tasks only.
  - `active`: part of the normal toolkit.
  - `disabled`: switched off, can be switched on again (MCP servers not needed right now).
  - `retired`: its purpose is done; the file is removed, git keeps the history.
- **Configured in**: where the CLI reads it, e.g. `.mcp.json` (Claude Code), `.codex/config.toml` (Codex CLI), `.gemini/settings.json` (Gemini CLI).
- **Secrets**: names from `team/secrets.md`, never values.
