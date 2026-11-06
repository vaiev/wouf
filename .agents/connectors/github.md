---
name: github
kind: mcp
secrets: GITHUB_TOKEN
install:
access: what the token allows: issues, pull requests, code, actions
---

# Connector: GitHub

GitHub's hosted MCP server: read and write issues, pull requests, files and workflow runs.

- Use it for: reading issues and pull requests, commenting on a pull request the brief names, checking CI.
- Key: a fine-grained token limited to the repositories and permissions the bot needs (read-only where possible).
- Never: push to main, merge, change settings or delete anything. Those go through WOUF and the human.

```json
{"transport": "http", "url": "https://api.githubcopilot.com/mcp/", "headers": {"Authorization": "Bearer ${GITHUB_TOKEN}"}}
```
