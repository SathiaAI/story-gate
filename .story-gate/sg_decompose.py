"""story-gate narrow judging: the judge answers small questions over evidence packs, and code combines the answers.

Why: one broad question over a whole diff ("the diff implements every acceptance criterion") hides which criterion the
judge doubted, and a large diff does not fit in one request. Here each criterion (AC) gets its own request, and the
evidence in that request is only what relates to that AC (its "evidence pack"). Code then takes the lowest score.
Nothing here takes a maximum over chunks: an AC built across several files is judged from one pack that holds all of them.

The judge (Jev) returns numbers only. Every `why` in this module is built by code from those numbers and from facts
about the evidence (which files, what was cut). It is never model text.

Pure functions plus run(), which takes the judge as a function (ask(state, questions) -> the shape sg_judges.ask returns),
so it can be tested with a fake judge. Decision record: docs/decisions/2026-10-22-judge-evidence-packs.md.
"""
import concurrent.futures, hashlib, json, re

SCHEMA = "decompose-1"
MODES = ("off", "shadow", "on")
DEFAULT_MODE = "shadow"
DEFAULT_MAX_REQUESTS = 60
MAX_REQUESTS_LIMIT = 1000
PARALLEL = 4  # requests in flight at once
RELEVANT = 0.5  # a changed file with at least this relevance to an AC goes into that AC's pack
MIN_PART = 1000  # a file that doesn't fit is shown cut only if at least this many characters are left
AC_TEXT_MAX = 2000  # an AC's text in the evidence: enough to read, bounded; a longer AC is marked cut
AC_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,39}$")  # ids appear in question wording, so they must be plain names

# Per-AC checks, per-group checks and the READY wording. The text of a question that only gets a criteria sentence is here.
NO_BLOCKERS = ("There are no unresolved open questions that would force the developer to guess. True means no open question "
               "forces the developer to guess. Follow-ups planned for after the merge, decisions already recorded, and dated "
               "known exceptions with an owner are NOT open questions.")
PER_AC_READY = ("ac_testable", "tests_plan_adequate")
PER_AC_DONE = ("impl_matches_acs", "tests_implemented")
PER_GROUP_DONE = ("impl_no_unplanned_scope", "no_deferrals", "drift_trd_after")
REST_DONE = ("handoff_out", "learnings_specific")
GROUP_NOTE = " Judge only the files shown here; they are one part of the change."
# The handoff and learnings are also checked against each group's code, so a claim the code contradicts is caught.
GROUP_DOCS = {"handoff_out": "handoff.md does not contradict the changes in these files, and names any interface, contract or "
                             "command they add that the next story or agent would need.",
              "learnings_specific": "The recorded learnings do not miss a workaround, failure or surprise that is visible in "
                                    "these files (no learnings is fine when nothing here went wrong or was surprising)."}


# ------------------------------------------------------------------ settings
def mode(c):
    """The configured mode: off, shadow or on (the default is shadow)."""
    j = c.get("judge") if isinstance(c.get("judge"), dict) else {}
    return j.get("decompose", DEFAULT_MODE)


def max_requests(c):
    j = c.get("judge") if isinstance(c.get("judge"), dict) else {}
    v = j.get("max_requests", DEFAULT_MAX_REQUESTS)
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 1 else DEFAULT_MAX_REQUESTS


def settings_problem(j):
    """None when judge.decompose and judge.max_requests are valid, else a plain-English reason."""
    if not isinstance(j, dict):
        return None
    if j.get("decompose", DEFAULT_MODE) not in MODES:
        return '"judge" > "decompose" must be "off", "shadow" or "on" (got %r)' % (j.get("decompose"),)
    v = j.get("max_requests", DEFAULT_MAX_REQUESTS)
    if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= MAX_REQUESTS_LIMIT:
        return '"judge" > "max_requests" must be a whole number from 1 to %d (got %r)' % (MAX_REQUESTS_LIMIT, v)
    return None


def fingerprint(c):
    """What to add to the policy fingerprint: nothing when off (so every stored verdict keeps its hash), else the schema,
    the mode and the request cap, so a changed method or cap re-scores instead of reusing an older verdict."""
    m = mode(c)
    return {} if m == "off" else {"dc": [SCHEMA, m, max_requests(c)]}


