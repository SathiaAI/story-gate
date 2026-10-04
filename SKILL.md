---
name: story-gate
description: "Per-story quality gate for any coding work, in any AI client: READY before code (story complete, drift vs PRD/TRD, acceptance criteria with positive/negative/edge/regression tests), CHECKPOINTS while coding (progress %, on course?, drift), DONE after (code matches spec, every AC traced to tests that ran and passed and shown working by a scenario run that CI repeats, a plain-English validation summary for the owner, handoff, learnings), then human acceptance on GitHub. Use before starting, during, or finishing any story or coding task, or when the repo has a .story-gate/ folder."
---

# Story Gate

**Success criteria.** A story:
- starts on defined specs,
- is built and tested against them (AC → test cases → automated tests → results),
- ends on them, complete and delivered,
- and is accepted by a human, not by you.

Follow **`.story-gate/PROTOCOL.md`** step by step. `gate.py` means **`story-gate`**, the verified copy installed on this computer (if `story-gate` isn't found, use the full command story-gate's messages print). Only where story-gate isn't installed (cloud agents, where hooks don't run) use `python3 .story-gate/gate.py` (`python` on Windows). With story-gate installed, the hooks refuse running the repository's copy, because a branch can replace it.

## Your identity
- Work under the **agent identity**: run `gate.py agent-env --repo owner/name` and use its token and git name.
- Never use the human's GitHub login.
- If `gate.py doctor --repo owner/name` warns that this shell holds a code owner's login, stop and tell the human.

## Sub-agents: tiers, not model names

| Tier | Use it for | Example models (pick what your client offers) |
|---|---|---|
| `small` | Fetch, copy, log | Claude haiku, a Gemini Flash model, a "mini" or "fast" tier model |
| `medium` | Read specs, write test plans and handoffs | Claude sonnet, or your client's standard coding model |
| never | Frontier models are not used for gate work | |

- **If your client can't choose a model for sub-agents** (or has no sub-agents), do the steps yourself, in order.
- **Judging never uses your model:** `gate.py` sends the evidence to the configured judge. That's Jev by default; see the README for the other options.

## The moments
1. **READY:** on a story branch (e.g. `feat/<ID>-short-name`), run `start <ID> --model <your model id>`, fill `story.md`, `context.md` and `tests.json`, then run `score <ID> ready`.
2. **CHECKPOINT:** runs automatically in clients with after-edit hooks. Otherwise run `gate.py checkpoint <ID>` after each AC. If it says OFF_COURSE, stop and correct the work, or escalate.
3. **DONE:**
   - Run `record-tests`.
   - Set `test_refs` for every AC.
   - Self-review the diff (`/engineering:code-review` in Claude clients).
   - For every AC, run the feature for real and record it: `scenario <ID> --name ... --ac AC-1 --expect <text> -- <command>`. CI runs these again.
   - Fill in `validation.md` for the owner (result, ACs, scenarios, bugs, lessons, limits, demo steps or 'Not demo-able: reason').
   - Write `handoff.md` (all seven sections), then run `learn` (`--type error|pattern` also needs `--root-cause` and `--rule`), then run `score <ID> done`.
   - Any later change to code, tests, `tests.json` or the specs makes the verdict out of date: re-run `record-tests` and re-score.
4. **ACCEPTANCE:** open the PR as the agent. CI re-checks everything, and a code owner approves the latest commit and merges. You never approve or merge.
5. **DRIFT:** never resolved silently.
   - Escalate it.
   - Waivers and decisions you record are only proposals until a code owner approves.

## Rules
- Quote the `gate.py` verdict line. Never declare a pass yourself.
- Never edit `.story-gate` code, config or verdicts, CODEOWNERS or the story-gate workflows, by any route.
- Never run `install`, `install --user`, `uninstall`, `enroll`, `unenroll`, `upgrade`, `rollback`, `release-sign`, `setup-repo`, `setup-agent`, `judge-calibrate`, `filter`, `lockdown` or `hook-trust`, and never touch the story-gate runtime, your tool's user hook settings or git's filter settings. Those are for the human.
- The judge gives scores, not reasons. For each failing check, explain the likely cause in one line.
- If the judge is unavailable, say so. Nothing passes without it.
- In Cowork, Cursor Cloud and Codex cloud, hooks don't run: run `gate.py status` before editing and run the checkpoints yourself. CI is the backstop.

## Claude / Cowork specifics
- **If the repo has no `.story-gate/`:** stop and ask the human to set it up: copy it from github.com/SathiaAI/story-gate, then run `gate.py install` and `gate.py setup-repo` themselves. Agents never run those commands.
- **Judge key on Paul's machine:** it lives in `F:\ENV\.env`. Run `gate.py` through `device_bash` with `STORY_GATE_ENV_FILE=$HOME/mnt/ENV/.env`. A cloud shell has no key, so local verdicts there can't PASS. CI still judges, using the repo secret.
- **Sub-agents:** use the Agent tool, with `model: haiku` for `small` and `model: sonnet` for `medium`. Sub-agents write only their evidence file.
- **Drift with real options:** run **frontier-gate**, then give Paul plain-English options with pros/cons and a recommendation.
- **Report:**
  - The verdict line.
  - A table of check → why → fix, only if the verdict isn't PASS.
  - One next step.
- **Related skills:**
  - **session-handshake:** session handoff.
  - **check-jev:** when the judge errors.
  - **pr-review-loop:** after the PR is open.
