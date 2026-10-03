"""story-gate pinned project hooks: an approved hook runs only the default branch's copy of the repository files it uses.

Problem this solves: approval of a project hook (on the default branch) covers the command *text*, e.g.
`bash scripts/check.sh`. A branch can change scripts/check.sh without touching the settings file, so git never re-runs
the checkout filter and the AI tool would run the branch's version of the script.

How:
  * Every approved command is sorted into a tier:
      plain     - runs nothing from the repository (e.g. `ruff check`, `/usr/local/bin/lint`): left as is
      pinned    - listed in project_hooks_allowed with `pins` (the repo files/folders it uses), and simple enough to
                  rewrite safely: the checkout filter swaps it for the story-gate runner
      accepted  - listed with "runs_repo_code": "accepted": runs as is; doctor labels it unverified
      blocked   - runs repository code but isn't pinned or accepted: removed from the hook file on disk
  * The runner (`gate.py run-approved <id>`, from the signed runtime) re-checks at run time that the command is still
    approved and pinned on the default branch, that the pinned files in the working tree match the default branch
    exactly (no edits, no extra files), then runs the command with the pinned paths pointing at a protected copy taken
    from the default branch's commit. Anything else: refused (exit 2) with the exact files and the one command a
    human can use to trust their own edits (bound to that exact content).
See docs/hook-pinning.md for the threat model.
"""
import base64, hashlib, json, os, re, shlex, shutil, subprocess, sys, time
from pathlib import Path

import sg_trust as T

RUNNERS = {"npm", "npx", "yarn", "pnpm", "bun", "bunx", "make", "just", "task", "gradle", "gradlew", "mvn", "mvnw", "rake",
           "bundle", "poetry", "uv", "uvx", "pipenv", "tox", "nox", "cargo", "go", "deno", "dotnet", "composer", "pre-commit",
           "lefthook", "husky", "turbo", "nx"}  # run code chosen by repository files (package.json, Makefile, ...)
PROJECT_VARS = ("CLAUDE_PROJECT_DIR", "CURSOR_PROJECT_DIR", "GEMINI_PROJECT_DIR", "CODEX_PROJECT_DIR", "PROJECT_DIR")
_VAR_PREFIX = re.compile(r"^(?:\$\{?(?:%s)\}?|%%(?:%s)%%)[/\\]" % ("|".join(PROJECT_VARS), "|".join(PROJECT_VARS)))
SHELL_META = re.compile(r"[|&;<>()`*?\[\]{}~!#\n\\]|\$")  # anything beyond plain words and quotes
WRAP_RE = re.compile(r'run-approved\s+([A-Za-z0-9_-]+)\s*$')
DEFAULT_MAX_AGE_DAYS = 7


# ------------------------------------------------------------------ policy
def entries(cfg):
    """project_hooks_allowed, normalised: {command: {"pins": [...], "accepted": bool}}. Plain strings have no pins."""
    out = {}
    for e in (cfg or {}).get("project_hooks_allowed") or []:
        if isinstance(e, str):
            out[e] = {"pins": [], "accepted": False}
        elif isinstance(e, dict) and isinstance(e.get("command"), str):
            pins = []
            for p in e.get("pins") or []:
                p = p.strip().replace("\\", "/") if isinstance(p, str) else ""
                p = p[2:] if p.startswith("./") else p
                p = p.rstrip("/")
                if p and ".." not in p.split("/") and not p.startswith("/") and not re.match(r"^[A-Za-z]:", p):
                    pins.append(p)
            out[e["command"]] = {"pins": pins,
                                 "accepted": e.get("runs_repo_code") == "accepted"}
    return out


def allowed_commands(cfg):
    return set(entries(cfg))


def policy(top):
    """(ref, commit, config) for an enrolled repository, read from the default branch; (None, None, {}) otherwise."""
    e = T.enrollment(top) or {}
    ref = e.get("policy_ref")
    sha = T.policy_commit(top, ref) if ref and e.get("enrolled") else None
    if not sha:
        return ref, None, {}
    try:
        cfg = json.loads(T.policy_text(top, ref, "config.json") or "{}")
    except ValueError:
        cfg = {}
    return ref, sha, cfg if isinstance(cfg, dict) else {}


def _git(top, *args, data=None):
    r = subprocess.run(["git", *args], cwd=str(top), input=data, capture_output=True)
    return r.stdout if r.returncode == 0 else None


# ------------------------------------------------------------------ classifying commands
def _rel(token):
    """The repository-relative path a command token names, if it is written as one."""
    t = _VAR_PREFIX.sub("", token, count=1)
    explicit = t != token or t.startswith("./")
    t = t[2:] if t.startswith("./") else t
    return t.replace("\\", "/"), explicit