# ------------------------------------------------------------------ splitting a diff
def _unquote(s):
    """A path git wrote in quotes ("a/b\\tc", octal escapes for bytes) as plain text."""
    if not (len(s) >= 2 and s[0] == '"' and s[-1] == '"'):
        return s
    s, out, i = s[1:-1], bytearray(), 0
    esc = {"t": 9, "n": 10, "r": 13, "\\": 92, '"': 34, "a": 7, "b": 8, "f": 12, "v": 11}
    while i < len(s):
        ch = s[i]
        if ch == "\\" and i + 1 < len(s):
            m = re.match(r"[0-7]{3}", s[i + 1:i + 4])
            if m:
                out.append(int(m.group(0), 8))
                i += 4
                continue
            out.append(esc.get(s[i + 1], ord(s[i + 1]) & 0xFF))
            i += 2
            continue
        out += ch.encode("utf-8")
        i += 1
    return out.decode("utf-8", "replace")


def _header_paths(rest):
    """(old, new) from the text after 'diff --git ': 'a/X b/Y'. Handles quotes and a path with spaces when both are equal."""
    quoted = re.findall(r'"(?:[^"\\]|\\.)*"', rest)
    if len(quoted) == 2:
        a, b = (_unquote(q) for q in quoted)
    elif len(quoted) == 1:  # one side quoted, the other not
        q = quoted[0]
        other = rest.replace(q, "", 1).strip()
        a, b = (_unquote(q), other) if rest.startswith('"') else (other, _unquote(q))
    else:
        n = len(rest)
        half = (n - 1) // 2  # 'a/P b/P' -> 2 + L + 1 + 2 + L
        if n % 2 == 1 and rest[:half].startswith("a/") and rest[half] == " " and rest[half + 1:].startswith("b/") \
                and rest[2:half] == rest[half + 3:]:
            a, b = rest[:half], rest[half + 1:]
        else:
            m = re.match(r"^(a/.+?) (b/.+)$", rest)
            a, b = (m.group(1), m.group(2)) if m else (rest, rest)
    strip = lambda p, pre: p[len(pre):] if p.startswith(pre) else p
    return strip(a, "a/"), strip(b, "b/")


def split_diff(diff):
    """A unified diff as one entry per file, in the order given:
    {"path", "old_path", "status": modified|added|deleted|renamed|copied, "binary", "text"}.
    "text" is that file's part of the diff, header included. Text before the first file header is dropped."""
    chunks, cur = [], None
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git "):
            cur = [line]
            chunks.append(cur)
        elif cur is not None:
            cur.append(line)
    out = []
    for lines in chunks:
        old, new = _header_paths(lines[0].rstrip("\r\n")[len("diff --git "):])
        status, binary, rename_to, plus = "modified", False, None, None
        for ln in lines[1:]:
            if ln.startswith("@@"):
                break
            s = ln.rstrip("\r\n")
            if s.startswith("new file"):  # 'new file mode ...' or the gate's 'new file (untracked)'
                status = "added"
            elif s.startswith("deleted file mode"):
                status = "deleted"
            elif s.startswith("rename from "):
                old, status = _unquote(s[len("rename from "):]), "renamed"
            elif s.startswith("rename to "):
                rename_to = _unquote(s[len("rename to "):])
            elif s.startswith("copy from "):
                old, status = _unquote(s[len("copy from "):]), "copied"
            elif s.startswith("copy to "):
                rename_to = _unquote(s[len("copy to "):])
            elif s.startswith("Binary files ") or s.startswith("GIT binary patch"):
                binary = True
            elif s.startswith("+++ ") and not s.startswith("+++ /dev/null"):
                plus = _unquote(s[4:].split("\t")[0])
        path = rename_to or (plus[2:] if plus and plus.startswith("b/") else plus) or new
        if status == "deleted":
            path = old
        out.append({"path": path, "old_path": old if status in ("renamed", "copied") else path, "status": status,
                    "binary": binary, "text": "".join(lines)})
    return out


def visible_text(f):
    """A file's diff without the removed lines (a deleted test is not a definition)."""
    return "".join(l for l in f["text"].splitlines(keepends=True) if not l.startswith("-"))


# A line that starts a test definition in the common test styles (pytest/unittest, node:test/jest/mocha, go, rust, gherkin).
TEST_START = re.compile(r"^[+ ]\s*(?:(?:async\s+)?def\s+test|(?:test|it|describe|context|scenario|specify)(?:\.\w+)?\s*\(|"
                        r"func\s+Test|fn\s+test|#\[test\]|@Test\b|Scenario:)")
TEST_BLOCK_MAX = 200  # lines kept from one test definition


