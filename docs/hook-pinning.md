# Pinned project hooks

A repository can ship hooks for AI tools: commands Claude Code, Codex, Cursor, Gemini and Windsurf run automatically
(for example `bash scripts/check.sh` after every edit). story-gate's checkout filter already removes hook *entries*
your default branch didn't approve. This page covers the next gap: an approved hook that runs a **file from the
repository**.

## The problem

Your default branch approves `bash scripts/check.sh`. A branch changes `scripts/check.sh` and nothing else. The hook
file is identical, so git never rewrites it, and the AI tool runs the branch's script on your computer. Approving the
command text isn't enough; the files it runs have to be approved too.

## What story-gate does

`gate.py doctor` shows every hook the default branch approves, in one of four tiers.

| Tier | Example | What happens |
|---|---|---|
| **plain** | `echo done`, `/usr/local/bin/notify` | Runs as is. It uses nothing from the repository. |
| **pinned** | `bash scripts/check.sh` with `"pins": ["scripts/check.sh"]` | Swapped for the story-gate runner on checkout. At run time the runner checks the pinned files against the default branch and runs **the default branch's copy**. If anything under the pins differs, it refuses. |
| **accepted** | `npm run lint` with `"runs_repo_code": "accepted"` | Runs as is. Labelled *unverified*: a code owner decided to trust repository code here. |
| **blocked** | `npm run lint`, `python -m tools.check`, an unpinned script | Removed from the hook file on disk. Doctor shows the exact line to add. |

Anything story-gate can't read with certainty is **blocked**, never plain:

- **Wrappers:** `env`, `time`, `xargs`.
- **Environment settings:** `VAR=...`.
- **Code given inline:** `-c`, `-e`, `-m`.
- **Tools that load code from repository config:** `pytest`, `eslint`, `jest`.
- **Shell features:** pipes, `&&`, `$VARS`.
- **Repository paths hidden inside options.**

## Pinning a hook

In `.story-gate/config.json` **on the default branch** (a branch can't approve its own hooks):

```json
"project_hooks_allowed": [
  "echo done",
  {"command": "bash scripts/check.sh", "pins": ["scripts/check.sh", "scripts/lib"]},
  {"command": "npm run lint", "runs_repo_code": "accepted"}
]
```

- `pins` lists every file or folder the hook uses. Folders are compared in full, including extra and ignored files.
- `gate.py doctor` warns when a pinned script seems to use a repository file that isn't pinned (for example `source scripts/lib.sh`). This is a best-effort hint, not proof.
- Pinned commands must be simple: a program, then plain words and paths. Use forward slashes.

## The runner, step by step

Each time the AI tool fires a pinned hook, story-gate's signed runtime:

1. Reads the policy again from the default branch's commit. The hook must still be approved and pinned.
2. Compares every pinned path in your working tree with that commit by content hash. Edits, deletions, symlinks, and extra or ignored files all count as different.
3. If anything differs, it **refuses** (exit 2) and names the files.
4. Otherwise it runs the command with each pinned path pointing at a **protected copy** taken from the commit. The copy is read-only, kept in your user folder, and re-checked on every use.
5. It removes repository folders from `PATH`. On Windows it uses Git Bash, never WSL's `bash`, and never picks up a program from the repository folder.
6. It passes the hook's input, output and exit code through unchanged.

## Editing a pinned script yourself

While you edit `scripts/check.sh` on a branch, its hook refuses to run. That's the point. To run your own edit:

```bash
gate.py hook-trust "bash scripts/check.sh"           # human only; logged in .git/story-gate-guard.log
gate.py hook-trust "bash scripts/check.sh" --revoke
```

- **Exact content only:** trust covers the exact content of the pinned files at that moment. Change them again and the hook refuses again.
- **Automatic return:** once your edit is merged, the default branch's copy is used again automatically.
- **Agents can't grant it:** an AI agent can't run `hook-trust`.

## What is and isn't protected

| Protected | Not protected (say so in reviews) |
|---|---|
| Pinned files and folders, compared by content on every run | Files a script uses that **aren't** pinned (doctor hints at them) |
| The hook entry itself (checkout filter) | **accepted** hooks: they run repository code by a code owner's choice |
| Programs from the repository folder on `PATH` | Programs installed on your computer, and their own config files |
| The protected copy (read-only, re-verified) | Your local copy of the default branch going stale: run `git fetch`. Doctor warns after 7 days (`policy_max_age_days`). |
| Commands story-gate can't parse | Repositories you haven't enrolled |

**Trusted root:** the default branch as git last fetched it (`refs/remotes/origin/<default>`), plus the signed story-gate runtime in your user folder.

**Tested with:**
- Real Claude Code on Windows, plus the full test suite on Linux and Windows in CI and on macOS by hand.
- Gemini CLI: its hook file is filtered and wrapped, but Google no longer lets personal sign-ins use the command-line tool, so the runner wasn't triggered from Gemini itself.
