# Measured checks: engine design

One page for maintainers. It fixes the contract that budgets (G1 PR 1) and guardrail areas (G1 PR 2) share. The owner's
guide to budgets is [budgets.md](budgets.md).

## Threat model

The AI that writes the code can also run commands, write story files and change scripts in its branch. Assume it may
try to make a check pass without doing the work. So:

- Rules come from the default branch only. A branch can add stricter checks, never weaken or remove one.
- The number that decides a check is parsed and compared by story-gate's trusted copy, never taken from a file the
  branch wrote.
- A measurement is only as honest as the code that produces it. CI running a command again proves it ran on this code.
  It does not prove the command measures the right thing. Pins (below) protect the measuring script, not the code
  being measured.

## Check states

| State | Meaning | Counts as |
|---|---|---|
| `checked` | CI ran the default branch's command on this code, with its pinned files unchanged, and parsed one value | evidence |
| `reported` | The value came from somewhere CI can't vouch for: the agent's computer, a budget the story proposed, or a run where a pinned file changed | information |
| `not_run` | No usable value: never run, stale (other code or another command), timed out, crashed, output malformed or too large, or CI ran out of time | nothing |

A decided budget needs `checked` in CI. `reported` or `not_run` on a decided budget fails DONE, whatever `accept_concerns`
says. A proposed budget that is missed or not measured is CONCERNS. In warn mode CI reports failures; in enforce mode
they block.

## Records

- `.story-gate/budgets.json` (policy, default branch only, changed through `story-gate settings budget ... --pr`):
  `{"version": 1, "budgets": [{name, unit, limit, direction, tolerance, status, source, measure, command, report,
  pins, paths, timeout, expect_exit}]}`. Unknown fields or an invalid value make the whole file invalid: the gate fails
  closed.
- `.story-gate/stories/<ID>/budgets.json`: budgets the story proposes. Always `proposed`.
- `.story-gate/stories/<ID>/budget_results.json`: `{"v": 1, "story", "results": {name: result}}`. A result has
  `state`, `value` (decimal text), `unit`, `exit_code`, `seconds`, `note`, `spec` (hash of the command identity),
  `parser`, `fingerprint` (the code), `pins` (what changed), `source` (`local` or `ci`), `at` (UTC) and a redacted
  `output_tail`. The limit is never stored as evidence: the comparison is recomputed from the current policy.

## Execution contract (shared with scenarios)

- One runner: `sg_validation.execute`. No shell, the repository as working folder, stdin closed, the whole process tree
  killed at the time limit, output read from a file (not a pipe), at most 5 MB. Over 5 MB, a timeout, or a failure to
  start is `not_run`.
- The command is an argv list from the policy. The program is found on `PATH`; one that resolves inside the repository
  must be pinned, or the result is `reported`. The program itself (when it is a repository path), the script after an
  interpreter and files an interpreter option loads (`node -r ./setup.js`) are pinned automatically; every other file
  only when `pins` lists it. When story-gate can't tell which file runs (`python -m`, inline code, `npm run`, `make`),
  the budget needs `pins`, or its result is `reported`.
- Exit codes: by default any exit code, but the output must parse. With `expect_exit`, another exit code is `not_run`.
  A run that is killed (a timeout or a signal) or prints nothing parseable is `not_run`, never a pass.
