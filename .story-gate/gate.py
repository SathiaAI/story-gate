#!/usr/bin/env python3
"""story-gate — per-story quality gate for AI coding sessions. Stdlib only, any OS, any AI client.

Two moments per story:
  READY  (before code): story complete?  drift vs PRD/TRD?  ACs + positive/negative/edge/regression tests planned?
                        upstream handoffs in place?  prior learnings read?
  DONE   (after code):  code matches story/TRD?  planned tests written and passing?  handoff written for
                        downstream work?  errors/learnings recorded?

LLM agents gather evidence into .story-gate/stories/<ID>/ ; THIS SCRIPT computes the verdict.
Semantic checks are scored by TypeSafe Jev (OpenRouter /api/alpha/decisions) when a key is available;
otherwise agent self-scores are used but can never reach PASS (allow_self_judge_pass=false).

Commands (run from the repo root):
  install                            set up this repository: config, instructions, skills, CI workflows (commit the result)
  install --user [--clients claude,codex,cursor,gemini,windsurf] [--dry-run] [--unsigned]
                                     once per computer, as the human: install the trusted runtime + user-level hooks, enroll this repo
  uninstall --user [--dry-run]       remove the user-level hooks and the runtime
  enroll [--policy-ref REF] | unenroll   turn checking (and the checkout filter for AI-tool hook files) on/off for this repository
  filter on|off|status               this repository only: the checkout filter that keeps a branch's hook changes off disk (off is logged)
  lockdown [--on [--keep-project-hook CMD] | --off | --bundle DIR]   optional, OFF by default: Claude Code's hard switch for
                                     repository hooks. Explains first; --on needs your explicit YES; --bundle writes files for IT
  upgrade [--ref REF | --from DIR | --url https://...] | rollback   (run with the installed runtime) verified upgrade / go back
  release-sign --key KEY             maintainers: sign the files in .story-gate as a release
  hook-selftest                      run each installed user-level hook the way the AI tool would, and time it
  feature <F> --title T [--description D]   register a feature (stories point to it with `feature:` in story.md)
  plan <ID> --title T [--feature F]  add a story to the backlog (not started; queued once READY passes)
  start <ID> [--model M] [--client C]   claim the story: make folder + skeletons, set active story, record who codes
  dashboard [--open] [--out DIR] [--offline]   build the progress dashboard from every branch (HTML + summary)
  source <ID>                        run 'command' sources from config, print their output
  record-tests <ID> -- <cmd...>      run the test command, store exit code + output tail (evidence)
  score <ID> ready|done [--base REF] compute checks + verdict -> stories/<ID>/<phase>.json
  decide <ID> --drift story|spec|none --by WHO --note TEXT [--phase ready|build|done]  record the drift decision
  waive <ID> <check> --by WHO --reason TEXT                     record a waiver
  learn <ID> --type error|learning|pattern|none --summary .. [--root-cause ..] [--rule ..] [--tags a,b]
  checkpoint <ID>                    mid-story Jev check: % complete (estimate), on course?, drift, TODOs, tests keeping pace
  learnings [words...]               search past learnings (read these before planning)
  status [<ID>]                      one plain-English line
  hook --client C --event pre|post|stop   called by AI-client hooks (reads JSON on stdin)
  ci [--tests DIR] | ci-tests DIR | audit       called by the CI workflows (see .github/workflows/story-gate*.yml)
  label <ID> ready|done correct|wrong [--note ..]  tell story-gate whether a verdict was right (tunes thresholds)
  judge-calibrate                    test a non-Jev judge on known cases before it may PASS anything
  setup-repo [--owners a,b]          one time, as YOU: CODEOWNERS + branch rules + Actions cannot approve PRs
  setup-agent [--org ORG]            one time: create your private "story-gate agent" GitHub App (AI identity)
  agent-env --repo owner/name        print env + git identity so AI tools act as the agent App, not as you
  agent-token --repo owner/name [--git-credential]   1-hour token for the agent App
  publish                            send outbox events to configured sinks
  doctor [--repo owner/name] [--strict] [--prove]   plain-English health check; --prove plants a canary hook on a throwaway
                                     commit and shows it never reaches disk
"""
import os, sys


