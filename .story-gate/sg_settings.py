"""story-gate settings: see and change .story-gate/config.json without editing it by hand.

  settings                         every setting: its value here, what CI enforces, and what it does
  settings KEY                     one setting
  settings set KEY VALUE [--pr]    change it (a list is comma-separated; "" empties it)
  settings unset KEY [--pr]        put it back to story-gate's default

Without --pr the change is written to this folder's .story-gate/config.json. With --pr nothing here changes: the same
change is made to the default branch's config.json on a new branch, as a pull request of its own that a code owner
approves. Every value is checked with the rules the gate itself applies. Keys and tokens are never accepted: they belong
in environment variables and GitHub secrets, not in the repository.
"""
import base64, difflib, json, os, re, secrets, stat, tempfile, time

# key: (kind, what it does). Kinds: bool, choice:a|b, list, list:a|b, users, reviewers, repos, number01, int0, int1,
# text, regex, branch, issue.
SETTINGS = {
    "mode": ("choice:warn|enforce", "warn reports problems; enforce blocks them at every point"),
    "enforce_points": ("list:ci|pre_edit|checkpoint|stop", "where problems block while mode is warn"),
    "accept_concerns": ("bool", "true lets a CONCERNS verdict count as passing (not recommended)"),
    "story_id_pattern": ("regex", "how story IDs look in branch names"),
    "base_branch": ("branch", "the branch pull requests merge into"),
    "exempt_globs": ("list", "files the gate never asks a story for"),
    "test_command": ("text", "the command CI runs as the project's real test suite"),
    "junit_path": ("text", "the JUnit XML file that command writes, so each test's own result counts"),
    "test_globs": ("list", "where test files live"),
    "spec_files": ("list", "PRD/TRD files fingerprinted at READY"),
    "require_spec_link": ("bool", "true: every story must link the spec it builds"),
    "spec_source_check": ("choice:warn|block", "a copied issue that changed, or couldn't be checked: warn or block"),
    "spec_repos": ("repos", "other repositories (owner/repo) whose issues spec-pull may copy in"),
    "thresholds.pass": ("number01", "score a check needs to PASS"),
    "thresholds.concerns": ("number01", "score below which a check FAILS instead of raising CONCERNS"),
    "judge_mode": ("choice:full|objective", "full: an AI judge scores the work; objective: only checks story-gate can verify"),
    "judge.emulated_allow_pass": ("bool", "lets a non-Jev judge award PASS (after judge-calibrate passes)"),
    "judge.allow_self_judge_pass": ("bool", "lets the AI's own scores reach PASS (not recommended)"),
    "approvers": ("users", "extra people, on top of CODEOWNERS, who can accept work"),
    "reviewers": ("reviewers", "review bots or people whose reviews count as independent"),
    "require_independent_review": ("bool", "an independent review of the latest commit is required"),
    "dashboard_issue": ("issue", "the issue the dashboard summary is posted to (none: off)"),
    "checkpoint.every_edits": ("int0", "edits between automatic checkpoints (0: off)"),
    "writing.enforce": ("bool", "the plain-writing check blocks instead of advising"),
    "writing.target": ("number01", "plain-writing score needed"),
    "writing.diagram_min_files": ("int1", "a change touching this many files needs a diagram"),
    "validation.required": ("bool", "DONE needs validation.md and a passing scenario for every acceptance criterion"),
}
# Shown by `settings`, changed another way.
ELSEWHERE = {
    "trackers": "Linear and Jira tickets (`story-gate jira-setup` prints the Jira part); edit config.json in a pull request of its own",
    "sources": "where specs come from; edit config.json in a pull request of its own",
    "sinks": "where verdicts and learnings are sent; edit config.json in a pull request of its own",
    "project_hooks_allowed": "your own AI-tool hooks allowed to run; edit config.json in a pull request of its own",
    "judge.provider": "which AI judge scores the work; edit config.json in a pull request of its own",
    "judge.max_chars": "how much text the judge reads; edit config.json in a pull request of its own",
    "models": "model size per task; edit config.json in a pull request of its own",
    "model_tiers": "what each model size means; edit config.json in a pull request of its own",
    "writing.standard": "the plain-writing standard; edit config.json in a pull request of its own",
}
EXTRA_KNOWN = ("min_runtime_version",)  # top-level keys the gate reads that aren't in DEFAULT_CONFIG
GH_USER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}")
BOT = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}(?:\[bot\])?")
BRANCH = re.compile(r"(?!-)(?!.*\.\.)(?!.*//)(?!.*@\{)[A-Za-z0-9._/-]{1,200}(?<![./])(?<!\.lock)")
_B = r"(?<![\w-])"  # a token starts at the beginning of a word, not after a hyphen ("make test-sk-model" is fine)
SECRET = re.compile("|".join([
    _B + r"gh[pousr]_[A-Za-z0-9]{20,}", _B + r"github_pat_\w{20,}", _B + r"sk-(?=[\w-]*\d)[A-Za-z0-9_-]{32,}", _B + r"sk_(?:live|test)_\w{16,}",
    _B + r"lin_(?:api|oauth)_\w{16,}", _B + r"ATATT\w{16,}", _B + r"xox[abprs]-[\w-]{10,}", _B + r"glpat-[\w-]{20,}",
    _B + r"npm_[A-Za-z0-9]{30,}", _B + r"AIza[\w-]{30,}", _B + r"hf_[A-Za-z0-9]{30,}", _B + r"(?:AKIA|ASIA)[0-9A-Z]{16}",
    _B + r"eyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{10,}",  # a JSON web token
    r"-----BEGIN [A-Z ]*PRIVATE KEY", r"(?i:\b(?:bearer|basic)\s+[A-Za-z0-9+/=._~-]{16,})",
    r"://[^/\s:@]+:[^/\s@]+@",  # a password inside a link
]))
DICTS = ("thresholds", "judge", "writing", "validation", "checkpoint", "models", "model_tiers", "trackers")
CONFIG = ".story-gate/config.json"
REPO = re.compile(r"$^")  # owner/repo names: gate.REPO_NAME, set in cli()


