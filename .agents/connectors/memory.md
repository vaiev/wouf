---
name: memory
kind: mcp
secrets:
install:
access: a small knowledge graph in the bot's own storage
---

# Connector: Memory

A persistent note store (entities, relations, observations) that lives in the bot's own storage and survives between tasks.

- Use it for: facts this bot needs again next time (a customer's preferences, a project's conventions).
- What the whole team should know goes to `out/knowledge/` instead, so WOUF can add it to the team's knowledge.

```json
{"transport": "stdio", "command": "npx", "args": ["-y", "@modelcontextprotocol/server-memory"], "env": {"MEMORY_FILE_PATH": "/bot/memory.json"}}
```
