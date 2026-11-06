---
name: context7
kind: mcp
secrets:
install:
access: read-only, public library documentation
---

# Connector: Context7

Current documentation and code examples for libraries and frameworks, so a bot does not rely on outdated knowledge.

- Use it for: "how does library X do Y in its current version" before writing code against it.
- Read-only and public. For higher limits it takes an optional key; add a header and a secret if you have one.

```json
{"transport": "http", "url": "https://mcp.context7.com/mcp", "headers": {}}
```