class SettingsError(Exception):
    """A plain-English reason the change wasn't made."""


def parse(key, raw):
    """The JSON value for KEY from the text typed on the command line; SettingsError when it isn't allowed."""
    kind = SETTINGS[key][0]
    raw = "" if raw is None else str(raw)
    if SECRET.search(raw):
        raise SettingsError("that value looks like a key or token. story-gate never puts secrets in config.json: put keys in "
                            "environment variables on your computer and in GitHub secrets for CI")
    s = raw.strip()
    if "\n" in s or "\r" in s or len(s) > 500:
        raise SettingsError("%s must be one line of at most 500 characters" % key)
    if kind == "bool":
        v = s.lower()
        if v in ("true", "yes", "on", "1"):
            return True
        if v in ("false", "no", "off", "0"):
            return False
        raise SettingsError("%s must be true or false" % key)
    if kind.startswith("choice:"):
        opts = kind[7:].split("|")
        if s not in opts:
            raise SettingsError("%s must be one of: %s" % (key, ", ".join(opts)))
        return s
    if kind in ("number01",):
        try:
            v = float(s)
        except ValueError:
            v = -1.0
        if not 0 <= v <= 1:
            raise SettingsError("%s must be a number from 0 to 1" % key)
        return v
    if kind in ("int0", "int1"):
        low = int(kind[3])
        if not re.fullmatch(r"\d{1,6}", s) or int(s) < low:
            raise SettingsError("%s must be a whole number of at least %d" % (key, low))
        return int(s)
    if kind == "issue":
        if s.lower() in ("", "none", "null", "off"):
            return None
        if not re.fullmatch(r"#?[1-9]\d{0,9}", s):
            raise SettingsError("%s must be an issue number, or none" % key)
        return int(s.lstrip("#"))
    if kind == "regex":
        if not s or len(s) > 200:
            raise SettingsError("%s must be a pattern of 1 to 200 characters" % key)
        if re.search(r"\(\?[aiLmsux]+\)", s):
            raise SettingsError("%s can't use inline flags such as (?i): the gate puts the pattern inside a larger one" % key)
        try:  # the forms the gate compiles it in
            for form in (r"(?<![A-Za-z0-9])(?:%s)(?![0-9])", r"\s*\[?(%s)\]?(?:[:\s]|$)"):
                re.compile(form % s)
        except re.error as e:
            raise SettingsError("%s isn't a valid pattern (%s)" % (key, e))
        return s
    if kind == "branch":
        if not BRANCH.fullmatch(s):
            raise SettingsError("%s must be a branch name such as main" % key)
        return s
    if kind == "text":
        return s
    items = []  # every remaining kind is a list
    for x in (p.strip() for p in s.split(",")):
        if x and x not in items:
            items.append(x)
    check = {"users": GH_USER, "reviewers": BOT}.get(kind)
    for x in items:
        if kind.startswith("list:") and x not in kind[5:].split("|"):
            raise SettingsError("%s takes only: %s (got %r)" % (key, ", ".join(kind[5:].split("|")), x))
        if check and not check.fullmatch(x.lstrip("@")):
            raise SettingsError("%r isn't a GitHub user name" % x)
        if kind == "repos" and not REPO.fullmatch(x):
            raise SettingsError("%r isn't an owner/repo name" % x)
    return [x.lstrip("@") for x in items] if check else items


