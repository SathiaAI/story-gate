"""story-gate dashboard: one honest picture of every story, built from the records on every branch.

Where it shows up (all inside the repository, so it follows the repository's own permissions):
  - a pinned "Story-gate dashboard" issue, refreshed by .github/workflows/story-gate-dashboard.yml
  - a full report (self-contained HTML, Tabler styles, Viaknox colours) attached to each run as an artifact
  - `gate.py dashboard --open` on your computer, or `gate.py dashboard --serve` for a page that keeps itself current

Rules this module keeps:
  - Branch content is DATA. Records are read as git blobs (`git cat-file`), never checked out or executed, and every
    field is validated, size-capped and escaped before it reaches Markdown or HTML.
  - Every number says how it is calculated. Estimates say "(estimate)". Agent-recorded verdicts are labelled as such;
    CI is what verifies them.
  - Stdlib only. No network except the GitHub API when publishing, with the workflow's own token.
"""
import base64, hashlib, html, http.server, json, os, re, secrets, signal, statistics, subprocess, tempfile, threading, time, urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = 1
MAX_BLOB = 1_000_000          # bytes per record file
MAX_STORIES = 2000            # stories per snapshot
ISSUE_LIMIT = 60_000          # characters (GitHub's issue body limit is 65,536; the rest is headroom)
LABEL = "story-gate-dashboard"


def issue_url(repo, web="https://github.com"):
    """Where people find the dashboard: the repository's pinned issue with story-gate's label (one issue, kept up to date)."""
    return "%s/%s/issues?q=is%%3Aissue+label%%3A%s" % (web, repo, LABEL)
STALE_DAYS = 3
STORY_FILES = ("story.md", "context.md", "tests.json", "ready.json", "done.json", "checkpoints.jsonl", "coder.json", "decisions.jsonl",
               "trace.md", "test_results.json")
STATUSES = [  # order matters: this is the pipeline, left to right
    ("draft", "Draft", "READY not passed yet"),
    ("queued", "Queued", "READY passed, nobody has started it"),
    ("in_progress", "In progress", "claimed by an agent, DONE not passed"),
    ("blocked", "Blocked", "drift escalated, or the last checkpoint said OFF_COURSE"),
    ("in_review", "In review", "DONE passed on a branch, not merged yet"),
    ("done", "Done", "DONE passed on the default branch (merged)"),
]
PASSING = ("PASS",)


# ------------------------------------------------------------------ git access (blobs only)
class Repo:
    def __init__(self, root):
        self.root = str(root)

    def git(self, *args, text=True):
        r = subprocess.run(["git", *args], cwd=self.root, capture_output=True)
        if r.returncode != 0:
            return "" if text else b""
        return r.stdout.decode("utf-8", "replace") if text else r.stdout

    def refs(self, default_ref, include_local=False):
        out, seen = [], set()
        sha = self.git("rev-parse", "--verify", "-q", default_ref + "^{commit}").strip()
        if sha:
            out.append((default_ref, sha)); seen.add(sha)
        pats = ["refs/remotes/origin"] + (["refs/heads"] if include_local else [])
        for line in self.git("for-each-ref", "--format=%(refname:short) %(objectname)", *pats).splitlines():
            name, _, s = line.partition(" ")
            if not s or name.endswith("/HEAD") or name == default_ref:
                continue
            out.append((name, s))
        return out

    def blobs(self, specs):
        """{spec: bytes or None} for 'ref:path' specs, via one `git cat-file --batch` (no checkout, nothing executed)."""
        if not specs:
            return {}
        inp = ("\n".join(specs) + "\n").encode("utf-8")
        r = subprocess.run(["git", "cat-file", "--batch"], cwd=self.root, input=inp, capture_output=True)
        data, pos, out = r.stdout, 0, {}
        for spec in specs:
            nl = data.find(b"\n", pos)
            if nl < 0:
                break
            header = data[pos:nl].decode("utf-8", "replace").split()
            pos = nl + 1
            if len(header) == 3 and header[1] == "blob":
                size = int(header[2])
                out[spec] = data[pos:pos + size] if size <= MAX_BLOB else None
                pos += size + 1
            else:
                out[spec] = None
        return out

    def ls_stories(self, ref):
        names = self.git("ls-tree", "-r", "--name-only", "-z", ref, "--", ".story-gate/stories").split("\0")
        by = {}
        for n in names:
            parts = n.split("/")
            if len(parts) == 4 and parts[3] in STORY_FILES:
                by.setdefault(parts[2], []).append(parts[3])
        return by

    def changed_stories(self, base, ref):
        names = self.git("diff", "--name-only", "-z", "%s...%s" % (base, ref), "--", ".story-gate/stories").split("\0")
        return {n.split("/")[2] for n in names if n.count("/") >= 3}

    def last_dates(self, ref):
        """{story_id: latest commit time touching it} and {path: latest commit time} on ref."""
        out, files, cur = {}, {}, None
        log = self.git("log", "-n", "3000", "--format=@%cI", "--name-only", ref, "--", ".story-gate/stories")
        for line in log.splitlines():
            if line.startswith("@"):
                cur = line[1:]
            elif line.startswith(".story-gate/stories/") and cur:
                files.setdefault(line, cur)
                parts = line.split("/")
                if len(parts) >= 3:
                    out.setdefault(parts[2], cur)
        return out, files


# ------------------------------------------------------------------ validation
def clean(s, n=200):
    s = s if isinstance(s, str) else ("" if s is None else str(s))
    s = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", s)
    return s[:n]


def as_json(raw):
    if raw is None:
        return None
    try:
        v = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return None
    return v


def jsonl_rows(raw, limit=500):
    rows = []
    for line in (raw or b"").decode("utf-8", "replace").splitlines()[-limit:]:
        try:
            v = json.loads(line)
        except ValueError:
            continue
        if isinstance(v, dict):
            rows.append(v)
    return rows


def front(text):
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


def verdict(v):
    if not isinstance(v, dict) or v.get("overall") not in ("PASS", "CONCERNS", "FAIL", "ESCALATED", "WAIVED"):
        return None
    checks = v.get("checks") if isinstance(v.get("checks"), dict) else {}
    return {"overall": v["overall"], "drift": clean(v.get("drift"), 40), "judge": clean(v.get("judge"), 30), "inputs_hash": clean(v.get("inputs_hash"), 20),
            "inputs_hash_base": clean(v.get("inputs_hash_base"), 20),
            "at": clean(v.get("at"), 30), "cost": v.get("cost") if isinstance(v.get("cost"), (int, float)) and not isinstance(v.get("cost"), bool) else None,
            "waived": sum(1 for c in checks.values() if isinstance(c, dict) and c.get("status") == "WAIVED")}


def no_judge(v):
    """Label for a verdict reached in objective mode: it passed only the checks story-gate verifies itself."""
    return " (checked without a judge)" if (v or {}).get("judge") == "objective" else ""


def trace_acs(text):
    """[(ac_id, verified_bool or None)] from trace.md rows."""
    out = []
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 6 and re.fullmatch(r"AC-[\w.-]+", cells[0]):
            res = cells[5]
            if res in ("GREEN",) or (res and "=" in res and all(p.split("=")[-1] == "passed" for p in res.split(", "))):
                out.append((cells[0], True))
            elif res in ("—", "", "NOT RUN"):
                out.append((cells[0], None))
            else:
                out.append((cells[0], False))
    return out


