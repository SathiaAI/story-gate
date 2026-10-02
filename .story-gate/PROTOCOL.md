# Story Gate Protocol (any AI client)

Every code change belongs to a story. A story starts on defined specs, is built and tested against them, and ends on them. It passes **READY** before code is written, **CHECKPOINTS** while it's being built, and **DONE** before the work is called finished. Agents collect the evidence; `gate.py` makes the call. Never declare a gate passed yourself. Quote the line `gate.py` prints.

`gate.py` = `python3 .story-gate/gate.py` (`python` on Windows). Run it from the repo root.

**Mode** (`.story-gate/config.json`):
- `"mode": "warn"` reports problems without blocking.
- `"mode": "enforce"` blocks edits, stopping and merging.
- `enforce_points` turns on blocking one point at a time (`pre_edit`, `checkpoint`, `stop`, `ci`) while mode is still `warn`.

**Sub-agents:** if your client can spawn them, run the steps marked ∥ in parallel. `config.json` → `models` names a **tier** per step:
- `small` = the cheapest fast model your client offers.
- `medium` = its mid-tier coding model.
- Never use a frontier model for gate work.

If your client can't spawn sub-agents, or can't pick their model, do the steps yourself in order. Judging is always done by Jev through `gate.py`, so your client's model choice doesn't change the verdict.

---

## READY (before any code)

1. `gate.py start <ID>` creates `.story-gate/stories/<ID>/` and makes `<ID>` the active story.

2. **Intake** (model: `intake`). Fetch the story from the source in `config.json` → `sources`:
   - Linear issue, repo file, control-hub document, or a `command` source (`gate.py source <ID>`).
   - Write it **verbatim** into `story.md`. Fill the front matter: `source`, `depends_on`, `consumers`.
   - Don't improve the story here. Gaps are findings, not something to fix silently.

3. ∥ **Context** (model: `context`). Fill `context.md`, quoting only relevant excerpts, each with its source:
   - `## PRD`: the PRD sections this story serves.
   - `## TRD`: the architecture and technical rules it must follow.
   - `## Upstream handoffs`: one `### <dep-id>` block per `depends_on`. Use that story's `handoff.md`, or the equivalent from Linear or control-hub.
   - `## Prior learnings`: run `gate.py learnings <3-6 keywords>`, then paste the relevant hits with their ids. If nothing fits, write `None — searched: …`.

4. ∥ **Test plan** (model: `tests`). Fill `tests.json`:
   - Add one entry per acceptance criterion.
   - Each entry needs concrete `positive`, `negative`, `edge` and `regression` cases. For any category that truly doesn't apply, put a reason under `not_applicable.<category>`.
   - Never invent or rewrite acceptance criteria. Put suggestions in `proposed_missing_acs`.
   - Leave `test_refs` empty for now. They're filled during DONE.

5. `gate.py score <ID> ready`. Jev scores 14 semantic checks plus the drift direction. The structural checks are deterministic.

6. Act on the result:
   - **PASS**: start coding.
   - **CONCERNS** or **FAIL**: show the owner the failing lines (one table: check → why → proposed fix).
     - Fix the story at its **source** (Linear, PRD repo, etc.) only with the owner's OK.
     - Then copy the change into `story.md` and re-score.
   - **ESCALATED**: see *Drift* below.

## Drift (spec vs story): never resolved silently

When `drift_decision` is ESCALATED, `gate.py` has already queued a `story_gate.drift_escalated` event for the sinks. Then:

1. Write `stories/<ID>/drift.md` with four parts:
   - What differs (quote both sides).
   - Since when. Use `git log` on the PRD/TRD and the story history.
   - Who or what changed it. Name the session, commit or decision, if known.
   - The options: change the story, change the PRD/TRD, or split the work.
