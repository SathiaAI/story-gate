# Budgets

A budget is a limit you set on something a command can count. For example: the app bundle is at most 250 KB, no
accessibility test fails, or the build has at most 10 warnings. When a story changes the code, story-gate measures every
budget that applies. DONE then shows whether the work stays within the limits.

This is the **budget foundation** of Guardrails (G1, part 1). Guardrail areas (security, performance, data,
accessibility), SARIF scanner results and price checks come in part 2. The design is in
[measured-checks.md](measured-checks.md).

## What you decide

| You choose | Example | Why it matters |
|---|---|---|
| The limit | at most 250 KB | The number DONE checks against |
| Decided or proposed | decided | A missed **decided** budget fails DONE. A missed **proposed** budget is CONCERNS. |
| Where the limit came from | "product owner, 2026-10: mobile users on slow networks" | Shown next to every result, so nobody has to guess. It is stored in the repository and shown on the dashboard: name a role or a ticket, not a person's name or email |
| Which changes it applies to | `src/**` | Leave it empty and the budget applies to every change |

Budgets count things. Timings (seconds, minutes) are refused, because the same code runs faster or slower from one run
to the next, and the check would fail at random.

## How a budget starts

1. **Your AI proposes it in a story.** It records the budget and measures it straight away:

   ```
   story-gate measure SAT-7 --budget bundle-kb --unit KB --limit 250 \
     --source "owner asked for a small bundle for mobile users" -- node scripts/bundle-size.js
   ```

   The command prints only the number (here `245` or `245KB`). The proposal lives in the story's folder. It can never
   decide anything: until it reaches the default branch, its results are **reported** and a miss is CONCERNS.

2. **You make it one of the project's budgets.** In a pull request of its own:

   ```
   story-gate settings budget add SAT-7 bundle-kb      # copied to .story-gate/budgets.json, still "proposed"
   story-gate settings budget decide bundle-kb         # now a miss fails DONE
   ```

   Every `settings budget` command opens a pull request and changes nothing on your computer. A code owner approves it.
   Like every story-gate file, `budgets.json` can't change in the same pull request as code.

3. **Change it later** the same way: `story-gate settings budget set bundle-kb limit 240`, or `... remove bundle-kb`.
   `story-gate settings budget` lists the current budgets.

## What CI does

On every pull request with a story:

1. A **measure** job of its own measures each of the default branch's budgets that apply, with the command from the
   default branch. It runs on a separate runner from the project's tests and scenarios, so their code can't touch the
   measurements. That job has no secrets.
2. The story-gate job reads the output, finds the number with its own code and compares it with the default branch's
   limit. It never takes a result the pull request wrote.
3. The check summary shows a table: budget, limit, measured value, how sure it is, and within or over.

| How sure | Meaning |
|---|---|
| **checked by CI** | CI ran the default branch's command on this code, and the files that decide how it's measured are unchanged |
| **reported** | A value CI can't vouch for: measured on the AI's computer, a budget the story proposed, or a pull request that changes the measuring script |
| **not run** | No usable value: the command timed out, crashed (stopped by a signal), printed two numbers or none, wrote more than 5 MB, or CI ran out of time (15 minutes for all budgets) |

A decided budget passes only when it is **checked by CI** and within the limit. Anything else fails DONE, and
`accept_concerns` doesn't change that. In `warn` mode CI reports it; in `enforce` mode it blocks the merge.

## Pins: protecting the measuring script

A pull request can change the script that does the measuring, for example so it always prints 0. So each budget can
**pin** the files that decide how it measures: the script, `package.json`, a config file. If a pull request changes a
pinned file, the result is **reported** and the decided budget fails. Change the script in a pull request of its own
first.

- story-gate pins the program itself when it is a repository file (`./scripts/count.sh`), and the script after an
  interpreter (`python scripts/count.py`, `node scripts/size.js`).