def test_blocks(f, refs, defined):
    """Only the parts of a test file's diff that define one of `refs`: from the defining line to the next test definition
    (at most TEST_BLOCK_MAX lines), with the file header. A whole test file can be larger than a pack, so an AC's pack
    carries just its own tests. -> (text, cut): text is "" when none of `refs` is defined in this diff; cut is True when
    a block was longer than TEST_BLOCK_MAX lines and its end is not shown."""
    lines = f["text"].splitlines(keepends=True)
    head = []
    for ln in lines:
        if ln.startswith("@@"):
            break
        head.append(ln)
    body = [ln for ln in lines[len(head):] if not ln.startswith("-")]
    starts = [i for i, ln in enumerate(body) if TEST_START.search(ln)]
    keep, cut = [], False
    for k, i in enumerate(starts):
        if any(defined(r, body[i][1:]) for r in refs):
            stop = starts[k + 1] if k + 1 < len(starts) else len(body)
            end = min(stop, i + TEST_BLOCK_MAX)
            keep.append("".join(body[i:end]))
            if end < stop:
                cut = True
                keep.append("[... cut: %d more lines of this test not shown]\n" % (stop - end))
    return ("".join(head) + "@@ tests for this acceptance criterion @@\n" + "".join(keep), cut) if keep else ("", False)


def clip(text, n):
    """(text cut to n characters with a visible marker, True when it was cut). Same marker the gate uses."""
    if len(text) <= n:
        return text, False
    return text[:n] + "\n[... cut: %d more characters not shown]" % (len(text) - n), True


# ------------------------------------------------------------------ requests
def noul(text):
    return {"type": "noul", "instructions": text}


def ac_text(a):
    return clip(a["text"], AC_TEXT_MAX)[0]


def text_cut_note(a):
    return ["the criterion's text is over %d characters and was cut" % AC_TEXT_MAX] if a.get("text_cut") else []


def relevance_request(f, acs, per_file):
    """One request: does this file's change serve each AC? One question per AC."""
    body, cut = clip(f["text"], per_file)
    state = {"file": f["path"], "status": f["status"], "diff": body, "truncated": cut,
             "acceptance_criteria": [{"id": a["id"], "text": ac_text(a)} for a in acs]}
    qs = {"rel:" + a["id"]: noul("The change to this file implements or directly supports the acceptance criterion with id "
                                 "%s in `acceptance_criteria`." % a["id"]) for a in acs}
    return {"id": "rel:" + f["path"], "kind": "relevance", "state": state, "questions": qs}


def planned_cases(a):
    p = a.get("plan") or {}
    return {**{k: p.get(k) or [] for k in ("positive", "negative", "edge", "regression")}, "not_applicable": p.get("not_applicable") or {}}


def ac_request(a, pack, phase):
    """One request per AC: DONE asks about the code and tests in its pack, READY about the AC and its plan."""
    state = {"acceptance_criterion": {"id": a["id"], "text": ac_text(a)}, "planned_cases": planned_cases(a), "test_refs": a["refs"]}
    if phase == "done":
        state.update(files=[p["path"] for p in pack["pieces"]], diff="\n".join(p["text"] for p in pack["pieces"]),
                     evidence_truncated=pack["truncated"],
                     evidence_note="Only the files in this evidence pack are shown. Count only behaviour present in the diff.")
        qs = {"impl_ac:" + a["id"]: noul("The code and tests in this evidence pack implement the acceptance criterion in "
                                         "`acceptance_criterion`. Count only behaviour present in the diff."),
              "tests_ac:" + a["id"]: noul("The tests in this evidence pack cover the planned positive, negative and edge cases "
                                          "(plus regression cases where the plan lists them) for the acceptance criterion in "
                                          "`acceptance_criterion`. A category the plan marks not applicable, with a reason, needs no test.")}
    else:
        state["story"] = pack["story"]
        qs = {"ac_testable:" + a["id"]: noul("The acceptance criterion in `acceptance_criterion` is binary and verifiable "
                                              "(clearly pass or fail), not vague."),
              "tests_plan_adequate:" + a["id"]: noul("The test plan in `planned_cases` for the acceptance criterion in "
                                                      "`acceptance_criterion` has meaningful positive, negative, edge and "
                                                      "regression cases, or a valid reason a category does not apply.")}
    return {"id": "ac:" + a["id"], "kind": "ac", "state": state, "questions": qs}


