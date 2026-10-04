![story-gate mark: a raven in glasses holding a checklist](story-gate-mark.svg)

# story-gate

**Make your AI prove its work before it ships.**

story-gate makes your AI coding tool plan before it builds, check its course while it builds, and show you the feature working when it's done. Then it waits for a person to approve the work on GitHub. An independent AI judge scores each check, so your coding AI never grades its own work.

Free and open source (MIT), by Viaknox. Pilot release (pre-1.0). Full documentation: https://github.com/SathiaAI/story-gate

## Example use cases

1. **Set it up in a repository.** "Set up story-gate in this repo." Claude asks before installing anything, then opens a setup page: sign in to GitHub, give the AI its own login, add the judge key, approve the setup pull request. About 10 minutes, no coding.
2. **Build a feature to spec.** "Build story PAY-12: refunds." Before any code, story-gate checks that the story is complete and that every acceptance criterion has positive, negative, edge and regression tests planned.
3. **Catch drift while building.** Every 10 code edits, Claude reports how far along each goal is and checks for scope creep, placeholder code and tests falling behind. If the work drifts from the spec, it stops and asks a person.
4. **Prove it works before "done".** "Is PAY-12 done?" Claude runs each acceptance criterion as a real scenario and builds a validation page with the evidence and screenshots. No evidence, no pass.
5. **Review an AI-written pull request.** CI re-runs the tests and scenarios, the independent judge re-scores the work, and a code owner approves on GitHub. Any new push cancels the approval.
6. **See where the project stands.** "Show me the story-gate dashboard." One page shows stories that are ready, in progress and done, which agent is working on what, and who is drifting.

## What this plugin contains

One skill (`story-gate`). No hooks, no MCP servers, no commands and no agents. The skill is guidance only: it tells Claude how to work story by story and how to use the story-gate command-line tool.

## What it runs, sends and fetches

- **Install, only after you say yes.** When a repository has no story-gate yet, the skill asks first. On a yes, Claude installs the pinned release from GitHub with `uv tool install`, then runs `story-gate init`, which opens a setup page on your own computer (127.0.0.1).
- **GitHub.** The setup page signs you in to GitHub, creates a separate GitHub login for your AI, stores the judge key as a GitHub secret and on your computer (never in the repository), and opens one pull request with the story-gate files. You merge it.
- **The judge.** For each check, story-gate sends that story's evidence to the judge you choose (OpenRouter by default): the story and its context, the test plan, the scenarios, the validation notes, the handoff, the learnings and the code changes.
- **Events, only if you set them up.** A webhook or command you configure receives story-gate's events: verdicts, progress and learnings.
- **Hooks.** Live checks inside your AI tools are installed only by story-gate's own verified setup, after you agree. This plugin never installs them.

## Requirements

A GitHub repository, `uv` (it fetches Python 3.12 for story-gate), and a judge key (OpenRouter by default; each check costs a fraction of a cent).