def parse_story(sid, files):
    """files: {name: bytes or None}. Returns a validated, display-safe dict."""
    st = (files.get("story.md") or b"").decode("utf-8", "replace")
    fm = front(st)
    tests = as_json(files.get("tests.json"))
    acs = []
    if isinstance(tests, dict) and isinstance(tests.get("acceptance_criteria"), list):
        for a in tests["acceptance_criteria"][:200]:
            if isinstance(a, dict):
                acs.append(clean(a.get("id"), 30))
    cps = jsonl_rows(files.get("checkpoints.jsonl"))
    last_cp = cps[-1] if cps else {}
    coder = as_json(files.get("coder.json"))
    coder = coder if isinstance(coder, dict) else None
    decisions = jsonl_rows(files.get("decisions.jsonl"))
    tr = as_json(files.get("test_results.json"))
    title = clean(fm.get("title"), 120)
    feature = clean(fm.get("feature"), 40)
    return {
        "id": sid,
        "title": "" if title in ("", "TODO") else title,
        "feature": "" if feature.lower() in ("", "none", "todo") else feature,
        "acs": len(acs),
        "ready": verdict(as_json(files.get("ready.json"))),
        "done": verdict(as_json(files.get("done.json"))),
        "checkpoint": {"status": clean(last_cp.get("status"), 20), "percent": last_cp.get("percent") if isinstance(last_cp.get("percent"), int) else None,
                       "drift": clean(last_cp.get("drift"), 40), "at": clean(last_cp.get("at"), 30)} if last_cp else None,
        "coder": {"client": clean(coder.get("client"), 30), "model": clean(coder.get("model"), 60), "branch": clean(coder.get("branch"), 120),
                  "claimed_at": clean(coder.get("claimed_at") or coder.get("at"), 30)} if coder else None,
        "drift_decisions": [clean(d.get("drift"), 10) for d in decisions if d.get("kind") == "drift"][-5:],
        "trace": trace_acs((files.get("trace.md") or b"").decode("utf-8", "replace")),
        "tests_green": (tr.get("exit_code") == 0) if isinstance(tr, dict) and "exit_code" in tr else None,
        "spec_linked": bool(fm.get("spec")) and str(fm.get("spec")).strip().lower() not in ("none", "[]"),
        # issues copied in with spec-pull: CI's check on the pull request says whether each still matches the live issue
        "snapshots": snapshot_paths(fm.get("spec")),
    }


def snapshot_paths(spec):
    """The pulled-issue copies a story's `spec` names (a list or a comma/space separated string), normalised and in order."""
    items = spec if isinstance(spec, list) else re.split(r"[,\s]+", str(spec or ""))
    out = []
    for it in items:
        p = re.sub(r"/+", "/", str(it).strip().strip("'\"").replace("\\", "/")).lstrip("./") if str(it).strip() else ""
        p = ".story-gate/" + p[len("story-gate/"):] if p.startswith("story-gate/") else p
        if re.fullmatch(r"\.story-gate/stories/[A-Za-z0-9._-]+/(?:issue-[A-Za-z0-9_.-]*?[0-9]{1,9}|(?:linear|jira)-[A-Z][A-Z0-9]{0,9}-[0-9]{1,9})\.md", p) and p not in out:
            out.append(p)
    return out


# ------------------------------------------------------------------ build the snapshot
def ready_ok(s):
    """READY counts only when it passed outright (a waiver is not evidence) AND still matches the story it was scored on."""
    return (s.get("ready") or {}).get("overall") in PASSING and s.get("ready_fresh") is not False


def status_of(s, merged):
    r, d, cp = s.get("ready") or {}, s.get("done") or {}, s.get("checkpoint") or {}
    if merged and d.get("overall") in ("PASS", "WAIVED"):
        return "done"  # finished; WAIVED ones are flagged and never count as proven
    if r.get("overall") == "ESCALATED" or d.get("overall") == "ESCALATED" or cp.get("status") == "OFF_COURSE":
        return "blocked"
    if d.get("overall") in ("PASS", "WAIVED"):
        return "in_review"
    if s.get("coder"):
        return "in_progress"
    if ready_ok(s):
        return "queued"
    return "draft"


def iso_days(a, b):
    try:
        fa = datetime.fromisoformat(a.replace("Z", "+00:00")); fb = datetime.fromisoformat(b.replace("Z", "+00:00"))
        return (fb - fa).total_seconds() / 86400
    except Exception:
        return None


def build(root, default_ref, id_pattern, include_local=False, now=None, gate=None):
    now = now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    repo = Repo(root)
    refs = repo.refs(default_ref, include_local)
    omissions, stories, conflicts = [], {}, []
    if not refs:
        return {"schema": SCHEMA, "generated_at": now, "error": "default branch %s not found" % default_ref, "stories": [], "metrics": []}
    idre = re.compile(r"(?:%s)" % id_pattern)
    calib, learn = {}, {}
    features = {}
    for i, (ref, sha) in enumerate(refs):
        merged = i == 0
        listing = repo.ls_stories(ref)
        ids = set(listing) if merged else (repo.changed_stories(refs[0][0], ref) & set(listing))
        bad = [x for x in ids if not idre.fullmatch(x)]
        if bad:
            omissions.append("%s: ignored %d folder(s) whose names aren't story ids" % (ref, len(bad)))
        ids = sorted(x for x in ids if idre.fullmatch(x))
        specs = ["%s:.story-gate/stories/%s/%s" % (sha, sid, f) for sid in ids for f in listing.get(sid, [])]
        specs += ["%s:.story-gate/%s" % (sha, f) for f in ("calibration.jsonl", "learnings.jsonl", "features.json")]
        spec_names = []
        if gate is not None:  # PRD/TRD at this ref, to check READY verdicts against the specs they were scored on
            try:
                pc = gate.cfg()
                spec_names = sorted(set(list(pc.get("spec_files") or []) + [x[k] for x in pc.get("sources") or [] if x.get("type") == "repo" for k in ("prd", "trd") if x.get(k)]))
            except Exception:
                pc = None
            specs += ["%s:%s" % (sha, f) for f in spec_names]
        blobs = repo.blobs(specs)
        too_big = [s for s, b in blobs.items() if b is None and s.split(":", 1)[1].split("/")[-1] in STORY_FILES and s in specs]
        dates, file_dates = repo.last_dates(sha)
        for row in jsonl_rows(blobs.get("%s:.story-gate/calibration.jsonl" % sha), 20000):
            calib[json.dumps(row, sort_keys=True)] = row
        for row in jsonl_rows(blobs.get("%s:.story-gate/learnings.jsonl" % sha), 20000):
            learn[clean(row.get("id"), 60) or json.dumps(row, sort_keys=True)] = row
        fj = as_json(blobs.get("%s:.story-gate/features.json" % sha))
        if isinstance(fj, dict):
            for k, v in list(fj.items())[:500]:
                if isinstance(k, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}", k):
                    features.setdefault(k, clean((v or {}).get("title") if isinstance(v, dict) else "", 120) or k)
        for sid in ids:
            if len(stories) >= MAX_STORIES:
                omissions.append("more than %d stories: the rest are not shown" % MAX_STORIES); break
            files = {f: blobs.get("%s:.story-gate/stories/%s/%s" % (sha, sid, f)) for f in listing.get(sid, [])}
            s = parse_story(sid, files)
            # a story linking a spec: its full hash also covers that spec, which isn't read here, so compare the part that
            # doesn't (story, context, test plan, PRD/TRD, policy). A spec-only change is caught when CI scores READY again.
            key = "inputs_hash_base" if s.get("spec_linked") else "inputs_hash"
            if s.get("spec_linked") and not (s.get("ready") or {}).get(key):
                s["ready_fresh"] = None  # scored before this field existed: unknown
            elif gate is not None and pc is not None and s.get("ready") and s["ready"].get(key):
                txt = lambda b: (b or b"").decode("utf-8", "ignore")  # exactly how gate.py's rd() reads the same files
                pairs = [(f, txt(blobs.get("%s:%s" % (sha, f)))) for f in spec_names if blobs.get("%s:%s" % (sha, f)) is not None]
                try:
                    s["ready_fresh"] = s["ready"].get(key) == gate.ready_hash_from(
                        txt(files.get("story.md")), txt(files.get("context.md")), txt(files.get("tests.json")), pairs, pc)
                except Exception:
                    s["ready_fresh"] = None
            s["ref"] = ref
            s["merged"] = merged
            s["last_reported"] = dates.get(sid)
            s["done_at"] = file_dates.get(".story-gate/stories/%s/done.json" % sid) if merged else None
            s["status"] = status_of(s, merged)
            prev = stories.get(sid)
            if prev is None:
                stories[sid] = s
                continue
            if prev["merged"] and prev["status"] == "done":
                continue  # merged and done on the default branch: a stale branch can't reopen it
            if not prev["merged"] and prev.get("coder") and s.get("coder") and \
                    (prev["coder"].get("branch"), prev["coder"].get("client")) != (s["coder"].get("branch"), s["coder"].get("client")):
                conflicts.append({"story": sid, "branches": sorted({prev["ref"], ref})})
            if prev["merged"] or (s.get("last_reported") or "") > (prev.get("last_reported") or ""):
                stories[sid] = s
        if too_big:
            omissions.append("%s: %d record file(s) over %d bytes were skipped" % (ref, len(too_big), MAX_BLOB))
    rows = sorted(stories.values(), key=lambda s: ([k for k, _, _ in STATUSES].index(s["status"]), s["id"]))
    for s in rows:
        if s["feature"]:
            features.setdefault(s["feature"], s["feature"])
        lr = s.get("last_reported")
        s["stale"] = bool(s["status"] in ("in_progress", "blocked") and lr and (iso_days(lr, now) or 0) > STALE_DAYS)
    data = {"schema": SCHEMA, "generated_at": now, "default_ref": refs[0][0], "default_sha": refs[0][1],
            "refs_scanned": [{"ref": r, "sha": s[:12]} for r, s in refs], "omissions": omissions, "conflicts": conflicts,
            "features": [{"id": k, "title": v} for k, v in sorted(features.items())], "stories": rows}
    data["metrics"] = metrics(rows, list(calib.values()), list(learn.values()), conflicts, now)
    return data


