"""story-gate trusted local runtime: the same rule CI follows, applied on the developer's computer.

- Hooks run a copy of story-gate installed in YOUR user folder (pinned by sha256), never the copy in the
  checked-out branch. A branch can't swap the gate code.
- Settings (mode, thresholds, judge) come from the repository's default branch (`git show origin/HEAD:...`).
  The working tree may only make them stricter. A branch can't loosen the rules.
- Hooks are registered in each AI client's USER settings (~/.claude, ~/.codex, ~/.cursor, ~/.gemini), outside
  any repository. Windsurf/Devin and Grok have no verified user-level hooks yet: they run in reduced protection.
- Upgrades are explicit and verified against the story-gate release key with `ssh-keygen -Y verify`.

Stdlib only. Nothing here talks to the network.
"""
import difflib, hashlib, json, os, re, shutil, subprocess, sys, tempfile, time
from pathlib import Path


# Public key that signs story-gate releases. Its fingerprint is published on the GitHub release page; compare
# them the first time you install (`gate.py install --user` prints it).
RELEASE_NAMESPACE = "story-gate-release"
RELEASE_SIGNERS = "story-gate-release ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIIN8LkolTGexkEFLGhW/dq7fmyAMw8qW8tsQUx1koD3y"
RELEASE_FINGERPRINT = "SHA256:YN6hCUUHe1XHbhYDj1VdYwoeVoIWDDlFJ6yEXDAcR+4"
RUNTIME_GLOBS = ("*.py", "PROTOCOL.md", "SKILL.md", "vendor/*")
HOOK_SIGNATURE = re.compile(r'(?:gate|launch)\.py"?\s+hook\s+--client')  # every story-gate hook command, repo or runtime
LAUNCHER = '''"""story-gate launcher: hooks and git filters call this stable path; it runs the active, pinned runtime."""
import json, os, subprocess, sys
here = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("STORY_GATE_HOME", os.path.dirname(here))
if sys.argv[1:2] == ["hook"]:
    d = os.getcwd()
    while not os.path.exists(os.path.join(d, ".git")):
        if os.path.dirname(d) == d:
            print("{}")
            sys.exit(0)  # outside any git repository: nothing to gate
        d = os.path.dirname(d)
try:
    with open(os.path.join(here, "active.json"), encoding="utf-8") as f:
        gate = os.path.join(json.load(f)["dir"], "gate.py")
except Exception:
    sys.stderr.write("story-gate: the trusted runtime is missing. Run: gate.py install --user\\n")
    sys.exit(2)
sys.exit(subprocess.run([sys.executable, "-I", gate] + sys.argv[1:]).returncode)
'''
USER_CLIENTS = ("claude", "codex", "cursor", "gemini", "windsurf")
DEGRADED_CLIENTS = ("grok",)  # user-level location documented, but merging with project hooks is unverified; see docs/client-security.md


class _LazyGitHub:  # sg_github pulls in http/xml modules; load it only when needed so hooks stay fast
    def __getattr__(self, name):
        import sg_github
        return getattr(sg_github, name)


G = _LazyGitHub()


class TrustError(Exception):
    pass


# ------------------------------------------------------------------ locations
def user_home():
    return Path(os.environ.get("STORY_GATE_USER_HOME") or Path.home())


def runtime_root():
    return G.config_dir() / "runtime"


def enrolled_path():
    return G.config_dir() / "enrolled.json"


def active_path():
    return runtime_root() / "active.json"


def read_json(p, default=None):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return {} if default is None else default


def write_json_atomic(p, obj):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp-%d" % os.getpid())
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def is_runtime(script_dir):
    """True when this gate.py is running from the installed user runtime (not from a repository)."""
    try:
        sd = Path(script_dir).resolve()
        return sd.parent == runtime_root().resolve() and (sd / "manifest.json").is_file()
    except Exception:
        return False


