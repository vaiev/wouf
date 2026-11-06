---
name: playwright
kind: mcp
secrets:
install: npx -y playwright install --with-deps chromium
access: a headless browser inside the bot's sandbox; the internet the sandbox can reach
---

# Connector: Playwright

A real, headless browser: open pages, click, fill forms, take screenshots, read what a page shows.

- Use it for: testing a web app the brief names, checking a page after a change, reading a page that needs JavaScript.
- Screenshots and traces go to `out/`.
- Never: log in with the human's accounts, buy, submit forms that send something outward, unless the brief says so and the human approved it.

```json
{"transport": "stdio", "command": "npx", "args": ["-y", "@playwright/mcp@latest", "--headless", "--isolated"]}
```
