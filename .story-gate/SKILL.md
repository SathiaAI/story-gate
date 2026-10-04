---
name: story-gate
description: "Quality gate for AI coding work. Use it whenever the repository has a .story-gate/ folder and you start, build or finish a feature, bug fix or story; when the user wants proof that AI-written code works before it merges; or when they ask to set up story-gate. Before code: write the story, its acceptance criteria and tests, and pass READY. While coding: run checkpoints that catch drift and scope creep. Before saying done: run the tests, run the feature for every acceptance criterion (scenarios that CI repeats), write a plain-English validation.md and handoff, record learnings, and pass DONE. A human approves on GitHub; you never approve or merge."
---

# story-gate

**Success criteria.** A story:
- starts on defined specs,
- is built and tested against them (AC → test cases → automated tests → results),
- ends on them, complete and delivered,
- and is accepted by a human, not by you.

Follow **`.story-gate/PROTOCOL.md`** step by step. `gate.py` means **`story-gate`**, the verified copy installed on this computer (if it isn't found, use the full command story-gate's messages print). Only where story-gate isn't installed (cloud agents) use `python3 .story-gate/gate.py` (`python` on Windows). With story-gate installed, the hooks refuse the repository's copy.

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
- **Judging never uses your model:** `gate.py` sends the evidence to the configured judge. That's Jev by default; see https://github.com/SathiaAI/story-gate/blob/main/docs/guide.md#fallbacks for the other options.

## The moments
1. **READY:** run `start <ID> --model <your model id>`, fill `story.md`, `context.md` and `tests.json`, then run `score <ID> ready`.
2. **CHECKPOINT:** runs automatically in clients with after-edit hooks. Otherwise run `gate.py checkpoint <ID>` after each AC. If it says OFF_COURSE, stop and correct the work, or escalate.
3. **DONE:**
   - Run `record-tests`.
   - Set `test_refs` for every AC.
   - Self-review the diff (`/engineering:code-review` in Claude clients).
   - For every AC, run the feature for real and record it: `scenario <ID> --name ... --ac AC-1 --expect <text> -- <command>`. CI runs these again.
   - Fill in `validation.md` for the owner (result, ACs, scenarios, bugs, lessons, limits, demo steps or 'Not demo-able: reason'). Add screenshots with `evidence`.
   - Write `handoff.md`, run `learn`, then run `score <ID> done`.
   - Show the owner: `report <ID> --open` builds the validation page. Show it for a change they can see; otherwise point to validation.md.
4. **ACCEPTANCE:** open the PR as the agent. CI re-checks everything, and a code owner approves the latest commit and merges. You never approve or merge.
5. **DRIFT:** never resolved silently.
   - Escalate it.
   - Waivers and decisions you record are only proposals until a code owner approves.

## Rules
- Quote the `gate.py` verdict line. Never declare a pass yourself.
- Write for a non-coder: short sentences, active voice, plain words, a Mermaid diagram where a picture is clearer (PROTOCOL.md > Writing). This covers chat replies, PR descriptions, the story's plain summary, validation.md, handoffs and learnings.
- Never edit `.story-gate` code, config or verdicts, CODEOWNERS or the story-gate workflows, by any route.
- Never run `install`, `install --user`, `enroll`, `unenroll`, `upgrade`, `rollback`, `release-sign`, `filter`, `lockdown` or `hook-trust`, and never touch the story-gate runtime, your tool's user hook settings or git's filter settings. Those are for the human.
- The judge gives scores, not reasons. For each failing check, explain the likely cause in one line.
- If the judge is unavailable, say so. Nothing passes without it.
- In Cowork, Cursor Cloud and Codex cloud, hooks don't run: run `gate.py status` before editing and run the checkpoints yourself. CI is the backstop.