# ------------------------------------------------------------------ manifests and signatures
def sha256(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def runtime_files(src):
    src = Path(src)
    out = set()
    for g in RUNTIME_GLOBS:
        for f in src.glob(g):
            if f.is_file() and not f.is_symlink() and "__pycache__" not in f.parts:
                out.add(f.relative_to(src).as_posix())
    return sorted(out)


def make_manifest(src, version):
    return {"version": version, "files": {f: sha256(Path(src) / f) for f in runtime_files(src)}}


def ssh_keygen():
    cands = [shutil.which("ssh-keygen")]
    if os.name == "nt":
        cands += [r"C:\Windows\System32\OpenSSH\ssh-keygen.exe", r"C:\Program Files\Git\usr\bin\ssh-keygen.exe"]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    raise TrustError("ssh-keygen not found (it comes with OpenSSH or Git for Windows); it is needed to check release signatures")


def key_fingerprint(signers=RELEASE_SIGNERS):
    """sha256 fingerprint of the release public key, in the form ssh-keygen prints."""
    import base64
    parts = signers.split()
    try:
        blob = base64.b64decode(parts[2])
    except Exception:
        return "unknown"
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def verify_release(src, signers=RELEASE_SIGNERS):
    """src holds release.json + release.json.sig. Returns the manifest if the signature is valid and every file matches."""
    src = Path(src)
    rel, sig = src / "release.json", src / "release.json.sig"
    if not rel.is_file() or not sig.is_file():
        raise TrustError("no signed release in %s (release.json + release.json.sig). Use a tagged story-gate release, "
                         "or pass --unsigned to install a development copy at your own risk" % src)
    with tempfile.TemporaryDirectory() as td:
        allowed = Path(td) / "allowed_signers"
        allowed.write_text(signers.strip() + "\n", encoding="utf-8")
        with open(rel, "rb") as fh:
            r = subprocess.run([ssh_keygen(), "-Y", "verify", "-f", str(allowed), "-I", RELEASE_NAMESPACE,
                                "-n", RELEASE_NAMESPACE, "-s", str(sig)], stdin=fh, capture_output=True, text=True)
    if r.returncode != 0:
        raise TrustError("release signature is NOT valid for the story-gate release key (%s)" % (r.stderr or r.stdout).strip()[:200])
    man = read_json(rel)
    files = man.get("files") or {}
    if not files:
        raise TrustError("signed release lists no files")
    bad = [f for f, h in files.items() if not (src / f).is_file() or sha256(src / f) != h]
    if bad:
        raise TrustError("files differ from the signed release: %s" % ", ".join(bad[:5]))
    extra = [f for f in runtime_files(src) if f not in files]
    if extra:
        raise TrustError("files not covered by the signed release: %s" % ", ".join(extra[:5]))
    return man


def sign_release(src, key, version):
    """Maintainer only: write release.json and release.json.sig for the files in src."""
    src = Path(src)
    man = make_manifest(src, version)
    (src / "release.json").write_text(json.dumps(man, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    sig = src / "release.json.sig"
    if sig.exists():
        sig.unlink()
    r = subprocess.run([ssh_keygen(), "-Y", "sign", "-f", str(key), "-n", RELEASE_NAMESPACE, str(src / "release.json")],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise TrustError("signing failed: %s" % (r.stderr or r.stdout).strip()[:200])
    return man


def fetch_release(url, dest):
    """Download a release archive over HTTPS (zip or tar.gz) and return the folder that holds its signed .story-gate files.
    Nothing in it is trusted until verify_release() has checked the signature."""
    import io, tarfile, urllib.request, zipfile
    if not url.startswith("https://"):
        raise TrustError("upgrades are only downloaded over https://")
    data = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "story-gate"}), timeout=60).read()
    if len(data) > 50_000_000:
        raise TrustError("release archive is unexpectedly large")
    dest = Path(dest)
    def safe(name):
        p = (dest / name).resolve()
        if not str(p).startswith(str(dest.resolve()) + os.sep):
            raise TrustError("archive tries to write outside its folder: %s" % name)
        return p
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for m in z.infolist():
                if m.is_dir():
                    continue
                p = safe(m.filename); p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(z.read(m))
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as t:
            for m in t.getmembers():
                if not m.isfile():
                    continue  # links and devices are never extracted
                p = safe(m.name); p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(t.extractfile(m).read())
    hits = sorted(dest.rglob("release.json"), key=lambda x: len(x.parts))
    if not hits:
        raise TrustError("the archive has no signed release (release.json)")
    return hits[0].parent


# ------------------------------------------------------------------ runtime install / verify
def install_runtime(src, version, unsigned=False, signers=RELEASE_SIGNERS):
    """Copy the release in src into runtime/<version>, verify, then activate atomically. Returns the version dir."""
    src = Path(src)
    signed = None
    if not unsigned:
        signed = verify_release(src, signers)
        version = signed.get("version") or version
    root = runtime_root()
    root.mkdir(parents=True, exist_ok=True)
    G.lock_down(root.parent, directory=True)
    dest = root / ("%s%s" % (version, "-unsigned" if unsigned else ""))
    stage = root / (".staging-%d-%d" % (os.getpid(), int(time.time())))
    if stage.exists():
        shutil.rmtree(stage)
    for f in runtime_files(src):
        (stage / f).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src / f, stage / f)
    man = make_manifest(stage, version)
    if signed and man["files"] != {f: h for f, h in signed["files"].items() if f in man["files"]}:
        shutil.rmtree(stage)
        raise TrustError("copied files do not match the signed release")
    man.update({"signed": bool(signed), "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "signer": key_fingerprint(signers) if signed else None})
    (stage / "manifest.json").write_text(json.dumps(man, indent=2) + "\n", encoding="utf-8")
    if dest.exists():
        shutil.rmtree(dest)
    os.replace(stage, dest)
    old = read_json(active_path())
    launcher_path().write_text(LAUNCHER, encoding="utf-8")
    write_json_atomic(active_path(), {"version": version, "dir": str(dest), "previous": old.get("dir"),
                                      "previous_manifest_sha256": old.get("manifest_sha256"), "signed": bool(signed),
                                      "manifest_sha256": sha256(dest / "manifest.json"), "launcher_sha256": sha256(launcher_path())})
    return dest


def launcher_path():
    return runtime_root() / "launch.py"


def launcher_ok():
    p = launcher_path()
    return p.is_file() and p.read_text(encoding="utf-8") == LAUNCHER and read_json(active_path()).get("launcher_sha256") == sha256(p)


# ------------------------------------------------------------------ install manifest (so uninstall can undo everything)
def manifest_path():
    return G.config_dir() / "install-manifest.json"


def record(kind, key, **data):
    """Remember one change we made (first write wins for 'before' state), so uninstall can put it back exactly."""
    m = read_json(manifest_path())
    entry = m.setdefault(kind, {}).setdefault(key, {})
    for k, v in data.items():
        if k.endswith("_before") and k in entry:
            continue
        entry[k] = v
    write_json_atomic(manifest_path(), m)


def forget(kind, key):
    m = read_json(manifest_path())
    if key in m.get(kind, {}):
        del m[kind][key]
        write_json_atomic(manifest_path(), m)


def runtime_problems(sd, manifest_sha256):
    """Integrity of one runtime folder: its manifest must have the recorded hash and every file must match the manifest."""
    sd = Path(sd).resolve()
    problems = []
    mp = sd / "manifest.json"
    if not manifest_sha256 or not mp.is_file() or sha256(mp) != manifest_sha256:
        problems.append("runtime manifest was changed after install")
    man = read_json(mp)
    for f, h in (man.get("files") or {}).items():
        p = sd / f
        if not p.is_file() or sha256(p) != h:
            problems.append("runtime file changed after install: %s" % f)
    extra = [f for f in runtime_files(sd) if f not in (man.get("files") or {})]
    if extra:
        problems.append("unexpected files in the runtime: %s" % ", ".join(extra[:5]))
    return problems


def verify_self(script_dir):
    """The running runtime must match its install-time manifest and be the active one. Returns a list of problems."""
    sd = Path(script_dir).resolve()
    problems = []
    act = read_json(active_path())
    if not act or Path(act.get("dir", "")).resolve() != sd:
        problems.append("this runtime is not the active story-gate runtime (%s)" % sd)
    if act.get("manifest_sha256"):
        problems += runtime_problems(sd, act["manifest_sha256"])
    else:
        problems += [x for x in runtime_problems(sd, None) if "manifest was changed" not in x]
    if act and not launcher_ok():
        problems.append("the launcher (%s) was changed after install" % launcher_path())
    return problems


def version_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v))[:3]) or (0,)


