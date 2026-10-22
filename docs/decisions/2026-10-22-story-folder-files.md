# Files in a story folder: only the gate's records are left out (2026-10-22)

**Problem.** `RECORD_PATHS` in `.story-gate/gate.py` listed the whole `.story-gate/stories` tree. So every file under
`.story-gate/stories/<ID>/` was left out of every check: the changed-file lists, the diff the broad judge reads, the narrow
judge's packs (PR #99), the guardrail areas, the budgets' paths and the work fingerprint that makes a verdict go stale.
The narrow judge's `story_record()` can tell the gate's records apart from other files, but it never saw the other files,
because the diff had already dropped them. Raised by Codex on PR #99 (comment 4347633099). It was kept out of that PR
because the rule is gate-wide and older than the PR.

**Threat: can code hide there and still run?** Yes.

1. A pull request puts `.story-gate/stories/SAT-1/util.py` (or `util.js`) in the story folder, and ordinary code imports it:
   `sys.path.insert(0, ".story-gate/stories/SAT-1")`, or `require("../.story-gate/stories/SAT-1/util")`. Node's CommonJS `require` also loads
   a file with an unknown extension as JavaScript, so even `notes.md` can be required.
2. The importing line is visible, but the module's body is not. The judges score a diff without it, a guardrail's content
   pattern (secrets, auth, SQL) never reads it, and the scope check never lists it.
3. After DONE passes, the module can be changed without making the verdict stale, because the work fingerprint skipped it.
   That is the worst part: an approved story could change behaviour after its approval.
4. The local edit guard blocks an agent's edit tools and most shell writes to non-Markdown files there. But it is a
   convenience on the agent's computer, not the merge check: it lets Markdown through, an opaque script gets past it, and
   a pull request from anywhere else never meets it. CI is the check that counts, and CI used the same rule.

**Options.**

- **(a) Keep the rule and warn when a non-record file is added there.** Pros: no change to what the judges read; small.
  Cons: a warning in `warn` mode is easy to miss; a changed file (not an added one) slips past; the fingerprint hole in
  point 3 stays; and the gate would know the file is code yet still not judge it.
- **(b) Leave out only the gate's own record names, for every check at once.** Pros: one rule, in one place, for every
  check; the hidden module is judged, guarded, scoped and fingerprinted like any other file; nothing new to remember.
  Cons: the record list must be complete. A name the gate writes but the list misses would count as work, so writing that
  record would change the fingerprint and make a verdict stale as soon as it was written.

**Decision: (b).** A non-record file in a story folder is code evidence, wherever it sits. The gate's records are:

- `story.md`, `context.md`, `tests.json`, `handoff.md`, `validation.md`, `trace.md`, `drift.md`
- `scenarios.json`, `scenario_results.json`, `test_results.json`, `budgets.json`, `budget_results.json`,
  `guardrail_results.json`, `checkpoints.jsonl`, `decisions.jsonl`, `coder.json`, `pitfalls.json`, `evidence.json`
- `ready.json`, `done.json`, `ready.self.json`, `done.self.json`
- copies pulled with `spec-pull` and the tracker commands: `issue-*[0-9].md`, `linear-*[0-9].md`, `jira-*[0-9].md`
- screenshots: `evidence/*.png`, `evidence/*.jpg`

They are `STORY_RECORDS` in `gate.py`, in git's glob syntax (`*` never crosses a `/`). `is_record()` and the diff's pathspec
excludes (`record_excludes()`) are both built from that one list, so the file list and the diff can't disagree.
Subfolders other than `evidence/`, and files beside the story folders in `.story-gate/stories/`, are work.

**What stays as it was.** Markdown in a story folder still matches the default `exempt_globs`
(`.story-gate/stories/*/*.md`), so a stray `notes.md` doesn't count as a code change that needs tests. But it is now in the
diff the judges and guardrails read, and in the fingerprint. The other `RECORD_PATHS` (learnings, outbox, calibration,
`.active` and so on) are unchanged.

**What a record name can still hide.** A record's own name is still left out, so `require("./story.md")` would run text
nobody judged. That is a much smaller channel: the import is visible in judged code, the name is fixed, and the records
are files people already read in the pull request. Closing it would mean judging the story's own records as code, which
the judges already get as the story.

**Tests.** `TestStoryFolderFiles` in `tests/test_gate.py`:

- the record names are records, and `payload.py`, `notes.md`, `issue-x.md`, `evidence/run.py`, `sub/story.md` are not;
- `diff_against()`: each file's content is in the diff exactly when `is_record()` says it isn't a record, so git's pathspec
  and the Python rule agree; budgets' changed paths list the module too;
- an untracked module counts before `git add`;
- editing the module changes the work fingerprint; writing `ready.json`, `handoff.md` or a screenshot doesn't;
- after `start`, READY, a scenario, a checkpoint and `record-tests`, every file the gate wrote is a record (the guard
  against the risk named under (b)).

Four of the five fail on the old rule. The last is the guard, and it passes on both.

**Follow-up for PR #99.** `sg_decompose.STORY_RECORDS` keeps its own list. Once both are merged it should use `gate.py`'s
list, so the narrow judge and the diff agree. Until then, its list lacks `drift.md`, the `issue-`/`linear-`/`jira-`
copies and `evidence/`. A pulled issue copy would then reach a pack as if it were code: harmless (it is Markdown the judge
can read), but noise. Its list also has `manifest.json`, which the gate writes in the runtime folder, not in a story folder.