def known_key(key):
    if key in SETTINGS:
        return key
    near = difflib.get_close_matches(key, list(SETTINGS), n=1)
    raise SettingsError("there's no setting called %r%s. Run `story-gate settings` to see them all"
                        % (key, " (did you mean %s?)" % near[0] if near else ""))


def changed(doc, key, value=None, remove=False):
    """A copy of the config.json object with KEY set (or removed). Other keys, known or not, are kept as they are."""
    out = json.loads(json.dumps(doc))
    top, _, leaf = key.partition(".")
    if not leaf:
        if remove:
            out.pop(top, None)
        else:
            out[top] = value
        return out
    part = dict(out.get(top)) if isinstance(out.get(top), dict) else {}
    if remove:
        part.pop(leaf, None)
    else:
        part[leaf] = value
    if part:
        out[top] = part
    else:
        out.pop(top, None)
    return out


def value_of(c, key):
    top, _, leaf = key.partition(".")
    v = c.get(top)
    return (v.get(leaf) if isinstance(v, dict) else None) if leaf else v


def unknown_keys(doc, defaults):
    """Top-level keys in config.json that story-gate doesn't read (usually a typo)."""
    return sorted(k for k in (doc if isinstance(doc, dict) else {}) if k not in defaults and k not in EXTRA_KNOWN)


def validate(G, doc):
    """The full config the gate would use for this config.json object; SettingsError when the gate would refuse it."""
    for k in DICTS:
        if k in doc and not isinstance(doc[k], dict):
            raise SettingsError('"%s" in %s must be an object like {...}. Fix it by hand first' % (k, CONFIG))
    c = G.full_config(json.dumps(doc))
    try:
        G.check_config(c)
    except G.ConfigError as e:
        raise SettingsError(str(e).replace(" - the gate fails closed until fixed", ""))
    th = c.get("thresholds") or {}
    try:
        if float(th.get("concerns", 0)) > float(th.get("pass", 1)):
            raise SettingsError("thresholds.concerns (%s) can't be higher than thresholds.pass (%s)" % (th.get("concerns"), th.get("pass")))
    except (TypeError, ValueError):
        raise SettingsError("thresholds must be numbers")
    return c


def looser_than(G, old_full, new_full):
    """Plain-English list of ways new_full is looser than old_full (weaker() is the same check CI reports)."""
    try:
        return G.T.weaker(old_full, new_full)
    except Exception:  # odd values in the committed policy: say so rather than guess
        return ["story-gate couldn't compare it with the current policy, so a code owner should read the change closely"]


def policy_ref(G):
    """(ref, label) for the policy CI enforces: the enrolled policy ref, else the remote's default branch (origin/HEAD),
    else origin/main or origin/master, else a local main or master. Never the base_branch in the file being changed,
    which the change itself could point somewhere else. A preview only: CI makes the same comparison on the pull request."""
    e = G.enrolled()
    if e and e.get("policy_ref"):
        return e["policy_ref"], e["policy_ref"]
    head = G.git("symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD").strip()
    for ref in ([head] if head else []) + ["origin/main", "origin/master", "main", "master"]:
        if G.git("rev-parse", "--verify", "-q", ref + "^{commit}").strip():
            return ref, ref
    return None, "your main branch"


def read_local(G):
    p = G.GATE / "config.json"
    if not p.is_file():
        raise SettingsError("story-gate isn't set up in this folder (no %s). Run `story-gate init` first" % CONFIG)
    text = p.read_text(encoding="utf-8-sig")
    try:
        doc = json.loads(text)
        assert isinstance(doc, dict)
    except Exception:
        raise SettingsError("%s is unreadable JSON. Fix it by hand first (or restore it with git)" % CONFIG)
    return p, text, doc


def dump(doc):
    return json.dumps(doc, indent=1, ensure_ascii=False) + "\n"


def write_local(p, before, doc):
    """Replace config.json in one step, and only if nobody changed it since `before` was read."""
    now = p.read_text(encoding="utf-8-sig") if p.is_file() else None
    if now != before:
        raise SettingsError("%s changed while this ran. Nothing was written: run the command again" % CONFIG)
    mode = stat.S_IMODE(p.stat().st_mode) if p.is_file() else None
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".config.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(dump(doc))
        if mode is not None:
            os.chmod(tmp, mode)  # mkstemp makes the file private; keep the permissions config.json had
        os.replace(tmp, str(p))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def show(v):
    return "none" if v is None else json.dumps(v, ensure_ascii=False)


