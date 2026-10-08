"""story-gate spec-pull: copy a GitHub issue (a Matt Pocock ticket or spec, or any plan kept on an issue) into the story's
folder as a snapshot file. The gate then checks the snapshot, which is reviewed in the pull request like any other file;
the live issue is only ever read here, on request, never by the gate itself.

Rules: only this repository's issues (its 'origin' remote on github.com); the issue text is data, never run; the file
records where it came from and a hash of its text, so an edit by hand is caught (spec_snapshot_problem in gate.py)."""
import os, re, sys, tempfile
from pathlib import Path

MAX_BODY = 256_000          # characters: an issue body is at most 65,536 on GitHub; anything bigger isn't one
KIND = "github-issue"
NOTE = "<!-- story-gate snapshot: don't edit by hand. Run `story-gate spec-pull %s %s` again to update it. -->"


def parse_ref(ref, own):
    """(repo, number) for '42', '#42', 'owner/repo#42' or 'https://github.com/owner/repo/issues/42'; None if unreadable."""
    ref = (ref or "").strip()
    m = re.fullmatch(r"#?(\d{1,9})", ref)
    if m:
        return own, int(m.group(1))
    m = re.fullmatch(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d{1,9})", ref)
    if m:
        return m.group(1), int(m.group(2))
    m = re.fullmatch(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/issues/(\d{1,9})/?(?:#.*)?", ref)
    if m:
        return m.group(1), int(m.group(2))
    return None


def clean(s):
    """Text that can be written as UTF-8: a lone surrogate (broken input) becomes '?' instead of crashing the write."""
    return str(s or "").encode("utf-8", "replace").decode("utf-8")


def one_line(s, n=300):
    return re.sub(r"\s+", " ", clean(s)).strip()[:n]


def snapshot_content(sid, num, title, body):
    """The text part of the copy: a do-not-edit note, '# title', then the issue body. CI rebuilds it from the live issue
    the same way, so 'unchanged' means exactly the same text."""
    return "%s\n\n# %s\n\n%s\n" % (NOTE % (sid, "#%d" % num), one_line(title) or "Issue %d" % num,
                                     clean(body).replace("\r\n", "\n").replace("\r", "\n").rstrip())


def snapshot_text(gate, sid, ref_arg, meta, title, body):
    """The file: the source block, then snapshot_content. content_sha256 covers the source fields and the text together
    (gate.snapshot_hash), so neither can be changed without the check failing."""
    content = snapshot_content(sid, int(meta["issue"]), title, body)
    fields = {k: one_line(meta[k]) for k in gate.SNAPSHOT_KEYS}
    fields["content_sha256"] = gate.snapshot_hash(fields, content)
    return "---\n%s\n---\n%s" % ("\n".join("%s: %s" % kv for kv in fields.items()), content)


def link(story_md, rel):
    """Add rel to story.md's 'spec:' front matter line (made a list when there are several). Returns the new text."""
    text = story_md
    m = re.match(r"(\s*---\s*\n)(.*?)(\n---)", text, re.S)
    if not m:
        return None
    lines = m.group(2).split("\n")
    for i, ln in enumerate(lines):
        if re.match(r"\s*spec\s*:", ln):
            v = ln.split(":", 1)[1].strip()
            cur = [x.strip().strip("'\"") for x in v[1:-1].split(",") if x.strip()] if v.startswith("[") else ([v] if v and v.lower() != "none" else [])
            if rel not in cur:
                cur.append(rel)
            lines[i] = "spec: " + (cur[0] if len(cur) == 1 else "[%s]" % ", ".join(cur))
            break
    else:
        lines.append("spec: " + rel)
    return text[:m.start(2)] + "\n".join(lines) + text[m.end(2):]


def cli(gate, args):
    kv, rest = gate.flags(args)
    usage = ("usage: spec-pull <ID> <issue>   (42, #42, owner/repo#42 or the issue's URL; this repository's issues only)\n"
             "       spec-pull <ID> <issue> --from-file <file or ->   (text you already have, e.g. from your AI tool; marked "
             "'not verified')")
    if len(rest) != 2:
        sys.exit(usage)
    sid, ref_arg = rest
    sd = gate.sdir(sid)
    root = Path(os.path.realpath(gate.STORIES))
    for p in (gate.STORIES, sd, sd / "story.md"):  # never write through a link, or outside the repository
        rp = os.path.realpath(p)
        if p.is_symlink() or not (rp == str(root) or rp.startswith(str(root) + os.sep)):
            sys.exit("story-gate: %s is a link (or leads outside the repository); story-gate doesn't write through links."
                     % p.relative_to(gate.ROOT).as_posix())
    if not (sd / "story.md").is_file():
        sys.exit("story-gate: story %s has no story.md yet. Run `%s` first." % (sid, gate.gate_cmd("start " + sid)))
    own = gate.origin_repo()
    if not own:
        sys.exit("story-gate: this repository's 'origin' remote isn't on github.com, so there's no issue to pull. Copy the "
                 "ticket into the repository and link it with `spec:` instead.")
    parsed = parse_ref(ref_arg, own)
    if not parsed:
        sys.exit("story-gate: '%s' isn't an issue reference.\n%s" % (ref_arg[:120], usage))
    repo, num = parsed
    if repo.lower() != own.lower():
        sys.exit("story-gate: only this repository's issues (%s) can be pulled; %s is another repository. Its text "
                 "isn't covered by this repository's reviews." % (own, repo))
    url = "https://github.com/%s/issues/%d" % (own, num)
    if kv.get("from-file"):
        src = kv["from-file"]
        try:
            raw = sys.stdin.read() if src == "-" else Path(src).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            sys.exit("story-gate: can't read %s: %s" % (src, e))
        title, body = kv.get("title", ""), raw
        meta = {"source_kind": KIND, "source_url": url, "repo": own, "issue": num, "source_updated_at": "unknown",
                "fetched_at": gate.now(), "fetched_by": "pasted"}
    else:
        import sg_github as G
        st, data, _ = G.call("GET", "/repos/%s/issues/%d" % (own, num), G.human_token())
        if st in (401, 403):
            sys.exit("story-gate: GitHub refused (HTTP %s). Sign in with `gh auth login`, or set GH_TOKEN, then try again." % st)
        if st == 404:
            sys.exit("story-gate: issue #%d wasn't found in %s (or this login can't see it)." % (num, own))
        if st != 200 or not isinstance(data, dict):
            sys.exit("story-gate: GitHub answered HTTP %s; nothing was saved. Try again." % st)
        if "pull_request" in data:
            sys.exit("story-gate: #%d is a pull request, not an issue." % num)
        if str(data.get("html_url") or "").lower().rstrip("/") != url.lower():  # e.g. moved to another repository
            sys.exit("story-gate: GitHub answered with %s, not %s (was the issue moved?). Nothing was saved."
                     % (one_line(data.get("html_url"), 120) or "another issue", url))
        title, body = data.get("title") or "", data.get("body") or ""
        meta = {"source_kind": KIND, "source_url": url, "repo": own, "issue": num,
                "source_updated_at": one_line(data.get("updated_at"), 40), "fetched_at": gate.now(), "fetched_by": "github-api"}
    if not isinstance(body, str) or len(body) > MAX_BODY:
        sys.exit("story-gate: the issue text is bigger than %d characters; nothing was saved." % MAX_BODY)
    if not body.strip():
        sys.exit("story-gate: issue #%d has no text, so there's nothing to check. Nothing was saved." % num)
    out = sd / ("issue-%d.md" % num)
    rel = out.relative_to(gate.ROOT).as_posix()
    if out.is_symlink() or (out.exists() and not out.is_file()):
        sys.exit("story-gate: %s is a link or a folder; story-gate only replaces a plain file there." % rel)
    text = snapshot_text(gate, sid, "#%d" % num, meta, title, body)
    gate.parse_snapshot(text)  # the gate must be able to read what was written: fail here, not later
    fd, tmp = tempfile.mkstemp(dir=str(sd), prefix=".issue-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, str(out))  # all or nothing: a failed write never leaves half a file
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        sys.exit("story-gate: couldn't write %s: %s" % (rel, e))
    st_md = sd / "story.md"
    new = link(st_md.read_text(encoding="utf-8"), rel)
    if new is None:
        print("Saved %s. story.md has no front matter, so add `spec: %s` to it yourself." % (rel, rel))
    else:
        st_md.write_text(new, encoding="utf-8")
    found, problem = gate.linked_scenarios(rel)
    print("Saved issue #%d as %s (%s) and linked it in story.md." % (
        num, rel, "from GitHub" if meta["fetched_by"] == "github-api" else "pasted text, not verified against the issue"))
    if problem:
        print("It can't be checked yet: " + problem)
        return 1
    print("%d requirement(s). Put each name in an AC's `covers` in tests.json:" % len(found))
    for ref, line in found.items():
        print("  %s\n      %s" % (ref, line.split("\n")[0][:100]))
    return 0


def live_status(gate, sid, rel, num, repo, token):
    """CI: compare the copy at rel with issue #num as it is now. The repository comes from CI (GITHUB_REPOSITORY) and the
    number from the file name, never from the copy, which an agent could rewrite. Returns (state, plain-English detail):
    'unchanged', 'changed' or 'not checked'."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo or ""):
        return "not checked", "not compared with the live issue: CI didn't say which repository this is"
    if not token:
        return "not checked", "not compared with the live issue: CI has no GitHub token"
    try:
        fields, content = gate.parse_snapshot(gate.rd(gate.ROOT / rel))
    except ValueError as e:
        return "not checked", "the copy can't be read (%s)" % e
    if fields["repo"].lower() != repo.lower():
        return "changed", "the copy says it came from %s, but this repository is %s" % (fields["repo"][:80], repo)
    import sg_github as G
    try:
        st, data, _ = G.call("GET", "/repos/%s/issues/%d" % (repo, num), token)
    except Exception as e:  # network trouble: say so, never pass silently
        return "not checked", "GitHub couldn't be reached (%s)" % type(e).__name__
    if st != 200 or not isinstance(data, dict):
        return "not checked", "GitHub answered HTTP %s for issue #%d (does the workflow have 'issues: read'?)" % (st, num)
    if "pull_request" in data or str(data.get("html_url") or "").lower().rstrip("/") != (
            "https://github.com/%s/issues/%d" % (repo, num)).lower():
        return "changed", "#%d is no longer this repository's issue (moved, or a pull request)" % num
    body = data.get("body") or ""
    if not isinstance(body, str) or len(body) > MAX_BODY:
        return "not checked", "the live issue is too big to compare"
    live = snapshot_content(sid, num, data.get("title") or "", body)
    verified = " (the pasted copy is now verified)" if fields["fetched_by"] == "pasted" else ""
    if live == content:
        return "unchanged", "matches the live issue" + verified
    old_u, new_u = [], []
    old = {k for k, _ in gate.spec_scenarios(content, old_u)}
    new = {k for k, _ in gate.spec_scenarios(live, new_u)}
    diff = []
    if new - old:
        diff.append("%d requirement(s) added or reworded" % len(new - old))
    if old - new:
        diff.append("%d removed or reworded" % len(old - new))
    if len(new_u) > len(old_u):
        diff.append("%d new line(s) that look like requirements but can't be read" % (len(new_u) - len(old_u)))
    return "changed", ("the live issue differs from the copy (%s). Run `story-gate spec-pull %s #%d` and check the "
                       "requirements again" % ("; ".join(diff) or "text only, same requirements", sid, num))
