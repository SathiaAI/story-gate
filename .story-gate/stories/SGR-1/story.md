---
id: SGR-1
title: Let callers choose the runner for the secret-free tests and measure jobs
feature: none
source: other (task from the VKGate coordinator, session_01WSd5XnnHWYegjkHxdphSwf, 7 Nov 2026)
depends_on: []
---
## Plain summary
Callers of story-gate's reusable gate workflow can now say which machine runs the two jobs that hold no secrets: `tests` and `measure`. They pass a new optional input called `runner`. If they don't pass it, those jobs run on `ubuntu-latest`, as before. The `story-gate` job, the audit workflow and the dashboard workflow stay on `ubuntu-latest` and ignore the input, because they get API keys or a write token. Tests read the workflow file and prove all of this.

## Story (word for word from the source)
Paul's decision (7 Nov 2026): "Move all but the lock job; keep every job that uses secrets on GitHub-hosted runners."

Goal: let callers run the secret-free jobs of the reusable gate workflow on a cheaper runner (Ubicloud), without ever moving a job that holds secrets or a write token.

Change, in `.github/workflows/gate.yml` only:
1. Add an optional `workflow_call` input `runner` (type string, default `ubuntu-latest`), with a description saying it applies only to the secret-free `tests` and `measure` jobs.
2. Set `runs-on: ${{ inputs.runner }}` on the `tests` and `measure` jobs only.
3. Leave the `story-gate` job, `audit.yml` and `dashboard.yml` on a hard-coded `ubuntu-latest`. They receive API keys or an issues:write token, so a caller must not be able to move them.
4. No other edits. Callers that don't pass `runner` must behave exactly as before.

Test must prove three things: the input exists with default `ubuntu-latest`; only tests and measure use it; story-gate, audit and dashboard never reference `inputs.runner`.

Also: check how `sg_reusable.py` / `caller_files()` render the caller file. Callers must be able to pass `runner` through `story-gate install`, or by hand without the next install deleting it. Say which in the PR.

Done means: a draft PR with green checks, a story at score done, and a message to the coordinator with the PR link, its head sha, and the release step.

## Scope and acceptance criteria
In scope: the `runner` input on gate.yml, the two `runs-on` lines, and tests.
Out of scope: cutting or signing a release, merging, changing repo settings, a `--runner` flag on `install`, and the `vkgate-lock` workflow.
- AC-1: gate.yml declares an optional `workflow_call` input `runner`, type string, default `ubuntu-latest`, whose description names `tests` and `measure`.
- AC-2: Exactly two jobs of gate.yml, `tests` and `measure`, set `runs-on: ${{ inputs.runner }}`, and neither reads a secret.
- AC-3: The `story-gate` job of gate.yml, and every job in audit.yml and dashboard.yml, set `runs-on: ubuntu-latest` and never mention `inputs.runner`.
- AC-4: When `runner` is not passed, every job of the three workflows resolves to `ubuntu-latest`.
- AC-5: `story-gate doctor` accepts a caller file that sets `runner` by hand.

## Decisions for this story
- Scope stays `.github/workflows/gate.yml` plus tests, as the brief says. `story-gate install` is not changed. The PR reports what install does with a hand-added `runner:` line (it drops it; checked by hand), and the coordinator decides on a follow-up.
- Confirmed by the coordinator (7 Nov 2026): the keep-on-GitHub-hosted "lock job" is `vkgate-lock`, which lives in Viaknox/vkgate, not here. In story-gate only `tests` and `measure` take the input. The `story-gate` job, `audit.yml` and `dashboard.yml` stay hard-coded `ubuntu-latest`.
- Confirmed by the coordinator: a `--runner` flag on `install` is a follow-up PR. Until it ships, a re-install removes a hand-added `runner:` line and the jobs return to GitHub-hosted runners. That costs more but is safe.
- No question about scope or the install flag is open.

## Dependencies
None

## Risks
### R1: A cheaper runner may not support story-gate's CI isolation
likelihood: medium
impact: low
mitigation: The default stays ubuntu-latest, so nothing changes unless a caller opts in. CI isolation (ci_isolation, strict by default) runs nothing where it cannot set up its throwaway account, so a runner that lacks it fails closed. The PR tells callers to try one repository first.

## Blockers
None
