# Skill: Review and merge

## When to use

When a worker returns `ready-for-review` or a flow ends `ready`. Every result is reviewed, in its flow or by hand (step 1); code reaches main only through this skill.

## Required inputs and access

- The brief and the worker's return block; for code the branch `<team>/<task>` and the repository's CI
- The review bot

Approval: the review needs none. Merging to main, publishing or sending the result needs the human's yes.

## Sequence of work

1. A `ready <branch> <commit>` flow of a class with reviews was reviewed on that head already: go to step 4. A class with `reviews = 0` (`decision`, `analysis`, `plan`) ends `ready` unreviewed, which is no APPROVE: review its result by hand before it is sent or relied on, and never merge code whose proposal says `review none`. Otherwise: `.agents/bin/team run review <task> --brief <brief> --repo <team>/<task>` (documents: `--input <files>`), the brief extended by "Review this result against the acceptance criteria above."
2. `CHANGES`: the blocking items go back to the worker as a brief section, run again with `--repo <team>/<task>` so its commits continue the branch, then review again. After three rounds ask the human.
3. `APPROVE <sha>` counts only for the branch head (`git rev-parse <team>/<task>`); a newer commit needs a new review.
4. `team merge propose <task> --head <sha>` records head, branch and verdict (never merges), after checking that the branch still points at that head and every required review approved it. Without a flow it takes the last `team run review <task>` of step 1 when it approved the current head of its branch and the task's class needs one review; a class with two (`security`) needs a flow. Push the branch or open a pull request: CI must be green on that commit. A repository with no CI: tell the human so; the review's test run is then the only check.
5. Ask the human: what changed, verdict, CI, residual risk; `team merge mark <task> --head <sha>` flags it for them. On yes: merge the way the repository merges, never by force.
6. `team task done <task>`; one log line.

## How to validate the result

- The APPROVE names the merged commit; CI is green on it; the human said yes.
- The reviewer was not the worker and did not change the result.

## What to return

- The merge or the delivered result, the task `done`; if blocked: the blocking items and who acts next

## What requires approval

Pushing, opening a pull request, merging, publishing or sending. A standing yes ("merge small docs fixes after APPROVE and green CI") is a `team decision add … --authority owner`; it never skips the APPROVE or CI.
