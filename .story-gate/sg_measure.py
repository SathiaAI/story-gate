"""Budgets: limits the owner sets on things a command can count (bundle size, failing accessibility tests, open warnings).

Design: docs/measured-checks.md. Owner's guide: docs/budgets.md.
  .story-gate/budgets.json                    the default branch's budgets (policy; changed through `settings budget ... --pr`)
  .story-gate/stories/<ID>/budgets.json       budgets a story proposes (always "proposed", never decide anything)
  .story-gate/stories/<ID>/budget_results.json  the last measurement of each budget for that story

A measurement has one of three states: "checked" (CI ran the default branch's command on this code, its pinned files
unchanged), "reported" (a value CI can't vouch for) or "not_run" (no usable value). Parsing and comparing always happen
here, in story-gate's own code, against the current policy: a stored value is evidence, a stored verdict is not.
Stdlib only.
"""
import fnmatch, hashlib, json, os, re, shutil, subprocess, time
from decimal import Decimal

SCHEMA = 1
PARSERS = {"number": "number-1", "junit": "junit-1"}
# Guardrail area checks (sg_areas.py) may also read a SARIF report or print a date. Budgets keep number and junit.
AREA_PARSERS = dict(PARSERS, sarif="sarif-1", date="date-1")
SARIF_LEVELS = ("error", "warning", "note")
OUTPUT_CAP = 5_000_000            # bytes of output (or report) a measurement may produce; more is not_run
MAX_BUDGETS, MAX_LIST, CI_TOTAL = 30, 20, 900
DEFAULT_TIMEOUT, MAX_TIMEOUT = 120, 600
STATES = ("checked", "reported", "not_run")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
UNIT = re.compile(r"[A-Za-z%][A-Za-z0-9%/_-]{0,15}")
TIME_UNITS = {"s", "sec", "secs", "second", "seconds", "ms", "msec", "millisecond", "milliseconds", "us", "ns", "m", "min",
              "mins", "minute", "minutes", "h", "hr", "hrs", "hour", "hours", "d", "day", "days"}
FIELDS = {"name", "unit", "limit", "direction", "tolerance", "status", "source", "measure", "command", "report", "pins",
          "paths", "timeout", "expect_exit"}
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-_]")
NUMBER = re.compile(r"-?(?:0|[1-9][0-9]{0,29})(?:\.[0-9]{1,12})?")


class PolicyError(ValueError):
    """The default branch's budgets.json can't be used; the gate fails closed."""


# ------------------------------------------------------------------ the budget records
def _repo_path(p):
    """A relative, forward-slash path that stays inside the repository (no '..', no drive, not absolute)."""
    return (isinstance(p, str) and 0 < len(p) <= 200 and "\\" not in p and not p.startswith("/") and not re.match(r"^[A-Za-z]:", p)
            and ".." not in p.split("/") and "\0" not in p)


def _num(x):
    """A JSON number (not true/false) as an exact Decimal, or None."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    d = Decimal(str(x))
    return d if d.is_finite() else None


def problems(b):
    """Why one budget record can't be used ([] when it can). Plain words, one per problem."""
    if not isinstance(b, dict):
        return ["not an object"]
    out = []
    extra = sorted(set(b) - FIELDS)
    if extra:
        out.append("unknown fields %s" % ", ".join(map(str, extra[:5])))
    if not isinstance(b.get("name"), str) or not NAME.fullmatch(b["name"]):
        out.append("name must be 1-64 letters, digits, '.', '_' or '-'")
    u = b.get("unit")
    if not isinstance(u, str) or not UNIT.fullmatch(u):
        out.append("unit must be a short word such as KB, count or %")
    elif u.lower() in TIME_UNITS:
        out.append("unit %r is a time: budgets count things, because timings change from run to run" % u)
    if _num(b.get("limit")) is None:
        out.append("limit must be a number")
    if b.get("direction", "max") not in ("max", "min"):
        out.append('direction must be "max" (at most the limit) or "min" (at least the limit)')
    t = _num(b.get("tolerance", 0))
    if t is None or t < 0:
        out.append("tolerance must be a number, 0 or more")
    if b.get("status") not in ("decided", "proposed"):
        out.append('status must be "decided" or "proposed"')
    s = b.get("source")
    if not isinstance(s, str) or not s.strip() or len(s) > 300:
        out.append("source must say who set the limit and why (1-300 characters)")
    if b.get("measure") not in PARSERS:
        out.append('measure must be "number" (the command prints one number) or "junit" (a JUnit XML report)')
    cmd = b.get("command")
    if not isinstance(cmd, list) or not cmd or len(cmd) > 50 or not all(isinstance(a, str) and a and len(a) <= 500 for a in cmd):
        out.append("command must be a list of words, the program first (no shell)")
    rep = b.get("report")
    if b.get("measure") == "junit":
        if not _repo_path(rep) or rep.startswith(".story-gate/") or rep.startswith(".git/"):
            out.append("a junit budget needs report: the repository path its command writes the XML to (an ignored file)")
    elif rep is not None:
        out.append("report is only for junit budgets")
    for k in ("pins", "paths"):
        v = b.get(k, [])
        if not isinstance(v, list) or len(v) > MAX_LIST or not all(_repo_path(x) for x in v):
            out.append("%s must be a list of at most %d repository paths (forward slashes, no '..')" % (k, MAX_LIST))
    tm = b.get("timeout", DEFAULT_TIMEOUT)
    if isinstance(tm, bool) or not isinstance(tm, int) or not 1 <= tm <= MAX_TIMEOUT:
        out.append("timeout must be 1 to %d seconds" % MAX_TIMEOUT)
    e = b.get("expect_exit")
    if e is not None and (isinstance(e, bool) or not isinstance(e, int)):
        out.append("expect_exit must be a whole number, or left out to accept any exit code")
    return out


