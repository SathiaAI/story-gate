# Review lessons

Mistakes that reviewers kept finding in story-gate's own pull requests, and how each one is now prevented. Read this before you open a pull request that changes story-gate. Where a lesson has an automatic check, it lives in `tests/test_gate.py` (`TestReviewLessons`) and fails the test run.

| # | Lesson | Prevented by |
|---|---|---|
| 1 | **Every new or changed function has a docstring.** Reviewers flag each undocumented function a pull request touches. | Automatic, repository-wide: the count of functions without a docstring may only go down (it can't tell which function a PR added). Per function: pre-PR checklist. |
| 2 | **A flag that needs a value refuses to run without one.** `--every` typed last used to be ignored, so the default applied without a word. | Fixed at the root: `flags()` stops with "needs a value". Automatic: every flag a command looks for in `rest` must be listed in `BOOL_FLAGS`. |
| 3 | **A test's name and docstring say what it checks.** A test called "writes only to the temp folder" only checked an error message. | Pre-PR checklist (needs judgment). |
| 4 | **Catch only the error you expect.** Retrying a "no permission" answer, or switching to another login on any exception, hides real failures. | Pre-PR checklist; the author policy (`pr_author`) only falls back when the agent App is missing. |
| 5 | **Docs quote the numbers the code enforces.** The guide said 65,000 characters; the code trims at 60,000. | Automatic: the guide's issue-size limit (60,000) and the screenshot limits are compared with the constants. Add a row when the docs quote a new limit. |
| 6 | **GitHub values reach the shell through `env:`, never inline in `run:`.** Event fields, inputs and step outputs inside `run:` can carry shell code. | Automatic: workflow templates and story-gate's workflows are scanned. Known exceptions may only be removed. |
| 7 | **A one-person repository must be able to finish every flow.** GitHub never counts an approval from whoever opened a pull request. | Fixed: setup-repo waits for CODEOWNERS before the ruleset (#75); one author policy for every pull request story-gate opens (#76). |
| 8 | **Running a setup command twice is safe, and one failed step doesn't stop the rest.** A random branch name opened a second CODEOWNERS pull request on a re-run; a refused pull request skipped the ruleset and Actions steps. | Pre-PR checklist; setup-repo uses one fixed branch and reports a failed step (#76). |
| 9 | **Data from GitHub or a tracker can have any shape; an odd shape means "unknown, play safe", never a crash.** A non-numeric approval count would have stopped setup-repo; a non-object `parameters` would have crashed it. | Pre-PR checklist; `approval_required` treats an unreadable rule as "approval needed" (#76). |

Also learned the hard way:

- GitHub's GraphQL API can report a rate limit inside an HTTP 200 answer; check `errors`, not only the status.
- GitHub ties an ETag to the token that asked, and Actions gets a new token every run, so ETags don't carry from one run to the next.
- A fresh review of the whole codebase found 7 real bugs before v0.9.0: repeat it before each release.

When a reviewer finds something new that could come back, add a row here and, if a machine can check it, a test in `TestReviewLessons`.