def build_pack(a, files, rel, defined, budget, per_file, unassessed):
    """The evidence for one AC: the diff of the test files that define any of its test_refs, then the code files whose
    relevance to this AC is at least RELEVANT, most relevant first, until `budget` characters. A file that does not fit
    marks the pack truncated. -> {"pieces": [{"path", "text", "cut"}], "truncated", "notes", "files"}."""
    refs = a["refs"]
    tests, cut_tests = [], []
    for f in files:
        if f["test"] and refs and any(defined(r, visible_text(f)) for r in refs):
            part, cut = test_blocks(f, refs, defined)
            tests.append(dict(f, text=part) if part else f)  # just this AC's tests when they can be found, else the file
            if cut:
                cut_tests.append(f["path"])
    code = sorted(((rel[f["path"]].get(a["id"], 0.0), f) for f in files if (not f["test"] or f.get("unlinked")) and f["path"] in rel),
                  key=lambda t: (-t[0], t[1]["path"]))
    chosen = tests + [f for p, f in code if p >= RELEVANT]
    pieces, used, notes, truncated = [], 0, text_cut_note(a), bool(a.get("text_cut"))
    if cut_tests:
        truncated = True
        notes.append("a test longer than %d lines was cut in %s" % (TEST_BLOCK_MAX, ", ".join(cut_tests)))
    partial = [f["path"] for p, f in code if p < RELEVANT and len(f["text"]) > per_file]
    if partial:  # relevance saw only the first part, so the rest of the file may still serve this AC
        truncated = True
        notes.append("left out after relevance read only part of %s" % ", ".join(partial[:3]) + (" and %d more" % (len(partial) - 3) if len(partial) > 3 else ""))
    for i, f in enumerate(chosen):
        room = budget - used
        if len(f["text"]) <= room:
            pieces.append({"path": f["path"], "text": f["text"], "cut": False})
            used += len(f["text"])
            if f.get("binary"):  # only the header can be shown
                truncated = True
                notes.append("%s is a binary change; its contents are not shown" % f["path"])
            if (not f["test"] or f.get("unlinked")) and len(f["text"]) > per_file:  # relevance saw only the first part
                truncated = True
                notes.append("%s was read only in part when its relevance was judged" % f["path"])
            continue
        truncated = True
        if room >= MIN_PART:
            part, _ = clip(f["text"], room)
            pieces.append({"path": f["path"], "text": part, "cut": True})
        left = [x["path"] for x in chosen[i:]]
        notes.append("evidence over the %d-character budget: %s %s" % (
            budget, ", ".join(left[:3]) + (" and %d more" % (len(left) - 3) if len(left) > 3 else ""),
            "cut or left out"))
        break
    if unassessed:
        truncated = True
        notes.append("%d changed file%s not checked for relevance (request cap): %s" % (
            len(unassessed), "" if len(unassessed) == 1 else "s", ", ".join(unassessed[:3]) + (" ..." if len(unassessed) > 3 else "")))
    return {"pieces": pieces, "truncated": truncated, "notes": notes, "files": [p["path"] for p in pieces]}


def build_groups(files, budget):
    """Changed files in order, packed into groups of at most `budget` characters. A file over the budget is cut and
    sits alone in its group, marked truncated. -> [{"pieces", "truncated", "notes", "files"}]."""
    groups, cur, used = [], [], 0

    def close():
        nonlocal cur, used
        if cur:
            groups.append(cur)
        cur, used = [], 0
    for f in files:
        if len(f["text"]) > budget:
            close()
            part, _ = clip(f["text"], budget)
            groups.append([{"path": f["path"], "text": part, "cut": True}])
            continue
        if used + len(f["text"]) > budget:
            close()
        cur.append({"path": f["path"], "text": f["text"], "cut": False, "binary": bool(f.get("binary"))})
        used += len(f["text"])
    close()
    out = []
    for g in groups:
        cut = [p["path"] for p in g if p["cut"]]
        binary = [p["path"] for p in g if p.get("binary")]
        notes = ["%s is over the %d-character budget and was cut" % (cut[0], budget)] if cut else []
        if binary:
            notes.append("binary change, contents not shown: %s" % ", ".join(binary[:3]) + (" and %d more" % (len(binary) - 3) if len(binary) > 3 else ""))
        out.append({"pieces": g, "truncated": bool(cut or binary), "files": [p["path"] for p in g], "notes": notes})
    return out or [{"pieces": [], "truncated": False, "files": [], "notes": []}]  # no files: still ask once, on an empty change