# ------------------------------------------------------------------ repositories: enrollment and policy
def git_in(cwd, *args):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.stdout.strip() if r.returncode == 0 else ""


def repo_identity(cwd):
    """(toplevel, identity) for the repository containing cwd. Worktrees share one identity (the common git dir)."""
    top = git_in(cwd, "rev-parse", "--show-toplevel")
    if not top:
        return None, None
    common = git_in(cwd, "rev-parse", "--git-common-dir")
    common = os.path.realpath(os.path.join(cwd, common)) if common and not os.path.isabs(common) else os.path.realpath(common or top)  # git prints it relative to cwd
    return os.path.realpath(top), os.path.normcase(common)


def default_policy_ref(cwd, base_branch="main"):
    head = git_in(cwd, "symbolic-ref", "-q", "refs/remotes/origin/HEAD")
    if head:
        return head.replace("refs/remotes/", "", 1)
    for ref in ("origin/" + base_branch, base_branch, "origin/master", "master"):
        if git_in(cwd, "rev-parse", "--verify", "-q", ref + "^{commit}"):
            return ref
    return None


def enrollment(cwd):
    top, ident = repo_identity(cwd)
    if not ident:
        return None
    rec = read_json(enrolled_path()).get(ident)
    return dict(rec, toplevel=top, identity=ident) if rec else {"toplevel": top, "identity": ident, "enrolled": False}


