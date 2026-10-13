"""Guardrail areas: groups of checks the owner turns on for parts of the work (security, performance, data, ...).

Design: docs/measured-checks.md. Owner's guide: docs/guardrails.md. The `guardrails` section of the default branch's
.story-gate/config.json:

  {"areas": {"security": {"mode": "enforce", "paths": ["src/**"], "content": ["password"],
                          "checks": [{"name": "scan", "measure": "sarif", "report": "out/scan.sarif",
                                      "command": ["./tools/scan.sh"], "pins": ["tools/scan.cfg"], "limit": 0,
                                      "source": "owner, 2026-10"}]}}}

An area is "off", "warn" (shown, never blocks) or "enforce" (a problem fails DONE, whatever accept_concerns says). It
applies when it isn't off and the change touches code: with no triggers always, else when a changed path matches one of
its globs or a changed line matches one of its patterns. Each check is run by the measured-check engine (sg_measure.py)
exactly like a decided budget: same runner, isolation, pins, parsers, trust rules and CI time. Stdlib only.
"""
import fnmatch, json, re, subprocess, sys

MODES = ("off", "warn", "enforce")
AREA = re.compile(r"[a-z][a-z0-9-]{0,31}")
CHECK = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
MEASURES = ("sarif", "junit", "number", "date")
MAX_AREAS, MAX_CHECKS, MAX_TRIGGERS, MAX_PATTERN = 10, 10, 20, 200
DIFF_CAP, MATCH_TIME = 5_000_000, 10
AREA_FIELDS = {"mode", "paths", "content", "checks"}
CHECK_FIELDS = {"name", "measure", "command", "report", "pins", "timeout", "expect_exit", "limit", "unit", "levels",
                "suppressed", "source"}
UNIT_OF = {"sarif": "findings", "junit": "failed tests", "date": "days"}


def _parser():
    """The stdlib regular-expression parser (re._parser from 3.11, sre_parse before)."""
    try:
        import re._parser as P  # noqa: F401  (3.11+)
        return P
    except ImportError:
        import sre_parse as P
        return P


def pattern_problem(p):
    """Why a content trigger can't be used ("" when it can). Patterns are kept small and simple so matching stays fast:
    no backreferences, no lookaround, no repeat inside a repeat."""
    if not isinstance(p, str) or not p or len(p) > MAX_PATTERN:
        return "a content pattern must be 1-%d characters" % MAX_PATTERN
    try:
        re.compile(p)
        tree = _parser().parse(p)
    except (re.error, OverflowError, RecursionError) as e:
        return "%r isn't a valid pattern (%s)" % (p[:40], str(e)[:60])
    P = _parser()
    bad = {getattr(P, n, None) for n in ("GROUPREF", "GROUPREF_EXISTS", "ASSERT", "ASSERT_NOT")} - {None}
    reps = {getattr(P, n, None) for n in ("MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT")} - {None}

    def walk(seq, inside):
        for op, av in seq:
            if op in bad:
                return "%r uses a backreference or lookaround; content patterns can't" % p[:40]
            many = op in reps and av[1] != 1
            if many and inside:
                return "%r repeats something that already repeats (like (a+)+), which can take very long; simplify it" % p[:40]
            for sub in _children(op, av, P):
                why = walk(sub, inside or many)
                if why:
                    return why
        return ""
    return walk(tree, False)


def _children(op, av, P):
    """The sub-patterns of one parsed node."""
    if op in {getattr(P, n, None) for n in ("MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT")}:
        return [av[2]]
    if op == P.SUBPATTERN:
        return [av[-1]]
    if op == P.BRANCH:
        return list(av[1])
    if op == getattr(P, "ATOMIC_GROUP", object()):
        return [av]
    return []


def _repo_glob(g):
    """A forward-slash glob that stays inside the repository."""
    return (isinstance(g, str) and 0 < len(g) <= 200 and "\\" not in g and not g.startswith("/") and ".." not in g.split("/")
            and "\0" not in g and not re.match(r"^[A-Za-z]:", g))