def analyse(cmd, top, sha):
    """-> dict(parse_ok, simple, runner, refs, outside). refs = repository paths the command names (as files/folders that
    exist in the default branch's commit); outside = tokens that point outside the repository (../)."""
    info = {"parse_ok": True, "simple": not SHELL_META.search(_VAR_PREFIX.sub("", cmd)) if "\\" not in cmd else False,
            "runner": False, "refs": [], "outside": []}
    try:
        tokens = shlex.split(cmd, posix=True) if "\\" not in cmd else shlex.split(cmd.replace("\\", "/"), posix=True)
    except ValueError:
        info.update(parse_ok=False, simple=False)
        return info
    if not tokens:
        info["parse_ok"] = False
        return info
    head = os.path.basename(tokens[0]).lower()
    head = re.sub(r"\.(exe|cmd|bat|ps1)$", "", head)
    if head in RUNNERS or (head.startswith("python") and "-m" in tokens[1:3]) or (head in ("node", "deno", "bun") and "-e" in tokens):
        info["runner"] = True
    if any(v in cmd for v in PROJECT_VARS) and not any(_VAR_PREFIX.match(t) for t in tokens):
        info["simple"] = False  # project variable used in a way we can't map to a path
    cands = []
    for t in tokens:
        if t.startswith("-"):
            t = t.split("=", 1)[1] if "=" in t else ""
        if not t:
            continue
        rel, explicit = _rel(t)
        if rel.startswith("../") or rel == "..":
            info["outside"].append(t)
            continue
        if rel and not rel.startswith("/") and not re.match(r"^[A-Za-z]:/", rel):
            cands.append((rel.rstrip("/"), explicit))
    if cands and sha:
        out = _git(top, "cat-file", "--batch-check", data=("\n".join("%s:%s" % (sha, c) for c, _ in cands) + "\n").encode()) or b""
        lines = out.decode("utf-8", "replace").splitlines()
        for (c, explicit), line in zip(cands, lines):
            if not line.endswith("missing"):
                info["refs"].append(c)
            elif explicit:
                info["refs"].append(c)  # written as a repo path (./x, $CLAUDE_PROJECT_DIR/x) but not on the default branch
    elif cands:
        info["refs"] = [c for c, explicit in cands if explicit]
    return info


def covered(ref, pins):
    return any(ref == p or ref.startswith(p + "/") for p in pins)


def classify(cmd, top, sha, entry):
    """-> (tier, info). tier: plain | pinned | accepted | blocked."""
    info = analyse(cmd, top, sha)
    runs_repo = info["runner"] or bool(info["refs"]) or bool(info["outside"]) or not info["parse_ok"]
    if not runs_repo:
        return "plain", info
    if entry and entry.get("accepted"):
        return "accepted", info
    pins = (entry or {}).get("pins") or []
    if (pins and info["parse_ok"] and info["simple"] and not info["runner"] and not info["outside"]
            and info["refs"] and all(covered(r, pins) for r in info["refs"])):
        return "pinned", info
    return "blocked", info


# ------------------------------------------------------------------ wrapping (checkout filter side)
def encode(cmd):
    return base64.urlsafe_b64encode(cmd.encode("utf-8")).decode().rstrip("=")


def decode(token):
    return base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode("utf-8")


def wrap(cmd, py, launcher):
    return '"%s" -I "%s" run-approved %s' % (str(py).replace("\\", "/"), str(launcher).replace("\\", "/"), encode(cmd))


def unwrap_cmd(c):
    m = WRAP_RE.search(c or "") if isinstance(c, str) and "run-approved" in c else None
    if not m:
        return None
    try:
        return decode(m.group(1))
    except (ValueError, UnicodeDecodeError):
        return None


def _walk_commands(data, fn):
    """Apply fn(command) -> new command | None (drop entry) to every hook entry's "command" string."""
    dropped = []
    def walk(x, depth):
        if isinstance(x, dict):
            if depth and isinstance(x.get("command"), str):
                new = fn(x["command"])
                if new is None:
                    dropped.append(x["command"])
                    return None
                x = dict(x, command=new)
            out = {}
            for k, v in x.items():
                nv = walk(v, depth + 1)
                if nv is None or (isinstance(v, (dict, list)) and v and not nv):
                    continue
                out[k] = nv
            return out
        if isinstance(x, list):
            return [w for w in (walk(v, depth + 1) for v in x) if not (w is None or (w in ({}, [])))]
        return x
    return walk(data, 0), dropped