def pct(a, b):
    return round(100.0 * a / b) if b else None


def metrics(rows, calib, learn, conflicts, now):
    by = {k: [s for s in rows if s["status"] == k] for k, _, _ in STATUSES}
    total = len(rows)
    dor = [s for s in rows if ready_ok(s)]
    active = by["in_progress"] + by["blocked"]
    finished = by["done"] + by["in_review"]
    ac_tot = ac_ok = ac_unknown = 0
    for s in finished:
        if (s.get("done") or {}).get("overall") == "WAIVED":
            ac_unknown += s["acs"]; continue  # a waiver is not test evidence
        if not s["trace"]:
            ac_unknown += s["acs"]
        for _, ok in s["trace"]:
            ac_tot += 1
            ac_ok += 1 if ok else 0
            ac_unknown += 1 if ok is None else 0
    est = [s["checkpoint"]["percent"] for s in active if s.get("checkpoint") and isinstance(s["checkpoint"].get("percent"), int)]
    scored = [r for r in calib if r.get("phase") in ("ready", "done") and r.get("overall")]
    scored.sort(key=lambda r: str(r.get("at")))
    first_ready, catches, done_runs = {}, 0, {}
    done_ids = {s["id"] for s in finished}
    for r in scored:
        sid = r.get("story")
        if r["phase"] == "ready":
            first_ready.setdefault(sid, r["overall"])
        if sid in done_ids and r["overall"] in ("FAIL", "CONCERNS", "ESCALATED"):
            catches += 1
        if r["phase"] == "done":
            done_runs[sid] = done_runs.get(sid, 0) + 1
    lead = [d for d in (iso_days(s["coder"]["claimed_at"], s["done_at"]) for s in by["done"] if s.get("coder") and s.get("done_at")) if d is not None and d >= 0]
    drifting = [s for s in active + by["queued"] if any(x and x != "none" for x in ((s.get("ready") or {}).get("drift"), (s.get("checkpoint") or {}).get("drift")))]
    cost = sum((s.get(p) or {}).get("cost") or 0 for s in rows for p in ("ready", "done"))
    M = []
    def add(key, label, value, formula, unit="", estimate=False, good=None):
        M.append({"key": key, "label": label, "value": value, "unit": unit, "formula": formula, "estimate": estimate, "good": good})
    add("features", "Features", len({s["feature"] for s in rows if s["feature"]} | set()), "distinct feature ids on stories")
    add("stories", "Stories", total, "story folders on the default branch plus stories changed on other branches")
    add("dor_met", "Meet Definition of Ready", len(dor), "stories whose READY verdict is PASS and still matches the story, tests and specs it was scored on (waivers don't count)")
    add("ready_stale", "READY out of date", sum(1 for s in rows if s.get("ready_fresh") is False), "READY verdicts recorded on a story, test plan, spec or policy that has since changed", good="low")
    add("waived", "Finished with waivers", sum(1 for s in finished if (s.get("done") or {}).get("overall") == "WAIVED" or (s.get("done") or {}).get("waived")), "finished stories whose DONE relied on a human waiver", good="low")
    add("dor_pct", "Ready rate", pct(len(dor), total), "Meet Definition of Ready / Stories", "%")
    add("queued", "Queued to start", len(by["queued"]), "READY passed and nobody has claimed it")
    add("in_progress", "In progress", len(by["in_progress"]), "claimed by an agent, DONE not passed")
    add("blocked", "Blocked", len(by["blocked"]), "drift escalated, or the last checkpoint was OFF_COURSE", good="low")
    add("agents", "Agents working", len({(s["coder"].get("client"), s["coder"].get("model")) for s in active if s.get("coder")}), "distinct client + model on claimed, unfinished stories")
    add("drifting", "Stories drifting", len(drifting), "unfinished stories whose READY or last checkpoint reports drift", good="low")
    add("done", "Done (merged)", len(by["done"]), "DONE passed on the default branch")
    add("in_review", "In review", len(by["in_review"]), "DONE passed on a branch, not merged yet")
    add("ac_verified", "Acceptance criteria proven", pct(ac_ok, ac_tot), "criteria whose mapped tests all passed / criteria traced, finished stories only (%d not traced)" % ac_unknown, "%")
    add("ac_progress", "Progress on active stories", round(statistics.mean(est)) if est else None, "average of the last checkpoint's % complete", "%", estimate=True)
    add("gate_catches", "Gate catches before merge", catches, "re-scores of finished stories that came back FAIL, CONCERNS or ESCALATED before passing")
    add("defects", "Defects recorded", sum(1 for r in learn if r.get("type") == "error"), "learnings of type error (one per confirmed defect)")
    add("first_try", "Ready on first try", pct(sum(1 for v in first_ready.values() if v == "PASS"), len(first_ready)), "stories whose first READY score passed / stories scored", "%")
    add("rework", "DONE attempts per story", round(statistics.mean(done_runs[s] for s in done_ids if s in done_runs), 1) if any(s in done_runs for s in done_ids) else None, "average DONE scores until it passed, finished stories")
    add("lead_time", "Days from claim to merge", round(statistics.median(lead), 1) if lead else None, "median, merged stories", "days")
    add("false_alarms", "Verdicts marked wrong", sum(1 for r in calib if r.get("label") == "wrong"), "gate.py label ... wrong (a human said the gate got it wrong)", good="low")
    add("stale", "Stale claims", sum(1 for s in rows if s.get("stale")), "active stories with no new record for %d days" % STALE_DAYS, good="low")
    add("conflicts", "Ownership conflicts", len(conflicts), "the same story claimed on two branches by different agents", good="low")
    add("judge_cost", "Judge cost (latest verdicts)", round(cost, 4), "sum of the cost reported on each story's latest READY and DONE verdicts", "$")
    return M


# ------------------------------------------------------------------ Markdown (the pinned issue)
def md(s, n=80):
    s = clean(s, n).replace("\n", " ")
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    s = re.sub(r"([\\`*_\[\]|#!~])", r"\\\1", s)
    return s.replace("@", "&#64;")


def fmt(m):
    v = m["value"]
    if v is None:
        return "—"
    return ("$%s" % v if m["unit"] == "$" else "%s%s" % (v, "%" if m["unit"] == "%" else (" " + m["unit"] if m["unit"] else ""))) + (" (estimate)" if m["estimate"] else "")


def ci_cell(s):
    ci = s.get("ci")
    if not ci:
        return "no PR"
    return "#%s %s%s" % (ci.get("pr"), md(ci.get("result"), 20), " · source %s" % md(ci["source"], 12) if ci.get("source") else "")


def mm(s, n=30):
    """A label safe inside a Mermaid "..." string: letters, digits, spaces and . , : / # + - only. Quotes, brackets, backticks,
    ">", "%", ";", "<" and line breaks can't get through, so a label can't close its string, start an arrow or add HTML."""
    return re.sub(r"-{2,}", "-", re.sub(r"\s+", " ", re.sub(r"[^\w .,:/#+-]", " ", clean(s, n)))).strip() or "?"


def next_refresh(iso):
    """When the schedule (minutes 11 and 41 of every hour) next fires after `iso`, as 'YYYY-MM-DD HH:MM'; None if `iso` can't be read."""
    try:
        t = datetime.strptime(str(iso), "%Y-%m-%dT%H:%M:%SZ").replace(second=0)
    except ValueError:
        return None
    t += timedelta(minutes=1)
    while t.minute not in (11, 41):
        t += timedelta(minutes=1)
    return t.strftime("%Y-%m-%d %H:%M")


