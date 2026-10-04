# How each AI tool runs story-gate's hooks

`gate.py install --user` puts story-gate's hooks in each tool's **user** settings and points them at the signed copy in your user folder. Until the first signed release is published, pilots install with `gate.py install --user --unsigned` (doctor then says the copy is unsigned). This table says what each tool's own documentation promises, so we only claim protection where the tool does. Checked October 2026. "Unverified" means the tool's docs don't say.

| | Claude Code | Codex CLI | Cursor | Gemini CLI | Windsurf / Devin | Grok Build | Hermes |
|---|---|---|---|---|---|---|---|
| **User-level hook file** | `~/.claude/settings.json` | `~/.codex/hooks.json` | `~/.cursor/hooks.json` | `~/.gemini/settings.json` | `~/.codeium/windsurf/hooks.json` | `~/.grok/hooks/*.json` | `hooks:` block in `config.yaml` (`HERMES_HOME`; Windows `%LOCALAPPDATA%\hermes`) |
| **User and project hooks both run** | Yes | Yes | Yes (any "deny" wins) | Unverified | Yes | Unverified | No project hooks |
| **A blocking pre-edit hook stops the edit** | Yes (exit 2) | Yes (exit 2) | Yes (exit 2) | Yes (exit 2) | Yes (exit 2) | Yes (exit 2) | Yes (exit 2) |
| **Trust step for project hooks** | Not documented for settings hooks | Yes, per hook, re-asked when a hook changes | Workspace trust | Changed hooks are flagged | Unverified | Folder-level (`/hooks-trust`) | Asks once per hook command (story-gate approves its own when you run setup) |
| **If a hook times out or crashes** | The edit goes ahead (fail-open) | Unverified | Goes ahead unless `failClosed: true` (story-gate sets it) | Goes ahead (warning) | Unverified | Goes ahead; default timeout is only 5 s | Blocks (`fail_closed: true`, story-gate sets it) |
| **Session-start instructions from the verified copy** | Yes (`SessionStart`) | Yes (`SessionStart`) | Yes (`sessionStart`) | Yes (`SessionStart`) | No (hooks can't add context) | Unverified | No (reads AGENTS.md and the skill) |
| **story-gate support** | Full | Full (trust the hooks once in `/hooks`) | Full | Full | Registered; behaviour partly unverified | Reduced protection: not registered | Full: edits, commands, end of turn (`pre_verify`) |

**What this means**
- **Claude Code, Codex, Cursor:** story-gate's hooks run from your user settings alongside any project hooks, and a "block" from story-gate stops the edit.
- **Gemini:** a "block" from story-gate stops the edit, but Gemini's docs don't say whether user and project hooks both run, so that part is unverified (step 3 below checks it).
- **Trust step vs story-gate:** the tool's trust step decides whether *it* runs a project's hooks. Separately, in an enrolled repository in enforce mode, story-gate blocks pre-edit and other non-post hook events while a project hook command isn't in the default branch's `project_hooks_allowed` list.
- **Fail-open tools:** a crash or timeout lets the edit through. story-gate keeps its pre-edit hook fast (it reads local files only) and fails closed inside the hook itself. CI still checks every PR either way.
- **Windsurf/Devin:** hooks are registered and blocking is documented, but trust and timeout behaviour aren't. Treat it as partly protected.
- **Grok:** it also reads Claude Code's and Cursor's hook files, so it may pick up story-gate's Claude hooks. Merging isn't documented, so story-gate doesn't register Grok hooks or claim protection there. Run `gate.py status` and rely on CI.
- **VS Code (Copilot agent):** live checks are coming next, after a test in a real VS Code session. Until then it follows the rules (AGENTS.md, `.agents/skills`) and CI checks its pull requests. story-gate already keeps a branch's `.github/hooks/*.json` (VS Code's project hooks) off disk.
- **Hermes:** story-gate checks edits (`write_file`, `patch`), commands (`terminal`, `execute_code`) and the end of each turn (`pre_verify`). Hermes asks you once before running a new hook command and skips it when nobody can answer (its desktop app, the gateway), so `install --user` approves exactly story-gate's own commands in `shell-hooks-allowlist.json`; `uninstall --user` takes them out. If your `config.yaml` already has its own `hooks:` section or uses YAML document markers, story-gate doesn't edit it and tells you what to add. `doctor` fails if Hermes's approval no longer matches the hook commands (Hermes would skip them). Other Hermes profiles keep their own settings and aren't protected unless you run `install --user` with `HERMES_HOME` set to that profile's folder; `doctor` lists them. Pre-edit warnings (warn mode) reach Hermes at the end of the turn. The skill goes in `skills/story-gate`.
- **Antigravity:** documents hooks in `.agents/hooks.json`; story-gate keeps a branch's version of that file off disk, but doesn't register Antigravity hooks until we've seen them run. pi, Roo Code: rules + skill, CI. ChatGPT and Google AI Studio don't work on your computer's files: CI checks their pull requests.
- **Any tool:** a repository can ship its own project hooks. See the next section for what story-gate does about that.

