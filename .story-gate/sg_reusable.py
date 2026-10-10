"""story-gate as a reusable GitHub workflow: one short file per check in the adopting repository.

The adopting repository's workflows call story-gate's own workflows at a pinned commit:

    uses: SathiaAI/story-gate/.github/workflows/gate.yml@<full commit SHA>  # vX.Y.Z

Inside those workflows, `gate.py runtime-prepare` (this module) checks the pinned story-gate copy (its release signature,
and the release key the caller expects), then copies it and the BASE branch's settings into a folder outside the
checkout. The checks run from that folder. Pull request code never runs where the secrets are, and the pull request can't
change the settings or the code that judges it: the same split as the copied workflows (`install --ci copy`).
"""
import json, os, re, shutil, sys, urllib.parse
from pathlib import Path

UPSTREAM = "SathiaAI/story-gate"
SHA = re.compile(r"[0-9a-f]{40}")
FINGERPRINT = re.compile(r"SHA256:[A-Za-z0-9+/]{43}")
SECRETS = ("OPENROUTER_API_KEY", "TYPESAFE_API_KEY", "JUDGE_API_KEY", "STORY_GATE_SPECS_TOKEN",
           "STORY_GATE_LINEAR_KEY", "STORY_GATE_JIRA_EMAIL", "STORY_GATE_JIRA_TOKEN")
CHECK = "story-gate / story-gate"  # the required check's name: caller job "story-gate" / called job "story-gate"


def out_line(name, value):
    """Set a step output (GitHub Actions); print it when run by hand."""
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write("%s=%s\n" % (name, value))


def prepare(G, kv, rest):
    """runtime-prepare --out DIR --base REF [--fingerprint SHA256:...] [--allow-unsigned true|false]
    Exit 0 with output ready=true when the checks can run, ready=false when story-gate isn't on the base branch yet."""
    T = G.T
    out, base = kv.get("out"), kv.get("base")
    if not out or not base:
        print("usage: runtime-prepare --out DIR --base REF [--fingerprint SHA256:...] [--allow-unsigned true|false]")
        return 2
    unsigned = str(kv.get("allow-unsigned", "false")).strip().lower() == "true"
    want = (kv.get("fingerprint") or "").strip()
    src = G.HERE
    if (G.ROOT / ".story-gate").is_symlink():
        print("::error title=story-gate::.story-gate is a symlink in this checkout; refusing to run")
        return 1
    if want and not FINGERPRINT.fullmatch(want):
        print("::error title=story-gate::release-key-fingerprint must look like SHA256:<43 characters> (got %r)" % want[:60])
        return 1
    if want and T.key_fingerprint() != want:
        print("::error title=story-gate::this story-gate copy trusts release key %s, not the %s your workflow expects. "
              "Check the commit you pinned." % (T.key_fingerprint(), want))
        return 1
    try:
        T.verify_release(src)
        print("story-gate %s: release signature valid (key %s)" % (G.VERSION, T.key_fingerprint()))
    except T.TrustError as e:
        if not unsigned:
            print("::error title=story-gate::the pinned story-gate commit isn't a signed release (%s). Pin the commit of a "
                  "story-gate release tag, or set allow-unsigned: true to run a development copy on purpose." % e)
            return 1
        print("::warning title=story-gate::running an UNSIGNED development copy of story-gate (allow-unsigned: true)")
    policy = G.git("show", "%s:.story-gate/config.json" % base)
    if not policy.strip():
        print("::warning title=story-gate::story-gate is not on the base branch yet (no .story-gate/config.json on %s), so this "
              "pull request is checked by human review only. It never runs code from the pull request itself." % base)
        out_line("ready", "false")
        return 0
    dest = Path(out)
    if dest.exists():
        shutil.rmtree(str(dest))
    for f in T.runtime_files(src):
        (dest / f).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(src / f), str(dest / f))
    (dest / "config.json").write_text(policy, encoding="utf-8")  # settings and calibration come from the base branch only
    cal = G.git("show", "%s:.story-gate/judge-calibration.json" % base)
    if cal.strip():
        (dest / "judge-calibration.json").write_text(cal, encoding="utf-8")
    out_line("ready", "true")
    print("story-gate is ready in %s, with the settings from %s" % (dest, base))
    return 0


# ------------------------------------------------------------------ the short files the adopting repository keeps
def _secrets():
    return "".join("      %s: ${{ secrets.%s }}\n" % (s, s) for s in SECRETS)


