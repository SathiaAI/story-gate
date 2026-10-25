"""story-gate <-> GitHub: human acceptance, protection checks, review status, JUnit results, setup and agent identity.

Design rules (decided by the frontier panel, Oct 2026):
- Acceptance = a native APPROVED review on the PR's current head commit, by a human (type User) listed in
  CODEOWNERS (or judge.approvers), who is neither the PR author nor the author of the head commit. Comments never count.
- Waivers and drift decisions written by agents are proposals; CI honours them only once that approval exists.
- Agents act under their own GitHub App ("story-gate agent"), never the human's login. The App has no
  `workflows` or `administration` permission, so it cannot edit CI or branch rules.
- Nothing here runs code from the pull request.
"""
import base64, csv, hashlib, http.server, json, os, re, secrets, shutil, subprocess, tempfile, time, urllib.error, urllib.parse, urllib.request, webbrowser
import xml.etree.ElementTree as ET
from pathlib import Path

API = os.environ.get("GITHUB_API_URL", "https://api.github.com")
WEB = os.environ.get("GITHUB_SERVER_URL", "https://github.com")


# ------------------------------------------------------------------ REST / GraphQL
class _SameHostRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but never send the token to a different host (urllib would forward the Authorization header)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).netloc.lower() != urllib.parse.urlsplit(req.full_url).netloc.lower():
            for k in list(new.headers):
                if k.lower() == "authorization":
                    del new.headers[k]
        return new


_OPENER = urllib.request.build_opener(_SameHostRedirect)


def call(method, path, token=None, body=None, accept="application/vnd.github+json"):
    url = path if path.startswith("http") else API + path
    h = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "story-gate"}
    if token:
        h["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode() if body is not None else None
    if data:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        r = _OPENER.open(req, timeout=30)
        raw = r.read().decode()
        return r.status, (json.loads(raw) if raw.strip() else {}), r.headers
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}"), e.headers
        except Exception:
            return e.code, {}, e.headers


def paged(path, token):
    out, url = [], API + path + ("&" if "?" in path else "?") + "per_page=100"
    for _ in range(20):
        st, data, hdr = call("GET", url, token)
        if st != 200 or not isinstance(data, list):
            raise RuntimeError("GitHub API %s -> HTTP %s" % (path, st))
        out += data
        m = re.search(r'<([^>]+)>;\s*rel="next"', (hdr or {}).get("Link", "") or "")
        if not m:
            return out
        url = m.group(1)
    raise RuntimeError("GitHub API %s: too many pages" % path)


def graphql(query, variables, token):
    st, data, _ = call("POST", API + "/graphql", token, {"query": query, "variables": variables})
    if st != 200 or data.get("errors"):
        raise RuntimeError("GitHub GraphQL failed (HTTP %s %s)" % (st, str(data.get("errors"))[:200]))
    return data["data"]


# ------------------------------------------------------------------ CODEOWNERS
CODEOWNERS_PATHS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")  # GitHub uses the first that exists
CODEOWNERS_HEADER = "# Code owners: the humans who accept work. Bots and apps cannot be code owners."
WILDCARDS = ("*", "/*", "/**", "**")


def add_owners(text, owners):
    """CODEOWNERS text with `owners` on the rule that covers everything, or None when they're already there.
    GitHub applies only the LAST matching line to each file, so appending a '*' line would take later path rules'
    files away from their owners. Instead: add the owners to the last '*' line in place (its owners stay); with no
    '*' line, put one before the first rule, so the path rules after it still win for their paths."""
    owners = ["@" + o.lstrip("@") for o in owners]
    lines = text.splitlines()
    rules = [i for i, l in enumerate(lines) if l.split("#", 1)[0].strip()]
    star = [i for i in rules if lines[i].split("#", 1)[0].split()[0] in WILDCARDS]
    if star:
        i = star[-1]
        rule, hash_, comment = lines[i].partition("#")
        parts = rule.split()
        add = [o for o in owners if o.lower() not in {p.lower() for p in parts[1:]}]
        if not add:
            return None
        lines[i] = " ".join(parts + add) + ("  #" + comment if hash_ else "")
    else:
        at = rules[0] if rules else len(lines)
        lines[at:at] = ([CODEOWNERS_HEADER] if not text.strip() else []) + ["* " + " ".join(owners)]
    return "\n".join(lines) + "\n"


def codeowners(text):
    """-> (users, teams) named on the '*' rule (the rule that covers everything, incl. CODEOWNERS itself)."""
    users, teams = [], []
    for line in text.splitlines():
        s = line.split("#", 1)[0].strip()
        if not s:
            continue
        parts = s.split()
        if parts[0] in ("*", "/*", "/**", "**"):
            users, teams = [], []
            for o in parts[1:]:
                if o.startswith("@") and "/" in o:
                    teams.append(o[1:])
                elif o.startswith("@"):
                    users.append(o[1:])
    return users, teams


# ------------------------------------------------------------------ acceptance (CI)
def pr_context():
    ev = os.environ.get("GITHUB_EVENT_PATH")
    if not ev or not os.path.isfile(ev):
        return None
    e = json.load(open(ev, encoding="utf-8"))
    pr = e.get("pull_request") or {}
    if not pr:
        return None
    return {"repo": os.environ.get("GITHUB_REPOSITORY", ""), "number": pr.get("number"), "head_sha": (pr.get("head") or {}).get("sha"),
            "author": (pr.get("user") or {}).get("login"), "base": (pr.get("base") or {}).get("ref"),
            "fork": ((pr.get("head") or {}).get("repo") or {}).get("full_name") != os.environ.get("GITHUB_REPOSITORY"),
            "event": os.environ.get("GITHUB_EVENT_NAME", "")}


def acceptance(ctx, token, owners_text, extra_approvers=()):
    """-> {"accepted": bool, "approver", "why"} using only GitHub's own review records."""
    users, teams = codeowners(owners_text)
    allowed = {u.lower() for u in users} | {a.lower().lstrip("@") for a in extra_approvers}
    if not allowed:
        return {"accepted": False, "why": "no human code owner found on the '*' rule of CODEOWNERS" + (" (teams are not supported yet: %s)" % ", ".join(teams) if teams else "")}
    st, commit, _ = call("GET", "/repos/%s/commits/%s" % (ctx["repo"], ctx["head_sha"]), token)
    if st != 200 or not isinstance(commit, dict):
        return {"accepted": False, "why": "could not read the head commit %s (HTTP %s)" % (ctx["head_sha"][:7], st)}
    # Whoever wrote or committed the latest commit cannot be the one who approves it.
    head_people = {((commit.get(k) or {}).get("login") or "").lower() for k in ("author", "committer")}
    if "" in head_people:
        # Without a linked account we can't prove the approver didn't write the latest commit, so we fail closed.
        return {"accepted": False, "why": "the latest commit %s has an author or committer email that isn't linked to a GitHub account; "
                "commit with a linked email (the agent identity from `gate.py agent-env` is)" % ctx["head_sha"][:7]}
    reviews = paged("/repos/%s/pulls/%s/reviews" % (ctx["repo"], ctx["number"]), token)
    latest = {}
    for r in reviews:  # keep each reviewer's most recent decisive review
        u = r.get("user") or {}
        if r.get("state") in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest[(u.get("login") or "").lower()] = (r, u)
    for login, (r, u) in latest.items():
        if r.get("state") == "CHANGES_REQUESTED":
            return {"accepted": False, "why": "%s requested changes" % login}
    for login, (r, u) in latest.items():
        if (r.get("state") == "APPROVED" and r.get("commit_id") == ctx["head_sha"] and u.get("type") == "User"
                and login in allowed and login != (ctx["author"] or "").lower() and login not in head_people):
            return {"accepted": True, "approver": login, "why": "approved by %s on %s" % (login, ctx["head_sha"][:7])}
    return {"accepted": False, "why": "waiting for a code owner (%s) to approve the latest commit %s" % (", ".join(sorted(allowed)), ctx["head_sha"][:7])}


def unresolved_threads(ctx, token, reviewers):
    owner, name = ctx["repo"].split("/", 1)
    q = """query($o:String!,$n:String!,$p:Int!,$a:String){repository(owner:$o,name:$n){pullRequest(number:$p){
      reviewThreads(first:100,after:$a){pageInfo{hasNextPage endCursor} nodes{isResolved comments(first:1){nodes{author{login}}}}}}}}"""
    want = None if reviewers is None else {r.lower().replace("[bot]", "") for r in reviewers}  # None: every thread counts
    n, after = 0, None
    for _ in range(50):  # 5000 threads is far beyond any real PR
        d = graphql(q, {"o": owner, "n": name, "p": int(ctx["number"]), "a": after}, token)
        rt = d["repository"]["pullRequest"]["reviewThreads"]
        for t in rt["nodes"]:
            a = (((t.get("comments") or {}).get("nodes") or [{}])[0].get("author") or {}).get("login", "")
            if not t.get("isResolved") and (want is None or a.lower().replace("[bot]", "") in want):
                n += 1
        if not rt["pageInfo"]["hasNextPage"]:
            break
        after = rt["pageInfo"]["endCursor"]
    return n