def describe(key, value, remove):
    return "put %s back to the default" % key if remove else "set %s to %s" % (key, show(value))


def strictness(looser, label):
    return ["Looser than %s: %s." % (label, "; ".join(looser))] if looser else ["Not looser than %s." % label]


def fence(text, lang=""):
    """A Markdown code block that the text can't break out of."""
    ticks = "`" * max(3, 1 + max([len(m) for m in re.findall(r"`+", text)] or [0]))
    return "%s%s\n%s\n%s" % (ticks, lang, text, ticks)


def cmd_set(G, key, raw, remove, pr):
    key = known_key(key)
    value = None if remove else parse(key, raw)
    if pr:
        return open_pr(G, key, value, remove)
    p, before, doc = read_local(G)
    new = changed(doc, key, value, remove)
    new_full = validate(G, new)
    if new == doc:
        print("%s is already %s here. Nothing changed." % (key, show(value_of(new_full, key))))
        return 0
    ref, label = policy_ref(G)
    base_text = G.git("show", "%s:%s" % (ref, CONFIG)) if ref else ""
    old_value = value_of(G.full_config(json.dumps(doc)), key)
    write_local(p, before, new)
    print("Changed %s here: %s -> %s (%s)." % (key, show(old_value), show(value_of(new_full, key)), CONFIG))
    if not base_text:
        print("%s has no story-gate settings yet, so CI isn't using any." % label)
    else:
        looser = looser_than(G, G.full_config(base_text), new_full)
        for line in strictness(looser, label + " (what CI enforces)"):
            print(line)
        if not looser:
            print("CI uses it once it's merged into the default branch.")
        if looser:
            print("A code owner has to approve this in a pull request; CI reports it as a weaker rule.")
            if G.enrolled():
                print("Until it's merged, this computer keeps the stricter rules from %s." % label)
    print("Next: put %s in a pull request of its own (story-gate files can't ride along with other changes), "
          "or run this again with --pr and story-gate opens that pull request for you." % CONFIG)
    return 0


def open_pr(G, key, value, remove):
    """Make the change to the default branch's config.json as its own pull request. Nothing local changes."""
    import sg_github as GH
    repo = G.origin_repo()
    if not repo:
        raise SettingsError("this folder's git remote 'origin' isn't a GitHub repository, so there's nowhere to open a pull request")
    tok = GH.human_token()
    if not tok:
        raise SettingsError("sign in to GitHub first (`gh auth login`), then run this again")

    def ok(path):
        st, data, _ = GH.call("GET", path, tok)
        if st == 404:
            return None
        if st != 200 or not isinstance(data, dict):
            raise SettingsError("GitHub refused GET %s (HTTP %s)" % (path.split("?")[0], st))
        return data

    meta = ok("/repos/%s" % repo)
    if not meta:
        raise SettingsError("%s isn't visible with your GitHub sign-in" % repo)
    base = meta.get("default_branch") or "main"
    ref = ok("/repos/%s/git/ref/heads/%s" % (repo, base))
    if not ref:
        raise SettingsError("%s has no branch %s" % (repo, base))
    tip = ref["object"]["sha"]
    f = ok("/repos/%s/contents/%s?ref=%s" % (repo, CONFIG, tip))
    if not f or f.get("encoding") != "base64":
        raise SettingsError("%s has no %s on %s yet. Set story-gate up first (`story-gate init`)" % (repo, CONFIG, base))
    try:
        text = base64.b64decode(f.get("content") or "").decode("utf-8-sig")
        doc = json.loads(text)
        assert isinstance(doc, dict)
    except Exception:
        raise SettingsError("%s on %s is unreadable JSON. Fix it by hand in a pull request of its own" % (CONFIG, base))
    new = changed(doc, key, value, remove)
    new_full = validate(G, new)
    old_full = G.full_config(text)
    if new == doc:
        print("%s is already %s on %s. No pull request needed." % (key, show(value_of(new_full, key)), base))
        return 0
    looser = looser_than(G, old_full, new_full)
    what = describe(key, value, remove)
    body = ["story-gate settings: %s `%s`." % ("reset" if remove else "change", key), "",
            "Before:", fence(show(value_of(old_full, key)), "json"), "After:", fence(show(value_of(new_full, key)), "json"), ""]
    if looser:
        body += ["**This makes the rules looser than %s:**" % base, fence("\n".join("- " + x for x in looser), "text")]
    else:
        body += ["Not looser than %s." % base]
    body += ["", "Only `%s` changes. A code owner approves this pull request; GitHub doesn't count an approval from "
                 "whoever opened it or pushed its latest commit (or a code owner adds the label `%s`)."
             % (CONFIG, getattr(G, "CHANGE_LABEL", "story-gate-change"))]
    branch = "story-gate-settings-%s-%s-%s" % (re.sub(r"[^a-z0-9]+", "-", key.lower()).strip("-"),
                                                time.strftime("%Y%m%d%H%M%S", time.gmtime()), secrets.token_hex(3))
    try:
        res = GH.open_setup_pr(tok, repo, {CONFIG: dump(new).encode("utf-8")}, branch=branch,
                               title="story-gate settings: %s %s" % ("reset" if remove else "change", key),
                               body="\n".join(body), base=base, parent=tip)
    except RuntimeError as e:
        raise SettingsError(str(e))
    if res.get("branch") != branch:
        raise SettingsError("GitHub returned another pull request (%s) instead of a new one. Check it before running this again" % res.get("url"))
    print("Opened pull request #%s to %s: %s" % (res["number"], what, res["url"]))
    for line in strictness(looser, base):
        print(line)
    print("Nothing changed in this folder. A code owner approves the pull request; it takes effect when it's merged.")
    return 0


