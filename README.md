<p align="center"><img src="docs/assets/header.jpg" alt="story-gate by Viaknox. Every story starts on spec, is tested against spec, and ends on spec. The story-gate raven mark, a raven in glasses holding a checklist. Ink drawing of a city rail station with a yellow buffer stop at the end of the track." width="100%"></p>

# story-gate

**Make your AI prove its work before it ships.**

story-gate makes your AI coding tool plan before it builds, check its course while it builds, and show you the feature working when it's done. Then it waits for your OK. Your work is much safer to ship. No coding needed (a little GitHub is).

**Free and open source (MIT) · pilot release (pre-1.0)**

Works in Claude Code, Codex, Cursor, VS Code, Gemini CLI, Hermes, Windsurf and cloud agents. [What each tool gets](docs/client-security.md).

<p align="center"><img src="docs/assets/clients.png" alt="Works with. Live checks: Claude Code, Codex, Cursor, VS Code (Copilot agent), Gemini CLI, Hermes, Windsurf (partly verified). Rules and skill: Antigravity, pi, Roo Code. Pull request checks: Grok Build, ChatGPT, Google AI Studio, cloud agents. Install from: Claude Code plugin, Gemini CLI extension, Hermes skills tap, skills.sh, uv." width="100%"></p>

## Sound familiar?

- Your AI says "done", but the button does nothing.
- You fix one thing, and two other things break.
- It built something you never asked for.
- You can't tell if the code is any good, so you're afraid to merge.
- The next session forgets everything the last one learned.

story-gate fixes this with four checks on every piece of work (a **story**):

| When | What story-gate checks | What you see |
|---|---|---|
| **Before** any code | The plan is clear and every goal has a test (READY) | A short plain-English summary |
| **While** it builds | It's still on course, with no scope creep (CHECKPOINTS) | % done on the dashboard |
| **When** it says done | The tests pass, and the AI ran the feature for real for every goal (DONE). The pull request check runs the tests and those runs again. A run that can't work there (it needs a device, say) is marked "reported", and the check flags it for you | A one-page validation report |
| **Before** it merges | You approve it on GitHub | Nothing merges without you, where GitHub enforces branch rules |

An independent AI judge scores each check. Your coding AI never grades its own work.

## What you get

<p align="center"><img src="docs/assets/validation-page.png" alt="A validation page for a sample story: READY and DONE passed, 2 of 2 goals shown working, each scenario with its result, and a screenshot of the app running." width="100%"></p>

For every story, a **validation page** shows:

- the plan and the goals (**acceptance criteria**),
- each goal next to the run that shows it working,
- screenshots, bugs found and fixed, and lessons learnt,
- steps to try it yourself.

Each fact is marked **checked** (the pull request check ran it) or **reported** (the AI says so). Ask your AI: `story-gate report <story id> --open`.

## Use it when

- you're building something real people will use,
- your AI keeps breaking things that worked,
- more than one person (or AI) works in the same project,
- you moved from a no-code builder to Claude Code, Cursor or Codex, and want a safety net.

**Skip it** for a weekend prototype or a throwaway script. The checks add a few minutes per story.

## Try it first (one command, nothing to set up)

See story-gate catch a real bug before you change anything. Paste this into your AI tool, or run it yourself:

```bash
uv tool install --python 3.12 git+https://github.com/SathiaAI/story-gate@v0.6.2
story-gate try
```

`story-gate try` makes a throwaway example project in your temp folder with one small story and a real bug. It runs each goal for real, shows one passing and one failing, and opens the validation page. No GitHub, no judge key, no network, and your own projects aren't touched. (If `story-gate` isn't found, run `uv tool update-shell` and open a new terminal.)

## Set it up in 5 steps

<p align="center"><img src="docs/assets/setup.png" alt="Five setup steps: 1 ask your AI, 2 sign in to GitHub, 3 give your AI its own login, 4 add the judge key, 5 approve the setup. Your AI does everything else." width="100%"></p>