def check_problems(ch):
    """Why one area check can't be used ([] when it can). The command and its files follow the budget rules."""
    import sg_measure as M
    if not isinstance(ch, dict):
        return ["a check must be an object"]
    out = []
    extra = sorted(set(ch) - CHECK_FIELDS)
    if extra:
        out.append("unknown fields %s" % ", ".join(map(str, extra[:5])))
    if not isinstance(ch.get("name"), str) or not CHECK.fullmatch(ch["name"]):
        out.append("name must be 1-64 letters, digits, '_' or '-'")
    m = ch.get("measure")
    if m not in MEASURES:
        out.append('measure must be "sarif" (a SARIF report), "junit" (a JUnit XML report), "number" (the command prints '
                   'one number) or "date" (the command prints the date your prices were last checked)')
    lim = M._num(ch.get("limit", 0))
    if lim is None or lim < 0 or (m in ("sarif", "junit", "date") and lim != int(lim)):
        out.append("limit must be a whole number, 0 or more (the most findings, failed tests or days allowed)")
    unit = ch.get("unit")
    if unit is not None and (m != "number" or not isinstance(unit, str) or not M.UNIT.fullmatch(unit) or unit.lower() in M.TIME_UNITS):
        out.append("unit is only for number checks: a short word such as count or KB (not a time)")
    lv = ch.get("levels")
    if lv is not None and (m != "sarif" or not isinstance(lv, list) or not lv or not all(x in M.SARIF_LEVELS for x in lv)
                           or len(set(lv)) != len(lv)):
        out.append('levels is only for sarif checks: a list from "error", "warning", "note"')
    sp = ch.get("suppressed")
    if sp is not None and (m != "sarif" or sp not in ("count", "exclude")):
        out.append('suppressed is only for sarif checks: "count" (default: suppressed findings still count) or "exclude"')
    if m == "date" and not ch.get("pins"):
        out.append("a date check needs pins: the files that hold your prices and the script that reads their date, so a "
                   "change to them can't be checked by the same pull request")
    rec = {"name": "x", "unit": "count", "limit": 0, "status": "decided", "source": ch.get("source"),
           "measure": m if m in ("number", "junit") else "number", "command": ch.get("command"),
           "pins": ch.get("pins", []), "timeout": ch.get("timeout", M.DEFAULT_TIMEOUT)}
    if ch.get("expect_exit") is not None:
        rec["expect_exit"] = ch["expect_exit"]
    if m in ("sarif", "junit"):
        rec["measure"], rec["report"] = "junit", ch.get("report")  # the same rules for the report path
    elif ch.get("report") is not None:
        out.append("report is only for sarif and junit checks")
    out += [w.replace("a junit budget", "a %s check" % m) for w in M.problems(rec)
            if not w.startswith("name ") and not w.startswith("unit ")]
    return out


def area_problems(name, a):
    """Why one area can't be used ([] when it can)."""
    if not isinstance(a, dict):
        return ["must be an object"]
    out = []
    extra = sorted(set(a) - AREA_FIELDS)
    if extra:
        out.append("unknown fields %s" % ", ".join(map(str, extra[:5])))
    if a.get("mode", "warn") not in MODES:
        out.append('mode must be "off", "warn" or "enforce"')
    paths = a.get("paths", [])
    if not isinstance(paths, list) or len(paths) > MAX_TRIGGERS or not all(_repo_glob(g) for g in paths):
        out.append("paths must be a list of at most %d repository globs (forward slashes, no '..')" % MAX_TRIGGERS)
    content = a.get("content", [])
    if not isinstance(content, list) or len(content) > MAX_TRIGGERS:
        out.append("content must be a list of at most %d patterns" % MAX_TRIGGERS)
    else:
        out += [w for w in map(pattern_problem, content) if w]
    checks = a.get("checks")
    if a.get("mode") == "enforce" and isinstance(checks, list):
        bare = [c.get("name") for c in checks if isinstance(c, dict) and c.get("measure") in ("sarif", "junit") and not c.get("pins")]
        if bare:
            out.append("an enforced scanner or test-report check needs pins (the tool's config, ignore and baseline files): "
                       "%s. Without them the pull request could change what the tool looks at and still pass" % ", ".join(map(str, bare)))
    if not isinstance(checks, list) or not checks or len(checks) > MAX_CHECKS:
        out.append("checks must list 1 to %d checks (an area with no check can't show anything)" % MAX_CHECKS)
    else:
        seen = set()
        for ch in checks:
            bad = check_problems(ch)
            nm = ch.get("name") if isinstance(ch, dict) else None
            if bad:
                out.append("check %s: %s" % (nm if isinstance(nm, str) else "?", "; ".join(bad)))
            if nm in seen:
                out.append("check %s is listed twice" % nm)
            seen.add(nm)
    return out