def enroll(cwd, policy_ref=None):
    top, ident = repo_identity(cwd)
    if not ident:
        raise TrustError("not inside a git repository")
    ref = policy_ref or default_policy_ref(top)
    if not ref:
        raise TrustError("can't find the default branch (no origin/HEAD, main or master). Pass --policy-ref <branch>")
    data = read_json(enrolled_path())
    data[ident] = {"enrolled": True, "policy_ref": ref, "origin": git_in(top, "remote", "get-url", "origin"),
                   "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    write_json_atomic(enrolled_path(), data)
    return data[ident]


def unenroll(cwd):
    _, ident = repo_identity(cwd)
    data = read_json(enrolled_path())
    if ident in data:
        del data[ident]
        write_json_atomic(enrolled_path(), data)
        return True
    return False


def policy_commit(top, ref):
    """The commit the policy ref points at right now. "origin/main" means the remote-tracking branch only: a local
    branch or tag with the same name (which git would otherwise prefer) can't stand in for it."""
    if not ref:
        return None
    if "/" in ref and ref.split("/", 1)[0] in git_in(top, "remote").split():
        candidates = ("refs/remotes/" + ref,)  # a remote's branch: if the tracking ref is gone, fail closed
    else:
        candidates = ("refs/heads/" + ref, "refs/tags/" + ref)  # no remote (e.g. "main" in a local-only repo)
    for full in candidates:
        if subprocess.run(["git", "show-ref", "--verify", "-q", full], cwd=top, capture_output=True).returncode == 0:
            return git_in(top, "rev-parse", "--verify", "-q", full + "^{commit}") or None
    return None


def policy_text(top, ref, name):
    """Contents of .story-gate/<name> on the policy ref, pinned to the commit it resolves to right now. None if absent."""
    sha = policy_commit(top, ref)
    if not sha:
        return None
    r = subprocess.run(["git", "show", "%s:.story-gate/%s" % (sha, name)], cwd=top, capture_output=True)
    return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else None


def tighten(policy, local):
    """Working-tree settings may only make the policy stricter."""
    out = json.loads(json.dumps(policy))
    if not isinstance(local, dict):
        return out
    if local.get("mode") == "enforce":
        out["mode"] = "enforce"
    out["enforce_points"] = sorted(set(out.get("enforce_points") or []) | set(x for x in (local.get("enforce_points") or []) if isinstance(x, str)))
    out["accept_concerns"] = bool(out.get("accept_concerns")) and bool(local.get("accept_concerns", out.get("accept_concerns")))
    th, lt = dict(out.get("thresholds") or {}), local.get("thresholds") or {}
    for k in ("pass", "concerns"):
        try:
            if k in lt:
                th[k] = max(float(th.get(k, 0)), float(lt[k]))
        except (TypeError, ValueError):
            pass
    out["thresholds"] = th
    j, lj = dict(out.get("judge") or {}), local.get("judge") or {}
    for k in ("emulated_allow_pass", "allow_self_judge_pass"):
        if k in lj:
            j[k] = bool(j.get(k)) and bool(lj[k])
    out["judge"] = j
    if local.get("require_independent_review"):
        out["require_independent_review"] = True
    return out


# ------------------------------------------------------------------ user-level client hooks
def user_hook_files():
    h = user_home()
    return {"claude": h / ".claude" / "settings.json", "codex": h / ".codex" / "hooks.json",
            "cursor": h / ".cursor" / "hooks.json", "gemini": h / ".gemini" / "settings.json",
            "windsurf": h / ".codeium" / "windsurf" / "hooks.json"}


def user_protected_paths():
    """Places an agent must never write: the runtime, enrollment, agent key, and the user-level hook files."""
    return [G.config_dir()] + list(user_hook_files().values())


def hook_entries(py, gate):
    cmd = lambda cl, ev: '"%s" -I "%s" hook --client %s --event %s' % (py, gate, cl, ev)
    return {
        "claude": {"hooks": {
            "PreToolUse": [{"matcher": "Edit|Write|MultiEdit|NotebookEdit|Bash", "hooks": [{"type": "command", "command": cmd("claude", "pre"), "timeout": 15}]}],
            "PostToolUse": [{"matcher": "Edit|Write|MultiEdit|NotebookEdit", "hooks": [{"type": "command", "command": cmd("claude", "post"), "timeout": 180}]}],
            "Stop": [{"hooks": [{"type": "command", "command": cmd("claude", "stop"), "timeout": 60}]}]}},
        "codex": {"hooks": {
            "PreToolUse": [{"matcher": "^(apply_patch|Edit|Write|Bash|shell|local_shell|exec_command)$", "hooks": [{"type": "command", "command": cmd("codex", "pre"), "timeout": 15}]}],
            "PostToolUse": [{"matcher": "^(apply_patch|Edit|Write)$", "hooks": [{"type": "command", "command": cmd("codex", "post"), "timeout": 180}]}],
            "Stop": [{"hooks": [{"type": "command", "command": cmd("codex", "stop"), "timeout": 60}]}]}},
        "cursor": {"version": 1, "hooks": {  # failClosed: a crash or timeout blocks instead of letting the edit through
            "preToolUse": [{"command": cmd("cursor", "pre"), "matcher": "Write", "failClosed": True}],
            "beforeShellExecution": [{"command": cmd("cursor", "pre"), "failClosed": True}],
            "postToolUse": [{"command": cmd("cursor", "post"), "matcher": "Write"}],
            "stop": [{"command": cmd("cursor", "stop")}]}},
        "gemini": {"hooks": {
            "BeforeTool": [{"matcher": "write_file|replace|run_shell_command", "hooks": [{"type": "command", "command": cmd("gemini", "pre"), "timeout": 15000}]}],
            "AfterTool": [{"matcher": "write_file|replace", "hooks": [{"type": "command", "command": cmd("gemini", "post"), "timeout": 180000}]}],
            "AfterAgent": [{"matcher": "*", "hooks": [{"type": "command", "command": cmd("gemini", "stop"), "timeout": 60000}]}]}},
        "windsurf": {"hooks": {
            "pre_write_code": [{"command": cmd("windsurf", "pre"), "show_output": True}],
            "pre_run_command": [{"command": cmd("windsurf", "pre"), "show_output": True}],
            "post_cascade_response": [{"command": cmd("windsurf", "stop"), "show_output": True}]}},
    }


def ours(entry):
    return bool(HOOK_SIGNATURE.search(json.dumps(entry).replace('\\"', '"')))


def merged_hook_json(text, new):
    """Return the new JSON text for a hooks/settings file: our entries replaced, everything else untouched."""
    data = json.loads(text) if text.strip() else {}
    if not isinstance(data, dict):
        raise TrustError("expected a JSON object")
    for k, v in new.items():
        if k != "hooks":
            data.setdefault(k, v)
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise TrustError('"hooks" is not an object')
    for ev, entries in new.get("hooks", {}).items():
        cur = hooks.setdefault(ev, [])
        cur[:] = [e for e in cur if not ours(e)] + entries
    return json.dumps(data, indent=2) + "\n"


def removed_hook_json(text):
    data = json.loads(text) if text.strip() else {}
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if isinstance(hooks, dict):
        for ev in list(hooks):
            if isinstance(hooks[ev], list):
                hooks[ev] = [e for e in hooks[ev] if not ours(e)]
                if not hooks[ev]:
                    del hooks[ev]
    return json.dumps(data, indent=2) + "\n"


def apply_file(path, new_text, dry_run, out, check_json=True):
    """Write with a backup and an install-manifest entry; print a diff; never clobber a file we couldn't parse.
    Writes through symlinks (dotfile managers keep their link)."""
    path = Path(path)
    real = Path(os.path.realpath(path))
    existed = real.exists()
    old = real.read_text(encoding="utf-8") if existed else ""
    if old == new_text:
        out.append("  %s: already up to date" % path)
        return False
    if check_json:
        json.loads(new_text)  # round-trip check before anything touches disk
    out.extend("    " + l.rstrip("\n") for l in difflib.unified_diff(old.splitlines(True), new_text.splitlines(True),
                                                                      str(path), str(path) + " (new)", n=1))
    if dry_run:
        return True
    real.parent.mkdir(parents=True, exist_ok=True)
    prev = read_json(manifest_path()).get("files", {}).get(str(path)) or {}
    edited = bool(prev.get("user_edited") or (prev and existed and sha256(real) != prev.get("sha_after")))  # you changed it since
    backup = None
    if existed:
        stamp = time.strftime("%Y%m%d%H%M%S") + "-%06d" % (int(time.time() * 1e6) % 1000000)
        backup, n = real.with_name("%s.story-gate-backup-%s" % (real.name, stamp)), 0
        while backup.exists():  # never overwrite an earlier backup
            n += 1
            backup = real.with_name("%s.story-gate-backup-%s-%d" % (real.name, stamp, n))
        shutil.copy2(real, backup)
    tmp = real.with_name(real.name + ".tmp-%d" % os.getpid())
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, real)
    record("files", str(path), existed_before=existed, real_before=str(real), backup_before=str(backup) if backup else None,
           sha_after=sha256(real), user_edited=edited)
    return True


