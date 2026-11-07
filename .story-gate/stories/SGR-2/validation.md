# Validation: SGR-2

## Result
The story is done. A link under story-gate's copy that the signed release does not list now fails both the release check and the CI signed-copy check, whatever the link is called. A clean copy still passes.

## Acceptance criteria
- AC-1: met. `verify_release` fails for a link to a package folder, a link to a file, a dangling link and a link inside `vendor/`. Tests: `test_an_extensionless_link_to_a_package_folder_fails`, `test_an_extensionless_link_to_a_file_fails`, `test_a_link_inside_a_real_subfolder_fails_but_stories_and_pycache_are_left_alone`.
- AC-2: met. `runtime_copy_check` fails for the same links and passes a copy with none. Test: `test_a_link_the_signed_release_does_not_list_is_refused_whatever_its_name`.
- AC-3: met. Links inside a real `stories/` or `__pycache__` folder are ignored. A link named `stories` or `__pycache__` fails. Test: `test_a_link_called_stories_or_pycache_is_not_left_alone_and_folder_links_are_not_followed`.
- AC-4: met. A folder link is listed as one entry and never followed. Same test.

- AC-5: met. A folder that cannot be listed fails `verify_release` ("couldn't list the folder ...") and gives `runtime_copy_check` a problem. Tests: `test_a_folder_that_cannot_be_listed_fails_verify_release`, `test_a_folder_the_copy_check_cannot_list_is_a_problem_not_a_pass`. The two chmod 000 versions skip on Windows and as root and print why; the mocked versions cover the same code everywhere.

## Scenarios run
- A scratch copy of the release folder with a link `json` to a folder holding `__init__.py`: `verify_release` fails with "links not covered by the signed release: json".
- The same link on the unfixed code (from main): the tests failed with "TrustError not raised" and an empty problem from `runtime_copy_check`. That was the reproduction.

## Bugs found and fixed
The hole itself: before, both checks looked only at importable file suffixes and skipped links. Fixed in `sg_trust.py` and `gate.py`. A first draft would also have ignored a link that is itself named `stories` or `__pycache__`. A test caught the idea and the code ignores only real folders of those names.

## Lessons learnt
Reproduce a security hole with a failing test first. Also check what a skip rule skips: a link can carry the name of a folder that is skipped.

## Known limits
- Runtime and code-owner files change (`gate.py`, `sg_trust.py`), so Paul approves.
- This change deletes no release signature on main (main has none). The next release signs it.
- The README line about links is on the v0.11.0 release branch and is updated there.

## Demo
Not demo-able: this is an internal security check. To see it, run `python3 -m unittest tests.test_gate.TestReleaseSignatures`.
