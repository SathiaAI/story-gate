"""story-gate init: guided setup in the browser.

The AI (or the person) runs `story-gate init` inside the project folder. A page opens on this computer
(127.0.0.1 only) and walks the person through the only steps that must be theirs:

  1. sign in to GitHub            (GitHub's device sign-in, or an existing `gh` login)
  2. create the AI's own login    (GitHub App manifest: one "Create" click, then "Install")
  3. add the judge key            (typed into the page, encrypted straight into a GitHub secret)
  4. merge the setup pull request (opened for them; branch rules are applied after the merge)

Everything else (files, workflows, CODEOWNERS, branch rules, hooks on this computer) is done for them.
Stdlib only. Nothing here runs from a repository branch: it runs from the installed package or a repo copy
of story-gate that the person chose to run.
"""
import html, http.server, json, os, secrets, shutil, subprocess, sys, tempfile, threading, time, urllib.parse, webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import sg_github as G  # noqa: E402

# story-gate's own OAuth app (SathiaAI, device flow only; a client ID is public, there is no secret). Empty: fall back to `gh`.
BUILTIN_OAUTH_CLIENT_ID = "Ov23liAhA19MDXrS9a4S"
OAUTH_CLIENT_ID = os.environ.get("STORY_GATE_OAUTH_CLIENT_ID", BUILTIN_OAUTH_CLIENT_ID)
STEPS = ("signin", "agent", "key", "merge", "done")
# AI tools story-gate can check live on this computer (user-level hooks), in the order the page lists them.
HOOKED_TOOLS = (("claude", "Claude Code"), ("codex", "Codex"), ("cursor", "Cursor"), ("vscode", "VS Code (Copilot agent)"),
                ("hermes", "Hermes"), ("gemini", "Gemini CLI"), ("windsurf", "Windsurf"))
# Tools with no verified live checks yet: they follow the rules in AGENTS.md / the skill, and GitHub checks their pull requests.
OTHER_TOOLS = (("Antigravity", "rules + skill; live checks once we've verified its hooks"), ("pi", "rules + skill"),
               ("Roo Code", "rules + skill"), ("ChatGPT", "GitHub checks its pull requests"),
               ("Google AI Studio", "GitHub checks its pull requests"))
GH_USER = __import__("re").compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")


