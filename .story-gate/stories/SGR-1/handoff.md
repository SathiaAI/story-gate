# Handoff: SGR-1

## What changed
`.github/workflows/gate.yml` has a new optional input, `runner` (string, default `ubuntu-latest`). The `tests` and `measure` jobs now use `runs-on: ${{ inputs.runner }}`. Nothing else in the workflow changed. Five new tests in `tests/test_gate.py` check it.

## Interfaces and contracts
- Input `runner` on `gate.yml`: a runner label. It applies to `tests` and `measure` only, which hold no secrets.
- The `story-gate` job, `audit.yml` and `dashboard.yml` stay on `ubuntu-latest`. A caller cannot move them.
- Callers that pass nothing see no change.

## How to verify
Run `python3 -m unittest tests.test_gate.TestReusableWorkflow`. Or run the two scenarios recorded for SGR-1.

## Known limits
- `story-gate install --ci reusable` rewrites the caller file and removes a hand-added `runner:` line. It has no `--runner` flag. A follow-up PR is planned.
- Callers need a new signed release before they can use `runner`.
- A runner that cannot create story-gate's throwaway CI account makes `tests` fail closed (`ci_isolation: strict`).

## Downstream consumers
Callers such as Viaknox/vkgate-suite, after a new release is pinned.

## Release and rollback
A new signed release is needed. Rollback: callers drop the `runner:` line, or pin the previous release.

## Drift decisions
None.
