---
name: triage_nightly
args: branch
allowed_tools: Read, Grep, Glob, Bash(git log *), Bash(git show *), Bash(git diff *), Bash(git merge-base *), Bash(git rev-parse *), Bash(git fetch origin *), mcp__mina-harness__ci_builds, mcp__mina-harness__ci_build, mcp__mina-harness__ci_job_log
disallowed_tools: Edit, Write, NotebookEdit, WebFetch, WebSearch, Agent, Task, Bash(git push *), Bash(git push), Bash(git checkout *), Bash(git switch *), Bash(git reset *), Bash(git commit *), Bash(git rebase *), Bash(git merge *), Bash(gh *)
permission_mode: default
max_turns: 60
max_budget_usd: 10
env: BUILDKITE_API_TOKEN
---
Triage the most recent finished nightly build of `{{branch}}`: for every
failed job, say why it failed and whether it is new, and back each claim
with a line from a log. This is read-only work. Nothing is edited, retried
or posted; the result is the report.

The CI tools read Buildkite through a read-only token: `ci_builds` lists
recent builds with their failed jobs, `ci_build` gives one build's failed
jobs with job ids, `ci_job_log` reads the end of a job log (raise
`tail_kib`, or use `grep`, when the cause is further up). Git is read-only:
log, show, diff, merge-base, rev-parse, and `git fetch origin <commit>`
when a build's commit is not in the local clone. The working tree may be at
a different commit from the build; read code at the build's commit with
`git show <commit>:<path>`, not from the working tree.

1. History. `ci_builds` with `branch={{branch}}` and `limit=10`. Take the
   newest build whose state is not running or scheduled as the subject. For
   each of its failed jobs, mark by step_key in how many of the previous
   builds it also failed, and the last build where it passed.

2. Classify each failed job before you read logs:
   - hard (`soft_failed` false) or soft (the step is allowed to fail);
   - `canceled` or `timed_out` is a limit, not an assertion: find which
     limit (job timeout, agent lost, log size) before you look for a bug;
   - a step that never ran because an upstream job failed is not a cause:
     report the upstream job instead.

3. Root cause, hard failures first, then soft. For each job, `ci_job_log`
   the tail and find the first real error, not the last line: the command
   that failed, the assertion or exit status, and the file or test it names.
   When several jobs fail with the same error, triage one and list the
   others under it. When the cause is in the repository, locate it at the
   build commit (`git show <commit>:<path>`, Grep) and give file:line.

4. New failures. For a job that passed in an earlier build and fails now,
   take the commits between the two builds (`git log --oneline
   <passed>..<failed>`, both commits from `ci_builds`) and name the commits
   that touch the failing area, or say that none does. When none does, the
   cause is more likely in the environment (agents, caches, external
   services, data) or in an intermittent test; say which, with the evidence.

5. Intermittent or not. A job that fails in some builds and passes in
   others, with no relevant commit in between, is intermittent. Compare the
   error lines across two of its failures: the same assertion each time is
   one bug that does not always trigger; a different one each time is a
   flaky test or an environment problem.

Separate what a log line shows from what you infer. Mark every inference as
such. When the budget will not cover every job, finish the hard failures
and list the rest as not triaged rather than guess.

Finish with a report:
- The build: number, branch, commit, link, and the count of failed jobs
  (hard and soft).
- One table row per root cause: jobs affected, hard/soft, new / persistent
  (failing since build N) / intermittent, the cause in one sentence, the
  evidence (one quoted log line with the job link), and file:line or the
  suspected commits when there are any.
- Jobs not triaged, and why.
- A suggested next step for each new or persistent hard failure, in one
  line: who or what is likely to own it, and the first thing to check.
