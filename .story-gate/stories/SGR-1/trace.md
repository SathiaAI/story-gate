# Traceability: SGR-1

| AC | Criterion | Planned cases | Automated tests (test_refs) | In current test files | Result |
|---|---|---|---|---|---|
| AC-1 | gate.yml declares an optional workflow_call input `runner`:  | 4 | test_gate_has_an_optional_runner_input_that_defaults_to_ubuntu_latest | 1/1 | GREEN |
| AC-2 | Exactly the `tests` and `measure` jobs of gate.yml set `runs | 4 | test_only_the_tests_and_measure_jobs_run_on_the_runner_input | 1/1 | GREEN |
| AC-3 | The `story-gate` job of gate.yml, audit.yml and dashboard.ym | 4 | test_secret_jobs_and_the_other_workflows_never_use_the_runner_input | 1/1 | GREEN |
| AC-4 | When `runner` is not passed, every job of gate.yml, audit.ym | 4 | test_without_a_runner_every_job_of_every_workflow_stays_on_ubuntu_latest, test_the_published_workflows_are_pinned_and_keep_secrets_away_from_pr_code | 2/2 | GREEN |
| AC-5 | A caller that passes `runner` by hand in the `with:` block o | 4 | test_doctor_accepts_a_caller_that_passes_a_runner_by_hand | 1/1 | GREEN |
