# How each AI tool runs story-gate's hooks

`gate.py install --user` puts story-gate's hooks in each tool's **user** settings and points them at the signed copy in your user folder. Until the first signed release is published, pilots install with `gate.py install --user --unsigned` (doctor then says the copy is unsigned). This table says what each tool's own documentation promises, so we only claim protection where the tool does. Checked October 2026. "Unverified" means the tool's docs don't say.

| | Claude Code | Codex CLI | Cursor | Gemini CLI | Windsurf / Devin | Grok Build |
|---|---|---|---|---|---|---|
| **User-level hook file** | `~/.claude/settings.json` | `~/.codex/hooks.json` | `~/.cursor/hooks.json` | `~/.gemini/settings.json` | `~/.codeium/windsurf/hooks.json` | `~/.grok/hooks/*.json` |
| **User and project hooks both run** | Yes | Yes | Yes (any "deny" wins) | Unverified | Yes | Unverified |
| **A blocking pre-edit hook stops the edit** | Yes (exit 2) | Yes (exit 2) | Yes (exit 2) | Yes (exit 2) | Yes (exit 2) | Yes (exit 2) |
| **Trust step for project hooks** | Not documented for settings hooks | Yes, per hook, re-asked when a hook changes | Workspace trust | Changed hooks are flagged | Unverified | Folder-level (`/hooks-trust`) |
| **If a hook times out or crashes** | The edit goes ahead (fail-open) | Unverified | Goes ahead unless `failClosed: true` (story-gate sets it) | Goes ahead (warning) | Unverified | Goes ahead; default timeout is only 5 s |
| **story-gate support** | Full | Full (trust the hooks once in `/hooks`) | Full | Full | Registered; behaviour partly unverified | Reduced protection: not registered |

**What this means**
- **Claude Code, Codex, Cursor:** story-gate's hooks run from your user settings alongside any project hooks, and a "block" from story-gate stops the edit.
- **Gemini:** a "block" from story-gate stops the edit, but Gemini's docs don't say whether user and project hooks both run, so that part is unverified (step 3 below checks it).
- **Trust step vs story-gate:** the tool's trust step decides whether *it* runs a project's hooks. Separately, in an enrolled repository in enforce mode, story-gate blocks pre-edit and other non-post hook events while a project hook command isn't in the default branch's `project_hooks_allowed` list.
- **Fail-open tools:** a crash or timeout lets the edit through. story-gate keeps its pre-edit hook fast (it reads local files only) and fails closed inside the hook itself. CI still checks every PR either way.
- **Windsurf/Devin:** hooks are registered and blocking is documented, but trust and timeout behaviour aren't. Treat it as partly protected.
- **Grok:** it also reads Claude Code's and Cursor's hook files, so it may pick up story-gate's Claude hooks. Merging isn't documented, so story-gate doesn't register Grok hooks or claim protection there. Run `gate.py status` and rely on CI.
- **Any tool:** a repository can ship its own project hooks. See the next section for what story-gate does about that.

## Hooks shipped inside a repository

| | Claude Code | Codex CLI | Cursor | Gemini CLI | Windsurf / Devin | Grok Build |
|---|---|---|---|---|---|---|
| **Checkout filter** (enrolled repositories) | Yes | Yes (`.codex/config.toml` is kept at the default branch's version) | Yes | Yes | Yes | Yes |
| **Tool's own hard switch** | `allowManagedHooksOnly` (story-gate lockdown, optional) | Re-asks trust when a hook changes | None documented | Folder trust (`security.folderTrust.enabled`) | None documented | `/hooks-trust`, per folder |
| **Level `doctor` reports** | hard with lockdown (doctor can't see MDM or registry policies that override it), otherwise partial | hard-on-change | partial | partial | partial | partial |

**Symlinks:** git writes symlinks without running the filter. story-gate refuses (enforce) or warns about any AI-tool settings file or folder stored as a symlink, and `doctor --prove` fails while one exists.

**Agents run the verified copy too:** `install --user` adds a `story-gate` command that runs the signed copy in your user folder. In an enrolled repository the hooks refuse an agent running the repository's story-gate code (`python .story-gate/gate.py ...`) and print the command to use instead. Tests and builds the agent runs are still the branch's code: see *Where the line is* in the README.

**What's still exposed:** repositories you haven't enrolled; hook files written outside git (a script you run, a download); and, for tools without a hard switch, a hook file an agent writes directly (story-gate's pre-edit block stops agents writing hook files, in enforce mode). CI checks every PR either way.

Sources for lockdown: [Claude Code managed settings](https://code.claude.com/docs/en/managed-settings) (drop-in `managed-settings.d/` files merge with `managed-settings.json`; hook lists combine) and [Claude Code hooks](https://code.claude.com/docs/en/hooks) (`allowManagedHooksOnly` blocks user, project and local hooks, and plugin hooks except those from plugins force-enabled in managed `enabledPlugins`).

## Check it on your computer

1. Run `gate.py hook-selftest` in an enrolled repository. It sends a sample pre-edit request to story-gate's hook handler once per registered tool and times it. It checks story-gate's side only: it doesn't start the AI tool or test how the tool merges user and project hooks (steps 2 and 3 do). In enforce mode every tool should say **blocks**.
2. Open each AI tool once, with no READY story, and ask it to edit a code file. The edit should be blocked (enforce) or warned about (warn).
3. Optional: add a hook of your own in the project settings, then confirm story-gate's hook still runs. That shows the tool runs user and project hooks together.
4. Run `gate.py doctor --prove`. It plants a canary hook in every AI-tool hook file on a throwaway commit and shows the canary never reaches disk.

The CI matrix tests story-gate's side on Linux and Windows; the same suite was also run by hand on macOS. Steps 2 and 3 confirm the tool's side, which no automated test outside the tool can do.

Sources: [Claude Code hooks](https://code.claude.com/docs/en/hooks) · [Codex hooks](https://developers.openai.com/codex/hooks) · [Cursor hooks](https://cursor.com/docs/agent/hooks) · [Gemini CLI hooks](https://geminicli.com/docs/hooks/) · [Windsurf/Devin hooks](https://docs.windsurf.com/windsurf/cascade/hooks) · [Grok hooks](https://docs.x.ai/build/features/hooks)