def restore_file(path, out, dry_run=False):
    """Put a file back exactly as it was before story-gate first touched it, if nobody has changed it since.
    If it was changed since (or the backup is gone), only story-gate's own entries are removed and your edits stay."""
    path = Path(path)
    e = read_json(manifest_path()).get("files", {}).get(str(path))
    if not e:
        return False
    real = Path(os.path.realpath(path))
    if e.get("real_before") and e["real_before"] != str(real):  # the link now points somewhere else: don't write into it
        out.append("  %s: now points to %s (it pointed to %s at install); not restored - remove the story-gate lines by hand"
                   % (path, real, e["real_before"]))
        return True
    if not e.get("real_before") and path.is_symlink():  # older install: link target not recorded, so fail closed
        out.append("  %s: is a link and this install didn't record its target; not restored - remove the story-gate lines by hand"
                   % path)
        return True
    backup = Path(e["backup_before"]) if e.get("backup_before") else None
    done = True
    if not real.exists():
        out.append("  %s: already gone" % path)
    elif sha256(real) == e.get("sha_after") and not e.get("user_edited") and (not e.get("existed_before") or (backup and backup.is_file())):
        if dry_run:
            out.append("  %s: would be restored exactly as before" % path)
            return True
        if e.get("existed_before"):
            shutil.copy2(backup, real)
            out.append("  %s: restored byte for byte from %s" % (path, backup))
        else:
            real.unlink()
            out.append("  %s: removed (story-gate created it)" % path)
    else:
        try:
            apply_file(path, removed_hook_json(real.read_text(encoding="utf-8")), dry_run, out)
            out.append("  %s: changed since install (or no backup), so only story-gate's entries were removed" % path)
        except (ValueError, TrustError) as ex:
            done = False
            out.append("  %s: NOT changed (%s) - remove the story-gate lines by hand" % (path, ex))
    if done and not dry_run:
        forget("files", str(path))
    return True


