# WOUF in a HERMES project

HERMES is a project management method that the Swiss federal government develops and makes freely available as an open standard (eCH-0054). This page is for a project lead who runs a project with HERMES and wants a WOUF team to do part of the work. It puts the two side by side: phases, roles and documents.

## A comparison, not a conformity claim

- This page compares. It does not say that WOUF conforms to HERMES, is certified for it, or produces HERMES results on its own. WOUF does not implement HERMES.
- The HERMES facts here come from HERMES Online for HERMES 2022, read on 2026-11-06 (see Sources at the end). HERMES can change; check the current version.
- Whether and how a project uses WOUF is the organisation's decision. For the setup itself, start from [An authority, such as the Swiss federal administration](examples.md#an-authority-such-as-the-swiss-federal-administration). It is a starting point for the organisation's own assessment, not a compliance, legal or sovereignty guarantee.

## Two levels

HERMES describes a whole project, run by people. In the traditional approach its phases are Initiation, Concept, Implementation, Deployment and Closure; in the agile approach they are Initiation, Execution and Closure. Each phase begins and ends with a milestone, a quality gate where results and the way forward are decided. With each phase release, the sponsor releases the money, people and infrastructure for the next phase.

WOUF's `team goal` describes one piece of agent work: from a goal in plain words to reviewed changes on branches. It has four phases, INIT, CONCEPT, REALIZE and VERIFY. Each phase change is recorded as an event in the team's event log.

The names are close; the size is not. A WOUF goal is a work package inside a HERMES project, not the project. Use it for work inside a HERMES phase, for example drafts during Concept or building during Implementation.

## Phases side by side

The commands below run in the team's folder as `.agents/bin/team …`; usually the lead runs them. `<task>` stands for the goal's task id, such as `T-12`, which `team goal new` prints in the draft brief. A command that leaves the phase before needs `--yes`.

- **INIT and Initiation.** `team goal new "goal"` drafts one brief. A classifier without any model proposes the task class, the files the work may change, the checks to run and, for security work, a threat model. The lead fills in what the draft marks `TO FILL`; the next phase refuses a brief with gaps. HERMES Initiation works out the project's foundations and possible solutions, compares and assesses them, and chooses the approach. INIT does none of that: it prepares one brief.
- **CONCEPT and Concept.** `team goal concept <task> --yes` lets a scout and the planner bot write a plan: packages, each with a draft brief, grouped in waves. No package is started and nothing is committed in this phase. HERMES Concept fleshes out the chosen solution and checks at its end whether the project should go ahead. The WOUF plan can be an input to that work; it does not decide whether to go ahead.
- **REALIZE and Implementation.** `team goal realize <task> --wave 1 --yes` runs the first wave of the plan; later waves run with `--wave 2` and so on, without `--yes`, because the goal stays in REALIZE. Its packages run at the same time, each with its own worker, through the check without a model, the review and rework; if their files overlap, none of them starts. HERMES Implementation builds and tests the product or system and prepares the deployment. REALIZE delivers reviewed changes on branches; it does not merge or deploy them.
- **VERIFY and acceptance.** `team goal verify <task> --yes` lists, for each package, the review verdict, the commit it approved and the merge proposal. It is an inspection, not a pass. `team goal complete <task> --yes` ends the goal. HERMES has no phase of this name. It decides acceptance at the end of Deployment, and calls the acceptance report a legally binding document. VERIFY is not an acceptance.
- **No WOUF phase for Deployment or Closure.** WOUF does not deploy, train users, hand over or close a project. By the team's rules, merging, publishing and deploying always go through a person.

## Phase changes and milestones

- WOUF: leaving a phase needs `--yes` on the command; without it the command stops. With `autonomy = ask`, the default, its message tells the lead to get a person's yes first. No phase can be skipped. The person's yes is a rule for the lead: the program only sees the `--yes`.
- HERMES: milestones are states that always follow a decision. The decisions at the steering level, such as phase releases and project closure, are the sponsor's.
- In practice: keep `autonomy = ask`, and agree in the project who gives the yes for each WOUF phase change. The lead can record that yes as a decision that waits for a person, `team decision add "Leave CONCEPT for REALIZE" --options yes no --why "<reason>" --risk "<risk>" --authority owner --task <task>`, and run `team decision approve D-<n>` once the person has said yes. It then appears in `.agents/team/decisions.md`.

