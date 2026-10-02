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
  install [--clients all|claude,codex,cursor,gemini,windsurf,grok] [--python python3]   wire hooks, instructions, CI
  start <ID>                         make story folder + skeletons, set active story
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
  ci                                 called by the CI workflow
  publish                            send outbox events to configured sinks
  doctor                             check config, judge, wiring
"""
import fnmatch, hashlib, json, os, re, subprocess, sys, time, urllib.request, urllib.error
from pathlib import Path

VERSION = "0.2.0"
ROOT = Path(os.environ.get("STORY_GATE_ROOT") or Path(__file__).resolve().parent.parent)
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

DEFAULT_CONFIG = {
    "version": 1,
    "mode": "warn",
    "enforce_points": [],
    "block_on_concerns": False,
    "story_id_pattern": "[A-Z][A-Z0-9]+-[0-9]+",
    "base_branch": "main",
    "exempt_globs": [".story-gate/stories/*/*.md", ".story-gate/stories/*/tests.json", "docs/**/*.md", "*.md"],
    "test_command": "",
    "thresholds": {"pass": 0.7, "concerns": 0.4},
    "judge": {"jev": True, "allow_self_judge_pass": False, "max_chars": 90000},
    "sources": [],
    "sinks": [{"type": "repo"}],
    "models": {"intake": "small", "context": "medium", "tests": "medium", "handoff": "medium", "learnings": "small"},
    "model_tiers": {"small": "cheapest fast model your client offers", "medium": "mid-tier coding model (never frontier)"},
    "checkpoint": {"every_edits": 10},
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
    "tests_implemented": "The diff adds or updates automated tests matching the planned positive, negative, edge and regression cases.",
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
HANDOFF_SECTIONS = ("## What changed", "## Interfaces and contracts", "## How to verify", "## Known limits", "## Downstream consumers", "## Drift decisions")


# ------------------------------------------------------------------ small helpers
def cfg():
    p = GATE / "config.json"
    c = json.loads(json.dumps(DEFAULT_CONFIG))
    if p.exists():
        user = json.loads(p.read_text(encoding="utf-8"))
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(c.get(k), dict):
                c[k].update(v)
            else:
                c[k] = v
    return c


def rd(p, default=""):
    p = Path(p)
    return p.read_text(encoding="utf-8", errors="ignore") if p.exists() else default


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


def exempt(path, c):
    p = str(path).replace("\\", "/")
    if os.path.isabs(p) or re.match(r"^[A-Za-z]:/", p):
        try:
            p = Path(p).resolve().relative_to(ROOT.resolve()).as_posix()
        except ValueError:
            return False  # outside the repo: not ours to exempt
    p = os.path.normpath(p).replace("\\", "/")
    if p.startswith("..") or p.startswith("/"):
        return False
    return any(fnmatch.fnmatch(p, g) for g in c["exempt_globs"])


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


def inputs_hash(sd, phase):
    names = ["story.md", "context.md", "tests.json"] + (["handoff.md", "test_results.json"] if phase == "done" else [])
    blob = "".join(rd(sd / n) for n in names)
    if phase == "done":
        blob += "".join(json.dumps(r) for r in jsonl(LEARNINGS) if r.get("story") == sd.name) + work_fingerprint()
    return hashlib.sha256(blob.encode("utf-8", "ignore")).hexdigest()[:16]


def emit(event_type, sid, payload):
    OUTBOX.parent.mkdir(parents=True, exist_ok=True)
    with OUTBOX.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now(), "event_type": "story_gate." + event_type, "story": sid,
                            "repo": ROOT.name, "payload": payload}, ensure_ascii=False) + "\n")


# ------------------------------------------------------------------ judge (Jev)
def openrouter_key():
    k = os.environ.get("OPENROUTER_API_KEY")
    if k:
        return k.strip()
    for p in (os.path.expanduser("~/mnt/ENV/.env"), r"F:\ENV\.env"):
        if os.path.exists(p):
            for line in open(p, encoding="utf-8", errors="ignore"):
                if line.strip().startswith("OPENROUTER_API_KEY="):
                    return line.split("=", 1)[1].strip().strip("'\"")
    return None


def jev(state, questions):
    """Returns {'answers': {...}} or {'error': ...}. Never raises."""
    k = openrouter_key()
    if not k:
        return {"error": "no OPENROUTER_API_KEY"}
    body = {"model": "typesafe/jev-1.13", "state": state, "questions": questions}
    req = urllib.request.Request("https://openrouter.ai/api/alpha/decisions", data=json.dumps(body).encode(),
                                 headers={"Authorization": "Bearer " + k, "Content-Type": "application/json",
                                          "X-Title": "story-gate"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=90).read().decode())
    except urllib.error.HTTPError as e:
        return {"error": "HTTP %s %s" % (e.code, e.read().decode(errors="ignore")[:300])}
    except Exception as e:
        return {"error": repr(e)[:300]}


def num(x):
    try:
        return float(x)
    except Exception:
        return 0.0


def judge(phase, state, c, sd, skip=()):
    """Score semantic checks. Jev first; else agent self-scores from <phase>.self.json."""
    qs_text = {k: v for k, v in (READY_Q if phase == "ready" else DONE_Q).items() if k not in skip}
    questions = {k: {"type": "noul", "instructions": v} for k, v in qs_text.items()}
    questions["drift_direction"] = {"type": "choice", "instructions": "Which way does any drift between the story/work and the PRD/TRD point?",
                                    "criteria": DRIFT_CHOICE if phase == "ready" else DRIFT_CHOICE_DONE}
    if c["judge"].get("jev", True):
        r = jev(state, questions)
        if isinstance(r.get("answers"), dict):
            a = r["answers"]
            scores = {}
            for k in qs_text:  # a missing or malformed answer scores 0 (fail closed)
                try:
                    scores[k] = float(a[k]["noul"])
                except Exception:
                    scores[k] = 0.0
            d = a.get("drift_direction") if isinstance(a.get("drift_direction"), dict) else {}
            choices = DRIFT_CHOICE if phase == "ready" else DRIFT_CHOICE_DONE
            drift = d.get("choice") if d.get("choice") in choices else "architect_must_decide"
            return {"judge": "jev", "scores": scores, "drift": drift,
                    "drift_conf": num(d.get("confidence")), "cost": (r.get("usage") or {}).get("cost")}
        jerr = r.get("error") or "unexpected Jev response"
    else:
        jerr = "jev disabled in config"
    selfp = sd / ("%s.self.json" % phase)
    if selfp.exists():
        s = load_json(selfp)
        sc = s.get("scores") if isinstance(s.get("scores"), dict) else {}
        def f(x):
            try:
                return min(1.0, max(0.0, float(x)))
            except Exception:
                return 0.0
        return {"judge": "self", "scores": {k: f(sc.get(k, 0)) for k in qs_text},
                "drift": s.get("drift", "architect_must_decide"), "drift_conf": 0.0, "jev_error": jerr}
    return {"judge": "none", "scores": {}, "drift": "architect_must_decide", "drift_conf": 0.0, "jev_error": jerr}


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
    nohand = [d for d in deps if not (STORIES / d / "handoff.md").exists()
              and not re.search(r"^###\s+" + re.escape(d) + r"\b(?!-)", up, re.M)]
    out["upstream_handoffs"] = (not nohand, "no handoff found for upstream: " + ", ".join(nohand))
    return out, fm


def trace(sd, diff_files, diff_text, tr):
    """AC -> planned cases -> automated test refs -> found in code -> suite result. Writes trace.md."""
    t = load_json(sd / "tests.json")
    plan = {str(a.get("id")): a for a in (t.get("acceptance_criteria") or []) if isinstance(a, dict)}
    corpus = diff_text + "".join(rd(ROOT / f) for f in diff_files if not f.startswith(".story-gate/"))
    suite = "GREEN" if tr.get("exit_code") == 0 else ("RED" if tr else "NOT RUN")
    rows, missing = [], []
    for aid, text, refs in acs(sd):
        a = plan.get(aid, {})
        cases = sum(len(a.get(k) or []) for k in TEST_CATS)
        found = [r for r in refs if r in corpus]
        if not refs or len(found) < len(refs):
            missing.append(aid)
        rows.append("| %s | %s | %d | %s | %s | %s |" % (aid, text[:60].replace("|", "/"), cases, ", ".join(refs) or "—",
                                                     "%d/%d" % (len(found), len(refs)), suite))
    md = "# Traceability: %s\n\n| AC | Criterion | Planned cases | Automated tests (test_refs) | Found in code | Suite |\n|---|---|---|---|---|---|\n" % sd.name
    wj_text(sd / "trace.md", md + "\n".join(rows) + "\n")
    return missing


def wj_text(p, text):
    Path(p).write_text(text, encoding="utf-8")


def struct_done(sd, sid, diff_files, c, diff_text=""):
    out = {}
    rj = load_json(sd / "ready.json")
    out["ready_gate_passed"] = (passed(rj, c, sd), "READY gate not passed (overall=%s)" % rj.get("overall", "never run"))
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
    miss = trace(sd, diff_files, diff_text, tr)
    out["traceability"] = (not miss, "ACs without automated tests found in the code (tests.json test_refs): " + ", ".join(miss))
    return out


# ------------------------------------------------------------------ verdict (pure, unit-tested)
def band(p, th):
    return "PASS" if p >= th["pass"] else ("CONCERNS" if p >= th["concerns"] else "FAIL")


def verdict(structural, judged, decisions, waivers, c):
    th = c["thresholds"]
    checks = {}
    for k, (ok, why) in structural.items():
        checks[k] = {"status": "PASS" if ok else "FAIL", "why": "" if ok else why, "kind": "structural"}
    for k, p in judged.get("scores", {}).items():
        st = band(p, th)
        if judged["judge"] != "jev" and st == "PASS" and not c["judge"].get("allow_self_judge_pass"):
            st = "CONCERNS"  # an agent cannot pass its own homework
        checks[k] = {"status": st, "score": round(p, 3), "kind": judged["judge"], "group": CHECK_GROUP.get(k, "")}
    if judged["judge"] == "none":
        checks["judge_available"] = {"status": "FAIL", "why": "no Jev and no self-scores: semantic checks did not run (%s)" % judged.get("jev_error"), "kind": "structural"}
    drift = judged.get("drift", "none")
    decided = decisions[-1] if decisions else None
    if drift == "work_should_change":  # code is simply wrong vs agreed docs: a fix, not an escalation
        checks["drift_fix_work"] = {"status": "FAIL", "why": "code deviates from the story/TRD — fix the code (no spec change needed)", "kind": "decision"}
    elif drift != "none":
        if decided:
            checks["drift_decision"] = {"status": "PASS", "why": "decided: %s by %s" % (decided["drift"], decided["by"]), "kind": "decision"}
        else:
            checks["drift_decision"] = {"status": "ESCALATED", "why": "drift points to '%s' — needs a recorded decision from the architect/orchestrator" % drift, "kind": "decision"}
    for k, w in waivers.items():
        if k in checks and checks[k]["status"] != "PASS":
            checks[k]["status"], checks[k]["why"] = "WAIVED", "waived by %s: %s" % (w["by"], w["reason"])
    sts = [v["status"] for v in checks.values()]
    overall = ("FAIL" if "FAIL" in sts else "ESCALATED" if "ESCALATED" in sts else
               "CONCERNS" if "CONCERNS" in sts else "WAIVED" if "WAIVED" in sts else "PASS")
    return {"overall": overall, "drift": drift, "checks": checks}


def passed(v, c, sd=None):
    v = v or {}
    o = v.get("overall")
    if v.get("judge") != "jev" and not c["judge"].get("allow_self_judge_pass"):
        return False  # without an independent judge nothing passes
    if sd is not None and v.get("inputs_hash") != inputs_hash(sd, v.get("phase", "ready")):
        return False  # story/tests/handoff/code changed since this verdict: re-score
    return o in ("PASS", "WAIVED") or (o == "CONCERNS" and not c.get("block_on_concerns"))


def records(sd, phase):
    dec, wav = [], {}
    for r in jsonl(sd / "decisions.jsonl"):
        if r.get("phase", phase) != phase:
            continue
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

## Drift decisions
None
""",
}


