# How each AI tool runs story-gate's hooks

`gate.py install --user` puts story-gate's hooks in each tool's **user** settings and points them at the signed copy in your user folder. This table says what each tool's own documentation promises, so we only claim protection where the tool does. Checked October 2026. "Unverified" means the tool's docs don't say.

| | Claude Code | Codex CLI | Cursor | Gemini CLI | Windsurf / Devin | Grok Build |
|---|---|---|---|---|---|---|
| **User-level hook file** | `~/.claude/settings.json` | `~/.codex/hooks.json` | `~/.cursor/hooks.json` | `~/.gemini/settings.json` | `~/.codeium/windsurf/hooks.json` | `~/.grok/hooks/*.json` |
| **User and project hooks both run** | Yes | Yes | Yes (any "deny" wins) | Unverified | Yes | Unverified |
| **A blocking pre-edit hook stops the edit** | Yes (exit 2) | Yes (exit 2) | Yes (exit 2) | Yes (exit 2) | Yes (exit 2) | Yes (exit 2) |
| **Trust step for project hooks** | Not documented for settings hooks | Yes, per hook, re-asked when a hook changes | Workspace trust | Changed hooks are flagged | Unverified | Folder-level (`/hooks-trust`) |
| **If a hook times out or crashes** | The edit goes ahead (fail-open) | Unverified | Goes ahead unless `failClosed: true` (story-gate sets it) | Goes ahead (warning) | Unverified | Goes ahead; default timeout is only 5 s |
| **story-gate support** | Full | Full (trust the hooks once in `/hooks`) | Full | Full | Registered; behaviour partly unverified | Reduced protection: not registered |

**What this means**
- **Claude Code, Codex, Cursor, Gemini:** story-gate's hooks run from your user settings alongside any project hooks, and a "block" from story-gate stops the edit.
- **Fail-open tools:** a crash or timeout lets the edit through. story-gate keeps its pre-edit hook fast (it reads local files only) and fails closed inside the hook itself. CI still checks every PR either way.
- **Windsurf/Devin:** hooks are registered and blocking is documented, but trust and timeout behaviour aren't. Treat it as partly protected.
- **Grok:** it also reads Claude Code's and Cursor's hook files, so it may pick up story-gate's Claude hooks. Merging isn't documented, so story-gate doesn't register Grok hooks or claim protection there. Run `gate.py status` and rely on CI.
- **Any tool:** a repository can ship its own project hooks. Only the tool's trust setting controls those. Open untrusted branches without hooks.

## Check it on your computer

1. Run `gate.py hook-selftest` in an enrolled repository. It runs each installed hook exactly as the tool would and times it. In enforce mode every tool should say **blocks**.
2. Open each AI tool once, with no READY story, and ask it to edit a code file. The edit should be blocked (enforce) or warned about (warn).
3. Optional: add a hook of your own in the project settings, then confirm story-gate's hook still runs. That shows the tool runs user and project hooks together.

The CI matrix tests story-gate's side on Windows, macOS and Linux. Steps 2 and 3 confirm the tool's side, which no automated test outside the tool can do.

Sources: [Claude Code hooks](https://code.claude.com/docs/en/hooks) · [Codex hooks](https://developers.openai.com/codex/hooks) · [Cursor hooks](https://cursor.com/docs/agent/hooks) · [Gemini CLI hooks](https://geminicli.com/docs/hooks/) · [Windsurf/Devin hooks](https://docs.windsurf.com/windsurf/cascade/hooks) · [Grok hooks](https://docs.x.ai/build/features/hooks)