def rows(G):
    """[(key, here, ci, effective, meaning, note)] for every setting."""
    p, _, doc = read_local(G)
    here = G.full_config(json.dumps(doc))
    ref, label = policy_ref(G)
    base_text = G.git("show", "%s:%s" % (ref, CONFIG)) if ref else ""
    ci = G.full_config(base_text) if base_text else None
    try:
        eff = G.cfg() if G.enrolled() else None
    except G.ConfigError:
        eff = None
    out = []
    for key, (_, meaning) in SETTINGS.items():
        out.append((key, value_of(here, key), value_of(ci, key) if ci else None, value_of(eff, key) if eff else None, meaning, ""))
    for key, why in ELSEWHERE.items():
        out.append((key, value_of(here, key), value_of(ci, key) if ci else None, None, why, "elsewhere"))
    return out, label, bool(base_text), unknown_keys(doc, G.DEFAULT_CONFIG)


def cmd_list(G, only=None):
    if only and only not in ELSEWHERE:
        only = known_key(only)
    table, label, have_ci, unknown = rows(G)
    print("story-gate settings: \"here\" is this folder's %s; %s is what CI enforces." % (CONFIG, label))
    if not have_ci:
        print("%s has no story-gate settings yet, so CI isn't using any." % label)
    for key, here, ci, eff, meaning, note in table:
        if only and key != only:
            continue
        short = lambda v: (lambda t: t if len(t) <= 80 else t[:77] + "...")(show(v))
        line = "%s = %s" % (key, short(here))
        if have_ci and ci != here:
            line += "   (%s: %s)" % (label, short(ci))
        if eff is not None and eff != here:
            line += "   (in effect on this computer: %s)" % short(eff)
        print(line)
        print("    " + meaning)
    for k in unknown:
        print("Unknown setting %r in %s: story-gate ignores it (a typo?)." % (k, CONFIG))
    if not only:
        print("Change one: story-gate settings set KEY VALUE [--pr]   (back to the default: settings unset KEY)")
    return 0


def cli(G, args):
    """Entry point for `gate.py settings ...`."""
    global REPO
    REPO = G.REPO_NAME
    pr = "--pr" in args
    a = [x for x in args if x != "--pr"]
    try:
        if not a:
            return cmd_list(G)
        if a[0] == "set":
            if len(a) != 3:
                raise SettingsError('usage: settings set KEY VALUE [--pr]   (quote a value with spaces; "" empties a list)')
            return cmd_set(G, a[1], a[2], False, pr)
        if a[0] == "unset":
            if len(a) != 2:
                raise SettingsError("usage: settings unset KEY [--pr]")
            return cmd_set(G, a[1], None, True, pr)
        if len(a) == 1 and not pr:
            return cmd_list(G, a[0])
        raise SettingsError("usage: settings | settings KEY | settings set KEY VALUE [--pr] | settings unset KEY [--pr]")
    except SettingsError as e:
        print("story-gate settings: %s" % e)
        return 1
    except (OSError, ValueError) as e:  # network, disk or encoding trouble: a plain message, never a traceback
        print("story-gate settings: nothing was changed (%s: %s)" % (e.__class__.__name__, str(e)[:200]))
        return 1