def _nothing_to_gate():
    """Cheap check (no subprocess, no heavy imports): True outside git repositories, and in repositories that neither
    contain .story-gate nor are enrolled on this computer. Hooks fire everywhere, so this keeps them fast."""
    d = os.getcwd()
    while not os.path.exists(os.path.join(d, ".git")):
        up = os.path.dirname(d)
        if up == d:
            return True
        d = up
    if os.path.isdir(os.path.join(d, ".story-gate")):
        return False
    g = os.path.join(d, ".git")
    if os.path.isfile(g):  # worktree or submodule: "gitdir: <common>/worktrees/<name>"
        try:
            ptr = open(g, encoding="utf-8").read().split("gitdir:", 1)[1].strip()
            g = os.path.join(d, ptr) if not os.path.isabs(ptr) else ptr
            g = os.path.dirname(os.path.dirname(g)) if os.path.basename(os.path.dirname(g)) == "worktrees" else g
        except Exception:
            return False
    home = os.environ.get("STORY_GATE_HOME") or os.path.join(
        os.environ.get("APPDATA", "") if os.name == "nt" else (os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")), "story-gate")
    try:
        enrolled_keys = open(os.path.join(home, "enrolled.json"), encoding="utf-8").read()
    except OSError:
        return True
    key = os.path.normcase(os.path.realpath(g))
    return json_escape(key) not in enrolled_keys


def json_escape(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


if sys.argv[1:2] == ["hook"] and not os.environ.get("STORY_GATE_ROOT") and _nothing_to_gate():
    print("{}")  # fast path: hooks fire in every folder, but outside a git repository there is nothing to gate
    sys.exit(0)

import fnmatch, hashlib, json, re, shlex, subprocess, time, urllib.request, urllib.error  # noqa: E402
from pathlib import Path  # noqa: E402

sys.dont_write_bytecode = True  # never leave __pycache__ inside the repo
HERE = Path(__file__).resolve().parent
sys.path[:] = [str(HERE)] + [p for p in sys.path if p not in ("", ".", str(HERE))]  # never import modules from the working tree
import sg_judges as J  # noqa: E402
import sg_trust as T  # noqa: E402

VERSION = "0.4.0"
RUNTIME = T.is_runtime(HERE)  # True when running the trusted copy installed with `gate.py install --user`


def _root():
    if os.environ.get("STORY_GATE_ROOT"):
        return Path(os.environ["STORY_GATE_ROOT"])
    if RUNTIME:  # the trusted runtime works on whichever repository the client is in
        top, _ = T.repo_identity(os.getcwd())
        return Path(top) if top else Path.cwd()
    return HERE.parent


ROOT = _root()
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
GATE = ROOT / ".story-gate"
STORIES = GATE / "stories"
LEARNINGS = GATE / "learnings.jsonl"
OUTBOX = GATE / "outbox.jsonl"
ACTIVE = GATE / ".active"
CALIB = GATE / "calibration.jsonl"
RECORD_PATHS = (".story-gate/stories", ".story-gate/features.json", ".story-gate/learnings.jsonl", ".story-gate/calibration.jsonl", ".story-gate/outbox.jsonl",
                ".story-gate/.outbox.sent", ".story-gate/.edits", ".story-gate/.stops", ".story-gate/.active", ".story-gate/__pycache__")  # records, not code


class ConfigError(Exception):
    pass

DEFAULT_CONFIG = {
    "version": 1,
    "mode": "warn",
    "enforce_points": [],
    "accept_concerns": False,
    "story_id_pattern": "[A-Z][A-Z0-9]+-[0-9]+",
    "base_branch": "main",
    "exempt_globs": [".story-gate/stories/*/*.md", ".story-gate/stories/*/tests.json", "docs/**/*.md", "*.md"],
    "test_command": "",
    "junit_path": "",
    "test_globs": ["**/test_*.py", "**/*_test.py", "**/tests/**", "**/test/**", "**/__tests__/**", "**/*.test.*", "**/*.spec.*", "**/*Test.java", "**/*_test.go"],
    "spec_files": [],
    "thresholds": {"pass": 0.7, "concerns": 0.4},
    "judge": {"provider": "openrouter", "emulated_allow_pass": False,
              "allow_self_judge_pass": False, "max_chars": 90000},
    "approvers": [],
    "reviewers": ["coderabbitai[bot]", "chatgpt-codex-connector[bot]"],
    "require_independent_review": True,
    "sources": [],
    "sinks": [{"type": "repo"}],
    "models": {"intake": "small", "context": "medium", "tests": "medium", "handoff": "medium", "learnings": "small"},
    "model_tiers": {"small": "cheapest fast model your client offers", "medium": "mid-tier coding model (never frontier)"},
    "checkpoint": {"every_edits": 10},
    "project_hooks_allowed": [],
    "dashboard_issue": None,
}

# ------------------------------------------------------------------ checks
# Each semantic check is one Jev question. "ok_high": True means a high probability is good.
READY_Q = {
    "dor_value": "The story states who benefits and why (user or business value).",
    "dor_scope": "Scope is specific: a developer knows exactly what to build, and what is out of scope is stated.",
    "dor_interfaces": "Inputs, outputs, data, APIs, UI or files the work touches are named concretely.",
    "dor_dependencies": "Upstream and downstream dependencies are listed, or explicitly stated as none.",
    "dor_nfr": "Relevant non-functional needs (security, privacy, performance, error handling, logging) are stated or explicitly not applicable.",
    "dor_small": "The story is small enough to build and verify in one focused pull request; it is not an epic.",
    "dor_no_blockers": "There are no unresolved open questions that would force the developer to guess.",
    "ac_testable": "Every acceptance criterion is binary and verifiable (clearly pass or fail), not vague.",
    "ac_covers_scope": "The acceptance criteria together cover the whole stated scope.",
    "tests_plan_adequate": "The test plan has meaningful positive, negative, edge and regression cases for every acceptance criterion, or a valid reason a category does not apply.",
    "drift_prd": "The story is consistent with the PRD excerpts: no contradiction and no scope the PRD does not support.",
    "drift_trd": "The story is consistent with the TRD / architecture excerpts.",
    "handoff_in": "Every upstream dependency has a handoff that gives this story what it needs (contracts, versions, how to use), or there are no upstream dependencies.",
    "learnings_applied": "Relevant prior learnings were considered and the story or test plan reflects them, or none were relevant.",
}
DONE_Q = {
    "impl_matches_acs": "The code diff implements every acceptance criterion of the story.",
    "impl_no_unplanned_scope": "The code diff does not add behaviour beyond the story's scope.",
    "drift_trd_after": "The implementation follows the TRD / architecture with no unrecorded deviation.",
    "tests_implemented": "The diff adds or updates automated tests covering the planned positive, negative and edge cases, plus regression cases where the plan lists them (a category the plan marks not applicable, with a reason, needs no test).",
    "no_deferrals": "The diff contains no TODO/FIXME/'temporary'/'later' markers or stubs that defer required work.",
    "handoff_out": "handoff.md lets the next story or agent use this work without asking: what changed, contracts/interfaces, how to verify, known limits, who consumes it.",
    "learnings_specific": "The recorded learnings for this story are specific and reusable (what happened, root cause, prevention rule); or 'no new learnings' is credible because nothing in the story, diff or test results went wrong or was surprising.",
}
DRIFT_CHOICE = {
    "none": "No drift: the story/work and the PRD/TRD agree.",
    "story_should_change": "The PRD/TRD is right; the story or the work should change to match it.",
    "spec_should_change": "The story/work reflects a deliberate, better decision; the PRD/TRD should be updated.",
    "architect_must_decide": "They disagree and it is not clear which is right; the architect/orchestrator must decide.",
}
DRIFT_CHOICE_DONE = {
    "none": "No drift: the code, the story and the PRD/TRD agree.",
    "work_should_change": "The story and PRD/TRD agree; the code deviates and should be fixed to match them.",
    "spec_should_change": "The code reflects a deliberate, better decision than the story/PRD/TRD; the documents should be updated.",
    "architect_must_decide": "The story and PRD/TRD conflict with each other or it is unclear which is right; the architect/orchestrator must decide.",
}
CHECK_GROUP = {  # how checks roll up for humans
    "dor_value": "1-ready", "dor_scope": "1-ready", "dor_interfaces": "1-ready", "dor_dependencies": "1-ready",
    "dor_nfr": "1-ready", "dor_small": "1-ready", "dor_no_blockers": "1-ready",
    "drift_prd": "2-drift", "drift_trd": "2-drift", "drift_trd_after": "2-drift", "impl_no_unplanned_scope": "2-drift",
    "ac_testable": "3-tests", "ac_covers_scope": "3-tests", "tests_plan_adequate": "3-tests",
    "impl_matches_acs": "3-tests", "tests_implemented": "3-tests", "no_deferrals": "3-tests",
    "handoff_in": "4-handoff", "handoff_out": "4-handoff",
    "learnings_applied": "5-learnings", "learnings_specific": "5-learnings",
}
TEST_CATS = ("positive", "negative", "edge", "regression")
CONTEXT_SECTIONS = ("## PRD", "## TRD", "## Upstream handoffs", "## Prior learnings")
HANDOFF_SECTIONS = ("## What changed", "## Interfaces and contracts", "## How to verify", "## Known limits", "## Downstream consumers",
                    "## Release and rollback", "## Drift decisions")


# ------------------------------------------------------------------ small helpers
def trusted(name):
    """Settings files. In CI they come from STORY_GATE_TRUSTED_DIR (the base branch's copies), never from the PR."""
    t = os.environ.get("STORY_GATE_TRUSTED_DIR")
    return Path(t) / name if t else GATE / name


def enrolled():
    """The trusted runtime's enrollment record for this repository (None outside the runtime or when not enrolled)."""
    if not RUNTIME or os.environ.get("STORY_GATE_TRUSTED_DIR"):
        return None
    e = T.enrollment(ROOT)
    return e if e and e.get("enrolled") else None


def policy_json(name):
    """A settings file from where policy comes from: CI's trusted copy, the enrolled default branch, or the working tree."""
    e = enrolled()
    if e:
        return T.policy_text(e["toplevel"], e["policy_ref"], name)
    p = trusted(name)
    return p.read_text(encoding="utf-8") if p.is_file() else None


def cfg():
    p = trusted("config.json")
    c = json.loads(json.dumps(DEFAULT_CONFIG))
    if os.environ.get("STORY_GATE_TRUSTED_DIR") and not p.is_file():
        raise ConfigError("the base branch's .story-gate/config.json is missing, so CI can't know the policy - the gate fails closed")
    e = enrolled()
    text = policy_json("config.json")
    if e and text is None:
        raise ConfigError("this repository is enrolled but %s has no .story-gate/config.json - the gate fails closed" % e["policy_ref"])
    if text is not None:
        try:
            user = json.loads(text)
            assert isinstance(user, dict)
        except Exception as ex:
            raise ConfigError(".story-gate/config.json is unreadable (%s) - fix it; the gate fails closed until then" % ex.__class__.__name__)
        if e:  # the working tree may only tighten the default branch's policy
            try:
                local = json.loads((GATE / "config.json").read_text(encoding="utf-8")) if (GATE / "config.json").is_file() else {}
            except Exception:
                local = {}
            base = json.loads(json.dumps(DEFAULT_CONFIG))
            for k, v in user.items():
                if isinstance(v, dict) and isinstance(base.get(k), dict):
                    base[k].update(v)
                else:
                    base[k] = v
            user = T.tighten(base, local)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(c.get(k), dict):
                c[k].update(v)
            else:
                c[k] = v
    if c.get("mode") not in ("warn", "enforce"):
        raise ConfigError('.story-gate/config.json: "mode" must be "warn" or "enforce" (got %r) - the gate fails closed until fixed' % c.get("mode"))
    bad = [x for x in c.get("enforce_points") or [] if x not in ("ci", "pre_edit", "checkpoint", "stop")]
    if bad:
        raise ConfigError('.story-gate/config.json: unknown enforce_points %s (use ci, pre_edit, checkpoint, stop)' % bad)
    return c


def rd(p, default=""):
    """Read a text file. Inside the repository, symlinks and paths that resolve outside it are never followed,
    so committed evidence can't point at /proc/self/environ or other secrets on a CI runner."""
    p = Path(p)
    try:
        inside = os.path.abspath(p).startswith(os.path.abspath(ROOT) + os.sep)
        if inside and (p.is_symlink() or not os.path.realpath(p).startswith(os.path.realpath(ROOT) + os.sep)):
            return default
        return p.read_text(encoding="utf-8", errors="ignore") if p.is_file() else default
    except OSError:
        return default


def wj(p, obj):
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    Path(p).write_text(json.dumps(obj, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def sdir(sid):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}", sid or ""):
        sys.exit("bad story id: %r" % sid)
    return STORIES / sid


def git(*args):
    try:
        r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def front_matter(text):
    """Tiny 'key: value' / 'key: [a, b]' front matter reader between --- lines."""
    m = re.match(r"\s*---\s*\n(.*?)\n---", text, re.S)
    out = {}
    for line in (m.group(1).splitlines() if m else []):
        if ":" in line:
            k, v = line.split(":", 1)
            v = v.strip()
            if v.startswith("[") and v.endswith("]"):
                v = [x.strip().strip("'\"") for x in v[1:-1].split(",") if x.strip()]
            out[k.strip()] = v
    return out


def sections(text):
    out, cur = {}, None
    for line in text.splitlines():
        if line.startswith("## "):
            cur = line.strip()
            out[cur] = ""
        elif cur:
            out[cur] += line + "\n"
    return out


def repo_rel(path):
    """Resolve a path the way the filesystem will (symlinks, '..', case on Windows/macOS) -> repo-relative posix or None."""
    p = str(path).replace("\\", "/")
    full = Path(p) if (os.path.isabs(p) or re.match(r"^[A-Za-z]:/", p)) else ROOT / p
    try:
        rel = Path(os.path.realpath(full)).relative_to(Path(os.path.realpath(ROOT)))
    except ValueError:
        try:  # case-insensitive filesystems: compare case-folded
            a, b = os.path.realpath(full), os.path.realpath(ROOT)
            if os.path.normcase(a).startswith(os.path.normcase(b) + os.sep):
                return a[len(b) + 1:].replace("\\", "/")
        except Exception:
            pass
        return None
    return rel.as_posix()


def exempt(path, c):
    p = repo_rel(path)
    if p is None or p.startswith(".."):
        return False  # outside the repo: not ours to exempt
    return any(fnmatch.fnmatch(p, g) for g in c["exempt_globs"])


HOOK_FILES = (".claude/settings.json", ".claude/settings.local.json", ".codex/hooks.json", ".codex/config.toml", ".cursor/hooks.json", ".gemini/settings.json",
              ".devin/hooks.json", ".windsurf/hooks.json", ".grok/hooks/story-gate.json")


STORY_DOCS = (".story-gate/stories/*/*.md", ".story-gate/stories/*/tests.json")


def user_protected(path):
    """The trusted runtime, enrollment, agent key and user-level hook files: never writable by an agent."""
    try:
        rp = os.path.normcase(os.path.realpath(os.path.expanduser(str(path))))
        for b in T.user_protected_paths():
            b = os.path.normcase(os.path.realpath(str(b)))
            if rp == b or rp.startswith(b.rstrip(os.sep) + os.sep):
                return True
    except Exception:
        return True  # can't tell: treat as protected
    return False


def protected(path, c):
    """Gate implementation, config and verdict/record files: never editable by an agent's edit tools."""
    if user_protected(path):
        return True
    p = repo_rel(path)
    if p is None:
        return False
    low = p.lower()
    if not (low.startswith(".story-gate/") or low.startswith(".git/") or low in HOOK_FILES or low in (".github/codeowners", "codeowners", "docs/codeowners")
            or low.startswith(".github/workflows/story-gate") or low.startswith(".agents/skills/story-gate/")
            or low.startswith(".claude/skills/story-gate/")):
        return False
    # Only the story documents agents must write stay editable; user exempt_globs (e.g. "*.md") never unprotect gate files.
    return not any(fnmatch.fnmatch(p, g) for g in STORY_DOCS)


def jsonl(p):
    out = []
    for line in rd(p).splitlines():
        try:
            r = json.loads(line) if line.strip() else None
            if isinstance(r, dict):
                out.append(r)
        except ValueError:
            pass
    return out


def load_json(p):
    try:
        r = json.loads(rd(p) or "{}")
        return r if isinstance(r, dict) else {}
    except ValueError:
        return {}


def spec_files(c):
    files = list(c.get("spec_files") or [])
    for s in c.get("sources") or []:
        if s.get("type") == "repo":
            files += [s[k] for k in ("prd", "trd") if s.get(k)]
    return sorted(set(f for f in files if f and (ROOT / f).is_file()))


def policy_fingerprint(c):
    j = c.get("judge") or {}
    return json.dumps({"v": VERSION, "th": c.get("thresholds"), "ac": c.get("accept_concerns"),  # policy: stricter rules re-score
                       "j": [J.identity(c), j.get("emulated_allow_pass"), j.get("allow_self_judge_pass")]}, sort_keys=True)


def ready_hash_from(story, context, tests, spec_pairs, c):
    """READY evidence hash from texts (used by `score` and by the dashboard, which reads them as git blobs)."""
    nl = lambda t: t.replace("\r\n", "\n")  # line endings aren't content: git blobs may keep CRLF that rd() reads as LF
    blob = nl(story) + nl(context) + nl(tests) + "".join(f + nl(t) for f, t in spec_pairs) + policy_fingerprint(c)
    return hashlib.sha256(blob.encode("utf-8", "ignore")).hexdigest()[:16]


def inputs_hash(sd, phase, c=None):
    if phase == "ready":
        try:
            c = c or cfg()
            return ready_hash_from(rd(sd / "story.md"), rd(sd / "context.md"), rd(sd / "tests.json"),
                                   [(f, rd(ROOT / f)) for f in spec_files(c)], c)
        except ConfigError:
            return hashlib.sha256((rd(sd / "story.md") + rd(sd / "context.md") + rd(sd / "tests.json") + "<config unreadable>")
                                  .encode("utf-8", "ignore")).hexdigest()[:16]
    names = ["story.md", "context.md", "tests.json"] + (["handoff.md"] if phase == "done" else [])
    blob = "".join(rd(sd / n) for n in names)
    if phase == "done":  # only the facts of the test run, so CI's own run of the same code yields the same evidence
        tr = load_json(sd / "test_results.json")
        blob += json.dumps({k: tr.get(k) for k in ("command", "exit_code", "fingerprint")}, sort_keys=True)
    try:
        c = c or cfg()
        blob += "".join(f + rd(ROOT / f) for f in spec_files(c))  # PRD/TRD pinned: a spec change makes READY out of date
        blob += policy_fingerprint(c)
    except ConfigError:
        blob += "<config unreadable>"
    if phase == "done":
        blob += "".join(json.dumps(r) for r in jsonl(LEARNINGS) if r.get("story") == sd.name) + work_fingerprint()
    return hashlib.sha256(blob.encode("utf-8", "ignore")).hexdigest()[:16]


def emit(event_type, sid, payload):
    OUTBOX.parent.mkdir(parents=True, exist_ok=True)
    with OUTBOX.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now(), "event_type": "story_gate." + event_type, "story": sid,
                            "repo": ROOT.name, "payload": payload}, ensure_ascii=False) + "\n")


# ------------------------------------------------------------------ judge (pluggable, see sg_judges.py)
def num(x):
    try:
        return float(x)
    except Exception:
        return 0.0


def jev(state, questions, c=None):
    """Back-compat wrapper used by checkpoints/doctor: {'answers', 'tier', ...} or {'error'}."""
    r = J.ask(c or cfg(), state, questions)
    return r if r.get("tier") != "none" else {"error": r.get("error")}


def calibrated(c):
    """An emulated judge may PASS only if the user opted in and its calibration run (same provider+model) passed."""
    if not (c.get("judge") or {}).get("emulated_allow_pass"):
        return False
    try:
        rec = json.loads(policy_json("judge-calibration.json") or "{}")
    except ValueError:
        rec = {}
    return rec.get("ok") is True and rec.get("identity") == J.identity(c)


def judge(phase, state, c, sd, skip=()):
    """Score semantic checks with the configured judge; else agent self-scores from <phase>.self.json."""
    qs_text = {k: v for k, v in (READY_Q if phase == "ready" else DONE_Q).items() if k not in skip}
    questions = {k: {"type": "noul", "instructions": v} for k, v in qs_text.items()}
    choices = DRIFT_CHOICE if phase == "ready" else DRIFT_CHOICE_DONE
    questions["drift_direction"] = {"type": "choice", "instructions": "Which way does any drift between the story/work and the PRD/TRD point?",
                                    "criteria": choices}
    r = J.ask(c, state, questions)
    if r.get("tier") in ("jev", "emulated"):
        a = r["answers"]
        tier, note = r["tier"], None
        if tier == "emulated":  # a general model must never grade work written by its own model family
            coder = load_json(sd / "coder.json").get("model") or ""
            if not coder:
                note = "coder model not recorded (run: start <ID> --model <coding model>), so this judge can't be shown to be independent"
            elif J.family(coder) == J.family(r.get("model") or ""):
                tier, note = "self", "judge %s is the same model family as the coder %s, so it counts as self-review" % (r.get("model"), coder)
            elif calibrated(c):
                tier = "emulated-calibrated"
        return {"judge": tier, "judge_note": note, "provider": r.get("provider"), "model": r.get("model"),
                "scores": {k: a[k]["noul"] for k in qs_text}, "drift": a["drift_direction"]["choice"],
                "drift_conf": a["drift_direction"]["confidence"], "cost": r.get("cost")}
    jerr = r.get("error")
    selfp = sd / ("%s.self.json" % phase)
    if selfp.exists():
        s = load_json(selfp)
        sc = s.get("scores") if isinstance(s.get("scores"), dict) else {}
        def f(x):
            try:
                return min(1.0, max(0.0, float(x)))
            except Exception:
                return 0.0
        d = s.get("drift") if s.get("drift") in choices else "architect_must_decide"
        return {"judge": "self", "scores": {k: f(sc.get(k, 0)) for k in qs_text}, "drift": d, "drift_conf": 0.0, "jev_error": jerr}
    return {"judge": "none", "scores": {}, "drift": "architect_must_decide", "drift_conf": 0.0, "jev_error": jerr}


def can_pass(judge_name, c):
    if judge_name in ("jev", "emulated-calibrated"):
        return True
    return judge_name == "self" and bool((c.get("judge") or {}).get("allow_self_judge_pass"))  # never widens emulated


def acs(sd):
    """[(id, text, test_refs)] from tests.json; text falls back to the AC line in story.md."""
    t = load_json(sd / "tests.json")
    story = rd(sd / "story.md")
    out = []
    for i, a in enumerate(t.get("acceptance_criteria") or []):
        if not isinstance(a, dict):
            continue
        aid = str(a.get("id") or "AC-%d" % (i + 1))
        text = a.get("text") or ""
        if not text or text == "TODO":
            m = re.search(r"^\W*" + re.escape(aid) + r"\W+(.+)$", story, re.M)
            text = m.group(1).strip() if m else aid
        refs = a.get("test_refs") if isinstance(a.get("test_refs"), list) else []
        out.append((aid, text, [str(r) for r in refs if str(r).strip()]))
    return out


# ------------------------------------------------------------------ structural checks (deterministic)
def struct_ready(sd):
    out = {}
    story = rd(sd / "story.md")
    fm = front_matter(story)
    out["story_present"] = (len(story.strip()) > 200 and "TODO: paste" not in story, "story.md missing, still a skeleton, or too thin")
    src = str(fm.get("source", "")).strip()
    out["story_source"] = (bool(src) and not src.upper().startswith("TODO"),
                           "story.md front matter 'source' must say where the story came from (linear:ID, repo:path, ...)")
    ctx = rd(sd / "context.md")
    secs = sections(ctx)
    missing = [s for s in CONTEXT_SECTIONS if not secs.get(s, "").strip() or "TODO" in secs.get(s, "")]
    pl = secs.get("## Prior learnings", "").strip().lower()
    if pl.startswith("none") and "searched" not in pl:
        missing.append("## Prior learnings (say 'None — searched: <keywords>')")
    out["context_present"] = (not missing, "context.md sections empty/TODO: " + ", ".join(missing))
    try:
        t = json.loads(rd(sd / "tests.json", "{}"))
        acs = t.get("acceptance_criteria", [])
        bad = [a.get("id", "?") for a in acs
               if any(not a.get(cat) and not (a.get("not_applicable") or {}).get(cat) for cat in TEST_CATS)]
        ok = bool(acs) and not bad
        out["test_matrix"] = (ok, "no acceptance criteria" if not acs else "ACs missing a positive/negative/edge/regression case (or N/A reason): " + ", ".join(bad))
    except Exception as e:
        out["test_matrix"] = (False, "tests.json invalid: %s" % e)
    deps = fm.get("depends_on") or []
    deps = [deps] if isinstance(deps, str) and deps and deps.lower() != "none" else (deps if isinstance(deps, list) else [])
    up = secs.get("## Upstream handoffs", "")
    pat = re.compile(r"(?:%s)" % cfg()["story_id_pattern"])
    bad_ids = [d for d in deps if not (isinstance(d, str) and pat.fullmatch(d))]  # story ids only: no paths, no traversal
    nohand = bad_ids + [d for d in deps if d not in bad_ids and not (STORIES / d / "handoff.md").exists()
                        and not re.search(r"^###\s+" + re.escape(d) + r"\b(?!-)", up, re.M)]
    out["upstream_handoffs"] = (not nohand, "no handoff found for upstream: " + ", ".join(nohand))
    return out, fm


def test_corpus(c):
    """Current contents of test files in the working tree (deleted tests and production code never count)."""
    files = set(zlist("ls-files")) | set(zlist("ls-files", "--others", "--exclude-standard"))
    out = []
    for f in sorted(files):
        if f.startswith(".story-gate/"):
            continue
        if any(fnmatch.fnmatch(f, g) or fnmatch.fnmatch("/" + f, g) for g in c.get("test_globs") or []):
            p = ROOT / f
            if p.is_file() and p.stat().st_size < 2_000_000:
                out.append(rd(p))
    return "\n".join(out)


def defined(ref, corpus):
    """The ref must be a test definition, not merely mentioned (a comment or string elsewhere does not count)."""
    e = re.escape(ref)
    pats = [r"\b(?:async\s+)?(?:def|func|function|fn|sub)\s+" + e + r"\s*[\(<\[]",                # python, go, js, rust, perl
            r"\b(?:void|public\s+void|fun|func)\s+" + e + r"\s*\(",                              # java, kotlin
            r"\b(?:test|it|describe|context|scenario|specify)(?:\.\w+)?\s*\(\s*['\"`]" + e + r"['\"`]",  # js/ts/rspec style
            r"\b(?:Scenario|Feature):\s*" + e + r"\s*$"]                                        # gherkin
    return any(re.search(p, corpus, re.M) for p in pats)


def trace(sd, c, tr, results=None):
    """AC -> planned cases -> automated tests -> exists in current test files -> executed outcome. Writes trace.md."""
    t = load_json(sd / "tests.json")
    plan = {str(a.get("id")): a for a in (t.get("acceptance_criteria") or []) if isinstance(a, dict)}
    corpus = test_corpus(c)
    suite = "GREEN" if tr.get("exit_code") == 0 else ("RED" if tr else "NOT RUN")
    rows, missing = [], []
    for aid, text, refs in acs(sd):
        a = plan.get(aid, {})
        cases = sum(len(a.get(k) or []) for k in TEST_CATS)
        found = [r for r in refs if defined(r, corpus)]
        if results is not None:
            import sg_github as G
            outcomes = {r: G.ref_outcome(r, results) for r in refs}
            ok = bool(refs) and len(found) == len(refs) and all(o == "passed" for o in outcomes.values())  # in our test files AND passed
            res = ", ".join("%s=%s" % kv for kv in outcomes.items()) or "—"
        else:
            ok = bool(refs) and len(found) == len(refs) and suite == "GREEN"
            res = suite
        if not ok:
            missing.append(aid)
        rows.append("| %s | %s | %d | %s | %s | %s |" % (aid, text[:60].replace("|", "/"), cases, ", ".join(refs) or "—",
                                                     "%d/%d" % (len(found), len(refs)), res))
    md = ("# Traceability: %s\n\n| AC | Criterion | Planned cases | Automated tests (test_refs) | In current test files | Result |\n"
          "|---|---|---|---|---|---|\n" % sd.name)
    wj_text(sd / "trace.md", md + "\n".join(rows) + "\n")
    return missing


def wj_text(p, text):
    Path(p).write_text(text, encoding="utf-8")


def struct_done(sd, sid, diff_files, c, diff_text="", results=None, truncated=False):
    out = {}
    rj = load_json(sd / "ready.json")
    out["ready_gate_passed"] = (passed(rj, c, sd), "READY gate not passed or out of date (overall=%s; the story, test plan or PRD/TRD changed since) - re-run READY" % rj.get("overall", "never run"))
    h = rd(sd / "handoff.md")
    hs = sections(h)
    miss = [s for s in HANDOFF_SECTIONS if not hs.get(s, "").strip() or "TODO" in hs.get(s, "")]
    out["handoff_written"] = (bool(h) and not miss, "handoff.md missing or sections empty/TODO: " + ", ".join(miss or ["(file)"]))
    has_learn = any(r.get("story") == sid for r in jsonl(LEARNINGS))
    out["learnings_recorded"] = (has_learn, "no learnings entry for this story (use 'learn --type none' if truly nothing)")
    tr = load_json(sd / "test_results.json")
    fresh = tr.get("fingerprint") == work_fingerprint()
    pinned = c.get("test_command")
    out["tests_ran_green"] = (tr.get("exit_code") == 0 and fresh and (not pinned or tr.get("command") == pinned),
                              "the recorded test command is not config test_command (%s)" % pinned
                              if tr and pinned and tr.get("command") != pinned else
                              "no recorded test run (use record-tests)" if not tr else
                              "last recorded test run failed (exit %s)" % tr.get("exit_code") if tr.get("exit_code") != 0 else
                              "code changed after the last green test run — run record-tests again")
    code = [f for f in diff_files if not exempt(f, c)]
    out["code_changed"] = (bool(code), "no code changes found against base")
    miss = trace(sd, c, tr, results)
    out["traceability"] = (not miss, "ACs whose tests (tests.json test_refs) are missing from the current test files or did not pass: " + ", ".join(miss))
    if truncated:
        out["evidence_complete"] = ("CONCERNS", "the change is too large for one judge pass; split the story or get a human review of the whole diff")
    return out


# ------------------------------------------------------------------ verdict (pure, unit-tested)
def band(p, th):
    return "PASS" if p >= th["pass"] else ("CONCERNS" if p >= th["concerns"] else "FAIL")


def verdict(structural, judged, decisions, waivers, c):
    th = c["thresholds"]
    checks = {}
    for k, (ok, why) in structural.items():
        st = ok if isinstance(ok, str) else ("PASS" if ok else "FAIL")
        checks[k] = {"status": st, "why": "" if st == "PASS" else why, "kind": "structural"}
    for k, p in judged.get("scores", {}).items():
        st = band(p, th)
        if st == "PASS" and not can_pass(judged["judge"], c):
            st = "CONCERNS"  # an uncalibrated or self judge cannot award a PASS
        checks[k] = {"status": st, "score": round(p, 3), "kind": judged["judge"], "group": CHECK_GROUP.get(k, "")}
    if judged["judge"] == "none":
        checks["judge_available"] = {"status": "FAIL", "why": "no judge and no self-scores: semantic checks did not run (%s)" % judged.get("jev_error"), "kind": "structural"}
    drift = judged.get("drift", "none")
    decided = decisions[-1] if decisions else None
    if drift == "work_should_change":  # code is simply wrong vs agreed docs: a fix, not an escalation
        checks["drift_fix_work"] = {"status": "FAIL", "why": "code deviates from the story/TRD — fix the code (no spec change needed)", "kind": "decision"}
    elif drift != "none":
        if decided and decided.get("drift") == "story":
            checks["drift_decision"] = {"status": "FAIL", "why": "decided by %s: the story or the work must change. Apply the decision, then re-score" % decided.get("by"), "kind": "decision"}
        elif decided:
            checks["drift_decision"] = {"status": "PASS", "why": "decided: %s by %s%s" % (decided["drift"], decided["by"], "" if decided.get("_trusted") else " (proposal: needs a code owner's approval in CI)"), "kind": "decision"}
        else:
            checks["drift_decision"] = {"status": "ESCALATED", "why": "drift points to '%s' — needs a recorded decision from the architect/orchestrator" % drift, "kind": "decision"}
    for k, w in waivers.items():
        if k in checks and checks[k]["status"] != "PASS" and checks[k]["kind"] != "structural":
            checks[k]["status"], checks[k]["why"] = "WAIVED", "waived by %s: %s%s" % (w["by"], w["reason"], "" if w.get("_trusted") else " (proposal: needs a code owner's approval in CI)")
    sts = [v["status"] for v in checks.values()]
    overall = ("FAIL" if "FAIL" in sts else "ESCALATED" if "ESCALATED" in sts else
               "CONCERNS" if "CONCERNS" in sts else "WAIVED" if "WAIVED" in sts else "PASS")
    return {"overall": overall, "drift": drift, "checks": checks}


def passed(v, c, sd=None):
    v = v or {}
    o = v.get("overall")
    if not can_pass(v.get("judge"), c):
        return False  # without a trusted judge nothing passes
    if sd is not None and v.get("inputs_hash") != inputs_hash(sd, v.get("phase", "ready"), c):
        return False  # story/tests/handoff/code/spec changed since this verdict: re-score
    return o in ("PASS", "WAIVED") or (o == "CONCERNS" and bool(c.get("accept_concerns")))


def stale(v, c, sd):
    return bool(v) and v.get("inputs_hash") != inputs_hash(sd, v.get("phase", "ready"), c)


def records(sd, phase, c=None, trusted=False):
    """Drift decisions and waivers that still match the evidence they were made on.
    trusted=True only in CI after a code owner approved the current head commit; locally they are proposals."""
    dec, wav = [], {}
    cur = inputs_hash(sd, phase, c)
    for r in jsonl(sd / "decisions.jsonl"):
        if r.get("phase", phase) != phase or r.get("evidence") != cur:
            continue  # made on different evidence (or unbound): does not carry over
        r = dict(r, _trusted=trusted)
        if r.get("kind") == "drift":
            dec.append(r)
        elif r.get("kind") == "waiver" and r.get("check") in READY_Q.keys() | DONE_Q.keys():
            wav[r["check"]] = r  # only semantic checks can be waived; structural facts cannot
    return dec, wav


# ------------------------------------------------------------------ commands
SKELETONS = {
    "story.md": """---
id: {id}
title: TODO
feature: none
source: TODO (linear:{id} | repo:path | control-hub:doc-id | other)
depends_on: []
consumers: []
---
TODO: paste the full story text: who/why, scope, out of scope, interfaces, NFRs, acceptance criteria (AC-1, AC-2, ...).
""",
    "context.md": """# Context for {id}
Each section: quote only the relevant excerpts and cite the source (file#heading, Linear URL, control-hub id).
Write 'None — <reason>' if a section truly has nothing. Never leave TODO.

## PRD
TODO

## TRD
TODO

## Upstream handoffs
TODO (one '### <story-id>' block per upstream dependency, or 'None — no upstream dependencies')

## Prior learnings
TODO (paste relevant hits from `gate.py learnings <words>` with their ids, or 'None — searched: <words>')
""",
    "tests.json": json.dumps({"story": "{id}", "acceptance_criteria": [
        {"id": "AC-1", "text": "TODO", "positive": [], "negative": [], "edge": [], "regression": [], "not_applicable": {}, "test_refs": []}],
        "proposed_missing_acs": []}, indent=1),
    "handoff.md": """# Handoff: {id}

## What changed
TODO

## Interfaces and contracts
TODO (APIs, schemas, events, env vars, versions — exact names)

## How to verify
TODO (commands / steps a downstream agent can run)

## Known limits
TODO (or 'None')

## Downstream consumers
TODO (story ids / systems that depend on this and what they need from it)

## Release and rollback
TODO (how it ships, feature flag, migrations, monitoring, how to roll back - or 'Not applicable: <reason>')

## Drift decisions
None
""",
}


CLIENT_ENV = (("CLAUDECODE", "claude-code"), ("CURSOR_TRACE_ID", "cursor"), ("CODEX_SANDBOX", "codex"), ("GEMINI_CLI", "gemini"))


def cmd_start(sid, client=None, model=None):
    sd = sdir(sid)
    sd.mkdir(parents=True, exist_ok=True)
    client = client or next((name for var, name in CLIENT_ENV if os.environ.get(var)), "")
    if client or model or not (sd / "coder.json").exists():
        old = load_json(sd / "coder.json")  # who is coding (the claim): dashboard, and a judge must never be the coder's model
        wj(sd / "coder.json", {"schema": 1, "client": client or old.get("client", ""), "model": model or old.get("model", ""),
                               "branch": current_branch(), "claimed_at": old.get("claimed_at") or now(), "at": now()})
    for name, body in SKELETONS.items():
        p = sd / name
        if not p.exists() and name != "handoff.md":
            p.write_text(body.replace("{id}", sid), encoding="utf-8")
    # scoped to this branch; the start commit is the story's baseline if work is committed straight onto the base branch
    ACTIVE.write_text("%s\n%s\n%s\n" % (sid, current_branch(), git("rev-parse", "HEAD").strip()), encoding="utf-8")
    emit("started", sid, load_json(sd / "coder.json"))
    print("story-gate: active story %s -> fill %s (story.md, context.md, tests.json), then: score %s ready" % (sid, sd.relative_to(ROOT), sid))


FEATURE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}")