def normal(b):
    """A valid budget with every optional field filled in."""
    return {"name": b["name"], "unit": b["unit"], "limit": b["limit"], "direction": b.get("direction", "max"),
            "tolerance": b.get("tolerance", 0), "status": b["status"], "source": b["source"], "measure": b["measure"],
            "command": list(b["command"]), "report": b.get("report"), "pins": sorted(set(b.get("pins") or [])),
            "paths": list(b.get("paths") or []), "timeout": b.get("timeout", DEFAULT_TIMEOUT), "expect_exit": b.get("expect_exit")}


def load(text, where="the default branch's .story-gate/budgets.json"):
    """Budgets from budgets.json text ([] when there is no file: None). Anything invalid, an empty file included, raises
    PolicyError: the gate fails closed."""
    if text is None:
        return []
    try:
        doc = json.loads(text)
    except ValueError as e:
        raise PolicyError("%s is not valid JSON (%s)" % (where, e.__class__.__name__))
    if not isinstance(doc, dict) or doc.get("version") != SCHEMA or not isinstance(doc.get("budgets"), list) or set(doc) - {"version", "budgets"}:
        raise PolicyError('%s must look like {"version": %d, "budgets": [...]}' % (where, SCHEMA))
    if len(doc["budgets"]) > MAX_BUDGETS:
        raise PolicyError("%s has more than %d budgets" % (where, MAX_BUDGETS))
    out, seen = [], set()
    for i, b in enumerate(doc["budgets"]):
        bad = problems(b)
        if bad:
            raise PolicyError("%s, budget %s: %s" % (where, b.get("name") if isinstance(b, dict) and isinstance(b.get("name"), str) else "#%d" % (i + 1), "; ".join(bad)))
        if b["name"] in seen:
            raise PolicyError("%s names budget %s twice" % (where, b["name"]))
        seen.add(b["name"])
        out.append(normal(b))
    return out


def story_budgets(doc):
    """A story's proposed budgets (from its budgets.json document). Invalid entries are dropped; all are 'proposed'."""
    bs = doc.get("budgets") if isinstance(doc, dict) else None
    out = []
    for b in (bs if isinstance(bs, list) else [])[:MAX_BUDGETS]:
        if isinstance(b, dict) and not problems(dict(b, status="proposed")):
            out.append(normal(dict(b, status="proposed")))
    return out


