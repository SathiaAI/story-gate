# Guardrails

A guardrail area is a group of checks you turn on for one kind of risk: security, performance, data, accessibility, or
anything else you name. Each check is a command you bring (your own scanner, linter, test run or script). When a story
changes code an area covers, story-gate runs its checks and DONE shows the result.

This is Guardrails part 2 (G1). [Budgets](budgets.md) are part 1. Both use the same engine: the same runner, the same
rules for what CI can vouch for, the same CI job. The design is in [measured-checks.md](measured-checks.md).

## What you decide

| You choose | Example | Why it matters |
|---|---|---|
| The area's mode | `warn` | `off`: not run. `warn`: shown on the pull request, never blocks. `enforce`: a problem fails DONE, whatever `accept_concerns` says |
| When it applies | changed files `src/**`, or changed lines matching `password` | No triggers: every change. A change that matches a path or a pattern turns the area on |
| Its checks | your scanner's SARIF report, at most 0 errors | What is counted, and the most you allow |
| Where the limit came from | "owner, 2026-10: no new security errors" | Shown with every result. Name a role or a ticket, not a person |

## Turn an area on

Every change opens a pull request of its own. A code owner approves it. Nothing changes on your computer.

```
story-gate settings area security check scan add --measure sarif --report out/scan.sarif --limit 0 \
  --pins tools/scan.cfg --source "owner: no new security errors" -- ./tools/scan.sh
story-gate settings area security mode enforce        # after a few warn-mode pull requests look right
story-gate settings area security paths "src/**,api/**"
story-gate settings area                              # what the default branch has now
```

Your AI can write the command for you. Add the area in `warn` mode first, look at a few pull requests, then enforce it.

## The four kinds of check

| `--measure` | The command | The value | Default limit |
|---|---|---|---|
| `sarif` | writes a SARIF 2.1.0 report to `--report` | findings at the levels you chose (`--levels error`, default `error`) | 0 |
| `junit` | writes a JUnit XML report to `--report` | failed test cases | 0 |
| `number` | prints one number | that number (`--unit KB`) | 0 |
| `date` | prints one date, like `2026-10-01` | how many days old it is | 0 |

Reports go to an ignored path (for example `out/`). Any exit code is fine as long as the report or the output is
usable; add `--expect-exit 0` when another exit code means the tool broke.

### SARIF in detail

- Counted: results whose kind is `fail` (or not given), at a level you chose. The level is the result's own, else its
  rule's default, else `warning`.
- Not counted: results marked `pass`, and results whose `baselineState` is `absent` (gone).
- **Suppressed findings still count.** Your AI can add a "suppress this warning" comment to the code, so by default
  story-gate counts those findings and says how many were suppressed, and in which files (up to five). Adding a
  suppression therefore never lowers the count. `--suppressed exclude` stops counting
  suppressions that are in effect (status `accepted`, or none). Choosing it is a weaker rule.
- Not usable (the check is *not run*): a run the tool says failed (`executionSuccessful: false`), a run with no
  `results` list, another SARIF version, unknown levels or kinds, malformed suppressions, a report over 5 MB.

### Price age

A `date` check catches old price data in your app: AI model prices, cloud prices, anything you copied from a vendor's
price page. Your small script prints the date those prices were last checked (for example a `checked` field in
`data/prices.json`). story-gate counts the days from that date to today, in UTC, and compares them with your limit.

```
story-gate settings area prices check age add --measure date --limit 30 \
  --pins data/prices.json,tools/price_date.py --source "owner: re-check prices monthly" -- python3 tools/price_date.py
```

- A date check needs `--pins`: the price file and the script. A pull request that changes them gets no credit, so new
  prices and their date go in a pull request of their own, which a person checks.
- Accepted: `2026-10-01`, `2026-10-01T09:30Z`, `2026-10-01T09:30:00+02:00`. No time zone means UTC. A date after today
  (UTC), an impossible date or more than one word is *not run*.