def open_review_threads(ctx, token, head):
    """Review threads that still block a review record: unresolved ones, and ones resolved by someone other than the
    reviewer who opened them while they sit on the current head. GitHub lets the PR's author resolve any thread, so a
    finding is cleared only by its reviewer, or by a later commit that the reviewer then reviewed cleanly (the live
    check already requires that clean review of the head)."""
    owner, name = ctx["repo"].split("/", 1)
    q = """query($o:String!,$n:String!,$p:Int!,$a:String){repository(owner:$o,name:$n){pullRequest(number:$p){
      reviewThreads(first:100,after:$a){pageInfo{hasNextPage endCursor} nodes{isResolved resolvedBy{login}
      comments(first:1){nodes{author{login} originalCommit{oid}}}}}}}}"""
    n, after = 0, None
    for _ in range(50):  # 5000 threads is far beyond any real PR
        d = graphql(q, {"o": owner, "n": name, "p": int(ctx["number"]), "a": after}, token)
        rt = d["repository"]["pullRequest"]["reviewThreads"]
        for t in rt["nodes"]:
            first = ((t.get("comments") or {}).get("nodes") or [{}])[0]
            opener = ((first.get("author") or {}).get("login") or "").lower()
            closer = ((t.get("resolvedBy") or {}).get("login") or "").lower()
            on_head = ((first.get("originalCommit") or {}).get("oid") or "") == head
            if not t.get("isResolved") or (on_head and closer != opener):
                n += 1
        if not rt["pageInfo"]["hasNextPage"]:
            break
        after = rt["pageInfo"]["endCursor"]
    return n


CODEX_SUMMARY = "<!-- codex-pull-request-review-summary -->"
CODEX_STATE = re.compile(r"codex-review-state:v1\s*(\{.*?\})\s*-->", re.S)


def codex_states(ctx, token, login):
    """The latest machine-readable review state Codex keeps in its summary comment, or None. Codex reports a clean
    round only there (no formal review), so a reviewer with source "codex-summary" is read from it. Only a comment
    written by that login as a Bot, and starting with Codex's summary marker, counts: anyone can paste the same text into a
    comment of their own, and Codex may quote it in another reply."""
    import html
    want = login.lower()  # the exact App login, like a native reviewer: "codex" and "codex[bot]" are not one account
    best = None
    for cm in paged("/repos/%s/issues/%s/comments" % (ctx["repo"], ctx["number"]), token):
        u = cm.get("user") or {}
        if (u.get("login") or "").lower() != want or u.get("type") != "Bot":
            continue
        body = html.unescape(cm.get("body") or "")
        if not body.lstrip().startswith(CODEX_SUMMARY):  # only Codex's own summary comment, never another reply that quotes it
            continue
        m = CODEX_STATE.search(body)
        if not m:
            continue
        try:
            state = json.loads(m.group(1))
        except ValueError:
            continue
        if best is None or (cm.get("updated_at") or "") > best[0]:
            best = (cm.get("updated_at") or "", cm.get("id"), state)
    return best


def review_policy_problem(policy):
    """'' when config.json -> review_record can ever lift a cap; otherwise why not. Checked locally too, so a record is
    never even proposed under a policy that CI could not accept (no reviewers, one reviewer, no security reviewer)."""
    revs = (policy or {}).get("reviewers") if isinstance(policy, dict) else None
    good = [r for r in revs if isinstance(r, dict) and isinstance(r.get("login"), str) and r["login"].strip()] if isinstance(revs, list) else []
    if not revs or len(good) != len(revs):
        return "config.json review_record.reviewers is missing or malformed"
    if any("part" in r and r.get("source") != "codex-summary" for r in good):
        return "review_record.reviewers: only a codex-summary reviewer has a part"
    if len({(r["login"].lower(), r.get("part", "")) for r in good}) < 2:  # a native reviewer is one login, one person
        return "review_record.reviewers lists fewer than two reviewers"
    if not any(r.get("security") is True for r in good):
        return "no reviewer in review_record.reviewers is marked security"
    return ""


def review_record_check(ctx, token, policy):
    """The live check behind an independent review record (docs/decisions/2026-10-24-independent-review-record.md).
    policy = config.json -> review_record. -> {"ok": bool, "why": str, "head": sha, "review_ids": [...]}. It reads only GitHub's
    own review records for the current head. Any problem, or an API error (the caller catches it), means ok False."""
    head = ctx.get("head_sha") or ""
    out = {"ok": False, "why": "", "head": head, "review_ids": []}
    problem = review_policy_problem(policy)
    if not head:
        out["why"] = "the pull request's head commit is unknown"
    elif problem:
        out["why"] = problem
    else:
        good = policy["reviewers"]
        reviews = paged("/repos/%s/pulls/%s/reviews" % (ctx["repo"], ctx["number"]), token)
        missing, snapshots = [], {}  # one Codex summary read per login: both parts are judged from the same snapshot
        for r in good:
            if r["login"].lower() == (ctx.get("author") or "").lower():  # the PR's author never reviews it, whatever the source
                missing.append("%s (the pull request's author)" % r["login"])
                continue
            if r.get("source") == "codex-summary":
                # one Codex login runs both a code and a security review; each is a "part" of its summary state
                if r["login"].lower() not in snapshots:
                    snapshots[r["login"].lower()] = codex_states(ctx, token, r["login"])
                found = snapshots[r["login"].lower()]
                part = (found[2].get(r.get("part")) if found and isinstance(found[2], dict) else None) or {}
                if (part.get("commit_sha") == head and part.get("status") == "completed"
                        and part.get("has_unresolved_findings") is False):
                    out["review_ids"].append("comment:%s:%s" % (found[1], r["part"]))
                else:
                    missing.append("%s (%s)" % (r["login"], r.get("part")))
                continue
            want, bot = r["login"].lower(), r["login"].lower().endswith("[bot]")

            def same(u):  # exact login, and a bot login must be that App: a user named like it is someone else
                return (u.get("login") or "").lower() == want and (u.get("type") == "Bot") == bot
            mine = sorted((x for x in reviews if x.get("commit_id") == head and x.get("state") != "PENDING"
                           and same(x.get("user") or {})), key=lambda x: (x.get("submitted_at") or "", x.get("id") or 0))
            # the reviewer's latest review of the current head decides: a later rejection or dismissal undoes an earlier pass
            if mine and mine[-1].get("state") in ("APPROVED", "COMMENTED"):
                out["review_ids"].append(str(mine[-1].get("id")))
            else:
                missing.append(r["login"] + (" (latest review: %s)" % mine[-1].get("state") if mine else ""))
        open_threads = open_review_threads(ctx, token, head)
        if missing:
            out["why"] = "no finished review on %s from: %s" % (head[:7], ", ".join(missing))
        elif open_threads:
            out["why"] = ("%d review thread(s) still open on the pull request (unresolved, or resolved on this commit by "
                          "someone other than the reviewer who opened it)" % open_threads)
        else:
            out["ok"] = True
    return out


def label_added_by(ctx, token, label):
    """Login of whoever most recently added `label` to the PR (and it is still on the PR), else None."""
    events = paged("/repos/%s/issues/%s/events" % (ctx["repo"], ctx["number"]), token)
    events.sort(key=lambda e: (e.get("created_at") or "", e.get("id") or 0))  # the API promises no order: oldest first
    who, present = None, False
    for e in events:
        if (e.get("label") or {}).get("name") != label:
            continue
        if e.get("event") == "labeled":
            who, present = ((e.get("actor") or {}).get("login")), True
        elif e.get("event") == "unlabeled":
            present = False
    return who if present else None


def reviewed_by(ctx, token, reviewers):
    want = {r.lower().replace("[bot]", "") for r in reviewers}
    reviews = paged("/repos/%s/pulls/%s/reviews" % (ctx["repo"], ctx["number"]), token)
    return sorted({(r.get("user") or {}).get("login", "") for r in reviews  # only reviews of the current head commit count
                   if r.get("commit_id") == ctx["head_sha"] and ((r.get("user") or {}).get("login", "").lower().replace("[bot]", "")) in want})


def protection(repo, branch, token, check=None):
    """Best-effort read of the rules that apply to the base branch. -> (enforced: bool, missing: [str]). `check` is the
    name this repository's workflow reports (ci_check_name); without it either story-gate check name counts."""
    st, rules, _ = call("GET", "/repos/%s/rules/branches/%s" % (repo, urllib.parse.quote(branch, safe="")), token)
    if st != 200 or not isinstance(rules, list):
        return False, ["could not read branch rules (HTTP %s)" % st]
    pr = [r.get("parameters") or {} for r in rules if r.get("type") == "pull_request"]
    checks = [c.get("context") for r in rules if r.get("type") == "required_status_checks" for c in (r.get("parameters") or {}).get("required_status_checks", [])]
    missing = []
    if not pr:
        missing.append("pull request reviews are not required")
    else:
        p = {  # GitHub applies every active ruleset together, the strictest value of each setting wins
            "require_code_owner_review": any(x.get("require_code_owner_review") for x in pr),
            "dismiss_stale_reviews_on_push": any(x.get("dismiss_stale_reviews_on_push") for x in pr),
            "require_last_push_approval": any(x.get("require_last_push_approval") for x in pr),
            "required_approving_review_count": max(int(x.get("required_approving_review_count") or 0) for x in pr)}
        if not p.get("require_code_owner_review"):
            missing.append("code owner review is not required")
        if not p.get("dismiss_stale_reviews_on_push"):
            missing.append("stale approvals are not dismissed on new pushes")
        if not p.get("require_last_push_approval"):
            missing.append("approval of the most recent push is not required")
        if int(p.get("required_approving_review_count") or 0) < 1:
            missing.append("no approving review is required")
    if check and check not in checks:  # a rule on the other name waits for a check this workflow never reports
        missing.append(f"the story-gate check is not a required status check (this repository's workflow reports `{check}`)")
    elif not check and not {"story-gate", "story-gate / story-gate"} & set(checks):  # copied / reusable workflow names
        missing.append("the story-gate check is not a required status check")
    return not missing, missing


