# Examples

Four ways to use the team, step by step. Each starts small and can grow. They use only what the pack ships: the lead, the core bots, the example bots, briefs, the board, the schedule and the settings in `.agents/team/config.ini`. Names, folders and tasks below are made up.

## Project management for a non-technical project

Say you organise a community event: a venue, a few suppliers, invitations and a budget in a spreadsheet.

1. Make a folder for the project, for example `summer-fair`, put the documents you already have in it, and set up the team there as described in Getting started.
2. Keep `autonomy = ask`, the default. The team's rules tell the bots that routine, reversible work such as reading, drafting and running tests goes ahead, and that every decision beyond that comes to you. These are instructions the models follow, not a lock: you stay the one who acts on anything that matters.
3. Tell the lead what you want in plain words: "WOUF, make a task list for the summer fair from the notes in this folder, and show it to me before anything else."
4. The lead writes a brief and passes it to the worker. You get the result after a second bot has reviewed it. Tasks and their state are on the board, a file you can open.
5. Ask for drafts, not actions: "WOUF, draft the invitation letter and show it to me before anything is sent." The rules tell the team to leave sending to you, but nothing in WOUF technically stops a bot that has the means to send. Keep it your step: give the bots no mail account or other way to send, and send yourself.
6. Add reminders: "WOUF, remind me every Monday at 8 to check the supplier answers." Reminders come up when you open a session, unless you set up a scheduler on your computer.
7. Grow it when needed: "WOUF, we need a writer bot." The writer is one of the ready example bots.

## A coding co-worker

Say you maintain a small web application in a git repository with a test suite.

1. Set up the team in the repository folder. Choose container mode if you have Docker or Podman.
2. Tell the lead which command runs your tests. It goes into `gate` in the `[flow]` section, so every change is tested before review, without keys and, in container mode, in a sandbox.
3. Ask for a change: "WOUF, fix the crash when the upload is empty, and run it as a flow."
4. The lead writes a brief with `Class:` and `Paths:`. `team flow` runs the worker, then the gate: a change outside the brief's paths or a failing test sends the work back once.
5. A review bot checks the result in a fresh context and approves the exact commit, or asks for changes, which go back to the worker.
6. You get a branch and the review. `team flow` and `team merge propose` never merge, and the team's rules say code reaches main only with the review, green CI and your yes. Those rules are instructions, not branch protection: to enforce them, set up protection for your main branch and required checks in your repository host, and do the merge yourself.
7. Grow it: a second reviewer for `security` work, a different CLI for the reviewer than for the worker, or a tester bot with a browser connector once you say yes.

## In a company

Say a team of five wants a shared agent team for internal documentation and small tools.

1. Decide who owns the setup: one person or a small group who answers the questions and holds the keys.
2. Run the bots on a VM or server you control, through an SSH alias in `docker_host`, so they do not run on a laptop.
3. Keep `autonomy = ask` at first. Move to `auto` only for reversible work, once you trust the results. Under both settings the team's rules say irreversible, outward, paid and new-access steps wait for a person; these are instructions to the models, so the company enforces them by what it gives the bots: no accounts, keys or access they could use for such a step on their own.
4. Set `network = egress` and list the hosts the work needs, after a trial run that shows which hosts the agent CLI really uses. Each run gets only its own CLI's model hosts on top of that list.
5. Store keys with `.agents/bin/secret`, typed by the key owner in their own terminal. Give each bot only the connectors it needs.
6. Write the company's own rules for the team into its knowledge and bot files, and keep sensitive material out of the folder unless the setup is approved for it.
7. Review the board, the decisions and the logs regularly; they are files. To move or share the team, `team export` leaves keys out, and data too unless you ask for it.
8. Grow it: more bots for recurring work, more runners, token budgets per day or per run in the `[budget]` section.

## An authority, such as the Swiss federal administration

This is how you can set it up. It is a starting point for the organisation's own assessment, not a compliance, legal or sovereignty guarantee. Data protection under the Swiss nDSG and sovereignty are goals the organisation sets and checks itself; WOUF does not ensure them on its own. Every choice below is the organisation's to make and to review.

1. **Who decides what information may be used.** The organisation decides which information the team may see at all, for example only public or internal documents and no personal data, together with its data protection and information security officers. WOUF has no classification of its own: what is in the folder and the briefs is what the bots can work with.
2. **Where the information and the model processing reside.** The organisation chooses where the folder and the bots run, for example on its own server reached through `docker_host`, and which model processes the text. A cloud agent CLI works through its provider's model API over the network, so the organisation has to assess what that provider processes and where. A local model keeps processing on the organisation's own systems, but the pack ships only an empty `local` runner to fill in, and the organisation has to verify that setup itself.
3. **Which network destinations are allowed.** Use container mode, never `host`. Choose `network = none` for a model inside the container, or `network = egress` with an allowlist of the destinations the organisation approved. The allowlist is not the whole list: each run also gets the model API and login hosts of its own agent CLI, added automatically (built-in for the CLIs the pack knows, or the hosts set in `needs.<runner>` in the `[egress]` section). The organisation has to assess and approve those hosts too, and can set `needs.<runner>` to the hosts it approved; the list of each run is shown at its start. The default `bridge` is open and is not suitable here. Note that the egress proxy refuses private addresses, so it cannot reach a model server inside a private network; the organisation needs another approved path for that.
4. **Keys.** Keys are typed by an authorised person in their own terminal with `.agents/bin/secret`. Remember that a bot given a key can still leak it; masking only prevents accidents.
5. **Autonomy.** Keep `autonomy = ask`. The team's rules say routine, reversible work in the sandbox goes ahead without asking, every other decision goes to a person, and sending, publishing, deleting outside a sandbox, new access and anything that costs money go to a person in every setting. These rules are instructions to the models, not technical controls. The organisation enforces them with its own means: the accounts, keys, network destinations and repository permissions it grants, and the people who carry out these steps.
6. **Review the organisation must arrange.** The review bot checks results against briefs; it does not check legal or data protection requirements. The organisation arranges its own review of the setup, the chosen model provider, the network rules and the stored logs, and repeats it when something changes.
7. **Start small and extend.** Begin with one low-risk use, such as drafting summaries of public documents, and extend only after the review, one step at a time.
