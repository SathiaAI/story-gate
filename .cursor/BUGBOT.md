# Review focus for story-gate

story-gate is a security gate for AI coding agents. Prioritise, in this order:

1. **Bypasses.** Any way a branch, a pull request or an AI agent's shell command can switch off or route around a check: hooks, the checkout filter (`sg_guard.py`), policy read from the default branch, admin-only commands in `gate.py`.
2. **Fail-open paths.** Errors, timeouts or unexpected input that let an edit or merge through instead of blocking.
3. **Secrets.** Tokens or keys printed, logged, written into the repository, or sent anywhere unexpected.
4. **Data loss.** Install, uninstall or the filter changing a user's files without restoring them exactly.
5. **Portability.** Windows paths and quoting, CRLF, non-UTF-8 text, Python 3.9 compatibility (stdlib only).

Skip style nits. For each finding give a concrete failure scenario and a minimal fix.