def policy_hash(budgets):
    """Fingerprint of the effective budgets, so a policy change makes stored DONE verdicts out of date."""
    return hashlib.sha256(json.dumps(budgets, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def spec_hash(b):
    """The identity of a measurement: what runs and how its output is read. A result only counts for this identity."""
    keep = {k: b.get(k) for k in ("measure", "command", "report", "pins", "expect_exit", "timeout")}
    keep["parser"] = AREA_PARSERS.get(b.get("measure"))
    for k in ("levels", "suppressed"):  # how a SARIF report is read (area checks only; budgets keep their old identity)
        if b.get(k) is not None:
            keep[k] = b[k]
    return hashlib.sha256(json.dumps(keep, sort_keys=True).encode("utf-8")).hexdigest()[:16]


# ------------------------------------------------------------------ which budgets apply, and can CI vouch for the command
def applies(b, changed):
    """True when the change touches files and the budget has no paths, or a changed path matches one of them."""
    if not changed:
        return False
    return not b["paths"] or any(fnmatch.fnmatchcase(f, g) for f in changed for g in b["paths"])


# Per interpreter: options whose next word is a value, not the script (a value that is a repository file is code too,
# and is pinned), and options that run code given inline or by module name (there is no script file to pin).
FAMILY = {"python": ({"-W", "-X", "-Q"}, {"-m", "-c"}),
          "node": ({"-r", "--require", "--import", "--loader", "--experimental-loader", "--env-file", "-C", "--conditions",
                    "--import-map", "--config"}, {"-e", "--eval", "-p", "--print"}),
          "shell": ({"-o", "+o", "-O"}, {"-c"}), "ruby": ({"-I", "-r"}, {"-e"}), "php": ({"-c", "-d"}, {"-r"}),
          "pwsh": ({"-File", "-file"}, {"-Command", "-command", "-c", "-EncodedCommand"}), "r": (set(), {"-e"})}
KIND = {"python": "python", "python3": "python", "py": "python", "node": "node", "deno": "node", "bun": "node", "bash": "shell",
        "sh": "shell", "zsh": "shell", "ruby": "ruby", "perl": "ruby", "php": "php", "pwsh": "pwsh", "powershell": "pwsh", "Rscript": "r"}
# Programs that run whatever the repository's own config or build files say (package.json scripts, a Makefile, ...).
RUNNERS = {"npm", "npx", "pnpm", "yarn", "make", "just", "rake", "gradle", "gradlew", "mvn", "mvnw", "cargo", "go", "dotnet",
           "tox", "nox", "uv", "poetry", "pipenv", "pytest", "jest", "vitest", "eslint", "tsc", "webpack", "vite"}


def _prog(cmd):
    """The interpreter family of a command (python, node, shell, ...), or None."""
    p = os.path.splitext(os.path.basename(cmd[0]))[0]
    return KIND.get(p) or ("python" if re.fullmatch(r"python3(?:\.\d+)?", p) else None)


def _script_args(cmd):
    """(words that may name the code an interpreter runs, True when it runs code given inline or by module name)."""
    values, opaque = FAMILY[_prog(cmd)]
    out, i = [], 1
    if os.path.splitext(os.path.basename(cmd[0]))[0] in ("deno", "bun") and len(cmd) > 1 and cmd[1] == "run":
        i = 2
    while i < len(cmd):
        a = cmd[i]
        if a in opaque:
            return out, True
        if a in values:
            out += cmd[i + 1:i + 2]
            i += 2
            continue
        if a.startswith("-"):
            if "=" in a:
                out.append(a.split("=", 1)[1])
            i += 1
            continue
        out.append(a)  # the script
        break
    return out, False


def auto_pins(b, root):
    """The code a budget runs, pinned without being listed: the program itself when it is a path (./scripts/count.sh),
    and after an interpreter the script plus any file an option loads (node -r ./setup.js scripts/size.js). Data the
    command measures is never pinned."""
    cmd = [a.replace("\\", "/") for a in b["command"]]
    cand = [cmd[0]] if os.path.dirname(cmd[0]) else []
    if _prog(cmd):
        cand += _script_args(cmd)[0]
    out, top = set(), os.path.realpath(root)
    for v in cand:
        if os.path.isabs(v) or re.match(r"^[A-Za-z]:", v):
            continue
        link = os.path.normpath(os.path.join(top, v))
        p = os.path.realpath(link)
        if os.path.isfile(p) and p.startswith(top + os.sep):
            out.add(os.path.relpath(p, top).replace(os.sep, "/"))
            if link.startswith(top + os.sep) and link != p:  # a symlink: repointing it changes the code as much as editing it
                out.add(os.path.relpath(link, top).replace(os.sep, "/"))
    return out


def unknown_code(b, root):
    """Why story-gate can't tell which repository files decide this measurement ("" when it can, or pins are listed)."""
    if b["pins"]:
        return ""
    cmd = [a.replace("\\", "/") for a in b["command"]]
    prog = os.path.splitext(os.path.basename(cmd[0]))[0]
    if prog in RUNNERS:
        return "%s runs whatever the repository's own files say (package.json, a Makefile, a config); list them in pins" % prog
    if _prog(cmd):
        _, opaque = _script_args(cmd)
        if opaque or not auto_pins(b, root):
            return "story-gate can't tell which repository file %s runs (inline code, a module name or no script); list it in pins" % prog
    return ""


def trust_problems(b, root, changed):
    """Why CI can't vouch for this budget's command on this change ([] when it can)."""
    out = []
    pins = set(b["pins"]) | auto_pins(b, root)
    hit = sorted(f for f in changed for p in pins if f == p or f.startswith(p.rstrip("/") + "/"))
    if hit:
        out.append("this change edits files that decide how it is measured (%s); change them in a pull request of their own"
                   % ", ".join(hit[:3]))
    why = unknown_code(b, root)
    if why:
        out.append(why)
    prog = b["command"][0]
    if not os.path.dirname(prog):
        exe = shutil.which(prog, path=os.environ.get("PATH"))
        if exe and os.path.realpath(exe).startswith(os.path.realpath(root) + os.sep):
            rel = os.path.relpath(os.path.realpath(exe), os.path.realpath(root)).replace(os.sep, "/")
            if not any(rel == p or rel.startswith(p.rstrip("/") + "/") for p in pins):
                out.append("the program %s is a repository file (%s) that isn't pinned" % (prog, rel))
    return out


# ------------------------------------------------------------------ parsers (exact, strict: anything odd is not_run)
def parse_number(text, unit):
    """(Decimal, "") when the output holds exactly one plain number (optionally with the unit glued on), else (None, why)."""
    words = [w for w in ANSI.sub("", text).split() if any(ch.isdigit() for ch in w)]
    if not words:
        return None, "the output has no number"
    if len(words) > 1:
        return None, ("the output has %d numbers (%s), so it's not clear which is the value; print only the value"
                      % (len(words), ", ".join(w[:20] for w in words[:3])))
    w = words[0]
    if unit and w.endswith(unit) and NUMBER.fullmatch(w[:-len(unit)]):
        w = w[:-len(unit)]
    if not NUMBER.fullmatch(w):
        return None, "%r is not a plain number in %s (no commas, exponents or other units)" % (words[0][:30], unit)
    return Decimal(w), ""


def parse_junit(raw):
    """(failed test cases as Decimal, test cases, "") from JUnit XML bytes, else (None, 0, why). Each testcase counts once."""
    import sg_github as G
    if len(raw) > OUTPUT_CAP:
        return None, 0, "the report is larger than 5 MB"
    if b"<!DOCTYPE" in raw[:2000].upper() or b"<!ENTITY" in raw:
        return None, 0, "the report declares a DTD or an entity"
    try:
        root = G._parse_xml_without_dtd(raw)
    except (ValueError, SyntaxError) as e:  # ET.ParseError is a SyntaxError; a refused DTD or entity is a ValueError
        return None, 0, "the report isn't readable XML (%s)" % e.__class__.__name__
    cases = list(root.iter("testcase"))
    if not cases:
        return None, 0, "the report has no test cases"
    failed = sum(1 for tc in cases if tc.find("failure") is not None or tc.find("error") is not None)
    return Decimal(failed), len(cases), ""


def _sarif_rules(run):
    """{(component, index) and ruleId: rule} for a SARIF run's driver (component -1) and its extensions."""
    tool = run.get("tool") if isinstance(run.get("tool"), dict) else {}
    comps = [tool.get("driver")] + (tool.get("extensions") if isinstance(tool.get("extensions"), list) else [])
    out = {}
    for ci, comp in enumerate(comps):
        rules = comp.get("rules") if isinstance(comp, dict) and isinstance(comp.get("rules"), list) else []
        for i, r in enumerate(rules):
            if isinstance(r, dict):
                out[(ci - 1, i)] = r
                if isinstance(r.get("id"), str) and ci == 0:
                    out.setdefault(r["id"], r)
    return out


def _sarif_level(res, rules):
    """A result's level: its own, else its rule's default configuration, else "warning" (SARIF 2.1.0, 3.27.10)."""
    if "level" in res:
        return res["level"]
    ref = res.get("rule") if isinstance(res.get("rule"), dict) else {}
    comp = ref.get("toolComponent", {}).get("index", -1) if isinstance(ref.get("toolComponent"), dict) else -1
    idx = ref.get("index", res.get("ruleIndex"))
    rule = rules.get((comp, idx)) if isinstance(idx, int) and not isinstance(idx, bool) else None
    if rule is None:
        rule = rules.get(ref.get("id") or res.get("ruleId"))
    cfg = (rule or {}).get("defaultConfiguration")
    return cfg.get("level", "warning") if isinstance(cfg, dict) else "warning"


def _suppressed(res):
    """True when a result carries a suppression that is in effect (status absent or "accepted"); None when malformed."""
    sup = res.get("suppressions", [])
    if not isinstance(sup, list) or not all(isinstance(x, dict) and x.get("kind") in ("inSource", "external")
                                            and x.get("status", "accepted") in ("accepted", "underReview", "rejected") for x in sup):
        return None
    return any(x.get("status", "accepted") == "accepted" for x in sup)


def parse_sarif(raw, levels=("error",), suppressed="count"):
    """(findings as Decimal, details, "") from SARIF 2.1.0 JSON bytes, else (None, {}, why).
    Counts results whose kind is "fail" (or absent) and whose level is in levels, from every run; results whose
    baselineState is "absent" (gone) never count. A result with a suppression in effect still counts unless the owner chose
    suppressed: "exclude" (the AI writing the code can add a suppression comment). A run the tool says failed, a missing
    results list, another SARIF version or anything malformed is not a usable report."""
    if len(raw) > OUTPUT_CAP:
        return None, {}, "the report is larger than 5 MB"
    try:
        doc = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError, RecursionError) as e:  # deeply nested JSON must not stop the gate
        return None, {}, "the report isn't readable SARIF JSON (%s)" % e.__class__.__name__
    if not isinstance(doc, dict) or doc.get("version") != "2.1.0" or not isinstance(doc.get("runs"), list) or not doc["runs"]:
        return None, {}, 'the report isn\'t SARIF 2.1.0 (it needs "version": "2.1.0" and a list of runs)'
    count = supp = total = 0
    where = []
    for run in doc["runs"]:
        if not isinstance(run, dict):
            return None, {}, "a run in the report isn't an object"
        inv = run.get("invocations", [])
        if not isinstance(inv, list) or not all(isinstance(i, dict) and isinstance(i.get("executionSuccessful"), bool) for i in inv):
            return None, {}, "the report's invocations are malformed (each needs executionSuccessful)"
        if any(i["executionSuccessful"] is False for i in inv):
            return None, {}, "the tool reported that its run failed (executionSuccessful: false)"
        results = run.get("results")
        if not isinstance(results, list):
            return None, {}, "a run has no results list, so the tool didn't finish its analysis"
        rules = _sarif_rules(run)
        for res in results:
            if not isinstance(res, dict):
                return None, {}, "a result in the report isn't an object"
            if res.get("kind", "fail") not in KINDS or res.get("baselineState", "new") not in BASELINE:
                return None, {}, "a result has an unknown kind or baselineState"
            if res.get("kind", "fail") != "fail" or res.get("baselineState") == "absent":
                continue
            level = _sarif_level(res, rules)
            if level not in SARIF_LEVELS + ("none",):
                return None, {}, "a result has an unknown level %r" % str(level)[:20]
            off = _suppressed(res)
            if off is None:
                return None, {}, "a result's suppressions are malformed"
            if level not in levels:
                continue
            total += 1
            if off:
                supp += 1
                if len(where) < 5:
                    where.append(_location(res))
                if suppressed == "exclude":
                    continue
            count += 1
    return Decimal(count), {"findings": total, "suppressed": supp, "suppressed_in": [w for w in where if w]}, ""


def _location(res):
    """The file a result points at, as plain path characters only (it is shown on the pull request), or ""."""
    try:
        uri = res["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
    except (KeyError, IndexError, TypeError):
        return ""
    return re.sub(r"[^A-Za-z0-9._/-]", "", uri if isinstance(uri, str) else "")[:100]


KINDS = ("pass", "open", "informational", "notApplicable", "review", "fail")
BASELINE = ("new", "unchanged", "updated", "absent")
DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2})(?::(\d{2})(?:\.\d{1,9})?)?(Z|[+-]\d{2}:\d{2})?)?")