def load(section):
    """The areas from a config's guardrails section, every optional field filled in. Raises ValueError (plain words)
    when anything is invalid: the gate fails closed."""
    if section in (None, {}):
        return {}
    if not isinstance(section, dict) or set(section) - {"areas"} or not isinstance(section.get("areas", {}), dict):
        raise ValueError('"guardrails" must look like {"areas": {"security": {...}}}')
    areas = section.get("areas", {})
    if len(areas) > MAX_AREAS:
        raise ValueError('"guardrails" has more than %d areas' % MAX_AREAS)
    out = {}
    for name, a in areas.items():
        if not isinstance(name, str) or not AREA.fullmatch(name):
            raise ValueError('area name %r must be 1-32 lowercase letters, digits or "-"' % str(name)[:40])
        bad = area_problems(name, a)
        if bad:
            raise ValueError("area %s: %s" % (name, "; ".join(bad)))
        out[name] = {"mode": a.get("mode", "warn"), "paths": list(a.get("paths", [])), "content": list(a.get("content", [])),
                     "checks": [_check(ch) for ch in a["checks"]]}
    return out


def _check(ch):
    """One valid check with every optional field filled in."""
    import sg_measure as M
    return {"name": ch["name"], "measure": ch["measure"], "command": list(ch["command"]), "report": ch.get("report"),
            "pins": sorted(set(ch.get("pins") or [])), "timeout": ch.get("timeout", M.DEFAULT_TIMEOUT),
            "expect_exit": ch.get("expect_exit"), "limit": ch.get("limit", 0),
            "unit": ch.get("unit") or UNIT_OF.get(ch["measure"], "count"),
            "levels": list(ch.get("levels") or ["error"]) if ch["measure"] == "sarif" else None,
            "suppressed": (ch.get("suppressed") or "count") if ch["measure"] == "sarif" else None, "source": ch["source"]}


def records(areas, only=None):
    """Engine records (sg_measure) for the checks of areas that aren't off: decided, at most the limit, ranked so enforce
    areas run before warn areas when CI's time is short. only: the area names that apply (None: all)."""
    out = []
    for name in sorted(areas):
        a = areas[name]
        if a["mode"] == "off" or (only is not None and name not in only):
            continue
        for ch in a["checks"]:
            out.append({"name": ch["name"], "unit": ch["unit"], "limit": ch["limit"], "direction": "max", "tolerance": 0,
                        "status": "decided", "source": ch["source"], "measure": ch["measure"], "command": ch["command"],
                        "report": ch["report"], "pins": ch["pins"], "paths": [], "timeout": ch["timeout"],
                        "expect_exit": ch["expect_exit"], "levels": ch["levels"], "suppressed": ch["suppressed"],
                        "_origin": "area", "_area": name, "_mode": a["mode"], "_key": "%s/%s" % (name, ch["name"]),
                        "_rank": 1 if a["mode"] == "enforce" else 2})
    return out


# ------------------------------------------------------------------ triggers
def changed_lines(diff, is_record):
    """(added and removed lines of a unified diff, outside story records, whole; complete). complete is False when the
    diff passed DIFF_CAP or changes a binary file, so content triggers can't be ruled out. Lines are never cut: a pattern past a cut would be missed."""
    out, keep, header, size = [], True, False, 0
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            parts = line.split(" b/", 1)
            keep, header = not (len(parts) == 2 and is_record(parts[1])), True
            continue
        if header:
            if line.startswith("+++ "):
                header = False
            elif keep and line.startswith(("Binary files ", "GIT binary patch")):
                return out, False  # a changed binary file: its content can't be matched, so patterns can't rule it out
            continue
        if keep and line[:1] in ("+", "-"):
            size += len(line)
            if size > DIFF_CAP:
                return out, False
            out.append(line[1:])
    return out, True


MATCHER = r"""
import json, re, sys
d = json.loads(sys.stdin.read())
out = {}
for area, pats in d["p"].items():
    rx = [re.compile(p) for p in pats]
    out[area] = any(r.search(l) for r in rx for l in d["l"])
print(json.dumps(out))
"""


def content_hits(patterns, lines, timeout=MATCH_TIME):
    """{area: True when a changed line matches one of its patterns}, matched in a separate Python with a time limit.
    When matching can't finish (time limit, error), every area with patterns counts as hit: a trigger story-gate
    couldn't evaluate never switches a check off. -> (hits, note)."""
    todo = {a: p for a, p in patterns.items() if p}
    if not todo:
        return {}, ""
    try:
        r = subprocess.run([sys.executable, "-I", "-c", MATCHER], input=json.dumps({"p": todo, "l": lines}),
                           capture_output=True, text=True, encoding="utf-8", timeout=timeout)
        hits = json.loads(r.stdout) if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        hits = None
    if not isinstance(hits, dict) or set(hits) != set(todo) or not all(isinstance(v, bool) for v in hits.values()):
        return {a: True for a in todo}, "content patterns couldn't be checked in %d seconds, so every area with patterns applies" % timeout
    return hits, ""