def to_markdown(d, artifact_url=None, limit=ISSUE_LIMIT):
    M = {m["key"]: m for m in d.get("metrics", [])}
    nxt = next_refresh(d.get("generated_at"))
    head = ["<!-- story-gate-dashboard generated_at=%s schema=%s -->" % (d.get("generated_at"), d.get("schema")),
            "# Story-gate dashboard", "",
            "Updated **%s (UTC)** from `%s` at `%s` and %d other branch(es)." % (md(d.get("generated_at"), 30), md(d.get("default_ref"), 60),
                                                                              md((d.get("default_sha") or "")[:12], 12), max(0, len(d.get("refs_scanned", [])) - 1))]
    if nxt:
        head.append("Next refresh: by **%s (UTC)**. It refreshes after merges and pull request checks, and at least every 30 minutes." % nxt)
    head.append("Live view on your own computer: `story-gate dashboard --serve`")
    if artifact_url:
        head.append("Full report: [download the HTML dashboard](%s) (repository members only; kept 30 days, a new one comes with every refresh)." % artifact_url)
    head += ["", "| | |", "|---|---|"]
    for k in ("features", "stories", "dor_met", "queued", "in_progress", "blocked", "in_review", "done", "ac_verified", "gate_catches", "defects"):
        if k in M:
            head.append("| %s | **%s** |" % (M[k]["label"], fmt(M[k])))
    counts = [(lab, len([s for s in d["stories"] if s["status"] == k])) for k, lab, _ in STATUSES]
    stage = dict((k, l) for k, l, _ in STATUSES)
    # the tables below are the data; the charts are a picture of the same counts (labels pass through mm())
    chart = ["", "```mermaid", "pie showData", "    title Stories by status"] + ['    "%s" : %d' % (mm(lab), n) for lab, n in counts if n] + ["```"] if any(n for _, n in counts) else []
    chart += ["", "```mermaid", "flowchart LR", "    " + " --> ".join('s%d["%s: %d"]' % (i, mm(lab), n) for i, (lab, n) in enumerate(counts)), "```"]
    act = [s for s in d["stories"] if s["status"] in ("in_progress", "blocked", "in_review")]
    arows = ["| %s %s | %s | %s | %s%s | %s | %s | %s | %s%s |" % (
        md(s["id"], 40), md(s["title"], 50), md((s.get("coder") or {}).get("client") or "?", 20), md((s.get("coder") or {}).get("model") or "?", 30), stage[s["status"]],
        " (%s)" % md((s.get("checkpoint") or {}).get("status"), 12) if (s.get("checkpoint") or {}).get("status") else "",
        "%s%% (estimate)" % s["checkpoint"]["percent"] if isinstance((s.get("checkpoint") or {}).get("percent"), int) else "—",
        md((s.get("checkpoint") or {}).get("drift") or (s.get("ready") or {}).get("drift") or "none", 25), ci_cell(s), md((s.get("last_reported") or "—")[:16], 16),
        " ⚠ stale" if s.get("stale") else "") for s in act] or ["| — | | | | | | | |"]
    qrows = ["- %s %s%s" % (md(s["id"], 40), md(s["title"], 70), " · feature %s" % md(s["feature"], 30) if s["feature"] else "")
             for s in d["stories"] if s["status"] == "queued"] or ["- none"]
    frows = []
    for f in d.get("features", []):
        ss = [s for s in d["stories"] if s["feature"] == f["id"]]
        tr = [ok for s in ss if s["status"] in ("done", "in_review") for _, ok in s["trace"]]
        frows.append("| %s %s | %d | %d | %d | %s |" % (md(f["id"], 40), md(f["title"], 60) if f["title"] != f["id"] else "", len(ss),
                                                      sum(s["status"] == "done" for s in ss), sum((s.get("ready") or {}).get("overall") == "PASS" for s in ss),
                                                      "%d%%" % pct(sum(1 for x in tr if x), len(tr)) if tr else "—"))
    defs = ["", "<details><summary>How each number is calculated</summary>", ""] + \
           ["- **%s**: %s." % (m["label"], md(m["formula"], 200)) for m in d.get("metrics", [])] + \
           ["", "Verdicts are recorded by the coding agents. The `story-gate` check in CI re-checks them on every pull request.", "</details>"]
    notes = []
    if any((s.get("ci") or {}).get("source") for s in d["stories"]):
        notes += ["", SOURCE_LEGEND + "."]
    if d.get("conflicts"):
        notes += ["", "**Ownership conflicts:** " + "; ".join("%s on %s" % (md(c["story"], 40), ", ".join(md(b, 60) for b in c["branches"])) for c in d["conflicts"][:20])]
    if d.get("omissions"):
        notes += ["", "**Not shown:** " + "; ".join(md(o, 160) for o in d["omissions"][:10])]
    srows = ["| %s %s | %s | %s | %s | %s | %s |" % (md(s["id"], 40), md(s["title"], 50), md(s["feature"] or "—", 30), stage[s["status"]],
                                                   (s.get("ready") or {}).get("overall", "—") + no_judge(s.get("ready")) + (" (out of date)" if s.get("ready_fresh") is False else ""),
                                                   (s.get("done") or {}).get("overall", "—") + no_judge(s.get("done")), md(s["ref"], 60) + (" · " + ci_cell(s) if s.get("ci") else ""))
             for s in d["stories"]]
    fold = lambda title, summary: ["", "## " + title, "", "<details><summary>%s</summary>" % summary, ""]
    footer = ["", "_Managed by story-gate. Edits to this issue are overwritten._"]
    table_chart = ["", "| Stage | Stories |", "|---|---|"] + ["| %s | %d |" % (lab, n) for lab, n in counts]
    # Fixed order on the page; when space runs out, sections are kept by priority (deterministic):
    # summary > chart (small; falls back to a table) > features > who is working > queued > notes > definitions > all stories.
    # Each part is (lines before the rows, rows, lines after); a long list keeps as many rows as fit (at most half of what is left,
    # so the lists after it still get room) and says how many it left out.
    order = ["head", "chart", "agents", "queued", "feats", "notes", "defs", "stories"]
    parts = {"head": (head, [], []), "chart": (chart, [], []),
             "agents": (["", "## Who is working on what", "", "| Story | Agent | Model | Status | Done | Drift | CI check | Last report |", "|---|---|---|---|---|---|---|---|"], arows, []),
             "queued": (fold("Queued to start", "%d queued" % len([s for s in d["stories"] if s["status"] == "queued"])), qrows, ["", "</details>"]),
             "feats": (fold("Features", "%d features" % len(frows)) + ["| Feature | Stories | Done | Ready | Acceptance criteria proven |", "|---|---|---|---|---|"], frows, ["", "</details>"]),
             "notes": (notes, [], []), "defs": (defs, [], []),
             "stories": (fold("All stories", "%d stories" % len(srows)) + ["| Story | Feature | Stage | READY | DONE | Branch |", "|---|---|---|---|---|---|"], srows, ["", "</details>"])}
    priority = ["head", "chart", "feats", "agents", "queued", "notes", "defs", "stories"]
    lists = ("stories", "agents", "feats", "queued")
    full = "[see the full report](%s)" % artifact_url if artifact_url else "see the full report"
    more = lambda n: "… %d more: %s" % (n, full)
    size = lambda ls: sum(len(x) + 1 for x in ls)
    budget = limit - len("\n".join(footer)) - 300
    kept, used, short = {}, 0, []
    for name in priority:
        pre, rows, post = parts[name]
        if used + size(pre + rows + post) <= budget:
            kept[name] = pre + rows + post
        elif name == "chart" and used + size(table_chart) <= budget:
            kept[name] = table_chart
        else:
            room = (budget - used) // 2 - size(pre) - size(post) - 200  # 200: the "more" line
            if name not in lists or room < 0:
                short.append(name)
                continue
            k = 0
            while k < len(rows) and room >= len(rows[k]) + 1:
                room -= len(rows[k]) + 1; k += 1
            kept[name] = pre + rows[:k] + ["", more(len(rows) - k)] + post
        used += size(kept[name])
    out = sum((kept[n] for n in order if n in kept), [])
    if short:
        out += ["", "Left out to stay under GitHub's size limit: %s (%s)." % (", ".join(short), full)]
    return "\n".join(out) + "\n" + "\n".join(footer)


# ------------------------------------------------------------------ HTML (the full report)
def h(s, n=200):
    return html.escape(clean(s, n), quote=True)


