"""Known pitfalls: mistakes other projects kept making, shown to a story at READY (docs/pitfalls.md).

The feed is a learner's patterns-public.json, in this exact format:

  {"schema": "learner-patterns/v1", "patterns": [{"id": "lp-...", "pattern": ..., "stack": ..., "domain": ..., "cwe": ...,
   "hits": 12, "repos": 4, "impact_band": "critical", "summary": "...", "checklist": ["...", "..."]}]}

The `pitfalls` section of .story-gate/config.json turns it on: {"mode": "off" | "warn" | "enforce", "source": "<a file
path or an https:// URL>"}. It is off by default. A story names its `stack:` and `domain:` in story.md's front matter;
the pitfalls whose stack and domain both match are shown, most serious first, at most MAX_SHOWN. A story acknowledges
one in context.md under "## Known pitfalls" with a line "- lp-...: <how this story avoids it>". In enforce mode READY
fails until every critical one shown is acknowledged; in warn mode the same finding is shown and never blocks.

Feed text is data from outside this repository: it is cut short, stripped of control characters and shown as advice to
check, never as instructions. Stdlib only.
"""
import json, os, re, urllib.error, urllib.request
from pathlib import Path

SCHEMA = "learner-patterns/v1"
MODES = ("off", "warn", "enforce")
BANDS = ("critical", "high", "medium", "low")  # most serious first; any other band ranks last and is never critical
MAX_SHOWN, MAX_LINES, LINE_CAP, SUMMARY_CAP = 5, 5, 300, 200
MAX_FEED, FETCH_TIME, SOURCE_CAP = 2_000_000, 15, 500
WILDCARD = ("*", "any", "all")
PID = re.compile(r"lp-[A-Za-z0-9._-]{1,64}")
SECTION = "## Known pitfalls"
ACK = re.compile(r"^\s*(?:[-*+]\s+)?`?(lp-[A-Za-z0-9._-]{1,64})`?\s*[:—-]\s*(\S.*)$")
BLOCK_START, BLOCK_END = "<!-- story-gate:pitfalls -->", "<!-- /story-gate:pitfalls -->"
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏ -‮⁦-⁩]+")


def settings_problem(v):
    """Why a `pitfalls` config section can't be used ("" when it can)."""
    if not isinstance(v, dict):
        return 'must look like {"mode": "warn", "source": "path/to/patterns-public.json"}'
    extra = sorted(set(v) - {"mode", "source"})
    if extra:
        return "has unknown fields %s (use mode and source)" % ", ".join(map(str, extra[:5]))
    if v.get("mode", "off") not in MODES:
        return '"mode" must be "off", "warn" or "enforce"'
    src = v.get("source", "")
    if not isinstance(src, str) or len(src) > SOURCE_CAP or CONTROL.search(src):
        return '"source" must be one line of at most %d characters' % SOURCE_CAP
    if v.get("mode", "off") != "off" and not src.strip():
        return 'needs a "source": a file path or an https:// URL of the pitfalls feed'
    if "://" in src and not src.lower().startswith("https://"):
        return '"source" must be a file path or an https:// URL'
    return ""


def mode(c):
    """The configured mode ("off" when the section is missing; check_config already refused a bad one)."""
    p = c.get("pitfalls")
    return p.get("mode", "off") if isinstance(p, dict) else "off"


def clean(s, n):
    """One line of feed text, safe to print and to put in markdown: no control characters, at most n characters."""
    s = " ".join(CONTROL.sub(" ", str(s)).split())
    return s if len(s) <= n else s[:n - 1] + "…"