def applicable(areas, changed, diff, is_record):
    """({area: why it applies} for areas that aren't off, notes). changed: changed paths from the merge base (None when
    the diff couldn't be read: then every area applies). diff: the unified diff text from the merge base."""
    live = {n: a for n, a in areas.items() if a["mode"] != "off"}
    if changed is None:
        return {n: "story-gate couldn't read the change, so every area applies" for n in live}, []
    if not changed:
        return {}, []
    out, notes = {}, []
    lines, complete = changed_lines(diff or "", is_record)
    if not complete:
        notes.append("the change is larger than story-gate reads for content patterns, or changes a binary file; every area "
                     "with patterns applies")
    pend = {}
    for n, a in sorted(live.items()):
        if not a["paths"] and not a["content"]:
            out[n] = "it applies to every change"
            continue
        hit = sorted({g for f in changed for g in a["paths"] if fnmatch.fnmatchcase(f, g)})
        if hit:
            out[n] = "a changed file matches %s" % hit[0]
        elif a["content"]:
            pend[n] = a["content"]
    if pend:
        hits, note = content_hits(pend, lines) if complete else ({n: True for n in pend}, "")
        if note:
            notes.append(note)
        for n in pend:
            if hits.get(n):
                out[n] = "a changed line matches one of its patterns" if complete and not note else "its patterns couldn't be ruled out"
    return out, notes


# ------------------------------------------------------------------ the DONE check
def refresh(b, r):
    """A stored date result's age worked out again for today (an as-of date gets older every day)."""
    import sg_measure as M
    if b["measure"] == "date" and r.get("as_of") and r.get("state") != "not_run":
        n = M.age_days(r["as_of"])
        return dict(r, value=str(n)) if n is not None else dict(r, state="not_run", value=None, why="the stored date can't be read")
    return r


def label(b):
    """'security / scan (at most 0 error findings)' for the plain-English lines."""
    what = {"sarif": "%s findings (%s)" % (b["limit"], ", ".join(b.get("levels") or ["error"])),
            "junit": "%s failed tests" % b["limit"], "date": "%s days old" % b["limit"]}.get(b["measure"], "%s %s" % (b["limit"], b["unit"]))
    return "%s / %s (at most %s)" % (b["_area"], b["name"], what)


def verdict(rows, in_ci):
    """guardrails_met from [(record, result)] for the checks of areas that apply -> (PASS|WARN|FAIL, why).
    enforce: a check needs a value within its limit that CI checked (in CI), or that only lacks CI's run (on the agent's
    computer); anything else fails, whatever accept_concerns says. warn: problems are shown (WARN) and never block."""
    import sg_measure as M
    from decimal import Decimal
    fail, warn = [], []
    for b, r in rows:
        value = Decimal(r["value"]) if r.get("value") is not None and r.get("state") != "not_run" else None
        if value is None:
            why = "%s: not measured - %s" % (label(b), r.get("why") or "no result")
        elif not M.met(b, value):
            why = "%s: measured %s %s%s" % (label(b), r["value"], b["unit"], _suppressed_note(r))
        else:
            why = ""
            blockers = [w for w in (r.get("why") or "").split("; ") if w and w != "measured on the agent's computer"]
            if r.get("state") != "checked" if in_ci else blockers:
                why = "%s: CI can't vouch for this value (%s)" % (label(b), r.get("why") or r.get("state"))
        if why:
            (fail if b["_mode"] == "enforce" else warn).append(why)
    if fail:
        return "FAIL", "enforced guardrails not met: " + " | ".join(fail + [w + " (warn)" for w in warn])
    if warn:
        return "WARN", "guardrails to look at (warn: shown, never blocks): " + " | ".join(warn)
    return "PASS", ""


def _suppressed_note(r):
    """', 2 suppressed in the code (src/a.py, src/b.py)' for a SARIF result with suppressions, else ''."""
    if not r.get("suppressed"):
        return ""
    files = [f for f in (r.get("suppressed_in") or []) if isinstance(f, str)][:5]
    return ", %s suppressed in the code%s" % (r["suppressed"], " (%s)" % ", ".join(files) if files else "")