def bar_chart(counts):
    """Horizontal bars, one hue (magnitude), blocked in the critical colour with a label, value labels at the bar end."""
    maxv = max([n for _, _, n, _ in counts] + [1])
    rowh, w, lab = 34, 420, 96
    out = ['<svg class="sg-chart" viewBox="0 0 %d %d" role="img" aria-label="Stories by stage">' % (w, rowh * len(counts) + 8)]
    for i, (key, label, n, hint) in enumerate(counts):
        y = 4 + i * rowh
        bw = 0 if not n else max(6, (w - lab - 40) * n / maxv)
        cls = "sg-bar-critical" if key == "blocked" else "sg-bar"
        out.append('<g><title>%s: %d (%s)</title><text x="0" y="%d" class="sg-axis">%s</text>'
                   '<rect x="%d" y="%d" width="%.1f" height="%d" rx="4" class="%s"></rect>'
                   '<text x="%.1f" y="%d" class="sg-value">%d</text></g>'
                   % (h(label), n, h(hint), y + 21, h(label), lab, y + 6, bw, rowh - 12, cls, lab + bw + 8, y + 21, n))
    out.append("</svg>")
    return "".join(out)


WORDMARK_FILE = Path(__file__).resolve().parent / "vendor" / "viaknox-wordmark.svg"
TABLER_FILE = Path(__file__).resolve().parent / "vendor" / "tabler.min.css"

BRAND_CSS = """
:root{--sg-accent:#FFD84D;--sg-accent-text:#8A6A00;--sg-graphite:#1F2327;--sg-paper:#FFF8F2;--sg-ink:#1F2327;--sg-muted:#5B6168;--sg-surface:#FFF8F2;--sg-card:#FFFFFF;
--sg-line:#E4E1DB;--sg-critical:#B42318;--sg-wordmark:#29122B;--tblr-primary:#1F2327;--tblr-body-bg:#FFF8F2;--tblr-body-color:#1F2327}
@media (prefers-color-scheme: dark){:root{--sg-ink:#F1F5F2;--sg-muted:#B9C0C6;--sg-surface:#16191C;--sg-card:#1F2327;--sg-line:#3A4046;--sg-accent-text:#FFD84D;
--sg-critical:#FF8A80;--sg-wordmark:#FFF8F2;--tblr-primary:#FFD84D;--tblr-body-bg:#16191C;--tblr-body-color:#F1F5F2}}
body{background:var(--sg-surface);color:var(--sg-ink)}
h1,h2,h3,.h1,.card-title,strong{color:var(--sg-ink)} .card-header{border-color:var(--sg-line)}
.table{--tblr-table-bg:transparent;--tblr-table-color:var(--sg-ink);--tblr-table-border-color:var(--sg-line)}
.table thead th{background:transparent;color:var(--sg-muted);border-color:var(--sg-line)}
.alert{background:var(--sg-card);color:var(--sg-ink);border:1px solid var(--sg-line);border-left:4px solid var(--sg-accent)}
.card{background:var(--sg-card);border-color:var(--sg-line)} .table{color:var(--sg-ink)} .text-secondary{color:var(--sg-muted)!important}
.sg-kpi .h1{font-weight:700;letter-spacing:-.01em;margin:0} .sg-kpi .subheader{color:var(--sg-muted)}
.sg-chart{width:100%;height:auto} .sg-bar{fill:var(--sg-accent);stroke:var(--sg-graphite);stroke-width:1.5} .sg-bar-critical{fill:var(--sg-critical)}
.sg-axis,.sg-value{fill:var(--sg-ink);font-size:14px} .sg-value{font-weight:600}
.sg-pill{display:inline-block;padding:.1rem .5rem;border-radius:999px;border:1px solid var(--sg-line);font-size:.8rem;white-space:nowrap}
.sg-pill.pass{border-color:#2E7D4F} .sg-pill.fail{border-color:var(--sg-critical)} .sg-est{color:var(--sg-muted);font-size:.8rem}
[hidden]{display:none!important} #sg-stale{background:#FFF3C4;color:#1F2327;border:1px solid #E0B400;border-radius:6px;padding:.6rem 1rem}
@media (prefers-color-scheme: dark){#sg-stale{background:#4A3B00;color:#F1F5F2;border-color:#B38F00}}
.sg-brand{color:var(--sg-ink);display:inline-flex;align-items:center;gap:.4rem} .sg-brand svg{height:14px;width:auto;color:var(--sg-wordmark)}
header.sg-head{border-bottom:3px solid var(--sg-accent)}
@media (max-width:640px){.container-xl{padding-left:16px;padding-right:16px}}
"""


def pill(v, verdict=None):
    if not v:
        return '<span class="sg-pill">—</span>'
    cls = "pass" if v in ("PASS",) else ("fail" if v in ("FAIL", "ESCALATED") else "")
    icon = {"PASS": "✓ ", "FAIL": "✕ ", "ESCALATED": "! ", "CONCERNS": "~ ", "WAIVED": "w "}.get(v, "")
    return '<span class="sg-pill %s">%s%s</span>%s' % (cls, icon, h(v, 12), '<span class="sg-est">%s</span>' % no_judge(verdict) if no_judge(verdict) else "")


def pill_level(text):
    kind = "pass" if text.startswith("hard:") else ("fail" if text == "none" else "warn")  # hard-on-change is not a hard stop
    return '<span class="sg-pill %s">%s</span>' % (kind, h(text, 120))


# Shown only on a saved copy (artifact download or --out file) that is over an hour old; a page served by --serve is fresh and leaves it out.
STALE_JS = ('(function(){var b=document.getElementById("sg-stale"),m=(Date.now()-Date.parse(b.getAttribute("data-generated")))/60000;if(!(m>60))return;'
            'var n=m<2880?Math.round(m/60):Math.round(m/1440),u=m<2880?"hour":"day";b.querySelector("b").textContent=n+" "+u+(n==1?"":"s");b.hidden=false})()')


