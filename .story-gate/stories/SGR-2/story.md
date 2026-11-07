---
id: SGR-2
title: A link under .story-gate that the signed release does not list fails the signed-copy check
feature: none
source: other (task from the VKGate coordinator, session_01WSd5XnnHWYegjkHxdphSwf, 7 Nov 2026; CodeRabbit finding 4499721918 on PR #104)
depends_on: []
---
## Plain summary
CI checks that story-gate's copied code in a repository is a signed release with nothing extra that Python could load. Today a link with no `.py` name, such as `json` pointing to a folder with an `__init__.py`, slips past that check. Python would load it instead of the real `json` module. This change makes any link the signed release does not list fail both checks, whatever its name. Tests show the link failing before the fix and the clean copy still passing.

## Why it matters
Code owners approve a story-gate copy because CI says it is a signed release with nothing extra. A hidden link breaks that promise: the code behind it would run inside the checks that hold the judge's keys. Closing the hole keeps the approval honest. The people who gain are every repository that uses the signed-copy check, and the maintainer who signs releases.

## Story (word for word from the source)
Coordinator: "4499721918 (Major, security): an extensionless symlink under `.story-gate/` slips past the signed-copy check. `runtime_copy_check` (gate.py ~4819) flags extra files only by their importable suffix, and `verify_release` (sg_trust.py ~132/181-198) skips symlinks when it lists extras. Because `.story-gate/` comes first on sys.path, a link named `json` that points to a directory with `__init__.py` would shadow `import json`.
a. Reproduce it first with a failing test: a symlink `.story-gate/json` -> dir with `__init__.py` must make `runtime_copy_check` AND `verify_release` fail. Also cover a link to a file with no suffix.
b. Fix: any symlink under `.story-gate/` that isn't in the manifest counts as an extra and fails, whatever its suffix. That includes directory links: check `is_symlink()` before walking into them, and keep the existing exclusions for `__pycache__` and `stories/`. Make the docstring and README wording match.
c. Run the full suite, gitleaks chained to the push, and open a PR to main. In the body, say it touches runtime and code-owner files (gate.py, sg_trust.py), so Paul approves it. Post "@codex review"."

- AC-1: `verify_release` raises TrustError "links not covered by the signed release" for a link under the folder that the manifest does not list: a link to a package folder, a link to a file, a dangling link, and a link inside a real subfolder such as `vendor/`.
- AC-2: `runtime_copy_check` returns a problem naming the link for the same cases, and still passes a copy with no link.
- AC-3: Links inside a real `stories/` or `__pycache__` folder are left alone. A link that is itself named `stories` or `__pycache__` is not.
- AC-4: Folder links are not followed (checked with `is_symlink()` before walking).
- AC-5: A folder that cannot be listed makes `verify_release` raise TrustError naming the folder, and makes `runtime_copy_check` return a problem. The check fails closed; it never skips what it cannot see. (CodeRabbit finding 4499800452 on PR #105, relayed by the coordinator.)

## Decisions for this story
- Scope is `.story-gate/gate.py` (`runtime_copy_check`), `.story-gate/sg_trust.py` (`verify_release`) and tests. Both are runtime and code-owner files, so Paul approves the PR.
- The README line about links ("no `.pyc`, `.so`, package folders or links") exists only on the v0.11.0 release branch (PR #104), not on main. It is updated there, after this PR merges, as the coordinator planned. On main only the docstring changes.
- There is no PRD for this story. The rule it protects is the signed-copy check described in the v0.11.0 notes and in `runtime_copy_check`'s docstring.
- No open question is left: the coordinator's message gives the reproduction, the fix and the PR steps.
- AC-6: CI runs `runtime_copy_check` for a pull request that changes only a link under `.story-gate/` with no importable suffix. Before, `cmd_ci` ran it only for importable suffixes, `vendor/`, `__pycache__` and listed runtime files, so such a PR skipped the check and the code-owner rule. (Cursor Bugbot finding on PR #105.)

## Dependencies
None

## Risks
### R1: A legitimate link under .story-gate starts failing
likelihood: low
impact: medium
mitigation: story-gate's own copy never contains links (the signed release lists real files only, and setup refuses links). The full suite and packaging tests run with the change.

## Blockers
None