def summary_lines(rows, in_ci, why=None):
    """Markdown table for the CI summary and the agent: area, check, limit, value, state, result. why: {area: reason}."""
    import sg_measure as M
    from decimal import Decimal
    if not rows:
        return []
    word = {"checked": "checked by CI", "reported": "reported", "not_run": "not run"}
    out = ["", "### Guardrails (areas the owner turned on)", "", "| Area | Check | Limit | Measured | How sure | Result |",
           "|---|---|---|---|---|---|"]
    for b, r in rows:
        v = r.get("value")
        ok = v is not None and r.get("state") != "not_run"
        res = ("within" if M.met(b, Decimal(v)) else "OVER") if ok else "-"
        how = word.get(r.get("state"), "not run") + (" (%s)" % M.md(r["why"][:120]) if r.get("why") else "")
        meas = "-" if v is None else ("%s days (as of %s)" % (v, r.get("as_of")) if b["measure"] == "date" else "%s %s" % (v, b["unit"]))
        meas += M.md(_suppressed_note(r))
        out.append("| %s (%s) | %s | at most %s %s | %s | %s | %s |" % (b["_area"], b["_mode"], b["name"], b["limit"], b["unit"],
                                                                     meas, how, res))
    if why:
        out.append("")
        out += ["- %s applies: %s" % (n, w) for n, w in sorted(why.items())]
    if not in_ci:
        out.append("")
        out.append("On your computer every value is 'reported'. CI runs the default branch's checks again on the pull request.")
    return out


# ------------------------------------------------------------------ policy changes
def weaker(old, new):
    """Plain-English ways the `new` guardrails are looser than `old` (both from load()). Edits to globs or patterns are
    reported whenever any old one is gone: story-gate can't prove a new pattern covers as much."""
    out, rank = [], {"off": 0, "warn": 1, "enforce": 2}
    for name, o in sorted(old.items()):
        n = new.get(name)
        if n is None:
            if o["mode"] != "off":
                out.append("guardrail area %s removed" % name)
            continue
        if rank[n["mode"]] < rank[o["mode"]]:
            out.append("guardrail area %s lowered from %s to %s" % (name, o["mode"], n["mode"]))
        if o["mode"] == "off":
            continue
        trig_o, trig_n = bool(o["paths"] or o["content"]), bool(n["paths"] or n["content"])
        if trig_n and (not trig_o or set(o["paths"]) - set(n["paths"]) or set(o["content"]) - set(n["content"])):
            out.append("guardrail area %s applies to fewer changes (paths or content patterns)" % name)
        nc = {c["name"]: c for c in n["checks"]}
        for c in o["checks"]:
            d = nc.get(c["name"])
            k = "guardrail check %s/%s" % (name, c["name"])
            if d is None:
                out.append("%s removed" % k)
                continue
            if float(d["limit"]) > float(c["limit"]):
                out.append("%s limit raised from %s to %s" % (k, c["limit"], d["limit"]))
            if c["measure"] == "sarif" and d["measure"] == "sarif":
                if set(c["levels"]) - set(d["levels"]):
                    out.append("%s counts fewer levels (%s)" % (k, ", ".join(d["levels"])))
                if c["suppressed"] == "count" and d["suppressed"] == "exclude":
                    out.append("%s no longer counts findings suppressed in the code" % k)
            gone = sorted(set(c["pins"]) - set(d["pins"]))
            if gone:
                out.append("%s no longer pins %s" % (k, ", ".join(gone[:3])))
            same = lambda x: {f: x[f] for f in ("measure", "command", "report", "expect_exit", "timeout", "unit")}
            if same(c) != same(d):
                out.append("%s is measured differently (command, report, unit, exit rule or time limit)" % k)
    return out


def tighten(policy, local):
    """A computer's own guardrails setting may only raise an area's mode (warn to enforce); it adds or changes nothing else."""
    out = json.loads(json.dumps(policy or {}))
    la = (local or {}).get("areas") if isinstance(local, dict) else None
    if isinstance(la, dict) and isinstance(out.get("areas"), dict):
        rank = {"off": 0, "warn": 1, "enforce": 2}
        for name, a in out["areas"].items():
            mine = la.get(name)
            if isinstance(a, dict) and isinstance(mine, dict) and mine.get("mode") in rank and rank[mine["mode"]] > rank.get(a.get("mode", "warn"), 1) \
                    and a.get("mode", "warn") != "off":
                a["mode"] = mine["mode"]
    return out