def register_user_hooks(py, gate, clients=USER_CLIENTS, dry_run=False):
    out, files, entries = [], user_hook_files(), hook_entries(py, gate)
    for cl in clients:
        p = files[cl]
        try:
            text = p.read_text(encoding="utf-8") if p.exists() else ""
            apply_file(p, merged_hook_json(text, entries[cl]), dry_run, out)
        except (ValueError, TrustError) as e:
            out.append("  %s: NOT changed - the file isn't plain JSON (%s). Add the story-gate hooks by hand (see README)." % (p, e))
    return out


def unregister_user_hooks(dry_run=False):
    out = []
    for cl, p in user_hook_files().items():
        if restore_file(p, out, dry_run):
            continue
        if p.exists():
            try:
                apply_file(p, removed_hook_json(p.read_text(encoding="utf-8")), dry_run, out)
            except (ValueError, TrustError) as e:
                out.append("  %s: NOT changed (%s)" % (p, e))
    return out


def registered_clients(gate):
    """Clients whose user-level hook file points at this runtime."""
    g = str(gate).replace("\\", "/")
    return [cl for cl, p in user_hook_files().items() if p.exists() and g in p.read_text(encoding="utf-8", errors="ignore").replace("\\\\", "/")]


LOCAL_ONLY = (".claude/settings.local.json",)  # your own per-computer file; it only counts when a branch commits it