def caller_files(upstream, sha, version, fingerprint, base_branch, managed):
    """{path: text} for the three workflows of an adopting repository."""
    pin = "%s # %s" % (sha, version)
    w = "      release-key-fingerprint: \"%s\"\n" % fingerprint
    gate = managed + """
name: story-gate
on:
  pull_request:
    types: [opened, edited, synchronize, reopened, ready_for_review, labeled, unlabeled]
  pull_request_review:
    types: [submitted, edited, dismissed]
permissions:
  contents: read
  pull-requests: read
  issues: read
concurrency:
  group: story-gate-${{ github.event.pull_request.number }}
  cancel-in-progress: true
jobs:
  story-gate:
    uses: %s/.github/workflows/gate.yml@%s
    with:
%s    secrets:
%s""" % (upstream, pin, w, _secrets())
    audit = managed + """
name: story-gate-audit
on:
  pull_request:
    types: [closed]
permissions:
  contents: read
  pull-requests: read
  issues: write
jobs:
  audit:
    if: github.event.pull_request.merged == true
    uses: %s/.github/workflows/audit.yml@%s
    with:
%s""" % (upstream, pin, w)
    dash = managed + """
name: story-gate-dashboard
on:
  schedule:
    - cron: "11,41 * * * *"
  workflow_run:
    workflows: [story-gate]
    types: [completed]
  push:
    branches: [%s]
  workflow_dispatch:
permissions:
  contents: read
  issues: write
  pull-requests: read
  checks: read
concurrency:
  group: story-gate-dashboard
  cancel-in-progress: false
jobs:
  dashboard:
    uses: %s/.github/workflows/dashboard.yml@%s
    with:
%s""" % (json.dumps(base_branch), upstream, pin, w)
    return {".github/workflows/story-gate.yml": gate, ".github/workflows/story-gate-audit.yml": audit,
            ".github/workflows/story-gate-dashboard.yml": dash}


def resolve_tag(G, upstream, version):
    """The commit SHA of tag v<version> on upstream, through GitHub's API. RuntimeError when it can't be found."""
    import sg_github as GH
    tok = GH.human_token()
    st, ref, _ = GH.call("GET", "/repos/%s/git/ref/tags/%s" % (upstream, urllib.parse.quote("v" + version)), tok)
    obj = (ref or {}).get("object") if st == 200 and isinstance(ref, dict) else None
    if obj and obj.get("type") == "tag":  # an annotated tag points at a tag object first
        st, tag, _ = GH.call("GET", "/repos/%s/git/tags/%s" % (upstream, obj.get("sha")), tok)
        obj = (tag or {}).get("object") if st == 200 and isinstance(tag, dict) else None
    sha = (obj or {}).get("sha") or ""
    if not SHA.fullmatch(sha):
        raise RuntimeError("couldn't find release tag v%s on %s (HTTP %s). Pass --ref <the 40-character commit SHA of a "
                           "story-gate release>" % (version, upstream, st))
    return sha


def install(G, kv, rest):
    """install --ci reusable [--ref SHA] [--upstream owner/repo]: write the three short workflows."""
    upstream = kv.get("upstream") or UPSTREAM
    if not G.REPO_NAME.fullmatch(upstream):
        sys.exit("--upstream must be owner/repo")
    sha = (kv.get("ref") or "").strip().lower()
    if sha and not SHA.fullmatch(sha):
        sys.exit("--ref must be a full 40-character commit SHA (a tag or branch name can be moved; a SHA can't)")
    try:
        sha = sha or resolve_tag(G, upstream, G.VERSION)
    except RuntimeError as e:
        sys.exit("story-gate install --ci reusable: %s" % e)
    a, b = G.USER_STEPS
    header = ("# managed by story-gate %s - `story-gate install --ci reusable` writes this file. To upgrade, change the commit SHA "
              "to a newer story-gate release in a pull request of its own" % G.VERSION)
    kept = [p for p in caller_files(upstream, sha, "v" + G.VERSION, "", "main", header)
            if a in G.rd(G.ROOT / p) and G.rd(G.ROOT / p).split(a, 1)[1].split(b, 1)[0].strip()]
    if kept and "--force" not in rest:
        sys.exit("%s has your own steps between the 'your steps' markers. The reusable workflow can't run them. Move them "
                 "out (see docs/guide.md), or run again with --force to drop them." % ", ".join(kept))
    base = re.sub(r"^origin/", "", G.cfg().get("base_branch", "main"))
    notes = [G.write_managed(p, body) for p, body in
             caller_files(upstream, sha, "v" + G.VERSION, G.T.key_fingerprint(), base, header).items()]
    return notes, sha
