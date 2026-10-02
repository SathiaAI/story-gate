<p align="center"><img src="docs/assets/header.jpg" alt="story-gate by Viaknox. Every story starts on spec, is tested against spec, and ends on spec. Ink drawing of a city rail station with a bright orange buffer stop at the end of the track." width="100%"></p>

**story-gate** is a quality gate for AI coding work that runs the same way in Claude Code, Codex, Cursor, Gemini CLI, Windsurf/Devin, Grok Build, Muse, Cowork and cloud agents.

1. Cheap agents gather the evidence.
2. An independent judge scores it.
3. Fixed rules decide.
4. A human accepts.

**Contents:** [The problem](#a-the-problem-were-solving) · [Expected outcome](#b-expected-outcome) · [Setup in about 10 minutes](#setup-in-about-10-minutes) · [Using it in each client](#c-using-it-in-each-client) · [What we evaluate](#d-what-we-evaluate-today) · [Settings and their impact](#e-turning-things-on-and-off) · [Fallbacks](#fallbacks) · [Known limits](#known-limits)

---

## A. The problem we're solving

AI coding agents are fast and eager to say "done". Left alone, they:

| What goes wrong | What it costs |
|---|---|
| Start coding from a vague or incomplete story | Rework, and features nobody asked for |
| Drift from the PRD or TRD without telling anyone | Architecture erodes quietly, and the docs stop telling the truth |
| Write tests that don't map to the acceptance criteria | "Green" builds that don't prove the story works |
| Wander off scope mid-story | Wasted sessions and half-finished work |
| Grade their own work, waive their own failures, or act with your GitHub login | Nobody actually checked; "approved" means nothing |
| Hand off nothing, learn nothing | The next story re-discovers the same context and repeats the same mistakes |

Every client has different hooks and models, so until now there has been no single, consistent check across them.

## B. Expected outcome

**A story is done only when it is complete, delivered, provably matches its specs, and a human has accepted it.**

<p align="center"><img src="docs/assets/lifecycle.png" alt="The story lifecycle in four moments: Ready, Checkpoints, Done and Acceptance, with the checks in each" width="100%"></p>

- **Spec in:** work starts only from a complete, testable story that agrees with the PRD and TRD. The specs are fingerprinted, so a change mid-story is caught.
- **Built to spec:** checkpoints during the work catch drift and scope creep while they're still cheap to fix.
- **Tested against spec:** each acceptance criterion maps to planned positive, negative, edge and regression cases, then to real tests that **CI runs itself**.
- **Spec out:** the code matches the story and TRD. Any deviation was decided by a person and recorded.
- **Accepted by a human:** a code owner approves the latest commit on GitHub, then merges. An AI can't approve, waive or merge its own work.
- **Nothing lost:** a handoff tells the next story what changed and how to roll it back. Learnings are logged so the next agent doesn't repeat the mistake.

<p align="center"><img src="docs/assets/who-decides.png" alt="Who decides: agents collect evidence, the judge scores it, rules decide, a human accepts" width="100%"></p>

---

## Setup in about 10 minutes

<p align="center"><img src="docs/assets/setup.png" alt="Five setup steps: add story-gate, name the humans, add the judge key, give the AI its own ID, prove it works" width="100%"></p>

You do this once per repository. If you don't want to type commands, open your AI tool in the repository and say **"set up story-gate using the README"**. It runs each step and stops whenever a click has to be yours.

**Step 1 · Add story-gate** (2 minutes)
```bash
cp -r story-gate/.story-gate your-repo/      # or download this repo and copy the .story-gate folder
cd your-repo
python3 .story-gate/gate.py install           # Windows: python .story-gate\gate.py install --python python
```
Commit what it adds (the hooks, the skill and two workflows) and merge it.

**Step 2 · Name the humans** (2 minutes, run it as yourself)
```bash
gh auth login                                 # once, if you haven't
python3 .story-gate/gate.py setup-repo        # add teammates with --owners you,teammate
```
This step:
- Writes `.github/CODEOWNERS` with your username.
- Adds a branch rule that requires a code owner's approval on the latest commit, the `story-gate` check, and resolved review threads.
- Stops GitHub Actions from approving pull requests.

Commit and merge the CODEOWNERS file. If your account can't create rules through the API, import `docs/story-gate-ruleset.json` in **Settings › Rules › Rulesets › New › Import**.

**Step 3 · Add the judge key** (2 minutes)
1. Create a key at openrouter.ai. Jev costs fractions of a cent per check.
2. In your repository, open **Settings › Secrets and variables › Actions › New repository secret**.
3. Name it `OPENROUTER_API_KEY` and paste the key.

Using a different judge? See [Fallbacks](#fallbacks).

**Step 4 · Give the AI its own GitHub identity** (3 minutes; only if AI tools run on your computer)
```bash
python3 .story-gate/gate.py setup-agent       # opens GitHub: click Create, then Install on your repos
python3 .story-gate/gate.py agent-env --repo you/your-repo   # paste the output into the AI tool's terminal
```
Your AI then pushes and opens PRs as **story-gate-agent[bot]**, never as you.

Cloud agents need no setup here, because they already have their own GitHub identity: Codex cloud, Cursor Cloud and Copilot.

**Step 5 · Prove it works** (1 minute)
1. Ask your AI to open a small test PR. The `story-gate` check should stay red until **you** approve the latest commit.
2. Run `python3 .story-gate/gate.py doctor --repo you/your-repo --strict`. It must finish without failures.

> **Free GitHub plan?** GitHub only enforces branch rules on **private** repositories on paid plans (Pro, Team, Enterprise). story-gate still runs everywhere, but on a free private repository every check says **ADVISORY – NOT ENFORCED**. An audit job opens an issue if anything is merged without your approval.

---

## C. Using it in each client

**Judging never happens in the client's model.** `gate.py` and the judge do the scoring, so every client gets the same verdict. What differs is how much happens automatically. **In every row, the CI check is the backstop.**

| Client | How it learns the rules | Blocks edits before READY | Automatic checkpoints | Blocks "done" before DONE | Cheap sub-agents |
|---|---|---|---|---|---|
| **Claude Code** | `.claude/skills`, CLAUDE.md | Yes, including shell writes | Yes | Yes | Yes: `small`=haiku, `medium`=sonnet |
| **Codex CLI** | `.agents/skills`, AGENTS.md | Yes (trust the project `.codex/` folder) | Yes | Yes | Yes, per-agent `model` |
| **Cursor** | `.agents/skills`, rules | Yes, including shell commands | Yes | Yes | Yes, per-agent `model` |
| **Gemini CLI** | `.agents/skills`, GEMINI.md | Yes, including shell commands | Yes | Yes | Yes (Flash models) |
| **Windsurf / Devin** | `.agents/skills` | Yes | Run `checkpoint` | Warn only | Pick the model in the UI |
| **Grok Build** | `.claude/skills`, AGENTS.md | Yes (`/hooks-trust`) | Run `checkpoint` | Yes | Not confirmed |
| **pi** | AGENTS.md | Through CI (a pi extension can add hooks; not shipped yet) | Run `checkpoint` | Through CI | Any model you configure |
| **Hermes Agent** | A Hermes skill + AGENTS.md | Through CI | Run `checkpoint` | Through CI | Any model you configure |
| **Muse Code** | `.agents/skills`, AGENTS.md | Not confirmed | Run `checkpoint` | Through CI | One model family: steps run inline |
| **Claude Cowork** | The story-gate skill | No (hooks don't run there) | Run `checkpoint` | Through CI | Yes |
| **Cursor Cloud, Codex cloud** | AGENTS.md | No (hooks don't run in the cloud) | Run `checkpoint` | Through CI | Built in |
| **ChatGPT, grok.com, chat-only bots** | Not possible (they can't run commands) | No | No | Through CI, once the code reaches a PR | — |

**LM Studio and Ollama** host models; they aren't coding agents. Use them as the model behind pi or Hermes, or as a local, advisory judge (see Fallbacks).

**Models are tiers, not names.** In `config.json` → `models`, each step uses one of two tiers:
- `small`: the cheapest fast model your client offers.
- `medium`: its standard coding model.

We never use frontier models for gate work.

**Day to day, the commands are the same everywhere:**
```bash
gate.py start VIA-12          # the agent fills story.md, context.md, tests.json
gate.py score VIA-12 ready    # READY
gate.py checkpoint VIA-12     # while building (automatic where hooks exist)
gate.py record-tests VIA-12   # runs the pinned test command
gate.py score VIA-12 done     # DONE, writes trace.md
gate.py status                # one line, including "out of date" warnings
```
Then the agent opens the PR. CI repeats everything, and **you approve and merge**.

**Code review fits in two places:**
- **Self-check, before the PR:** the agent runs its client's reviewer. In Claude clients, that's `/engineering:code-review`.
- **Proof, on the PR:** the independent reviewers in `config.json` → `reviewers` (CodeRabbit and Codex by default). The check fails while any of their threads are unresolved.

## D. What we evaluate today

There are two kinds of check:
- **Structural** checks are facts read from files, git and CI. They can't be waived.
- **Judge** checks are a probability: 0.7 or above passes, 0.4 or above is a concern, anything lower fails.

### READY: is the story fit to build?
| Check | Type | Question |
|---|---|---|
| `story_present`, `story_source` | Structural | The story is copied verbatim and says where it came from |
| `context_present` | Structural | PRD, TRD, upstream handoffs and prior learnings are filled ("None — searched: …" is allowed) |
| `test_matrix` | Structural | Every AC has positive, negative, edge **and** regression cases, or a reason one doesn't apply |
| `upstream_handoffs` | Structural | Every `depends_on` story has a handoff |
| `dor_value`, `dor_scope`, `dor_interfaces`, `dor_dependencies`, `dor_nfr`, `dor_small`, `dor_no_blockers` | Judge | Definition of Ready: value, scope, interfaces, dependencies, NFRs, small enough, no blockers |
| `ac_testable`, `ac_covers_scope`, `tests_plan_adequate` | Judge | ACs are pass/fail, cover the scope, and the test plan is meaningful |
| `drift_prd`, `drift_trd` + direction | Judge | Consistent with the PRD and TRD? If not, which side should change? |
| `learnings_applied` | Judge | Relevant past learnings were used |

### CHECKPOINT: still on course?
| Signal | Meaning |
|---|---|
| Per-AC progress | Done, partial or todo for each AC. The average gives an **estimated** % complete |
| `on_course`, `no_unplanned_scope` | Heading toward the ACs and the TRD, with no extra features |
| `no_deferrals`, `tests_keeping_pace` | No TODO stubs, and tests written alongside the code |
| Drift direction | Has the work started to deviate? |

The result is one of three: **ON_TRACK**; **AT_RISK** (fix the named cause); or **OFF_COURSE** (stop, correct the work, or escalate).

### DONE: delivered to spec?
| Check | Type | Question |
|---|---|---|
| `ready_gate_passed` | Structural | READY passed, and neither the story nor the PRD/TRD has changed since |
| `tests_ran_green` | Structural | The pinned test command passed on **this exact code**. CI uses its own run |
| `traceability` | Structural | Every AC's `test_refs` exist in the current test files and ran and passed (writes `trace.md`) |
| `handoff_written` | Structural | All seven handoff sections, including release/rollback and drift decisions |
| `learnings_recorded`, `code_changed`, `evidence_complete` | Structural | Learnings logged, real code in the diff, and the diff small enough for one judge pass |
| `impl_matches_acs`, `impl_no_unplanned_scope`, `drift_trd_after` | Judge | The code does what the ACs say, nothing more, and follows the TRD |
| `tests_implemented`, `no_deferrals` | Judge | The tests written match the plan, with no deferred work |
| `handoff_out`, `learnings_specific` | Judge | The handoff is usable by a stranger, and the learnings are specific |

### ACCEPTANCE: in CI, on GitHub
| Check | Question |
|---|---|
| Human acceptance | A code owner (a person, not a bot, not the PR's author or last pusher) approved the **current** head commit |
| Independent review | The configured reviewers looked at it, with no unresolved threads |
| Enforcement | Do GitHub's branch rules actually block the merge? If not, the check says **ADVISORY – NOT ENFORCED** |
| Audit (after merge) | Anything merged without that approval, or merged by a bot, opens an issue |

**Drift is never fixed silently:**
1. The gate raises **ESCALATED** and notifies your channels.
2. The architect, the orchestrator and the owning session review it.
3. A person decides.

Waivers and drift decisions written by an agent are **proposals**. They count only after a code owner approves the commit that contains them.

## E. Turning things on and off

<p align="center"><img src="docs/assets/rollout.png" alt="Rollout ladder from warn mode to full enforcement" width="100%"></p>

All settings live in `.story-gate/config.json`. CI always reads the copy on your main branch, so a pull request can't loosen its own rules.

| Setting | Default | What changing it does |
|---|---|---|
| `mode` | `"warn"` | `"enforce"` blocks at every point below. `"warn"` reports everything and blocks nothing |
| `enforce_points` | `[]` | Block only at the listed points while still in warn mode: `"ci"`, `"pre_edit"`, `"checkpoint"`, `"stop"` |
| `accept_concerns` | `false` | `true` lets CONCERNS count as passing. Not recommended |
| `test_command`, `junit_path` | empty | Pin the real test suite (e.g. `pytest --junitxml=reports/junit.xml`). **Strongly recommended:** CI runs exactly this |
| `spec_files` | empty | PRD/TRD files to fingerprint at READY (also taken from a `repo` source) |
| `test_globs` | common patterns | Where tests live, so `test_refs` resolve only to real test files |
| `checkpoint.every_edits` | `10` | How often automatic checkpoints run. `0` turns them off |
| `thresholds.pass` / `.concerns` | `0.7` / `0.4` | Stricter or looser. Tune with `gate.py label` data |
| `judge.provider` | `openrouter` | `jev-direct`, `decisions-proxy` (LiteLLM etc.), `openai-compatible` (any model, capped), or `none` |
| `judge.emulated_allow_pass` | `false` | Lets a non-Jev judge award PASS, but only after `gate.py judge-calibrate` passes |
| `judge.temperature` | not sent | Sent to an `openai-compatible` judge only if you set it. Some reasoning models reject it |
| `reviewers`, `require_independent_review` | CodeRabbit, Codex · on | Whose reviews count as independent, and whether one is required |
| `approvers` | `[]` | Extra human approvers on top of CODEOWNERS |
| `sources`, `sinks` | Linear, repo, control-hub, custom | Where specs come from, and where verdicts, checkpoints, drift alerts and learnings are sent |

**Recommended rollout:**
1. Warn mode for a few weeks.
2. Add `ci`.
3. Add `pre_edit`.
4. Add `checkpoint` and `stop`.
5. Switch to `"mode": "enforce"`.

## Fallbacks

<p align="center"><img src="docs/assets/fallbacks.png" alt="Judge trust tiers: Jev on any route can pass; any other model is capped at concerns; no judge means human review only" width="100%"></p>

| You don't have… | What happens |
|---|---|
| **Jev through OpenRouter** | Use the TypeSafe direct API (`judge.provider: "jev-direct"`, key in `TYPESAFE_API_KEY`), or LiteLLM passing Jev through (`"decisions-proxy"`). Still full trust, as long as the answer really comes from a Jev model |
| **Any Jev access** | Any OpenAI-compatible model, including through LiteLLM or a local LM Studio or Ollama model of 7B or larger: `"openai-compatible"` with `base_url` and `model`. It's labelled **emulated** and capped at CONCERNS until `gate.py judge-calibrate` passes and you opt in. Use a different model from the one that writes the code |
| **Any judge** | Structural checks, real test runs and traceability still apply. A human reviews the rest. Nothing PASSES on its own |
| **Judge credit or availability** | The check says "judge unavailable" and blocks. It never quietly passes |
| **A paid GitHub plan** (private repo) | Everything runs, but merges aren't blocked. Every check says **ADVISORY – NOT ENFORCED**, and the audit job flags unapproved merges |

## Known limits

- **Hook locations:** hooks other than Claude Code's assume they start in the repository's top folder.
- **Pilot status for some clients:**
  - Grok Build's and Muse's hook formats are partly unverified.
  - Cursor and Grok also read Claude's hook file, so a warning can show twice.
- **Shell writes:** detection is best-effort. Opaque scripts can still write files, and the CI check is what catches them.
- **Judge output:** the judge returns scores, not reasons, and % complete is an estimate.
- **Teams in CODEOWNERS:** team entries (`@org/team`) aren't resolved yet. List people, or use `approvers`.
- **Stop hook in enforce mode:** it blocks the agent from ending the session up to 3 times in a row, then lets it end so a stuck agent can't loop forever. The PR check still blocks the merge.
- **First PR:** the PR that adds story-gate is checked by human review only, because CI never runs gate code taken from a PR.
- **Untrusted branches:** hooks run `.story-gate/gate.py` from the checked-out branch, just as tests and package scripts do. Only run an AI agent with hooks on branches you trust. CI is unaffected: it always runs the base branch's copy.
- **Agent key:** the agent App's key lives on your computer. It can only act as the agent, never as you, and it can't edit CI or branch rules.

## Tests

```bash
python -m unittest tests/test_gate.py
```
The suite runs on Linux and Windows, with Python 3.9 and 3.12.

<sub>story-gate is MIT licensed. by Viaknox.</sub>
