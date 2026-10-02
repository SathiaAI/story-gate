# story-gate

**Every story starts on defined specs, is built and tested against them, and ends on them.** story-gate is the set of checks that makes sure of this, and it works in any AI coding client.

| Moment | What is checked |
|---|---|
| **READY** (before code) | Is the story complete? Does it drift from the PRD or TRD? Does every acceptance criterion have positive, negative, edge and regression tests? Are upstream handoffs in place? Were past learnings read? |
| **CHECKPOINT** (while coding) | Are we on course? Rough % complete per acceptance criterion. Drift, scope creep, TODOs, and whether tests keep pace with the code |
| **DONE** (after code) | Does the code match the spec? Is every acceptance criterion traced to automated tests that exist and pass on *this* code? Is there a handoff for downstream work? Are learnings recorded? |

How it works:
- Agents gather the evidence.
- **TypeSafe Jev** scores it in about 0.3 seconds, for well under $0.001 per check.
- `gate.py` (Python, stdlib only) makes the call. An agent grading its own work can never get a PASS.

## Install into a repo
```bash
cp -r story-gate/.story-gate <repo>/
cd <repo> && python3 .story-gate/gate.py install     # Windows: python .story-gate\gate.py install --python python
python3 .story-gate/gate.py doctor
```

`install` can be run more than once without harm, and it only adds to existing files. It writes:
- **Hooks** for every client below.
- **The skill** at `.agents/skills/story-gate/` and `.claude/skills/story-gate/`.
- **An instruction block** in `AGENTS.md`, `CLAUDE.md` and `GEMINI.md`.
- **A CI check** at `.github/workflows/story-gate.yml`.
- **`.gitignore` lines.**

Add an `OPENROUTER_API_KEY` repo secret so CI re-scores every PR independently.

## Using it in each client
Judging happens in `gate.py` and Jev, never in the client's model, so every client gets the same verdict. Clients differ only in how much is automatic:

| Client | Skill loaded from | Edit gate | Auto checkpoint | Stop gate | Cheap sub-agents |
|---|---|---|---|---|---|
| Claude Code | `.claude/skills` | ✅ | ✅ PostToolUse | ✅ | ✅ (`small`=haiku, `medium`=sonnet) |
| Codex CLI | `.agents/skills` | ✅ (trust `.codex/`) | ✅ PostToolUse | ✅ | ✅ agent TOML `model=` |
| Cursor | `.agents/skills`, `.claude/skills` | ✅ | ✅ postToolUse | ✅ | ✅ agent `model:` |
| Gemini CLI | `.agents/skills` | ✅ | ✅ AfterTool | ✅ | ✅ agent `model:` (Flash) |
| Windsurf / Devin | `.agents/skills` | ✅ | manual `checkpoint` | warn only | ❓ pick the model in the UI |
| Grok Build | `.claude/skills` | ✅ (`/hooks-trust`) | manual | ✅ | ❓ unverified |
| Muse Code | `.agents/skills` | ❓ | manual | ❓ | ❌ one model family, so do the steps inline |
| Cowork | account skill | ❌ hooks don't fire | manual | ❌ | ✅ |

In every row, the CI check is the backstop.

**Models are tiers, not names.** In `config.json` → `models`, each step is `small` (the cheapest fast model your client offers) or `medium` (its mid-tier coding model). Frontier models are never used for gate work.

## Modes (`.story-gate/config.json`)

| Setting | Effect |
|---|---|
| `"mode": "warn"` (default) | Everything reports problems but nothing blocks. Use this for the tuning period. |
| `"enforce_points": [...]` | Turns on blocking one point at a time: `pre_edit`, `checkpoint` (OFF_COURSE blocks edits until the work is corrected or a decision is recorded), `stop`, `ci`. |
| `"mode": "enforce"` | Everything blocks. Make `story-gate` a required check in branch protection. |
| `"test_command"` | Pins the test suite, so a fake `record-tests` run can't count. |
| `"checkpoint": {"every_edits": 10}` | How often automatic checkpoints run. |

## Sources and sinks
- **Sources:** `linear`, `repo`, `control-hub`, or `command`. A `command` source is your own channel: any shell command that prints the doc for `{id}`.
- **Sinks:**
  - `repo` (always on).
  - `control-hub`: the Supabase `events` table, via an insert-only role.
  - `webhook`.
  - `command`: your own channel, which gets JSON lines on stdin.
- **What sinks receive:** READY/DONE verdicts, checkpoints (live % per story), drift escalations and learnings.

## Tests
`python3 -m unittest tests/test_gate.py`
