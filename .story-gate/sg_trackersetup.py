"""story-gate tracker-setup: connect Linear or Jira Cloud once, in the browser (experimental, like tracker support).

The person runs `story-gate tracker-setup` in their project folder (the AI's hooks refuse it: it's an admin command). A
page opens on this computer (127.0.0.1 only) and walks them through:

  1. sign in to GitHub      (always a fresh device sign-in: a code typed on github.com, never a login already on this computer)
  2. the tracker's details  (workspace or site, a read-only key, and one ticket to test with)
  3. a review screen        (the exact address the key goes to, the secret names, what gets replaced); nothing is sent before
  4. confirm                (one test read of that ticket, the key encrypted into GitHub secrets, a settings pull request)

The key is never written to the repository, a log, the terminal or the pull request. It lives only in this process's
memory until it's sent, and is dropped when the run ends. Jira Server / Data Center keeps the manual setup (jira-setup).
"""
import base64, html, http.server, json, os, re, secrets, sys, threading, time, urllib.parse, webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import sg_github as G  # noqa: E402
import sg_setup as S  # noqa: E402
import sg_trackers as TR  # noqa: E402

KINDS = ("linear", "jira")                 # Jira Cloud only here; Jira Server / Data Center: `story-gate jira-setup`
ALLOWED_SECRETS = frozenset(("STORY_GATE_LINEAR_KEY", "STORY_GATE_JIRA_EMAIL", "STORY_GATE_JIRA_TOKEN"))
IDLE = 600                                 # the page stops 10 minutes after it was closed (it asks every 2 s while open)...
LIFETIME = 1800                            # ...and after 30 minutes in any case
EMAIL = re.compile(r"[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,63}")
LOCAL_FILE = "trackers.env"                # the opt-in copy for spec-pull on this computer, in the story-gate user folder
DESTINATION = {"linear": "https://api.linear.app/graphql", "jira": TR.JIRA_GATEWAY.replace("%s", "{cloud_id}")}