def _branch_hook_files(top, hook_files):
    tracked = set(git_in(top, "ls-files", "--", *LOCAL_ONLY).splitlines())
    return [rel for rel in hook_files if rel not in LOCAL_ONLY or rel in tracked]


def _hook_commands(p):
    """Command strings under the `hooks` section of a hook file (JSON, or TOML where Python has tomllib).
    None when the file can't be parsed, so the caller can fail closed."""
    text = p.read_text(encoding="utf-8", errors="ignore")
    try:
        if p.suffix == ".toml":
            import tomllib
            data = tomllib.loads(text)
        else:
            data = json.loads(text) if text.strip() else {}
    except Exception:  # unparseable, or no tomllib (Python < 3.11)
        return None
    out = []

    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if k == "command" and isinstance(v, (str, list)):
                    out.append(v if isinstance(v, str) else " ".join(map(str, v)))
                else:
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(data.get("hooks") if isinstance(data, dict) else None)
    return out


def _runs_repo_gate(p):
    cmds = _hook_commands(p)
    if cmds is None:  # can't read it with certainty: treat as running repository code
        return ".story-gate/gate.py" in p.read_text(encoding="utf-8", errors="ignore").replace("\\", "/")
    return any(".story-gate/gate.py" in c.replace("\\", "/") for c in cmds)


def project_hook_findings(top, hook_files):
    """Project-level hook files whose hook commands still run story-gate code from the repository (a branch could swap it).
    Only commands under `hooks` count, so a permission rule such as Bash(python3 .story-gate/gate.py:*) is not a finding."""
    found = [rel for rel in _branch_hook_files(top, hook_files) if (Path(top) / rel).is_file() and _runs_repo_gate(Path(top) / rel)]
    hd = Path(top) / ".grok" / "hooks"
    if hd.is_dir():
        found += [str(f.relative_to(top)).replace("\\", "/") for f in hd.glob("*.json") if _runs_repo_gate(f)]
    return sorted(set(found))


def other_project_hooks(top, hook_files):
    """Commands from the repository's own project hook files (not story-gate's). story-gate can't vouch for them."""
    cmds = []
    paths = [Path(top) / r for r in _branch_hook_files(top, hook_files)] + (sorted((Path(top) / ".grok" / "hooks").glob("*.json")) if (Path(top) / ".grok" / "hooks").is_dir() else [])
    for p in paths:
        if not p.is_file() or p.suffix != ".json":
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        def walk(x):
            if isinstance(x, dict):
                if isinstance(x.get("command"), str) and not HOOK_SIGNATURE.search(x["command"]):
                    cmds.append((p.relative_to(top).as_posix(), x["command"]))
                for v in x.values():
                    walk(v)
            elif isinstance(x, list):
                for v in x:
                    walk(v)
        walk(data.get("hooks") if isinstance(data, dict) else None)
    return cmds


def remove_project_hooks(top, hook_files, dry_run=False):
    out = []
    for rel in project_hook_findings(top, hook_files):
        p = Path(top) / rel
        try:
            apply_file(p, removed_hook_json(p.read_text(encoding="utf-8")), dry_run, out)
        except (ValueError, TrustError) as e:
            out.append("  %s: NOT changed (%s) - remove the story-gate lines by hand" % (rel, e))
    return out