# ------------------------------------------------------------------ JUnit
RANK = {"passed": 0, "skipped": 1, "failed": 2}


def _parse_xml_without_dtd(raw):
    """Parse XML with the standard library's expat, refusing any DTD, entity declaration or external entity at the
    parser itself (what defusedxml does), so no entity expansion or external fetch can happen whatever the bytes are."""
    import xml.parsers.expat
    builder = ET.TreeBuilder()
    p = xml.parsers.expat.ParserCreate()

    def refuse(*_):
        """Expat handler: any DTD or entity declaration stops the parse."""
        raise ValueError("JUnit report rejected (it declares a DTD or an entity)")
    p.StartDoctypeDeclHandler = p.EntityDeclHandler = p.UnparsedEntityDeclHandler = refuse
    p.ExternalEntityRefHandler = refuse
    p.StartElementHandler = builder.start
    p.EndElementHandler = builder.end
    p.CharacterDataHandler = builder.data
    try:
        p.Parse(raw, True)
    except xml.parsers.expat.ExpatError as e:
        raise ET.ParseError(str(e))
    return builder.close()


def junit(path, max_bytes=5_000_000):
    """-> {test_name: outcome} with outcome in passed|failed|skipped. Rejects oversized files and DTDs."""
    p = Path(path)
    if not p.is_file():
        return None
    raw = p.read_bytes()
    if len(raw) > max_bytes or b"<!DOCTYPE" in raw[:2000].upper() or b"<!ENTITY" in raw:
        raise ValueError("JUnit report rejected (too large or contains a DTD/entity)")
    root = _parse_xml_without_dtd(raw)
    out = {}
    for tc in root.iter("testcase"):
        name, cls = tc.get("name") or "", tc.get("classname") or ""
        if tc.find("failure") is not None or tc.find("error") is not None:
            o = "failed"
        elif tc.find("skipped") is not None:
            o = "skipped"
        else:
            o = "passed"
        keys = {name, (cls + "." + name) if cls else name, (cls + "::" + name) if cls else name}
        keys |= {k.rsplit("[", 1)[0] for k in keys if k.endswith("]") and "[" in k}  # test_x[1], test_x[2] also count as test_x
        for key in keys:
            if key:  # one name shared by several tests: the worst outcome wins, so a skipped twin never hides behind a pass
                out[key] = max(out.get(key, o), o, key=RANK.get)
    return out


def ref_outcome(ref, results):
    """Match a test_ref to JUnit names: exact, or a unique suffix match (e.g. 'test_x' vs 'tests.test_mod.test_x')."""
    if ref in results:
        return results[ref]
    hits = {k: v for k, v in results.items() if k.endswith("." + ref) or k.endswith("::" + ref) or k.endswith("/" + ref)}
    if not hits:
        return "missing"
    if len(set(hits.values())) > 1:
        return "ambiguous"  # same name in several places with different results: never counted as passing
    return next(iter(hits.values()))


# ------------------------------------------------------------------ human token (setup only)
def human_token():
    t = os.environ.get("STORY_GATE_HUMAN_TOKEN") or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if t:
        return t.strip()
    if shutil.which("gh"):
        r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    return None


def whoami(token):
    st, u, _ = call("GET", "/user", token)
    return (u or {}).get("login") if st == 200 else None


RULESET_NAME = "story-gate: human acceptance"
UNATTRIBUTED = "require_extra_approval_for_unattributed_changes"


def ci_check_name(root):
    """The name of story-gate's pull request check: 'story-gate', or 'story-gate / story-gate' when the repository's
    workflow calls story-gate's reusable workflow (GitHub names the check '<calling job> / <called job>')."""
    p = Path(root) / ".github" / "workflows" / "story-gate.yml"
    text = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""
    # only an active job-level `uses:` counts: not a comment, and not a step (`- uses:`) someone added to a copied workflow
    call = re.search(r"^[ \t]*uses:[ \t]*[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/\.github/workflows/gate\.yml@", text, re.M)
    return "story-gate / story-gate" if call else "story-gate"


def ruleset_json(check="story-gate"):
    """Return the default branch ruleset requiring human acceptance without extra AI approval."""
    return {"name": RULESET_NAME, "target": "branch", "enforcement": "active",
            "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
            "bypass_actors": [],
            "rules": [{"type": "deletion"}, {"type": "non_fast_forward"},
                      {"type": "pull_request", "parameters": {"required_approving_review_count": 1, "dismiss_stale_reviews_on_push": True,
                                                              "require_code_owner_review": True, "require_last_push_approval": True,
                                                              "required_review_thread_resolution": True,
                                                              # GitHub turns this on when a payload leaves it out. It then asks for a
                                                              # second approval on every pull request the AI's login opens, which a solo
                                                              # owner can never give. A code owner's approval of the latest push is the gate.
                                                              UNATTRIBUTED: False}},
                      {"type": "required_status_checks", "parameters": {"strict_required_status_checks_policy": False,
                                                                        "required_status_checks": [{"context": check}]}}]}


def require_check(repo, ruleset_id, token, check):
    """Make story-gate's own ruleset require `check` in place of the other name of story-gate's check (only that)."""
    st, rs, _ = call("GET", "/repos/%s/rulesets/%s" % (repo, ruleset_id), token)
    if st != 200 or not isinstance(rs, dict):
        return "Ruleset: could not read it (HTTP %s) to check which story-gate check it requires" % st
    rules = rs.get("rules") or []
    rsc = [r for r in rules if r.get("type") == "required_status_checks"]
    if not rsc:
        return "Ruleset: it doesn't require any check. Add '%s' in Settings > Rules so a red story-gate check blocks merging" % check
    p = rsc[0].setdefault("parameters", {})
    have = [c.get("context") for c in p.get("required_status_checks") or [] if isinstance(c, dict)]
    other = ({"story-gate", "story-gate / story-gate"} - {check}).pop()
    if check in have and other not in have:  # the other name may never report again, so it can't stay required
        return "Ruleset: requires the check '%s'" % check
    p["required_status_checks"] = [c for c in p.get("required_status_checks") or []
                                   if not (isinstance(c, dict) and c.get("context") in ("story-gate", "story-gate / story-gate"))]
    p["required_status_checks"].append({"context": check})
    st, r, _ = call("PUT", "/repos/%s/rulesets/%s" % (repo, ruleset_id), token, {"rules": rules})
    if st in (200, 201):
        return "Ruleset: now requires the check '%s'" % check
    return "Ruleset: could not change the required check to '%s' (HTTP %s %s); change it in Settings > Rules" % (check, st, (r or {}).get("message", ""))


def allow_ai_prs(repo, ruleset_id, token):
    """Turn off GitHub's extra approval for AI-opened pull requests in story-gate's own ruleset (only that setting)."""
    st, rs, _ = call("GET", "/repos/%s/rulesets/%s" % (repo, ruleset_id), token)
    if st != 200 or not isinstance(rs, dict):
        return "Ruleset: could not read it (HTTP %s) to check the extra approval for AI pull requests" % st
    rules = rs.get("rules") or []
    pr = [r for r in rules if r.get("type") == "pull_request"]
    if not pr:
        return ("Ruleset: '%s' has no pull request rule, so approvals aren't required. Delete it in Settings > Rules and run "
                "setup-repo again, or import docs/story-gate-ruleset.json." % RULESET_NAME)
    p = pr[0].get("parameters") or {}
    try:
        count = int(p.get("required_approving_review_count") or 0)
    except (TypeError, ValueError):
        count = 0
    missing = [what for ok, what in (
        (rs.get("enforcement") == "active", "it isn't active (enforcement: %s)" % rs.get("enforcement")),
        (bool(p.get("require_code_owner_review")), "it doesn't require a code owner's review"),
        (count >= 1, "it doesn't require an approval"),
        (bool(p.get("require_last_push_approval")), "it doesn't require approval of the latest push"),
        (bool(p.get("dismiss_stale_reviews_on_push")), "it doesn't cancel old approvals when new commits arrive")) if not ok]
    need = ("one code owner approval of the latest push" if count == 1 else
            "%d approvals, including a code owner's, of the latest push" % count)
    gap = ("" if not missing else " Note: this isn't story-gate's full rule: %s. Delete it in Settings > Rules and run setup-repo "
           "again, or import docs/story-gate-ruleset.json." % "; ".join(missing))
    if p.get(UNATTRIBUTED) is False:
        return ("Ruleset: pull requests the AI opens need %s" % need) if not missing else "Ruleset:" + gap
    pr[0].setdefault("parameters", {})[UNATTRIBUTED] = False
    st, r, _ = call("PUT", "/repos/%s/rulesets/%s" % (repo, ruleset_id), token, {"rules": rules})
    if st in (200, 201):
        return "Ruleset: turned off GitHub's extra approval for AI pull requests" + (" (they need %s)" % need if not missing else "." + gap)
    return ("Ruleset: could not turn off GitHub's extra approval for AI pull requests (HTTP %s %s). In Settings > Rules, open '%s' "
            "and untick 'Require an additional approval for unattributed Copilot pull requests'." % (st, (r or {}).get("message", ""), RULESET_NAME))


def owners_on_default_branch(repo, token):
    """Users on the '*' rule of the CODEOWNERS file GitHub uses on the default branch (.github/, then the root, then docs/);
    [] when there is none; None when GitHub's answer can't tell (then the caller carries on as before)."""
    for path in CODEOWNERS_PATHS:
        st, f, _ = call("GET", "/repos/%s/contents/%s" % (repo, path), token)
        if st == 200 and isinstance(f, dict) and f.get("encoding") == "base64":
            return codeowners(base64.b64decode(f.get("content") or "").decode("utf-8", "replace"))[0]
        if st != 404:
            return None
    return []