2. Send it to the **orchestrator / architect** and to the **session that owns the conflicting change**. Use the channels configured as sinks (Linear comment, control-hub event, command or webhook) and the owner's HANDOFF.md.
3. If there are real options to weigh, use the **frontier-gate** skill (Jev triage → frontier panel). Its decision becomes the recommendation.
4. The human owner or the architect decides. Record it:
   `gate.py decide <ID> --drift story|spec|none --by <who> --note "<why>"`
5. Apply the decision at the source: update the PRD/TRD, or update the story. Then re-score.

## CHECKPOINTS (while coding): keep course, contain drift

`gate.py checkpoint <ID>`: Jev reads the story, the test plan and the diff so far, then reports:
- `ON_TRACK`, `AT_RISK` or `OFF_COURSE`.
- An **estimated** % complete: the average of the per-AC "implemented" probabilities. Use it as a progress signal, not a contract.
- Per-AC status: done, partial or todo.
- Drift, scope creep, TODO/deferrals, and whether tests are keeping pace with the code.

**When it runs:**
- **Automatically** every `checkpoint.every_edits` code edits (default 10) in clients with after-edit hooks: Claude Code, Codex, Cursor, Gemini CLI. The result is fed back to the coding model.
- **Manually** elsewhere (Windsurf, Grok, Muse, Cowork): after finishing each acceptance criterion, before any large refactor, and at least every ~30 minutes of work.

**How to act on it:**
- **ON_TRACK:** continue. Mention the % line in your progress updates.
- **AT_RISK:** fix the named cause now (write the lagging tests, remove the TODO, re-read the AC), then continue.
- **OFF_COURSE:** stop adding code.
  - If the work drifted, correct it and re-run `checkpoint`.
  - If the story or spec is the problem, follow *Drift* and record `gate.py decide <ID> --phase build ...`.
  - With `checkpoint` in `enforce_points`, code edits are blocked until one of those happens.

Checkpoints are appended to `stories/<ID>/checkpoints.jsonl` and published as `story_gate.checkpoint` events. That gives a dashboard live progress per story.

## DONE (before saying "done")

1. `gate.py record-tests <ID> -- <the project's test command>`. If `config.json` → `test_command` is set, that command always runs. The tests must be green, on the current code. This is evidence, not a claim.

   **Traceability:** in `tests.json`, set each acceptance criterion's `test_refs` to the names of the automated tests that cover its cases (e.g. `test_ac1_expired_token_401`). DONE fails any criterion with no tests that exist in the changed code. `trace.md` is written for reviewers: AC → planned cases → tests → found → suite result.

2. ∥ **Handoff writer** (model: `handoff`). Write `stories/<ID>/handoff.md`:
   - Sections: What changed · Interfaces and contracts · How to verify · Known limits · Downstream consumers · Drift decisions.
   - Downstream stories read this. Write it for a stranger.

3. ∥ **Learnings recorder** (model: `learnings`). Record every error hit, wrong turn and reusable insight:
   - `gate.py learn <ID> --type error|learning|pattern --summary "…" --root-cause "…" --rule "<what future agents must do>" --tags a,b --client <your client>`
   - If nothing was learned: `--type none --summary "no new learnings"`.
   - Rules repeated 3+ times are flagged by `gate.py learnings`. Promote those into AGENTS.md / CLAUDE.md, with the owner's OK.

4. `gate.py score <ID> done`. This checks the diff against the story and TRD, the tests against the plan, traceability, deferrals, handoff quality and learnings. Act on it as in READY.

5. `gate.py publish` sends events to the configured sinks (control-hub, webhook, custom command).

6. Notify the downstream consumers listed in `story.md` that the handoff is ready, using the same channels.

## Waivers
Only the human owner may waive a check:
`gate.py waive <ID> <check> --by <who> --reason "…"`.

## Rules
- Fail closed. If the judge is unavailable, the verdict can't be PASS. Say so; don't work around it.
- Don't edit `ready.json` / `done.json` by hand. Don't delete `decisions.jsonl` or `learnings.jsonl` lines (append-only).
- Quote the `gate.py` verdict line in your reply. Never paraphrase it into a pass.
