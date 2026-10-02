---
name: story-gate
description: "Per-story quality gate for any coding work, in any AI client: READY before code (story complete, drift vs PRD/TRD, acceptance criteria with positive/negative/edge/regression tests), CHECKPOINTS while coding (progress %, on course?, drift), DONE after (code matches spec, tests traced to every AC and green, handoff, learnings). Use before starting, during, or finishing any story or coding task, or when the repo has a .story-gate/ folder."
---

# Story Gate

**Success criteria.** A story:
- starts on defined specs,
- is built and tested against those specs (acceptance criteria → test cases → automated tests → results),
- ends on defined specs, complete and delivered.

Anything else is a finding, never a quiet fix.

Follow **`.story-gate/PROTOCOL.md`** step by step. `gate.py` = `python3 .story-gate/gate.py` (`python` on Windows).

## Sub-agents: use tiers, not model names
`.story-gate/config.json` → `models` gives each step a **tier**:

| Tier | Meaning | Examples (pick what *your* client offers) |
|---|---|---|
| `small` | Cheapest fast model, for fetching, copying and logging | Claude: haiku · Gemini: a Flash model · Codex/Cursor: the "mini"/"luna"/fast tier |
| `medium` | Mid-tier coding model, for reading specs and writing test plans and handoffs | Claude: sonnet · otherwise the client's standard coding model |
| (never) | Frontier/top models are **not** used for gate work | — |

- **Your client can spawn sub-agents with a chosen model** (Claude Code, Codex, Cursor, Gemini CLI): run the parallel steps as sub-agents on the tier's model.
- **It can't** (e.g. Muse has one model family, Windsurf has no documented sub-agents): do the steps yourself, in order.

Either way the cost stays low, because **judging is not done by your model**. `gate.py` sends the evidence to **Jev** (a sub-second scoring model, fractions of a cent per check). The same judge is used in every client.

## The four moments
1. **READY:** `start` → fill `story.md`, `context.md`, `tests.json` → `score <ID> ready`.
2. **CHECKPOINT** (while coding): runs automatically every N code edits in clients with after-edit hooks (Claude Code, Codex, Cursor, Gemini). In other clients, run `gate.py checkpoint <ID>` after each acceptance criterion or about every 30 minutes.
   - It reports `ON_TRACK / AT_RISK / OFF_COURSE`, an estimated % complete, and per-AC status.
   - **OFF_COURSE:** stop and correct the work, or escalate the drift. Don't keep coding.
3. **DONE:**
   - `record-tests` (the configured test command).
   - Map every acceptance criterion to its automated tests in `tests.json` → `test_refs`.
   - Write `handoff.md`, then run `learn`, then `score <ID> done`.
   - Check `trace.md`: AC → cases → tests → result.
4. **DRIFT:** never resolved silently. Escalate to the architect/orchestrator and the owning session. A human records `gate.py decide`.

## Rules
- Quote the `gate.py` verdict line. Never declare a pass yourself, and never edit `*.json` verdicts or config.
- Jev gives scores, not reasons. For each failing check, explain the likely cause in one line from the check's wording and the evidence.
- Only a human may `waive` a check or `decide` drift.
- **If the judge is unavailable:** say so. The gate can't PASS without it.
  - In Claude/Cowork, the **check-jev** skill diagnoses it.
- **Cowork:** hooks don't fire there, so run `gate.py status` before your first edit and the checkpoints yourself. CI is the backstop.

## Claude / Cowork specifics
- **If the repo has no `.story-gate/`:** copy it from github.com/sathiaai/story-gate (or `F:\ENV\story-gate\`), run `gate.py install`, and commit.
- **Run `gate.py` where the repo and the OpenRouter key live.** On Paul's machine that is `device_bash`, with `F:\ENV` connected; the key is in `~/mnt/ENV/.env`.
  - A cloud shell has no key, so verdicts there are self-scores and can never PASS. Say so.
- **Sub-agents:** use the Agent tool with `model: haiku` for `small` and `model: sonnet` for `medium`.
  - Sub-agents write only their evidence file. They never run `score`, `decide` or `waive`.
- **Drift with real options:** run **frontier-gate**, then give Paul plain-English options with pros/cons and a recommendation.
- **Report** in one block:
  - The verdict line.
  - A table of check → why → fix, only if the verdict isn't PASS.
  - One next step.
- **Related skills:**
  - **session-handshake:** session handoff, which is not the same as the story handoff.
  - **check-jev:** when the judge errors.
  - **adversarial-review / pr-review-loop:** after DONE.
