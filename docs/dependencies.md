# Dependencies, risks and blockers

A link between stories is written **once**, on the story that waits. "Blocks" is worked out by story-gate from the other side (`gate.py deps <ID>`), never typed in. You can see a link from both ends, but you only store it at one.

## In story.md

`depends_on` in the front matter lists the IDs. A `## Dependencies` section says when each one matters and why:

```
---
depends_on: [SAT-9, SAT-10]
---
## Dependencies
### SAT-9
when: before-start
why: needs the export API that SAT-9 adds

### SAT-10
when: before-merge
why: shares the CSV schema; we agreed the contract in context.md

## Risks
### R1: CSV files over 1 GB time out
likelihood: medium
impact: high
mitigation: stream rows; add a size test

## Blockers
### B1: waiting on the S3 bucket name from ops
owner: Paul
status: open
```

| Section | Heading | Keys |
|---|---|---|
| `## Dependencies` | exactly a story ID | `when` (`before-start` or `before-merge`), `why`: both required |
| `## Risks` | `R1: short title` | `likelihood`, `impact` (`low`, `medium`, `high`): required. `mitigation`: optional |
| `## Blockers` | `B1: short title` | `owner`, `status` (`open` or `resolved`): both required |

Rules, all strict: a section is the word `None` (more text after it is fine) or one or more `### ` blocks. Inside a block, one `key: value` per line. Keys can be any capitals. An unknown key, a repeated key or an empty value is an error, and the message names the line in story.md and shows a correct example. A section that is missing means there is nothing to check. `## Dependencies` must list exactly the IDs in `depends_on`.

The learner's [known pitfalls](pitfalls.md) are a separate thing. `## Risks` is about this story only.

## Old stories (no migration)

A story with no `## Dependencies` heading is a legacy story. Each `depends_on` ID counts as `before-start`, and READY shows a WARN asking you to add the section. `consumers:` in the front matter is legacy too: new stories leave it out, and READY only warns when it disagrees with what the other stories say. A story that has the `## Dependencies` heading is never read as legacy, even if the section is broken.

## What READY and DONE check

| Check | When | Result |
|---|---|---|
| `dependencies` | READY | FAIL: bad or missing ID, a story that depends on itself, a cycle, a broken section, `depends_on` and the blocks disagree, a `before-start` dependency not finished on the default branch. WARN: a `before-merge` dependency not finished yet, a dependency done with a waiver ("done (waived)"), a legacy story |
| `blockers_resolved` | READY | FAIL while any blocker is `open` |
| `risks_handled` | READY | CONCERNS when a `high` impact risk has no `mitigation` |
| `dependencies_done` | DONE | FAIL unless every dependency (both kinds) is finished on the default branch and no blocker is open |

Policy:
- **A waived dependency counts as done**, and is shown as "done (waived)".
- **A `before-merge` dependency may live on another branch.** READY only warns and says where it was found ("unverified": a branch is never proof). DONE is what blocks the merge.
- **An open blocker fails READY.** These are structural checks, so they can't be waived (see the [guide](guide.md#d-what-we-evaluate-today)). If work can safely start, move the item to `## Risks` with a mitigation.

"Finished" means the dependency's `done.json` on the **latest commit of the default branch** says PASS or WAIVED. If your branch changes the dependency's own story.md, its finished verdict on main is for the old version and does not count.

If the default branch can't be found (no remote fetched), a story that has dependencies fails with "run `git fetch origin`". A story with no dependencies never needs git for this.

Because DONE is checked against the latest default branch, turn on GitHub's setting **Require branches to be up to date before merging**, so the check runs against the latest main.

## `gate.py deps <ID>`

Prints, in plain English: what the story depends on (when, why, and whether each is done), which stories on this branch or the default branch depend on it (worked out, not stored), its open blockers and its risks. It exits 1 only for a bad ID or a story it can't find.