- The age is worked out again every day: data that was fresh last week can be too old today.
- **What it can't prove:** that the prices are right. It checks the date someone recorded, not the prices themselves.

## When an area applies

- The area isn't `off`, and the change touches files (story records don't count).
- No `paths` and no `content`: every change.
- `paths`: a changed file matches a glob (`fnmatch`, forward slashes). Added, changed, deleted, both names of a renamed
  file, and new untracked files all count.
- `content`: an added or removed line matches a pattern. Patterns are at most 200 characters and kept simple: no
  backreferences, no lookahead or lookbehind, no repeat inside a repeat (like `(a+)+`). Every line is matched whole.
- Paths and patterns are part of `config.json`, so anyone who can read the repository can read them. Don't put secrets in
  them (`settings` refuses values that look like keys or tokens).
- When story-gate can't be sure, the area applies: the change couldn't be read (for example no merge base in a shallow
  clone; story-gate's own workflow fetches the full history), it is over 5 MB, it changes a binary file (its content
  can't be matched), or the patterns couldn't be matched within 10 seconds.

## What CI does

1. The **measure** job (no secrets, its own runner, the pull request's code running as a throwaway account) runs the
   checks of the areas that apply, enforce areas first, then warn areas, inside the same 15-minute limit as budgets.
   It saves only the raw report or output.
2. The **story-gate** job works out again which areas apply, from its own checkout, reads each report with its own
   parser and compares it with the default branch's limit. It never takes a value the pull request wrote.
3. The check summary shows a table: area, check, limit, measured value, how sure, within or over. Warn-area problems
   also show as a warning annotation.

| How sure | Meaning |
|---|---|
| **checked by CI** | CI ran the default branch's command on this code, and the check's pinned files are unchanged |
| **reported** | Measured on your computer, or the pull request changed a pinned file |
| **not run** | No usable value: timed out, crashed, an unusable report, or CI's time ran out |

An enforce-area check passes only when it is **checked by CI** and within its limit. A warn-area problem is shown as
**WARN** and never changes the verdict.

## Pins

Pin the files that decide what a check finds: the scanner's config and ignore files, a baseline file, the script, the
price data. A pull request that changes a pinned file gets *reported* for that check, so an enforced area fails until
that file changes in a pull request of its own. story-gate pins the program itself when it is a repository file
(`./tools/scan.sh`) and the script after an interpreter (`python3 tools/x.py`); add the rest with `--pins`. An
**enforced** SARIF or JUnit check must list pins: story-gate refuses to enforce one without them. A scanner that reads an ignore file you didn't pin can be told to
skip findings by the pull request itself: pin it.

## Looser settings are flagged

Compared with the default branch, CI warns when a change removes or lowers an area, narrows its triggers (any glob or
pattern removed), removes a check, raises a limit, counts fewer levels, stops counting suppressed findings, drops a pin
or changes how a check runs. Like every story-gate setting, it needs a pull request of its own and a code owner. A
computer's own `config.json` can only raise an area's mode (warn to enforce), never add or loosen a check.

## Fields in `config.json`

```json
{"guardrails": {"areas": {
  "security": {"mode": "enforce", "paths": ["src/**"], "content": ["password"],
               "checks": [{"name": "scan", "measure": "sarif", "report": "out/scan.sarif", "command": ["./tools/scan.sh"],
                           "pins": ["tools/scan.cfg"], "limit": 0, "levels": ["error"], "suppressed": "count",
                           "timeout": 120, "source": "owner, 2026-10"}]}}}}
```

At most 10 areas, 10 checks per area and 20 paths or patterns per area. An invalid section stops the gate until it is
fixed (it fails closed). Area names are lowercase; check names are letters, digits, `_` and `-`.

## Upgrading

Areas run in the **measure** job that budgets added. Repositories with a copied workflow that predates budgets need
`story-gate install` once; until then CI says so, and an enforced area fails rather than being skipped.