## Roles

- **Project sponsor.** Has overall responsibility and steers the project. WOUF has no such role; the sponsor stays a person outside the team.
- **Project manager.** Responsible for the smooth progress of the project, and gives work orders to the project staff. The WOUF lead does a small part of this for the agent work: it writes briefs, hands them to bots, keeps the board and collects the bots' questions. The lead is a model in an agent CLI; it cannot carry the project manager's responsibility. The project manager stays a person, and is the one who talks to the lead.
- **User representative.** Responsible for the specialist design of the solution. WOUF has no counterpart. Bots ask the lead, and the lead asks a person.
- **Quality and risk manager.** Reports directly to the sponsor. WOUF has no counterpart. The review bot checks a result against its brief only. It does not check legal, data protection or HERMES requirements.
- **Execution roles, such as developers.** The worker, the scout, the planner and the other bots do execution work: each from a written brief, in its own sandbox in container or bubblewrap mode.
- **The decide bot.** Has no HERMES counterpart. It takes a hard call that the lead hands it. By its role, with `autonomy = ask` it only recommends, and a person decides. Decisions at HERMES milestones stay with the roles HERMES names.

## WOUF files as inputs to HERMES results

In HERMES, a result is either a document or a state, such as a milestone. WOUF keeps its state in plain files you can open. None of them is a HERMES result. Some can be inputs to one:

- **Brief** (`.agents/team/env/lead/<task>.md`): objective, acceptance criteria, scope, out of scope, inputs, constraints and expected output. It is close to a HERMES **work order**, which holds objectives, results, boundaries, prerequisites, activities and quality assurance. A brief directs a bot; work orders to people stay with the project manager.
- **Board** (`.agents/team/board.md`): each task with its owner, status, next gate, blocked reason and date, and the open questions for you. An input to the **project status report**. The board has no costs, forecasts or risks.
- **Decisions** (`.agents/team/decisions.md`): one entry per decision, with date, task, options, reasons, risk, who decided and the next step. An input to the HERMES **lists of project decisions**. The decisions HERMES lists at the steering and management level are taken by people.
- **Review verdict**: `APPROVE` with the exact commit the review bot checked, or `CHANGES` with what blocks. An input to a **review report**, which records review findings and the decisions about them. It is not an **acceptance report**.
- **Event log and logs**: the event log records tasks, questions, decisions, runs, merge proposals and goal phases; each member writes one log line per run, searchable with `.agents/bin/log`. Evidence of who did what and when, for status reports and lessons learned.

To hand these files on, `.agents/bin/team export --with-data` adds the readable files (board, decisions, schedule, knowledge and logs) to the export. Keys are never part of it.

## What stays outside WOUF

- The HERMES documents themselves: the project initiation order, the study, the project management plan, phase reports, change requests, the test concept, the acceptance report and the final project evaluation. A bot can draft text for them when a brief asks for it; the draft is for people to check and own.
- Every decision at a milestone: phase releases, acceptance and closure.
- Data protection, information security and what information the team may see: the organisation decides these. See [the authority example](examples.md#an-authority-such-as-the-swiss-federal-administration) and [Limits](limits.md).

## Sources

HERMES, read on 2026-11-06:

- Federal Chancellery, HERMES project management method: <https://www.bk.admin.ch/bk/de/home/digitale-transformation-ikt-lenkung/hermes_projektmanagementmethode.html>
- HERMES Online, phases: <https://www.hermes.admin.ch/en/project-management/phases.html>, with the pages of each phase (initiation, concept, implementation, deployment, execution, closure)
- HERMES Online, roles: <https://www.hermes.admin.ch/en/project-management/roles.html>
- HERMES Online, outcomes: <https://www.hermes.admin.ch/en/project-management/outcomes.html>, with the pages for work order, project status report, list of steering project decisions, list of management project decisions, review report and acceptance report

WOUF: every statement about WOUF on this page is listed with its source in the code and its test in [Sources](SOURCES.md), under "WOUF in a HERMES project".