- A run that creates or changes files in the work is void (`not_run`), as for scenarios.
- CI order is fixed: a measure job of its own (no secrets, never the project's tests or scenarios) runs decided budgets
  first, then proposed ones, each sorted by name, inside a 900-second total. Anything left is `not_run` with its own line.

## Parsers

- `number`: ANSI colour codes are removed, then exactly one word in the whole output may contain a digit, and that word
  must be a plain decimal (`-12`, `245`, `0.75`), optionally with the budget's unit glued on (`245KB`). Two numbers,
  none, commas, exponents or another unit: `not_run`. Values are compared as exact decimals.
- `junit`: the XML file at `report` (inside the repository, deleted before the run, must be written by it). DTDs and
  entities rejected, 5 MB cap. The value is the number of `testcase` elements with a `failure` or `error`, counted once
  each, so nested suites never double-count. A report with no test cases is `not_run`.
- Time units (`s`, `ms`, `seconds`, `minutes`, ...) are refused: budgets count things; timings flake.

## Trust boundary in CI

1. `runtime-prepare` copies the default branch's `config.json` and `budgets.json` before any PR code runs.
2. The measure job (no secrets, its own runner, no tests or scenarios) runs the budgets that apply and saves, per
   budget: exit code, the capped raw output or report (secret shapes removed), the time and the command hash. Nothing
   else. The commands run as a separate throwaway account (`sg_isolate.py`): it can't write the saved output, and all
   its processes are stopped, and the stop verified, before story-gate reads anything. Where isolation can't be set up,
   `ci_isolation` decides: strict measures nothing, relaxed measures and marks every value reported.
3. The story-gate job (secrets, never runs PR code) recomputes which budgets apply, which pinned files changed (from its
   own checkout), parses the saved output with its own parser and compares it with the default branch's limit. A saved
   run for another command hash is ignored.

## Applicability

A budget applies when the change touches code and either it has no `paths`, or a changed path matches one of its globs
(`fnmatch`). Changed paths come from the merge base: added, modified, deleted, both sides of a rename, and untracked
files. Story records don't count. An empty diff applies nothing.

## Weaker and stricter rules

Compared with the default branch, a `budgets.json` change is a weaker rule when it: removes a decided budget, turns
`decided` into `proposed`, raises a `max` limit or lowers a `min` limit, raises the tolerance, narrows or adds `paths`,
changes the command, report, parser, unit, direction or exit rule, or removes a pin. CI prints it as a warning; like any
story-gate file, `budgets.json` changes in a pull request of its own that a code owner approves. Stories can only add
proposed budgets in their own folder.

## Evidence binding

The DONE evidence hash includes the policy's `budgets.json` hash, the story's proposals, each stored result's `spec`,
`value` and `fingerprint`, and the code fingerprint. Not the state or who measured it: CI's run of the same code is the
same evidence, and CI works the state out again itself. A change to the code, the command or the default branch's policy makes DONE out
of date. CI always recomputes from the current default branch, so a policy change before merge applies on the next run.
GitHub doesn't re-run a pull request's check when the default branch changes; requiring branches to be up to date
before merging makes that next run happen.

## History

Results are committed with the story, so the dashboard reads them from merged stories: value, limit, unit, state, source
and date per budget, the last 50 per budget. Output tails are redacted (secret-named variables and token shapes) before
they are stored. They live as long as the story folder.

## Guardrail areas (G1 part 2)

Owner's guide: [guardrails.md](guardrails.md). Areas live in the default branch's `config.json` (`guardrails.areas`), not
in `budgets.json`, and reuse this contract through an adapter (`sg_areas.records`): each check becomes a decided record
with direction `max` and tolerance 0, keyed `area/check`, plus its area and mode. Nothing above changes for budgets.

- **Modes.** `off`: no record. `warn`: problems are a `WARN` status on the DONE check `guardrails_met`, shown and never
  counted in the overall verdict. `enforce`: same rule as a decided budget: `checked` and within the limit in CI, or it
  fails, whatever `accept_concerns` says. Area results never enter `budgets_met`.
- **Applicability.** From the trusted merge-base diff (the same one budgets use), worked out in the measure job and again
  in the story-gate job: no triggers means every change; `paths` match changed paths (fnmatch); `content` patterns match
  added or removed lines outside story records. Patterns are validated (<=200 characters, no backreferences or
  lookaround, no nested repeats) and matched in a separate Python process with a 10-second limit. A diff that can't be
  read, is over 5 MB, or can't be matched in time makes the areas apply.
- **Order and time.** One 900-second budget for everything the measure job runs: decided budgets, enforce-area checks,
  warn-area checks, proposed budgets; each by name. A check reached after the time ran out is `not_run` with that reason.
- **Parsers.** `sarif` (SARIF 2.1.0 JSON; kind `fail` or absent; level from the result, its rule's
  `defaultConfiguration`, else `warning`; `baselineState: absent` never counts; suppressions in effect are counted
  unless the check says `suppressed: exclude`; `executionSuccessful: false`, a missing `results` list, another version
  or any malformed field is `not_run`) and `date` (one ISO-8601 date or date-time, UTC when no zone; the value is the
  age in whole UTC calendar days, recomputed from the stored `as_of` date whenever it is read; a future date is
  `not_run`). `levels` and `suppressed` are part of the record's `spec` hash. `junit` and `number` as above.
- **Records.** `.story-gate/stories/<ID>/guardrail_results.json`: `{"v": 1, "story", "results": {"area/check": result}}`,
  the same result shape as budgets plus `findings`/`suppressed` (SARIF) or `as_of` (date). The measure job saves raw
  output under `measures/areas/<area>/<check>.raw`.
- **Weaker rules** (`sg_areas.weaker`, reported through `sg_trust.weaker`): an area removed or its mode lowered; any
  glob or pattern removed (containment of a new pattern can't be proven); a check removed; a limit raised; fewer SARIF
  levels; `suppressed` count to exclude; a pin dropped; command, report, unit, exit rule or time limit changed. Local
  settings may only raise an area's mode. Stories can't add area checks (they propose budgets instead).
- **What it can't prove.** A date check proves the recorded date's age, not that the prices are correct; pins keep a
  pull request from changing the price data and its date without its own review.