- Add the rest yourself: `story-gate settings budget set bundle-kb pins scripts/bundle-size.js,package.json,webpack.config.js`.
- `npm run size` runs whatever `package.json` says, so pin `package.json` for a budget like that. A budget whose command
  is a runner like that (`npm`, `npx`, `yarn`, `make`, `pytest`, ...) or runs a module or inline code (`python -m`,
  `node -e`) with no `pins` at all is always **reported**, because story-gate can't tell which files to protect.
- A script can load other repository files: modules it imports, a config it reads. Pin those too, or pin the whole
  folder (`scripts`): a pinned folder covers every file in it.

**What CI can't prove.** Running a command again proves it ran on this code. It doesn't prove the command measures the
right thing. A measuring command that runs the pull request's own code (its tests, say) can be fooled by that code,
which can even outlive the command on the measure job's runner; reviewers see such code in the diff. Pins stop a pull request from quietly changing the measuring script; they can't check that the script was
right in the first place. Read a budget's command before you decide it.

## Writing a measuring command

- **Print one number.** The whole output may contain exactly one word with a digit in it. Use quiet modes
  (`npm run --silent size`) so banners and version numbers don't add more. Colour codes are ignored.
- Plain decimals only: `245`, `0.75`, `-3`. No commas, exponents or other units. The budget's own unit may be glued on
  (`245KB`).
- **JUnit:** `--measure junit --report reports/a11y.xml` reads a JUnit XML report the command writes. The value is the
  number of failed test cases (failures and errors), each counted once. A report with no tests is **not run**.
- Write the report to an ignored path. A run that creates or changes files the repository tracks doesn't count.
- **Exit codes:** any exit code is fine as long as the output has the number (many tools exit 1 when they find
  something). Add `--exit 0` if another exit code means the tool broke.
- Time limit: 120 seconds by default (`--timeout`, at most 600).
- Size limits: the command is stopped once its output passes 5 MB, and on Linux and macOS it can't write any file
  larger than 1 GB (a build that needs bigger files can't be a budget's command).

## Fields in `.story-gate/budgets.json`

```json
{"version": 1, "budgets": [
  {"name": "bundle-kb", "unit": "KB", "limit": 250, "direction": "max", "tolerance": 0, "status": "decided",
   "source": "owner, for mobile users", "measure": "number", "command": ["node", "scripts/bundle-size.js"],
   "pins": ["package.json"], "paths": ["src/**"], "timeout": 120}
]}
```

| Field | Meaning |
|---|---|
| `direction` | `max` (default): at most the limit. `min`: at least the limit, e.g. a test count |
| `tolerance` | Extra room, same unit. Within means `value <= limit + tolerance` (or `>= limit - tolerance`) |
| `measure` | `number` or `junit` (`junit` needs `report`) |
| `paths` | Globs. The budget applies when a changed, added, deleted or renamed file matches one. Empty: every change |
| `expect_exit` | Leave it out to accept any exit code |

An invalid `budgets.json` stops the gate until it is fixed (it fails closed).

## Looser budgets are flagged

When a pull request changes `budgets.json`, CI compares it with the default branch. It warns, in plain words, when a
change removes a decided budget, makes one proposed again, raises a `max` limit (or lowers a `min` one), raises the
tolerance, narrows `paths`, drops a pin, or changes how a budget is measured.

## History on the dashboard

Results are saved with the story (`budget_results.json`), so they reach the default branch when the story merges. The
dashboard's **Budgets** table shows each budget's limit, the latest value, the last five values and how many were over
(the last 50 values from merged stories). Output kept in the records is cut to 4,000 characters and has known secret shapes
removed. The values the dashboard shows are the ones the AI recorded (marked *reported*). CI measures every decided
budget again on the pull request, but it doesn't commit its runs, so the dashboard never claims a value was checked by CI.

## Upgrading

Budgets need the new **measure** job in the story-gate workflow. Repositories that use story-gate's reusable workflow
get it when they move to the release with budgets. Repositories with a copied workflow need `story-gate install` once.
Until then CI says so, and a decided budget fails rather than being skipped.