def group_request(g, i, n, ev):
    st = ev["state"]
    state = {"story": st.get("story", ""), "context": st.get("context", ""), "changed_files": st.get("changed_files", []),
             "group": "%d of %d" % (i + 1, n), "group_files": g["files"], "diff": "\n".join(p["text"] for p in g["pieces"]),
             "evidence_truncated": g["truncated"], "handoff": st.get("handoff", ""), "learnings": st.get("learnings", "")}
    questions = {k: noul(ev["questions"][k] + GROUP_NOTE) for k in PER_GROUP_DONE if k in ev["questions"]}
    questions.update({"doc:" + k: noul(t + GROUP_NOTE) for k, t in GROUP_DOCS.items() if k in ev["questions"]})
    questions["drift_direction"] = {"type": "choice", "instructions": "Which way does any drift between the story/work and the "
                                    "PRD/TRD point? " + GROUP_NOTE.strip(), "criteria": ev["choices"]}
    return {"id": "group:%d" % (i + 1), "kind": "group", "state": state, "questions": questions}


def rest_request(ev):
    """The questions whose evidence is small and stays as it was (the story, handoff, learnings...), in one request."""
    q = ev["questions"]
    if ev["phase"] == "ready":
        keys = [k for k in q if k not in PER_AC_READY]
        questions = {k: noul(NO_BLOCKERS if k == "dor_no_blockers" else q[k]) for k in keys}
        questions["drift_direction"] = {"type": "choice", "instructions": "Which way does any drift between the story/work and "
                                        "the PRD/TRD point?", "criteria": ev["choices"]}
        state = dict(ev["state"])
    else:
        questions = {k: noul(q[k]) for k in REST_DONE if k in q}
        state = {k: v for k, v in ev["state"].items() if k not in ("diff", "diff_truncated")}
    return {"id": "rest", "kind": "rest", "state": state, "questions": questions}


# ------------------------------------------------------------------ running requests
def execute(reqs, ask, workers=PARALLEL):
    """Ask every request, up to `workers` at a time. The results come back in the order of `reqs`, whatever order the
    answers arrive in, so a run's outcome does not depend on timing."""
    def one(r):
        try:
            return ask(r["state"], r["questions"])
        except Exception as e:  # the judge's own ask() never raises; this is a seat belt
            return {"tier": "none", "error": repr(e)[:200]}
    if len(reqs) <= 1:
        return [one(r) for r in reqs]
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(one, reqs))


def first_error(reqs, results):
    for r, res in zip(reqs, results):
        if not isinstance(res, dict) or res.get("tier") not in ("jev", "emulated") or not isinstance(res.get("answers"), dict):
            return "request %s: %s" % (r["id"], (res or {}).get("error") or "no answer")
    return None


def p_of(res, key):
    return res["answers"][key]["noul"]


# ------------------------------------------------------------------ combining
def item(id_, score, files=(), truncated=False, notes=(), not_asked=False, detail=""):
    return {"id": id_, "detail": detail, "score": None if score is None else round(float(score), 3), "files": list(files),
            "truncated": bool(truncated or not_asked), "not_asked": bool(not_asked), "notes": list(notes)}


def short_files(files, n=4):
    return ", ".join(files[:n]) + (" +%d more" % (len(files) - n) if len(files) > n else "")


def combine(items, th, band):
    """One check from its items: the lowest score (never a maximum), its band, and a `why` built from the items.
    A truncated or not-asked item means the check cannot PASS. -> {"score", "band", "items", "why", "capped"}."""
    scored = [i for i in items if i["score"] is not None]
    score = min((i["score"] for i in scored), default=None)
    capped = any(i["truncated"] for i in items)
    low = sorted((i for i in scored if i["score"] < th["pass"]), key=lambda i: (i["score"], i["id"]))[:3]
    parts = ["%s %.2f%s" % (i["id"], i["score"], " (files: %s)" % short_files(i["files"]) if i["files"] else
                            " (%s)" % i["detail"] if i["detail"] else "") for i in low]
    notes = []
    for i in items:
        for n in i["notes"]:
            if n not in notes:
                notes.append(n)
    unasked = [i["id"] for i in items if i["not_asked"]]
    if unasked:
        notes.append("not asked (request cap reached): " + ", ".join(unasked[:5]) + (" ..." if len(unasked) > 5 else ""))
    why = "; ".join(parts + notes)
    st = "CONCERNS" if score is None else band(score, th)
    if capped and st == "PASS":
        st = "CONCERNS"
    return {"score": score, "band": st, "items": items, "why": why, "capped": capped}