def codeowners_on_default_branch(repo, token):
    """(path, text) of the CODEOWNERS file GitHub uses on the default branch (the first of CODEOWNERS_PATHS there);
    ("", "") when there is none; None when GitHub's answer can't tell (an error, or a folder or link at that path)."""
    for path in CODEOWNERS_PATHS:
        st, f, _ = call("GET", "/repos/%s/contents/%s" % (repo, path), token)
        if st == 200:
            if isinstance(f, dict) and f.get("type") == "file" and f.get("encoding") == "base64":
                return path, base64.b64decode(f.get("content") or "").decode("utf-8", "replace")
            return None
        if st != 404:
            return None
    return "", ""


def codeowners_in_git(root, ref):
    """(path, text) of the CODEOWNERS file GitHub would use at git `ref` in the clone at root: the first of
    CODEOWNERS_PATHS there; ("", "") when there is none; None when git can't read `ref` or that path is not a regular
    file (a link or a folder), so nobody guesses."""
    run = lambda *a: subprocess.run(["git", *a], cwd=str(root), capture_output=True)
    if run("rev-parse", "--verify", "--quiet", ref + "^{commit}").returncode != 0:
        return None
    for path in CODEOWNERS_PATHS:
        r = run("ls-tree", "-z", ref, "--", path)
        if r.returncode != 0:
            return None
        entry = r.stdout.split(b"\0")[0]
        if not entry:
            continue
        mode, kind, sha = entry.split(b"\t")[0].split(b" ")
        if kind != b"blob" or mode not in (b"100644", b"100755"):
            return None
        blob = run("cat-file", "blob", sha.decode())
        return (path, blob.stdout.decode("utf-8", "replace")) if blob.returncode == 0 else None
    return "", ""


def _link_on_path(root, rel):
    """True when rel or any folder on the way to it is a link (writing there could land outside the checkout)."""
    p = Path(root)
    for part in Path(rel).parts:
        p = p / part
        if p.is_symlink():
            return True
    return False


def codeowners_change(co, new, repo, token, login, added, rel=".github/CODEOWNERS"):
    """Put the new CODEOWNERS text where it can be approved: a pull request from the agent App, or the local file for you
    to commit. -> one line for setup-repo's report."""
    st, info, _ = call("GET", "/repos/%s" % repo, token)
    base = (info.get("default_branch") if st == 200 and isinstance(info, dict) else None) or "main"
    try:
        who, tok, why = pr_author(repo, base, token, login)
    except NoAuthor as e:
        return "CODEOWNERS: not changed. %s" % e
    except RuntimeError as e:
        return "CODEOWNERS: not changed: couldn't tell who should open the pull request (%s)" % e
    if who == "app":
        try:  # one fixed branch: running setup-repo again before that pull request is merged finds it instead of opening another
            res = open_setup_pr(tok, repo, {rel: new.encode("utf-8")},
                                branch="story-gate-codeowners", title="story-gate: code owners",
                                body="Names the people who accept story-gate's work (`%s`).\n\nOpened by: %s." % (rel, why),
                                base=base)
        except RuntimeError as e:  # report it and let setup-repo go on with the ruleset and Actions steps
            return "CODEOWNERS: not changed: couldn't open the pull request (%s)" % e
        if res.get("existing"):
            ref = "?ref=" + urllib.parse.quote(res["branch"])
            for other in CODEOWNERS_PATHS[:CODEOWNERS_PATHS.index(rel)] if rel in CODEOWNERS_PATHS else ():
                st_o, _, _ = call("GET", "/repos/%s/contents/%s%s" % (repo, other, ref), tok)
                if st_o != 404:  # an earlier run's file there would be the one GitHub reads, hiding this change
                    return ("CODEOWNERS: not changed: pull request #%s still has %s, which GitHub would read instead of %s. "
                            "Close it, then run setup-repo again." % (res.get("number"), other, rel))
            err = update_branch_file(tok, repo, res["branch"], rel, new, "story-gate: code owners")
            if err:
                return ("CODEOWNERS: not changed: pull request #%s is already open with other code owners and couldn't be "
                        "updated (%s). Close it, then run setup-repo again." % (res.get("number"), err))
        return "%s in pull request #%s (%s). Approve and merge it, then run setup-repo again." % (added, res.get("number"), why)
    co.parent.mkdir(parents=True, exist_ok=True)
    co.write_text(new, encoding="utf-8")
    return "%s (commit and merge this file: %s)" % (added, why)


def update_branch_file(token, repo, branch, path, text, message):
    """Make `path` on `branch` read exactly `text` (one commit, only when it differs). -> None, or why it couldn't."""
    ref = "?ref=" + urllib.parse.quote(branch)
    st, f, _ = call("GET", "/repos/%s/contents/%s%s" % (repo, path, ref), token)
    if st == 200 and isinstance(f, dict) and f.get("encoding") == "base64":
        if base64.b64decode(f.get("content") or "") == text.encode("utf-8"):
            return None
        sha = f.get("sha")
    elif st == 404:
        sha = None
    else:
        return "couldn't read %s on %s (HTTP %s)" % (path, branch, st)
    body = {"message": message, "content": base64.b64encode(text.encode("utf-8")).decode(), "branch": branch}
    if sha:
        body["sha"] = sha
    st, _, _ = call("PUT", "/repos/%s/contents/%s" % (repo, path), token, body)
    return None if st in (200, 201) else "GitHub refused the update (HTTP %s)" % st


def setup_repo(root, repo, owners, token, dry_run=False, login=None):
    """Human-run, one time: CODEOWNERS, ruleset, Actions cannot approve PRs. Returns a list of plain-English results.
    A CODEOWNERS change goes in a pull request the agent App opens when it is installed; otherwise in the local file for
    you to commit, when that pull request can be approved (pr_author decides)."""
    out = []
    # The file GitHub uses on the default branch, and its rules, decide; never this checkout's own files.
    found = codeowners_on_default_branch(repo, token) if token and not dry_run else None
    if found is None:  # GitHub can't tell: origin's default branch as git last fetched it
        head = subprocess.run(["git", "symbolic-ref", "-q", "refs/remotes/origin/HEAD"], cwd=str(root), capture_output=True, text=True)
        found = codeowners_in_git(root, head.stdout.strip()) if head.returncode == 0 and head.stdout.strip() else None
    if found is None and dry_run:  # a dry run writes nothing: the local file is enough to show what would change
        local = next((r for r in CODEOWNERS_PATHS if (Path(root) / r).is_file() and not _link_on_path(root, r)), "")
        found = (local, (Path(root) / local).read_text(encoding="utf-8") if local else "")
    rel, text = found if found is not None else ("", "")
    rel = rel or CODEOWNERS_PATHS[0]  # none on the default branch yet: GitHub reads .github/CODEOWNERS first
    co = Path(root) / rel
    link = _link_on_path(root, rel)  # never write through a link: it could point outside the checkout
    new = None if found is None or link else add_owners(text, owners)  # on the effective (last) '*' rule
    if found is None:
        out.append("CODEOWNERS: not changed: neither GitHub (it needs Contents: read) nor this clone's origin/HEAD says which "
                   "CODEOWNERS file the default branch uses. Run `git remote set-head origin --auto`, then setup-repo again")
    elif link:
        out.append("CODEOWNERS: not changed: %s is a link or is inside one; replace it with a real file and run setup-repo "
                   "again" % rel)
    elif new is not None:
        added = "CODEOWNERS: added %s to the '*' rule" % " ".join("@" + o.lstrip("@") for o in owners)
        if dry_run:
            out.append(added + " (dry run: nothing written)")
        else:
            out.append(codeowners_change(co, new, repo, token, login or (owners[0] if owners else None), added, rel))
    else:
        out.append("CODEOWNERS: already lists %s" % ", ".join(owners))
    if dry_run:
        return out + ["(dry run: no GitHub changes)"]
    st, existing, _ = call("GET", "/repos/%s/rulesets" % repo, token)
    # only this repository's own ruleset: an organization's ruleset with the same name is listed too, but can't be changed here
    mine = [r for r in existing if r.get("name") == RULESET_NAME and r.get("source_type", "Repository") == "Repository"] \
        if st == 200 and isinstance(existing, list) else []
    if mine:
        out.append("Ruleset: '%s' already exists" % RULESET_NAME)
        out.append(allow_ai_prs(repo, mine[0].get("id"), token))
        out.append(require_check(repo, mine[0].get("id"), token, ci_check_name(root)))
    else:
        live = owners_on_default_branch(repo, token)
        if live is not None and not {o.lstrip("@").lower() for o in owners} & {u.lower() for u in live}:
            # story-gate takes code owners from the default branch, so a rule turned on now would block the very pull
            # request that adds them: nobody's approval could count.
            out.append("Ruleset: not turned on yet. Merge the CODEOWNERS change into the default branch first, then run "
                       "setup-repo again (story-gate only counts approvals from code owners already on that branch).")
        else:
            st, r, _ = call("POST", "/repos/%s/rulesets" % repo, token, ruleset_json(ci_check_name(root)))
            out.append("Ruleset: %s" % ("created" if st in (200, 201) else "NOT created (HTTP %s %s). Import docs/story-gate-ruleset.json in Settings > Rules instead." % (st, (r or {}).get("message", ""))))
    st, _, _ = call("PUT", "/repos/%s/actions/permissions/workflow" % repo, token,
                    {"default_workflow_permissions": "read", "can_approve_pull_request_reviews": False})
    out.append("Actions: %s" % ("default token is read-only and cannot approve PRs" if st in (200, 204) else "could not update (HTTP %s); set it in Settings > Actions" % st))
    st, info, _ = call("GET", "/repos/%s" % repo, token)
    if st == 200 and info.get("private") and (info.get("owner") or {}).get("type") in ("User", "Organization"):
        out.append("Note: branch rules on PRIVATE repos are only enforced on paid GitHub plans (Pro, Team, Enterprise).")
    out.append("Secret: add your judge key (e.g. OPENROUTER_API_KEY) at %s/%s/settings/secrets/actions/new" % (WEB, repo))
    return out


