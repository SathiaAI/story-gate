# Validation: SGR-1
For the owner. Plain English, short sentences (PROTOCOL.md > Writing). story-gate adds the checked facts (tests, scenario runs).

## Result
The story is done. A caller of story-gate's reusable gate workflow can now pass an optional `runner` input. It moves only the `tests` and `measure` jobs, which hold no secrets. If a caller passes nothing, every job runs on `ubuntu-latest`, as before.

## Acceptance criteria
- AC-1: met. `gate.yml` has the `runner` input: string, optional, default `ubuntu-latest`. Test: `test_gate_has_an_optional_runner_input_that_defaults_to_ubuntu_latest`.
- AC-2: met. Only `tests` and `measure` use it. Test: `test_only_the_tests_and_measure_jobs_run_on_the_runner_input`.
- AC-3: met. The `story-gate` job, `audit.yml` and `dashboard.yml` stay on `ubuntu-latest` and never mention the input. Test: `test_secret_jobs_and_the_other_workflows_never_use_the_runner_input`.
- AC-4: met. With the default, every job runs on `ubuntu-latest`. Test: `test_without_a_runner_every_job_of_every_workflow_stays_on_ubuntu_latest`.
- AC-5: met. `story-gate doctor` accepts a caller that sets `runner` by hand. Test: `test_doctor_accepts_a_caller_that_passes_a_runner_by_hand`. See Known limits for what `install` does.

## Scenarios run
- The resolved `runs-on` of every job, with no `runner` and with `runner: ubicloud-standard-2`. Only `tests` and `measure` change.
- A scratch repository: `install --ci reusable`, add `runner:` by hand, run `doctor`. It says ok.

## Bugs found and fixed
None found. Three deliberate breakages of the workflow (a changed default, the secret job moved, `measure` not moved) were each caught by the new tests.

## Lessons learnt
Test the claim from both sides: the jobs that should move do, and the jobs that must not move cannot.

## Known limits
- `story-gate install --ci reusable` rewrites the caller file and drops a hand-added `runner:` line. It has no `--runner` flag. Callers can pass `runner` by hand, but the next install removes it. A fix needs a change in `sg_reusable.py`, which this story does not touch.
- Callers need a new signed release before they can use `runner`. This story cuts no release.
- A non-GitHub runner may not support CI isolation (the throwaway account for PR code). It fails closed. Try one repository first.

## Demo
Not demo-able: this changes a CI workflow file. To see it work after a release, add `runner: ubicloud-standard-2` under `with:` in a caller's `story-gate.yml`. The `tests` and `measure` jobs then run on that runner and the `story-gate` job stays on `ubuntu-latest`.