def smudge(data_bytes, top, path, py=None, launcher=None):
    """After the sanitizer: swap pinned commands for the runner, drop repository-code commands that aren't pinned or
    accepted. Returns (bytes, notes)."""
    if not path.endswith(".json"):
        return data_bytes, []
    try:
        data = json.loads(data_bytes.decode("utf-8")) if data_bytes.strip() else None
    except (ValueError, UnicodeDecodeError):
        return data_bytes, []
    if not isinstance(data, dict):
        return data_bytes, []
    _, sha, cfg = policy(top)
    ents = entries(cfg)
    py = py or sys.executable
    launcher = launcher or T.launcher_path()
    notes = []
    def fn(cmd):
        if unwrap_cmd(cmd) is not None or T.HOOK_SIGNATURE.search(cmd):
            return cmd  # already a story-gate command
        tier, info = classify(cmd, top, sha, ents.get(cmd))
        if tier == "pinned":
            return wrap(cmd, py, launcher)
        if tier == "blocked":
            notes.append(cmd)
            return None
        return cmd
    new, dropped = _walk_commands(data, fn)
    if not dropped and json.dumps(new, sort_keys=True) == json.dumps(data, sort_keys=True):
        return data_bytes, []
    return (json.dumps(new, indent=2) + "\n").encode("utf-8"), notes


def unwrap_bytes(data_bytes):
    """Our filtered copy with every runner command turned back into the original (for the clean filter)."""
    try:
        data = json.loads(data_bytes.decode("utf-8")) if data_bytes.strip() else None
    except (ValueError, UnicodeDecodeError):
        return data_bytes
    if not isinstance(data, (dict, list)):
        return data_bytes
    changed = []
    def fn(cmd):
        orig = unwrap_cmd(cmd)
        if orig is not None:
            changed.append(1)
            return orig
        return cmd
    new, _ = _walk_commands(data, fn)
    return (json.dumps(new, indent=2) + "\n").encode("utf-8") if changed else data_bytes


# ------------------------------------------------------------------ the runner (AI tool side)
def differing(top, sha, pins):
    """Pinned paths whose working-tree content isn't exactly the default branch's (edited, deleted, retyped, or new
    untracked files inside a pinned folder)."""
    out = set((_git(top, "diff", "--name-only", "--no-renames", sha, "--", *pins) or b"").decode("utf-8", "replace").split())
    out |= set((_git(top, "ls-files", "-o", "--exclude-standard", "--", *pins) or b"").decode("utf-8", "replace").split())
    if _git(top, "diff", "--quiet", sha, "--", *pins) is None and not out:
        out.add("(unreadable)")
    return sorted(out)


