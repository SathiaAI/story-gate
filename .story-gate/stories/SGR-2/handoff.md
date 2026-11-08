# Handoff: SGR-2

## What changed
`.story-gate/sg_trust.py`: new `stray_links()`, and `verify_release` fails when it lists any link. `.story-gate/gate.py`: `runtime_copy_check` fails on any link the signed release does not list, and its docstring says so. Tests in `tests/test_gate.py`.

## Interfaces and contracts
- `stray_links` raises `TrustError` when a folder can't be listed (`os.walk` gets an `onerror`). `runtime_copy_check` uses `stray_links` for its link check and returns a problem for that error.
- `stray_links(src)` returns the relative paths of links under `src`, leaving out real `stories/` and `__pycache__` folders. It never follows folder links.
- `verify_release` raises `TrustError` "links not covered by the signed release: ...".
- `runtime_copy_check` returns the problem "has links the signed release doesn't list".

- `cmd_ci` counts a link under `.story-gate/` (outside `stories/`) as story-gate code, so the signed-copy check and the code-owner rule apply to it.

- Test helper `ci` (in `tests/test_gate.py`) takes `links` and `keep_config`; default behaviour is unchanged.

## How to verify
Run `python3 -m unittest tests.test_gate.TestReleaseSignatures tests.test_gate.TestStoryGateOnlyPullRequests`. It needs `ssh-keygen`.

## Known limits
A real folder that holds only ordinary files is not checked for importability beyond the existing rules.

## Downstream consumers
PR #104 (Release v0.11.0): merge main into it, update the README link wording, then Paul signs.

## Release and rollback
Ships in the next signed release. Rollback: revert this PR.

## Drift decisions
None.
