# Security policy

story-gate decides whether AI-written code may merge, so a way around it is a security bug. Thank you for reporting one privately.

## Report a vulnerability

Use GitHub's private report form: **[Report a vulnerability](https://github.com/SathiaAI/story-gate/security/advisories/new)**.

Please don't open a public issue, pull request or discussion for a security problem. The report stays private between you and the maintainers until a fix is released.

Include, if you can:

- what an attacker can do, and what they need first (for example, push access to a branch, or a pull request from a fork)
- steps to reproduce, or a proof of concept
- the story-gate release you used (for example v0.9.0, or the commit) and the AI tool involved

## What happens next

- We aim to acknowledge a report within 5 business days.
- We confirm the problem, agree a fix date with you, and keep you updated.
- The fix ships in a signed release with a GitHub security advisory. We credit you unless you'd rather stay anonymous.

## In scope

- Getting code merged that story-gate should block, or switching the gate off from a branch or pull request
- Running code from a pull request in a job that holds secrets
- Leaking a tracker key, judge key or GitHub token (Linear, Jira, OpenRouter, GitHub)
- Getting past the hook trust checks, or the signature check on releases and upgrades
- The local pages (`init`, `tracker-setup`, `dashboard --serve`) answering anything other than the person who started them

Problems in GitHub, Linear, Jira or an AI tool itself belong with that vendor. If story-gate's use of them makes the problem worse, report it here too.

## Supported versions

story-gate is pre-1.0. Fixes go into the latest release only, so please upgrade before you report. Every release since v0.7.0 is signed, and story-gate checks the signature before it installs or upgrades itself.