def git(cwd, *a):
    r = subprocess.run(["git", *a], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    return r.stdout.strip() if r.returncode == 0 else ""


def github_repo(top):
    """owner/name from the origin remote, or None when origin isn't on github.com."""
    url = git(top, "config", "--get", "remote.origin.url")  # as written, before any url.insteadOf rewriting
    for pre in ("https://github.com/", "git@github.com:", "ssh://git@github.com/"):
        if url.startswith(pre):
            name = url[len(pre):].rstrip("/")
            return name[:-4] if name.endswith(".git") else name
    return None


def has_agent():
    try:
        G.agent_record()
        return True
    except Exception:
        return False


def setup_files(top, base, py):
    """The files the setup pull request adds: story-gate installed into a clean copy of the default branch."""
    links = setup_links(top, "origin/" + base)
    if links:
        raise RuntimeError("%s has links (symlinks) where story-gate writes its files (%s). Writing through them could change "
                           "files outside the repository, so story-gate stops. Replace them with real files or folders, then try "
                           "again." % (base, ", ".join(links[:5])))
    tmp = Path(tempfile.mkdtemp(prefix="sg-setup-"))
    wt = tmp / "wt"
    try:
        if subprocess.run(["git", "worktree", "add", "--detach", str(wt), "origin/" + base], cwd=str(top), capture_output=True).returncode:
            raise RuntimeError("could not read origin/%s - run `git fetch` and try again" % base)
        dest = wt / ".story-gate"
        dest.mkdir(exist_ok=True)
        import sg_trust as T
        for f in T.runtime_files(HERE):  # the same files the runtime is made of, plus the repo's own settings
            (dest / f).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(HERE / f, dest / f)
        for f in T.RELEASE_PROOF:  # the release signature, so CI can check the copy is a signed release
            if (HERE / f).is_file():
                shutil.copyfile(HERE / f, dest / f)
            elif (dest / f).is_file():
                (dest / f).unlink()  # an old signature would no longer match these files
        env = dict(os.environ, STORY_GATE_ROOT=str(wt))
        r = subprocess.run([sys.executable, str(dest / "gate.py"), "install", "--python", py], cwd=str(wt), env=env,
                           capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError("repository setup failed: %s" % (r.stderr or r.stdout)[-400:])
        out = {}
        # Raw NUL-separated output: git() strips the whole text, which would eat the leading space of the first " M path"
        # line, and the -z form never quotes odd file names.
        st = subprocess.run(["git", "status", "--porcelain", "-z", "--untracked-files=all"], cwd=str(wt), capture_output=True)
        parts = st.stdout.decode("utf-8", "replace").split("\0")
        i = 0
        while i < len(parts):
            entry, i = parts[i], i + 1
            if len(entry) < 4:
                continue
            if "R" in entry[:2] or "C" in entry[:2]:
                i += 1  # a rename or copy is followed by the old path
            p = entry[3:]
            if p and (wt / p).is_file() and "__pycache__" not in p:
                out[p] = _lf(top, base, p, (wt / p).read_bytes())
        cfgf = dest / "config.json"  # an unchanged, already-tracked config is still the one setup's choices go into
        if cfgf.is_file():
            out.setdefault(".story-gate/config.json", _lf(top, base, ".story-gate/config.json", cfgf.read_bytes()))
        return out
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=str(top), capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)


SETUP_PATHS = (".story-gate", ".github", ".agents", ".claude", ".codex", ".cursor", ".gemini", ".devin", ".windsurf", ".grok",
               "AGENTS.md", "CLAUDE.md", "GEMINI.md", ".gitignore", "CODEOWNERS", "docs")  # every folder of gate.HOOK_FILES too


def setup_links(top, ref):
    """Symlinks at `ref` on any path setup (and install) writes into, or on a folder above one. -> their paths."""
    r = subprocess.run(["git", "ls-tree", "-r", "-t", "--full-tree", ref], cwd=str(top), capture_output=True, text=True)
    out = []
    for line in r.stdout.splitlines():
        meta, _, path = line.partition("\t")
        # any letter case: on Windows and macOS `.Github` and `.github` are the same folder
        if meta.split(" ")[0] == "120000" and path.split("/")[0].lower() in {p.lower() for p in SETUP_PATHS}:
            out.append(path)
    return out


def run_without_judge(files):
    """The owner skipped the judge key: the setup PR turns on objective mode (only the checks story-gate can verify itself
    decide). Choosing it in the reviewed config, not detecting a missing key, keeps a lost key failing closed."""
    raw = files.get(".story-gate/config.json")
    if not raw:
        return False
    c = json.loads(raw.decode("utf-8"))
    c["judge_mode"] = "objective"
    files[".story-gate/config.json"] = (json.dumps(c, indent=1, ensure_ascii=False) + "\n").encode("utf-8")
    return True


def safe_junit_path(jp):
    """A JUnit report path story-gate will read: a file inside the repository, written relative to it."""
    jp = (jp or "").strip()
    parts = jp.replace("\\", "/").split("/")
    return bool(jp) and not jp.startswith(("/", "\\")) and ":" not in jp and ".." not in parts and parts[-1] not in ("", ".")


def pin_junit(files, junit_path, top=None):
    """Where CI finds each test's own result (a JUnit XML file the test command writes). Needed in objective mode.
    Left alone when someone already set it. With `top`, a path that resolves outside the repository (a symlinked
    folder) is refused too, since CI would refuse to read it."""
    raw, jp = files.get(".story-gate/config.json"), (junit_path or "").strip()
    if not raw or not safe_junit_path(jp):
        return False
    if top is not None:
        root = os.path.realpath(str(top))
        if os.path.commonpath([root, os.path.realpath(os.path.join(root, jp.replace("\\", "/")))]) != root:
            return False
    c = json.loads(raw.decode("utf-8"))
    if c.get("junit_path"):
        return False
    c["junit_path"] = jp
    files[".story-gate/config.json"] = (json.dumps(c, indent=1, ensure_ascii=False) + "\n").encode("utf-8")
    return True


def keep_test_command(files, test_command):
    """CI runs the project's tests wherever a test command was given, even where GitHub can't block merges (then the
    check only reports). Never replaces a command someone already set."""
    raw, tc = files.get(".story-gate/config.json"), (test_command or "").strip()
    if not raw or not tc:
        return False
    c = json.loads(raw.decode("utf-8"))
    if c.get("test_command"):
        return False
    c["test_command"] = tc
    files[".story-gate/config.json"] = (json.dumps(c, indent=1, ensure_ascii=False) + "\n").encode("utf-8")
    return True


def block_merges_in_ci(files, test_command):
    """Where GitHub can enforce branch rules, a red story-gate check blocks the merge from day one: enforce at the PR check
    ("ci"), while the live checks on the AI's computer still only warn. Only with a test command: without one CI can't run
    the tests, so every story would fail and nothing could merge. Returns True when the config was changed."""
    raw = files.get(".story-gate/config.json")
    if not raw or not (test_command or "").strip():
        return False
    c = json.loads(raw.decode("utf-8"))
    if c.get("mode") != "warn" or c.get("enforce_points") or c.get("test_command"):
        return False  # someone chose these settings already: leave them
    c["enforce_points"] = ["ci"]
    c["test_command"] = test_command.strip()
    files[".story-gate/config.json"] = (json.dumps(c, indent=1, ensure_ascii=False) + "\n").encode("utf-8")
    return True


class Wizard:
    """State machine behind the page. Each step records ok / error / waiting; the page polls /state."""

    def __init__(self, top, repo, py, token=None, open_browser=True):
        """Fresh wizard for one setup run: every step starts "todo" until the page drives it forward."""
        self.top, self.repo, self.py, self.open_browser = Path(top), repo, py, open_browser
        self.secret = secrets.token_urlsafe(24)
        self.agent_state = secrets.token_urlsafe(16)
        self.token = token
        self.login = None
        self.st = {s: {"status": "todo", "msg": ""} for s in STEPS}
        self.device = None
        self.pr = None
        self.private_free = False
        self.private = False
        self.org = None  # the repository's owner when it is an organization: the AI's login must belong to it
        self.has_issues = True
        self.can_enforce = False  # known only after sign-in; unknown stays advisory
        self.test_command = ""  # from `init --test-command`: the project's tests as CI runs them
        self.objective = False  # the owner chose "Skip for now" at the key step: no AI judge
        self.junit_path = ""  # from `init --junit-path`: the JUnit XML report the test command writes
        import sg_trust as T
        self.found = T.detected_clients()
        self.clients = [cl for cl, _ in HOOKED_TOOLS if cl in self.found] or ["claude", "codex", "cursor"]
        self.approvers = []  # other people who accept work (CODEOWNERS), besides the person signing in
        self.already = False  # the repository already has story-gate on GitHub (a teammate's computer)
        self.base = "main"
        self.lock = threading.Lock()

    # ---- state helpers
    def set(self, step, status, msg=""):
        with self.lock:
            self.st[step] = {"status": status, "msg": msg}

    def snapshot(self):
        with self.lock:
            return {"steps": json.loads(json.dumps(self.st)), "login": self.login, "repo": self.repo, "device": self.device,
                    "pr": self.pr, "private_free": self.private_free, "clients": list(self.clients),
                    "approvers": list(self.approvers), "dashboard": self.dashboard_url()}

    def dashboard_url(self):
        """The pinned dashboard issue, or None when the repository has Issues turned off."""
        import sg_dashboard as D
        return D.issue_url(self.repo, G.WEB) if self.has_issues else None

    def set_clients(self, text):
        """The AI tools to protect on this computer, as ticked on the page (only tools story-gate can check live)."""
        want = [x for x in (text or "").split(",") if x]
        known = [cl for cl, _ in HOOKED_TOOLS]
        if not want or any(x not in known for x in want):
            return False
        with self.lock:
            self.clients = [cl for cl in known if cl in want]
        return True

    def background(self, fn, step=None):
        """Run fn on a thread; a failure is shown on `step`'s card (when given), else on the first step still working."""
        threading.Thread(target=self._guard, args=(fn, step), daemon=True).start()

    def _guard(self, fn, step=None):
        """Run fn; any exception becomes a plain message on the step that owns the work, not whichever step is "working"."""
        try:
            fn()
        except Exception as e:  # every failure becomes a plain message on the page
            step = step or next((s for s in STEPS if self.st[s]["status"] == "working"), "signin")
            self.set(step, "error", str(e)[:400])

    # ---- step 1: sign in
    def start_signin(self):
        """Sign-in step: reuse a working GitHub token, else start the device sign-in flow."""
        tok = self.token or G.human_token()
        if tok and G.whoami(tok):
            return self._signed_in(tok)
        if not OAUTH_CLIENT_ID:
            self.set("signin", "error", "Sign in with the GitHub CLI first: run `gh auth login` in a terminal, then press Start again.")
            return
        self.set("signin", "working", "Waiting for you to enter the code on GitHub")
        self.background(lambda: self._signed_in(G.device_flow(OAUTH_CLIENT_ID, on_code=self._show_code)), "signin")

    def _show_code(self, code, uri):
        with self.lock:
            self.device = {"code": code, "uri": uri}
        if self.open_browser:
            webbrowser.open(uri)

    def _signed_in(self, tok):
        """Loads the repo once sign-in succeeds, and records the owning organization (if any) and its plan."""
        self.token = tok
        self.login = G.whoami(tok)
        st, info, _ = G.call("GET", "/repos/%s" % self.repo, tok)
        if st != 200:
            self.set("signin", "error", "%s can't see %s (HTTP %s). Sign in with the account that owns it." % (self.login, self.repo, st))
            return
        self.base = info.get("default_branch") or "main"
        cst, _, _ = G.call("GET", "/repos/%s/contents/.story-gate/config.json?ref=%s" % (self.repo, self.base), tok)
        self.already = cst == 200
        if self.already:  # set up on GitHub already: this computer only needs the AI's login and local protection
            self.set("key", "ok", "Already set up on GitHub")
            self.set("merge", "ok", "Already set up on GitHub")
        elif not (info.get("permissions") or {}).get("admin"):
            self.set("signin", "error", "%s isn't an admin of %s, so story-gate can't set its rules. Ask the owner to run setup." % (self.login, self.repo))
            return
        self.private = bool(info.get("private"))
        self.has_issues = info.get("has_issues") is not False  # the dashboard is an issue; without Issues it lives in run summaries
        owner = info.get("owner") or {}
        self.org = owner.get("login") if owner.get("type") == "Organization" else None
        paid = self._paid(tok, owner) if self.private else None
        self.private_free = self.private and paid is False
        self.can_enforce = not self.private or paid is True  # GitHub enforces branch rules here (public, or a paid plan)
        self.set("signin", "ok", "Signed in as %s" % self.login)

    def _paid(self, tok, owner):
        """True/False when GitHub shows the owner's plan; None when it doesn't (the sign-in token has no `user` scope and
        orgs show their plan only to owners). Unknown is not "free": the ruleset result after the merge settles it."""
        path = "/user" if owner.get("type") == "User" else "/orgs/%s" % owner.get("login")
        st, o, _ = G.call("GET", path, tok)
        name = (o.get("plan") or {}).get("name") if st == 200 and isinstance(o, dict) else None
        return None if not name else name != "free"

    # ---- step 2: the AI's own GitHub login (agent App)
    def agent_installed(self):
        try:
            G.agent_token(self.repo)
            return True
        except Exception:
            return False

    def agent_form(self, port):
        """GitHub's create-app form. For a repository owned by an organization the app is created in that organization:
        a private app can only be installed on the account that owns it, so a personal app could never reach the repo."""
        name = ("story-gate-agent-%s-%s" % ((self.org or self.login or "me").lower(), secrets.token_hex(2)))[:34]
        mf = json.dumps(G.manifest(name, "http://127.0.0.1:%d/agent/callback" % port))
        where = "/organizations/%s/settings/apps/new" % urllib.parse.quote(self.org, safe="") if self.org else "/settings/apps/new"
        return ('<form id="f" method="post" action="%s%s?state=%s"><input type="hidden" name="manifest" value="%s">'
                '</form><script>document.getElementById("f").submit()</script>' % (G.WEB, where, self.agent_state, html.escape(mf, quote=True)))

    def agent_callback(self, state, code):
        """Finish the agent App setup from GitHub's redirect; False when the state or code doesn't match."""
        if state != self.agent_state or not code:
            return False
        rec = G.setup_agent(code=code, open_browser=False)
        self.set("agent", "working", "Now click Install and choose %s" % self.repo)
        if self.open_browser:
            webbrowser.open(rec["install_url"])
        self.background(self._wait_install, "agent")
        return True

    def _wait_install(self):
        for _ in range(600):
            if self.agent_installed():
                self.set("agent", "ok", "Your AI has its own GitHub login, installed on %s" % self.repo)
                if self.already:
                    self.finish(self.base)
                return
            time.sleep(2)
        self.set("agent", "error", "Didn't see the app installed on %s yet. Install it, then press the button again." % self.repo)

    # ---- step 3: judge key
    def save_key(self, key):
        key = (key or "").strip()
        if len(key) < 20 or any(c.isspace() for c in key):
            self.set("key", "error", "That doesn't look like an OpenRouter key (it starts with sk-or-).")
            return
        G.set_repo_secret(self.token, self.repo, "OPENROUTER_API_KEY", key)
        p = G.config_dir() / "judge.env"
        G.write_private(p, "OPENROUTER_API_KEY=%s\n" % key)
        if self.pr and self.objective:  # the setup PR already says "objective": the key alone doesn't turn the judge on
            self.set("key", "ok", "Saved. The setup pull request still runs without a judge: after merging it, set "
                     "\"judge_mode\": \"full\" in .story-gate/config.json in a pull request of its own")
            return
        self.objective = False
        self.set("key", "ok", "Saved as a GitHub secret and on this computer (never in the repository)")

    def skip_key(self):
        """Run without an AI judge (objective mode). Only before the setup PR exists: its config records the choice."""
        if self.pr:
            self.set("key", "error", "The setup pull request is already open, so it's too late to skip here. Add a key, or "
                     "set \"judge_mode\": \"objective\" in .story-gate/config.json in a pull request after merging.")
            return
        self.objective = True
        self.set("key", "ok", "Skipped: story-gate will run without an AI judge. It still checks the plan, runs your tests and "
                 "the story's scenarios, and needs your approval. To turn the judge on later, add a key and set \"judge_mode\": "
                 "\"full\" in a pull request.")

    # ---- step 4: setup pull request, then branch rules after the merge
    def check_approvers(self, text):
        """Other GitHub users who may approve work. Each must exist and have write access, or GitHub ignores them as owners."""
        names, bad = [], []
        for raw in (text or "").replace(",", " ").split():
            n = raw.strip().lstrip("@")
            if not n or n.lower() == (self.login or "").lower() or n.lower() in (x.lower() for x in names):
                continue
            if not GH_USER.match(n):
                bad.append("'%s' isn't a GitHub username" % raw)
                continue
            st, info, _ = G.call("GET", "/repos/%s/collaborators/%s/permission" % (self.repo, n), self.token)
            perm = (info or {}).get("permission") if st == 200 and isinstance(info, dict) else None
            if perm not in ("admin", "maintain", "write"):
                bad.append("%s can't approve yet: they need write access to %s (Settings > Collaborators)" % (n, self.repo))
                continue
            names.append(n)
        if bad:
            raise RuntimeError("; ".join(bad) + ". Fix the list, then press the button again.")
        return names

    def open_pr(self, approvers_text=""):
        """Open the setup pull request with the author pr_author picks, after checking the approvers named on the page."""
        self.set("merge", "working", "Preparing the setup pull request")
        self.approvers = self.check_approvers(approvers_text)
        st, info, _ = G.call("GET", "/repos/%s" % self.repo, self.token)
        if st != 200 or not isinstance(info, dict) or not info.get("default_branch"):
            raise RuntimeError("couldn't read %s from GitHub (HTTP %s). Check you're signed in with the right account, then press "
                               "the button again." % (self.repo, st))
        base = info["default_branch"]
        git(self.top, "fetch", "--quiet", "origin", base)
        files = setup_files(self.top, base, self.py)
        blocks = block_merges_in_ci(files, self.test_command) if self.can_enforce else False
        keep_test_command(files, self.test_command)  # after block_merges_in_ci, which only acts on a config nobody tuned
        nojudge = run_without_judge(files) if self.objective else False
        if self.objective:  # full mode keeps working as before; per-test results are what replace the judge's test check
            pin_junit(files, self.junit_path, self.top)
        final = json.loads(files[".story-gate/config.json"].decode("utf-8")) if ".story-gate/config.json" in files else {}
        junit = final.get("junit_path") if final.get("test_command") else ""
        owners = [self.login] + self.approvers
        # The file GitHub uses (first found) on the remote's copy of the default branch, with its rules kept; a local branch
        # named origin/<base> can't stand in. When git can't tell (or that path is a link), setup-repo adds the owners later.
        found = G.codeowners_in_git(self.top, "refs/remotes/origin/%s" % base)
        new = G.add_owners(found[1], owners) if found is not None else None  # the same merge setup_repo does
        if new is not None:
            files.setdefault(found[0] or G.CODEOWNERS_PATHS[0], new.encode())
        body = ("This adds story-gate: the settings, the agent instructions and three workflows (the PR check, the audit and the "
                "dashboard). It was prepared by `story-gate init`. Merge it to finish setup; branch rules are applied right after.\n\n"
                + ("**Merges are blocked until the story-gate check passes**, because GitHub can enforce branch rules on this "
                   "repository. CI runs the tests with `%s`. Your AI's live checks on your computer warn but don't stop it "
                   "(`enforce_points: [\"ci\"]` in `.story-gate/config.json`; set `\"mode\": \"enforce\"` to block there too)."
                   % self.test_command if blocks else
                   "story-gate starts in **warn** mode: the check reports problems but doesn't block merges. "
                   + ("To block them, set `test_command` (how CI runs your tests) and `\"enforce_points\": [\"ci\"]` in "
                      "`.story-gate/config.json`, in a pull request of their own." if self.can_enforce else
                      "GitHub doesn't enforce branch rules on this repository (a private repository on a free plan), so the "
                      "check marks problems as ADVISORY."))
                + ("\n\n**No AI judge (objective mode).** You skipped the judge key, so `\"judge_mode\": \"objective\"` is set. "
                   "story-gate still checks the plan's structure, runs the tests and the story's scenarios in CI, and needs a "
                   "human approval. It does not check whether the code really does what the story asks, whether the tests "
                   "really cover the planned cases, unplanned extra scope, drift from the PRD or TRD, or work left for later. "
                   "To turn the judge on later: add the `OPENROUTER_API_KEY` repository secret, then set `\"judge_mode\": "
                   "\"full\"` in a pull request." if nojudge else "")
                + ("\n\nWithout a judge, story-gate needs each test's own result, not just a green test run. "
                   + ("CI runs `%s` and reads each test's result from `%s`." % (final["test_command"], junit) if junit else
                      "**Before any story can finish**, set `test_command` (how CI runs your tests), make it write a JUnit XML "
                      "report, and set `junit_path` to that file in `.story-gate/config.json`, in a pull request of its own.")
                   if nojudge else ""))
        # the setup pull request adds workflow files, which the agent App can't change: you write those, the App writes the
        # rest and opens it ('split'); without the App, pr_author picks you or says what to do
        who, tok, why = G.pr_author(self.repo, base, self.token, self.login, touches_workflows=True)
        human, app = tok if who == "split" else (tok, None)
        # running init again before that pull request is merged brings the open one up to date instead of leaving it stale
        self.pr = G.open_setup_pr(human, self.repo, files, body=body + "\n\nOpened by: %s." % why, base=base,
                                  update_existing=True, app_token=app)
        self.set("merge", "working", "Waiting for you to merge the setup pull request")
        self.background(lambda: self._wait_merge(base), "merge")

    def _wait_merge(self, base):
        for _ in range(1800):
            if G.pr_merged(self.token, self.repo, self.pr["number"]):
                break
            time.sleep(2)
        else:
            self.set("merge", "error", "The setup pull request isn't merged yet. Merge it, then press the button again.")
            return
        tmp = Path(tempfile.mkdtemp(prefix="sg-rules-"))
        (tmp / ".github").mkdir()
        owners = [self.login] + self.approvers
        (tmp / ".github" / "CODEOWNERS").write_text("* %s\n" % " ".join("@" + o for o in owners), encoding="utf-8")
        git(self.top, "fetch", "--quiet", "origin", base)
        wf = git(self.top, "show", "origin/%s:.github/workflows/story-gate.yml" % base)  # so ci_check_name sees the merged workflow
        if wf:
            (tmp / ".github" / "workflows").mkdir()
            (tmp / ".github" / "workflows" / "story-gate.yml").write_text(wf + "\n", encoding="utf-8")
        notes = G.setup_repo(tmp, self.repo, owners, self.token)  # CODEOWNERS already merged; this sets the rules
        shutil.rmtree(tmp, ignore_errors=True)
        if self.private and any(n.startswith("Ruleset: NOT created (HTTP 403") for n in notes):
            self.private_free = True  # GitHub refused the rules: a private repository on the free plan
        self.set("merge", "ok", "Merged. " + " ".join(n for n in notes if n.startswith(("Ruleset", "Actions"))))
        self.finish(base)

    # ---- last: protect this computer and check everything
    def finish(self, base):
        self.set("done", "working", "Turning on protection on this computer")
        git(self.top, "fetch", "--quiet", "origin", base)
        if git(self.top, "rev-parse", "--abbrev-ref", "HEAD") == base and not git(self.top, "status", "--porcelain"):
            git(self.top, "merge", "--ff-only", "--quiet", "origin/" + base)
        try:
            signed = signed_or_not(HERE)
        except RuntimeError as e:
            self.set("done", "error", "Couldn't turn on protection on this computer: %s" % e)
            return
        r = subprocess.run([sys.executable, str(HERE / "gate.py"), "install", "--user", *signed, "--python", self.py,
                            "--clients", ",".join(self.clients)],
                           cwd=str(self.top), capture_output=True, text=True, env=dict(os.environ, STORY_GATE_ROOT=str(self.top)))
        if r.returncode:
            self.set("done", "error", "Couldn't turn on protection on this computer: %s" % (r.stdout + r.stderr)[-400:])
            return
        d = subprocess.run([sys.executable, str(HERE / "gate.py"), "doctor"], cwd=str(self.top), capture_output=True, text=True)
        fails = [l.strip() for l in d.stdout.splitlines() if "FAIL" in l]
        self.set("done", "ok" if not fails else "error", "story-gate is protecting %s" % self.repo if not fails else "; ".join(fails[:3]))


def signed_or_not(here):
    """A signed release is installed only after its signature checks out; a copy with neither file (a development copy) is
    installed with --unsigned, which install and doctor both report. Only one of the two files means a broken or tampered
    release: refuse it rather than fall back to an unchecked install."""
    have = [(Path(here) / f).is_file() for f in ("release.json", "release.json.sig")]
    if all(have):
        return []
    if not any(have):
        return ["--unsigned"]
    raise RuntimeError("this story-gate copy has only one of release.json and release.json.sig, so it can't be checked. "
                       "Install it again from the release.")


# ------------------------------------------------------------------ the page
def make_handler(wz):
    """The local web server's request handler, bound to this setup wizard."""
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _host_ok(self):  # DNS-rebinding guard: only our own loopback address
            return self.headers.get("Host", "") == "127.0.0.1:%d" % self.server.server_address[1]

        def _send(self, code, body, ctype="text/html; charset=utf-8"):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'; form-action 'self' https://github.com")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            """Serve the setup page, its fonts and the status feed to this computer's own browser only."""
            u = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(u.query)
            if not self._host_ok():
                return self._send(403, "forbidden")
            if u.path.startswith("/font/") and u.path[6:] in FONTS:  # brand fonts, served from the installed package
                data = (HERE / "vendor" / u.path[6:]).read_bytes()
                self.send_response(200); self.send_header("Content-Type", "font/woff2"); self.send_header("Content-Length", str(len(data)))
                self.end_headers(); self.wfile.write(data)
                return
            if u.path == "/agent/callback":  # GitHub redirects here after "Create"; state proves it's our request
                ok = wz.agent_callback(qs.get("state", [""])[0], qs.get("code", [""])[0])
                return self._send(200 if ok else 400, page_shell("<h1>%s</h1><p>You can close this tab.</p>" %
                                                                 ("Created. Now click Install." if ok else "Unexpected request.")))
            if qs.get("t", [""])[0] != wz.secret:
                return self._send(403, "forbidden")
            if u.path == "/":
                return self._send(200, page(wz))
            if u.path == "/state":
                return self._send(200, json.dumps(wz.snapshot()), "application/json")
            if u.path == "/agent/start":
                if has_agent() and wz.agent_installed():
                    wz.set("agent", "ok", "Your AI already has its own GitHub login on %s" % wz.repo)
                    if wz.already:
                        wz.background(lambda: wz.finish(wz.base), "done")
                    return self._send(200, page_shell("<h1>Already done.</h1><p>Go back to the setup page.</p>"))
                wz.set("agent", "working", "Click Create on GitHub")
                return self._send(200, page_shell(wz.agent_form(self.server.server_address[1])))
            return self._send(404, "not found")

        def do_POST(self):
            """Run a setup action posted by the page; refused without the session secret and a loopback origin."""
            u = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(u.query)
            origin = self.headers.get("Origin", "")
            if not self._host_ok() or qs.get("t", [""])[0] != wz.secret or origin not in ("", "http://127.0.0.1:%d" % self.server.server_address[1]):
                return self._send(403, "forbidden")
            n = min(int(self.headers.get("Content-Length") or 0), 4096)
            form = urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8", "replace"))
            act = u.path.strip("/")
            if act == "signin":
                wz.start_signin()
            elif act == "key" and wz.token:
                wz.set("key", "working", "Saving")
                wz.background(lambda: wz.save_key(form.get("key", [""])[0]), "key")
            elif act == "nokey" and wz.token:
                wz.skip_key()
            elif act == "merge" and wz.token:
                wz.background(lambda: wz.open_pr(form.get("approvers", [""])[0]), "merge")
            elif act == "tools":
                if not wz.set_clients(form.get("clients", [""])[0]):
                    return self._send(400, json.dumps({"error": "Tick at least one tool."}), "application/json")
            return self._send(200, json.dumps(wz.snapshot()), "application/json")
    return H


def page_shell(inner):
    return ("<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>story-gate setup</title><style>" + CSS + HELP_CSS + "</style><body><main>" + inner + HELP_JS + "</main></body></html>")


FONTS = ("ClashDisplay-600.woff2", "Switzer-400.woff2", "Switzer-600.woff2")


def wordmark():
    """The Viaknox wordmark artwork (never typed), for the 'by Viaknox' line."""
    try:
        return (HERE / "vendor" / "viaknox-wordmark.svg").read_text(encoding="utf-8")
    except OSError:
        return ""


CSS = """
@font-face{font-family:"Clash Display";font-weight:600;src:url(/font/ClashDisplay-600.woff2) format("woff2")}
@font-face{font-family:Switzer;font-weight:400;src:url(/font/Switzer-400.woff2) format("woff2")}
@font-face{font-family:Switzer;font-weight:600;src:url(/font/Switzer-600.woff2) format("woff2")}
:root{--yellow:#FFD84D;--yellow-text:#8A6A00;--graphite:#1F2327;--paper:#FFF8F2;--muted:#5B6168;--line:#E4E1DB;--ok:#1F7A4D;--error:#B42318}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--graphite);font:16px/1.5 Switzer,"Segoe UI",system-ui,sans-serif}
main{max-width:760px;margin:6vh auto;padding:0 20px}h1{font:600 40px/1.1 "Clash Display",Switzer,system-ui,sans-serif;margin:.2em 0 .3em}
.eyebrow{color:var(--yellow-text);letter-spacing:.2em;font-size:13px;font-weight:600;text-transform:uppercase}
.lead{color:var(--muted);margin:0 0 28px}.step{background:#fff;border:1px solid var(--line);border-radius:14px;padding:18px 20px;margin:12px 0;display:flex;gap:16px}
.n{font:600 28px/1 "Clash Display",system-ui;color:var(--yellow-text);min-width:28px}.step h2{font-size:18px;margin:0 0 4px}.step p{margin:0;color:var(--muted)}
.step.ok{border-color:#BFE3CF}.step.ok .act{display:none}a{color:var(--graphite)}.step.ok .n{color:var(--ok)}.step.error{border-color:var(--error)}.msg{margin-top:8px;font-size:14px}.hint{margin:8px 0 0;font-size:14px;color:var(--muted)}
button,.btn{background:var(--yellow);color:var(--graphite);border:2px solid var(--graphite);border-radius:10px;padding:10px 16px;font-weight:600;font-size:15px;cursor:pointer;text-decoration:none;display:inline-block;margin-top:10px}
button.link{background:none;border:0;padding:0;margin:0;font-size:inherit;text-decoration:underline;color:var(--graphite)}button:disabled{opacity:.4;cursor:default}input{font:inherit;padding:9px 12px;border:1px solid var(--line);border-radius:10px;width:min(420px,100%)}
.lbl{display:block;margin-top:10px;font-weight:600}.lbl span{display:block;font-weight:400;color:var(--muted);font-size:14px}.opt{font-size:12px;font-weight:600;letter-spacing:.04em;color:var(--muted);border:1px solid var(--line);border-radius:999px;padding:1px 8px;margin-left:6px;vertical-align:1px}.lbl input{display:block;margin-top:6px}
.tool{display:block;margin:6px 0}.tool input{width:auto;margin-right:6px}.tool span{color:var(--ok);font-size:13px;margin-left:6px}.tool em{color:var(--muted);font-size:13px;font-style:normal;margin-left:6px}.other{margin:4px 0 0;padding-left:20px;color:var(--muted);font-size:14px}
.code{font:600 28px/1 ui-monospace,monospace;letter-spacing:.15em;background:var(--paper);padding:8px 12px;border-radius:8px;display:inline-block;margin-top:8px}
.by svg{height:14px;width:auto;vertical-align:-2px}.note{background:var(--yellow);color:var(--graphite);border:2px solid var(--graphite);border-radius:14px;padding:14px 18px;margin-top:18px}.foot{margin-top:28px;color:var(--muted);font-size:13px}
"""


HELP_CSS = """
.help{position:relative;display:inline-block!important;margin-left:6px;vertical-align:-3px}
.help .info{width:20px;height:20px;border-radius:50%;border:1.5px solid var(--muted);background:#fff;color:var(--muted);font:600 12px/1 Georgia,serif;
padding:0;margin:0;display:inline-flex;align-items:center;justify-content:center;cursor:help}
.help .info:hover,.help .info:focus-visible,.help .info[aria-expanded=true]{border-color:var(--graphite);color:var(--graphite);outline:2px solid var(--yellow)}
.help .tip{display:none;position:absolute;left:-10px;top:28px;z-index:10;width:min(340px,80vw);background:var(--graphite);color:#fff;
font:400 14px/1.45 Switzer,system-ui,sans-serif;padding:10px 12px;border-radius:10px;box-shadow:0 6px 20px rgba(0,0,0,.18);text-align:left;
pointer-events:none;white-space:normal;letter-spacing:normal;text-transform:none}
.help .tip code{color:var(--yellow);background:none;font-size:13px}.help .tip b{color:#fff;font-weight:600}
.help:hover .tip,.help .info:focus-visible+.tip,.help .tip.open{display:block}
.help .tip.open{pointer-events:auto}.help.hush .tip{display:none!important}
.bad{border-color:var(--error)!important;outline:2px solid var(--error)}
.ferr{color:var(--error);font-size:14px;margin:4px 0 0;font-weight:600}
"""
HELP_JS = """<script>
document.addEventListener('click',function(e){var b=e.target.closest&&e.target.closest('.info');
document.querySelectorAll('.tip.open').forEach(function(t){if(!b||t.id!==b.getAttribute('aria-controls')){t.classList.remove('open');
var o=document.querySelector('[aria-controls="'+t.id+'"]');if(o)o.setAttribute('aria-expanded','false')}});
if(b){e.preventDefault();var t=document.getElementById(b.getAttribute('aria-controls'));var open=!t.classList.contains('open');
t.classList.toggle('open',open);b.setAttribute('aria-expanded',String(open))}});
document.addEventListener('keydown',function(e){if(e.key!=='Escape')return;document.querySelectorAll('.tip.open').forEach(function(t){t.classList.remove('open')});
document.querySelectorAll('.info[aria-expanded=true]').forEach(function(b){b.setAttribute('aria-expanded','false')});
document.querySelectorAll('.help:hover,.help:focus-within').forEach(function(h){h.classList.add('hush')});
var a=document.activeElement;if(a&&a.classList&&a.classList.contains('info'))a.blur()});
document.addEventListener('mouseout',function(e){var h=e.target.closest&&e.target.closest('.help');if(h&&!h.contains(e.relatedTarget))h.classList.remove('hush')});
document.addEventListener('focusin',function(e){var h=e.target.closest&&e.target.closest('.help');if(h)h.classList.remove('hush')});
</script>"""


def info(tid, field, text):
    """An (i) button with a short explanation: shown on hover, on keyboard focus, or with a tap (phones), and read out by
    screen readers. `text` is trusted HTML written here, never anything a person typed."""
    return ("<span class=help><button type=button class=info aria-label='What is %s?' aria-expanded=false aria-controls=tip-%s>i</button>"
            "<span class=tip role=tooltip id=tip-%s>%s</span></span>" % (html.escape(field, quote=True), tid, tid, text))


def tools_html(wz):
    """Tickboxes for the tools story-gate can check live (found ones pre-ticked), then the ones covered another way."""
    rows = "".join("<label class=tool><input type=checkbox name=c value=%s%s> %s%s</label>"
                   % (cl, " checked" if cl in wz.clients else "", html.escape(name), (" <span>found on this computer</span>" if cl in wz.found else "")
                      + (" <em>(story-gate approves its own check inside Hermes for you)</em>" if cl == "hermes" else ""))
                   for cl, name in HOOKED_TOOLS)
    other = "".join("<li><b>%s</b>: %s</li>" % (html.escape(n), html.escape(d)) for n, d in OTHER_TOOLS)
    return ("<form id=tools onchange=\"tools(this)\">%s</form><p class=msg id=toolmsg></p><p class=hint>Also covered, without live checks: </p><ul class=other>%s</ul>"
            % (rows, other))


def page(wz):
    """The setup page's HTML: one card per step, each posting back to this wizard."""
    t = wz.secret
    steps = [
        ("signin", "Sign in to GitHub", "So story-gate can set up this repository for you.",
         "<button onclick=\"go('signin')\">Start</button><div id=dev></div>"),
        ("agent", "Give your AI its own GitHub login", "Click <b>Create</b>, then <b>Install</b> on this repository. Your AI never uses your login.",
         "<a class=btn id=agentbtn href='/agent/start?t=%s' target=_blank>Open GitHub</a>" % t),
        ("key", "Add the judge key", "Make a key at <a href='https://openrouter.ai/keys' target=_blank>openrouter.ai/keys</a> and paste it here. "
         "Setup does not give it to your AI. It goes into a GitHub secret, and the copy on this computer is readable by anything running as you.",
         "<form onsubmit=\"event.preventDefault();go('key',new URLSearchParams(new FormData(this)))\"><label class=lbl>Judge key"
         + info("judgekey", "the judge key", "An <b>OpenRouter</b> API key. It starts with <code>sk-or-</code>. story-gate's AI judge "
                "uses it to check that the code really does what the story asks. Make one at openrouter.ai/keys and copy it right "
                "away. It's encrypted into a GitHub secret and saved on this computer. Setup does not give it to your AI, but anything running as you can read the saved copy.")
         + "<input name=key type=password autocomplete=new-password placeholder='sk-or-...' aria-describedby=msg-key></label> <button>Save</button></form>"
         "<p class=hint>No key? <button class=link onclick=\"go('nokey')\">Skip for now</button> and story-gate runs without an AI "
         "judge: it still checks the plan, runs your tests and needs your approval, but nothing checks that the code really does "
         "what the story asks. To turn the judge on later: add a key, then set <code>\"judge_mode\": \"full\"</code> in a pull request.</p>"),
        ("merge", "Approve the setup", "We open a pull request with everything story-gate needs. You merge it on GitHub.",
         "<form onsubmit=\"event.preventDefault();go('merge',new URLSearchParams(new FormData(this)))\">"
         "<label class=lbl>Who else can approve work? <b class=opt>Optional</b>"
         + info("approvers", "who can approve work", "GitHub usernames of other people who may accept your AI's work, separated "
                "by commas, e.g. <code>alex, sam</code>. Each needs <b>write</b> access to this repository (Settings &gt; "
                "Collaborators). Any one of you can approve. Leave it empty if it's only you.")
         + "<span>GitHub usernames with write access to this repository. "
         "Any one of you can approve. Leave it empty if it's just you.</span>"
         "<input name=approvers autocomplete=off placeholder='Optional, e.g. alex, sam' aria-describedby=msg-merge></label>"
         "<button>Open the pull request</button></form><div id=pr></div>"),
        ("done", "Protect your AI tools" + info("tools", "the AI tools list", "The AI tools on this computer that story-gate "
                  "should watch while they work. Ticked ones get live checks as they edit: missing story, missing tests, edits "
                  "outside the plan. Tools found on this computer are ticked for you."),
         "Live checks turn on for the tools ticked below, then story-gate checks itself. "
         "Change the ticks before you merge.", tools_html(wz)),
    ]
    cards = "".join("<section class=step id=s-%s><div class=n>%s</div><div><h2>%s</h2><p>%s</p><div class=act>%s</div><div class=msg id=msg-%s role=status></div></div></section>"
                    % (k, i + 2, h, p, a, k) for i, (k, h, p, a) in enumerate(steps))
    js = """<script>
const T=%s;async function go(a,body){await fetch('/'+a+'?t='+T,{method:'POST',body:body||''});tick()}
async function tools(f){const c=[...f.querySelectorAll('input:checked')].map(i=>i.value).join(',');const r=await fetch('/tools?t='+T,{method:'POST',body:new URLSearchParams({clients:c})});
document.getElementById('toolmsg').textContent=r.ok?'':'Tick at least one tool.'}
async function tick(){const s=await (await fetch('/state?t='+T)).json();
for(const [k,v] of Object.entries(s.steps)){const e=document.getElementById('s-'+k);if(!e)continue;e.className='step '+v.status;e.querySelector('.msg').textContent=v.status=='ok'?'✓ '+v.msg:v.msg;
const f={key:'input[name=key]',merge:'input[name=approvers]'}[k];const fi=f&&e.querySelector(f);if(fi){const bad=v.status=='error'&&(k=='key'||/Fix the list/.test(v.msg));
fi.classList.toggle('bad',bad);if(bad)fi.setAttribute('aria-invalid','true');else fi.removeAttribute('aria-invalid');
let fe=document.getElementById('ferr-'+k);if(bad&&!fe){fe=document.createElement('div');fe.id='ferr-'+k;fe.className='ferr';fe.setAttribute('role','alert');fi.closest('label').after(fe)}
if(fe){fe.textContent=bad?v.msg:'';fi.setAttribute('aria-describedby',bad?'ferr-'+k:'msg-'+k)}if(bad)e.querySelector('.msg').textContent='';
e.querySelector('.msg').classList.toggle('ferr',v.status=='error'&&!bad)}}
const d=document.getElementById('dev');if(s.device&&s.steps.signin.status!='ok'){if(d.dataset.code!==s.device.code){d.dataset.code=s.device.code;d.innerHTML='Enter this code at <a target=_blank rel=noopener></a><br><span class=code></span>';const a=d.querySelector('a');a.href=a.textContent=s.device.uri;d.querySelector('.code').textContent=s.device.code}}else{d.innerHTML='';d.dataset.code=''}
const p=document.getElementById('pr');if(s.pr&&p.dataset.url!==s.pr.url){p.dataset.url=s.pr.url;p.innerHTML='<a class=btn target=_blank rel=noopener>Review and merge on GitHub</a><p class=hint>Opens GitHub in a new tab. Click the green <b>Merge pull request</b> button there, then come back to this page.</p>';p.querySelector('a').href=s.pr.url;p.previousElementSibling.style.display='none'}
const n=document.getElementById('next');if(s.steps.done.status=='ok'&&!n.dataset.on){n.dataset.on=1;n.style.display='block';if(s.dashboard){n.querySelector('a').href=s.dashboard}else{n.querySelector('.dash').style.display='none';n.querySelector('.noissues').style.display='block'}}
document.getElementById('free').style.display=s.private_free?'block':'none'}
tick();setInterval(tick,2000)</script>""" % json.dumps(t)
    return page_shell(
        "<div class=eyebrow>story-gate setup · %s</div><h1>Four clicks and you're protected.</h1>"
        "<p class=lead>Step 1 is done: you asked your AI to set up story-gate. Do the steps below in order; this page updates by itself.</p>%s"
        "<div class=note id=next style=display:none><h2>You're set up. What's next</h2>"
        "<div class=dash><p><a class=btn target=_blank rel=noopener>Open your dashboard</a></p>"
        "<p>Your dashboard is a pinned issue in your repository on GitHub, called <b>Story-gate dashboard</b>. It shows every "
        "story: ready, in progress and done. It appears a minute or two after setup and updates every 30 minutes. "
        "To find it later: your repository on GitHub, then <b>Issues</b>.</p></div>"
        "<p class=noissues style=display:none>Issues are turned off in this repository, so the dashboard can't be a pinned issue. "
        "Find it on GitHub under <b>Actions</b>, in each <b>story-gate-dashboard</b> run's summary and report. Or turn Issues on "
        "in the repository's Settings.</p>"
        "<p>Now ask your AI for one small feature. It writes the story, builds it, shows it working, and opens a pull request for you to approve.</p>"
        "<p>Keep your stories in Linear or Jira Cloud? Run <code>story-gate tracker-setup</code> yourself in this folder to connect it.</p></div>"
        "<div class=note id=free style=display:none><b>Free GitHub plan, private repository:</b> GitHub won't enforce the "
        "'must be approved' rule here. story-gate still checks every pull request and marks it ADVISORY - NOT ENFORCED.</div>"
        "<div class=foot>Runs only on this computer (127.0.0.1). story-gate · MIT · <span class=by>by %s</span></div>%s"
        % (html.escape(wz.repo), cards, wordmark(), js))


def run(top, py, open_browser=True, port=0, serve_seconds=3600, test_command="", junit_path=""):
    repo = github_repo(top)
    if not repo:
        print("story-gate init: this folder's 'origin' isn't a GitHub repository. Open your project folder (the one you push "
              "to GitHub) and run it there.")
        return 1
    wz = Wizard(top, repo, py, open_browser=open_browser)
    wz.test_command = (test_command or "").strip()
    wz.junit_path = (junit_path or "").strip()
    if wz.junit_path and not pin_junit({".story-gate/config.json": b"{}"}, wz.junit_path, top):
        print("story-gate init: --junit-path must be a file inside the repository, written relative to it and not through a link to outside it (e.g. "
              "reports/junit.xml). Got: %s" % wz.junit_path)
        return 1
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), make_handler(wz))
    url = "http://127.0.0.1:%d/?t=%s" % (srv.server_address[1], wz.secret)
    print("story-gate setup for %s\nOpen this page to continue (it should open by itself):\n  %s\n"
          "Steps there need you, not the AI: sign in, create the AI's login, add the judge key, merge one pull request." % (repo, url))
    sys.stdout.flush()
    if open_browser:
        webbrowser.open(url)
    t0 = time.time()
    srv.timeout = 1
    while time.time() - t0 < serve_seconds and wz.st["done"]["status"] not in ("ok",):
        srv.handle_request()
    for _ in range(5):  # let the page fetch the final state
        srv.handle_request()
    srv.server_close()
    if wz.st["done"]["status"] == "ok":
        url = wz.dashboard_url()
        print("story-gate: %s\n%s" % (wz.st["done"]["msg"],
              "Your dashboard (a pinned issue on GitHub; it appears a minute or two after setup): %s" % url if url else
              "Your dashboard: Issues are turned off in this repository, so it is in each story-gate-dashboard run's summary "
              "on GitHub (Actions)."))
    else:
        print("story-gate setup stopped before the end. Run `story-gate init` again to continue.")
    return 0 if wz.st["done"]["status"] == "ok" else 1


# ---- story-gate upgrade-repo: bring a set-up repository to this story-gate version, in a pull request you approve
UPGRADE_BRANCH = "story-gate-upgrade"


def _lf(top, base, path, data):
    """`data` with Windows line endings (CRLF) turned into LF, so a setup run on Windows sends the same text a Mac or Linux
    run does. Binary files, and files the branch already stores with CRLF, are left as they are."""
    if b"\r\n" not in data or b"\0" in data or b"\r\n" in (_at(top, "origin/" + base, path) or b""):
        return data
    return data.replace(b"\r\n", b"\n")


def _at(top, ref, path):
    """The bytes of `path` at `ref` in the repository at `top`, or None when it isn't there."""
    r = subprocess.run(["git", "show", "%s:%s" % (ref, path)], cwd=str(top), capture_output=True)
    return r.stdout if r.returncode == 0 else None


def upgrade_files(top, base, py):
    """{path: bytes or None} that bring origin/<base> to this story-gate version: changed and new files, and None for
    story-gate runtime files the new version no longer has. Raises when the repository isn't set up yet."""
    ref = "origin/" + base
    if _at(top, ref, ".story-gate/config.json") is None:
        raise RuntimeError("story-gate isn't set up in this repository yet (no .story-gate/config.json on %s). "
                           "Run `story-gate init` instead." % base)
    out = {p: c for p, c in setup_files(top, base, py).items() if _at(top, ref, p) != c}
    import sg_trust as T
    new = set(T.runtime_files(HERE))
    # What the repository's copy holds now, read from git itself (never from its release.json, which may be old,
    # missing or edited): only story-gate's own runtime files, so settings, budgets and stories are never touched.
    listed = subprocess.run(["git", "ls-tree", "-r", "--name-only", ref, ".story-gate/"], cwd=str(top), capture_output=True,
                            text=True).stdout.splitlines()
    for p in listed:
        f = p[len(".story-gate/"):]
        if is_runtime_name(f) and f not in new:
            out[p] = None  # a runtime file the new version no longer has
    return out


def is_runtime_name(f):
    """True for a path (inside .story-gate/) that is one of story-gate's runtime files by name: a top-level *.py,
    PROTOCOL.md, SKILL.md, or a file directly in vendor/."""
    parts = f.split("/")
    if any(x in ("", ".", "..") for x in parts) or ":" in f or "\\" in f:
        return False
    if len(parts) == 1:
        return f.endswith(".py") or f in ("PROTOCOL.md", "SKILL.md")
    return len(parts) == 2 and parts[0] == "vendor"


def version_at(top, ref):
    """The story-gate version recorded in .story-gate/release.json at `ref`, or '' when there is none."""
    try:
        return json.loads((_at(top, ref, ".story-gate/release.json") or b"{}").decode("utf-8")).get("version") or ""
    except ValueError:
        return ""


def this_version():
    """The version of the story-gate running this code (its gate.py VERSION)."""
    import re
    m = re.search(r'^VERSION = "([^"]+)"', (HERE / "gate.py").read_text(encoding="utf-8"), re.M)
    return m.group(1) if m else "unknown"


def upgrade_repo(top, py, dry=False, token=None, out=print):
    """Upgrade the repository at `top` to this story-gate version in one pull request ('story-gate-upgrade'). You commit
    the workflow files, the agent App commits the rest and opens it (pr_author), so your approval counts.
    -> (status, detail): status 'up-to-date', 'dry-run', 'opened', 'updated', 'unchanged' or 'error'."""
    version = this_version()
    import sg_trust as T
    try:
        T.verify_release(HERE)  # copy only a signed release into repositories
    except T.TrustError as ex:
        return "error", ("upgrade-repo copies the story-gate on this computer into your repositories, so it must be a "
                         "signed release: %s. Install one (`story-gate upgrade`), then try again." % ex)
    repo = github_repo(top)
    if not repo:
        return "error", "this folder isn't a clone of a GitHub repository"
    tok = token or G.human_token()
    if not tok:
        return "error", "sign in to GitHub first: `gh auth login` (or set STORY_GATE_HUMAN_TOKEN)"
    st, info, _ = G.call("GET", "/repos/%s" % repo, tok)
    if st != 200 or not isinstance(info, dict) or not info.get("default_branch"):
        return "error", "couldn't read %s from GitHub (HTTP %s)" % (repo, st)
    base = info["default_branch"]
    if subprocess.run(["git", "fetch", "--quiet", "origin", base], cwd=str(top), capture_output=True, env=_clone_env(tok)).returncode:
        return "error", "couldn't fetch %s from GitHub" % base
    before = version_at(top, "origin/" + base) or "unknown"
    if _vtuple(before) and _vtuple(version) and _vtuple(before) > _vtuple(version):
        return "ahead", ("%s is on story-gate %s, newer than the %s on this computer: nothing to do. Upgrade this computer "
                         "(`story-gate upgrade`) first." % (repo, before, version))
    try:
        files = upgrade_files(top, base, py)
    except RuntimeError as ex:
        return "error", str(ex)
    if not files:
        return "up-to-date", "%s is on story-gate %s" % (repo, version)
    wf = sorted(p for p in files if G.is_workflow_path(p))
    login = G.whoami(tok)
    try:
        who, t, why = G.pr_author(repo, base, tok, login, touches_workflows=bool(wf))
    except G.NoAuthor as ex:
        return "error", str(ex)
    human, app = t if who == "split" else (t, None)
    plan = "%s: %s -> %s, %d file(s) (%d workflow file(s)); %s" % (repo, before, version, len(files), len(wf), why)
    if dry:
        out("Would open: " + plan)
        for p in sorted(files):
            out("  %s %s" % ("delete" if files[p] is None else "write ", p))
        return "dry-run", plan
    body = ("Upgrades story-gate from %s to %s: the copied runtime in `.story-gate/`, the workflows (your own steps between "
            "the 'your steps' markers are kept) and the agent instructions. Prepared by `story-gate upgrade-repo` with the "
            "signed story-gate %s installed on the owner's computer. Review it like any change to your CI, then merge.\n\n"
            "Opened by: %s." % (before, version, version, why))
    pr = G.open_setup_pr(human, repo, files, branch=UPGRADE_BRANCH, title="Upgrade story-gate to v%s" % version,
                         body=body, base=base, update_existing=True, app_token=app)
    status = ("updated" if pr.get("updated") else "unchanged") if pr.get("existing") else "opened"
    return status, "%s #%s %s" % (repo, pr["number"], pr["url"])


def _clone_env(token):
    """Environment for `git clone` that sends `token` to github.com without it appearing in the command line or the
    clone's saved config, and never stops to ask for a password."""
    import base64
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    if token:
        basic = base64.b64encode(("x-access-token:%s" % token).encode()).decode()
        n = int(env.get("GIT_CONFIG_COUNT", "0") or 0)
        env.update({"GIT_CONFIG_COUNT": str(n + 1), "GIT_CONFIG_KEY_%d" % n: "http.https://github.com/.extraheader",
                    "GIT_CONFIG_VALUE_%d" % n: "AUTHORIZATION: basic " + basic})
    return env


def upgrade_many(repos, py, dry=False, token=None, out=print):
    """upgrade_repo for each owner/name, each in a fresh temporary clone. -> [(repo, status, detail)]; one repo's failure
    never stops the others."""
    results = []
    for repo in repos:
        tmp = Path(tempfile.mkdtemp(prefix="sg-upgrade-"))
        try:
            r = subprocess.run(["git", "clone", "-q", "--filter=blob:none", "https://github.com/%s.git" % repo, str(tmp / "r")],
                               capture_output=True, text=True, timeout=600, env=_clone_env(token or G.human_token()))
            if r.returncode:
                results.append((repo, "error", "couldn't clone it: %s" % (r.stderr.strip().splitlines() or ["?"])[-1]))
                continue
            status, detail = upgrade_repo(tmp / "r", py, dry=dry, token=token, out=out)
            results.append((repo, status, detail))
        except Exception as ex:  # report and go on to the next repository
            results.append((repo, "error", str(ex)))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return results


def _vtuple(v):
    """'0.10.1' -> (0, 10, 1) with trailing zeros dropped, so 0.10 and 0.10.0 compare equal; None when it isn't a version."""
    parts = (v or "").split(".")
    if not v or not all(p.isdigit() for p in parts):
        return None
    t = [int(p) for p in parts]
    while len(t) > 1 and t[-1] == 0:
        t.pop()
    return tuple(t)


def version_report(repos, token, latest, out=print):
    """Which story-gate version each repository's default branch has, and its open upgrade pull request. -> rows."""
    import base64
    rows = []
    for repo in repos:
        st, f, _ = G.call("GET", "/repos/%s/contents/.story-gate/release.json" % repo, token)
        ver = ""
        if st == 200 and isinstance(f, dict) and f.get("content"):
            try:
                ver = json.loads(base64.b64decode(f["content"]).decode("utf-8")).get("version") or "?"
            except (ValueError, TypeError):
                ver = "?"
        if not ver:  # set up before release.json was copied: still a story-gate repository, version unknown
            st3, _, _ = G.call("GET", "/repos/%s/contents/.story-gate/config.json" % repo, token)
            ver = "?" if st3 == 200 else ""
        owner = repo.split("/", 1)[0]
        st2, prs, _ = G.call("GET", "/repos/%s/pulls?state=open&head=%s" % (repo, urllib.parse.quote("%s:%s" % (owner, UPGRADE_BRANCH))), token)
        pr = "#%s" % prs[0]["number"] if st2 == 200 and isinstance(prs, list) and prs else ""
        a, b = _vtuple(ver), _vtuple(latest)
        state = ("not set up" if not ver else "unknown" if a is None or b is None
                 else "current" if a == b else "behind" if a < b else "ahead")
        rows.append((repo, ver or "-", state, pr))
    w = max([len(r[0]) for r in rows] + [10])
    out("%-*s  %-10s %-11s %s" % (w, "repository", "version", "status", "upgrade PR"))
    for r in rows:
        out("%-*s  %-10s %-11s %s" % (w, *r))
    return rows