def cmd_start(sid):
    sd = sdir(sid)
    sd.mkdir(parents=True, exist_ok=True)
    for name, body in SKELETONS.items():
        p = sd / name
        if not p.exists() and name != "handoff.md":
            p.write_text(body.replace("{id}", sid), encoding="utf-8")
    ACTIVE.write_text(sid, encoding="utf-8")
    emit("started", sid, {})
    print("story-gate: active story %s -> fill %s (story.md, context.md, tests.json), then: score %s ready" % (sid, sd.relative_to(ROOT), sid))


def cmd_source(sid):
    for s in cfg().get("sources", []):
        if s.get("type") == "command" and s.get("command"):
            cmd = s["command"].replace("{id}", sid)
            r = subprocess.run(cmd, shell=True, cwd=ROOT, capture_output=True, text=True, timeout=120)
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
        if n.startswith(".story-gate/"):
            continue
        f = ROOT / n
        mode = b"x" if f.is_file() and os.access(f, os.X_OK) else b"-"  # executable bit is part of the evidence
        h.update(n.encode("utf-8", "replace") + b"\0" + mode + (hashlib.sha256(f.read_bytes()).digest() if f.is_file() else b"DELETED"))
    return h.hexdigest()[:16]


def cmd_record_tests(sid, argv):
    pinned = cfg().get("test_command")
    if pinned:
        argv = pinned
    if not argv:
        sys.exit("usage: record-tests <ID> -- <test command...>")
    t0 = time.time()
    try:
        r = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800,
                           shell=isinstance(argv, str) or os.name == "nt")
        code, tail = r.returncode, (r.stdout + r.stderr)[-4000:]
    except Exception as e:
        code, tail = -1, repr(e)
    wj(sdir(sid) / "test_results.json", {"command": argv if isinstance(argv, str) else " ".join(argv), "exit_code": code, "seconds": round(time.time() - t0, 1),
                                         "head": git("rev-parse", "HEAD").strip(), "fingerprint": work_fingerprint(), "at": now(), "output_tail": tail})
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
                mb = git("merge-base", "HEAD", up).strip() if up else mb
            return mb or b
    raise RuntimeError("base branch %r not found (set base_branch in .story-gate/config.json)" % base)