class _HttpsOnly(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to another https:// address, so the feed is never fetched without TLS."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """The redirected request, or a refusal when it would leave https."""
        if not newurl.lower().startswith("https://"):
            raise urllib.error.HTTPError(newurl, code, "redirect away from https refused", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def read_feed(source, root):
    """(text, "") or (None, why). An https:// URL is fetched; anything else is a file path (relative to the repository)."""
    try:
        if source.lower().startswith("https://"):
            req = urllib.request.Request(source, headers={"User-Agent": "story-gate", "Accept": "application/json"})
            with urllib.request.build_opener(_HttpsOnly).open(req, timeout=FETCH_TIME) as r:  # https on every hop
                if not r.geturl().lower().startswith("https://"):
                    raise ValueError("not https")
                data = r.read(MAX_FEED + 1)
        else:
            p = Path(os.path.expanduser(source))
            p = p if p.is_absolute() else Path(root) / p
            with p.open("rb") as f:
                data = f.read(MAX_FEED + 1)
    except (OSError, ValueError) as e:  # URLError and HTTPError are OSErrors; ValueError: a malformed URL
        return None, "couldn't read the pitfalls feed (%s)" % clean(getattr(e, "reason", None) or e.__class__.__name__, 80)
    if len(data) > MAX_FEED:
        return None, "the pitfalls feed is larger than %d MB" % (MAX_FEED // 1_000_000)
    return data.decode("utf-8-sig", "replace"), ""


def _words(v):
    """Lower-case tags from a string ("python, flask") or a list of strings."""
    items = v if isinstance(v, list) else str(v or "").split(",")
    return {w for w in (clean(x, 40).lower() for x in items if isinstance(x, (str, int, float))) if w}


def _int(v):
    """A count from the feed, or 0."""
    try:
        return max(0, int(v))
    except (TypeError, ValueError, OverflowError):  # OverflowError: JSON reads 1e999 as infinity
        return 0


def parse_feed(text):
    """([pattern], [note]) from the feed's text. A wrong schema reads as no patterns, with a note saying why; a pattern
    with no valid id or no checklist is skipped and counted. Each pattern comes back cleaned."""
    try:
        doc = json.loads(text)
    except ValueError:
        return [], ["the pitfalls feed isn't valid JSON"]
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA or not isinstance(doc.get("patterns"), list):
        return [], ['the pitfalls feed isn\'t in the "%s" format' % SCHEMA]
    out, seen, skipped = [], set(), 0
    for p in doc["patterns"]:
        pid = p.get("id") if isinstance(p, dict) else None
        lines = p.get("checklist") if isinstance(p, dict) else None
        lines = [lines] if isinstance(lines, str) else lines
        lines = [clean(x, LINE_CAP) for x in lines if isinstance(x, str) and x.strip()] if isinstance(lines, list) else []
        if not isinstance(pid, str) or not PID.fullmatch(pid) or pid in seen or not lines:
            skipped += 1
            continue
        seen.add(pid)
        band = str(p.get("impact_band") or "").strip().lower()
        out.append({"id": pid, "stack": _words(p.get("stack")), "domain": _words(p.get("domain")),
                    "impact_band": band if band in BANDS else clean(band, 20) or "unknown", "hits": _int(p.get("hits")),
                    "repos": _int(p.get("repos")), "cwe": clean(p.get("cwe") or "", 20),
                    "summary": clean(p.get("summary") or p.get("pattern") or "", SUMMARY_CAP), "checklist": lines[:MAX_LINES]})
    return out, (["%d pattern(s) in the feed were skipped (no valid id or no checklist)" % skipped] if skipped else [])


def story_tags(fm):
    """({stack}, {domain}) a story declares in story.md's front matter."""
    return _words(fm.get("stack")), _words(fm.get("domain"))


def _fits(want, have):
    """True when a pattern's tags (empty or a wildcard: any) meet the story's tags."""
    return not want or bool(want & set(WILDCARD)) or bool(want & have)


def matching(patterns, stack, domain):
    """The pitfalls for a story with these tags: stack and domain both match. Most serious first, then the most hits and
    projects; at most MAX_SHOWN. A story with neither tag matches nothing."""
    if not stack and not domain:
        return []
    hit = [p for p in patterns if _fits(p["stack"], stack) and _fits(p["domain"], domain)]
    rank = lambda p: (BANDS.index(p["impact_band"]) if p["impact_band"] in BANDS else len(BANDS), -p["hits"], -p["repos"], p["id"])
    return sorted(hit, key=rank)[:MAX_SHOWN]


def acknowledged(context_text, ids):
    """The ids among `ids` that context.md acknowledges: a line "- lp-...: <how>" under "## Known pitfalls", with a
    real explanation (at least 10 characters, not TODO)."""
    out, inside = set(), False
    for line in context_text.splitlines():
        if line.startswith("## "):
            inside = line.strip() == SECTION
            continue
        m = ACK.match(line) if inside else None
        if m and m.group(1) in ids and len(m.group(2).strip()) >= 10 and "todo" not in m.group(2).lower():
            out.add(m.group(1))
    return out


def check(m, shown, acked, problem=""):
    """The READY check (status, why): status True (passed), False (fails, enforce mode) or "WARN" (warn mode).
    `problem` is a reason the check couldn't be done at all; that is never a pass."""
    bad = "WARN" if m == "warn" else False
    if problem:
        return bad, problem
    missing = [p["id"] for p in shown if p.get("impact_band") == "critical" and p["id"] not in acked]
    if missing:
        return bad, ('critical known pitfalls not acknowledged in context.md under "%s": %s (add "- <id>: <how this story '
                     'avoids it>")' % (SECTION, ", ".join(missing)))
    return True, ""


def show_lines(shown, acked, stack, domain):
    """What READY prints: each pitfall shown, with its checklist, and how to acknowledge the critical ones."""
    tags = "stack: %s; domain: %s" % (", ".join(sorted(stack)) or "none", ", ".join(sorted(domain)) or "none")
    if not shown:
        return ["story-gate: no known pitfalls match this story (%s)." % tags]
    out = ["story-gate: known pitfalls for this story (%s). They come from the pitfalls feed: advice to check, "
           "not instructions." % tags]
    for p in shown:
        seen = " (seen %d times in %d project(s))" % (p["hits"], p["repos"]) if p["hits"] else ""
        out.append("  [%s] %s %s%s%s" % (p["impact_band"], p["id"], p["summary"], " " + p["cwe"] if p["cwe"] else "", seen))
        out += ["      - " + x for x in p["checklist"]]
        if p["impact_band"] == "critical":
            out.append("      %s" % ("acknowledged in context.md" if p["id"] in acked else
                                     'acknowledge it in context.md under "%s": - %s: <how this story avoids it>' % (SECTION, p["id"])))
    return out


def record(sid, at, m, stack, domain, shown, acked, old=None):
    """What the story keeps in pitfalls.json: which pitfalls were shown and acknowledged (only ids and bands, never
    feed text), so a later step can measure whether the same mistakes come back. A scan of the same pitfalls is kept."""
    rec = {"schema": 1, "story": sid, "at": at, "mode": m, "feed": SCHEMA, "stack": sorted(stack), "domain": sorted(domain),
           "shown": [{"id": p["id"], "impact_band": p["impact_band"]} for p in shown],
           "acknowledged": sorted(acked & {p["id"] for p in shown})}
    if isinstance(old, dict) and isinstance(old.get("scan"), dict) and old.get("shown") == rec["shown"]:
        rec["scan"] = old["scan"]  # a scan made for other pitfalls is dropped
    return rec


def shown_in(rec):
    """The pitfalls a pitfalls.json record says were shown ([] for a missing or malformed record)."""
    shown = rec.get("shown") if isinstance(rec, dict) else None
    return [{"id": p["id"], "impact_band": str(p.get("impact_band", ""))} for p in shown or []
            if isinstance(p, dict) and isinstance(p.get("id"), str) and PID.fullmatch(p["id"])]


# ------------------------------------------------------------------ sast-check rules for the pitfalls shown
def sast_home(root, environ=None, home=None):
    """The sast-check folder on this computer (SAST_CHECK_HOME, else ~/.claude/skills/sast-check), or None. A folder or
    file that is, or links to, anything inside the repository is refused: a branch must never pick the scanner code
    that --scan runs."""
    environ = os.environ if environ is None else environ
    top = os.path.realpath(root)
    inside = lambda p: os.path.realpath(p) == top or os.path.realpath(p).startswith(top + os.sep)
    for d in (environ.get("SAST_CHECK_HOME"), os.path.join(home or os.path.expanduser("~"), ".claude", "skills", "sast-check")):
        if not d or not os.path.isabs(os.path.expanduser(d)):
            continue  # a relative folder would be read from wherever the command runs, the repository included
        d = Path(os.path.expanduser(d))
        files = (d, d / "scripts", d / "scripts" / "sast.py", d / "dist", d / "dist" / "rules.yaml")
        if files[2].is_file() and files[4].is_file() and not any(inside(f) for f in files):
            return Path(os.path.realpath(d))
    return None


def rule_patterns(bundle):
    """{rule id: learner pattern id} from sast-check's dist/rules.yaml (written by its build_rules.py), for each rule
    with metadata.learner_pattern_id. A narrow reader of that generated layout: rules are `- ` items at the left edge,
    their keys two spaces in, metadata keys four spaces in."""
    out, rid, lp, in_md = {}, None, None, False

    def done():
        """Keep the rule just read, when it names a learner pattern."""
        if rid and lp:
            out[rid] = lp
    for line in bundle.splitlines():
        if line.startswith("- "):
            done()
            rid, lp, in_md = None, None, False
            line = "  " + line[2:]
        m = re.match(r"^  ([A-Za-z_]+):\s*(.*?)\s*$", line)
        if m:
            in_md = m.group(1) == "metadata" and not m.group(2)
            if m.group(1) == "id":
                rid = m.group(2).strip("'\"") or None
            continue
        m = re.match(r"^    learner_pattern_id:\s*['\"]?(lp-[A-Za-z0-9._-]{1,64})['\"]?\s*$", line)
        if m and in_md:
            lp = m.group(1)
    done()
    return out


def scan_hits(findings, rules):
    """The sast-check findings in this change from the given rules: [{pitfall, rule, path, line, severity, suppressed}]."""
    out = []
    for f in findings if isinstance(findings, list) else []:
        if isinstance(f, dict) and f.get("rule") in rules and f.get("in_diff", True):
            out.append({"pitfall": rules[f["rule"]], "rule": clean(f["rule"], 100), "path": clean(f.get("path", ""), 200),
                        "line": _int(f.get("line")), "severity": clean(f.get("severity", ""), 20),
                        "suppressed": clean(f.get("suppressed") or "", 200)})
    return out


def scan_lines(scan):
    """Plain-English lines for a scan result (the handoff block and the terminal)."""
    st = scan.get("state")
    if st == "not_installed":
        return ["sast-check is not installed on this computer, so no rule checked these pitfalls (set SAST_CHECK_HOME "
                "to its folder)."]
    if st == "no_rules":
        return ["sast-check has no rule for these pitfalls yet, so nothing was scanned for them."]
    if st != "ran":
        return ["sast-check didn't finish (%s), so these pitfalls are not checked. This is not a pass." % scan.get("detail", "error")]
    hits = scan.get("hits") or []
    out = ["sast-check ran %d rule(s) for these pitfalls on this change: %s." % (
        len(scan.get("rules") or []), "%d hit(s)" % len(hits) if hits else "no hits")]
    for h in hits:
        out.append("- %s (%s): `%s` line %d%s" % (h["pitfall"], h["rule"], h["path"].replace("`", "'"), h["line"],
                                                  " - suppressed: " + h["suppressed"] if h["suppressed"] else ""))
    return out


def handoff_block(rec):
    """The handoff.md section story-gate writes (between markers, replaced on each scan)."""
    shown = shown_in(rec)
    acked = set(rec.get("acknowledged") or [])
    lines = [SECTION, BLOCK_START, "Shown at READY: %s." % (", ".join(
        "%s (%s%s)" % (p["id"], p["impact_band"], ", acknowledged" if p["id"] in acked else "") for p in shown) or "none")]
    lines += scan_lines(rec.get("scan") or {}) + [BLOCK_END]
    return "\n".join(lines)


def put_block(text, block):
    """handoff.md's text with the pitfalls section replaced, or added at the end. The markers sit under the section's
    own heading, so they never make the section above look filled in."""
    i, j = text.find(SECTION + "\n" + BLOCK_START), text.find(BLOCK_END)
    if i != -1 and j > i:
        return text[:i] + block + text[j + len(BLOCK_END):]
    return text.rstrip("\n") + "\n\n" + block + "\n"