def _today():
    """Today's date in UTC: the one clock every age is measured against."""
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).date()


def parse_date(text):
    """(the UTC calendar date, "") when the output is exactly one ISO-8601 date (2026-10-01) or date and time
    (2026-10-01T09:30Z, 2026-10-01T09:30:00+02:00; no zone means UTC), else (None, why). A future date is refused."""
    import datetime
    words = ANSI.sub("", text).split()
    if len(words) != 1:
        return None, ("the output has no date" if not words else "the output must be one date, like 2026-10-01 (it has %d words)" % len(words))
    m = DATE.fullmatch(words[0])
    if not m:
        return None, "%r is not a date like 2026-10-01 or 2026-10-01T09:30Z" % words[0][:30]
    y, mo, d, hh, mi, ss, zone = m.groups()
    try:
        t = datetime.datetime(int(y), int(mo), int(d), int(hh or 0), int(mi or 0), int(ss or 0), tzinfo=datetime.timezone.utc)
        if zone and zone != "Z":
            sign = 1 if zone[0] == "+" else -1
            off = datetime.timedelta(hours=int(zone[1:3]), minutes=int(zone[4:6]))
            if off >= datetime.timedelta(hours=24):
                raise ValueError("offset")
            t -= sign * off
    except (ValueError, OverflowError):
        return None, "%r is not a real date" % words[0][:30]
    if t.date() > _today():
        return None, "the date %s is in the future (UTC)" % t.date().isoformat()
    return t.date(), ""


