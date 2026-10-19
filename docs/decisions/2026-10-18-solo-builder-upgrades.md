# Solo builders: who opens setup and upgrade pull requests (2026-10-18)

**Problem.** Setup and upgrades change workflow files. The agent App can't change those (and must not: that would let an AI run workflows with your secrets). So these pull requests were opened as you, and a solo owner can't approve their own pull request. story-gate stopped with "add a second code owner".

**Decision.** Split authorship. You commit the workflow files; the agent App commits the rest, makes the last commit and opens the pull request. Your approval counts. Upgrades use the same path through `story-gate upgrade-repo`. Proven on a real repository first (an App-opened pull request containing workflow files you pushed, approved by you).

**Rejected.** A second "setup" App with workflow rights (its key would sit where your AI can read it). Turning the branch rule off for a few minutes (a shortcut). An admin bypass (it would let you skip review for AI pull requests too). Giving the agent App the workflows permission (secrets could leak).

**Correction.** A first review assumed repositories call story-gate's workflows by a pinned commit, so Dependabot could do upgrades. Setup actually copies story-gate into the repository, with full workflow files and your own steps. A second review (4 of 4 models) chose split upgrades now.

**If a repository isn't upgraded.** It keeps working on its older, signed copy of story-gate. That is the worst case: CI never runs a copy that isn't a signed release, because a pull request that changes story-gate's code must carry a signed release or its check fails.

**Rollout.** Canary first: Viaknox/tune-up, then Viaknox/tune-up-site and SathiaAI/sast-check, each through `upgrade-repo` (or `init`) and a real approval. Then `upgrade-repo --report --org ...` for the version inventory, and the batch, which reports each repository's result and carries on past failures.

**Later, as its own story: reusable workflows by default, with Dependabot opening upgrade pull requests.** Start it only when all of these hold:

1. A place for your own CI setup steps (for example installing Node) that the reusable workflow runs, with no secrets, before your tests.
2. The copied story-gate in the repository stays in step with the pinned one (or is no longer needed by cloud agents).
3. A Dependabot pull request that only moves the pin to a signed story-gate release passes without secrets; anything else in it fails.
4. Existing copy-mode repositories can move over with one `upgrade-repo` pull request, and back.
5. A new frontier review approves the design before building.