def cmd_plan(sid, title, feature=None):
    """Add a story to the backlog without starting it (no claim, no active story). Commit it so the dashboard sees it."""
    sd = sdir(sid)
    if feature and not FEATURE_ID.fullmatch(feature):
        sys.exit("feature ids are letters, digits, . _ - (max 40)")
    sd.mkdir(parents=True, exist_ok=True)
    for name, body in SKELETONS.items():
        p = sd / name
        if not p.exists() and name != "handoff.md":
            p.write_text(body.replace("{id}", sid), encoding="utf-8")
    text = rd(sd / "story.md")
    if title:
        text = re.sub(r"(?m)^title:.*$", lambda m: "title: " + title.replace("\n", " ")[:120], text, count=1)
    if feature:
        text = re.sub(r"(?m)^feature:.*$", lambda m: "feature: " + feature, text, count=1)
    (sd / "story.md").write_text(text, encoding="utf-8")
    emit("planned", sid, {"title": title, "feature": feature})
    print("story-gate: planned %s%s. Fill story.md, context.md and tests.json, then score %s ready. It's queued once READY passes."
          % (sid, " in feature " + feature if feature else "", sid))


def cmd_feature(fid, title, description=""):
    if not FEATURE_ID.fullmatch(fid or ""):
        sys.exit("feature <ID> --title TEXT   (ids: letters, digits, . _ -)")
    p = GATE / "features.json"
    data = load_json(p)
    data[fid] = {"title": (title or data.get(fid, {}).get("title") or fid)[:120], "description": (description or data.get(fid, {}).get("description", ""))[:500]}
    wj(p, data)
    print("story-gate: feature %s saved in .story-gate/features.json" % fid)


def current_branch():
    return git("rev-parse", "--abbrev-ref", "HEAD").strip()


def cmd_source(sid):
    sdir(sid)  # validates the id before it goes anywhere near a shell
    for s in cfg().get("sources", []):
        if s.get("type") == "command" and s.get("command"):
            cmd = s["command"].replace("{id}", shlex.quote(sid))
            r = subprocess.run(cmd, shell=True, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=120, env=dict(os.environ, STORY_GATE_ID=sid))
            print("### source: %s (exit %s)\n%s" % (s.get("name", cmd), r.returncode, r.stdout[-20000:]))
        else:
            print("### source (%s): fetch with your tools — %s" % (s.get("type"), json.dumps({k: v for k, v in s.items() if k != "type"})))


def work_fingerprint():
    """Hash of the changed files' current content vs the base branch, ignoring .story-gate/.
    Content-based, so git add / commit do not invalidate a green test run; any edit does."""
    try:
        mb = resolve_base(cfg()["base_branch"])
    except Exception:
        mb = "HEAD"
    names = sorted(set(zlist("diff", "--name-only", mb)) | set(zlist("diff", "--name-only"))
                   | set(zlist("ls-files", "--others", "--exclude-standard")))
    h = hashlib.sha256()
    for n in names:
        if is_record(n):
            continue
        f = ROOT / n
        mode = b"x" if f.is_file() and os.access(f, os.X_OK) else b"-"  # executable bit is part of the evidence
        if f.is_file():
            body = hashlib.sha256(f.read_bytes()).digest()
        elif f.is_dir():  # a submodule: its checked-out commit is the evidence
            sub = git("-C", str(f), "rev-parse", "HEAD") + git("-C", str(f), "status", "--porcelain") + git("-C", str(f), "diff", "HEAD")
            body = b"GITLINK:" + hashlib.sha256(sub.encode("utf-8", "replace")).digest()  # commit plus any uncommitted edits
        else:
            body = b"DELETED"
        h.update(n.encode("utf-8", "replace") + b"\0" + mode + body)
    return h.hexdigest()[:16]


def is_record(path):
    return any(path == r or path.startswith(r + "/") for r in RECORD_PATHS)


def partially_staged():
    """Files whose staged version differs from both the last commit and the working copy (git add -p)."""
    staged = set(zlist("diff", "--cached", "--name-only"))
    unstaged = set(zlist("diff", "--name-only"))
    return sorted(f for f in staged & unstaged if not is_record(f))


def cmd_record_tests(sid, argv):
    c = cfg()
    pinned = c.get("test_command")
    if pinned:
        argv = pinned
    if not argv:
        sys.exit("usage: record-tests <ID> -- <test command...>   (or set test_command in .story-gate/config.json)")
    split = partially_staged()
    if split:
        sys.exit("story-gate: %s staged differently from the working copy. Tests must run on exactly what you will commit: "
                 "stage everything (git add) or unstage it, then re-run record-tests." % ", ".join(split[:5]))
    t0 = time.time()
    try:
        r = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800,
                           shell=isinstance(argv, str) or os.name == "nt")
        code, tail = r.returncode, (r.stdout + r.stderr)[-4000:]
    except Exception as e:
        code, tail = -1, repr(e)
    rec = {"command": argv if isinstance(argv, str) else " ".join(argv), "exit_code": code, "seconds": round(time.time() - t0, 1),
           "head": git("rev-parse", "HEAD").strip(), "fingerprint": work_fingerprint(), "at": now(), "output_tail": tail}
    if c.get("junit_path") and (ROOT / c["junit_path"]).is_file():
        import sg_github as G
        try:
            rec["junit"] = G.junit(ROOT / c["junit_path"])
        except Exception as e:
            rec["junit_error"] = str(e)
    wj(sdir(sid) / "test_results.json", rec)
    print("story-gate: tests %s (exit %s)" % ("GREEN" if code == 0 else "RED", code))
    return 0 if code == 0 else 1


def resolve_base(base):
    """First of base, origin/base, master, origin/master that exists; on the base branch itself use its upstream."""
    if not git("rev-parse", "--git-dir").strip():
        raise RuntimeError("not a git repository — cannot see what changed")
    for b in (base, "origin/" + base if not base.startswith("origin/") else base, "master", "origin/master"):
        if git("rev-parse", "--verify", "--quiet", b + "^{commit}").strip():
            mb = git("merge-base", "HEAD", b).strip()
            if mb and mb == git("rev-parse", "HEAD").strip():  # on the base branch: compare with its upstream if ahead
                up = git("rev-parse", "--verify", "--quiet", "@{upstream}").strip()
                if up:
                    mb = git("merge-base", "HEAD", up).strip()
                else:  # no upstream: compare with the commit where the active story started, so committed work stays visible
                    act = rd(ACTIVE).split()
                    if len(act) >= 3 and subprocess.run(["git", "merge-base", "--is-ancestor", act[2], "HEAD"], cwd=ROOT,
                                                        capture_output=True).returncode == 0:
                        mb = act[2]
            return mb or b
    raise RuntimeError("base branch %r not found (set base_branch in .story-gate/config.json)" % base)


def zlist(*args):
    return [x for x in git("-c", "core.quotepath=false", *args, "-z").split("\0") if x]


def diff_against(base):
    mb = resolve_base(base)
    untracked = [n for n in zlist("ls-files", "--others", "--exclude-standard") if not is_record(n)]
    names = set(zlist("diff", "--name-only", mb)) | set(zlist("diff", "--name-only")) | set(untracked)
    excl = [":(exclude)%s" % r for r in RECORD_PATHS]
    diff = git("diff", mb, "--", ".", *excl)  # story records never count as code evidence; the gate's own code does
    for n in untracked[:200]:  # new files are evidence too, even before `git add`
        f = ROOT / n
        if f.is_file() and f.stat().st_size < 500_000:
            body = rd(f)
            diff += "\ndiff --git a/%s b/%s\nnew file (untracked)\n+++ b/%s\n" % (n, n, n) + "".join("+" + l + "\n" for l in body.splitlines())
    return mb, sorted(names), diff