def age_days(as_of):
    """Whole UTC calendar days from an ISO date (YYYY-MM-DD) to today; None when it can't be read or is in the future."""
    import datetime
    try:
        d = datetime.date.fromisoformat(str(as_of))
    except ValueError:
        return None
    n = (_today() - d).days
    return n if n >= 0 else None


def met(b, value):
    """True when value is within the budget's limit (plus tolerance), compared as exact decimals."""
    lim, tol = _num(b["limit"]), _num(b["tolerance"])
    return value <= lim + tol if b["direction"] == "max" else value >= lim - tol


# ------------------------------------------------------------------ running a measurement
def _remove(path, account):
    """Delete a file (or a link itself), as the throwaway account when it owns the folder (CI)."""
    try:
        os.remove(path)
    except PermissionError:
        if account is None:
            raise
        subprocess.run(account.wrap(["rm", "-f", "--", path]), capture_output=True, timeout=60)
        account.stop()


def run(b, root, work_state, timeout=None, account=None):
    """Run a budget's command once (sg_validation.execute: no shell, time limit, process tree killed; in CI as the
    throwaway account). Returns (run record, raw bytes to parse): the output, or the JUnit report the run wrote."""
    import sg_isolate as I, sg_validation as V
    before = work_state()
    rep = os.path.join(root, b["report"]) if b.get("report") else None
    inside = lambda p: os.path.realpath(p).startswith(os.path.realpath(root) + os.sep)
    if rep and os.path.islink(rep) and inside(os.path.dirname(rep)):
        _remove(rep, account)  # the link itself, never what it points to
    elif rep and os.path.isfile(rep) and inside(rep):
        _remove(rep, account)  # only a report this run writes can count
    t0, limit = time.time(), int(timeout or b["timeout"])
    code, out, size, note = V.execute(b["command"], root, limit, keep=OUTPUT_CAP + 1, max_bytes=OUTPUT_CAP, account=account)
    if note.startswith("stopped: the output passed"):
        note = "the output is larger than 5 MB"
    raw = out.encode("utf-8")
    if not note and size > OUTPUT_CAP:
        note = "the output is larger than 5 MB"
    if not note and code is None:
        note = "the command did not finish"
    if not note and code < 0 and b.get("expect_exit") is None:
        note = "the command was stopped by a signal (%s): a crash, so its output doesn't count" % code
    if not note and b.get("expect_exit") is not None and code != b["expect_exit"]:
        note = "exit code %s, expected %s" % (code, b["expect_exit"])
    if not note and rep:
        if os.path.islink(rep) or not os.path.isfile(rep) or not inside(rep):
            note = "the command did not write its report %s" % b["report"]
        else:
            raw = I.read_regular(rep, OUTPUT_CAP)
            if raw is None:
                note, raw = "story-gate couldn't read the report %s (not a plain readable file)" % b["report"], b""
            elif len(raw) > OUTPUT_CAP:
                note = "the report is larger than 5 MB"
    after = work_state()
    changed = sorted(n for n in set(before) | set(after) if before.get(n) != after.get(n))
    if changed:
        note = (note + "; " if note else "") + ("the run created or changed files in the repository (%s), so it doesn't count: "
                                                "write output to an ignored path" % ", ".join(changed[:5]))
    if not note:  # what gets stored or uploaded (the tests job's artifact) has known secret shapes removed; parsed the same everywhere
        raw = V.redact(raw.decode("utf-8", "replace")).encode("utf-8")
    return ({"spec": spec_hash(b), "exit_code": code, "seconds": round(time.time() - t0, 1), "note": note,
             "fingerprint": V.fingerprint_of(before), "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "output_tail": V.redact(out)[-V.TAIL:]}, b"" if note else raw)


def run_all(budgets, root, work_state, total=CI_TOTAL, account=None):
    """CI order: decided budgets, then enforce-area checks, then warn-area checks (their _rank), then proposed budgets,
    each by name, inside one time budget. -> {key: (record, raw)}; the key is _key (area checks) or the name."""
    out, t0 = {}, time.monotonic()  # a clock change can't stretch or cut the time
    for b in sorted(budgets, key=lambda x: (x.get("_rank", 0 if x["status"] == "decided" else 3), x.get("_key", x["name"]))):
        key = b.get("_key", b["name"])
        left = total - (time.monotonic() - t0)
        if left < 5:
            out[key] = ({"spec": spec_hash(b), "note": "not run: the %d-second time for all measured checks ran out" % total}, b"")
            continue
        out[key] = run(b, root, work_state, timeout=min(b["timeout"], int(left)), account=account)
    return out


def current(b, r, fingerprint):
    """A stored result if it was measured with this budget's command on this code, else a not_run result saying why."""
    r = r if isinstance(r, dict) else {}
    if not r:
        return {"state": "not_run", "value": None, "why": "not measured on this code"}
    if r.get("spec") != spec_hash(b):
        return dict(r, state="not_run", value=None, why="measured with another command or setup; measure it again")
    if r.get("fingerprint") != fingerprint:
        return dict(r, state="not_run", value=None, why="measured on other code; measure it again")
    if r.get("state") not in STATES or (r.get("value") is not None and not NUMBER.fullmatch(str(r["value"]))):
        return dict(r, state="not_run", value=None, why="the stored result is unreadable")
    return r


def evaluate(b, rec, raw, fingerprint, source, trust):
    """The result for one budget from one run record (or None). trust: why CI can't vouch for it ([] when it can).
    source: "ci" only for a run the story-gate job read from the tests job; everything else is "local"."""
    base = {"v": SCHEMA, "unit": b["unit"], "parser": AREA_PARSERS[b["measure"]], "spec": spec_hash(b), "source": source,
            "value": None, "state": "not_run", "why": ""}
    if not rec:
        return dict(base, why="not measured on this code")
    base.update({k: rec.get(k) for k in ("exit_code", "seconds", "fingerprint")})
    base.update({k: str(rec.get(k) or "")[:limit] for k, limit in (("at", 30), ("output_tail", 4000))})  # text, whatever arrived
    if rec.get("spec") != base["spec"]:
        return dict(base, why="measured with another command or setup; measure it again")
    if rec.get("fingerprint") != fingerprint:
        return dict(base, why="measured on other code; measure it again")
    if rec.get("note"):
        return dict(base, why=str(rec["note"])[:500])
    if b["measure"] == "junit":
        value, n, why = parse_junit(raw)
        base["tests"] = n
    elif b["measure"] == "sarif":
        value, info, why = parse_sarif(raw, tuple(b.get("levels") or ("error",)), b.get("suppressed") or "count")
        base.update(info)
    elif b["measure"] == "date":
        day, why = parse_date(raw.decode("utf-8", "replace"))
        value = None
        if day is not None:
            base["as_of"] = day.isoformat()
            value = Decimal(age_days(base["as_of"]))
    else:
        value, why = parse_number(raw.decode("utf-8", "replace"), b["unit"])
    if value is None:
        return dict(base, why=why)
    reasons = list(trust) + (["proposed by this story"] if b.get("_origin") == "story" else [])
    if source != "ci":
        reasons.append("measured on the agent's computer")
    return dict(base, value=str(value), state="reported" if reasons else "checked", why="; ".join(reasons))


# ------------------------------------------------------------------ the DONE check
def verdict(rows, in_ci):
    """budgets_met from [(budget, result)] for the budgets that apply. -> (PASS|CONCERNS|FAIL, why).
    A decided budget needs a value within its limit that CI checked (in CI) or that only lacks CI's run (locally).
    Anything else on a decided budget fails, whatever accept_concerns says. Proposed budgets raise CONCERNS."""
    fail, concern = [], []
    for b, r in rows:
        value = Decimal(r["value"]) if r.get("value") is not None and r.get("state") != "not_run" else None
        ok = value is not None and met(b, value)
        label = "%s (%s %s %s%s)" % (b["name"], "at most" if b["direction"] == "max" else "at least", b["limit"], b["unit"],
                                     ", tolerance %s" % b["tolerance"] if _num(b["tolerance"]) else "")
        if value is None:
            why = "%s: not measured - %s" % (label, r.get("why") or "no result")
        elif not ok:
            why = "%s: measured %s %s" % (label, r["value"], b["unit"])
        else:
            why = ""
        decided = b["status"] == "decided" and b.get("_origin") != "story"
        if decided:
            blockers = [w for w in (r.get("why") or "").split("; ") if w and w != "measured on the agent's computer"]
            if not why and (r.get("state") != "checked" if in_ci else blockers):
                why = "%s: CI can't vouch for this value (%s)" % (label, r.get("why") or r.get("state"))
            if why:
                fail.append(why)
        elif why:
            concern.append(why + " (proposed)")
    if fail:
        return "FAIL", "decided budgets not met: " + " | ".join(fail + concern)
    if concern:
        return "CONCERNS", "proposed budgets not met: " + " | ".join(concern)
    return "PASS", ""


# ------------------------------------------------------------------ policy changes
def weaker(old, new):
    """Plain-English ways the `new` budgets are looser than `old` (both lists from load())."""
    out, n = [], {b["name"]: b for b in new}
    for o in old:
        b = n.get(o["name"])
        name = o["name"]
        if b is None:
            if o["status"] == "decided":
                out.append("budget %s removed" % name)
            continue
        if o["status"] == "decided" and b["status"] != "decided":
            out.append("budget %s is no longer decided" % name)
        lo, ln = _num(o["limit"]), _num(b["limit"])
        if o["direction"] != b["direction"]:
            out.append("budget %s now counts the other way (%s)" % (name, b["direction"]))
        elif (ln > lo) if o["direction"] == "max" else (ln < lo):
            out.append("budget %s limit loosened from %s to %s %s" % (name, o["limit"], b["limit"], b["unit"]))
        if _num(b["tolerance"]) > _num(o["tolerance"]):
            out.append("budget %s tolerance raised from %s to %s" % (name, o["tolerance"], b["tolerance"]))
        if b["paths"] and (not o["paths"] or set(o["paths"]) - set(b["paths"])):  # no paths at all covers every change
            out.append("budget %s applies to fewer changes (paths)" % name)
        gone = sorted(set(o["pins"]) - set(b["pins"]))
        if gone:
            out.append("budget %s no longer pins %s" % (name, ", ".join(gone[:3])))
        if spec_hash(dict(o, pins=[])) != spec_hash(dict(b, pins=[])) or o["unit"] != b["unit"]:
            out.append("budget %s is measured differently (command, report, unit, exit rule or time limit)" % name)
    return out


# ------------------------------------------------------------------ plain-English output
def md(text):
    """Text from a run (or a report) made inert for a Markdown table: no links, images, code, mentions or new cells."""
    return re.sub(r"([\\`*_\[\]()<>!|#@~])", r"\\\1", str(text).replace("\r", " ").replace("\n", " "))


def summary_lines(rows, in_ci):
    """Markdown table for the CI summary and the agent: budget, limit, value, state, result."""
    if not rows:
        return []
    word = {"checked": "checked by CI", "reported": "reported", "not_run": "not run"}
    out = ["", "### Budgets (limits the owner set)", "", "| Budget | Limit | Measured | How sure | Result |", "|---|---|---|---|---|"]
    for b, r in rows:
        v = r.get("value")
        res = ("within" if met(b, Decimal(v)) else "OVER" if b["direction"] == "max" else "UNDER") if v is not None and r.get("state") != "not_run" else "-"
        how = word.get(r.get("state"), "not run") + (" (%s)" % md(r["why"][:120]) if r.get("why") else "")
        out.append("| %s%s | %s %s %s | %s | %s | %s |" % (
            b["name"], "" if b["status"] == "decided" and b.get("_origin") != "story" else " (proposed)",
            "at most" if b["direction"] == "max" else "at least", b["limit"], b["unit"],
            "%s %s" % (v, b["unit"]) if v is not None else "-", how, res))
    if not in_ci:
        out.append("")
        out.append("On your computer every value is 'reported'. CI measures the default branch's budgets again on the pull request.")
    return out
