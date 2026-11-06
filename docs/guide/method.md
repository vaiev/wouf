# Method

WOUF works the way a small company works with a team. This part explains who does what, and which steps are checked by code and which are rules the models are asked to follow.

## One lead

You talk to one member only: the lead, called WOUF unless you rename the team. The lead is the agent CLI session you already use, started in your project folder.

The lead talks to you, writes briefs, hands them out, relays questions and reports back. It does not do the work, review it or decide alone. Bots never talk to you directly; they ask the lead, for example "WOUF, I need the logo file from the user", and the lead collects such questions and asks you once.

This is a rule the models are asked to follow, not something a program can force on a model.

## Roles

Each member has one role, written down in `.agents/roles/`:

- **Lead**: talks to you, briefs, dispatches and reports.
- **Worker**: does the briefed work, and only that, in its own sandbox.
- **Review**: checks one result against its brief and approves or asks for changes. It never fixes what it reviews.
- **Decide**: makes a hard call once, with reasons, when the brief and the rules do not settle it.
- **Specialist**: a bot for one domain, such as the example research, writer, tester and ops bots.

Nobody grades their own work. Flows also use helper bots: a scout that reads the code first, a bugfixer, and a planner that turns a large goal into a plan and draft tasks for the lead to read.

## The brief

Every piece of work starts from a brief. A brief states the objective, the acceptance criteria, the paths the work may touch, what is in and out of scope, the inputs and the expected output. `team brief new` prints an empty brief with the next free task id.

The brief's `Class:` line picks the bot, the tier and the number of reviews: for example `mechanical` runs on the light tier with one review, and `security` runs on the strong tier with two reviews.

The brief's `Paths:` line is checked by code. Before any review, a pre-gate without any model checks the branch: a changed file outside those paths turns the gate red, and so do failing tests when you configured a gate command. The gate command gets no keys. In container and `bwrap` mode it runs in a sandbox; in `host` mode it runs in a throwaway clone on your computer, with no sandbox. A red gate sends the work back to the worker once; a second red gate blocks the task.

## Fresh, independent review

A second bot checks every result, in a fresh context and its own sandbox, so it does not inherit the worker's reasoning. It checks the result against the brief, not against the worker's summary.

The review role asks an approval to name the exact commit it checked. `team flow` accepts an approval only when the commit it names is the head of the task branch. It accepts the full commit id or a shortened one of at least 7 characters that the head's id starts with, as git shows short ids. An approval of another commit, a commit id shorter than 7 characters or a missing commit counts as no approval. Any new commit needs a new review.

## Decide

Some questions do not follow from the brief and the rules: two reasonable options, rules in conflict, or high risk. The lead then asks the decide bot, which picks one option, names the remaining risk and hands the decision back.

How far the team may decide without you is a setting, `autonomy` in `.agents/team/config.ini`:

- `ask`, the default: decisions go to you.
- `auto`: the lead decides reversible things inside the rules with the decide bot, records them in `team/decisions.md` and tells you in its next report.

In both settings some things always wait for your yes: sending, publishing, deploying, purchasing, deleting or overwriting outside a sandbox, merging to main, new access such as a new key or host, and anything that costs money. These limits are rules for the models. Merging and publishing are your own steps.

## Speak up

Every member says so, unasked, when something is wrong, missing, risky or could be better: bots under `Flags` in their return block, the lead to you.

## A whole task on its own

`team flow <task>` takes a brief through worker, pre-gate, review and up to two rounds of rework. It ends `ready` with an approval on the exact head, `blocked`, `limited` or with one question for you. If a run hits a usage limit, the flow waits, or switches once to another CLI when you configured a fallback.

A goal too big for one brief goes through four phases: INIT, CONCEPT, REALIZE and VERIFY. Leaving a phase needs an explicit yes, and with `autonomy = ask` your yes first.
