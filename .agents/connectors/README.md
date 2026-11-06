# Connectors

Example connectors (MCP servers) a bot can use. They are declared here, not active: nothing is installed, loaded or sent to a model until a bot lists one in its declaration (`connectors: github` in `team/bots/<id>.md`). An inactive connector costs nothing.

Turning one on (the lead does it, the human approves, because it is new access):

1. Add the name to the bot's `connectors:` line.
2. Store the keys it names under `secrets:` with `.agents/bin/secret set NAME` (in the human's own terminal).
3. The next run installs what it needs into the team image, writes the MCP config for that bot's CLI, and sends this file into the bot's sandbox as its usage guide.

Turning one off: remove it from the `connectors:` line. Your own connectors go to `team/connectors/<name>.md` in the same format: a header with `secrets`, `install` and `access`, a short usage guide, and one `json` block with the server (`"transport": "stdio"` with `command` and `args`, or `"transport": "http"` with `url` and `headers`; `${NAME}` stands for a key and is filled in by the bot's CLI at run time).

The commands and addresses below are examples; check them against each server's documentation before you activate one, and pin versions you have tested.
