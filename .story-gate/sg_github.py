"""story-gate <-> GitHub: human acceptance, protection checks, review status, JUnit results, setup and agent identity.

Design rules (decided by the frontier panel, Oct 2026):
- Acceptance = a native APPROVED review on the PR's current head commit, by a human (type User) listed in
  CODEOWNERS (or judge.approvers), who is neither the PR author nor the author of the head commit. Comments never count.
- Waivers and drift decisions written by agents are proposals; CI honours them only once that approval exists.
- Agents act under their own GitHub App ("story-gate agent"), never the human's login. The App has no
  `workflows` or `administration` permission, so it cannot edit CI or branch rules.
- Nothing here runs code from the pull request.
"""
import base64, csv, http.server, json, os, re, secrets, shutil, subprocess, tempfile, time, urllib.error, urllib.parse, urllib.request, webbrowser
import xml.etree.ElementTree as ET
from pathlib import Path

API = os.environ.get("GITHUB_API_URL", "https://api.github.com")
WEB = os.environ.get("GITHUB_SERVER_URL", "https://github.com")


# ------------------------------------------------------------------ REST / GraphQL
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
        r = urllib.request.urlopen(req, timeout=30)
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
    want = {r.lower().replace("[bot]", "") for r in reviewers}
    n, after = 0, None
    for _ in range(50):  # 5000 threads is far beyond any real PR
        d = graphql(q, {"o": owner, "n": name, "p": int(ctx["number"]), "a": after}, token)
        rt = d["repository"]["pullRequest"]["reviewThreads"]
        for t in rt["nodes"]:
            a = (((t.get("comments") or {}).get("nodes") or [{}])[0].get("author") or {}).get("login", "")
            if not t.get("isResolved") and a.lower().replace("[bot]", "") in want:
                n += 1
        if not rt["pageInfo"]["hasNextPage"]:
            break
        after = rt["pageInfo"]["endCursor"]
    return n


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


def protection(repo, branch, token):
    """Best-effort read of the rules that apply to the base branch. -> (enforced: bool, missing: [str])."""
    st, rules, _ = call("GET", "/repos/%s/rules/branches/%s" % (repo, urllib.parse.quote(branch, safe="")), token)
    if st != 200 or not isinstance(rules, list):
        return False, ["could not read branch rules (HTTP %s)" % st]
    pr = [r.get("parameters") or {} for r in rules if r.get("type") == "pull_request"]
    checks = [c.get("context") for r in rules if r.get("type") == "required_status_checks" for c in (r.get("parameters") or {}).get("required_status_checks", [])]
    missing = []
    if not pr:
        missing.append("pull request reviews are not required")
    else:
        p = pr[0]
        if not p.get("require_code_owner_review"):
            missing.append("code owner review is not required")
        if not p.get("dismiss_stale_reviews_on_push"):
            missing.append("stale approvals are not dismissed on new pushes")
        if not p.get("require_last_push_approval"):
            missing.append("approval of the most recent push is not required")
        if int(p.get("required_approving_review_count") or 0) < 1:
            missing.append("no approving review is required")
    if "story-gate" not in checks:
        missing.append("the story-gate check is not a required status check")
    return not missing, missing


# ------------------------------------------------------------------ JUnit
RANK = {"passed": 0, "skipped": 1, "failed": 2}


def junit(path, max_bytes=5_000_000):
    """-> {test_name: outcome} with outcome in passed|failed|skipped. Rejects oversized files and DTDs."""
    p = Path(path)
    if not p.is_file():
        return None
    raw = p.read_bytes()
    if len(raw) > max_bytes or b"<!DOCTYPE" in raw[:2000].upper() or b"<!ENTITY" in raw:
        raise ValueError("JUnit report rejected (too large or contains a DTD/entity)")
    root = ET.fromstring(raw)
    out = {}
    for tc in root.iter("testcase"):
        name, cls = tc.get("name") or "", tc.get("classname") or ""
        if tc.find("failure") is not None or tc.find("error") is not None:
            o = "failed"
        elif tc.find("skipped") is not None:
            o = "skipped"
        else:
            o = "passed"
        for key in {name, (cls + "." + name) if cls else name, (cls + "::" + name) if cls else name}:
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