def to_html(d, artifact_note="", live=False, issue=None):
    M = d.get("metrics", [])
    label = dict((k, l) for k, l, _ in STATUSES)
    counts = [(k, l, len([s for s in d["stories"] if s["status"] == k]), hint) for k, l, hint in STATUSES]
    css = TABLER_FILE.read_text(encoding="utf-8") if TABLER_FILE.is_file() else ""
    mark = WORDMARK_FILE.read_text(encoding="utf-8") if WORDMARK_FILE.is_file() else ""
    mark = re.sub(r'fill="#[0-9A-Fa-f]{6}"', 'fill="currentColor"', re.sub(r"<title>.*?</title>", "", mark)) if mark else ""
    kpi = "".join('<div class="col-6 col-md-4 col-xl-2"><div class="card sg-kpi"><div class="card-body"><div class="subheader" title="%s">%s</div>'
                  '<div class="h1">%s</div>%s</div></div></div>' % (h(m["formula"], 300), h(m["label"]), h(fmt(m).replace(" (estimate)", ""), 40),
                                                                     '<div class="sg-est">estimate</div>' if m["estimate"] else "")
                  for m in M if m["key"] in ("stories", "dor_met", "queued", "in_progress", "done", "ac_verified"))
    quality = "".join('<tr><td>%s%s</td><td class="text-end"><strong>%s</strong></td><td class="text-secondary">%s</td></tr>'
                      % (h(m["label"]), ' <span class="sg-est">(estimate)</span>' if m["estimate"] else "", h(fmt(m).replace(" (estimate)", ""), 40), h(m["formula"], 300)) for m in M)
    agents = "".join('<tr><td><strong>%s</strong><div class="text-secondary">%s</div></td><td>%s</td><td>%s</td><td>%s%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s%s</td></tr>' % (
        h(s["id"], 40), h(s["title"], 80), h((s.get("coder") or {}).get("client") or "?", 30), h((s.get("coder") or {}).get("model") or "?", 60), label[s["status"]],
        " · " + h((s.get("checkpoint") or {}).get("status"), 20) if (s.get("checkpoint") or {}).get("status") else "",
        ("%d%% <span class=\"sg-est\">estimate</span>" % s["checkpoint"]["percent"]) if isinstance((s.get("checkpoint") or {}).get("percent"), int) else "—",
        h((s.get("checkpoint") or {}).get("drift") or (s.get("ready") or {}).get("drift") or "none", 30),
        h(("PR #%s · %s%s" % (s["ci"].get("pr"), s["ci"].get("result"), " · source " + s["ci"]["source"] if s["ci"].get("source") else ""))
          if s.get("ci") else "no PR", 60), h((s.get("last_reported") or "—")[:16], 16),
        ' <span class="sg-pill fail">stale</span>' if s.get("stale") else "") for s in d["stories"] if s["status"] in ("in_progress", "blocked", "in_review")) \
        or '<tr><td colspan="8" class="text-secondary">No agent is working on a story right now.</td></tr>'
    feats = "".join('<tr><td><strong>%s</strong> <span class="text-secondary">%s</span></td><td class="text-end">%d</td><td class="text-end">%d</td><td class="text-end">%d</td></tr>' % (
        h(f["id"], 40), h(f["title"], 80) if f["title"] != f["id"] else "", len([s for s in d["stories"] if s["feature"] == f["id"]]),
        len([s for s in d["stories"] if s["feature"] == f["id"] and (s.get("ready") or {}).get("overall") == "PASS"]),
        len([s for s in d["stories"] if s["feature"] == f["id"] and s["status"] == "done"])) for f in d.get("features", [])) \
        or '<tr><td colspan="4" class="text-secondary">No features yet. Add one with gate.py feature &lt;ID&gt; --title "…"</td></tr>'
    rows = "".join('<tr><td><strong>%s</strong><div class="text-secondary">%s</div></td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td class="text-secondary">%s</td></tr>' % (
        h(s["id"], 40), h(s["title"], 100), h(s["feature"] or "—", 40), label[s["status"]], pill((s.get("ready") or {}).get("overall"), s.get("ready")),
        pill((s.get("done") or {}).get("overall"), s.get("done")), ("%d/%d" % (sum(1 for _, ok in s["trace"] if ok), len(s["trace"]))) if s["trace"] else "—", h(s["ref"], 80) + (h(" · " + ci_cell(s), 80) if s.get("ci") else "")) for s in d["stories"]) \
        or '<tr><td colspan="8" class="text-secondary">No stories yet. Plan one with gate.py plan &lt;ID&gt; --title "…"</td></tr>'
    notes = ""
    if any((s.get("ci") or {}).get("source") for s in d["stories"]):
        notes += '<div class="alert alert-info">%s</div>' % h(SOURCE_LEGEND, 400)
    if d.get("conflicts"):
        notes += '<div class="alert alert-warning">Ownership conflicts: %s</div>' % h("; ".join("%s on %s" % (c["story"], ", ".join(c["branches"])) for c in d["conflicts"]), 1000)
    if d.get("omissions"):
        notes += '<div class="alert alert-info">Not shown: %s</div>' % h("; ".join(d["omissions"]), 1000)
    if d.get("computer"):  # local runs only: how well this computer is protected from hooks shipped inside branches
        notes += ('<div class="card mb-3"><div class="card-header"><h3 class="card-title">This computer: hooks shipped inside branches</h3></div>'
                  '<div class="table-responsive"><table class="table card-table"><thead><tr><th>AI tool</th><th>Protection</th><th>Next step</th></tr></thead><tbody>%s'
                  '</tbody></table></div><div class="card-body text-secondary small">Hard = the tool itself refuses repository hooks. Hard-on-change = the tool asks you again whenever a hook changes. Partial = '
                  'story-gate\'s checkout filter removes unapproved hook commands in the repositories it covers. Prove it: gate.py doctor --prove</div></div>'
                  % "".join('<tr><td>%s</td><td>%s</td><td class="text-secondary">%s</td></tr>' % (h(r["tool"], 20), pill_level(r["protection"]), h(r["next"], 200))
                            for r in d["computer"]))
    stale = "" if live else (
        '<div class="container-xl pt-3"><div id="sg-stale" role="status" data-generated="%s" hidden>This is a snapshot from <b></b> ago. Live view: the %s or <code>story-gate dashboard --serve</code>. '
        'Anyone you sent this file to keeps this copy.</div></div><script>%s</script>'
        % (h(d.get("generated_at"), 30), ('<a href="%s">pinned \'Story-gate dashboard\' issue</a>' % h(issue, 300)) if issue else "pinned 'Story-gate dashboard' issue", STALE_JS))
    script_src = "" if live else "; script-src 'sha256-%s'" % base64.b64encode(hashlib.sha256(STALE_JS.encode("utf-8")).digest()).decode()
    return """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:%s">
<title>Story-gate dashboard</title><style>%s</style><style>%s</style></head><body>%s
<header class="sg-head py-3 mb-3"><div class="container-xl d-flex flex-wrap justify-content-between align-items-center gap-2">
<div><h1 class="m-0">Story-gate dashboard</h1><div class="text-secondary">Updated %s · %s at %s · %d branches scanned%s</div></div>
</div></header>
<main class="container-xl">%s
<div class="row row-cards mb-3">%s</div>
<div class="row row-cards">
<div class="col-lg-5"><div class="card"><div class="card-header"><h3 class="card-title">Pipeline</h3></div><div class="card-body">%s
<p class="text-secondary small mt-2 mb-0">Each bar counts stories at that stage. Hover a bar for what the stage means.</p></div></div></div>
<div class="col-lg-7"><div class="card"><div class="card-header"><h3 class="card-title">Who is working on what</h3></div><div class="table-responsive">
<table class="table table-vcenter card-table"><thead><tr><th>Story</th><th>Agent</th><th>Model</th><th>Stage</th><th>Done</th><th>Drift</th><th>CI check</th><th>Last report</th></tr></thead><tbody>%s</tbody></table></div></div></div>
<div class="col-lg-5"><div class="card"><div class="card-header"><h3 class="card-title">Features</h3></div><div class="table-responsive">
<table class="table card-table"><thead><tr><th>Feature</th><th class="text-end">Stories</th><th class="text-end">Ready</th><th class="text-end">Done</th></tr></thead><tbody>%s</tbody></table></div></div></div>
<div class="col-lg-7"><div class="card"><div class="card-header"><h3 class="card-title">Quality and flow</h3></div><div class="table-responsive">
<table class="table card-table"><thead><tr><th>Measure</th><th class="text-end">Value</th><th>How it's calculated</th></tr></thead><tbody>%s</tbody></table></div></div></div>
<div class="col-12"><div class="card"><div class="card-header"><h3 class="card-title">All stories</h3></div><div class="table-responsive">
<table class="table table-vcenter card-table"><thead><tr><th>Story</th><th>Feature</th><th>Stage</th><th>READY</th><th>DONE</th><th>Criteria proven</th><th>Branch</th></tr></thead><tbody>%s</tbody></table></div></div></div>
</div>
<p class="text-secondary small my-3">Verdicts are recorded by the coding agents; the story-gate check in CI re-checks them on every pull request. Estimates are marked. %s</p>
</main>
<footer class="container-xl py-3"><span class="sg-brand text-secondary">story-gate · by <span aria-label="Viaknox">%s</span></span></footer>
</body></html>""" % (script_src, css, BRAND_CSS, stale, h(d.get("generated_at"), 30), h(d.get("default_ref"), 60), h((d.get("default_sha") or "")[:12], 12),
                      len(d.get("refs_scanned", [])), (" · " + h(artifact_note, 200)) if artifact_note else "", notes, kpi, bar_chart(counts), agents, feats,
                      quality, rows, h("Snapshot of: " + ", ".join(r["ref"] for r in d.get("refs_scanned", [])[:30]), 2000), mark)


# ------------------------------------------------------------------ publishing (CI)
def call(G, method, path, token, body=None, tries=3):
    """GitHub API call that backs off on rate limits (403/429 with Retry-After or a reset time)."""
    for i in range(tries):
        st, data, hdr = G.call(method, path, token, body)
        if st not in (403, 429) or i == tries - 1:
            return st, data, hdr
        wait = (hdr or {}).get("Retry-After") or 0
        try:
            wait = int(wait) or max(1, int((hdr or {}).get("X-RateLimit-Reset", 0)) - int(time.time()))
        except (TypeError, ValueError):
            wait = 5
        time.sleep(min(60, max(1, wait)))
    return st, data, hdr


