"""Reading a story.md: front matter, sections, and the dependency / risk / blocker sections (D1). Pure functions: no git,
no files. gate.py and sg_dashboard.py both use this, so a story means the same thing everywhere.

A link between stories is stored once, on the waiting story (`depends_on` plus its `## Dependencies` section).
"Blocks" is worked out from the other side (blocks_of), never stored."""
import re

WHENS = ("before-start", "before-merge")
LEVELS = ("low", "medium", "high")
STATUSES = ("open", "resolved")
_EXAMPLES = {
    "Dependencies": "### SAT-9 / when: before-start / why: needs the export API that SAT-9 adds",
    "Risks": "### R1: CSV files over 1 GB time out / likelihood: medium / impact: high / mitigation: stream rows",
    "Blockers": "### B1: waiting on the S3 bucket name / owner: Paul / status: open",
}


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
    """{'## Heading': body text} for every '## ' heading in a markdown file."""
    out, cur = {}, None
    for line in text.splitlines():
        if line.startswith("## "):
            cur = line.strip()
            out[cur] = ""
        elif cur:
            out[cur] += line + "\n"
    return out


def depends_on_ids(fm):
    """The story IDs in a front matter's depends_on: a list, or one string that is not 'none'. Anything else is []."""
    deps = fm.get("depends_on") or []
    return [deps] if isinstance(deps, str) and deps and deps.lower() != "none" else (deps if isinstance(deps, list) else [])


def natural_key(sid):
    """Sort key so SAT-2 comes before SAT-10."""
    return [(0, int(p), "") if p.isdigit() else (1, 0, p.lower()) for p in re.split(r"(\d+)", str(sid)) if p]


def has_section(story_text, name):
    """True when story.md has a '## <name>' heading (any capitals)."""
    return _find(story_text.splitlines(), name)[0] is not None


def _find(lines, name):
    """(index of the '## name' heading or None, index of the next '## ' heading or len(lines)). The LAST such heading
    counts: story-gate's own sections come after the word-for-word story, which may contain the same heading."""
    want = "## " + name.lower()
    start = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].startswith("## ") and lines[i].strip().lower() == want), None)
    if start is None:
        return None, len(lines)
    return start, next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))


def _blocks(story_text, name, keys):
    """Read one section. -> (present, [(heading, line, {key: (value, line)})], errors). Every error names the line in
    story.md and shows a correct example. A section is `None` or one or more '### ' blocks of 'key: value' lines."""
    lines = story_text.splitlines()
    start, end = _find(lines, name)
    if start is None:
        return False, [], []
    ex = " Example: " + _EXAMPLES[name]
    errors, blocks, cur, saw_none = [], [], None, 0

    def err(i, msg):
        errors.append("story.md line %d, ## %s: %s.%s" % (i + 1, name, msg, ex))

    for i in range(start + 1, end):
        raw = lines[i]
        text = raw.strip()
        if raw.startswith("### ") or text == "###":
            title = text[3:].strip()
            if not title:
                err(i, "a '###' heading needs a name")
            cur = (title, i + 1, {})
            blocks.append(cur)
        elif not text:
            continue
        elif cur is None:
            if re.match(r"none(?![A-Za-z0-9_])", text, re.I) and not saw_none:
                saw_none = i + 1
            else:
                err(i, "write only 'None' or '### ' blocks here, not %r (if this heading came from the pasted story, add "
                       "story-gate's own '## %s' section at the end of story.md)" % (text[:40], name))
        elif ":" not in text:
            err(i, "expected 'key: value', got %r" % text[:40])
        else:
            k, v = text.split(":", 1)
            k, v = k.strip().lower(), v.strip()
            if k not in keys:
                err(i, "unknown key %r (use: %s)" % (k, ", ".join(keys)))
            elif k in cur[2]:
                err(i, "%r appears twice in %r" % (k, cur[0]))
            elif not v:
                err(i, "%r has no value" % k)
            else:
                cur[2][k] = (v, i + 1)
    if saw_none and blocks:
        errors.append("story.md line %d, ## %s: it says None but also lists entries; remove one.%s" % (saw_none, name, ex))
    if not saw_none and not blocks and not errors:
        errors.append("story.md line %d, ## %s: write 'None' if there is nothing to list.%s" % (start + 1, name, ex))
    return True, blocks, errors


def _need(name, blk, keys, errors):
    """Required keys present? (errors get one line per missing key.)"""
    for k in keys:
        if k not in blk[2]:
            errors.append("story.md line %d, ## %s: %r needs '%s:'. Example: %s" % (blk[1], name, blk[0], k, _EXAMPLES[name]))


def parse_dependencies(story_text, id_pattern):
    """-> (records, errors, is_d1). records: {story id: {"when": "before-start"|"before-merge", "why": text, "line": n}}.
    is_d1 is True when story.md has a '## Dependencies' heading; a story without one is a legacy story and the caller
    treats each depends_on ID as before-start. A malformed D1 section gives errors, never a silent legacy reading."""
    present, blocks, errors = _blocks(story_text, "Dependencies", ("when", "why"))
    recs = {}
    pat = re.compile(r"(?:%s)" % id_pattern)
    for blk in blocks:
        sid = blk[0]
        if not pat.fullmatch(sid):
            errors.append("story.md line %d, ## Dependencies: %r is not a story ID. Example: %s" % (blk[1], sid[:40], _EXAMPLES["Dependencies"]))
            continue
        if sid in recs:
            errors.append("story.md line %d, ## Dependencies: %s is listed twice. Example: %s" % (blk[1], sid, _EXAMPLES["Dependencies"]))
            continue
        _need("Dependencies", blk, ("when", "why"), errors)
        when = blk[2].get("when", ("", 0))
        if when[0] and when[0].lower() not in WHENS:
            errors.append("story.md line %d, ## Dependencies: when must be before-start or before-merge, not %r. Example: %s"
                          % (when[1], when[0][:30], _EXAMPLES["Dependencies"]))
        elif when[0] and "why" in blk[2]:
            recs[sid] = {"when": when[0].lower(), "why": blk[2]["why"][0], "line": blk[1]}
    return recs, errors, present