# ------------------------------------------------------------------ agent identity (GitHub App via manifest)
def config_dir():
    if os.environ.get("STORY_GATE_HOME"):  # e.g. a folder an AI tool's sandbox can also reach
        d = Path(os.environ["STORY_GATE_HOME"])
        d.mkdir(parents=True, exist_ok=True)
        return d
    base = os.environ.get("APPDATA") if os.name == "nt" else os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    d = Path(base or os.path.expanduser("~")) / "story-gate"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _user_sid():
    """The current Windows account's SID (the full identity, never just the user name)."""
    r = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True)
    row = next(csv.reader([r.stdout.strip()]), [])
    if r.returncode != 0 or len(row) < 2 or not row[-1].startswith("S-1-"):
        raise RuntimeError("could not read your Windows account SID (whoami: %s)" % (r.stdout + r.stderr).strip()[:200])
    return row[-1]


def lock_down(path, directory=False):
    """Owner-only access. Directories are locked before any secret is created in them, so new files start private."""
    if os.name == "nt":
        grant = "*%s:%sF" % (_user_sid(), "(OI)(CI)" if directory else "")
        r = subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", grant], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError("could not restrict %s to your account (icacls: %s)" % (path, (r.stdout + r.stderr).strip()[:200]))
    else:
        os.chmod(path, 0o700 if directory else 0o600)


def write_private(path, text):
    """Create the file readable by the owner only from the first byte (no window where others can read it)."""
    path = Path(path)
    lock_down(path.parent, directory=True)  # new files inherit owner-only access from here
    if os.path.lexists(path):
        os.remove(path)  # a fresh file: an old one could carry extra permissions (explicit ACEs on Windows)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    lock_down(path)


def key_access(path):
    """-> (private: bool, detail: str). On Windows every allow entry must be the current account's SID."""
    if os.name != "nt":
        mode = os.stat(path).st_mode & 0o777
        return (mode & 0o077) == 0, "mode %o" % mode
    try:
        sid = _user_sid()
    except RuntimeError as e:
        return False, str(e)
    tmp = Path(tempfile.mkdtemp()) / "acl"
    try:
        r = subprocess.run(["icacls", str(path), "/save", str(tmp), "/q"], capture_output=True, text=True)
        raw = tmp.read_bytes() if tmp.exists() else b""
    finally:
        shutil.rmtree(tmp.parent, ignore_errors=True)
    if r.returncode != 0 or not raw:
        return False, "icacls /save failed: %s" % (r.stdout + r.stderr).strip()[:200]
    text = raw.decode("utf-16", errors="ignore") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") or b"\x00" in raw[:8] else raw.decode("utf-8", "ignore")
    dacl = next((l.strip()[2:].split("S:", 1)[0] for l in text.splitlines() if l.strip().startswith("D:")), "")
    aces = re.findall(r"\(([^)]*)\)", dacl)
    trustees = [a.split(";")[-1] for a in aces if a.split(";")[0] in ("A", "OA")]
    me = {sid} | ({"LA"} if sid.endswith("-500") else set()) | ({"LG"} if sid.endswith("-501") else set())  # SDDL aliases for built-in accounts
    return bool(trustees) and all(t in me for t in trustees), "you are %s; access list: %s" % (sid, dacl or text.strip()[:300])


def key_is_private(path):
    return key_access(path)[0]


def manifest(name, redirect):
    return {"name": name, "url": "https://github.com/SathiaAI/story-gate", "public": False,
            "hook_attributes": {"url": "https://example.invalid/story-gate", "active": False},
            "redirect_url": redirect, "description": "AI coding agent identity created by story-gate. It can push branches and open PRs; it cannot approve, merge protected branches, or change CI.",
            "default_permissions": {"contents": "write", "pull_requests": "write", "issues": "write", "metadata": "read", "checks": "read"},
            "default_events": []}


def setup_agent(owner=None, org=False, code=None, open_browser=True):
    """Create the user's own private agent App with GitHub's manifest flow. Returns the saved app record."""
    if code is None:
        state = secrets.token_urlsafe(24)
        got = {}

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.headers.get("Host", "") != "127.0.0.1:%d" % self.server.server_address[1]:  # DNS-rebinding guard, as in sg_setup
                    self.send_response(403); self.end_headers()
                    return
                u = urllib.parse.urlparse(self.path)
                qs = urllib.parse.parse_qs(u.query)
                if u.path == "/start":
                    body = page(form_html)
                elif u.path == "/callback" and qs.get("state", [""])[0] == state and qs.get("code"):
                    got["code"] = qs["code"][0]
                    body = page("<h1>Agent App created.</h1><p>You can close this tab and go back to the terminal.</p>")
                else:
                    body = page("<h1>Unexpected request.</h1>")
                self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers()
                self.wfile.write(body.encode())

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        port = srv.server_address[1]
        name = ("story-gate-agent-%s-%s" % ((owner or "me").lower(), secrets.token_hex(2)))[:34]
        target = ("%s/organizations/%s/settings/apps/new" % (WEB, owner)) if org else (WEB + "/settings/apps/new")
        mf = json.dumps(manifest(name, "http://127.0.0.1:%d/callback" % port))
        form_html = ('<h1>Create your story-gate agent</h1><p>GitHub will ask you to confirm. Nothing is sent anywhere else.</p>'
                     '<form id="f" method="post" action="%s?state=%s"><input type="hidden" name="manifest" value=\'%s\'>'
                     '<button type="submit">Create on GitHub</button></form><script>document.getElementById("f").submit()</script>'
                     % (target, state, mf.replace("'", "&#39;")))
        url = "http://127.0.0.1:%d/start" % port
        print("Opening %s - if no browser opens, paste that address into your browser." % url)
        if open_browser:
            webbrowser.open(url)
        srv.timeout = 1
        t0 = time.time()
        while "code" not in got and time.time() - t0 < 3300:  # the manifest code is only valid for one hour
            srv.handle_request()
        srv.server_close()
        code = got.get("code")
        if not code:
            raise RuntimeError("no code came back from GitHub. Re-run, or pass --code <code> from the redirect URL.")
    st, app, _ = call("POST", "/app-manifests/%s/conversions" % urllib.parse.quote(code), None, {})
    if st not in (200, 201) or not app.get("pem"):
        raise RuntimeError("GitHub did not convert the manifest (HTTP %s %s)" % (st, (app or {}).get("message", "")))
    d = config_dir()
    keyp = d / ("%s.pem" % app["slug"])
    write_private(keyp, app["pem"])
    rec = {"id": app["id"], "slug": app["slug"], "owner": (app.get("owner") or {}).get("login"), "key": str(keyp),
           "install_url": "%s/apps/%s/installations/new" % (WEB, app["slug"])}
    write_private(d / "agent.json", json.dumps(rec, indent=1))
    if open_browser:
        webbrowser.open(rec["install_url"])
    return rec


def page(inner):
    """Wrap `inner` HTML in the small styled page the setup flows show in the browser."""
    return ("<!doctype html><meta charset=utf-8><title>story-gate setup</title><style>body{font:16px/1.5 system-ui,sans-serif;"
            "background:#FFF8F2;color:#1F2327;max-width:640px;margin:10vh auto;padding:0 24px}button{background:#FFD84D;color:#1F2327;"
            "border:2px solid #1F2327;border-radius:10px;padding:12px 18px;font-weight:600}</style>" + inner)


class AgentMissing(RuntimeError):
    """This computer has no agent App, or it isn't installed on the repository. Any other agent error is a plain RuntimeError."""


class NoAuthor(RuntimeError):
    """Nobody can open the pull request so that someone else can approve it; the message says what to do."""


def approval_required(repo, branch, token):
    """True when GitHub needs an approval to merge into `branch`, False when it doesn't, None when it can't tell.
    Branch protection without readable rules counts as True: when unsure, assume an approval is needed."""
    q = urllib.parse.quote(branch, safe="")
    st, rules, _ = call("GET", "/repos/%s/rules/branches/%s" % (repo, q), token)
    if st != 200 or not isinstance(rules, list):
        return None
    for r in rules:
        if not (isinstance(r, dict) and r.get("type") == "pull_request"):
            continue
        p = r.get("parameters") or {}
        if not isinstance(p, dict):
            return True  # a pull-request rule we can't read: assume it needs an approval
        try:
            count = int(p.get("required_approving_review_count") or 0)
        except (TypeError, ValueError):
            return True
        if count >= 1 or p.get("require_code_owner_review"):
            return True
    st, b, _ = call("GET", "/repos/%s/branches/%s" % (repo, q), token)
    if st != 200 or not isinstance(b, dict):
        return None
    return bool(b.get("protected"))