def ci_status(G, repo, token, data):
    """Mark each unmerged story with the result of the story-gate check on its open pull request (CI-verified or not)."""
    # the check this installation runs: 'story-gate' (copied workflow) or 'story-gate / story-gate' (reusable workflow)
    name = G.ci_check_name(os.environ.get("STORY_GATE_ROOT") or os.getcwd())
    prs, page = [], 1
    while page <= 5:  # up to 500 open pull requests
        st, chunk, _ = call(G, "GET", "/repos/%s/pulls?state=open&per_page=100&page=%d" % (repo, page), token)
        if st != 200 or not isinstance(chunk, list):
            data["omissions"].append("could not read open pull requests (HTTP %s), so some CI results aren't shown" % st)
            break
        prs += chunk
        if len(chunk) < 100:
            break
        page += 1
    own = [p for p in prs if (((p.get("head") or {}).get("repo") or {}).get("full_name") or "").lower() == repo.lower()]  # a fork's branch can share a name
    heads = {"origin/" + (p.get("head") or {}).get("ref", ""): ((p.get("head") or {}).get("sha"), p.get("number")) for p in own}
    for s in data["stories"]:
        if s["merged"] or s["ref"] not in heads:
            continue
        sha, num = heads[s["ref"]]
        st, runs, _ = call(G, "GET", "/repos/%s/commits/%s/check-runs?check_name=%s" % (repo, sha, urllib.parse.quote(name, safe="")), token)
        runs = (runs or {}).get("check_runs") if isinstance(runs, dict) else None
        if st == 200 and runs:
            r = runs[0]
            s["ci"] = {"pr": num, "result": clean(r.get("conclusion") or r.get("status"), 20)}
            if s.get("snapshots"):
                s["ci"]["source"] = source_state(G, repo, token, r, s["snapshots"])
        else:
            s["ci"] = {"pr": num, "result": "not run"}
            if s.get("snapshots"):
                s["ci"]["source"] = "not checked"


SOURCE_TITLE = re.compile(r"^story-gate: source (verified|changed|not checked)$")


def source_state(G, repo, token, run, expected):
    """'verified' only when the story-gate check on this exact commit has finished and left a 'source verified' note
    on each of this story's pulled copies (expected: their paths) and nothing worse; 'changed' if any differs; otherwise 'not checked'. Annotations are written
    by CI (default-branch code), never by the pull request."""
    if run.get("status") != "completed" or not run.get("id"):
        return "not checked"
    st, notes, _ = call(G, "GET", "/repos/%s/check-runs/%s/annotations?per_page=100" % (repo, int(run["id"])), token)
    if st != 200 or not isinstance(notes, list):
        return "not checked"
    seen = {}  # copy path -> words CI wrote for it; only this story's own copies count
    for n in notes:
        m = SOURCE_TITLE.match(str(n.get("title") or "")) if isinstance(n, dict) else None
        if m and n.get("path") in expected:
            seen.setdefault(n["path"], set()).add(m.group(1))
    words = set().union(*seen.values()) if seen else set()
    if "changed" in words:
        return "changed"
    if "not checked" in words or any("verified" not in seen.get(p, ()) for p in expected):
        return "not checked"
    return "verified"


SOURCE_LEGEND = ("Source (stories with issues copied in by spec-pull): verified = CI compared each copy with the live issue on "
                 "the pull request's latest commit and they match; changed = they differ; not checked = no comparison yet")

def publish_issue(G, repo, token, body, generated_at, issue_number=None):
    """Create or update the single dashboard issue. Never overwrites a newer snapshot. Returns a plain-English result.
    The issue is found by `dashboard_issue` in config (if a human set it), else by the story-gate-dashboard label."""
    st, _, _ = call(G, "GET", "/repos/%s/labels/%s" % (repo, LABEL), token)
    if st == 404:
        call(G, "POST", "/repos/%s/labels" % repo, token, {"name": LABEL, "color": "FFD84D", "description": "Managed by story-gate"})
    st, issues, _ = call(G, "GET", "/repos/%s/issues?labels=%s&state=all&per_page=20&sort=created&direction=asc" % (repo, LABEL), token)
    if st == 410:
        return "Issues are turned off in this repository, so the dashboard is only in the workflow summary and the report artifact."
    if st != 200 or not isinstance(issues, list):
        return "Could not read issues (HTTP %s); the dashboard is in the workflow summary." % st
    issues = [i for i in issues if not i.get("pull_request")]
    if issue_number:
        st, one, _ = call(G, "GET", "/repos/%s/issues/%s" % (repo, int(issue_number)), token)
        if st == 200 and isinstance(one, dict) and not one.get("pull_request"):
            issues = [one]
    if not issues:
        st, made, _ = call(G, "POST", "/repos/%s/issues" % repo, token, {"title": "Story-gate dashboard", "body": body, "labels": [LABEL]})
        if st not in (200, 201):
            return "Could not create the dashboard issue (HTTP %s)." % st
        pin(G, token, repo, made.get("node_id"))
        return "Created the dashboard issue #%s." % made.get("number")
    issue = issues[0]
    m = re.search(r"generated_at=(\S+)", issue.get("body") or "")
    if m and m.group(1) > generated_at:
        return "Skipped: issue #%s already shows a newer snapshot (%s)." % (issue["number"], m.group(1))
    patch = {"body": body}
    if issue.get("state") == "closed":
        patch["state"] = "open"
    st, _, _ = call(G, "PATCH", "/repos/%s/issues/%s" % (repo, issue["number"]), token, patch)
    return ("Updated the dashboard issue #%s." % issue["number"]) if st == 200 else "Could not update issue #%s (HTTP %s)." % (issue["number"], st)


FAIL_MARK = "<!-- story-gate-dashboard refresh-failed -->"


def report_failure(G, repo, token, when, run_url="", issue_number=None):
    """A refresh failed: keep the last good snapshot, and say so at the top of the issue."""
    st, issues, _ = call(G, "GET", "/repos/%s/issues?labels=%s&state=all&per_page=20&sort=created&direction=asc" % (repo, LABEL), token)
    issues = [i for i in (issues if st == 200 and isinstance(issues, list) else []) if not i.get("pull_request")]
    if issue_number:
        st, one, _ = call(G, "GET", "/repos/%s/issues/%s" % (repo, int(issue_number)), token)
        issues = [one] if st == 200 and isinstance(one, dict) else issues
    if not issues:
        return "no dashboard issue to annotate"
    issue = issues[0]
    body = re.sub(r"\n?%s[^\n]*\n" % re.escape(FAIL_MARK), "\n", issue.get("body") or "")
    note = "%s **⚠ STALE: the last refresh failed at %s**%s. Everything below is the last good snapshot, not the current state.\n" % (
        FAIL_MARK, md(when, 30), (" ([run log](%s))" % run_url) if run_url.startswith("https://") else "")
    body = body.replace("# Story-gate dashboard\n", "# Story-gate dashboard\n\n" + note, 1) if "# Story-gate dashboard\n" in body else note + body
    st, _, _ = call(G, "PATCH", "/repos/%s/issues/%s" % (repo, issue["number"]), token, {"body": body})
    return "marked issue #%s: last refresh failed" % issue["number"] if st == 200 else "could not mark the issue (HTTP %s)" % st


def pin(G, token, repo, node_id):
    """Pin the issue only when one of the repository's three pin slots is free (never unpins anything)."""
    if not node_id:
        return "not pinned"
    try:
        owner, name = repo.split("/", 1)
        d = G.graphql("query($o:String!,$n:String!){repository(owner:$o,name:$n){pinnedIssues(first:3){totalCount}}}", {"o": owner, "n": name}, token)
        if ((d.get("repository") or {}).get("pinnedIssues") or {}).get("totalCount", 3) >= 3:
            return "not pinned (all three pin slots are in use)"
        G.graphql("mutation($id:ID!){pinIssue(input:{issueId:$id}){issue{number}}}", {"id": node_id}, token)
        return "pinned"
    except Exception:
        return "not pinned (the workflow token may not be allowed to pin)"


# ------------------------------------------------------------------ the live page on this computer (--serve)
SERVE_CSP = "default-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'; form-action 'none'"