def ruleset_json():
    return {"name": RULESET_NAME, "target": "branch", "enforcement": "active",
            "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
            "bypass_actors": [],
            "rules": [{"type": "deletion"}, {"type": "non_fast_forward"},
                      {"type": "pull_request", "parameters": {"required_approving_review_count": 1, "dismiss_stale_reviews_on_push": True,
                                                              "require_code_owner_review": True, "require_last_push_approval": True,
                                                              "required_review_thread_resolution": True}},
                      {"type": "required_status_checks", "parameters": {"strict_required_status_checks_policy": False,
                                                                        "required_status_checks": [{"context": "story-gate"}]}}]}


def setup_repo(root, repo, owners, token, dry_run=False):
    """Human-run, one time: CODEOWNERS, ruleset, Actions cannot approve PRs. Returns a list of plain-English results."""
    out = []
    co = Path(root) / ".github" / "CODEOWNERS"
    line = "* " + " ".join("@" + o.lstrip("@") for o in owners)
    text = co.read_text(encoding="utf-8") if co.exists() else ""
    have = {u.lower() for u in codeowners(text)[0]}  # the effective (last) '*' rule, by exact name
    if not all(o.lstrip("@").lower() in have for o in owners):
        co.parent.mkdir(parents=True, exist_ok=True)
        if not dry_run:
            co.write_text((text.rstrip() + "\n" if text.strip() else "# Code owners: the humans who accept work. Bots and apps cannot be code owners.\n") + line + "\n", encoding="utf-8")
        out.append("CODEOWNERS: added '%s' (commit and merge this file)" % line)
    else:
        out.append("CODEOWNERS: already lists %s" % ", ".join(owners))
    if dry_run:
        return out + ["(dry run: no GitHub changes)"]
    st, existing, _ = call("GET", "/repos/%s/rulesets" % repo, token)
    if st == 200 and any(r.get("name") == RULESET_NAME for r in existing):
        out.append("Ruleset: '%s' already exists" % RULESET_NAME)
    else:
        st, r, _ = call("POST", "/repos/%s/rulesets" % repo, token, ruleset_json())
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
    return ("<!doctype html><meta charset=utf-8><title>story-gate setup</title><style>body{font:16px/1.5 system-ui,sans-serif;"
            "background:#FFF8F2;color:#29122B;max-width:640px;margin:10vh auto;padding:0 24px}button{background:#FF4B20;color:#29122B;"
            "border:0;border-radius:10px;padding:12px 18px;font-weight:600}</style>" + inner)


def agent_record():
    p = config_dir() / "agent.json"
    if not p.exists():
        raise RuntimeError("no agent App yet. Run: gate.py setup-agent")
    rec = json.loads(p.read_text(encoding="utf-8"))
    if not os.path.isfile(rec.get("key", "")):  # same folder seen from another OS (e.g. F:\ENV from a Linux sandbox)
        rec["key"] = str(config_dir() / Path(rec.get("key", "").replace("\\", "/")).name)
    return rec


def openssl():
    cands = [shutil.which("openssl")]
    if os.name == "nt":
        cands += [r"C:\Program Files\Git\usr\bin\openssl.exe", r"C:\Program Files\Git\mingw64\bin\openssl.exe"]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    raise RuntimeError("openssl not found. Install Git for Windows (it includes openssl) or your OS openssl package.")


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def app_jwt(rec):
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
    if st != 200:
        raise RuntimeError("the agent App is not installed on %s. Install it: %s" % (repo, rec["install_url"]))
    st, tok, _ = call("POST", "/app/installations/%s/access_tokens" % inst["id"], jwt, {"repositories": [repo.split("/", 1)[1]]})
    if st not in (200, 201):
        raise RuntimeError("GitHub refused an agent token (HTTP %s)" % st)
    return tok["token"], tok.get("expires_at"), rec


def bot_identity(rec):
    st, u, _ = call("GET", "/users/%s%%5Bbot%%5D" % rec["slug"])
    uid = (u or {}).get("id", rec["id"])
    return "%s[bot]" % rec["slug"], "%s+%s[bot]@users.noreply.github.com" % (uid, rec["slug"])