**Step 1.** Open your project in your AI tool and paste this:

```text
Set up story-gate in my project (the folder I have open). Install it from https://github.com/SathiaAI/story-gate, but don't change that repository.
```

**Steps 2 to 5** happen on a page that opens in your browser. It tells you exactly what to click, and ticks each step off when it's done.

<p align="center"><img src="docs/assets/setup-page.png" alt="The setup page in the browser: sign in, give your AI its own login, add the judge key, approve the setup, tick the AI tools to protect. Finished steps show a green tick." width="70%"></p>

**What you need:** a GitHub account, your project on GitHub, and an AI coding tool. Nothing else; your AI installs the rest.

**The judge key (step 4):** make one at [openrouter.ai/keys](https://openrouter.ai/keys) and add some credit. Each check is one small judge call; OpenRouter shows what each one costs. The key goes straight into a GitHub secret, and your AI never sees it.

**Who approves:** you, plus anyone you add in step 5 (they need write access to the repository). Any one of you can approve.

**Teammates:** each person pastes the same sentence once on their own computer. Their setup skips steps 4 and 5.

## Your first story

1. Ask your AI for one small feature, for example: "Add a contact form with name, email and message."
2. Your AI writes the story: a plain summary, the goals and the tests. story-gate checks it (READY).
3. Your AI builds it. story-gate checks it stays on course.
4. Your AI runs the feature for every goal and writes the validation report (DONE).
5. It opens a pull request. Open the validation page, try the demo steps, then approve on GitHub.

**Habits that pay off:**

- **Keep stories small.** One feature a person can see or try. Big stories are hard to check.
- **Read the report before you approve.** If a goal isn't shown working, ask why.
- **Lost track of where a story is?** Ask your AI to run `story-gate next`. It says what's done and what comes next.
- **When a check fails, ask your AI to fix the cause.** Don't ask it to skip the check. It can't approve its own work anyway.
- **Look at the dashboard once a day.** `AT_RISK` means look now.

## Use the dashboard

<p align="center"><img src="docs/assets/dashboard.png" alt="The story-gate dashboard: a pinned GitHub issue with the headline numbers, and a full report showing the pipeline, who is working on what, and the quality numbers." width="100%"></p>

- **Find it:** open your repository on GitHub → **Issues** → **Story-gate dashboard**, pinned at the top. It updates by itself every 30 minutes.
- **Read it in this order:**
  1. **The top numbers.** How many stories are ready, in progress and done, and how many acceptance criteria are proven by tests.
  2. **Who is working on what.** Each AI agent, its story and % done. `AT_RISK` or a drift note means: look now.
  3. **Queued to start.** Stories that passed READY and are waiting for an agent.
- **The full report:** click **download the HTML dashboard** in the issue. Or ask your AI to run `story-gate dashboard --open`.
- **Private stays private:** the dashboard lives in your repository, so only people who can see the repository can see it.

## How a change flows

<p align="center"><img src="docs/assets/lifecycle.png" alt="The story lifecycle in four moments: Ready, Checkpoints, Done and Acceptance, with the checks in each" width="100%"></p>

You ask for a feature. Your AI writes the story and the tests, and story-gate checks they're good enough to build from (**READY**). While it builds, story-gate checks it's still on course (**CHECKPOINTS**). When it's finished, the tests must prove every acceptance criterion, and your AI must run the feature for real and show you the result (**DONE**). Then it opens a pull request, and **you approve and merge**.

## Good to know

- **Your AI never uses your GitHub login.** It works through its own login (step 3), so it can't approve or merge its own work.
- **Branches can't switch story-gate off.** The rules come from your main branch, and the checks run from a verified copy on your computer. See [security](docs/client-security.md).
- **Plain English.** Your AI writes replies, PR descriptions, story summaries and handoffs in short, plain sentences, with diagrams where a picture is clearer. story-gate scores this (an STE-style score, target 80%) and gives advice. See [plain writing](docs/guide.md#plain-writing).
- **Pilot:** story-gate is pre-1.0. Signed releases are coming; until then, setup installs from a pinned version on GitHub.

## How it compares

| | Checks the plan first | Proves each goal works | Re-checks on every pull request | Needs your approval |
|---|---|---|---|---|
| Nothing (just the AI) | No | No | No | No |
| Your tests in CI | No | Only what the tests cover | Yes | No |
| A PR review bot (e.g. CodeRabbit) | No | No | Yes (reads the code) | No |
| **story-gate** | **Yes** | **Yes** | **Yes** | **Yes** |

story-gate works with your tests and your review bot. It runs your tests itself and waits for the review bot's threads to be resolved.

## FAQ

**Do I need to code?** No. You need a GitHub account, and you click Approve and Merge on GitHub. Your AI does the rest.

**What does it cost?** story-gate is free. The judge runs on [OpenRouter](https://openrouter.ai/keys), which charges per call. Each check is one small call, and your OpenRouter activity page shows the exact cost.

**Will it slow my AI down?** A little. Each story takes extra time for the plan, the checks and the report. You get that back in less rework.

**What if the judge is wrong?** Tell story-gate (`story-gate label`). A person can also record a decision or a waiver, but it counts only after a code owner approves.

**Is my code sent anywhere?** Yes, to the judge you chose (OpenRouter by default). For each check it gets that story's evidence: the story and its context, the test plan, the scenarios (what each one runs and expects), `validation.md`, the handoff, the learnings, and the code changes. If you set up a webhook, control-hub or a command to receive events, it gets story-gate's events: verdicts, progress and learnings. Your AI coding tool already sends far more to its own model.

**I'm on a free GitHub plan with a private repository.** GitHub doesn't enforce "must be approved" there. story-gate still checks every pull request and marks it **ADVISORY**.

<details>
<summary><b>Words used here</b></summary>

| Word | Meaning |
|---|---|
| Story | One piece of work, small enough to build and check in one go |
| Acceptance criteria (goals) | The things that must be true when the story is done |
| Scenario | Your AI running the feature the way a person would, to show a goal working |
| READY, CHECKPOINTS, DONE | The three checks story-gate runs: before, during and after the build |
| Judge | An independent AI that scores the evidence. It is never your coding AI |
| Pull request (PR) | GitHub's page where a change waits for your approval before it joins your project |
| CI | The checks GitHub runs on every pull request |
| Drift | The work, the story and your product documents (PRD, TRD) no longer agree |
| Code owner | A person allowed to approve changes. You, and anyone you add |

</details>

## Learn more

- [The full guide](docs/guide.md): every setting, manual setup, lockdown and fallbacks.
- [How each AI tool runs story-gate](docs/client-security.md) · [Pinned project hooks](docs/hook-pinning.md)

<details>
<summary><b>For AI agents: how to run the setup</b></summary>

When the person asks you to set up story-gate:

1. Make sure `uv` is installed (`uv --version`). If it isn't, install it with the official installer:
   - macOS / Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh`
   - Windows (PowerShell): `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`
2. Install story-gate: `uv tool install --python 3.12 git+https://github.com/SathiaAI/story-gate@v0.6.2`, then `uv tool update-shell` if `story-gate` isn't found.
3. In the project folder, run `story-gate init` **in the background**. It prints a local page address and opens it in the browser. Keep it running until it prints `story-gate: story-gate is protecting ...`.
4. Tell the person: "A setup page opened in your browser. Follow it; I'll wait." Do not click, sign in, create apps or type the judge key for them. Those steps are theirs.
5. When it finishes, run `story-gate doctor` and report its summary in plain words.

If the setup page reports an error, read it to the person and follow its instructions. Run `story-gate init` again to continue; it skips what is already done on GitHub.
</details>

<sub>Tests: `python -m unittest tests/test_gate.py tests/test_packaging.py`. CI runs `tests/test_gate.py` on Linux, Windows and macOS; the packaging tests run locally for now. MIT licensed. story-gate by Viaknox.</sub>