def ago(t):
    n = int(time.time() - t)
    return "just now" if n < 60 else ("%d min ago" % (n // 60) if n < 3600 else "%d h ago" % (n // 3600))


def reason(e):
    """One short line for the banner: no line breaks, and no password or token from a URL in the message."""
    return re.sub(r"//[^/@\s]*@", "//", re.sub(r"\s+", " ", str(e) or type(e).__name__)).strip()[:200]


class Live:
    """`dashboard --serve`: the page on 127.0.0.1 only, under a secret path, rebuilt every few minutes into a temporary folder.
    `build(folder)` returns the page's HTML or raises; if it raises, the last good page stays up under a red banner."""

    def __init__(self, build, every=300, idle=7200):
        self.build, self.every, self.idle = build, every, idle
        self.tmp = tempfile.TemporaryDirectory(prefix="story-gate-live-")
        self.path = "/s/%s/" % secrets.token_urlsafe(24)
        self.lock, self.stop = threading.Lock(), threading.Event()
        self.page, self.ok_at, self.error, self.last_hit = None, None, "", time.time()
        self.serving = self.closed = False
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self._handler())

    @property
    def url(self):
        return "http://127.0.0.1:%d%s" % (self.httpd.server_address[1], self.path)

    def refresh(self):
        try:
            page = self.build(self.tmp.name)
        except (Exception, SystemExit) as e:  # a failed rebuild never stops the server
            with self.lock:
                self.error = reason(e)
            return False
        with self.lock:
            self.page, self.ok_at, self.error = page, time.time(), ""
        return True

    def render(self):
        with self.lock:
            page, ok_at, err = self.page, self.ok_at, self.error
        if err:
            bar = ('<div role="alert" style="background:#B42318;color:#fff;padding:.6rem 1rem;font:600 14px sans-serif">Couldn\'t refresh: %s. %s</div>'
                   % (h(err, 300), "Showing the last good version (from %s)." % ago(ok_at) if page else "Trying again soon."))
        else:
            bar = ('<div style="padding:.4rem 1rem;font:13px sans-serif;opacity:.8">Live view on this computer. %s</div>'
                   % ("Updated %s; rebuilds every %d min." % (ago(ok_at), self.every // 60) if page else "Building the dashboard..."))
        if page is None:
            return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta http-equiv="refresh" content="3"><title>Story-gate dashboard</title></head><body>%s</body></html>' % bar).encode("utf-8")
        return page.replace("<head>", '<head><meta http-equiv="refresh" content="60">', 1).replace("<body>", "<body>" + bar, 1).encode("utf-8")

    def _handler(self):
        live = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):  # nothing is logged: the address holds the session secret
                pass

            def _send(self, code, data, ctype="text/plain; charset=utf-8", allow=None):
                data = data if isinstance(data, bytes) else data.encode("utf-8")
                self.send_response(code)
                for k, v in (("Content-Type", ctype), ("Cache-Control", "no-store"), ("Content-Security-Policy", SERVE_CSP), ("X-Frame-Options", "DENY"),
                             ("Referrer-Policy", "no-referrer"), ("X-Content-Type-Options", "nosniff"), ("Allow", allow), ("Content-Length", str(len(data)))):
                    if v:
                        self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def _host_ok(self):  # DNS-rebinding guard: only our own loopback address and port
                return self.headers.get("Host", "") == "127.0.0.1:%d" % self.server.server_address[1]

            def do_GET(self):
                if not self._host_ok():
                    return self._send(403, "forbidden")
                if not secrets.compare_digest(urllib.parse.urlparse(self.path).path.encode("utf-8", "replace"), live.path.encode("utf-8")):
                    return self._send(404, "not found")
                live.last_hit = time.time()
                self._send(200, live.render(), "text/html; charset=utf-8")

            def _other(self):  # GET only
                self._send(405 if self._host_ok() else 403, "method not allowed" if self._host_ok() else "forbidden", allow="GET")

            do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _other

        return H

    def serve_thread(self):
        self.serving = True
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()

    def start(self):
        """Start the server and the rebuild loop (the first build begins at once); returns the address to open."""
        self.serve_thread()

        def loop():
            self.refresh()
            while not self.stop.wait(self.every):
                self.refresh()
        threading.Thread(target=loop, daemon=True).start()
        return self.url

    def wait(self):
        """Block until Ctrl-C or until nobody has asked for the page for `idle` seconds; then clean up."""
        try:
            try:
                signal.signal(signal.SIGTERM, signal.default_int_handler)  # a plain `kill` stops it cleanly too
            except ValueError:  # not the main thread (tests)
                pass
            while not self.stop.wait(1):
                if time.time() - self.last_hit > self.idle:
                    print("story-gate dashboard: stopped, nobody opened the page for %d minutes." % (self.idle // 60))
                    break
        except KeyboardInterrupt:
            print("story-gate dashboard: stopped.")
        finally:
            self.close()

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.stop.set()
        if self.serving:
            self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()  # the fetched data and built files go with it


# ------------------------------------------------------------------ command line
def snapshot(gate, c, offline):
    """The data for this run: in CI from the checked-out repository; on a computer after a fetch (unless offline), with local branches
    and this computer's hook protection. Nothing is published."""
    import sg_github as G
    root, in_ci, local_note = gate.ROOT, bool(os.environ.get("GITHUB_ACTIONS")), ""
    if not in_ci and not offline:
        r = subprocess.run(["git", "fetch", "--quiet", "--prune", "origin"], cwd=str(root), capture_output=True, text=True)
        local_note = "" if r.returncode == 0 else "could not fetch from origin (%s); this snapshot may be out of date" % (r.stderr.strip()[:120] or "offline")
    default = os.environ.get("SG_DEFAULT_BRANCH")
    ref = ("origin/" + default) if default else (gate.T.default_policy_ref(root, c.get("base_branch", "main")) or c.get("base_branch", "main"))
    data = build(root, ref, c["story_id_pattern"], include_local=not in_ci, gate=gate)
    if in_ci and os.environ.get("GITHUB_TOKEN") and os.environ.get("GITHUB_REPOSITORY"):
        ci_status(G, os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"], data)
    if not in_ci:
        import sg_guard as SG
        data["computer"] = [{"tool": t, "protection": p, "next": n} for t, p, n in SG.client_matrix(str(root))]
        data["omissions"] = ([local_note] if local_note else []) + ["local snapshot: includes this computer's local branches and anything not pushed"] + data["omissions"]
    return data


def write_files(data, out, artifact_url=None, issue=None, live=False):
    """dashboard.json, dashboard.md and dashboard.html in `out`; returns the Markdown."""
    out.mkdir(parents=True, exist_ok=True)
    (out / "dashboard.json").write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    body = to_markdown(data, artifact_url)
    (out / "dashboard.md").write_text(body, encoding="utf-8")
    (out / "dashboard.html").write_text(to_html(data, live=live, issue=issue), encoding="utf-8")
    return body


def serve(gate, c, kv, rest, issue):
    """`dashboard --serve [--every MIN] [--idle MIN] [--no-browser]`: uses the same GitHub access as --open (your own git login)."""
    try:
        every, idle = max(1, int(kv.get("every", 5))), max(1, int(kv.get("idle", 120)))
    except ValueError:
        print("story-gate dashboard: --every and --idle are whole numbers of minutes.")
        return 2

    def build_page(tmp):
        write_files(snapshot(gate, c, False), Path(tmp), issue=issue, live=True)
        return (Path(tmp) / "dashboard.html").read_text(encoding="utf-8")
    live = Live(build_page, every * 60, idle * 60)
    url = live.start()
    print("story-gate dashboard: live view at\n  %s\nIt rebuilds every %d min, stops after %d min without a visit, and Ctrl-C stops it now. Built files live in a temporary folder that is removed when it stops." % (url, every, idle))
    if "--no-browser" not in rest:
        import webbrowser
        webbrowser.open(url)
    live.wait()
    return 0


def cli(gate, kv, rest):
    import sg_github as G
    c = gate.cfg()
    root = gate.ROOT
    offline = "--offline" in rest
    repo = os.environ.get("GITHUB_REPOSITORY") or gate.origin_repo()
    issue = issue_url(repo, G.WEB) if repo else None
    if "--report-failure" in rest:
        token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
        if not token or not repo:
            return 1
        run_url = "%s/%s/actions/runs/%s" % (os.environ.get("GITHUB_SERVER_URL", "https://github.com"), repo, os.environ.get("GITHUB_RUN_ID", ""))
        print(report_failure(G, repo, token, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), run_url, c.get("dashboard_issue")))
        return 0
    if "--serve" in rest:
        return serve(gate, c, kv, rest, issue)
    if kv.get("from-json"):
        data = json.loads(Path(kv["from-json"]).read_text(encoding="utf-8"))
    else:
        data = snapshot(gate, c, offline)
    out = Path(kv.get("out") or tempfile.mkdtemp(prefix="story-gate-dashboard-"))
    body = write_files(data, out, kv.get("artifact-url"), issue)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary and "--publish" in rest:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(body + "\n")
    msg = "story-gate dashboard: %d stories, %s. Files in %s" % (len(data.get("stories", [])), data.get("generated_at"), out)
    if "--publish" in rest:
        token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
        if not token or not repo:
            print("::warning::--publish needs GITHUB_TOKEN and GITHUB_REPOSITORY"); return 1
        msg += "\n" + publish_issue(G, repo, token, body, data.get("generated_at") or "", c.get("dashboard_issue"))
    print(msg)
    if "--open" in rest:
        import webbrowser
        webbrowser.open((out / "dashboard.html").resolve().as_uri())
    return 0