def cmd_score(sid, phase, base=None, ci_trust=None, results=None, quiet=False):
    """ci_trust: None = local run (waivers/decisions shown as proposals), True = CI with a code owner's approval
    on the head commit (honoured), False = CI without it (ignored). results = per-test outcomes from CI's own run."""
    c = cfg()
    sd = sdir(sid)
    if not sd.exists():
        sys.exit("no story folder; run: start %s" % sid)
    mx = int(c["judge"].get("max_chars", 90000))
    if phase == "ready":
        structural, fm = struct_ready(sd)
        pl = sections(rd(sd / "context.md")).get("## Prior learnings", "").strip().lower()
        skip = {"learnings_applied"} if pl.startswith("none") and "searched" in pl else set()  # skip only with search evidence
        state = {"story": rd(sd / "story.md")[:mx // 3], "context": rd(sd / "context.md")[:mx // 3],
                 "test_plan": rd(sd / "tests.json")[:mx // 3]}
    elif phase == "done":
        mb, files, diff = diff_against(base or c["base_branch"])
        budget = mx // 2
        if results is None:
            results = (load_json(sd / "test_results.json").get("junit") or None)
        structural = struct_done(sd, sid, files, c, diff, results, truncated=len(diff) > budget)
        skip = set()
        learn = [l for l in rd(LEARNINGS).splitlines() if '"%s"' % sid in l]
        state = {"story": rd(sd / "story.md")[:mx // 6], "context": rd(sd / "context.md")[:mx // 6],
                 "test_plan": rd(sd / "tests.json")[:mx // 8], "handoff": rd(sd / "handoff.md")[:mx // 8],
                 "learnings": "\n".join(learn)[:mx // 16], "changed_files": files[:300],
                 "diff": diff[:budget], "diff_truncated": len(diff) > budget}
    else:
        sys.exit("phase must be ready or done")
    judged = judge(phase, state, c, sd, skip)
    dec, wav = ([], {}) if ci_trust is False else records(sd, phase, c, trusted=bool(ci_trust))
    v = verdict(structural, judged, dec, wav, c)
    v.update({"story": sid, "phase": phase, "at": now(), "judge": judged["judge"], "judge_provider": judged.get("provider"),
              "judge_model": judged.get("model"), "judge_note": judged.get("judge_note"), "jev_error": judged.get("jev_error"), "inputs_hash": inputs_hash(sd, phase, c),
              "drift_confidence": judged.get("drift_conf"), "cost": judged.get("cost"), "gate_version": VERSION})
    wj(sd / ("%s.json" % phase), v)
    failed = [k for k, x in v["checks"].items() if x["status"] not in ("PASS", "WAIVED")]
    emit(phase + "_scored", sid, {"overall": v["overall"], "drift": v["drift"], "judge": v["judge"], "failed": failed})
    if (v["checks"].get("drift_decision") or {}).get("status") == "ESCALATED":  # even when another check FAILs
        emit("drift_escalated", sid, {"direction": v["drift"], "phase": phase})
    calib_log(sid, phase, v, judged)
    if not quiet:
        print_verdict(v)
    return 0 if passed(v, c, sd) else 1


def calib_log(sid, phase, v, judged):
    """Every verdict is logged so thresholds can be tuned against human labels (gate.py label)."""
    qset = hashlib.sha256(json.dumps([READY_Q, DONE_Q], sort_keys=True).encode()).hexdigest()[:10]
    row = {"at": v["at"], "story": sid, "phase": phase, "overall": v["overall"], "judge": v["judge"], "provider": judged.get("provider"),
           "model": judged.get("model"), "questions": qset, "gate_version": VERSION, "inputs_hash": v["inputs_hash"],
           "scores": judged.get("scores"), "drift": v["drift"]}
    with CALIB.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def cmd_label(sid, phase, label, note):
    if label not in ("correct", "wrong"):
        sys.exit("label <ID> ready|done correct|wrong [--note TEXT]")
    with CALIB.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": now(), "story": sid, "phase": phase, "label": label, "note": note}) + "\n")
    print("story-gate: labelled %s %s as %s (used to tune thresholds)" % (sid, phase, label))


DRIFT_CHOICE_MID = {
    "none": "The work so far is consistent with the story and PRD/TRD (being unfinished is fine).",
    "work_should_change": "The work so far deviates from the agreed story/TRD and should be corrected now.",
    "spec_should_change": "The work deliberately departs from the story/PRD/TRD in a way that may be better; the documents may need updating.",
    "architect_must_decide": "The work exposes a conflict between story and PRD/TRD that someone must decide.",
}


CHECKPOINT_BUDGET = 150  # seconds; post-edit hooks allow 180


def cmd_checkpoint(sid, quiet=False):
    """Mid-story Jev check: per-AC progress, on course?, drift, deferrals, tests keeping pace. Never blocks by itself."""
    c = cfg()
    sd = sdir(sid)
    mx = int(c["judge"].get("max_chars", 90000))
    _, files, diff = diff_against(c["base_branch"])
    items = acs(sd)
    base_q = {
        "on_course": "The work so far is heading toward the story's acceptance criteria and follows the TRD (unfinished is fine).",
        "no_unplanned_scope": "The work so far stays inside the story's scope (no extra features or rewrites).",
        "no_deferrals": "The work so far has no TODO/FIXME/'temporary'/stub markers that defer required work.",
        "tests_keeping_pace": "Automated tests are being written alongside the implemented behaviour (not left for the end).",
    }
    state = {"story": rd(sd / "story.md")[:mx // 6], "context": rd(sd / "context.md")[:mx // 8],
             "test_plan": rd(sd / "tests.json")[:mx // 8], "changed_files": files[:300], "diff": diff[:mx // 2]}
    scores, drift, conf, err = {}, "none", 0.0, None
    chunks = [items[i:i + 12] for i in range(0, len(items), 12)] or [[]]
    deadline = time.time() + CHECKPOINT_BUDGET  # stays inside the clients' hook timeouts
    for n, chunk in enumerate(chunks):
        left = deadline - time.time()
        if left < 5:
            err = "checkpoint ran out of time (%ds budget); run `gate.py checkpoint %s` by hand" % (CHECKPOINT_BUDGET, sid); break
        cc = json.loads(json.dumps(c)); cc["judge"]["timeout"] = int(min(left, int(c["judge"].get("timeout", 90))))
        q = {"ac_%02d" % j: {"type": "noul", "instructions": "The code so far fully implements %s: %s" % (aid, text)}
             for j, (aid, text, _) in enumerate(chunk)}
        if n == 0:
            q.update({k: {"type": "noul", "instructions": v} for k, v in base_q.items()})
            q["drift_direction"] = {"type": "choice", "instructions": "Is the work drifting from the story/PRD/TRD?", "criteria": DRIFT_CHOICE_MID}
        r = jev(state, q, cc)
        if not isinstance(r.get("answers"), dict):
            err = r.get("error") or "unexpected Jev response"; break
        a = r["answers"]
        for j, (aid, _, _) in enumerate(chunk):
            scores[aid] = num((a.get("ac_%02d" % j) or {}).get("noul"))
        if n == 0:
            for k in base_q:
                scores[k] = num((a.get(k) or {}).get("noul"))
            d = a.get("drift_direction") if isinstance(a.get("drift_direction"), dict) else {}
            drift = d.get("choice") if d.get("choice") in DRIFT_CHOICE_MID else "architect_must_decide"
            conf = num(d.get("confidence"))
    if err:
        res = {"story": sid, "at": now(), "status": "UNKNOWN", "error": err}
        if not quiet:
            print("story-gate %s CHECKPOINT: UNKNOWN (judge unavailable: %s)" % (sid, err))
        return res
    ac_p = [scores[aid] for aid, _, _ in items]
    pct = int(round(100 * sum(ac_p) / len(ac_p))) if ac_p else 0
    done_n = sum(p >= 0.7 for p in ac_p)
    prev = (jsonl(sd / "checkpoints.jsonl") or [{}])[-1]
    why = []
    if drift != "none" and conf >= 0.6:
        why.append("drift: " + drift)
    if scores["on_course"] < 0.4: why.append("off course (%.2f)" % scores["on_course"])
    if scores["no_unplanned_scope"] < 0.4: why.append("scope creep (%.2f)" % scores["no_unplanned_scope"])
    status = "OFF_COURSE" if why else "ON_TRACK"
    if status == "ON_TRACK":
        if scores["on_course"] < 0.7: why.append("course unclear (%.2f)" % scores["on_course"])
        if scores["no_deferrals"] < 0.5: why.append("deferred work / TODOs")
        if scores["tests_keeping_pace"] < 0.4 and done_n: why.append("tests lagging behind code")
        if isinstance(prev.get("percent"), int) and pct < prev["percent"] - 15: why.append("progress went backwards (%d%% -> %d%%)" % (prev["percent"], pct))
        status = "AT_RISK" if why else "ON_TRACK"
    res = {"story": sid, "at": now(), "status": status, "percent": pct, "acs_done": done_n, "acs_total": len(items),
           "per_ac": {aid: round(scores[aid], 2) for aid, _, _ in items}, "drift": drift, "drift_conf": round(conf, 2),
           "signals": {k: round(scores[k], 2) for k in base_q}, "why": why, "files": len(files)}
    with (sd / "checkpoints.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(res) + "\n")
    emit("checkpoint", sid, {k: res[k] for k in ("status", "percent", "acs_done", "acs_total", "drift", "why")})
    if status == "OFF_COURSE" and drift in ("spec_should_change", "architect_must_decide"):
        emit("drift_escalated", sid, {"direction": drift, "phase": "build"})
    if not quiet:
        print(checkpoint_line(res))
        for aid, p in res["per_ac"].items():
            print("  %-8s %s %.2f" % (aid, "done   " if p >= 0.7 else "partial" if p >= 0.4 else "todo   ", p))
    return res


def checkpoint_line(res):
    return "story-gate %s CHECKPOINT: %s ~%d%% (%d/%d ACs done, estimate)%s" % (
        res["story"], res["status"], res.get("percent", 0), res.get("acs_done", 0), res.get("acs_total", 0),
        " — " + "; ".join(res["why"]) if res.get("why") else "")


def contained(sid, c):
    """Last checkpoint OFF_COURSE and no later decision -> message, else ''."""
    sd = STORIES / sid
    cps = jsonl(sd / "checkpoints.jsonl")
    if not cps or cps[-1].get("status") != "OFF_COURSE":
        return ""
    later = [d for d in jsonl(sd / "decisions.jsonl") if d.get("kind") == "drift" and d.get("phase") == "build" and d.get("at", "") >= cps[-1].get("at", "")]
    if later:
        return ""
    return "Story %s is OFF COURSE at the last checkpoint (%s). Correct the work and re-run `gate.py checkpoint %s`, or record `gate.py decide %s --phase build --drift story|spec|none --by <who>`." % (
        sid, "; ".join(cps[-1].get("why", [])), sid, sid)


def print_verdict(v):
    print("story-gate %s %s: %s  (judge=%s, drift=%s)" % (v["story"], v["phase"].upper(), v["overall"], v["judge"], v["drift"]))
    if v.get("judge_note"):
        print("  note: " + v["judge_note"])
    for k, x in sorted(v["checks"].items(), key=lambda kv: (kv[1].get("group", ""), kv[0])):
        if x["status"] != "PASS":
            print("  %-10s %-24s %s" % (x["status"], k, x.get("why") or "score=%s" % x.get("score")))


def append_decision(sid, rec):
    sd = sdir(sid)
    sd.mkdir(parents=True, exist_ok=True)
    rec["at"] = now()
    if rec.get("phase") in ("ready", "done"):
        rec["evidence"] = inputs_hash(sd, rec["phase"])  # only valid for the evidence it was made on
    with (sd / "decisions.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    emit(rec["kind"], sid, rec)
    print("story-gate: recorded %s for %s — re-run score to refresh the verdict" % (rec["kind"], sid))


def cmd_learn(sid, kv):
    t = kv.get("type")
    if t not in ("error", "learning", "pattern", "none") or not kv.get("summary"):
        sys.exit("learn needs --type error|learning|pattern|none and --summary")
    if t in ("error", "pattern") and not (kv.get("root-cause") and kv.get("rule")):
        sys.exit("errors/patterns need --root-cause and --rule (the prevention rule other agents should follow)")
    rec = {"id": "L-%s-%d" % (sid, int(time.time() * 1000) % 10 ** 9), "story": sid, "type": t, "summary": kv["summary"],
           "root_cause": kv.get("root-cause", ""), "rule": kv.get("rule", ""),
           "tags": [x.strip() for x in kv.get("tags", "").split(",") if x.strip()],
           "client": kv.get("client", ""), "at": now()}
    LEARNINGS.parent.mkdir(parents=True, exist_ok=True)
    with LEARNINGS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    emit("learning", sid, rec)
    print("story-gate: learning %s saved" % rec["id"])


def cmd_learnings(words):
    rows = jsonl(LEARNINGS)  # tolerant: a bad line (e.g. merge-conflict marker) is skipped, never fatal
    ws = [w.lower() for w in words]
    hits = []
    for r in rows:
        if r.get("type") == "none":
            continue
        blob = json.dumps(r).lower()
        score = sum(blob.count(w) for w in ws) if ws else 1
        if score:
            hits.append((score, r))
    hits.sort(key=lambda x: -x[0])
    rules = {}
    for r in rows:
        if r.get("rule"):
            rules.setdefault(str(r["rule"]).strip().lower(), []).append(r.get("story", "?"))
    for _, r in hits[:20]:
        tags = r.get("tags") if isinstance(r.get("tags"), list) else []
        print("- [%s] %s (%s) %s%s" % (r.get("id", "?"), r.get("summary", ""), r.get("type", "?"),
                                      ("RULE: " + str(r["rule"])) if r.get("rule") else "", "  tags=" + ",".join(map(str, tags)) if tags else ""))
    for k, v in rules.items():
        if len(v) >= 3:
            print("! repeated %dx — promote to AGENTS.md/CLAUDE.md: %s" % (len(v), k))
    if not hits:
        print("None found for: %s" % " ".join(words))


def story_id(c, payload=None):
    """Explicit env > branch name > active file > PR title (title last: 'Fix UTF-8' must not become a story)."""
    pat = re.compile(r"(?<![A-Za-z0-9])(?:%s)(?![0-9])" % c["story_id_pattern"])
    for src in (os.environ.get("STORY_GATE_ID", ""), os.environ.get("SG_HEAD_REF") or os.environ.get("GITHUB_HEAD_REF", ""),
                git("rev-parse", "--abbrev-ref", "HEAD").strip()):
        m = pat.search(src or "")
        if m:
            return m.group(0)
    lines = rd(ACTIVE).strip().splitlines()
    if lines and lines[0].strip():
        if len(lines) < 2 or lines[1].strip() == current_branch():
            return lines[0].strip()  # an active story only applies on the branch where it was started
    m = re.match(r"\s*\[?(%s)\]?(?:[:\s]|$)" % c["story_id_pattern"], os.environ.get("PR_TITLE", ""))  # only a leading "VIA-12: ..." / "[VIA-12] ..."
    return m.group(1) if m else None


def load_v(sid, phase):
    p = STORIES / sid / ("%s.json" % phase)
    return load_json(p) or None if p.exists() else None


def cmd_status(sid=None):
    c = cfg()
    sid = sid or story_id(c)
    if not sid:
        print("story-gate: no active story (mode=%s). Start one with: start <ID>" % c["mode"]); return 0
    sd = STORIES / sid
    def show(v):
        if not v:
            return "not run"
        return v.get("overall", "?") + (" (OUT OF DATE - re-score)" if stale(v, c, sd) else "")
    print("story-gate %s: READY=%s DONE=%s mode=%s" % (sid, show(load_v(sid, "ready")), show(load_v(sid, "done")), c["mode"]))
    return 0


# ------------------------------------------------------------------ hooks (one entry point for every client)
def detect_client(payload, hint):
    if "hookEventName" in payload or "toolName" in payload:
        return "grok"
    if "agent_action_name" in payload or "tool_info" in payload:
        return "windsurf"
    if payload.get("hook_event_name") in ("BeforeTool", "AfterAgent"):
        return "gemini"
    if "cursor_version" in payload or "workspace_roots" in payload or "loop_count" in payload:
        return "cursor"
    return hint or "claude"


STOP_LIMIT = 3  # blocked stops in a row before an enforce-mode session is allowed to end (CI still blocks)
SHELL_TOOLS = ("bash", "shell", "run_shell_command", "terminal", "exec_command", "local_shell")


def shell_command(payload):
    """The shell command an agent is about to run, or None for non-shell tools."""
    name = str(payload.get("tool_name") or payload.get("toolName") or "").lower()
    ti = payload.get("tool_input") or payload.get("toolInput") or payload.get("tool_info") or {}
    if isinstance(payload.get("command"), str) and not ti:  # Cursor beforeShellExecution
        return payload["command"]
    if isinstance(ti, dict):
        cmd = ti.get("command") or ti.get("command_line") or ti.get("cmd")
        if isinstance(cmd, list):
            cmd = " ".join(map(str, cmd))
        if isinstance(cmd, str) and "*** Begin Patch" not in cmd and (name in SHELL_TOOLS or "command_line" in ti or not name):
            return cmd
    return None


WRITE_TARGETS = [
    r"(?:^|[^0-9&<>=-])>>?\s*([^\s;|&<>]+)",                       # > file, >> file
    r"\btee\s+(?:-a\s+)?([^\s;|&]+)",                              # tee file
    r"\bsed\s+(?:-[^\s]*i[^\s]*)\s+\S+\s+([^\s;|&]+)",              # sed -i expr file
    r"\b(?:cp|mv)\s+(?:-\S+\s+)*\S+\s+([^\s;|&]+)",                   # cp/mv src dest
    r"\b(?:rm|truncate|shred|touch)\s+(?:-\S+\s+)*([^\s;|&]+)",       # rm/touch file
    r"\bperl\s+-\S*i\S*\s+(?:-\S+\s+)*\S+\s+([^\s;|&]+)",
    r"\bdd\s+.*\bof=([^\s;|&]+)",
    r"\b(?:Set-Content|Add-Content|Out-File)\s+(?:-\w+\s+)*([^\s;|&]+)",
]
GATE_WRITE = re.compile(r"(>|\btee\b|\brm\b|\brmdir\b|\bmv\b|\bcp\b|\bdd\b|\bsed\b|\bperl\b|write|unlink|remove|rename|truncate|"
                        r"-delete|\bcheckout\b|\brestore\b|\bapply\b|\bpatch\b|\bchmod\b|\bln\b|\btouch\b|Set-Content|Add-Content|"
                        r"Out-File|Remove-Item|Move-Item|Copy-Item|\bcd\b|open\()", re.I)
GATE_CLI = re.compile(r"\s*(?:python3?|py)(?:\s+-3)?\s+(?:\./)?\.story-gate[/\\]gate\.py(?:\s+[A-Za-z0-9_.:,=@/+-]+)*\s*")


def unquote(cmd):
    """Quoted single paths ('app.py', ".github/x.yml") keep their text so quoting can't hide a write target;
    other quoted text (messages, sed expressions) is blanked so it isn't mistaken for commands."""
    cmd = re.sub(r"(['\"])([\w./\\~-]*[./\\][\w./\\~-]*)\1", r"\2", cmd)
    cmd = re.sub(r"(>>?\s*)(['\"])([^'\"]*)\2", lambda m: m.group(1) + m.group(3).replace(" ", "?"), cmd)  # > "my file.py"
    return re.sub(r"'[^']*'|\"[^\"]*\"", "''", cmd)


def shell_writes(cmd):
    """Best-effort list of repo files a shell command would write. Opaque scripts are not detectable; CI is the backstop."""
    bare = unquote(cmd)
    out = []
    for rx in WRITE_TARGETS:
        out += [m.strip("'\"") for m in re.findall(rx, bare)]
    keep = []
    for t in out:
        if t and user_protected(t):
            keep.append(t); continue  # the trusted runtime and user-level hook files are always ours to guard
        if not t or t.startswith("/dev/") or t in ("&1", "&2") or repo_rel(t) is None:
            continue  # outside the repository: not our business
        if git("check-ignore", "-q", "--", t) == "" and subprocess.run(["git", "check-ignore", "-q", "--", t], cwd=ROOT).returncode == 0:
            continue  # ignored files (node_modules, build output) are not code
        keep.append(t)
    return keep


ADMIN_COMMANDS = {"install", "uninstall", "enroll", "unenroll", "upgrade", "rollback", "release-sign", "setup-repo", "setup-agent",
                  "judge-calibrate", "lockdown", "filter", "hook-trust"}
# Ways to switch off or route around the checkout filter (sg_guard), or to change what "the default branch" means.
# Nothing an agent needs; refused in any shell command (matched with quotes removed). The hooks also check the filter
# itself on every call (sg_guard.tampered), because text matching can't see every spelling.
FILTER_BYPASS = re.compile(
    r"filter\.storygate|filter\.[^\s.]*\.(?:smudge|clean|required|process)\b|\bgit\s+config\b[^|;&]*\b(?:smudge|clean|required|process)\b"
    r"|info[/\\]attributes|attributesfile|--no-filters|GIT_ATTR|GIT_CONFIG|--config-env|\binclude\.path|\bincludeif\."
    r"|\.git[/\\]config\b|config\.worktree|\bgit\b[^|;&]*\s-c\s*(?:filter|include|core\.attributes|core\.hookspath|remote)"
    r"|update-ref|refs/remotes|\bremote\s+(?:add|set-url|rename|remove|rm|set-head|set-branches)\b|\bremote\.[^.\s]+\.(?:url|fetch|pushurl)",
    re.I)
ADMIN_CALL = re.compile(r"(?:gate|launch)\.py\s+(?:-\S+\s+)*(%s)\b" % "|".join(sorted(ADMIN_COMMANDS)))


def touches_gate(cmd):
    """Any write-capable command that mentions .story-gate (outside the story docs) is refused, except a plain gate.py call."""
    plain = re.sub(r"'[^']*'|\"[^\"]*\"", "Q", cmd)  # quoted free text (--summary "...") is data, not shell
    m = GATE_CLI.fullmatch(plain)
    sub = (plain.split("gate.py", 1)[1].split() or [""])[0] if m else ""
    bare = re.sub(r"[\'\"\\]", "", cmd)  # 'filter.story''gate' and "filter".x read as what the shell will run
    if FILTER_BYPASS.search(bare) or ADMIN_CALL.search(bare):
        return True  # switching protection off, changing the policy source or any admin command: human only, anywhere in the line
    if sub in ADMIN_COMMANDS:
        return True  # installing, enrolling, upgrading or signing is for the human, never an agent
    if ("`" not in cmd and "$(" not in cmd and "${" not in cmd and m and plain.count(".story-gate") == 1):
        return False
    low = cmd.replace("\\", "/").lower()
    guarded = [str(T.runtime_root()).replace("\\", "/").lower(), "enrolled.json", "agent.json", "story_gate_home", ".git/hooks"]
    if any(x in low for x in guarded) and GATE_WRITE.search(cmd):
        return True
    mentions = [m for m in re.finditer(r"\.story-gate(?:[/\\][^\s;|&'\"]*)?", cmd, re.I)]
    sensitive = [m.group(0) for m in mentions if not re.match(r"\.story-gate[/\\]stories[/\\][^/\\]+[/\\](?:[^/\\]+\.md|tests\.json)$", m.group(0), re.I)]
    hooks = [h for h in HOOK_FILES if h in cmd.replace("\\", "/").lower()]
    return bool((sensitive or hooks) and GATE_WRITE.search(cmd))


def edited_paths(payload):
    ti = payload.get("tool_input") or payload.get("toolInput") or payload.get("tool_info") or {}
    paths = []
    if isinstance(ti, dict):
        for k in ("file_path", "path", "filePath", "target_file", "notebook_path"):
            if isinstance(ti.get(k), str):
                paths.append(ti[k])
        patch = ti.get("command") if isinstance(ti.get("command"), str) else ti.get("patch") or ti.get("input")
        if isinstance(patch, str):
            paths += re.findall(r"^\*\*\* (?:Update File|Add File|Delete File|Move to): (.+)$", patch, re.M)
    return paths


def hook_out(client, event, msg, block):
    """Exit 2 + stderr blocks in every client that has hooks. Warnings use each client's visible channel."""
    if block:
        sys.stderr.write(msg + "\n")
        return 2
    if not msg:
        print("{}"); return 0
    if event == "post":  # model-visible context after an edit
        if client in ("claude", "codex"):
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": msg}, "systemMessage": msg}))
        elif client == "gemini":
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "AfterTool", "additionalContext": msg}, "systemMessage": msg}))
        elif client == "cursor":
            print(json.dumps({"additional_context": msg}))
        else:
            print(msg)
        return 0
    if client in ("claude", "codex"):
        out = {"systemMessage": msg}
        if event == "pre":
            out["hookSpecificOutput"] = {"hookEventName": "PreToolUse", "additionalContext": msg}
        print(json.dumps(out))
    elif client == "gemini":
        print(json.dumps({"systemMessage": msg}))
    elif client == "cursor":
        print(json.dumps({"permission": "allow", "user_message": msg, "agent_message": msg} if event == "pre"
                         else {"followup_message": msg}))  # stop: one follow-up; loop_count guard prevents repeats
    elif client == "grok":
        print(json.dumps({"decision": "allow", "reason": msg}))
    else:  # windsurf: show_output prints stdout to the user
        print(msg)
    return 0


def cmd_hook(client, event):
    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {}
    payload = payload if isinstance(payload, dict) else {}
    try:
        client = detect_client(payload, client)
    except Exception:
        client = client or "claude"
    enforce = False
    if RUNTIME:
        early = runtime_precheck(client, event)
        if early is not None:
            return early
    try:
        try:
            c = cfg()
        except ConfigError as e:
            if event == "post":
                return hook_out(client, "post", "", False)
            return hook_out(client, event, "STORY GATE (BLOCKED): %s" % e, True)  # unknown mode: fail closed
        point = {"pre": "pre_edit", "stop": "stop", "post": "checkpoint"}.get(event, event)
        enforce = c["mode"] == "enforce" or point in c.get("enforce_points", [])
        if RUNTIME and event != "post":
            problem = runtime_policy_problem(c)
            if problem:
                return hook_out(client, event, "STORY GATE (%s): %s" % ("BLOCKED" if enforce else "warning only", problem), enforce)
            import sg_pin as PIN
            allowed = PIN.allowed_commands(c)  # from the default branch's policy only
            unknown = [(f, cmd) for f, cmd in T.other_project_hooks(ROOT, HOOK_FILES)
                       if (PIN.unwrap_cmd(cmd) if PIN.unwrap_cmd(cmd) is not None else cmd) not in allowed]
            if unknown:
                return hook_out(client, event, "STORY GATE (%s): this branch has AI-tool hooks the default branch hasn't approved, so "
                                "story-gate can't vouch for them: %s. If they're wanted, a code owner lists the exact commands in "
                                "project_hooks_allowed in .story-gate/config.json on the default branch."
                                % ("BLOCKED" if enforce else "warning only", "; ".join("%s: %s" % (f, cmd[:80]) for f, cmd in unknown[:5])), enforce)
        if event == "post":
            return hook_out(client, "post", post_edit(payload, c), False)  # checkpoints inform; containment happens at the next pre-edit
        msg, bad = gate_check(event, payload, c)
        if bad and event == "pre" and msg.startswith("Story ") and "OFF COURSE" in msg:
            enforce = c["mode"] == "enforce" or "checkpoint" in c.get("enforce_points", [])
    except Exception as e:  # never crash a client; fails closed only when enforcing
        msg, bad = "story-gate internal error: %r" % e, True
    if not bad:
        return hook_out(client, event, "", False)
    prefix = "STORY GATE (%s): " % ("BLOCKED" if enforce else "warning only")
    return hook_out(client, event, prefix + msg, enforce)


def runtime_precheck(client, event):
    """Trusted runtime only. Returns a hook result to stop early, or None to carry on."""
    if not T.repo_identity(os.getcwd())[1]:
        return hook_out(client, event, "", False)  # not in a git repository: nothing to gate
    problems = T.verify_self(HERE)
    if problems:  # the installed runtime itself was changed: never run it
        return hook_out(client, event, "STORY GATE (BLOCKED): the installed story-gate runtime failed its integrity check (%s). "
                        "Re-install it: gate.py install --user" % "; ".join(problems[:3]), event != "post")
    if not enrolled():
        if event == "stop" and GATE.is_dir():
            return hook_out(client, event, "STORY GATE (warning only): this repository has story-gate but this computer hasn't "
                            "enrolled it, so nothing is checked here. A human runs: gate.py enroll", False)
        return hook_out(client, event, "", False)
    return None


def runtime_policy_problem(c):
    """Policy-level problems the trusted runtime reports for an enrolled repository."""
    need = c.get("min_runtime_version")
    if need and T.version_tuple(VERSION) < T.version_tuple(need):
        return "this repository needs story-gate %s or newer; this computer runs %s. Upgrade: gate.py upgrade" % (need, VERSION)
    import sg_guard as SG
    links = SG.symlinked_hook_paths(str(ROOT))
    if links:
        return ("this branch stores AI-tool settings as symlinks (%s). Git writes symlinks without the checkout filter, so a "
                "branch could point your AI tool at hooks nobody approved. Replace them with regular files." % ", ".join(links[:5]))
    if SG.tampered(str(ROOT)):
        return ("the checkout filter that keeps a branch's AI-tool hooks off this computer was switched off outside story-gate. "
                "A human turns it back on with: gate.py filter on  (or off on purpose, logged: gate.py filter off)")
    found = T.project_hook_findings(ROOT, HOOK_FILES)
    if found:
        return ("project hook files run story-gate code from the branch (%s), which a branch could swap. Remove those entries "
                "(gate.py install --user does it) - the hooks in your user settings already cover this repository" % ", ".join(found))
    return None


def post_edit(payload, c):
    """Count code edits; every N run a Jev checkpoint and return a model-visible line (or '')."""
    paths = edited_paths(payload)
    if paths and all(exempt(p, c) for p in paths):
        return ""
    sid = story_id(c, payload)
    if not sid or not (STORIES / sid).exists():
        return ""
    cnt_p = GATE / ".edits"
    cnt = load_json(cnt_p)
    n = int(cnt.get(sid, 0)) + 1
    every = int((c.get("checkpoint") or {}).get("every_edits", 10) or 0)
    cnt[sid] = n
    wj(cnt_p, cnt)
    if not every or n % every:
        return ""
    res = cmd_checkpoint(sid, quiet=True)
    if res.get("status") == "UNKNOWN":
        return ""
    line = checkpoint_line(res).replace("story-gate ", "STORY GATE ", 1)
    if res["status"] != "ON_TRACK":
        line += ". Pause and correct course before continuing (see .story-gate/PROTOCOL.md > Checkpoints)."
    return line


def gate_check(event, payload, c):
    """Returns (message, problem?)."""
    if event == "pre":
        cmd = shell_command(payload)
        if cmd is not None:
            targets = shell_writes(cmd)
            bad = [t for t in targets if protected(t, c)]
            if bad or touches_gate(cmd):
                return "Agents must not change story-gate files from the shell (%s). Verdicts, config and records are written only by gate.py or a human." % (", ".join(bad) or "gate files"), True
            paths = [t for t in targets if not exempt(t, c)]
            if not paths:
                return "", False
        else:
            paths = edited_paths(payload)
            if paths and all(exempt(p, c) for p in paths):
                return "", False
            bad = [p for p in paths if protected(p, c)]
            if bad:
                return "Agents must not edit gate files (%s). Use gate.py commands; verdicts, config and records are written only by gate.py or a human." % ", ".join(bad), True
        sid = story_id(c, payload)
        if not sid:
            return "No active story. Before editing code run `python .story-gate/gate.py start <STORY-ID>` and the READY gate (.story-gate/PROTOCOL.md).", True
        v = load_v(sid, "ready")
        if not passed(v, c, STORIES / sid):
            return "Story %s READY gate is %s (or out of date). Finish the READY steps in .story-gate/PROTOCOL.md before editing code." % (sid, (v or {}).get("overall", "not run")), True
        msg = contained(sid, c)
        return (msg, True) if msg else ("", False)
    # stop
    retry = bool(payload.get("stop_hook_active") or int(payload.get("loop_count") or 0) >= 1)
    if retry and c["mode"] == "warn" and "stop" not in c.get("enforce_points", []):
        return "", False  # warn mode: nag once per turn
    # Enforce keeps blocking on retries, but not forever: after STOP_LIMIT blocked stops in a row the session may end.
    # Nothing is lost - the PR check in CI still blocks the merge.
    stops = 0
    try:
        stops = int(rd(GATE / ".stops").strip() or 0) + 1 if retry else 0
    except ValueError:
        stops = 0
    if stops >= STOP_LIMIT:
        (GATE / ".stops").write_text("0", encoding="utf-8")
        return "", False
    if GATE.is_dir():
        (GATE / ".stops").write_text(str(stops), encoding="utf-8")
    sid = story_id(c, payload)
    _, files, _ = diff_against(c["base_branch"])
    code = [f for f in files if not exempt(f, c)]
    if not code:
        return "", False
    if not sid:
        return "Code changed (%d files) with no active story. Link the work to a story and run the gate." % len(code), True
    d = load_v(sid, "done")
    if not passed(d, c, STORIES / sid):
        return "Story %s has code changes but the DONE gate is %s. Run the DONE steps (handoff, tests, learnings) in .story-gate/PROTOCOL.md." % (sid, (d or {}).get("overall", "not run")), True
    return "", False


# ------------------------------------------------------------------ CI
GATE_FILES = HOOK_FILES + (".story-gate/gate.py", ".story-gate/sg_judges.py", ".story-gate/sg_github.py", ".story-gate/sg_trust.py",
              ".story-gate/sg_guard.py", ".story-gate/sg_pin.py", ".story-gate/sg_dashboard.py", ".story-gate/config.json", ".story-gate/release.json", ".story-gate/release.json.sig",
              ".story-gate/judge-calibration.json",
              ".github/workflows/story-gate.yml", ".github/workflows/story-gate-audit.yml", ".github/workflows/story-gate-dashboard.yml",
              ".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")


def summary(lines):
    p = os.environ.get("GITHUB_STEP_SUMMARY")
    if p:
        with open(p, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


def cmd_ci_tests(out_dir):
    """Job 1 (no secrets): run the base branch's pinned test command on the PR code; save exit code + JUnit as data."""
    c = cfg()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    cmd = c.get("test_command")
    rec = {"command": cmd, "at": now()}
    if not cmd:
        rec.update({"exit_code": None, "error": "no test_command configured"})
    else:
        t0 = time.time()
        try:
            r = subprocess.run(cmd, shell=True, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3000)
            rec.update({"exit_code": r.returncode, "output_tail": (r.stdout + r.stderr)[-4000:]})
        except Exception as e:
            rec.update({"exit_code": -1, "output_tail": repr(e)})
        rec["seconds"] = round(time.time() - t0, 1)
        jp = c.get("junit_path")
        if jp and (ROOT / jp).is_file():
            (out / "junit.xml").write_bytes((ROOT / jp).read_bytes()[:5_000_000])
    wj(out / "results.json", rec)
    print("story-gate tests: %s" % ("exit %s" % rec.get("exit_code") if cmd else "no test_command configured"))
    return 0


CHANGE_LABEL = "story-gate-change"


def cmd_ci(tests_dir=None):
    """Job 2 (has the judge key, runs no PR code): re-score READY and DONE with CI's own test results,
    verify human acceptance through GitHub, report clearly whether merges are actually blocked."""
    import sg_github as G
    problems, notes, code, enforce = [], [], [], True
    sid = None
    try:
        c = cfg()
        enforce = c["mode"] == "enforce" or "ci" in c.get("enforce_points", [])
        bref = os.environ.get("SG_BASE_REF") or os.environ.get("GITHUB_BASE_REF")  # pull_request_review has no GITHUB_BASE_REF
        base = ("origin/" + bref) if bref else c["base_branch"]
        c["base_branch"] = base
        _, files, _ = diff_against(base)
        code = [f for f in files if not exempt(f, c) and not is_record(f)]
        sid = story_id(c)
        ctx = G.pr_context()
        token = os.environ.get("GITHUB_TOKEN")
        touched = sorted(f for f in files if f in GATE_FILES or f.startswith(".github/workflows/") or f.startswith(".grok/hooks/")
                         or (f.startswith(".story-gate/") and (f.endswith(".py") or f.startswith(".story-gate/vendor/"))))
        if touched:
            who = G.label_added_by(ctx, token, CHANGE_LABEL) if ctx and token else None
            owners_txt = (git("show", "%s:.github/CODEOWNERS" % base) or git("show", "%s:CODEOWNERS" % base) or "")
            allowed = {u.lower() for u in G.codeowners(owners_txt)[0]} | {a.lower().lstrip("@") for a in (c.get("approvers") or [])}
            if who and who.lower() in allowed:
                notes.append("This PR changes story-gate code, hook or workflow files (%s); %s confirmed with the '%s' label."
                             % (", ".join(touched), who, CHANGE_LABEL))
            else:
                problems.append("This PR changes story-gate code, hook or workflow files (%s). A code owner must review them and add "
                                "the label '%s' to confirm%s." % (", ".join(touched), CHANGE_LABEL,
                                                                  " (it was added by %s, who is not a code owner)" % who if who else ""))
        if code and not sid:
            problems.append("code changed but no story id in the branch name or a leading '[ID]' in the PR title (pattern %s)" % c["story_id_pattern"])
        elif code:
            sd = STORIES / sid
            results, tr = None, None
            if tests_dir and not (Path(tests_dir) / "results.json").is_file():
                # Never fall back to the agent's own recorded results.
                tr = {"command": c.get("test_command"), "exit_code": None, "error": "the CI tests job left no results"}
                if sd.exists():
                    wj(sd / "test_results.json", dict(tr, fingerprint="", source="ci"))
                problems.append("CI test results are missing (the tests job failed before recording, or its artifact did not upload)")
            elif tests_dir:
                tr = load_json(Path(tests_dir) / "results.json")
                junit = Path(tests_dir) / "junit.xml"
                try:
                    results = G.junit(junit) if junit.is_file() else None
                except Exception as e:
                    problems.append("JUnit report rejected: %s" % e)
                if sd.exists():  # CI's own run replaces anything the agent recorded
                    wj(sd / "test_results.json", dict(tr, fingerprint=work_fingerprint(), head=git("rev-parse", "HEAD").strip(), source="ci"))
                if tr.get("exit_code") != 0:
                    problems.append("tests failed or did not run in CI (%s)" % (tr.get("error") or "exit %s" % tr.get("exit_code")))
            accepted = {"accepted": False, "why": "not running on a pull request"}
            if ctx and token:
                # Owners come only from the base branch: a PR can never name its own approvers.
                base_owners = (git("show", "%s:.github/CODEOWNERS" % base) or git("show", "%s:CODEOWNERS" % base)
                               or git("show", "%s:docs/CODEOWNERS" % base))
                accepted = G.acceptance(ctx, token, base_owners, c.get("approvers") or [])  # owners from the base branch
            if not J.available(c):
                problems.append("judge unavailable in CI (%s). Add the judge key as a repository secret%s." % (
                    J.settings(c).get("key_env"), "; PRs from forks never get secrets, so a maintainer must re-run the check" if (ctx or {}).get("fork") else ""))
            elif not sd.exists():
                problems.append("no story folder .story-gate/stories/%s in this PR" % sid)
            else:
                for ph in ("ready", "done"):
                    cmd_score(sid, ph, base, ci_trust=accepted["accepted"], results=results if ph == "done" else None, quiet=True)
                    v = load_v(sid, ph)
                    if not passed(v, c):
                        bad = ["%s (%s)" % (k, x.get("why") or x.get("score")) for k, x in (v or {}).get("checks", {}).items() if x["status"] not in ("PASS", "WAIVED")]
                        problems.append("%s %s gate is %s: %s" % (sid, ph.upper(), (v or {}).get("overall", "missing"), "; ".join(bad)[:900]))
            if ctx and token and c.get("require_independent_review"):
                try:
                    who = G.reviewed_by(ctx, token, c.get("reviewers") or [])
                    open_threads = G.unresolved_threads(ctx, token, c.get("reviewers") or [])
                    if not who and not accepted["accepted"]:
                        problems.append("no independent review yet (expected one of: %s, or a code owner)" % ", ".join(c.get("reviewers") or []))
                    if open_threads:
                        problems.append("%d unresolved review thread(s) from the independent reviewers" % open_threads)
                except Exception as e:
                    problems.append("could not read reviews: %s" % e)
            if not accepted["accepted"]:
                problems.append("human acceptance: " + accepted["why"])
            enforced, missing = (G.protection(ctx["repo"], ctx["base"], token) if ctx and token else (False, ["not running on a pull request"]))
            if not enforced:
                notes.insert(0, "ADVISORY - NOT ENFORCED: GitHub will not block this merge (%s). Run `gate.py setup-repo` or see the README." % "; ".join(missing))
    except ConfigError as e:
        problems.append(str(e)); enforce = True
    except Exception as e:
        problems.append("story-gate could not run: %s" % e)
    level = "error" if enforce else "warning"
    for p in problems:
        print("::%s title=story-gate::%s" % (level, p.replace("\n", " ")))
    for n in notes:
        print("::notice title=story-gate::%s" % n)
    lines = ["## story-gate %s" % ("- %s" % sid if sid else ""), ""]
    lines += ["> **%s**" % n for n in notes] + [""]
    if problems:
        lines += ["- [ ] %s" % p for p in problems]
    elif code:
        lines += ["- [x] READY and DONE gates passed, CI tests passed, and a code owner approved this commit."]
    else:
        lines += ["- [x] No code files changed, so the story gates were skipped. Normal code owner review still applies."]
    if sid and (STORIES / sid / "trace.md").exists():
        lines += ["", rd(STORIES / sid / "trace.md")]
    if sid:
        recs = jsonl(STORIES / sid / "decisions.jsonl")
        if recs:
            lines += ["", "### Waivers and drift decisions in this story (count only after a code owner approves this commit)"]
            lines += ["- %s %s: %s by %s - %s" % (r.get("phase"), r.get("kind"), r.get("check") or r.get("drift"), r.get("by"), r.get("reason") or r.get("note", "")) for r in recs]
    summary(lines)
    print("story-gate CI: %s (%s mode, %d code files)" % ("OK" if not problems else "%d problem(s)" % len(problems), "enforce" if enforce else "warn", len(code)))
    return 1 if (problems and enforce) else 0


def cmd_audit():
    """After a merge: record who merged and whether a code owner approved the merged commit; flag anything else."""
    import sg_github as G
    ctx, token = G.pr_context(), os.environ.get("GITHUB_TOKEN")
    if not ctx or not token:
        print("story-gate audit: not a pull_request event"); return 0
    ev = json.load(open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8"))["pull_request"]
    if not ev.get("merged"):
        print("story-gate audit: PR closed without merge"); return 0
    b = os.environ.get("BASE") or "HEAD"  # the base commit before the merge, so the merged PR can't pick its own owners
    owners = git("show", "%s:.github/CODEOWNERS" % b) or git("show", "%s:CODEOWNERS" % b)
    acc = G.acceptance(ctx, token, owners, (cfg().get("approvers") or []))
    merged_by = ev.get("merged_by") or {}
    issues = []
    if not acc["accepted"]:
        issues.append("merged without a code owner approving the final commit (%s)" % acc["why"])
    if merged_by.get("type") == "Bot":
        issues.append("merged by a bot account (%s)" % merged_by.get("login"))
    row = {"at": now(), "pr": ctx["number"], "sha": ctx["head_sha"], "merged_by": merged_by.get("login"), "approver": acc.get("approver"), "issues": issues}
    print(json.dumps(row))
    if issues:
        for i in issues:
            print("::warning title=story-gate audit::%s" % i)
        st, r, _ = G.call("POST", "/repos/%s/issues" % ctx["repo"], token, {"title": "story-gate audit: PR #%s merged without human acceptance" % ctx["number"],
               "body": "\n".join("- " + i for i in issues) + "\n\nCommit %s. Review it, then revert or record why it was acceptable." % ctx["head_sha"]})
        if st not in (200, 201):
            print("::error title=story-gate audit::could not open the audit issue (HTTP %s %s). The findings are in this log." % (st, (r or {}).get("message", "") if isinstance(r, dict) else ""))
            return 1
    return 0


# ------------------------------------------------------------------ sinks
def cmd_publish():
    c = cfg()
    lines = [l for l in rd(OUTBOX).splitlines() if l.strip()]
    sent_p = GATE / ".outbox.sent"
    sent = load_json(sent_p)  # per-sink cursor: a failing sink never causes duplicates in a working one
    rc, delivered = 0, 0
    for i, s in enumerate(c.get("sinks", [])):
        t = s.get("type")
        key = "%d:%s" % (i, json.dumps(s, sort_keys=True))  # full identity: duplicate-looking sinks never share a cursor
        todo = lines[int(sent.get(key, 0)):]
        if t == "repo" or not todo:
            continue
        try:
            if t == "command" and s.get("command"):
                r = subprocess.run(s["command"], shell=True, cwd=ROOT, input="\n".join(todo) + "\n", text=True,
                                   encoding="utf-8", errors="replace", capture_output=True, timeout=120)
                ok = r.returncode == 0
            elif t == "webhook" and os.environ.get(s.get("url_env", "")):
                for l in todo:
                    urllib.request.urlopen(urllib.request.Request(os.environ[s["url_env"]], data=l.encode(), headers={"Content-Type": "application/json"}), timeout=30)
                ok = True
            elif t == "control-hub" and os.environ.get(s.get("url_env", "")) and os.environ.get(s.get("key_env", "")):
                ok = publish_control_hub(s, todo)
            else:
                print("story-gate: sink %s not configured here, skipped" % t); continue
        except Exception as e:
            print("story-gate: sink %s FAILED %r" % (t, e)); ok = False
        print("story-gate: sink %s %s (%d events)" % (t, "OK" if ok else "FAILED", len(todo)))
        if ok:
            sent[key] = len(lines); delivered += 1
        else:
            rc = 1
    wj(sent_p, sent)
    if not delivered and rc == 0:
        print("story-gate: nothing new delivered (no external sink configured/reachable, or all up to date)")
    return rc


def publish_control_hub(s, todo):
    url, key = os.environ[s["url_env"]].rstrip("/"), os.environ[s["key_env"]]
    h = {"apikey": key, "Authorization": "Bearer " + key, "Content-Type": "application/json"}
    pid = None
    if s.get("project_slug"):
        q = urllib.request.Request(url + "/rest/v1/projects?select=id&slug=eq." + urllib.request.quote(s["project_slug"]), headers=h)
        rows = json.loads(urllib.request.urlopen(q, timeout=30).read().decode())
        pid = rows[0]["id"] if rows else None
    body = [{"project_id": pid, "event_type": e["event_type"], "actor": "story-gate", "payload": e} for e in map(json.loads, todo)]
    r = urllib.request.urlopen(urllib.request.Request(url + "/rest/v1/events", data=json.dumps(body).encode(), headers=h, method="POST"), timeout=30)
    return 200 <= r.status < 300


# ------------------------------------------------------------------ install / doctor
BLOCK_START, BLOCK_END = "<!-- story-gate:start -->", "<!-- story-gate:end -->"
INSTRUCTION_BLOCK = BLOCK_START + """
## Story gate (required for any code change)
Every code change belongs to a story and passes the story gate. Full steps: `.story-gate/PROTOCOL.md`.
1. Before editing code: `{py} .story-gate/gate.py start <STORY-ID>`, fill the story folder, then `{py} .story-gate/gate.py score <STORY-ID> ready`.
2. Drift between story and PRD/TRD is never resolved silently: escalate, then record `gate.py decide`.
3. Before saying you are done: run tests via `gate.py record-tests`, write `handoff.md`, record learnings with `gate.py learn`, then `gate.py score <STORY-ID> done`.
4. Read past learnings first: `{py} .story-gate/gate.py learnings <keywords>`.
Mode is in `.story-gate/config.json` (warn = report only, enforce = block).
""" + BLOCK_END


def put_block(path, py):
    p = ROOT / path
    text = rd(p)
    block = INSTRUCTION_BLOCK.replace("{py}", py)
    if BLOCK_START in text:
        text = re.sub(re.escape(BLOCK_START) + ".*?" + re.escape(BLOCK_END), lambda m: block, text, flags=re.S)
    else:
        text = (text.rstrip() + "\n\n" if text.strip() else "") + block + "\n"
    p.write_text(text, encoding="utf-8")


MANAGED = "# managed by story-gate %s - re-run `gate.py install` to update; local edits are overwritten" % VERSION
TRUSTED_COPY = """          if [ -L .story-gate ]; then echo "::error title=story-gate::.story-gate is a symlink in this PR; refusing to run"; exit 1; fi
          mkdir -p "$RUNNER_TEMP/sg"
          if ! git cat-file -e "$BASE:.story-gate/gate.py" 2>/dev/null; then
            echo "::warning title=story-gate::story-gate is not on the base branch yet, so this PR is checked by human review only. It never runs code from the PR itself."
            exit 0
          fi
          for f in gate.py sg_judges.py sg_github.py config.json judge-calibration.json; do
            rm -f "$RUNNER_TEMP/sg/$f"
            if git cat-file -e "$BASE:.story-gate/$f" 2>/dev/null; then git show "$BASE:.story-gate/$f" > "$RUNNER_TEMP/sg/$f"; fi
          done
          # Settings and judge calibration are read from the base branch's copies; the PR's own files stay as evidence.
          export STORY_GATE_TRUSTED_DIR="$RUNNER_TEMP/sg\""""
CI_YML = MANAGED + """
name: story-gate
on:
  pull_request:
    types: [opened, edited, synchronize, reopened, ready_for_review, labeled, unlabeled]
  pull_request_review:
    types: [submitted, edited, dismissed]
permissions:
  contents: read
concurrency:
  group: story-gate-${{ github.event.pull_request.number }}
  cancel-in-progress: true
jobs:
  tests:
    # Runs the PR's code with the BASE branch's pinned test command. No secrets here.
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
          ref: ${{ github.event.pull_request.head.sha }}
          persist-credentials: false
      - name: run pinned tests
        env:
          BASE: origin/${{ github.event.pull_request.base.ref }}
          SG_BASE_REF: ${{ github.event.pull_request.base.ref }}
          SG_HEAD_REF: ${{ github.event.pull_request.head.ref }}
          STORY_GATE_ROOT: ${{ github.workspace }}
        run: |
""" + TRUSTED_COPY + """
          python3 "$RUNNER_TEMP/sg/gate.py" ci-tests "$RUNNER_TEMP/sg-tests"
      - uses: actions/upload-artifact@v4
        with:
          name: sg-tests
          path: ${{ runner.temp }}/sg-tests
          retention-days: 7
  story-gate:
    # Has the judge key; runs story-gate from the BASE branch and never executes PR code.
    needs: tests
    if: always()
    runs-on: ubuntu-latest
    permissions:
      contents: read
      pull-requests: read
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
          ref: ${{ github.event.pull_request.head.sha }}
          persist-credentials: false
      - uses: actions/download-artifact@v4
        with:
          name: sg-tests
          path: ${{ runner.temp }}/sg-tests
        continue-on-error: true
      - name: story gate
        env:
          BASE: origin/${{ github.event.pull_request.base.ref }}
          SG_BASE_REF: ${{ github.event.pull_request.base.ref }}
          SG_HEAD_REF: ${{ github.event.pull_request.head.ref }}
          STORY_GATE_ROOT: ${{ github.workspace }}
          PR_TITLE: ${{ github.event.pull_request.title }}
          GITHUB_TOKEN: ${{ github.token }}
          OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}
          TYPESAFE_API_KEY: ${{ secrets.TYPESAFE_API_KEY }}
          JUDGE_API_KEY: ${{ secrets.JUDGE_API_KEY }}
        run: |
""" + TRUSTED_COPY + """
          python3 "$RUNNER_TEMP/sg/gate.py" ci --tests "$RUNNER_TEMP/sg-tests"
"""
AUDIT_YML = MANAGED + """
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
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
          persist-credentials: false
      - name: who merged, and was it accepted?
        env:
          BASE: ${{ github.event.pull_request.base.sha }}
          STORY_GATE_ROOT: ${{ github.workspace }}
          GITHUB_TOKEN: ${{ github.token }}
        run: |
""" + TRUSTED_COPY + """
          python3 "$RUNNER_TEMP/sg/gate.py" audit
"""


DASH_YML = MANAGED + """
name: story-gate-dashboard
# Runs only the default branch's copy of this file and of story-gate (schedule, workflow_run and default-branch
# pushes always use the default branch). Records on other branches are read as data, never executed.
on:
  schedule:
    - cron: "11,41 * * * *"
  workflow_run:
    workflows: [story-gate]
    types: [completed]
  push:
    branches: [{base}]
  workflow_dispatch:
permissions:
  contents: read
  issues: write
concurrency:
  group: story-gate-dashboard
  cancel-in-progress: false
jobs:
  dashboard:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{{{ github.event.repository.default_branch || github.ref_name }}}}
          fetch-depth: 0
          persist-credentials: false
      - name: build
        env:
          SG_DEFAULT_BRANCH: ${{{{ github.event.repository.default_branch || github.ref_name }}}}
          STORY_GATE_ROOT: ${{{{ github.workspace }}}}
        run: python3 .story-gate/gate.py dashboard --out "$RUNNER_TEMP/sg-dashboard"
      - id: report
        uses: actions/upload-artifact@v4
        with:
          name: story-gate-dashboard
          path: ${{{{ runner.temp }}}}/sg-dashboard/dashboard.html
          retention-days: 30
      - name: publish
        env:
          STORY_GATE_ROOT: ${{{{ github.workspace }}}}
          GITHUB_TOKEN: ${{{{ github.token }}}}
        run: python3 .story-gate/gate.py dashboard --from-json "$RUNNER_TEMP/sg-dashboard/dashboard.json" --publish --artifact-url "${{{{ steps.report.outputs.artifact-url }}}}" --out "$RUNNER_TEMP/sg-dashboard"
      - name: say so if the refresh failed (the last good snapshot stays)
        if: failure()
        env:
          STORY_GATE_ROOT: ${{{{ github.workspace }}}}
          GITHUB_TOKEN: ${{{{ github.token }}}}
        run: python3 .story-gate/gate.py dashboard --report-failure
"""


def write_managed(rel, body):
    """Write a story-gate workflow; refuse to overwrite a hand-made file with the same name."""
    p = ROOT / rel
    old = rd(p)
    if old and not old.startswith("# managed by story-gate"):
        return "%s exists and is not managed by story-gate - left untouched (rename it or delete it, then re-run install)" % rel
    if old == body:
        return "%s up to date" % rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return "%s %s" % (rel, "updated" if old else "created")


def cmd_install(py):
    """Repository setup (commit the result). Hooks are NOT written into the repository any more: each person runs
    `gate.py install --user` once per computer, so a branch can never change what the hooks run."""
    GATE.mkdir(exist_ok=True)
    STORIES.mkdir(exist_ok=True)
    if not (GATE / "config.json").exists():
        wj(GATE / "config.json", DEFAULT_CONFIG)
    removed = T.remove_project_hooks(ROOT, HOOK_FILES)
    for f in ("AGENTS.md", "CLAUDE.md", "GEMINI.md"):
        put_block(f, py)
    skill = rd(GATE / "SKILL.md")
    if skill:  # .agents/skills: Codex, Cursor, Gemini, Devin, Muse.  .claude/skills: Claude Code, Cursor, Grok, Muse
        for d in (".agents/skills/story-gate", ".claude/skills/story-gate"):
            (ROOT / d).mkdir(parents=True, exist_ok=True)
            (ROOT / d / "SKILL.md").write_text(skill, encoding="utf-8")
    wf_notes = [write_managed(".github/workflows/story-gate.yml", CI_YML), write_managed(".github/workflows/story-gate-audit.yml", AUDIT_YML),
                write_managed(".github/workflows/story-gate-dashboard.yml", dash_yml())]
    gi = rd(ROOT / ".gitignore")
    add = [x for x in (".story-gate/.active", ".story-gate/outbox.jsonl", ".story-gate/.outbox.sent", ".story-gate/.edits", ".story-gate/.stops", ".story-gate/__pycache__/") if x not in gi]
    if add:
        (ROOT / ".gitignore").write_text(gi.rstrip() + ("\n" if gi.strip() else "") + "\n".join(add) + "\n", encoding="utf-8")
    print("story-gate installed in this repository (mode=%s). Instructions: AGENTS.md, CLAUDE.md, GEMINI.md. CI: .github/workflows/story-gate.yml" % cfg()["mode"])
    for n in wf_notes:
        print("  " + n)
    if removed:
        print("  Removed story-gate hooks from project hook files (they ran branch code):")
        print("\n".join(removed))
    print("Skill: .agents/skills/story-gate + .claude/skills/story-gate.")
    print("Next: 1) commit and merge this  2) gate.py setup-repo (code owners + branch rules)  3) add your judge key as a repo secret\n"
          "      4) on each computer that runs AI tools: gate.py install --user  (turns on the hooks, safely)")


def dash_yml():
    base = re.sub(r"^origin/", "", cfg().get("base_branch", "main"))
    return DASH_YML.replace("{base}", json.dumps(base)).replace("{{", "{").replace("}}", "}")


def cmd_user(cmd, kv, rest):
    """install --user / uninstall --user / enroll / unenroll / upgrade / rollback / release-sign."""
    dry = "--dry-run" in rest
    if cmd == "install":
        py = kv.get("python") or sys.executable
        clients = [x.strip() for x in kv.get("clients", ",".join(T.USER_CLIENTS)).split(",") if x.strip()]
        bad = [x for x in clients if x not in T.USER_CLIENTS]
        if bad:
            sys.exit("user-level hooks are supported for %s; %s run in reduced protection (CI still checks every PR)"
                     % (", ".join(T.USER_CLIENTS), ", ".join(bad)))
        unsigned = "--unsigned" in rest
        print("story-gate %s - trusted runtime install%s" % (VERSION, " (dry run: nothing is written)" if dry else ""))
        print("Release key fingerprint: %s  (it must match the one on the story-gate release page)" % T.key_fingerprint())
        try:
            if unsigned:
                print("WARNING: --unsigned installs a DEVELOPMENT copy that no one signed. Only do this for code you trust.")
            else:
                T.verify_release(HERE)
                print("Signature: valid")
            if dry:
                dest = T.runtime_root() / VERSION
            else:
                dest = T.install_runtime(HERE, VERSION, unsigned=unsigned)
        except T.TrustError as e:
            print("NOT installed: %s" % e); return 1
        out = T.register_user_hooks(os.path.abspath(py).replace("\\", "/"), str(T.launcher_path()).replace("\\", "/"), clients, dry)
        print("Runtime: %s" % dest)
        print("User-level hooks (%s):" % ", ".join(clients))
        print("\n".join(out) or "  nothing to change")
        top, ident = T.repo_identity(os.getcwd())
        if top and (Path(top) / ".story-gate").is_dir():
            if not dry:
                e = T.enroll(top, kv.get("policy-ref"))
                print("Enrolled this repository. Policy comes from %s; branches can only make it stricter." % e["policy_ref"])
            rm = T.remove_project_hooks(top, HOOK_FILES, dry)
            if rm:
                print("Project hook files that ran branch code (story-gate entries removed):")
                print("\n".join(rm))
            turn_filter_on(top, py, dry)
        print("Recommended, optional and OFF unless you say yes: gate.py lockdown  (explains Claude Code's hard switch for "
              "repository hooks; nothing changes without your permission)")
        print("Notes:\n  - Codex asks you to trust new hooks once: run /hooks in Codex and trust the story-gate entries."
              "\n  - Grok: reduced protection (its hook merging isn't documented); run gate.py status yourself. See docs/client-security.md."
              "\n  - Cowork, Cursor Cloud and Codex cloud have no hooks: run gate.py status yourself; CI is the backstop."
              "\n  - In each other repository with story-gate, run: gate.py enroll")
        return 0
    if cmd == "uninstall":
        import sg_guard as SG
        st = SG.lockdown_status()
        if st["ours"]:
            print("NOT uninstalled: lockdown is still on, and Claude Code's managed hooks point at the runtime.\n"
                  "Turn it off first (needs admin), then run this again:\n  %s" % lockdown_off_command(st))
            return 1
        print("Checkout filters (one per covered repository):")
        out = []
        for key, rec in sorted(T.read_json(T.manifest_path()).get("repos", {}).items()):
            top = (rec or {}).get("toplevel") or key  # the working folder recorded at enable time (the key is the .git folder)
            if Path(top).is_dir() and T.git_in(top, "rev-parse", "--is-inside-work-tree") == "true":
                out += ["  %s" % top] + SG.disable_filter(top, dry)
            elif Path(key).is_dir():  # working folder moved or gone: settings can still be restored in the .git folder
                out += ["  %s (working folder not found; hook files weren't re-checked)" % key] + SG.disable_filter(key, dry)
            else:
                out.append("  %s: not found. If it moved, run there: git config --local --remove-section filter.%s" % (top, SG.FILTER))
                if not dry:
                    T.forget("repos", key)
        print("\n".join(out) or "  none")
        print("User-level hooks:")
        out = T.unregister_user_hooks(dry)
        print("\n".join(out) or "  no story-gate hooks in your user settings")
        if not dry and T.runtime_root().exists():
            shutil_rmtree(T.runtime_root())
            print("Removed the runtime: %s (enrollment and agent key are kept)" % T.runtime_root())
        if not dry and not any(T.read_json(T.manifest_path()).get(k) for k in ("files", "repos")):
            try:
                T.manifest_path().unlink()
            except OSError:
                pass
        return 0
    if cmd == "enroll":
        if dry:
            top = T.repo_identity(os.getcwd())[0]
            print("Would enroll %s." % top)
            return 0 if not top else (0 if turn_filter_on(top, kv.get("python") or sys.executable, True) or True else 1)
        try:
            e = T.enroll(os.getcwd(), kv.get("policy-ref"))
        except T.TrustError as ex:
            print("NOT enrolled: %s" % ex); return 1
        top = T.repo_identity(os.getcwd())[0]
        print("Enrolled %s. Policy comes from %s." % (top, e["policy_ref"]))
        turn_filter_on(top, kv.get("python") or sys.executable, dry)
        return 0
    if cmd == "unenroll":
        import sg_guard as SG
        top = T.repo_identity(os.getcwd())[0]
        if top and (SG.filter_active(top) or SG.tampered(top) or dry):
            print("Checkout filter off:\n" + "\n".join(SG.disable_filter(top, dry)))
        if dry:
            print("Would unenroll %s." % top); return 0
        print("Unenrolled." if T.unenroll(os.getcwd()) else "This repository was not enrolled.")
        return 0
    if cmd in ("upgrade", "rollback"):
        if not RUNTIME:
            print("Run this with the installed runtime (the one your hooks use), so the new version is checked with the key you "
                  "already trust: python %s %s" % (Path(read_active_dir()) / "gate.py" if read_active_dir() else "<runtime>/gate.py", cmd))
            return 1
        if cmd == "rollback":
            prev = T.read_json(T.active_path()).get("previous")
            if not prev or not Path(prev).is_dir():
                print("No previous runtime to roll back to."); return 1
            if T.verify_self(prev) and any("changed" in x or "unexpected" in x for x in T.verify_self(prev)):
                print("The previous runtime failed its integrity check; not rolling back."); return 1
            m = T.read_json(Path(prev) / "manifest.json")
            T.launcher_path().write_text(T.LAUNCHER, encoding="utf-8")
            T.write_json_atomic(T.active_path(), {"version": m.get("version"), "dir": prev, "previous": str(HERE),
                                                  "signed": m.get("signed"), "manifest_sha256": T.sha256(Path(prev) / "manifest.json"),
                                                  "launcher_sha256": T.sha256(T.launcher_path())})
            print("Rolled back to %s." % m.get("version")); return 0
        import tempfile as _tf
        with _tf.TemporaryDirectory() as td:
            if kv.get("url"):
                try:
                    src = T.fetch_release(kv["url"], td)
                except (T.TrustError, OSError) as ex:
                    print("NOT upgraded: %s" % ex); return 1
            elif kv.get("from"):
                src = Path(kv["from"])
            else:
                e = T.enrollment(os.getcwd()) or {}
                ref = kv.get("ref") or e.get("policy_ref") or T.default_policy_ref(os.getcwd())
                if not ref:
                    print("Say where the new version is: --from <folder> or --ref <branch or tag>"); return 1
                names = [n for n in git("ls-tree", "-r", "--name-only", ref, ".story-gate").splitlines()]
                for n in names:
                    rel_ = n.split("/", 1)[1]
                    if rel_.startswith("stories/"):
                        continue
                    dst = Path(td) / rel_
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_bytes(subprocess.run(["git", "show", "%s:%s" % (ref, n)], cwd=ROOT, capture_output=True).stdout)
                src = Path(td)
            try:
                man = T.verify_release(src)  # checked with the key in THIS (already trusted) runtime
                dest = T.install_runtime(src, man.get("version") or "unknown")
            except T.TrustError as ex:
                print("NOT upgraded: %s" % ex); return 1
        print("Upgraded to %s. Roll back with: gate.py rollback" % man.get("version")); return 0
    if cmd == "release-sign":
        if not kv.get("key"):
            sys.exit("release-sign --key <private signing key>   (maintainers only)")
        try:
            m = T.sign_release(HERE, kv["key"], VERSION)
        except T.TrustError as ex:
            print("NOT signed: %s" % ex); return 1
        print("Signed %d files for story-gate %s: .story-gate/release.json + release.json.sig" % (len(m["files"]), VERSION)); return 0
    return 2


def turn_filter_on(top, py, dry=False):
    """Checkout filter for one enrolled repository (sg_guard): a branch's AI-tool hook changes never reach disk here."""
    import sg_guard as SG
    if not T.launcher_ok():
        print("Checkout filter: needs the trusted runtime first (gate.py install --user)")
        return False
    ok, lines = SG.enable_filter(top, os.path.abspath(py), T.launcher_path(), dry)
    print(("Checkout filter %s:" % ("would be turned on" if dry else "on")) if ok else "Checkout filter:")
    print("\n".join(lines))
    if ok and not dry:
        print("  Prove it any time: gate.py doctor --prove")
    return ok


def lockdown_off_command(st):
    import sg_guard as SG
    b = G_config_dir() / "lockdown"
    if os.name == "nt":
        return 'PowerShell as Administrator: powershell -ExecutionPolicy Bypass -File "%s"   (or delete %s)' % (b / "uninstall.ps1", st["file"])
    return 'sudo sh "%s"   (or: sudo rm "%s")' % (b / "uninstall.sh", st["file"])


def G_config_dir():
    import sg_github as G
    return G.config_dir()


def consent(kv, question):
    """Explicit permission: an interactive YES, or --consent yes typed by the person on the command line."""
    if str(kv.get("consent", "")).strip().lower() == "yes":
        return True
    if not sys.stdin.isatty():
        return False
    try:
        return input(question + " Type YES to continue: ").strip() == "YES"
    except EOFError:
        return False


def cmd_lockdown(kv, rest):
    """Optional, OFF by default: Claude Code's allowManagedHooksOnly switch, only with explicit permission."""
    import sg_guard as SG
    user_text = rd(T.user_hook_files()["claude"])
    st = SG.lockdown_status(user_text)
    keep = []
    for cmd_ in [k for k in (kv.get("keep-project-hook") or "").split("\n") if k]:
        try:
            keep.append(SG.find_project_hook(SG.approved_for(str(ROOT), ".claude/settings.json")[0], cmd_))
        except T.TrustError as ex:
            print("NOT changed: %s" % ex); return 1
    py = os.path.abspath(kv.get("python") or sys.executable)
    if "--on" not in rest and "--off" not in rest and not kv.get("bundle"):
        print(SG.explain(st))
        print("Protection on this computer against hooks shipped inside branches:")
        for tool, level, nxt in SG.client_matrix(str(ROOT)):
            print("  %-8s %s%s" % (tool, level, ("  (" + nxt + ")") if nxt else ""))
        print("\nTurn on: gate.py lockdown --on    Files for IT: gate.py lockdown --bundle <folder>    Undo: gate.py lockdown --off")
        return 0
    if "--off" in rest:
        if not st["ours"]:
            print("Lockdown is already off (no story-gate file at %s)." % st["file"]); return 0
        try:
            Path(st["file"]).unlink()
            print("Lockdown is OFF. Removed %s." % st["file"])
            T.forget("lockdown", "claude")
            return 0
        except OSError:
            print("Lockdown stays on until you run this one command (it needs admin rights):\n  %s" % lockdown_off_command(st))
            return 1
    if not T.launcher_ok():
        print("Lockdown needs the trusted runtime first: gate.py install --user"); return 1
    if kv.get("bundle"):
        try:
            dest, _ = SG.build_bundle(kv["bundle"], py, T.launcher_path(), user_text, keep)
        except T.TrustError as ex:
            print("NOT built: %s" % ex); return 1
        print("Lockdown files for IT are in %s (nothing on this computer changed):" % dest)
        print(rd(Path(dest) / "SHA256SUMS").rstrip())
        print("Give IT README-IT.md; it says where each file goes on macOS, Linux and Windows.")
        return 0
    print(SG.explain(st))
    if not consent(kv, "Turn on lockdown for Claude Code on this computer?"):
        print("Nothing changed. Lockdown stays OFF. (To say yes from a script you typed yourself: gate.py lockdown --on --consent yes)")
        return 1
    bundle = G_config_dir() / "lockdown"
    try:
        _, body = SG.build_bundle(bundle, py, T.launcher_path(), user_text, keep)
    except T.TrustError as ex:
        print("NOT turned on: %s" % ex); return 1
    T.record("lockdown", "claude", consent_at=now(), file=st["file"], bundle=str(bundle))
    target = Path(st["file"])
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp-%d" % os.getpid())
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, target)
        print("Lockdown is ON for Claude Code: %s\nRestart Claude Code, then check: gate.py doctor --prove" % target)
        return 0
    except OSError:
        cmd = ('PowerShell as Administrator: powershell -ExecutionPolicy Bypass -File "%s"' % (bundle / "install.ps1")) if os.name == "nt" \
            else 'sudo sh "%s"' % (bundle / "install.sh")
        print("You said yes. Writing %s needs admin rights, so run this one command yourself:\n  %s\n"
              "Then restart Claude Code and check: gate.py doctor --prove" % (target, cmd))
        return 0


def cmd_filter(rest):
    """Audited escape hatch: turn the checkout filter off (or back on) for this repository only."""
    import sg_guard as SG
    top = T.repo_identity(os.getcwd())[0]
    if not top:
        print("Not inside a git repository."); return 1
    what = rest[0] if rest else "status"
    if what == "off":
        if not SG.filter_active(top) and not SG.tampered(top):
            print("The checkout filter is already off here."); return 0
        print("\n".join(SG.disable_filter(top)))
        SG.log(top, "checkout filter turned OFF by %s" % (os.environ.get("USER") or os.environ.get("USERNAME") or "?"))
        print("Checkout filter OFF for %s (logged). A branch's hook changes now reach disk here. Turn back on: gate.py filter on" % top)
        return 0
    if what == "on":
        if not (T.enrollment(top) or {}).get("enrolled"):
            print("This repository isn't covered by story-gate. Run gate.py enroll first."); return 1
        return 0 if turn_filter_on(top, sys.executable) else 1
    print("Checkout filter here: %s" % ("ON" if SG.filter_active(top) else "OFF"))
    return 0


def cmd_hook_trust(rest):
    """Human-only escape hatch: let MY edited copy of a pinned hook's files run, for exactly this content. Logged."""
    import sg_pin as PIN, sg_guard as SG
    revoke = "--revoke" in rest
    cmds = [r for r in rest if r != "--revoke"]
    top = T.repo_identity(os.getcwd())[0]
    if not cmds or not top:
        print('usage (inside the repository): gate.py hook-trust "<exact hook command>" [--revoke]'); return 2
    try:
        pins = PIN.trust_local(top, cmds[0], revoke)
    except T.TrustError as ex:
        print("NOT changed: %s" % ex); return 1
    SG.log(top, "hook-trust %s by %s: %s" % ("revoked" if revoke else "granted", os.environ.get("USER") or os.environ.get("USERNAME") or "?", cmds[0]))
    print(("Revoked." if revoke else "Trusted for exactly the current content of: %s. Any further change to them needs this "
           "again; the default branch's copy is used again once they match it.") % ", ".join(pins) if not revoke else "Revoked.")
    return 0


def cmd_hook_selftest():
    """Run each registered user-level hook command exactly as the AI tool would, with a sample edit, and time it."""
    act = T.read_json(T.active_path())
    if not act:
        print("No trusted runtime installed. Run: gate.py install --user"); return 1
    gate = T.launcher_path()
    payload = json.dumps({"tool_name": "Write", "tool_input": {"file_path": str(ROOT / "story_gate_selftest.py")}, "cwd": str(ROOT)})
    limits = {"claude": 15, "codex": 15, "cursor": 15, "gemini": 15, "windsurf": 15}
    rc = 0
    for cl in T.registered_clients(gate):
        e = dict(os.environ); e.pop("STORY_GATE_ROOT", None)
        t0 = time.time()
        r = subprocess.run([sys.executable, "-I", str(gate), "hook", "--client", cl, "--event", "pre"], cwd=ROOT, input=payload,
                           capture_output=True, text=True, env=e, timeout=60)
        ms = (time.time() - t0) * 1000
        verdict = "blocks" if r.returncode == 2 else ("allows" if r.returncode == 0 else "ERROR exit %d" % r.returncode)
        ok = r.returncode in (0, 2) and ms < limits.get(cl, 15) * 1000 * 0.5
        rc |= 0 if ok else 1
        print("  %-8s %s an edit with no READY story here (%d ms)%s" % (cl, verdict, ms, "" if ok else "  <- PROBLEM"))
    print("Expected: 'blocks' in enforce mode, 'allows' (with a warning) in warn mode. Then try a real edit in each tool once.")
    return rc


def read_active_dir():
    return T.read_json(T.active_path()).get("dir")


def shutil_rmtree(p):
    import shutil
    shutil.rmtree(p, ignore_errors=True)


def cmd_doctor(repo=None, strict=False, prove=False):
    """Plain-English health check. With --repo it also checks GitHub protection and agent identity."""
    import sg_github as G
    fails = []
    try:
        c = cfg()
    except ConfigError as e:
        print("  FAIL  " + str(e)); return 1
    print("story-gate %s  root=%s  mode=%s  enforce_points=%s" % (VERSION, ROOT, c["mode"], c.get("enforce_points")))
    act = T.read_json(T.active_path())
    if act:
        rt = Path(act.get("dir", ""))
        probs = T.verify_self(rt) if rt.is_dir() else ["missing"]
        print("  trusted runtime: %s (%s, %s)" % (act.get("version"), "signed" if act.get("signed") else "UNSIGNED development copy",
                                                 "intact" if not probs else "PROBLEM: " + "; ".join(probs[:2])))
        regd = T.registered_clients(T.launcher_path())
        for cl in T.USER_CLIENTS:
            print("  %-8s user hooks %s" % (cl, "on" if cl in regd else "off"))
        for cl in T.DEGRADED_CLIENTS:
            print("  %-8s reduced protection (no verified user-level hooks; CI still checks every PR)" % cl)
        if probs:
            fails.append("runtime")
    else:
        print("  trusted runtime: NOT installed on this computer - hooks are off. Run: gate.py install --user")
        fails.append("runtime")
    e = T.enrollment(ROOT) or {}
    print("  this repository: %s" % ("enrolled, policy from %s" % e.get("policy_ref") if e.get("enrolled") else "NOT enrolled (gate.py enroll)"))
    import sg_guard as SG
    filt = SG.filter_active(str(ROOT))
    print("  checkout filter: %s" % ("ON - a branch's hook changes can't reach disk here" if filt else
                                     ("OFF - turn on: gate.py filter on" if e.get("enrolled") else "OFF (only enrolled repositories are covered)")))
    if e.get("enrolled") and not filt:
        fails.append("filter")
    st = SG.lockdown_status(rd(T.user_hook_files()["claude"]))
    print("  lockdown (optional): %s" % ("ON (%s)%s" % (st["file"], " - your own Claude hooks changed since; run gate.py lockdown --on again" if st["stale"] else "")
                                         if st["on"] else "off - recommended; gate.py lockdown explains it"))
    for tool, level, nxt in SG.client_matrix(str(ROOT)):
        print("  %-8s repository hooks: %s%s" % (tool, level, ("  (" + nxt + ")") if nxt else ""))
    import sg_pin as PIN
    rows = PIN.report(str(ROOT), SG.default_hook_commands(str(ROOT)))
    if rows:
        label = {"plain": "runs nothing from the repository", "pinned": "PINNED - runs only the default branch's copy",
                 "accepted": "ACCEPTED - runs repository code unverified", "blocked": "BLOCKED - runs repository code, not pinned"}
        print("  project hooks approved on the default branch:")
        for cmd_, tier, hint in rows:
            print("    %-58s %s%s" % (cmd_[:58], label[tier], ("\n      -> " + hint) if hint else ""))
    age = PIN.policy_age_days(str(ROOT))
    if age is not None and age > 7:
        print("  WARNING: last git fetch was %d days ago; hooks use approvals from your local copy of the default branch" % age)
    links = SG.symlinked_hook_paths(str(ROOT))
    if links:
        print("  PROBLEM: AI-tool settings stored as symlinks (the filter can't check them): %s" % ", ".join(links[:5]))
        fails.append("symlinks")
    if prove:
        ok, lines = SG.prove(str(ROOT))
        print("  proof (canary hook on a throwaway commit):")
        print("\n".join(lines))
        ok = ok and not links
        print("  proof: %s" % ("PASSED - nothing from the canary reached disk" if ok else "FAILED"))
        if not ok:
            fails.append("prove")
    found = T.project_hook_findings(ROOT, HOOK_FILES)
    if found:
        print("  WARNING: project hook files run story-gate code from the branch: %s (gate.py install --user removes them)" % ", ".join(found))
        fails.append("project-hooks")
    for f, body in ((".github/workflows/story-gate.yml", CI_YML), (".github/workflows/story-gate-audit.yml", AUDIT_YML),
                    (".github/workflows/story-gate-dashboard.yml", dash_yml())):
        cur = rd(ROOT / f)
        st = "ok" if cur == body else ("OUTDATED - re-run install" if cur.startswith("# managed by story-gate") else ("missing" if not cur else "NOT MANAGED by story-gate"))
        print("  %-42s %s" % (f, st))
        if st != "ok":
            fails.append(f)
    owners = rd(ROOT / ".github/CODEOWNERS") or rd(ROOT / "CODEOWNERS")
    users, teams = G.codeowners(owners)
    print("  %-42s %s" % ("CODEOWNERS ('*' rule)", ", ".join(users + teams) or "MISSING - run gate.py setup-repo"))
    if not users:
        fails.append("codeowners")
    s_ = J.settings(c)
    print("  judge: provider=%s model=%s key=%s" % (s_["provider"], s_.get("model"), "set" if J.env_key(s_.get("key_env", "")) else "NOT SET (%s)" % s_.get("key_env")))
    if J.available(c):
        t0 = time.time()
        r = J.ask(c, {"ping": "story-gate doctor"}, {"ok": {"type": "noul", "instructions": "This is a connectivity test; answer yes."}})
        tier = r.get("tier")
        print("  judge answered: %s" % ("%s (%d ms)%s" % (tier, (time.time() - t0) * 1000, " - capped at CONCERNS unless calibrated" if tier == "emulated" else "") if tier != "none" else "NO - %s" % r.get("error")))
    else:
        print("  judge: unavailable locally - CI still judges if the repo secret is set; local verdicts can never PASS")
    if repo:
        tok = G.human_token()
        if tok:
            enforced, missing = G.protection(repo, c["base_branch"], tok)
            print("  branch rules: %s" % ("ENFORCED" if enforced else "NOT ENFORCED - " + "; ".join(missing)))
            if not enforced:
                fails.append("rules")
            st_, info, _ = G.call("GET", "/repos/%s" % repo, tok)
            if st_ == 200 and isinstance(info, dict) and info.get("has_issues") is False:
                print("  WARNING: Issues are turned off, so the dashboard can't be pinned as an issue. It still appears in each run's summary and report.")
            me = G.whoami(tok)
            if me and me.lower() in [u.lower() for u in users]:
                print("  WARNING: this shell holds the GitHub login of code owner '%s'. AI agents must not run with it - use gate.py agent-env." % me)
                fails.append("human-token-in-agent-env")
        else:
            print("  branch rules: could not check (no GitHub token in this shell)")
    try:
        rec = G.agent_record()
        print("  agent App: %s (key %s)" % (rec["slug"], "private" if G.key_is_private(rec["key"]) else "TOO OPEN - chmod 600"))
    except Exception:
        print("  agent App: none (only needed when AI tools run on this computer: gate.py setup-agent)")
    return 1 if ((strict or prove) and (fails if strict else "prove" in fails)) else 0


# ------------------------------------------------------------------ main
def origin_repo():
    u = git("remote", "get-url", "origin").strip()
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", u)
    return m.group(1) if m else None


def cmd_setup(cmd, kv, rest):
    import sg_github as G
    repo = kv.get("repo") or origin_repo()
    if cmd == "setup-repo":
        tok = G.human_token()
        if not tok:
            sys.exit("Sign in first: run `gh auth login` (or set GH_TOKEN to your own token). This one-time step must run as you, not as the AI.")
        me = G.whoami(tok)
        owners = [o.strip() for o in (kv.get("owners") or me or "").split(",") if o.strip()]
        if not repo or not owners:
            sys.exit("usage: gate.py setup-repo [--repo owner/name] [--owners you,teammate]")
        for line in G.setup_repo(ROOT, repo, owners, tok, dry_run="--dry-run" in rest):
            print("  " + line)
        if "--dry-run" not in rest:
            wj(ROOT / "docs" / "story-gate-ruleset.json", G.ruleset_json())
            print("  Ruleset JSON for manual import: docs/story-gate-ruleset.json")
        return 0
    if cmd == "setup-agent":
        rec = G.setup_agent(owner=kv.get("org"), org=bool(kv.get("org")), code=kv.get("code"), open_browser="--no-browser" not in rest)
        print("Agent App created: %s. Install it on your repos: %s" % (rec["slug"], rec["install_url"]))
        print("Key saved privately at %s. Then run: gate.py agent-env --repo %s" % (rec["key"], repo or "owner/name"))
        return 0
    if not repo:
        sys.exit("pass --repo owner/name")
    if cmd == "agent-token":
        if "--git-credential" in rest:  # git credential helper protocol: answer only for github.com
            req = dict(l.split("=", 1) for l in sys.stdin.read().splitlines() if "=" in l)
            if req.get("host") not in ("github.com", None) or "get" not in rest:
                return 0  # git also calls helpers with store/erase: never mint a token for those
            tok, _, _ = G.agent_token(repo)
            print("username=x-access-token\npassword=%s" % tok)
            return 0
        tok, exp, _ = G.agent_token(repo)
        print(tok)
        return 0
    if cmd == "agent-env":
        tok, exp, rec = G.agent_token(repo)
        name, email = G.bot_identity(rec)
        sh = os.name != "nt"
        print(("export GH_TOKEN=%s\nexport GITHUB_TOKEN=%s" if sh else "$env:GH_TOKEN='%s'\n$env:GITHUB_TOKEN='%s'") % (tok, tok))
        print('git config user.name "%s"\ngit config user.email "%s"\ngit config commit.gpgsign false' % (name, email))
        push = git("remote", "get-url", "--push", "origin").strip()
        if push and not push.startswith("https://"):  # SSH would push with YOUR key; credential helpers only cover HTTPS
            print("git remote set-url --push origin https://github.com/%s.git" % repo)
        print("git config --local --replace-all credential.helper ''")  # drop inherited helpers (e.g. a keychain holding YOUR login)
        home = os.environ.get("STORY_GATE_HOME")  # remembered so later git sessions find the same agent key
        helper = '!%s"%s" "%s" agent-token --repo %s --git-credential' % (
            ('STORY_GATE_HOME="%s" ' % home.replace("\\", "/")) if home else "", sys.executable.replace("\\", "/"), str(GATE / "gate.py").replace("\\", "/"), repo)
        print("git config --local --add credential.helper '%s'" % helper)  # this Python, absolute path: works on Windows too
        print("# token expires %s; git refreshes it through the helper above" % exp)
        return 0
    return 2


def flags(argv):
    kv, rest, i = {}, [], 0
    while i < len(argv):
        if argv[i] in ("--strict", "--dry-run", "--no-browser", "--git-credential", "--user", "--unsigned", "--offline", "--open", "--publish",
                       "--report-failure", "--prove", "--on", "--off", "--explain"):
            rest.append(argv[i]); i += 1
        elif argv[i].startswith("--") and i + 1 < len(argv):
            k = argv[i][2:]
            kv[k] = (kv[k] + "\n" + argv[i + 1]) if k == "keep-project-hook" and k in kv else argv[i + 1]  # repeatable
            i += 2
        else:
            rest.append(argv[i]); i += 1
    return kv, rest


def main(argv):
    if not argv:
        print(__doc__); return 0
    cmd, args = argv[0], argv[1:]
    if cmd == "hook-filter":  # git checkout filter (sg_guard), called through the launcher: stdin -> stdout
        import sg_guard as SG
        return SG.filter_main(args[0], args[1])
    if cmd == "run-approved":  # a pinned project hook, called by the AI tool through the launcher (sg_pin)
        import sg_pin as PIN
        return PIN.run_approved(args[0] if args else "")
    if cmd == "record-tests":
        if not args:
            sys.exit("usage: record-tests <ID> [-- <cmd...>]")
        return cmd_record_tests(args[0], args[args.index("--") + 1:] if "--" in args else [])
    kv, rest = flags(args)
    if cmd == "install" and "--user" in rest:
        return cmd_user("install", kv, rest)
    if cmd == "install":
        return cmd_install(kv.get("python", "python" if os.name == "nt" else "python3")) or 0
    if cmd == "uninstall" and "--user" in rest:
        return cmd_user("uninstall", kv, rest)
    if cmd == "hook-selftest":
        return cmd_hook_selftest()
    if cmd in ("enroll", "unenroll", "upgrade", "rollback", "release-sign"):
        return cmd_user(cmd, kv, rest)
    if cmd == "lockdown":
        return cmd_lockdown(kv, rest)
    if cmd == "filter":
        return cmd_filter(rest)
    if cmd == "hook-trust":
        return cmd_hook_trust(rest)
    if cmd == "plan":
        return cmd_plan(rest[0], kv.get("title", ""), kv.get("feature")) or 0
    if cmd == "feature":
        return cmd_feature(rest[0] if rest else "", kv.get("title", ""), kv.get("description", "")) or 0
    if cmd == "dashboard":
        import sg_dashboard as D
        return D.cli(sys.modules[__name__], kv, rest)
    if cmd == "start":
        return cmd_start(rest[0], kv.get("client"), kv.get("model")) or 0
    if cmd == "source":
        return cmd_source(rest[0]) or 0
    if cmd == "score":
        return cmd_score(rest[0], rest[1], kv.get("base"))
    if cmd == "decide":
        if kv.get("drift") not in ("story", "spec", "none") or not kv.get("by"):
            sys.exit("decide <ID> --drift story|spec|none --by WHO --note TEXT")
        esc = [(load_v(rest[0], x) or {}).get("at", "") + "|" + x for x in ("ready", "done")
               if (load_v(rest[0], x) or {}).get("overall") == "ESCALATED"]
        ph = kv.get("phase") or (max(esc).split("|")[1] if esc else "ready")
        return append_decision(rest[0], {"kind": "drift", "phase": ph, "drift": kv["drift"], "by": kv["by"], "note": kv.get("note", "")}) or 0
    if cmd == "waive":
        if len(rest) < 2 or not kv.get("by") or not kv.get("reason"):
            sys.exit("waive <ID> <check> --by WHO --reason TEXT")
        if rest[1] not in READY_Q.keys() | DONE_Q.keys():
            sys.exit("only semantic checks can be waived (%s); structural facts must be fixed" % ", ".join(sorted(READY_Q.keys() | DONE_Q.keys())))
        ph = kv.get("phase") or ("ready" if rest[1] in READY_Q else "done")
        return append_decision(rest[0], {"kind": "waiver", "phase": ph, "check": rest[1], "by": kv["by"], "reason": kv["reason"]}) or 0
    if cmd == "checkpoint":
        r = cmd_checkpoint(rest[0])
        return 0 if r.get("status") == "ON_TRACK" else 1
    if cmd == "learn":
        return cmd_learn(rest[0], kv) or 0
    if cmd == "learnings":
        return cmd_learnings(rest) or 0
    if cmd == "status":
        return cmd_status(rest[0] if rest else None)
    if cmd == "hook":
        return cmd_hook(kv.get("client", ""), kv.get("event", "pre"))
    if cmd == "ci":
        return cmd_ci(kv.get("tests"))
    if cmd == "ci-tests":
        return cmd_ci_tests(rest[0] if rest else "sg-tests")
    if cmd == "audit":
        return cmd_audit()
    if cmd == "label":
        return cmd_label(rest[0], rest[1], rest[2] if len(rest) > 2 else "", kv.get("note", "")) or 0
    if cmd == "judge-calibrate":
        ok, details = J.calibrate(cfg())
        print("\n".join(details))
        wj(GATE / "judge-calibration.json", {"ok": ok, "identity": J.identity(cfg()), "at": now(), "details": details})
        print("story-gate judge calibration: %s" % ("PASSED - set judge.emulated_allow_pass to let this judge PASS stories" if ok else "FAILED - this judge stays capped at CONCERNS"))
        return 0 if ok else 1
    if cmd in ("setup-repo", "setup-agent", "agent-token", "agent-env"):
        return cmd_setup(cmd, kv, rest)
    if cmd == "publish":
        return cmd_publish()
    if cmd == "doctor":
        return cmd_doctor(kv.get("repo"), strict="--strict" in args, prove="--prove" in args)
    print(__doc__); return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