def zlist(*args):
    return [x for x in git("-c", "core.quotepath=false", *args, "-z").split("\0") if x]


def diff_against(base):
    mb = resolve_base(base)
    names = set(zlist("diff", "--name-only", mb)) | set(zlist("diff", "--name-only")) \
        | set(zlist("ls-files", "--others", "--exclude-standard"))
    return mb, sorted(names), git("diff", mb, "--", ".", ":(exclude).story-gate")  # gate records never count as code evidence


def cmd_score(sid, phase, base=None):
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
        structural = struct_done(sd, sid, files, c, diff)
        skip = set()
        learn = [l for l in rd(LEARNINGS).splitlines() if '"%s"' % sid in l]
        state = {"story": rd(sd / "story.md")[:mx // 6], "context": rd(sd / "context.md")[:mx // 6],
                 "test_plan": rd(sd / "tests.json")[:mx // 8], "handoff": rd(sd / "handoff.md")[:mx // 8],
                 "learnings": "\n".join(learn)[:mx // 16], "changed_files": files[:300],
                 "diff": diff[:mx // 2], "diff_truncated": len(diff) > mx // 2}
    else:
        sys.exit("phase must be ready or done")
    judged = judge(phase, state, c, sd, skip)
    dec, wav = records(sd, phase)
    v = verdict(structural, judged, dec, wav, c)
    v.update({"story": sid, "phase": phase, "at": now(), "judge": judged["judge"], "jev_error": judged.get("jev_error"),
              "inputs_hash": inputs_hash(sd, phase),
              "drift_confidence": judged.get("drift_conf"), "cost": judged.get("cost"), "gate_version": VERSION})
    wj(sd / ("%s.json" % phase), v)
    emit(phase + "_scored", sid, {"overall": v["overall"], "drift": v["drift"], "judge": v["judge"],
                                  "failed": [k for k, x in v["checks"].items() if x["status"] not in ("PASS", "WAIVED")]})
    if v["overall"] == "ESCALATED":
        emit("drift_escalated", sid, {"direction": v["drift"], "phase": phase})
    print_verdict(v)
    return 0 if passed(v, c, sd) else 1


DRIFT_CHOICE_MID = {
    "none": "The work so far is consistent with the story and PRD/TRD (being unfinished is fine).",
    "work_should_change": "The work so far deviates from the agreed story/TRD and should be corrected now.",
    "spec_should_change": "The work deliberately departs from the story/PRD/TRD in a way that may be better; the documents may need updating.",
    "architect_must_decide": "The work exposes a conflict between story and PRD/TRD that someone must decide.",
}


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
    for n, chunk in enumerate(chunks):
        q = {"ac_%02d" % j: {"type": "noul", "instructions": "The code so far fully implements %s: %s" % (aid, text)}
             for j, (aid, text, _) in enumerate(chunk)}
        if n == 0:
            q.update({k: {"type": "noul", "instructions": v} for k, v in base_q.items()})
            q["drift_direction"] = {"type": "choice", "instructions": "Is the work drifting from the story/PRD/TRD?", "criteria": DRIFT_CHOICE_MID}
        r = jev(state, q) if c["judge"].get("jev", True) else {"error": "jev disabled"}
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
    for k, x in sorted(v["checks"].items(), key=lambda kv: (kv[1].get("group", ""), kv[0])):
        if x["status"] != "PASS":
            print("  %-10s %-24s %s" % (x["status"], k, x.get("why") or "score=%s" % x.get("score")))


def append_decision(sid, rec):
    sd = sdir(sid)
    sd.mkdir(parents=True, exist_ok=True)
    rec["at"] = now()
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
    for src in (os.environ.get("STORY_GATE_ID", ""), os.environ.get("GITHUB_HEAD_REF", ""),
                git("rev-parse", "--abbrev-ref", "HEAD").strip()):
        m = pat.search(src or "")
        if m:
            return m.group(0)
    s = rd(ACTIVE).strip()
    if s:
        return s
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
    r, d = load_v(sid, "ready"), load_v(sid, "done")
    print("story-gate %s: READY=%s DONE=%s mode=%s" % (sid, (r or {}).get("overall", "not run"), (d or {}).get("overall", "not run"), c["mode"]))
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
    try:
        c = cfg()
        point = {"pre": "pre_edit", "stop": "stop", "post": "checkpoint"}.get(event, event)
        enforce = c["mode"] == "enforce" or point in c.get("enforce_points", [])
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
        paths = edited_paths(payload)
        if paths and all(exempt(p, c) for p in paths):
            return "", False
        gate_files = [p for p in paths if p.replace("\\", "/").split(".story-gate/")[0] in ("", "./") or "/.story-gate/" in p.replace("\\", "/")]
        gate_files = [p for p in gate_files if not exempt(p, c)]
        if gate_files:
            return "Agents must not edit gate files (%s). Use gate.py commands; verdicts, config and records are written only by gate.py or a human." % ", ".join(gate_files), True
        sid = story_id(c, payload)
        if not sid:
            return "No active story. Before editing code run `python .story-gate/gate.py start <STORY-ID>` and the READY gate (.story-gate/PROTOCOL.md).", True
        v = load_v(sid, "ready")
        if not passed(v, c, STORIES / sid):
            return "Story %s READY gate is %s (or out of date). Finish the READY steps in .story-gate/PROTOCOL.md before editing code." % (sid, (v or {}).get("overall", "not run")), True
        msg = contained(sid, c)
        return (msg, True) if msg else ("", False)
    # stop
    if payload.get("stop_hook_active") or int(payload.get("loop_count") or 0) >= 1:
        return "", False  # loop guard: we already asked once this turn
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
def cmd_ci():
    problems, code, enforce = [], [], False
    try:
        c = cfg()
        enforce = c["mode"] == "enforce" or "ci" in c.get("enforce_points", [])
        base = ("origin/" + os.environ["GITHUB_BASE_REF"]) if os.environ.get("GITHUB_BASE_REF") else c["base_branch"]
        c["base_branch"] = base
        _, files, _ = diff_against(base)
        code = [f for f in files if not exempt(f, c)]
        sid = story_id(c)
        if code and not sid:
            problems.append("code changed but no story id in branch name / PR title (pattern %s)" % c["story_id_pattern"])
        elif code:
            independent = bool(openrouter_key()) and c["judge"].get("jev", True)
            for ph in ("ready", "done"):
                if independent and (STORIES / sid).exists():
                    cmd_score(sid, ph, base)  # re-score here: the coding agent cannot grade itself
                    v = load_v(sid, ph)
                    ok = passed(v, c)
                else:
                    v = load_v(sid, ph)
                    ok = passed(v, c)
                if not ok:
                    problems.append("%s %s gate is %s" % (sid, ph.upper(), (v or {}).get("overall", "missing")))
            if not independent:
                problems.append("not independently verified: add the OPENROUTER_API_KEY secret so CI can re-score with Jev")
    except Exception as e:
        problems.append("story-gate could not run: %s" % e)
    level = "error" if enforce else "warning"
    for p in problems:
        print("::%s title=story-gate::%s" % (level, p))
    print("story-gate CI: %s (%s mode, %d code files)" % ("OK" if not problems else "%d problem(s)" % len(problems), "enforce" if enforce else "warn", len(code)))
    return 1 if (problems and enforce) else 0


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


def merge_hooks(path, new):
    """Add our hook entries to a JSON hooks file without touching anything else. Idempotent."""
    p = ROOT / path
    p.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(rd(p, "{}") or "{}")
    for k, v in new.items():
        if k != "hooks":
            data.setdefault(k, v)
    hooks = data.setdefault("hooks", {})
    for ev, entries in new["hooks"].items():
        cur = hooks.setdefault(ev, [])
        cur[:] = [e for e in cur if "story-gate" not in json.dumps(e) and ".story-gate/gate.py" not in json.dumps(e)]
        cur.extend(entries)
    p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def client_configs(py):
    g = ".story-gate/gate.py"
    cmd = lambda cl, ev, pre="": "%s %s%s hook --client %s --event %s" % (py, pre, g, cl, ev)
    claude_pre = '"$CLAUDE_PROJECT_DIR"/'
    return {
        "claude": (".claude/settings.json", {"hooks": {
            "PreToolUse": [{"matcher": "Edit|Write|MultiEdit|NotebookEdit", "hooks": [{"type": "command", "command": cmd("claude", "pre", claude_pre), "timeout": 15}]}],
            "PostToolUse": [{"matcher": "Edit|Write|MultiEdit|NotebookEdit", "hooks": [{"type": "command", "command": cmd("claude", "post", claude_pre), "timeout": 60}]}],
            "Stop": [{"hooks": [{"type": "command", "command": cmd("claude", "stop", claude_pre), "timeout": 60}]}]}}),
        "codex": (".codex/hooks.json", {"hooks": {
            "PreToolUse": [{"matcher": "^(apply_patch|Edit|Write)$", "hooks": [{"type": "command", "command": cmd("codex", "pre"), "timeout": 15}]}],
            "PostToolUse": [{"matcher": "^(apply_patch|Edit|Write)$", "hooks": [{"type": "command", "command": cmd("codex", "post"), "timeout": 60}]}],
            "Stop": [{"hooks": [{"type": "command", "command": cmd("codex", "stop"), "timeout": 60}]}]}}),
        "cursor": (".cursor/hooks.json", {"version": 1, "hooks": {
            "preToolUse": [{"command": cmd("cursor", "pre"), "matcher": "Write"}],
            "postToolUse": [{"command": cmd("cursor", "post"), "matcher": "Write"}],
            "stop": [{"command": cmd("cursor", "stop")}]}}),
        "gemini": (".gemini/settings.json", {"hooks": {
            "BeforeTool": [{"matcher": "write_file|replace", "hooks": [{"type": "command", "command": cmd("gemini", "pre"), "timeout": 15000}]}],
            "AfterTool": [{"matcher": "write_file|replace", "hooks": [{"type": "command", "command": cmd("gemini", "post"), "timeout": 60000}]}],
            "AfterAgent": [{"matcher": "*", "hooks": [{"type": "command", "command": cmd("gemini", "stop"), "timeout": 60000}]}]}}),
        "windsurf": (".devin/hooks.json", {"hooks": {
            "pre_write_code": [{"command": cmd("windsurf", "pre"), "show_output": True}],
            "post_cascade_response": [{"command": cmd("windsurf", "stop"), "show_output": True}]}}),
        "grok": (".grok/hooks/story-gate.json", {"hooks": {
            "PreToolUse": [{"matcher": "Edit|Write|MultiEdit", "hooks": [{"type": "command", "command": cmd("grok", "pre"), "timeout": 15}]}],
            "Stop": [{"hooks": [{"type": "command", "command": cmd("grok", "stop"), "timeout": 60}]}]}}),
    }


CI_YML = """name: story-gate
on:
  pull_request:
    types: [opened, edited, synchronize, reopened]
jobs:
  story-gate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
          ref: ${{ github.event.pull_request.head.sha }}   # same commit the developer tested; tests.yml covers the merge result
          persist-credentials: false
      - name: story gate (script and config from the base branch)
        env:
          PR_TITLE: ${{ github.event.pull_request.title }}
          OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}
          STORY_GATE_ROOT: ${{ github.workspace }}
          BASE: origin/${{ github.base_ref }}
        run: |
          mkdir -p "$RUNNER_TEMP/sg"
          if git cat-file -e "$BASE:.story-gate/gate.py" 2>/dev/null; then
            git show "$BASE:.story-gate/gate.py" > "$RUNNER_TEMP/sg/gate.py"
            if git cat-file -e "$BASE:.story-gate/config.json" 2>/dev/null; then git show "$BASE:.story-gate/config.json" > .story-gate/config.json; fi
          else
            cp .story-gate/gate.py "$RUNNER_TEMP/sg/gate.py"   # first install PR
          fi
          python3 "$RUNNER_TEMP/sg/gate.py" ci
"""


def cmd_install(clients, py):
    GATE.mkdir(exist_ok=True)
    STORIES.mkdir(exist_ok=True)
    if not (GATE / "config.json").exists():
        wj(GATE / "config.json", DEFAULT_CONFIG)
    allc = client_configs(py)
    chosen = list(allc) if clients in ("all", "") else [x.strip() for x in clients.split(",")]
    done = []
    for cl in chosen:
        if cl not in allc:
            sys.exit("unknown client %s (known: %s)" % (cl, ", ".join(allc)))
        path, conf = allc[cl]
        merge_hooks(path, conf)
        done.append(path)
    for f in ("AGENTS.md", "CLAUDE.md", "GEMINI.md"):
        put_block(f, py)
    skill = rd(GATE / "SKILL.md")
    if skill:  # .agents/skills: Codex, Cursor, Gemini, Devin, Muse.  .claude/skills: Claude Code, Cursor, Grok, Muse
        for d in (".agents/skills/story-gate", ".claude/skills/story-gate"):
            (ROOT / d).mkdir(parents=True, exist_ok=True)
            (ROOT / d / "SKILL.md").write_text(skill, encoding="utf-8")
    wf = ROOT / ".github/workflows/story-gate.yml"
    if not wf.exists():
        wf.parent.mkdir(parents=True, exist_ok=True)
        wf.write_text(CI_YML, encoding="utf-8")
    gi = rd(ROOT / ".gitignore")
    add = [x for x in (".story-gate/.active", ".story-gate/outbox.jsonl", ".story-gate/.outbox.sent", ".story-gate/.edits") if x not in gi]
    if add:
        (ROOT / ".gitignore").write_text(gi.rstrip() + ("\n" if gi.strip() else "") + "\n".join(add) + "\n", encoding="utf-8")
    print("story-gate installed (mode=%s). Hooks: %s. Instructions: AGENTS.md, CLAUDE.md, GEMINI.md. CI: .github/workflows/story-gate.yml" % (cfg()["mode"], ", ".join(done)))
    print("Skill: .agents/skills/story-gate + .claude/skills/story-gate. Next: trust project hooks in Codex/Grok when prompted; Cowork and Muse rely on the instruction files + CI.")


def cmd_doctor():
    c = cfg()
    print("story-gate %s  root=%s  mode=%s  enforce_points=%s" % (VERSION, ROOT, c["mode"], c.get("enforce_points")))
    for cl, (path, _) in client_configs("py").items():
        print("  %-8s hooks %s" % (cl, "installed" if ".story-gate/gate.py" in rd(ROOT / path) else "NOT installed"))
    for f in ("AGENTS.md", "CLAUDE.md", "GEMINI.md", ".github/workflows/story-gate.yml"):
        print("  %-38s %s" % (f, "ok" if (ROOT / f).exists() else "missing"))
    if not c["judge"].get("jev", True):
        print("  judge: Jev disabled -> self-scores only (never PASS)"); return 0
    t0 = time.time()
    r = jev({"ping": "story-gate doctor"}, {"ok": {"type": "noul", "instructions": "This is a connectivity test; answer yes."}})
    print("  judge: %s" % ("Jev OK (%d ms)" % ((time.time() - t0) * 1000) if "answers" in r else "Jev unavailable: %s -> semantic checks fall back to agent self-scores (capped at CONCERNS)" % r.get("error")))
    return 0


# ------------------------------------------------------------------ main
def flags(argv):
    kv, rest, i = {}, [], 0
    while i < len(argv):
        if argv[i].startswith("--") and i + 1 < len(argv):
            kv[argv[i][2:]] = argv[i + 1]; i += 2
        else:
            rest.append(argv[i]); i += 1
    return kv, rest


def main(argv):
    if not argv:
        print(__doc__); return 0
    cmd, args = argv[0], argv[1:]
    if cmd == "record-tests":
        if "--" not in args:
            sys.exit("usage: record-tests <ID> -- <cmd...>")
        i = args.index("--")
        return cmd_record_tests(args[0], args[i + 1:])
    kv, rest = flags(args)
    if cmd == "install":
        return cmd_install(kv.get("clients", "all"), kv.get("python", "python" if os.name == "nt" else "python3")) or 0
    if cmd == "start":
        return cmd_start(rest[0]) or 0
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
        return cmd_ci()
    if cmd == "publish":
        return cmd_publish()
    if cmd == "doctor":
        return cmd_doctor()
    print(__doc__); return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