def content_hash(top, pins):
    """Hash of what is in the working tree right now under the pins (for a human's explicit trust of their own edits)."""
    names = sorted(set((_git(top, "ls-files", "-z", "-c", "-o", "--exclude-standard", "--", *pins) or b"").decode("utf-8", "replace").split("\0")) - {""})
    h = hashlib.sha256()
    for n in names:
        p = Path(top) / n
        h.update(n.encode() + b"\0")
        if p.is_symlink():
            h.update(b"symlink:" + os.readlink(p).encode())
        elif p.is_file():
            h.update(hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()


def trust_path():
    return T.G.config_dir() / "local-hook-trust.json"


def trusted_locally(top, cmd, pins):
    rec = T.read_json(trust_path()).get(T.repo_identity(top)[1] or "", {}).get(cmd)
    return bool(rec and rec.get("content_sha256") == content_hash(top, pins))


def trust_local(top, cmd, revoke=False):
    """Human-only: run my own edited copy of this pinned hook's files, for exactly this content. Logged."""
    _, sha, cfg = policy(top)
    ent = entries(cfg).get(cmd)
    if not ent or not ent["pins"]:
        raise T.TrustError("'%s' isn't a pinned hook in project_hooks_allowed on the default branch" % cmd)
    data = T.read_json(trust_path())
    ident = T.repo_identity(top)[1]
    if revoke:
        data.get(ident, {}).pop(cmd, None)
    else:
        data.setdefault(ident, {})[cmd] = {"content_sha256": content_hash(top, ent["pins"]),
                                           "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                           "by": os.environ.get("USER") or os.environ.get("USERNAME") or "?"}
    T.write_json_atomic(trust_path(), data)
    return ent["pins"]


def cache_root():
    return T.G.config_dir() / "approved-cache"


def ensure_cache(top, sha, pins):
    """A protected copy of the pinned files from the default branch's commit (content-addressed; never the branch)."""
    key = hashlib.sha256(("%s\0%s" % (sha, "\0".join(sorted(pins)))).encode()).hexdigest()[:24]
    dest = cache_root() / key
    if (dest / ".complete").is_file():
        return dest
    tmp = cache_root() / (key + ".tmp-%d" % os.getpid())
    shutil.rmtree(tmp, ignore_errors=True)
    listing = (_git(top, "ls-tree", "-r", "-z", sha, "--", *pins) or b"").decode("utf-8", "replace").split("\0")
    if not any(listing):
        raise T.TrustError("the pinned files aren't on the default branch (%s)" % ", ".join(pins))
    for row in filter(None, listing):
        meta, name = row.split("\t", 1)
        mode, kind, oid = meta.split()
        if kind != "blob" or mode not in ("100644", "100755"):
            raise T.TrustError("%s is a %s on the default branch; pinned hooks run regular files only" % (name, "symlink" if mode == "120000" else kind))
        parts = name.split("/")
        if ".." in parts or name.startswith("/"):
            raise T.TrustError("unsafe path in the default branch: %s" % name)
        f = tmp.joinpath(*parts)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(_git(top, "cat-file", "blob", oid) or b"")
        if mode == "100755":
            f.chmod(0o755)
    (tmp / ".complete").write_text(sha, encoding="utf-8")
    try:
        os.replace(tmp, dest)
    except OSError:  # another hook built it at the same moment
        shutil.rmtree(tmp, ignore_errors=True)
    return dest


def rewrite(cmd, cache, pins):
    """The command with every pinned path pointing at the protected copy."""
    tokens = shlex.split(cmd.replace("\\", "/"), posix=True)
    out = []
    for t in tokens:
        prefix, val = "", t
        if t.startswith("-") and "=" in t:
            prefix, val = t.split("=", 1)[0] + "=", t.split("=", 1)[1]
        rel, _ = _rel(val)
        rel = rel.rstrip("/")
        out.append(prefix + (str(cache.joinpath(*rel.split("/"))).replace("\\", "/") if rel and covered(rel, pins) else val))
    return out


def shell_argv(tokens=None, cmdline=None):
    """How the command runs: POSIX sh; on Windows Git Bash when present (what Claude Code uses), else cmd.exe."""
    if os.name == "nt":
        bash = shutil.which("bash")
        if bash:
            return [bash, "-c", shlex.join(tokens) if tokens is not None else cmdline]
        return ["cmd.exe", "/d", "/s", "/c", subprocess.list2cmdline(tokens) if tokens is not None else cmdline]
    return ["/bin/sh", "-c", shlex.join(tokens) if tokens is not None else cmdline]


def policy_age_days(top):
    try:
        fh = Path(_git(top, "rev-parse", "--git-common-dir").decode().strip())
        fh = (fh if fh.is_absolute() else Path(top) / fh) / "FETCH_HEAD"
        return (time.time() - fh.stat().st_mtime) / 86400 if fh.is_file() else None
    except Exception:
        return None


def refuse(msg):
    sys.stderr.write("story-gate: %s\n" % msg)
    return 2


def run_approved(token):
    """Entry point for `gate.py run-approved <id>`. Never runs anything it can't vouch for."""
    try:
        cmd = decode(token)
    except (ValueError, UnicodeDecodeError):
        return refuse("this hook entry is damaged; re-check it out (git checkout -- <hook file>)")
    start = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    top = T.repo_identity(start)[0]
    if not top:
        return refuse("hook '%s' not run: not inside a git repository" % cmd)
    ref, sha, cfg = policy(top)
    if not sha:
        return refuse("hook '%s' not run: this repository isn't enrolled, or its default branch (%s) can't be found. "
                      "Fetch it (git fetch) or run gate.py enroll" % (cmd, ref or "?"))
    ent = entries(cfg).get(cmd)
    tier, _ = classify(cmd, top, sha, ent)
    if tier != "pinned":
        return refuse("hook '%s' not run: it isn't approved with pins on %s any more (tier: %s)" % (cmd, ref, tier))
    pins = ent["pins"]
    age = policy_age_days(top)
    limit = cfg.get("policy_max_age_days", DEFAULT_MAX_AGE_DAYS)
    if age is not None and isinstance(limit, (int, float)) and age > limit:
        sys.stderr.write("story-gate: warning: your copy of %s is %d days old; run git fetch so hooks use current approvals\n" % (ref, age))
    diff = differing(top, sha, pins)
    env = dict(os.environ)
    real_top = os.path.normcase(os.path.realpath(top))
    env["PATH"] = os.pathsep.join(p for p in env.get("PATH", "").split(os.pathsep)
                                  if p and not os.path.normcase(os.path.realpath(p)).startswith(real_top))  # no repo-supplied programs
    if diff:
        if not trusted_locally(top, cmd, pins):
            return refuse("hook '%s' not run: %s differ from %s, so this could be a branch's code. If these are your own "
                          "edits, a human can allow exactly this content with: gate.py hook-trust \"%s\"" % (cmd, ", ".join(diff[:5]), ref, cmd))
        argv = shell_argv(cmdline=cmd)  # your own edits, explicitly trusted for this exact content
    else:
        try:
            cache = ensure_cache(top, sha, pins)
        except T.TrustError as ex:
            return refuse("hook '%s' not run: %s" % (cmd, ex))
        argv = shell_argv(tokens=rewrite(cmd, cache, pins))
    try:
        return subprocess.run(argv, cwd=start if os.path.isdir(start) else top, env=env).returncode  # stdin/out/err pass through
    except OSError as ex:
        return refuse("hook '%s' could not start: %s" % (cmd, ex))


# ------------------------------------------------------------------ reporting (doctor, CI)
_REF_PATTERNS = (re.compile(r"""(?:^|\s)(?:source|\.)\s+["']?([\w./${}-]+)"""),
                 re.compile(r"""\$\(dirname\s+["']?\$\{?0\}?["']?\)/([\w./-]+)"""),
                 re.compile(r"""require\(\s*["'](\.{1,2}/[^"']+)["']\s*\)"""),
                 re.compile(r"""from\s+["'](\.{1,2}/[^"']+)["']"""),
                 re.compile(r"""(?:^|\s)(?:from|import)\s+([A-Za-z_][\w.]*)""", re.M),
                 re.compile(r"""["']([\w./-]+\.(?:sh|bash|py|js|mjs|cjs|ts|rb|ps1|pl|json|toml|ya?ml))["']"""))


def unpinned_references(top, sha, pins):
    """Best-effort: repository files the pinned scripts appear to use that aren't pinned. A hint, not proof."""
    listing = (_git(top, "ls-tree", "-r", "-z", "--name-only", sha) or b"").decode("utf-8", "replace").split("\0")
    tree = set(filter(None, listing))
    found = set()
    for name in [n for n in tree if covered(n, pins)]:
        blob = _git(top, "cat-file", "blob", "%s:%s" % (sha, name)) or b""
        if len(blob) > 300000 or b"\0" in blob[:2000]:
            continue
        text = blob.decode("utf-8", "replace")
        base = os.path.dirname(name)
        for pat in _REF_PATTERNS:
            for m in pat.finditer(text):
                ref = m.group(1).replace("${", "").replace("}", "")
                cands = [os.path.normpath(os.path.join(base, ref)).replace("\\", "/"), ref.lstrip("./")]
                if "." in ref and "/" not in ref and pat is _REF_PATTERNS[4]:  # python dotted module
                    mod = ref.replace(".", "/")
                    cands += [os.path.join(base, mod + ".py").replace("\\", "/"), mod + ".py", mod + "/__init__.py"]
                elif pat is _REF_PATTERNS[4]:
                    cands += [os.path.join(base, ref + ".py").replace("\\", "/"), ref + ".py", ref + "/__init__.py"]
                for c in cands:
                    if c in tree and not covered(c, pins):
                        found.add(c)
    return sorted(found)


def report(top, default_hook_commands=()):
    """Every project hook command the default branch approves, with its tier, for doctor and CI."""
    ref, sha, cfg = policy(top)
    if not sha:
        return []
    ents = entries(cfg)
    rows = []
    for cmd in sorted(set(ents) | set(default_hook_commands)):
        if T.HOOK_SIGNATURE.search(cmd) or unwrap_cmd(cmd) is not None:
            continue
        tier, info = classify(cmd, top, sha, ents.get(cmd))
        hint = ""
        if tier == "pinned":
            extra = unpinned_references(top, sha, ents[cmd]["pins"])
            hint = ("pins may be incomplete - the scripts seem to use: %s" % ", ".join(extra[:5])) if extra else ""
        elif tier == "blocked":
            if info["runner"] or not info["simple"] or info["outside"] or not info["parse_ok"]:
                hint = ('runs code chosen by repository files; can only run if a code owner sets "runs_repo_code": "accepted" '
                        '(unverified) in project_hooks_allowed')
            else:
                hint = 'add {"command": %s, "pins": %s} to project_hooks_allowed on %s' % (json.dumps(cmd), json.dumps(sorted(set(info["refs"]))), ref)
        rows.append((cmd, tier, hint))
    return rows
