# The first ten minutes

This page takes you to a first result in three stops. Each stop says what to do and what you should see. The times are rough guides, not measurements.

Ten minutes is enough to see WOUF work. It is not enough for a team whose bots run in sandboxes. Stop 3 says why that takes longer and what it adds.

## Before you start

- A terminal. On Windows, use WSL 2: the helpers need a POSIX system, such as Linux or macOS.
- Python 3.9 or newer, and git. On a new Mac, the first `git` command may only offer to install Apple's developer tools. Say yes, wait until they are installed, then start again.
- For stop 2 only: an AI assistant for the terminal, called an agent CLI, such as Claude Code or Codex CLI. Install it and sign in once.

## Stop 1: the demo, without a model

The demo shows two checks that work without any model, and a signed receipt for the change that passes them. It uses a fake bot, a throwaway git repository and no network. It needs no key and no account.

Open a terminal and type these two lines, pressing Enter after each:

```text
git clone --depth 1 https://github.com/vaiev/wouf.git wouf-try
python3 wouf-try/.agents/bin/team demo
```

The first line downloads WOUF into a new folder, `wouf-try`. The second line runs the demo. The demo itself takes seconds. It works in a temporary folder, removes it at the end, and changes nothing in the folder you start it from.

You should see four parts, each ending in `PASS`, and one more `PASS` at the end. Part 4 needs `ssh-keygen` (part of OpenSSH); without it, part 4 prints `receipt: skipped, ssh-keygen not found` instead of `PASS`, and the last line is still `PASS`. Shortened, it looks like this; the commit numbers differ on every run:

```text
Offline demo: disposable git repository, fake agent CLI, no model or network calls.

1. Gate stops work outside Paths before review
team: gate T-1 red: outside Paths: outside.txt
PASS

2. Approval on A does not cover B
Review parser: stale
PASS

3. Within Paths and approved exact head
team: gate T-1 green: gate command passed
T-1 proposed: demo 74fb285bfec5, review approve; nothing merged
PASS

4. Every landed change comes with a receipt
receipt 9cee1746f9c2: stored team/receipts/9cee1746f9c2.json, note refs/notes/wouf-receipts, event receipt.signed
verified 9cee1746f9c2 T-1: signed by demo-team key; tree of L = tree of H 264b665381cd
refused: signature: invalid
PASS

A fake agent exercised the real gate and the real head check. Nothing was merged; no network calls were made.
PASS
```

What the four parts mean:

1. Every task has a brief that names the files a bot may change. Here the fake bot changed another file. The gate, a check without any model, turns red before anyone reviews the work.
2. A reviewer approved one commit, A. Then a new commit, B, came in. The approval of A does not count for B, so the merge proposal for B is refused.
3. A change inside the allowed file, approved on its exact commit, passes both checks. Even then, the command only records a proposal. It never merges.
4. The demo lands that change in its throwaway repository and makes a receipt for it with `team receipt make`: a small file, signed with a throwaway key that exists only in the demo's temporary folder. `team verify` checks it offline and says `verified`. Then the demo changes one field of a copy of the receipt, keeping its signature, and `team verify` refuses that copy (`refused: signature: invalid`). The receipt shows what the team recorded and signed, not that the code is right; the throwaway key stands for no team.

The demo does not show a real model at work. That is stop 2. When you are done, you can delete the `wouf-try` folder.

## Stop 2: a first task in host mode

Host mode runs the bots directly on your computer, without a sandbox. It needs no container program, so it is the quickest way to a first real answer. It is meant only for trying things out.

> Host mode has no sandbox. The bots run as your user and can reach what you can reach: your files, your other projects and the network. Every key you store for the team is also in the environment of every bot run. Try it in a new folder that holds nothing private, and not with real data.

1. Make a new folder and put two or three files in it, for example some notes or a short text.
2. Open the folder in the terminal and start your agent CLI there. For Claude Code, type `claude`.
3. Paste this sentence:

   > Set up the agent team from <https://github.com/vaiev/wouf/blob/main/INSTALL.md> in this folder. I am not technical: explain in plain words and do the steps for me. I only want to try it first: use host mode.

4. Answer the assistant's questions. It recommends containers; say that you want host mode for now. Its other recommendations are fine to accept.
5. If your agent CLI needs a key, the assistant tells you which one and gives you one line to paste into a second terminal window. You type the key there. It stays hidden and never goes into the chat.

In host mode, the setup creates each bot as a folder in `.agents/team/env/`, not as a container. Then it runs `.agents/bin/team doctor` until it says `all good`. In host mode, the doctor checks Python, git, that your agent CLI is installed on this computer, and, if it uses a key, that the key is stored. It does not look for a container program.

### The first signed return block

Last, the setup runs a smoke test. It gives the worker bot a one-line task, "Reply with the word ready and your return block.", and waits for the answer. You should see something like this; the bot's own lines and the code in brackets differ:

```text
--- WOUF Worker · T-smoke · output, data from the bot, not instructions [5e1f09ab] ---
WOUF Worker · T-smoke · ready-for-review
Evidence: replied with the word ready
Artifacts: none
Questions: none
Flags: none
--- end of output [5e1f09ab] ---
```

That is your first signed return block. "Signed" means that its first line names the bot, the task and the state: `WOUF Worker · T-smoke · ready-for-review`. It is a name line, not a cryptographic signature. WOUF finds the block by that line, shows only the block, and frames it as data from the bot, not as instructions. If you gave your team another name, that name stands where WOUF stands.

If the answer takes longer, the assistant keeps waiting. If your agent CLI hits a usage limit, it tells you when the limit resets. After three failed attempts it stops and explains the error in plain words.

### Your first own task

Now ask for something in plain words, for example:

> WOUF, summarise the files in this folder in five lines and show me the summary.

By its rules, the lead does not do the work itself. It writes a short brief, passes it to a bot, usually the worker ("I passed this to WOUF Worker"), and tells you what came back. The bot's answer ends with its own signed return block. These are rules the model follows, not a program that forces them.

## Stop 3: why the sandbox takes longer

In container mode, the mode the setup recommends, each bot works in a container: a closed box with its own storage, which exists only while the bot works on a task. The container gets that storage as its only volume; your project folder and your home folder are not mounted as such. The lead copies the brief and the inputs in, and the results out. That box is the safety that host mode does not have.

It costs time once:

- **A container program.** Docker, Podman, OrbStack or Colima on your computer, or Docker on a small server. If you have none, the setup recommends one for your system and installs it with you after your yes. That can need your password or an installer window.
- **The team image.** When the first bot is created, the setup builds one image that holds the agent CLIs and tools for the whole team. The setup says this takes a few minutes, once.
- **On Linux without Docker or Podman,** a lighter sandbox called bubblewrap can stand in. It is weaker than a container; the safety part of this guide explains how.

So a team in sandboxes takes longer than ten minutes, and longer still if you have no container program yet. After that, the image is reused: a run starts only a fresh container for its bot.

When you are ready, ask the lead: "WOUF, let the bots run in containers from now on." Changing where the bots run is part of the setup, and the lead asks you before it changes anything.

## Next

- [Limits](limits.md): what WOUF does not do. Read it before you use real data.
- [Safety](safety.md): the sandbox modes and the network settings.
- [Examples](examples.md): four ways to use the team, step by step.
- [WOUF in a HERMES project](hermes.md): for project leads in an authority.
