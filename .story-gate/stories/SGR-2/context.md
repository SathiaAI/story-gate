# Context for SGR-2

## PRD
None — this story comes from a security finding, not a PRD. Source: CodeRabbit finding 4499721918 on PR #104, relayed by the VKGate coordinator.

## TRD
- `.story-gate/gate.py#runtime_copy_check`: "that copy must be a signed release no older than the base branch's, with no symlinks or compiled files". It flagged extra files only by importable suffix (`IMPORTABLE`).
- `.story-gate/sg_trust.py#verify_release`: `runtime_files()` skips symlinks, so a link was never listed as an extra.
- `python3 .story-gate/gate.py` puts `.story-gate/` first on sys.path, so a link there can shadow a standard module.

## Upstream handoffs
None — no upstream dependencies

## Prior learnings
None — searched: symlink, signed copy, importable (run `gate.py learnings symlink`)