def combine_meta(results):
    """The judge that answered: 'jev' only if every answer came from Jev, else 'emulated'; the models, the cost."""
    tier = "jev" if all(r["tier"] == "jev" for r in results) else "emulated"
    models = sorted({str(r.get("model") or "") for r in results} - {""})
    provs = sorted({str(r.get("provider") or "") for r in results} - {""})
    costs = [r["cost"] for r in results if isinstance(r.get("cost"), (int, float)) and not isinstance(r.get("cost"), bool)]
    return {"tier": tier, "provider": "+".join(provs) or None, "model": "+".join(models) or None,
            "cost": round(sum(costs), 6) if costs else None}


def evidence_hash(asked):
    """A short hash of exactly what was sent: every request's id, evidence and questions."""
    blob = json.dumps([[r["id"], r["state"], r["questions"]] for r in asked], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8", "ignore")).hexdigest()[:16]


def take(reqs, left):
    """(the requests to ask, the ones the cap leaves out)."""
    return reqs[:max(left, 0)], reqs[max(left, 0):]


# ------------------------------------------------------------------ the whole thing
def run(phase, ev, ask):
    """Score a phase with narrow questions. ev (built by gate.py): phase, state (the broad request's evidence), diff, acs
    [{id, text, refs, plan}], questions (the broad questions asked, minus skipped), choices (drift choices), max_chars,
    max_requests, th (thresholds), is_test(path), defined(ref, text), band(p, th).
    -> {"error": why} or {"scores", "why", "items", "capped", "unscored", "drift", "drift_conf", "tier", "provider", "model",
    "cost", "checks", "requests", "evidence_hash", "packs", "groups"}.  Never raises for a judge failure."""
    ids = [str(a.get("id") or "").strip() for a in ev["acs"]]
    dup = sorted({i for i in ids if ids.count(i) > 1}) if len(set(ids)) != len(ids) else []
    if "" in ids or dup:  # packs and answers are keyed by id: two criteria with one id would share one score
        return {"error": "acceptance criteria need unique, non-empty ids (repeated: %s)" % (", ".join(dup) or "an empty id")}
    bad = [i for i in ids if not AC_ID.match(i)]
    if bad:  # an id is the only story text that reaches a question's wording
        return {"error": "acceptance-criterion ids must be short plain names (letters, digits, . _ : -): %r" % bad[0][:60]}
    ev = dict(ev, acs=[dict(a, text_cut=len(a.get("text") or "") > AC_TEXT_MAX) for a in ev["acs"]])
    return _run_done(ev, ask) if phase == "done" else _run_ready(ev, ask)


def _finish(ev, checks, drift, conf, all_results, asked, extra):
    meta = combine_meta(all_results)
    scores = {k: v["score"] for k, v in checks.items() if v["score"] is not None}
    out = {"scores": scores, "drift": drift, "drift_conf": conf, **meta,
           "why": {k: v["why"] for k, v in checks.items() if v["why"]},
           "items": {k: v["items"] for k, v in checks.items() if v["items"]},
           "capped": sorted(k for k, v in checks.items() if v["capped"]),
           "unscored": {k: v["why"] or "no score" for k, v in checks.items() if v["score"] is None},
           "checks": {k: {"score": v["score"], "band": v["band"], "why": v["why"],
                          "items": [{"id": i["id"], "score": i["score"], "files": i["files"], "truncated": i["truncated"]}
                                    for i in v["items"]]} for k, v in checks.items()},
           "requests": len(asked), "max_requests": ev["max_requests"], "evidence_hash": evidence_hash(asked)}
    out.update(extra)
    return out


def _run_ready(ev, ask):
    th, band, cap = ev["th"], ev["band"], ev["max_requests"]
    acs = ev["acs"]
    rest = rest_request(ev)
    story, _ = clip(ev["state"].get("story", ""), ev["max_chars"] // 3)
    ac_reqs = [ac_request(a, {"story": story}, "ready") for a in acs]
    reqs, dropped = take([rest] + ac_reqs, cap)
    results = execute(reqs, ask)
    err = first_error(reqs, results)
    if err:
        return {"error": err}
    by = {r["id"]: res for r, res in zip(reqs, results)}
    checks = {}
    for k, q in rest["questions"].items():
        if q["type"] == "noul":
            p = p_of(by["rest"], k)
            checks[k] = {"score": round(p, 3), "band": band(p, th), "items": [], "why": "", "capped": False}
    cd = by["rest"]["answers"]["drift_direction"]
    dropped_ids = {r["id"] for r in dropped}
    for key in PER_AC_READY:
        if key not in ev["questions"]:
            continue
        items = []
        for a in acs:
            rid = "ac:" + a["id"]
            if rid in dropped_ids:
                items.append(item(a["id"], None, not_asked=True))
            else:
                items.append(item(a["id"], p_of(by[rid], key + ":" + a["id"]), (), a.get("text_cut"), text_cut_note(a)))
        checks[key] = combine(items, th, band) if items else {
            "score": 0.0, "band": "FAIL", "items": [], "why": "no acceptance criteria in tests.json", "capped": False}
    return _finish(ev, checks, cd["choice"], cd["confidence"], results, reqs, {"packs": {}, "groups": []})


# The files the gate itself keeps in a story folder. Anything else there is ordinary work and stays in the evidence.
STORY_RECORDS = {"story.md", "context.md", "tests.json", "handoff.md", "validation.md", "trace.md", "scenarios.json",
                 "scenario_results.json", "test_results.json", "budgets.json", "budget_results.json", "guardrail_results.json",
                 "checkpoints.jsonl", "decisions.jsonl", "coder.json", "pitfalls.json", "manifest.json", "evidence.json",
                 "ready.json", "done.json", "ready.self.json", "done.self.json"}


def story_record(path, ev):
    """True for a record the gate keeps in this story's folder (story.md, tests.json, verdicts...). The judge already gets
    the story in its fields, so these never go into packs or groups. Other files in the folder are evidence like any file."""
    d = (ev.get("story_dir") or "").rstrip("/")
    return bool(d and path.startswith(d + "/") and path[len(d) + 1:] in STORY_RECORDS)


def story_copy(path, ev):
    """True for a file named <ID>.md outside the story folder (a project copy of the story). It stays out of the AC packs,
    which already carry the story, but stays in the file groups, so scope, deferral and drift still see it."""
    sid, d = ev.get("story_id") or "", (ev.get("story_dir") or "").rstrip("/")
    inside = bool(d and path.startswith(d + "/"))  # a file in the story folder is a record or ordinary evidence, never a copy
    return bool(sid and path.rsplit("/", 1)[-1] == sid + ".md" and not inside)


def _run_done(ev, ask):
    th, band, cap, mx = ev["th"], ev["band"], ev["max_requests"], ev["max_chars"]
    acs = ev["acs"]
    budget, per_file = mx // 2, mx // 4
    files = split_diff(ev["diff"])
    excluded = sorted(f["path"] for f in files if story_record(f["path"], ev))
    files = [f for f in files if not story_record(f["path"], ev)]
    copies = {f["path"] for f in files if story_copy(f["path"], ev)}
    all_refs = [r for a in acs for r in a["refs"]]
    for f in files:
        f["test"] = bool(ev["is_test"](f["path"]))
        # an edit below a test's definition (a changed assertion or fixture) does not name the test: judge its relevance
        f["unlinked"] = f["test"] and not any(ev["defined"](r, visible_text(f)) for r in all_refs)
    code = [f for f in files if not f["test"] or f["unlinked"]]
    rest = rest_request(ev)
    rel_reqs = [relevance_request(f, acs, per_file) for f in code if f["path"] not in copies] if acs else []
    stage1, dropped1 = take([rest] + rel_reqs, cap)
    res1 = execute(stage1, ask)
    err = first_error(stage1, res1)
    if err:
        return {"error": err}
    by = {r["id"]: res for r, res in zip(stage1, res1)}
    rel = {}
    for f in code:
        res = by.get("rel:" + f["path"])
        if res is not None:
            rel[f["path"]] = {a["id"]: p_of(res, "rel:" + a["id"]) for a in acs}
    unassessed = [r["state"]["file"] for r in dropped1]
    # evidence packs, one per AC
    packs = {a["id"]: build_pack(a, files, rel, ev["defined"], budget, per_file, unassessed) for a in acs}
    ac_reqs = [ac_request(a, packs[a["id"]], "done") for a in acs if packs[a["id"]]["pieces"]]
    groups = build_groups(files, budget)
    grp_reqs = [group_request(g, i, len(groups), ev) for i, g in enumerate(groups)]
    stage2, dropped2 = take(ac_reqs + grp_reqs, cap - len(stage1))
    res2 = execute(stage2, ask)
    err = first_error(stage2, res2)
    if err:
        return {"error": err}
    by.update({r["id"]: res for r, res in zip(stage2, res2)})
    gone = {r["id"] for r in dropped2}
    checks = {}
    for k in REST_DONE:
        if k in ev["questions"]:
            items = [item("the story records", p_of(by["rest"], k), [], False, [])]
            for i, g in enumerate(groups):
                rid, name = "group:%d" % (i + 1), "group %d of %d" % (i + 1, len(groups))
                if rid in gone:
                    items.append(item(name, None, g["files"], g["truncated"], g["notes"], not_asked=True))
                else:
                    items.append(item(name, p_of(by[rid], "doc:" + k), g["files"], g["truncated"], g["notes"]))
            checks[k] = combine(items, th, band)
    for key, qk in (("impl_matches_acs", "impl_ac:"), ("tests_implemented", "tests_ac:")):
        if key not in ev["questions"]:
            continue
        items = []
        for a in acs:
            pk, rid = packs[a["id"]], "ac:" + a["id"]
            if not pk["pieces"] and unassessed:  # some files were never checked, so "nothing relates" is not known
                items.append(item(a["id"], None, [], True, pk["notes"], not_asked=True))
            elif not pk["pieces"]:  # nothing in the diff relates to this AC: no request is needed to know it is missing
                items.append(item(a["id"], 0.0, [], pk["truncated"], pk["notes"], detail="no changed file relates to it"))
            elif rid in gone:
                items.append(item(a["id"], None, pk["files"], pk["truncated"], pk["notes"], not_asked=True))
            else:
                items.append(item(a["id"], p_of(by[rid], qk + a["id"]), pk["files"], pk["truncated"], pk["notes"]))
        checks[key] = combine(items, th, band) if items else {
            "score": 0.0, "band": "FAIL", "items": [], "why": "no acceptance criteria in tests.json", "capped": False}
    drift, conf, low_drift = "none", 0.0, None
    for key in PER_GROUP_DONE:
        if key not in ev["questions"]:
            continue
        items = []
        for i, g in enumerate(groups):
            rid, name = "group:%d" % (i + 1), "group %d of %d" % (i + 1, len(groups))
            if rid in gone:
                items.append(item(name, None, g["files"], g["truncated"], g["notes"], not_asked=True))
                continue
            p = p_of(by[rid], key)
            items.append(item(name, p, g["files"], g["truncated"], g["notes"]))
            if key == "drift_trd_after" and (low_drift is None or p < low_drift):  # lowest TRD score; the first group on a tie
                low_drift = p
                ch = by[rid]["answers"]["drift_direction"]
                drift, conf = ch["choice"], ch["confidence"]
        checks[key] = combine(items, th, band)
    if low_drift is None and groups:  # no group was asked about drift: unknown is not "none"
        drift, conf = "architect_must_decide", 0.0
    asked = stage1 + stage2
    extra = {"packs": {a["id"]: {"files": packs[a["id"]]["files"], "truncated": packs[a["id"]]["truncated"]} for a in acs},
             "groups": [{"files": g["files"], "truncated": g["truncated"]} for g in groups],
             "not_asked": sorted(r["id"] for r in dropped1 + dropped2), "story_records": excluded}
    return _finish(ev, checks, drift, conf, res1 + res2, asked, extra)


# ------------------------------------------------------------------ showing it
def summary_lines(v):
    """Plain-English lines about a verdict's narrow-question result (its `decomposed` record); [] when there is none.
    "on": the narrow score is the binding one. "shadow": the broad score decides and this one is only for comparison."""
    d = v.get("decomposed") if isinstance(v, dict) else None
    if not isinstance(d, dict):
        return []
    who = {"jev": "Jev"}.get(v.get("judge"), str(v.get("judge") or "judge"))
    if d.get("mode") == "on":
        head = "Score (%s, binding): narrow questions over evidence packs" % who
        if d.get("error"):
            return [head + " did not run (%s), so the judge counts as unavailable." % d["error"]]
        lines = [head + " (%d requests, evidence %s)." % (d.get("requests", 0), d.get("evidence_hash"))]
    else:
        head = "Shadow (not binding): narrow questions over evidence packs"
        if d.get("error"):
            return [head + " did not run: %s" % d["error"]]
        lines = ["Score (%s, binding): broad questions over the whole change, as shown above." % who,
                 head + " would give %s (%d requests, evidence %s)." % (d.get("would_be_overall", "?"), d.get("requests", 0), d.get("evidence_hash"))]
    for k, x in sorted((d.get("checks") or {}).items()):
        if x.get("band") != "PASS":
            lines.append("  %s %s: %s" % (x.get("band"), k, x.get("why") or ("score %.2f" % x["score"] if x.get("score") is not None else "no score")))
    return lines
