# Context for SGR-1

## PRD
None — this story comes from a direct task, not a PRD. Source: Paul's decision of 7 Nov 2026, relayed by the VKGate coordinator.

## TRD
- `.github/workflows/gate.yml` header: "Pull request code never runs in a job that holds secrets; settings and story-gate's own code never come from the pull request."
- `.github/workflows/gate.yml#tests` and `#measure`: "No secrets here." Both set `permissions: contents: read`.
- `.github/workflows/gate.yml#story-gate`: "Has the secrets; runs the pinned story-gate ..." It is the only job that reads `secrets.*`.
- `.story-gate/sg_reusable.py#caller_files`: the caller file's `with:` block holds only `release-key-fingerprint`; `check_callers` (doctor) accepts extra `with:` keys.
- `tests/test_gate.py#test_the_published_workflows_are_pinned_and_keep_secrets_away_from_pr_code`: existing guard that the tests job has no `secrets.`.
- Release notes v0.9.2: CI isolation (`ci_isolation`, default strict) needs a Linux runner where story-gate can create an account. GitHub-hosted runners can; other runners must be checked.

## Upstream handoffs
None — no upstream dependencies

## Prior learnings
None — searched: runner, workflow, reusable