def _levels(name, blk, keys, errors):
    for k in keys:
        if k in blk[2] and blk[2][k][0].lower() not in (LEVELS if k != "status" else STATUSES):
            errors.append("story.md line %d, ## %s: %s must be %s, not %r. Example: %s" % (
                blk[2][k][1], name, k, " | ".join(LEVELS if k != "status" else STATUSES), blk[2][k][0][:30], _EXAMPLES[name]))


def parse_risks(story_text):
    """-> (risks, errors). Each risk: {"id": "R1", "title", "likelihood", "impact" (low|medium|high), "mitigation" ("" when
    none), "line"}. No '## Risks' section, or `None`, gives ([], [])."""
    _, blocks, errors = _blocks(story_text, "Risks", ("likelihood", "impact", "mitigation"))
    out = []
    for blk in blocks:
        m = re.fullmatch(r"(R\d+):\s*(\S.*)", blk[0])
        if not m:
            errors.append("story.md line %d, ## Risks: heading %r must look like 'R1: short title'. Example: %s" % (blk[1], blk[0][:40], _EXAMPLES["Risks"]))
            continue
        _need("Risks", blk, ("likelihood", "impact"), errors)
        _levels("Risks", blk, ("likelihood", "impact"), errors)
        out.append({"id": m.group(1), "title": m.group(2), "line": blk[1], "mitigation": blk[2].get("mitigation", ("", 0))[0],
                    "likelihood": blk[2].get("likelihood", ("", 0))[0].lower(), "impact": blk[2].get("impact", ("", 0))[0].lower()})
    return out, errors


def parse_blockers(story_text):
    """-> (blockers, errors). Each blocker: {"id": "B1", "title", "owner", "status" ("open"|"resolved"), "line"}.
    No '## Blockers' section, or `None`, gives ([], [])."""
    _, blocks, errors = _blocks(story_text, "Blockers", ("owner", "status"))
    out = []
    for blk in blocks:
        m = re.fullmatch(r"(B\d+):\s*(\S.*)", blk[0])
        if not m:
            errors.append("story.md line %d, ## Blockers: heading %r must look like 'B1: short title'. Example: %s" % (blk[1], blk[0][:40], _EXAMPLES["Blockers"]))
            continue
        _need("Blockers", blk, ("owner", "status"), errors)
        _levels("Blockers", blk, ("status",), errors)
        out.append({"id": m.group(1), "title": m.group(2), "line": blk[1], "owner": blk[2].get("owner", ("", 0))[0],
                    "status": blk[2].get("status", ("", 0))[0].lower()})
    return out, errors


def graph_cycles(edges, through=None):
    """Cycles in {story id: set of the ids it waits on}. -> list of cycles, each a list of ids in order (the last one waits
    on the first; a story that waits on itself is [id]). One cycle per group of stories that wait on each other, found
    from the lowest ID (or from `through`, which then returns only the cycle through that story), shortest path first.
    Deterministic: same input, same output."""
    nodes = sorted(set(edges) | {d for ds in edges.values() for d in ds}, key=natural_key)
    nxt = {n: sorted(edges.get(n) or (), key=natural_key) for n in nodes}
    reach = {}
    for n in nodes:  # everything n waits on, directly or not
        seen, todo = set(), list(nxt[n])
        while todo:
            x = todo.pop()
            if x not in seen:
                seen.add(x)
                todo.extend(nxt[x])
        reach[n] = seen
    out, done = [], set()
    for n in nodes:
        if n in done or n not in reach[n]:
            continue
        group = {m for m in reach[n] if n in reach[m]} | {n}
        done |= group
        start = through if through in group else n
        if through is not None and through not in group:
            continue
        prev, queue, found = {start: None}, [start], None
        while queue and found is None:  # shortest way back to start, inside the group
            cur = queue.pop(0)
            for m in nxt[cur]:
                if m == start:
                    found = cur
                    break
                if m in group and m not in prev:
                    prev[m] = cur
                    queue.append(m)
        path = []
        while found is not None:
            path.append(found)
            found = prev[found]
        out.append(path[::-1])
    return sorted(out, key=lambda cy: natural_key(cy[0]))


def blocks_of(edges, sid):
    """The stories that wait on `sid` (derived from the waiting side, never stored), sorted naturally."""
    return sorted((s for s, ds in edges.items() if sid in ds and s != sid), key=natural_key)


def topo_order(edges, ids):
    """`ids` in build order: a story comes after everything it waits on (only links among `ids` count). Ties go to the
    lowest ID (SAT-2 before SAT-10). Stories stuck in a cycle come last, sorted."""
    left = set(ids)
    out = []
    while True:
        ready = sorted((s for s in left if not (set(edges.get(s) or ()) & left)), key=natural_key)
        if not ready:
            break
        out.append(ready[0])
        left.discard(ready[0])
    return out + sorted(left, key=natural_key)