# The most the agent App may hold (what setup-agent asks for). Anything more is refused, never used.
AGENT_MAX_PERMS = {"contents": "write", "pull_requests": "write", "issues": "write", "metadata": "read", "checks": "read"}
_RANK = {None: 0, "": 0, "none": 0, "read": 1, "write": 2, "admin": 3}


def pr_author(repo, branch, token, login, touches_workflows=False):
    """Who opens a pull request story-gate makes, decided before anything is written. GitHub never counts an approval
    from whoever opened a pull request, so:
      1. the agent App, when it is installed on the repository (it opens, you approve). The App can't change workflow
         files, so with `touches_workflows` the answer is 'split': you commit the workflow files, then the App commits the
         rest and opens the pull request (token is then the pair (yours, the App's));
      2. otherwise you, when `branch` needs no approval yet or another code owner on it can approve;
      3. otherwise NoAuthor, with the next step. An agent error other than "not set up" is raised, never swallowed, and an
         App that can change workflow files is refused (it would let your AI run unreviewed workflows with your secrets).
    -> (who, token, why) with who 'app', 'split' or 'human'."""
    try:
        tok, _, rec = agent_token(repo)
        perms = rec.get("permissions") or {}
        granted = rec.get("installation_permissions") or perms  # what the App itself holds, beyond this token
        short = [p for p in ("contents", "pull_requests") if perms.get(p) != "write"]
        if short:  # installed, but it couldn't push the branch or open the pull request: say so, don't switch to you
            raise RuntimeError("the agent App on %s lacks write access to %s. Update its permissions in the App's settings "
                               "and accept them on the installation, then try again." % (repo, " and ".join(short)))
        extra = sorted(k for k, v in granted.items() if _RANK.get(v, 3) > _RANK[AGENT_MAX_PERMS.get(k, "none")])
        if extra:
            # the builder must never be able to change workflows, branch rules, secrets or check results: refuse such an App
            raise RuntimeError("the agent App on %s has more access than an AI builder may have (%s). With it, your AI could "
                               "change workflows, branch rules, secrets or check results without review. Remove those "
                               "permissions in the App's settings (it needs only %s), accept the change on the installation, "
                               "then try again." % (repo, ", ".join("%s: %s" % (k, granted[k]) for k in extra),
                                                     ", ".join("%s: %s" % kv for kv in sorted(AGENT_MAX_PERMS.items()))))
        slug = rec.get("slug", "story-gate-agent")
        if not touches_workflows:
            return "app", tok, "the agent App (%s[bot]) opens it, so a code owner's approval counts" % slug
        # Workflow files: you write them (the App can't), then the App adds the rest and opens the pull request. It is the
        # author and the last pusher, so your approval counts, and the App never gets the power to change workflows.
        return "split", (token, tok), ("you added the workflow files; the agent App (%s[bot]) added the rest and opened it, "
                                       "so a code owner's approval counts" % slug)
    except AgentMissing:
        pass
    need = approval_required(repo, branch, token)
    if need is False:
        return "human", token, "you open it: %s doesn't require an approval yet" % branch
    others = sorted(o for o in (owners_on_default_branch(repo, token) or []) if o.lower() != (login or "").lower())
    if others:
        return "human", token, "you open it and another code owner (%s) approves it" % ", ".join("@" + o for o in others)
    why = ("GitHub doesn't count your approval of your own pull request, and nobody else is a code owner on %s" % branch
           if need else "story-gate couldn't read the rules on %s, and nobody else is a code owner there" % branch)
    raise NoAuthor("%s. Do one, then try again: run `story-gate setup-agent` and install the agent App on %s (the App opens "
                   "story-gate's pull requests and you approve them), or add a second code owner to .github/CODEOWNERS."
                   % (why, repo))


def agent_record():
    """The saved agent App record (agent.json). Raises AgentMissing when setup-agent has not run."""
    p = config_dir() / "agent.json"
    if not p.exists():
        raise AgentMissing("no agent App yet. Run: gate.py setup-agent")
    rec = json.loads(p.read_text(encoding="utf-8"))
    if not os.path.isfile(rec.get("key", "")):  # same folder seen from another OS (e.g. a Windows drive seen from a Linux sandbox)
        rec["key"] = str(config_dir() / Path(rec.get("key", "").replace("\\", "/")).name)
    return rec


def openssl():
    """Path to an openssl binary (PATH, or Git for Windows' copy). Raises RuntimeError when there is none."""
    cands = [shutil.which("openssl")]
    if os.name == "nt":
        cands += [r"C:\Program Files\Git\usr\bin\openssl.exe", r"C:\Program Files\Git\mingw64\bin\openssl.exe"]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    raise RuntimeError("openssl not found. Install Git for Windows (it includes openssl) or your OS openssl package.")


def b64u(b):
    """Base64url without padding, as JWTs use it."""
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def app_jwt(rec):
    """A 9-minute RS256 JWT for the agent App, signed with its private key by openssl (refuses a key others can read)."""
    ok, detail = key_access(rec["key"])
    if not ok:
        raise RuntimeError("the agent key %s is readable by other users (%s). Re-run `gate.py setup-agent`, or on Mac/Linux run chmod 600 on it" % (rec["key"], detail))
    now_ = int(time.time())
    msg = b64u(json.dumps({"alg": "RS256", "typ": "JWT"}).encode()) + "." + b64u(json.dumps({"iat": now_ - 60, "exp": now_ + 540, "iss": str(rec["id"])}).encode())
    r = subprocess.run([openssl(), "dgst", "-sha256", "-sign", rec["key"]], input=msg.encode(), capture_output=True)
    if r.returncode != 0:
        raise RuntimeError("openssl could not sign the token")
    return msg + "." + b64u(r.stdout)


def agent_token(repo):
    """1-hour installation token for the agent App, limited to one repository."""
    rec = agent_record()
    jwt = app_jwt(rec)
    st, inst, _ = call("GET", "/repos/%s/installation" % repo, jwt)
    if st == 404:
        raise AgentMissing("the agent App is not installed on %s. Install it: %s" % (repo, rec["install_url"]))
    if st != 200:
        raise RuntimeError("GitHub didn't say whether the agent App is installed on %s (HTTP %s)" % (repo, st))
    have = inst.get("permissions") or {}
    # Ask only for what an AI builder may have (AGENT_MAX_PERMS), never more than the App holds: an App given extra
    # access (workflows, secrets, ...) still hands the AI a token without it.
    want = {k: (v if _RANK.get(have[k], 0) >= _RANK[v] else have[k]) for k, v in AGENT_MAX_PERMS.items()
            if _RANK.get(have.get(k), 0) > 0}
    if not want:
        raise RuntimeError("the agent App on %s has none of the access an AI builder needs (%s)" % (
            repo, ", ".join("%s: %s" % kv for kv in sorted(AGENT_MAX_PERMS.items()))))
    st, tok, _ = call("POST", "/app/installations/%s/access_tokens" % inst["id"], jwt,
                      {"repositories": [repo.split("/", 1)[1]], "permissions": want})
    if st not in (200, 201):
        raise RuntimeError("GitHub refused an agent token (HTTP %s)" % st)
    return tok["token"], tok.get("expires_at"), dict(rec, permissions=tok.get("permissions") or {}, installation_permissions=have)


def bot_identity(rec):
    st, u, _ = call("GET", "/users/%s%%5Bbot%%5D" % rec["slug"])
    uid = (u or {}).get("id", rec["id"])
    return "%s[bot]" % rec["slug"], "%s+%s[bot]@users.noreply.github.com" % (uid, rec["slug"])


# ------------------------------------------------------------------ device flow (human sign-in without pasting a token)
def _form_post(url, fields):
    """POST form-encoded fields, ask for JSON back. -> parsed dict. Tests replace this."""
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(), method="POST",
                                 headers={"Accept": "application/json", "User-Agent": "story-gate",
                                          "Content-Type": "application/x-www-form-urlencoded"})
    try:
        raw = urllib.request.urlopen(req, timeout=30).read().decode()
    except urllib.error.HTTPError as e:
        raw = e.read().decode() or "{}"
    try:
        return json.loads(raw)
    except ValueError:
        raise RuntimeError("GitHub sent a reply we could not read. Try again in a minute.")


