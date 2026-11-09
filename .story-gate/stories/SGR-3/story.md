---
id: SGR-3
title: Disable judge.decompose "on" in Release 1 of the narrow judge checks
feature: none
source: other (task from the VKGate coordinator, session_01WSd5XnnHWYegjkHxdphSwf, 8 Nov 2026; Paul's decision, option A, on the Codex security thread of PR #99)
depends_on: []
---
## Plain summary
PR #99 adds narrow judge questions over evidence packs. Codex found that test code in one acceptance criterion's pack can change what another criterion's check sees. That cannot matter while the old judge still decides (`shadow`), but it could if the narrow scores decided (`on`). Paul decided to ship Release 1 with `on` switched off. A config that sets `on` now stops the gate with a message that names issue #106; `shadow` and `off` work as they do today. Tests show `on` refused, nothing quietly downgraded, and `shadow` and `off` unchanged.

## Why it matters
`on` makes a narrow score the verdict. Until the evidence packs are hardened, a verdict could rest on evidence that another criterion's test changed. Refusing `on` loudly (rather than quietly using `shadow`) means nobody believes the narrow checks are deciding when they are not. The people who gain are every repository that updates story-gate, and Paul, who approves the release.

## Story (word for word from the source)
Coordinator: "Paul decided on 8 Nov 2026: option A for the open Codex security thread on .story-gate/sg_decompose.py:496 (comments 4353080479 and 4353098741, 'cross-AC test side effects in evidence packs'). Option A means: ship Release 1 with `judge.decompose: on` disabled. Only `off` and `shadow` are allowed until a hardening story fixes this finding.
1. Bring the branch up to date. Merge origin/main into the branch with a merge commit. No rebase, no amend, no force-push.
2. Disable `on`. When the config sets `judge.decompose: on`, config loading rejects it with a clear message, for example: 'judge.decompose: on is disabled in this release until the narrow-check hardening (issue #<n>) is done. Use shadow or off.' Fail loudly. Never quietly downgrade to shadow. Add tests: `on` is rejected; `shadow` and `off` behave as they do now. Keep the rest minimal. You don't need to delete the binding code path, but nothing reachable may make narrow checks binding.
3. Open one GitHub issue in SathiaAI/story-gate: 'Harden narrow checks before judge.decompose: on'. List three items: (a) this finding, with a link to the thread; (b) known limit 1, a large file cut from a pack; (c) known limit 2, the request cap on changes with many files. Use its number in the message from step 2.
4. Update every doc that tells people they can set `on`: the PR body (setting values, Known limits, Rollback), docs/decisions/2026-10-22-judge-evidence-packs.md, and any README, SECURITY.md or config docs. Say plainly that `on` is disabled for now, and why.
5. Reply once on the security thread: the decision (Paul, option A), the commit that disables `on`, and the issue link. Then resolve the thread.
6. Follow the repo's CLAUDE.md and story-gate steps. Run the full test suite and push only when it passes. Push with gitleaks detect ... && git push. Commit author: Claude <noreply@anthropic.com>, with the usual trailers and no model id.
7. Wait for every check and bot review on the new head. Fix anything real. Then send me the head SHA, CI status and any open threads. Don't approve, merge or change settings."

- AC-1: Config loading (`check_config`) refuses `judge.decompose: on` with "judge.decompose: on is disabled in this release until the narrow-check hardening (issue #106) is done. Use shadow or off." and the gate fails closed until the config is fixed.
- AC-2: `score` never lets narrow checks decide: with `on` in the config it stops with the same message and writes no verdict (no quiet fallback to `shadow`).
- AC-3: `shadow` (the default) and `off` behave as before: `shadow` records `decomposed` beside the binding verdict, `off` records none.
- AC-4: Every doc that says people can set `on` says it is disabled for now and why: the decision record, the guide, PROTOCOL.md, the README and `story-gate settings`; the PR body is updated the same way.

## Decisions for this story
- Mechanism: a module flag `sg_decompose.ON_ENABLED = False` (a fixed line in the shipped code, not a setting or an environment variable, so nothing a pull request author or caller controls can switch it on). `settings_problem` refuses `on` while it is false; `score` raises `ConfigError` for the same case in case a path skipped config loading. The binding code path stays.
- Tests that exercise the binding path switch the flag on in their own repository copy (`TestDecompose.set_on_enabled`), so the shipped file stays `False`.
- The `on` to `shadow` "weaker" rule in `sg_trust.weaker` stays: it is harmless and applies again once `on` is enabled.
- The issue is #106 (https://github.com/SathiaAI/story-gate/issues/106).
- No open question is left; the coordinator's message gives the decision, the wording and the steps.

## Dependencies
None

## Risks
### R1: An existing config that sets `on` stops the gate
likelihood: low
impact: medium
mitigation: Release 1 is not yet released, and `shadow` was the default, so no released version accepted `on` in a shipped config. The message names the fix (`shadow` or `off`).

## Blockers
None