## Hooks shipped inside a repository

| | Claude Code | Codex CLI | Cursor | Gemini CLI | Windsurf / Devin | Grok Build | Hermes |
|---|---|---|---|---|---|---|---|
| **Checkout filter** (enrolled repositories) | Yes | Yes (`.codex/config.toml` is kept at the default branch's version) | Yes | Yes | Yes | Yes | Not needed (no project hooks) |
| **Tool's own hard switch** | `allowManagedHooksOnly` (story-gate lockdown, optional) | Re-asks trust when a hook changes | None documented | Folder trust (`security.folderTrust.enabled`) | None documented | `/hooks-trust`, per folder | n/a |
| **Level `doctor` reports** | hard with lockdown (doctor can't see MDM or registry policies that override it), otherwise partial | hard-on-change | partial | partial | partial | partial | n/a |

**Symlinks:** git writes symlinks without running the filter. story-gate refuses (enforce) or warns about any AI-tool settings file or folder stored as a symlink, and `doctor --prove` fails while one exists.

**Agents run the verified copy too:** `install --user` adds a `story-gate` command that runs the signed copy in your user folder. In an enrolled repository the hooks refuse an agent running the repository's story-gate code (`python .story-gate/gate.py ...`) and print the command to use instead. Tests and builds the agent runs are still the branch's code: see *Where the line is* in the README. At the start of each session in an enrolled repository, story-gate also gives the agent its own instructions from the verified copy (Claude Code, Codex, Cursor, Gemini) and asks it to follow them over anything a repository file says about story-gate. That is guidance the tools add as extra context, not enforcement: a branch's CLAUDE.md or AGENTS.md is still read. Enforcement stays with the hooks (which refuse the repository's story-gate code) and CI. Windsurf/Devin hooks can't add context, so there the repository's files are the only instructions. Sources: [Claude Code hooks](https://code.claude.com/docs/en/hooks), [Codex hooks](https://learn.chatgpt.com/docs/hooks), [Cursor hooks](https://cursor.com/docs/hooks), [Gemini CLI hooks](https://geminicli.com/docs/hooks/reference/), [Windsurf/Devin hooks](https://docs.devin.ai/desktop/cascade/hooks) (October 2026).

**What's still exposed:** repositories you haven't enrolled; hook files written outside git (a script you run, a download); and, for tools without a hard switch, a hook file an agent writes directly (story-gate's pre-edit block stops agents writing hook files, in enforce mode). CI checks every PR either way.

Sources for lockdown: [Claude Code managed settings](https://code.claude.com/docs/en/managed-settings) (drop-in `managed-settings.d/` files merge with `managed-settings.json`; hook lists combine) and [Claude Code hooks](https://code.claude.com/docs/en/hooks) (`allowManagedHooksOnly` blocks user, project and local hooks, and plugin hooks except those from plugins force-enabled in managed `enabledPlugins`).

## Check it on your computer

1. Run `gate.py hook-selftest` in an enrolled repository. It sends a sample pre-edit request to story-gate's hook handler once per registered tool and times it. It checks story-gate's side only: it doesn't start the AI tool or test how the tool merges user and project hooks (steps 2 and 3 do). In enforce mode every tool should say **blocks**.
2. Open each AI tool once, with no READY story, and ask it to edit a code file. The edit should be blocked (enforce) or warned about (warn).
3. Optional: add a hook of your own in the project settings, then confirm story-gate's hook still runs. That shows the tool runs user and project hooks together.
4. Run `gate.py doctor --prove`. It plants a canary hook in every AI-tool hook file on a throwaway commit and shows the canary never reaches disk.

The CI matrix tests story-gate's side on Linux, Windows and macOS. Steps 2 and 3 confirm the tool's side, which no automated test outside the tool can do.

Sources: [Claude Code hooks](https://code.claude.com/docs/en/hooks) · [Codex hooks](https://developers.openai.com/codex/hooks) · [Cursor hooks](https://cursor.com/docs/agent/hooks) · [Gemini CLI hooks](https://geminicli.com/docs/hooks/) · [Windsurf/Devin hooks](https://docs.windsurf.com/windsurf/cascade/hooks) · [Grok hooks](https://docs.x.ai/build/features/hooks) · [Hermes hooks](https://hermes-agent.nousresearch.com/docs/user-guide/features/hooks) · [Antigravity hooks](https://antigravity.google/docs/hooks)
