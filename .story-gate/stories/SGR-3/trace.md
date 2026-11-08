# Traceability: SGR-3

| AC | Criterion | Planned cases | Automated tests (test_refs) | In current test files | Result |
|---|---|---|---|---|---|
| AC-1 | check_config refuses judge.decompose: on with the message na | 4 | test_on_is_refused_loudly_and_shadow_and_off_are_accepted, test_bad_decompose_settings_are_a_config_error | 2/2 | GREEN |
| AC-2 | score with on in the config stops with the message and write | 4 | test_scoring_with_on_set_fails_and_never_falls_back_to_shadow | 1/1 | GREEN |
| AC-3 | shadow records decomposed beside the binding verdict; off re | 3 | test_shadow_and_off_still_score | 1/1 | GREEN |
| AC-4 | Docs that said on can be set now say it is disabled and why  | 2 | test_the_docs_say_on_is_disabled_and_name_the_issue | 1/1 | GREEN |
