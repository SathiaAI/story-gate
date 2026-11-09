# Validation: SGR-3

## Result
The story is done. `judge.decompose: on` is disabled in Release 1: config loading refuses it with a message that names issue #106, `score` refuses it too and writes no verdict, and `shadow` and `off` behave as before. The docs say so and say why.

## Acceptance criteria
- AC-1: met. `check_config` raises `ConfigError` "judge.decompose: on is disabled in this release until the narrow-check hardening (issue #106) is done. Use shadow or off." `shadow` and `off` load, and the default is still `shadow`. Tests: `test_on_is_refused_loudly_and_shadow_and_off_are_accepted`, `test_bad_decompose_settings_are_a_config_error`.
- AC-2: met. `score` with `on` in the config exits non-zero with the message, writes no `done.json` and sends the judge no request. Test: `test_scoring_with_on_set_fails_and_never_falls_back_to_shadow`.
- AC-3: met. `shadow` records `decomposed`; `off` records none. Test: `test_shadow_and_off_still_score`. The 90+ existing narrow-check tests still pass, with the flag switched on in their own repository copy.
- AC-4: met. The decision record, guide, PROTOCOL.md, README and the `settings` text say `on` is disabled and why. Test: `test_the_docs_say_on_is_disabled_and_name_the_issue`. The PR body was updated by hand (setting values, Known limits, Rollback).

## Scenarios run
- The settings check on the shipped code: `on` returns the message; `shadow` and `off` return nothing.
- The three tests above, run as scenarios.

## Bugs found and fixed
- A first version of the test helper switched `on` back on only after the stale-verdict test had scored, which changed the signed-copy bytes mid-test and made the verdict stale. The helper now switches it on once in `setUp`.
- The merge brought main's signed `release.json` and `.sig` into a branch that changes runtime files. RELEASING.md says such a pull request deletes both, so this branch does. Two tests that check the signature failed until then.

## Lessons learnt
Refuse a disabled setting where the config is loaded and again where it is used, so a path that skips loading cannot switch it on. Make test switches change a fixed line in the test's own copy, not an environment variable that real callers could also set.

## Known limits
- The binding code path is still in the code, switched off. Enabling `on` needs the three items in issue #106 first.
- Runtime and code-owner files change (`gate.py`, `sg_decompose.py`), so Paul approves.

## Demo
Not demo-able: it is a refused setting. To see it, put `"decompose": "on"` under `judge` in `.story-gate/config.json` and run any `score` command.