def device_flow(client_id, scope="repo workflow", on_code=None, sleep=time.sleep, now=time.time):
    """GitHub OAuth device flow. Shows the user a code to type at github.com/login/device. -> access token."""
    d = _form_post(WEB + "/login/device/code", {"client_id": client_id, "scope": scope})
    if not d.get("device_code"):
        raise RuntimeError("GitHub did not start sign-in (%s). Check the OAuth app's client id and that device flow is enabled."
                           % (d.get("error_description") or d.get("error") or "no reason given"))
    if on_code:
        on_code(d["user_code"], d.get("verification_uri") or WEB + "/login/device")
    interval = int(d.get("interval") or 5)
    deadline = now() + int(d.get("expires_in") or 900)
    while now() < deadline:
        sleep(interval)
        r = _form_post(WEB + "/login/oauth/access_token", {"client_id": client_id, "device_code": d["device_code"],
                                                          "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
        if r.get("access_token"):
            return r["access_token"]
        err = r.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval = int(r.get("interval") or interval + 5)
            continue
        if err == "access_denied":
            raise RuntimeError("Sign-in was cancelled on GitHub. Run it again and choose Authorize.")
        if err == "expired_token":
            break
        raise RuntimeError("GitHub sign-in failed (%s)." % (r.get("error_description") or err or "unknown reason"))
    raise RuntimeError("The sign-in code expired before it was used. Run it again and enter the code sooner.")


# ------------------------------------------------------------------ setup pull request (Git Data API)
WORKFLOW_DIR = ".github/workflows/"


def is_workflow_path(path):
    """True for a file GitHub treats as a workflow (only a token with the 'workflows' permission may change it)."""
    return path.replace("\\", "/").lower().startswith(WORKFLOW_DIR)


def open_setup_pr(token, repo, files, branch="story-gate-setup", title="Set up story-gate",
                  body="Adds story-gate files. Review and merge to turn the gate on.", base=None, parent=None,
                  update_existing=False, app_token=None):
    """One commit with every file in `files` ({posix path: bytes}) on a new branch, then a PR. -> {number, url, branch}.
    `parent`: the base commit the files were computed from (default: the base branch's tip now).
    When a PR from `branch` into `base` is already open it is returned with `existing: True`. With `update_existing`,
    that branch first gets one commit on top of its tip whose content is exactly the base branch now plus `files`
    (anything else pushed to the branch is undone; no commit when it already matches), and `updated` says whether it
    did. An open PR from `branch` into another base is an error, never updated.
    With `app_token` (pr_author said 'split'): `token` (yours) commits only the workflow files, then the agent App commits
    everything else on top and opens the pull request, so the App is its author and last pusher and your approval counts.
    The App never writes a workflow file."""
    if not files:
        raise RuntimeError("nothing to commit: no files were given")

    def checked(tok):
        def ok(method, path, payload=None, want=(200, 201)):
            st, data, _ = call(method, path, tok, payload)
            if st not in want or not isinstance(data, (dict, list)):
                raise RuntimeError("GitHub refused %s %s (HTTP %s %s)" % (method, path, st, (data or {}).get("message", "") if isinstance(data, dict) else ""))
            return data
        return ok

    ok = checked(token)
    if app_token:
        return _open_split_pr(ok, checked(app_token), app_token, repo, files, branch, title, body, base, parent, update_existing)

    r = "/repos/%s" % repo
    owner = repo.split("/", 1)[0]
    st, open_prs, _ = call("GET", "%s/pulls?state=open&head=%s" % (r, urllib.parse.quote("%s:%s" % (owner, branch))), token)
    def same_repo(pr):
        """A PR whose head branch lives in `repo` itself (the head filter alone also matches other repos of the owner)."""
        head = pr.get("head") if isinstance(pr, dict) else None
        hrepo = head.get("repo") if isinstance(head, dict) else None
        return isinstance(hrepo, dict) and hrepo.get("full_name") == repo
    found = next((pr for pr in open_prs if same_repo(pr)), None) if st == 200 and isinstance(open_prs, list) else None
    if found and not update_existing:  # `existing`: the files were NOT written; the caller decides
        return {"number": found["number"], "url": found["html_url"], "branch": branch, "existing": True}
    base = base or ok("GET", r)["default_branch"]
    tip = parent or ok("GET", "%s/git/ref/heads/%s" % (r, urllib.parse.quote(base)))["object"]["sha"]
    base_tree = ok("GET", "%s/git/commits/%s" % (r, tip))["tree"]["sha"]
    if found:
        into = (found.get("base") or {}).get("ref")
        if into != base:
            raise RuntimeError(f"pull request #{found['number']} from '{branch}' goes into '{into}', not '{base}'. "
                               "Close it, then run this again.")
        res = {"number": found["number"], "url": found["html_url"], "branch": branch, "existing": True}
        res["updated"] = _update_branch(ok, r, branch, _tree_with(ok, r, base_tree, files), title)
        return res
    tree = _tree_with(ok, r, base_tree, files)
    commit = ok("POST", r + "/git/commits", {"message": title, "tree": tree, "parents": [tip]})
    st, ref, _ = call("POST", r + "/git/refs", token, {"ref": "refs/heads/" + branch, "sha": commit["sha"]})
    if st == 422:
        raise RuntimeError("the branch '%s' already exists on %s. Merge or delete it on GitHub, then run this again." % (branch, repo))
    if st not in (200, 201):
        raise RuntimeError("GitHub refused to create the branch (HTTP %s %s)" % (st, (ref or {}).get("message", "")))
    pr = ok("POST", r + "/pulls", {"title": title, "head": branch, "base": base, "body": body})
    return {"number": pr["number"], "url": pr["html_url"], "branch": branch}


def _workflow_folders(ok, r, tree_sha):
    """[(path, tree sha)] of every .github/workflows folder (any letter case) in a commit tree."""
    out = []
    for gh in ok("GET", "%s/git/trees/%s" % (r, tree_sha)).get("tree") or []:
        if gh.get("type") == "tree" and gh.get("path", "").lower() == ".github":
            for wf in ok("GET", "%s/git/trees/%s" % (r, gh["sha"])).get("tree") or []:
                if wf.get("path", "").lower() == "workflows":
                    out.append((gh["path"] + "/" + wf["path"], wf["sha"]))
    return sorted(out)


def _workflows_differ(ok, r, tree_a, tree_b):
    """True when two commit trees hold different workflow files."""
    return _workflow_folders(ok, r, tree_a) != _workflow_folders(ok, r, tree_b)


def _open_split_pr(ok, app_ok, app_token, repo, files, branch, title, body, base, parent, update_existing):
    """open_setup_pr when the human writes the workflow files and the agent App writes the rest and opens the PR."""
    r = "/repos/%s" % repo
    wf = {p: c for p, c in files.items() if is_workflow_path(p)}
    rest = {p: c for p, c in files.items() if not is_workflow_path(p)}
    owner = repo.split("/", 1)[0]
    st, open_prs, _ = call("GET", "%s/pulls?state=open&head=%s" % (r, urllib.parse.quote("%s:%s" % (owner, branch))), app_token)
    found = next((pr for pr in open_prs if isinstance(pr, dict) and ((pr.get("head") or {}).get("repo") or {}).get("full_name") == repo),
                 None) if st == 200 and isinstance(open_prs, list) else None
    if found and not update_existing:
        return {"number": found["number"], "url": found["html_url"], "branch": branch, "existing": True}
    if found and ((found.get("user") or {}).get("type") != "Bot"):
        # opened as a person: that person's approval would never count, so don't keep feeding it
        raise RuntimeError("pull request #%s from '%s' was opened by %s, whose approval GitHub won't count. Close it (and delete "
                           "the branch), then press the button again: the agent App will open a new one."
                           % (found["number"], branch, (found.get("user") or {}).get("login", "a person")))
    base = base or ok("GET", r)["default_branch"]
    tip = parent or ok("GET", "%s/git/ref/heads/%s" % (r, urllib.parse.quote(base)))["object"]["sha"]
    base_tree = ok("GET", "%s/git/commits/%s" % (r, tip))["tree"]["sha"]
    if found:
        into = (found.get("base") or {}).get("ref")
        if into != base:
            raise RuntimeError(f"pull request #{found['number']} from '{branch}' goes into '{into}', not '{base}'. "
                               "Close it, then run this again.")
        ref = f"{r}/git/refs/heads/{urllib.parse.quote(branch)}"
        head = ok("GET", f"{r}/git/ref/heads/{urllib.parse.quote(branch)}")["object"]["sha"]
        wf_tree = _tree_with(ok, r, base_tree, wf) if wf else base_tree
        want = _tree_with(app_ok, r, wf_tree, rest) if rest else wf_tree
        # the title and body are always story-gate's own, whoever opened the pull request first
        app_ok("PATCH", f"{r}/pulls/{found['number']}", {"title": title, "body": body})
        head_commit = ok("GET", f"{r}/git/commits/{head}")
        if head_commit["tree"]["sha"] == want and _by_bot(app_ok, r, head):
            return {"number": found["number"], "url": found["html_url"], "branch": branch, "existing": True, "updated": False}
        if head_commit["tree"]["sha"] != want and (wf or _workflows_differ(ok, r, base_tree, head_commit["tree"]["sha"])):
            # You bring the workflow files to exactly what they should be (also when that means undoing an earlier
            # workflow change on the branch); the App's commit then changes only the other files. With no workflow
            # change to make or undo, there is no commit of yours.
            head = ok("POST", r + "/git/commits", {"message": title + " (workflow files)", "tree": wf_tree, "parents": [head]})["sha"]
            ok("PATCH", ref, {"sha": head})
        app = app_ok("POST", r + "/git/commits", {"message": title, "tree": want, "parents": [head]})
        app_ok("PATCH", ref, {"sha": app["sha"]})
        return {"number": found["number"], "url": found["html_url"], "branch": branch, "existing": True, "updated": True}
    start = tip
    if wf:
        wf_tree = _tree_with(ok, r, base_tree, wf)
        start = ok("POST", r + "/git/commits", {"message": title + " (workflow files)", "tree": wf_tree, "parents": [tip]})["sha"]
    else:
        wf_tree = base_tree
    # the branch is created by whoever wrote its first commit: you when there are workflow files, else the App
    creator = ok if wf else app_ok
    try:
        creator("POST", r + "/git/refs", {"ref": "refs/heads/" + branch, "sha": start})
    except RuntimeError as ex:
        if "HTTP 422" not in str(ex):
            raise
        # The branch exists but no pull request does. Reuse it only when its tip is story-gate's own commit from a run
        # that stopped half way; anything else may be someone's work, so stop and say so.
        tip_now = ok("GET", f"{r}/git/ref/heads/{urllib.parse.quote(branch)}")["object"]["sha"]
        msg = (ok("GET", f"{r}/git/commits/{tip_now}").get("message") or "").strip()
        if msg not in (title, title + " (workflow files)"):
            raise RuntimeError("the branch '%s' already exists on %s and isn't one story-gate left behind. Merge or delete it "
                               "on GitHub, then run this again." % (branch, repo))
        creator("PATCH", f"{r}/git/refs/heads/{urllib.parse.quote(branch)}", {"sha": start, "force": True})
    # the App's commit always comes last (even when it changes nothing), so the App is the last pusher
    app_tree = _tree_with(app_ok, r, wf_tree, rest) if rest else wf_tree
    app = app_ok("POST", r + "/git/commits", {"message": title, "tree": app_tree, "parents": [start]})
    app_ok("PATCH", f"{r}/git/refs/heads/{urllib.parse.quote(branch)}", {"sha": app["sha"]})
    pr = app_ok("POST", r + "/pulls", {"title": title, "head": branch, "base": base, "body": body})
    return {"number": pr["number"], "url": pr["html_url"], "branch": branch}


def _by_bot(ok, r, sha):
    """True when commit `sha` was committed by a bot (the agent App), so a person's approval of it counts."""
    c = ok("GET", f"{r}/commits/{sha}")
    return ((c.get("committer") or {}).get("type") == "Bot") or ((c.get("author") or {}).get("type") == "Bot")


def _tree_with(ok, r, base_tree, files):
    """A tree: `base_tree` with every file in `files` ({posix path: bytes, or None to delete it}) written over it. -> its sha.
    `ok` is open_setup_pr's checked GitHub call."""
    entries = []
    for path, content in sorted(files.items()):
        if content is None:  # delete the file
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
            continue
        blob = ok("POST", r + "/git/blobs", {"content": base64.b64encode(content).decode(), "encoding": "base64"})
        entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
    return ok("POST", r + "/git/trees", {"base_tree": base_tree, "tree": entries})["sha"]


def _update_branch(ok, r, branch, tree, message):
    """Give `branch` the content `tree` in one commit on top of its tip (a fast-forward: if the branch moves meanwhile,
    GitHub refuses and the error is raised). -> True if a commit was made, False when it already had that content."""
    tip = ok("GET", f"{r}/git/ref/heads/{urllib.parse.quote(branch)}")["object"]["sha"]
    if ok("GET", f"{r}/git/commits/{tip}")["tree"]["sha"] == tree:
        return False
    commit = ok("POST", r + "/git/commits", {"message": message, "tree": tree, "parents": [tip]})
    ok("PATCH", f"{r}/git/refs/heads/{urllib.parse.quote(branch)}", {"sha": commit["sha"]})
    return True


def pr_merged(token, repo, number):
    st, pr, _ = call("GET", "/repos/%s/pulls/%s" % (repo, number), token)
    if st != 200 or not isinstance(pr, dict):
        raise RuntimeError("could not read pull request #%s (HTTP %s)" % (number, st))
    return bool(pr.get("merged"))


# ------------------------------------------------------------------ Actions secret (libsodium sealed box, pure Python)
# crypto_box_seal(m, recipient_pk) = ephemeral_pk || crypto_box(m, nonce, recipient_pk, ephemeral_sk)
#   nonce = BLAKE2b-24(ephemeral_pk || recipient_pk)
#   crypto_box = XSalsa20-Poly1305 keyed with HSalsa20(X25519(sk, pk), 16 zero bytes)
# Encryption only: nothing here needs to be constant-time, and it is only used on a secret the caller already holds.
_M32 = 0xFFFFFFFF
_P255 = 2 ** 255 - 19


def x25519(k, u):
    """RFC 7748 X25519. k: 32-byte scalar, u: 32-byte point -> 32 bytes."""
    k = bytearray(k); k[0] &= 248; k[31] &= 127; k[31] |= 64
    k = int.from_bytes(k, "little")
    x1 = int.from_bytes(u, "little") & ((1 << 255) - 1)
    x2, z2, x3, z3, swap = 1, 0, x1, 1, 0
    for t in range(254, -1, -1):
        bit = (k >> t) & 1
        if swap ^ bit:
            x2, x3, z2, z3 = x3, x2, z3, z2
        swap = bit
        a, b = (x2 + z2) % _P255, (x2 - z2) % _P255
        c, d = (x3 + z3) % _P255, (x3 - z3) % _P255
        aa, bb, da, cb = a * a % _P255, b * b % _P255, d * a % _P255, c * b % _P255
        e = (aa - bb) % _P255
        x3, z3 = (da + cb) ** 2 % _P255, x1 * (da - cb) ** 2 % _P255
        x2, z2 = aa * bb % _P255, e * (aa + 121665 * e) % _P255
    if swap:
        x2, x3, z2, z3 = x3, x2, z3, z2
    return (x2 * pow(z2, _P255 - 2, _P255) % _P255).to_bytes(32, "little")


def _rotl(v, n):
    return ((v << n) & _M32) | (v >> (32 - n))


def _salsa_rounds(x):
    """20 Salsa20 rounds on a list of 16 words (in place); returns the list."""
    for _ in range(10):
        for a, b, c, d in ((0, 4, 8, 12), (5, 9, 13, 1), (10, 14, 2, 6), (15, 3, 7, 11),   # columns
                           (0, 1, 2, 3), (5, 6, 7, 4), (10, 11, 8, 9), (15, 12, 13, 14)):  # rows
            x[b] ^= _rotl((x[a] + x[d]) & _M32, 7)
            x[c] ^= _rotl((x[b] + x[a]) & _M32, 9)
            x[d] ^= _rotl((x[c] + x[b]) & _M32, 13)
            x[a] ^= _rotl((x[d] + x[c]) & _M32, 18)
    return x


_SIGMA = (0x61707865, 0x3320646E, 0x79622D32, 0x6B206574)  # "expand 32-byte k"


def _hsalsa20(key, n16):
    """HSalsa20: 32-byte key + 16-byte input -> 32-byte subkey."""
    import struct
    k, n = struct.unpack("<8I", key), struct.unpack("<4I", n16)
    x = _salsa_rounds([_SIGMA[0], k[0], k[1], k[2], k[3], _SIGMA[1], n[0], n[1], n[2], n[3], _SIGMA[2], k[4], k[5], k[6], k[7], _SIGMA[3]])
    return struct.pack("<8I", x[0], x[5], x[10], x[15], x[6], x[7], x[8], x[9])


def _salsa20_block(key, n8, counter):
    import struct
    k, n = struct.unpack("<8I", key), struct.unpack("<2I", n8)
    init = [_SIGMA[0], k[0], k[1], k[2], k[3], _SIGMA[1], n[0], n[1], counter & _M32, counter >> 32, _SIGMA[2], k[4], k[5], k[6], k[7], _SIGMA[3]]
    x = _salsa_rounds(list(init))
    return struct.pack("<16I", *[(a + b) & _M32 for a, b in zip(x, init)])


def _xsalsa20_stream(key, nonce24, length):
    sub, out, ctr = _hsalsa20(key, nonce24[:16]), b"", 0
    while len(out) < length:
        out += _salsa20_block(sub, nonce24[16:], ctr)
        ctr += 1
    return out[:length]


def _poly1305(key, msg):
    r = int.from_bytes(key[:16], "little") & 0x0ffffffc0ffffffc0ffffffc0fffffff
    s, p, acc = int.from_bytes(key[16:32], "little"), (1 << 130) - 5, 0
    for i in range(0, len(msg), 16):
        chunk = msg[i:i + 16]
        acc = (acc + int.from_bytes(chunk + b"\x01", "little")) * r % p
    return ((acc + s) & ((1 << 128) - 1)).to_bytes(16, "little")


def _crypto_box(msg, nonce24, their_pk, my_sk):
    """NaCl crypto_box: tag(16) || ciphertext."""
    key = _hsalsa20(x25519(my_sk, their_pk), b"\0" * 16)
    stream = _xsalsa20_stream(key, nonce24, 32 + len(msg))
    ct = bytes(a ^ b for a, b in zip(msg, stream[32:]))
    return _poly1305(stream[:32], ct) + ct


def seal_box(msg, recipient_pk, eph_sk=None):
    """libsodium crypto_box_seal. eph_sk is only for tests; real use draws a fresh random one."""
    eph_sk = eph_sk or os.urandom(32)
    eph_pk = x25519(eph_sk, (9).to_bytes(32, "little"))
    nonce = hashlib.blake2b(eph_pk + recipient_pk, digest_size=24).digest()
    return eph_pk + _crypto_box(msg, nonce, recipient_pk, eph_sk)


def set_repo_secret(token, repo, name, value):
    """Create or replace a GitHub Actions secret. The value is encrypted here; it is never printed or logged."""
    st, key, _ = call("GET", "/repos/%s/actions/secrets/public-key" % repo, token)
    if st != 200 or not key.get("key") or not key.get("key_id"):
        raise RuntimeError("could not read the repository's secret-encryption key (HTTP %s). The token needs permission to manage Actions secrets." % st)
    pk = base64.b64decode(key["key"])
    if len(pk) != 32:
        raise RuntimeError("GitHub sent an unexpected encryption key")
    enc = base64.b64encode(seal_box(value.encode("utf-8"), pk)).decode()
    st, resp, _ = call("PUT", "/repos/%s/actions/secrets/%s" % (repo, urllib.parse.quote(name, safe="")), token,
                       {"encrypted_value": enc, "key_id": key["key_id"]})
    if st not in (201, 204):
        raise RuntimeError("GitHub did not save secret %s (HTTP %s %s)" % (name, st, (resp or {}).get("message", "") if isinstance(resp, dict) else ""))