# Settings that would send the sign-in, the secret write or the test read somewhere else, or let something else read them.
# A same-user program can put these in a shell's startup file; tracker-setup refuses to run with them.
REDIRECTING_ENV = ("GITHUB_API_URL", "GITHUB_SERVER_URL", "STORY_GATE_OAUTH_CLIENT_ID", "STORY_GATE_TEST_HTTP",
                   "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "PYTHONHTTPSVERIFY")


def unsafe_environment(env=os.environ):
    """Plain words for each setting that would change where the key or the GitHub sign-in goes; [] when none."""
    found = [n for n in REDIRECTING_ENV if (env.get(n) or "").strip()]
    if G.API != "https://api.github.com" or G.WEB != "https://github.com" or S.OAUTH_CLIENT_ID != S.BUILTIN_OAUTH_CLIENT_ID:
        found.append("a GitHub address or sign-in app other than github.com's")
    return sorted(set(found))


def proxy_in_use(env=os.environ):
    """A proxy only passes encrypted traffic along (TLS is still checked against the system's certificates); say so."""
    return next(((env.get(n) or "").strip() for n in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy") if (env.get(n) or "").strip()), "")


class SetupError(Exception):
    """A plain message for the page. Never carries a key."""


class FieldError(SetupError):
    """A problem with one field the person typed: shown next to that field, and the form stays as it is."""

    def __init__(self, field, msg, kind=None):
        SetupError.__init__(self, msg)
        self.field, self.kind = field, kind  # kind: the tracker form the field belongs to, when both have one


class Session:
    """One run of the page. Holds the typed key in memory only, and only until it's sent or the run ends."""

    def __init__(self, gate, top, repo, open_browser=True, clock=time.time):
        self.gate, self.top, self.repo, self.open_browser, self.clock = gate, Path(top), repo, open_browser, clock
        self.url_token = secrets.token_urlsafe(24)   # works once: the first visit swaps it for a cookie
        self.cookie = None
        self.nonce = None                            # changes on every page load; every POST must carry it
        self.started = self.last = clock()
        self.token = None                            # the person's GitHub login, from this run's device sign-in only
        self.login = None
        self.device = None
        self.private = True
        self.base = "main"
        self.step = "signin"                         # signin -> details -> review -> saving -> done | error
        self.msg = ""
        self.plan = None                             # what the person reviews; confirm must name this exact plan
        self._key = None                             # {secret name: value}, only between "review" and the send
        self.result = None
        self.field_error = None                      # {"field", "msg"}: shown next to the field, until it's changed
        self._flow = False                           # a device sign-in is waiting for its code
        self.lock = threading.Lock()

    # ---- lifetime
    def busy(self):
        return self.step in ("checking", "saving") or self._flow

    def expired(self):
        if self.busy():  # never stop half-way through sending
            return False
        now = self.clock()
        return now - self.last > IDLE or now - self.started > LIFETIME

    def touch(self):
        self.last = self.clock()

    def forget_key(self):
        with self.lock:
            self._key = None

    def scrub(self, text):
        """Defence in depth: no message leaves this process with a key in it, even one from a third party's error."""
        text = str(text)
        for v in (self._key or {}).values():
            if v and len(v) >= 4:
                text = text.replace(v, "***")
        return text

    def set(self, step, msg=""):
        with self.lock:
            self.step, self.msg, self.field_error = step, self.scrub(msg), None

    def snapshot(self):
        with self.lock:
            return {"step": self.step, "msg": self.msg, "login": self.login, "repo": self.repo, "device": self.device,
                    "plan": self.plan, "result": self.result, "field_error": self.field_error}

    def background(self, fn):
        def run():
            try:
                fn()
            except FieldError as e:  # a typing mistake: back to the form, the message next to the field
                msg = self.scrub(e)
                self.forget_key()
                self.set("details")
                with self.lock:
                    self.field_error = {"field": e.field, "msg": msg, "kind": e.kind}
            except Exception as e:  # every failure is a plain message on the page; the key is dropped
                msg = self.scrub(e) if isinstance(e, (SetupError, TR.TrackerError, RuntimeError)) else type(e).__name__
                self.forget_key()
                self.set("error", "%s Start again when you're ready." % msg)
        threading.Thread(target=run, daemon=True).start()

    # ---- 1. sign in: always a fresh device sign-in, so a person types a code on github.com
    def start_signin(self, flow=None):
        """Never uses a GitHub login already on this computer (gh, GH_TOKEN): an AI on this computer could use those."""
        with self.lock:
            if self.token or self._flow:
                return
            self._flow = True
        if not S.OAUTH_CLIENT_ID:
            raise SetupError("This copy of story-gate has no GitHub sign-in app configured.")
        self.set("signin", "Waiting for you to enter the code on GitHub")
        flow = flow or G.device_flow

        def go():
            try:
                self._signed_in(flow(S.OAUTH_CLIENT_ID, on_code=self._show_code))
            finally:
                with self.lock:
                    self._flow = False
        self.background(go)

    def _show_code(self, code, uri):
        with self.lock:
            self.device = {"code": code, "uri": uri}
        if self.open_browser:
            webbrowser.open(uri)

    def _signed_in(self, tok):
        st, me, _ = G.call("GET", "/user", tok)
        if st != 200 or not isinstance(me, dict) or me.get("type") != "User":
            raise SetupError("GitHub didn't confirm a person's account for this sign-in.")
        st, info, _ = G.call("GET", "/repos/%s" % self.repo, tok)
        if st != 200 or not isinstance(info, dict):
            raise SetupError("%s can't see %s (HTTP %s). Sign in with the account that owns it." % (me.get("login"), self.repo, st))
        if not (info.get("permissions") or {}).get("admin"):
            raise SetupError("%s isn't an admin of %s, so it can't save secrets there. Ask the owner to run this."
                             % (me.get("login"), self.repo))
        self.token, self.login = tok, me.get("login")
        self.private = info.get("private") is not False
        self.base = info.get("default_branch") or "main"
        with self.lock:
            self.device = None
        self.set("details", "Signed in as %s" % self.login)

    # ---- 2. details -> a plan to review. Nothing is sent with the key here.
    def details_start(self):
        with self.lock:
            if self.step not in ("details", "review", "error") or not self.token:
                raise SetupError("Sign in to GitHub first." if not self.token else "Wait for the current step to finish.")
            self.step, self.msg, self.plan, self._key, self.field_error = "checking", "Checking the details", None, None, None

    def details(self, form):
        kind = (form.get("kind") or "").strip()
        if kind not in KINDS:
            raise FieldError("kind", "Choose Linear or Jira Cloud.")
        ticket = (form.get("ticket") or "").strip().upper()
        if not TR.TICKET.fullmatch(ticket):
            raise FieldError("ticket", "Give one ticket you can see, like ENG-12, so story-gate can test the key.")
        public_ok = form.get("public_ok") == "yes"
        if not self.private and not public_ok:
            raise FieldError("public_ok", "%s is public. Tick the box that allows ticket text to be copied into it, or use a private "
                             "repository." % self.repo)
        if kind == "linear":
            ws = (form.get("workspace") or "").strip().lower()
            if not TR.WORKSPACE.fullmatch(ws):
                raise FieldError("workspace", "The workspace is the short name in your Linear links: linear.app/<workspace>/...")
            key = (form.get("key") or "").strip()
            if not re.fullmatch(r"lin_api_[A-Za-z0-9]{20,80}", key):
                raise FieldError("key", key_problem(key, "a Linear personal API key", "It starts with lin_api_."), "linear")
            settings, keys, dest = {"workspace": ws}, {"STORY_GATE_LINEAR_KEY": key}, DESTINATION["linear"]
        else:
            site = (form.get("site") or "").strip().lower()
            site = site[len("https://"):] if site.startswith("https://") else site
            site = site.rstrip("/")
            if not TR.JIRA_SITE.fullmatch(site):
                raise FieldError("site", "The site must look like your-company.atlassian.net (Jira Cloud). For Jira Server or Data "
                                 "Center, use `story-gate jira-setup` and the guide.")
            email = (form.get("email") or "").strip()
            if not EMAIL.fullmatch(email):
                raise FieldError("email", "Give the email address of the Atlassian account that made the token.")
            key = (form.get("key") or "").strip()
            if len(key) < 100 or any(c.isspace() for c in key):
                raise FieldError("key", key_problem(key, "an Atlassian API token", "Tokens with scopes are long (about 190 "
                                                    "characters) and start with ATATT."), "jira")
            st, tenant = TR.get_json("https://%s/_edge/tenant_info" % site, {})   # public: no credentials sent
            cloud = (tenant or {}).get("cloudId") if isinstance(tenant, dict) else None
            if st != 200 or not isinstance(cloud, str) or not TR.CLOUD_ID.fullmatch(cloud):
                raise FieldError("site", "Couldn't find the Jira Cloud site %s (HTTP %s). Is the name right?" % (site, st))
            settings = {"site": site, "cloud_id": cloud}
            keys = {"STORY_GATE_JIRA_EMAIL": email, "STORY_GATE_JIRA_TOKEN": key}
            dest = DESTINATION["jira"].format(cloud_id=cloud)
        ac_name = (form.get("ac_field") or "").strip()[:80] if kind == "jira" else ""
        if not self.private:
            settings["allow_in_public_repo"] = True
        if not set(keys) <= ALLOWED_SECRETS or set(keys) != set(TR.secrets_for(kind, settings)):
            raise SetupError("Refusing to write an unexpected secret.")
        existing = [n for n in sorted(keys) if self._secret_exists(n)]
        plan = {"id": secrets.token_urlsafe(12), "kind": kind, "label": "Linear" if kind == "linear" else "Jira Cloud",
                "site": TR.site_of(kind, settings), "settings": settings, "ac_field_name": ac_name, "ticket": ticket,
                "destination": dest, "secrets": sorted(keys), "replaces": existing,
                "local_copy": form.get("local_copy") == "yes", "github": self.login, "repo": self.repo,
                "public": not self.private, "branch": self.base, "warnings": self._workflow_warnings(),
                "github_api": G.API, "proxy": proxy_in_use(),
                "notes": [] if self._review_enforced() else [
                    "GitHub isn't enforcing story-gate's approval rule on %s (no active '%s' ruleset), so the settings pull "
                    "request could be merged without a code owner's review. Run `story-gate setup-repo` to turn it on."
                    % (self.repo, G.RULESET_NAME)]}
        with self.lock:
            self._key, self.plan = keys, plan
        self.set("review", "Check these details, then confirm. Nothing has been sent with your key yet.")

    def _workflow_warnings(self):
        """story-gate's PR check must run on `pull_request`: with `pull_request_target` a pull request's own code could run
        next to the secrets."""
        st, f, _ = G.call("GET", "/repos/%s/contents/.github/workflows/story-gate.yml?ref=%s"
                          % (self.repo, urllib.parse.quote(self.base)), self.token)
        if st == 200 and isinstance(f, dict) and "pull_request_target" in base64.b64decode(f.get("content") or "").decode("utf-8", "replace"):
            return ["story-gate.yml uses pull_request_target. Pull requests' own code could run next to these secrets: "
                    "change it back to pull_request before connecting a tracker."]
        return []

    def _review_enforced(self):
        st, rules, _ = G.call("GET", "/repos/%s/rulesets" % self.repo, self.token)
        return st == 200 and isinstance(rules, list) and any(
            isinstance(r, dict) and r.get("name") == G.RULESET_NAME and r.get("enforcement") == "active" for r in rules)

    def _secret_exists(self, name):
        st, _, _ = G.call("GET", "/repos/%s/actions/secrets/%s" % (self.repo, name), self.token)
        if st not in (200, 404):
            raise SetupError("Couldn't check the repository's secrets (HTTP %s)." % st)
        return st == 200

    # ---- 3. confirm: test read, secrets, local copy, settings pull request
    def confirm_start(self, form):
        """Checked and moved to 'saving' in one step, so a second click can't start a second save."""
        with self.lock:
            plan = self.plan
            if self.step != "review" or not plan or not self._key or form.get("plan") != plan["id"]:
                raise SetupError("These details changed or expired. Review them again.")
            if plan["replaces"] and form.get("replace_ok") != "yes":
                raise SetupError("Tick the box to replace the key that's saved now (CI starts using the new one at once).")
            if plan["warnings"]:
                raise SetupError(plan["warnings"][0])
            self.step, self.msg = "saving", "Testing the key with %s" % plan["ticket"]
        return plan

    def confirm(self, form, fetch=None, field_list=None):
        plan = self.confirm_start(form)
        self.save(plan, fetch, field_list)

    def save(self, plan, fetch=None, field_list=None):
        with self.lock:
            keys = dict(self._key or {})
        kind, settings = plan["kind"], dict(plan["settings"])
        token = ":".join(keys[n] for n in TR.secrets_for(kind, settings))   # Jira Cloud: email:token, as spec-pull uses it
        try:
            t = (fetch or TR.FETCH[kind])(settings, plan["ticket"], token)   # the same code path CI uses: fixed host only
        except TR.NotFound:
            raise SetupError("The key works, but %s wasn't found (or this key can't see it). Try a ticket you can open."
                             % plan["ticket"])
        warn = []
        if kind == "jira":
            if plan["ac_field_name"]:
                settings["ac_field"] = self._ac_field(settings, token, plan["ac_field_name"], field_list)
            if self._can_edit(settings, token, t):
                warn.append("This Jira token can edit tickets. story-gate only reads them: a read-only token is safer.")
        self.set("saving", "Saving the key as GitHub secrets")
        for name in plan["secrets"]:
            if name not in ALLOWED_SECRETS:
                raise SetupError("Refusing to write an unexpected secret.")
            G.set_repo_secret(self.token, self.repo, name, keys[name])
        local = ""
        if plan["local_copy"]:
            save_local(keys)
            local = "A copy is saved on this computer for spec-pull (readable by programs running as you)."
        self.forget_key()
        self.set("saving", "Opening the settings pull request")
        pr = open_settings_pr(self.gate, self.token, self.repo, kind, settings, plan)
        self.result = {"pr": pr, "warn": warn, "local": local, "title": t.get("title", "")[:120]}
        self.set("done", "Connected. Merge the pull request to turn it on.")

    def _ac_field(self, settings, token, name, field_list=None):
        st, fields = field_list if field_list else TR.get_json((TR.JIRA_GATEWAY % settings["cloud_id"]) + "/field",
                                                             TR.jira_auth(token))
        if st != 200:
            raise SetupError("Jira answered HTTP %s when listing fields." % st)
        fid, close = TR.match_field(fields, name)
        if not fid:
            raise SetupError("No single Jira field is called '%s'. Close matches: %s." % (name, ", ".join(close) or "none"))
        return fid

    def _can_edit(self, settings, token, ticket):
        """Best effort: True only when Jira says this token may edit the test ticket."""
        q = urllib.parse.urlencode({"permissions": "EDIT_ISSUES", "issueKey": ticket.get("key", "")})
        try:
            st, data = TR.get_json((TR.JIRA_GATEWAY % settings["cloud_id"]) + "/mypermissions?" + q, TR.jira_auth(token))
        except TR.TrackerError:
            return False
        perm = ((data or {}).get("permissions") or {}).get("EDIT_ISSUES") if st == 200 and isinstance(data, dict) else None
        return bool(isinstance(perm, dict) and perm.get("havePermission"))

    def cancel(self):
        with self.lock:
            if self.step in ("saving", "checking", "done"):
                raise SetupError("Too late to go back: this step is already running. Wait for it to finish.")
            self.plan, self._key = None, None
        self.set("details" if self.token else "signin", "Cancelled. Nothing was sent.")


def key_problem(key, what, hint):
    """Why a pasted key was refused, in words that say what to do. Never repeats the key."""
    if not key:
        return "Paste the key here: this box is empty."
    if any(c.isspace() for c in key):
        return "This has a space or line break in it, so it isn't %s. Copy it again with the Copy button." % what
    return ("This isn't %s: it's %d characters long. %s Did your browser fill in a saved password, or did you copy the "
            "key's name? Copy the key itself." % (what, len(key), hint))


def save_local(keys):
    """Owner-only file in the story-gate user folder (never in a repository). Other names already in it are kept."""
    p = G.config_dir() / LOCAL_FILE
    have = read_local(p)
    have.update({k: v for k, v in keys.items() if k in ALLOWED_SECRETS})
    G.write_private(p, "".join("%s=%s\n" % (k, have[k]) for k in sorted(have)))


def read_local(p=None):
    p = Path(p) if p else G.config_dir() / LOCAL_FILE
    out = {}
    if p.is_file():
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            k, _, v = line.strip().partition("=")
            if k in ALLOWED_SECRETS and v.strip():
                out[k] = v.strip()
    return out


def open_settings_pr(gate, token, repo, kind, settings, plan):
    """A pull request that changes only trackers.<kind> in .story-gate/config.json on the default branch."""
    import sg_settings as SET
    cfgp = SET.CONFIG
    st, ref, _ = G.call("GET", "/repos/%s/git/ref/heads/%s" % (repo, urllib.parse.quote(plan["branch"])), token)
    if st != 200:
        raise SetupError("Couldn't read %s's %s branch (HTTP %s)." % (repo, plan["branch"], st))
    tip = ref["object"]["sha"]
    st, f, _ = G.call("GET", "/repos/%s/contents/%s?ref=%s" % (repo, cfgp, tip), token)
    if st != 200 or not isinstance(f, dict) or f.get("encoding") != "base64":
        raise SetupError("%s has no %s yet. Set story-gate up first (story-gate init)." % (repo, cfgp))
    text = base64.b64decode(f.get("content") or "").decode("utf-8-sig")
    doc = json.loads(text)
    trackers = dict(doc.get("trackers") or {})
    old = trackers.get(kind) if isinstance(trackers.get(kind), dict) else {}
    if TR.site_of(kind, old) == TR.site_of(kind, settings) and not old.get("server"):
        settings = dict(old, **settings)  # same workspace or site: keep its other settings (e.g. ac_field)
    if trackers.get(kind) == settings:
        return {"url": None, "number": None, "same": True}
    trackers[kind] = settings
    new = dict(doc, trackers=trackers)
    try:
        new_full = SET.validate(gate, new)
    except SET.SettingsError as e:
        raise SetupError(str(e))
    looser = SET.looser_than(gate, gate.full_config(text), new_full)
    body = ["story-gate tracker-setup: connect %s (`%s`). Tracker support is **experimental**." % (plan["label"], plan["site"]),
            "", "Settings change (only `trackers.%s` in `%s`):" % (kind, cfgp), SET.fence(json.dumps(settings, indent=1), "json"),
            "Read-only key saved as GitHub secrets: %s. Their values are never shown here or anywhere else."
            % ", ".join("`%s`" % n for n in plan["secrets"]),
            "CI sends them only to `%s`." % plan["destination"], "",
            "Tested by reading %s." % plan["ticket"]]
    for note in plan.get("notes") or []:
        body += ["", "**Note:** " + note]
    if plan["public"]:
        body += ["", "**This repository is public.** `allow_in_public_repo` lets ticket text be copied into it, where anyone "
                     "can read it."]
    if looser:
        body += ["", "**This makes the rules looser than %s:**" % plan["branch"], SET.fence("\n".join("- " + x for x in looser), "text")]
    body += ["", "A code owner approves this pull request; GitHub doesn't count an approval from whoever opened it (or a code "
                 "owner adds the label `%s`)." % getattr(gate, "CHANGE_LABEL", "story-gate-change")]
    branch = "story-gate-tracker-%s-%s-%s" % (kind, time.strftime("%Y%m%d%H%M%S", time.gmtime()), secrets.token_hex(3))
    res = G.open_setup_pr(token, repo, {cfgp: SET.dump(new).encode("utf-8")}, branch=branch,
                          title="story-gate: connect %s (%s)" % (plan["label"], plan["site"]), body="\n".join(body),
                          base=plan["branch"], parent=tip)
    if res.get("branch") != branch:
        raise SetupError("GitHub returned another pull request (%s) instead of a new one." % res.get("url"))
    return res


# ------------------------------------------------------------------ the page
def make_handler(ss):
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):  # never log requests: a body could hold a key
            pass

        def _host_ok(self):
            return self.headers.get("Host", "") == "127.0.0.1:%d" % self.server.server_address[1]

        def _session_ok(self):
            c = self.headers.get("Cookie", "")
            got = dict(p.strip().split("=", 1) for p in c.split(";") if "=" in p).get("sg_ts", "")
            return bool(ss.cookie) and secrets.compare_digest(got, ss.cookie)

        def _send(self, code, body, ctype="text/html; charset=utf-8", extra=()):
            data = body.encode("utf-8")
            self.send_response(code)
            for k, v in extra:
                self.send_header(k, v)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'; form-action 'none'; frame-ancestors 'none'")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            if not self._host_ok() or ss.expired():
                return self._send(403, "forbidden")
            if u.path.startswith("/font/") and u.path[6:] in S.FONTS:
                data = (HERE / "vendor" / u.path[6:]).read_bytes()
                self.send_response(200); self.send_header("Content-Type", "font/woff2"); self.send_header("Content-Length", str(len(data)))
                self.end_headers(); self.wfile.write(data)
                return
            qs = urllib.parse.parse_qs(u.query)
            if u.path == "/" and "t" in qs:  # the printed link works once: swap it for a cookie, then drop it from the address
                with ss.lock:
                    fresh = ss.cookie is None and secrets.compare_digest(qs["t"][0], ss.url_token)
                    if fresh:
                        ss.cookie = secrets.token_urlsafe(24)
                if not fresh:
                    return self._send(403, S.page_shell("<h1>This link was already used.</h1><p>Run "
                                                       "<code>story-gate tracker-setup</code> again for a new one.</p>"))
                ss.touch()
                return self._send(303, "", extra=(("Location", "/"), ("Set-Cookie", "sg_ts=%s; HttpOnly; SameSite=Strict; Path=/" % ss.cookie)))
            if not self._session_ok():
                return self._send(403, "forbidden")
            if u.path == "/":
                ss.touch()
                with ss.lock:
                    ss.nonce = secrets.token_urlsafe(18)
                return self._send(200, page(ss))
            if u.path == "/state":  # the open page asks every 2 seconds: that counts as use
                ss.touch()
                return self._send(200, json.dumps(ss.snapshot()), "application/json")
            return self._send(404, "not found")

        def do_POST(self):
            u = urllib.parse.urlparse(self.path)
            origin = self.headers.get("Origin", "")
            if (not self._host_ok() or ss.expired() or not self._session_ok()
                    or origin != "http://127.0.0.1:%d" % self.server.server_address[1]
                    or not ss.nonce or not secrets.compare_digest(self.headers.get("X-SG-Nonce", ""), ss.nonce)):
                return self._send(403, json.dumps({"error": "forbidden"}), "application/json")
            ss.touch()
            n = min(int(self.headers.get("Content-Length") or 0), 8192)
            form = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8", "replace")).items()}
            act = u.path.strip("/")
            try:
                if act == "signin":
                    ss.start_signin()
                elif act == "details":
                    ss.details_start()
                    ss.background(lambda: ss.details(form))
                elif act == "confirm":
                    plan = ss.confirm_start(form)
                    ss.background(lambda: ss.save(plan))
                elif act == "cancel":
                    ss.cancel()
                else:
                    return self._send(404, json.dumps({"error": "not found"}), "application/json")
            except SetupError as e:
                if act == "confirm":  # the plan stays: fix the tick and confirm again
                    with ss.lock:
                        ss.msg = str(e)
                else:
                    ss.set("error", str(e))
            return self._send(200, json.dumps(ss.snapshot()), "application/json")
    return H


def page(ss):
    """The page: sign in, details, review, done. The state comes from /state; every POST carries this load's nonce."""
    pub = ("" if ss.private else
           "<label class=tool><input type=checkbox name=public_ok value=yes> <b>" + html.escape(ss.repo) + " is public.</b> "
           "I understand that ticket text copied into it can be read by anyone." + S.info("public", "this choice", "story-gate copies each ticket's text into "
           "the story's folder, so reviewers see exactly what was built against. In a public repository that text becomes "
           "public. Tick only if your tickets contain nothing private.") + "</label>")
    form = (
        "<form id=det onsubmit=\"event.preventDefault();send('details',this)\">"
        "<p class=lbl>Where your tickets live" + S.info("kind", "the tracker choice", "Pick where your team keeps its tickets. "
        "<b>Jira Cloud</b> is Jira at an address like <code>acme.atlassian.net</code>. Jira on your own servers (Server or Data "
        "Center) isn't covered here yet: use <code>story-gate jira-setup</code> and the guide.") + "</p>"
        "<label class=tool><input type=radio name=kind value=linear checked onchange=kindf()> Linear</label>"
        "<label class=tool><input type=radio name=kind value=jira onchange=kindf()> Jira Cloud</label>"
        "<div id=lin><label class=lbl>Workspace" + S.info("workspace", "the workspace", "The short name of your Linear account. "
        "Open any ticket and look at the address: in <code>linear.app/acme/issue/ENG-12</code> the workspace is <code>acme</code>. "
        "It isn't secret.") + "<span>The short name in your Linear links: linear.app/<b>workspace</b>/...</span>"
        "<input name=workspace autocomplete=off></label>"
        "<label class=lbl>Read-only API key" + S.info("linkey", "the Linear key", "A Linear personal API key with "
        "<b>Read</b> access only. Linear: Settings &gt; Security &amp; access &gt; Personal API keys &gt; New key. It starts with "
        "<code>lin_api_</code>. Linear shows it once, so copy it before you close that window. It goes into an encrypted GitHub "
        "secret, never into your repository.") + "<span>Linear: Settings &gt; Security &amp; access &gt; Personal API keys. "
        "Choose read-only access.</span><input name=key type=password autocomplete=new-password placeholder='lin_api_...'></label></div>"
        "<div id=jir style=display:none><label class=lbl>Site" + S.info("site", "the Jira site", "Your Jira Cloud "
        "address, from any ticket link: in <code>acme.atlassian.net/browse/ENG-12</code> the site is <code>acme.atlassian.net</code>. "
        "Leave out <code>https://</code> and anything after the name.") + "<span>Like your-company.atlassian.net</span>"
        "<input name=site autocomplete=off></label>"
        "<label class=lbl>Email" + S.info("email", "the email", "The email you sign in to Atlassian with: the account "
        "that made the token below. Jira needs the two together.") + "<span>The Atlassian account that made the token</span><input name=email autocomplete=off></label>"
        "<label class=lbl>Read-only API token" + S.info("jiratoken", "the Jira token", "An Atlassian API token "
        "<b>with scopes</b>: id.atlassian.com &gt; Security &gt; API tokens &gt; Create API token with scopes &gt; Jira &gt; tick "
        "only <code>read:jira-work</code>. It's about 190 characters and starts with <code>ATATT</code>. Atlassian shows it once.") + "<span>id.atlassian.com &gt; Security &gt; API tokens &gt; "
        "<b>Create API token with scopes</b>, read-only Jira scopes.</span>"
        "<input name=key type=password autocomplete=new-password placeholder='ATATT...' disabled></label>"
        "<label class=lbl>Acceptance-criteria field <b class=opt>Optional</b>" + S.info("acfield", "the acceptance-criteria field",
        "Only if your team keeps acceptance criteria in a separate Jira field. Type that field's name exactly as Jira shows it, e.g. "
        "<code>Acceptance criteria</code>. Leave it empty if they're in the description, under an <b>Acceptance criteria</b> "
        "heading.") + "<span>Its name in Jira, if you keep acceptance "
        "criteria in their own field</span><input name=ac_field autocomplete=off></label></div>"
        "<label class=lbl>A ticket to test with" + S.info("ticket", "the test ticket", "Any ticket this key can open, "
        "e.g. <code>ENG-12</code>. story-gate reads it once to prove the key works, the same way CI will. Nothing in the ticket "
        "is changed.") + "<span>Any ticket this key can see, like ENG-12</span>"
        "<input name=ticket autocomplete=off></label>" + pub +
        "<label class=tool><input type=checkbox name=local_copy value=yes> Also keep a copy on this computer, so "
        "<code>spec-pull</code> works here <em>(off by default; other programs running as you can read it)</em>"
        + S.info("local", "the local copy", "Lets <code>story-gate spec-pull</code> read tickets on this computer without "
                 "setting environment variables. It's saved in your story-gate user folder, readable only by your user account, "
                 "but other programs you run (including AI tools) can read it too. CI doesn't need it. Leave it off if unsure.")
        + "</label>"
        "<button id=rv>Review</button> <span class=btnerr id=btnerr></span></form>")
    js = """<script>
const N=%s;
function kindf(){const j=document.querySelector('input[name=kind]:checked').value=='jira';
document.getElementById('lin').style.display=j?'none':'';document.getElementById('jir').style.display=j?'':'none';
document.querySelector('#lin input[name=key]').disabled=j;document.querySelector('#jir input[name=key]').disabled=!j}
async function post(a,body){const r=await fetch('/'+a,{method:'POST',headers:{'X-SG-Nonce':N},body:body||''});
if(r.status==403){document.getElementById('msg').textContent='This page expired. Run story-gate tracker-setup again.';return}tick()}
function send(a,f){dismissed='';clearErr();post(a,new URLSearchParams(new FormData(f)))}
function fieldEl(n,kind){const scope=kind?(kind=='jira'?'#jir':'#lin'):'#det';const e=document.querySelector(scope+' [name="'+n+'"]');return e&&!e.disabled&&e.offsetParent!==null?e:null}
function clearErr(){shownErr='';document.querySelectorAll('#det .bad').forEach(e=>e.classList.remove('bad'));document.querySelectorAll('#det .ferr').forEach(e=>e.remove());
document.querySelectorAll('#det [aria-invalid]').forEach(e=>{e.removeAttribute('aria-invalid');e.removeAttribute('aria-describedby')});document.getElementById('btnerr').textContent='';document.getElementById('rv').classList.remove('bad')}
let shownErr='',dismissed='';function showErr(fe){let k=fe?fe.field+'|'+fe.msg:'';if(k&&k===dismissed)k='';if(k===shownErr)return;clearErr();shownErr=k;
if(!k)return;const f=fieldEl(fe.field,fe.kind);const fix=()=>{dismissed=k;clearErr()};const m=el('div',fe.msg);m.className='ferr';m.id='ferr';m.setAttribute('role','alert');
if(f){f.classList.add('bad');f.setAttribute('aria-invalid','true');f.setAttribute('aria-describedby','ferr');(f.closest('label')||f).after(m);f.focus();f.addEventListener('input',fix,{once:true});f.addEventListener('change',fix,{once:true})}
else document.getElementById('det').prepend(m);
document.getElementById('btnerr').textContent='Fix the field marked in red, then press Review again.';document.getElementById('rv').classList.add('bad')}
function el(t,txt){const e=document.createElement(t);e.textContent=txt;return e}
async function tick(){const r=await fetch('/state');if(!r.ok){document.getElementById('msg').textContent='This page expired. Run story-gate tracker-setup again.';return}
const s=await r.json();document.getElementById('msg').textContent=s.msg||'';showErr(s.step=='details'?s.field_error:null);
for(const k of ['signin','details','review','done'])document.getElementById('c-'+k).style.display='none';
const show=s.step=='checking'||s.step=='error'?(s.login?'details':'signin'):s.step=='saving'?'review':s.step;
document.getElementById('c-'+show).style.display='';
const d=document.getElementById('dev');d.textContent='';if(s.device){d.append(el('p','1. Open '),el('p','2. Type this code there and click Authorize:'));
const a=document.createElement('a');a.href=a.textContent=s.device.uri;a.target='_blank';a.rel='noopener';d.firstChild.append(a);d.append(el('div',s.device.code));d.lastChild.className='code'}
if(s.plan&&show=='review'){const p=s.plan,ul=document.getElementById('plan');ul.textContent='';
for(const [k,v] of [['GitHub account',p.github],['Repository',p.repo],['Tracker',p.label+' - '+p.site],
['Your key is sent only to',p.destination],['GitHub secrets are saved through',p.github_api],
['Network proxy',p.proxy||'none'],['Test ticket',p.ticket],['GitHub secrets written',p.secrets.join(', ')],
['Copy on this computer',p.local_copy?'yes':'no'],['Ticket text in a public repository',p.public?'allowed':'no (private repository)']]){const li=el('li','');li.append(el('b',k+': '),el('span',v));ul.append(li)}
document.getElementById('rep').style.display=p.replaces.length?'':'none';document.getElementById('repn').textContent=p.replaces.join(', ');
document.getElementById('pid').value=p.id;for(const w of p.warnings||[])ul.append(el('li','Stop: '+w));for(const w of p.notes||[])ul.append(el('li','Note: '+w))}
if(s.result&&show=='done'){const o=document.getElementById('out');o.textContent='';
if(s.result.pr&&s.result.pr.url){const a=document.createElement('a');a.className='btn';a.href=s.result.pr.url;a.target='_blank';a.rel='noopener';a.textContent='Review and merge on GitHub';o.append(a)}
else o.append(el('p','The settings already say this, so no pull request was needed.'));
for(const w of s.result.warn)o.append(el('p','Warning: '+w));if(s.result.local)o.append(el('p',s.result.local))}}
kindf();tick();setInterval(tick,2000)</script>""" % json.dumps(ss.nonce)
    cards = (
        "<section class=step id=c-signin><div><h2>1. Sign in to GitHub</h2><p>You'll type a short code on github.com. That "
        "step is yours alone: your AI can't do it for you.</p><button onclick=\"post('signin')\">Start</button><div id=dev></div></div></section>"
        "<section class=step id=c-details style=display:none><div><h2>2. Your tracker</h2><p>Nothing is sent with your key "
        "until you confirm on the next screen.</p>%s</div></section>"
        "<section class=step id=c-review style=display:none><div><h2>3. Check and confirm</h2><ul id=plan></ul>"
        "<form onsubmit=\"event.preventDefault();send('confirm',this)\"><input type=hidden name=plan id=pid>"
        "<label class=tool id=rep><input type=checkbox name=replace_ok value=yes> Replace the saved <b id=repn></b> "
        "(CI starts using the new key at once)</label><button>Confirm</button> "
        "<button type=button class=link style=margin-left:14px onclick=\"post('cancel')\">Back</button></form></div></section>"
        "<section class=step id=c-done style=display:none><div><h2>4. Done</h2><div id=out></div><p class=hint>Merge the "
        "pull request to turn it on. Then ask your AI to pull a ticket into a story.</p></div></section>" % form)
    return S.page_shell(
        "<div class=eyebrow>story-gate · connect a tracker · %s</div><h1>Connect Linear or Jira.</h1>"
        "<style>.btnerr{color:var(--error);font-size:14px;font-weight:600;margin-left:8px}</style>"
        "<p class=lead>Your key goes straight into encrypted GitHub secrets. It's never written to your repository, and your "
        "AI never sees it. Tracker support is experimental.</p><p class=msg id=msg></p>%s"
        "<div class=foot>Runs only on this computer (127.0.0.1). This page stops 10 minutes after you close it (30 minutes at most).</div>%s"
        % (html.escape(ss.repo), cards, js))


def run(gate, top, open_browser=True, port=0, serve_seconds=LIFETIME):
    repo = S.github_repo(top)
    if not repo or not gate.REPO_NAME.fullmatch(repo):
        print("story-gate tracker-setup: this folder's 'origin' isn't a GitHub repository. Run it in your project folder.")
        return 1
    bad = unsafe_environment()
    if bad:
        print("story-gate tracker-setup: refusing to run, because this shell has settings that would send your key or your "
              "GitHub sign-in somewhere else, or let another program read them: %s. Open a new terminal without them (check "
              "your shell's startup files if they come back), then run it again." % ", ".join(bad))
        return 1
    ss = Session(gate, top, repo, open_browser=open_browser)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), make_handler(ss))
    url = "http://127.0.0.1:%d/?t=%s" % (srv.server_address[1], ss.url_token)
    print("story-gate tracker-setup for %s (experimental)\nOpen this page (it should open by itself). The link works once:\n  %s\n"
          "No browser here? Do it by hand instead (see the guide, 'Specs in Linear' and 'Specs in Jira'):\n"
          "  - for CI, add the secret STORY_GATE_LINEAR_KEY (Linear) or STORY_GATE_JIRA_EMAIL and STORY_GATE_JIRA_TOKEN "
          "(Jira) at %s/%s/settings/secrets/actions/new\n"
          "  - on this computer, set LINEAR_API_KEY, or JIRA_EMAIL and JIRA_API_TOKEN, for spec-pull\n"
          "  - add \"trackers\" to .story-gate/config.json in a pull request of its own"
          % (repo, url, G.WEB, repo))
    sys.stdout.flush()
    if open_browser and not os.environ.get("BROWSER"):  # BROWSER runs a command of its own choosing with the link
        webbrowser.open(url)
    elif open_browser:
        print("BROWSER is set in this shell, so the page wasn't opened for you: copy the link above into your browser.")
    srv.timeout = 1
    t0 = time.time()
    while time.time() - t0 < serve_seconds and not ss.expired() and ss.step != "done":
        srv.handle_request()
    for _ in range(5):
        srv.handle_request()
    srv.server_close()
    ss.forget_key()
    if ss.step == "done":
        pr = (ss.result or {}).get("pr") or {}
        print("story-gate: connected. %s" % ("Merge the pull request to turn it on: %s" % pr["url"] if pr.get("url")
                                             else "The settings already had it."))
        return 0
    print("story-gate tracker-setup stopped before the end. Nothing more was changed. Run it again to continue.")
    return 1
