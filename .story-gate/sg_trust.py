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
HOOK_SIGNATURE = re.compile(r'gate\.py"?\s+hook\s+--client')  # every story-gate hook command, repo or runtime
USER_CLIENTS = ("claude", "codex", "cursor", "gemini", "windsurf")
DEGRADED_CLIENTS = ("grok",)  # user-level location documented, but merging with project hooks is unverified; see docs/client-security.md


class _LazyGitHub:  # sg_github pulls in http/xml modules; load it only when needed so hooks stay fast
    def __getattr__(self, name):
        """Load sg_github on first attribute access and delegate the lookup."""
        import sg_github
        return getattr(sg_github, name)


G = _LazyGitHub()


class TrustError(Exception):
    pass


# ------------------------------------------------------------------ locations
def user_home():
    """Return the client settings home, honoring STORY_GATE_USER_HOME."""
    return Path(os.environ.get("STORY_GATE_USER_HOME") or Path.home())


def runtime_root():
    """Return the directory containing installed runtime versions."""
    return G.config_dir() / "runtime"


def enrolled_path():
    """Return the path to the repository enrollment registry."""
    return G.config_dir() / "enrolled.json"


def active_path():
    """Return the path to the active runtime metadata."""
    return runtime_root() / "active.json"


def read_json(p, default=None):
    """Read JSON, returning the supplied default or an empty dict on any read or parse error."""
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return {} if default is None else default


def write_json_atomic(p, obj):
    """Write JSON through a sibling temporary file and atomically replace the destination."""
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
    """Return the hexadecimal SHA-256 digest of a file."""
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def runtime_files(src):
    """Return sorted relative runtime file paths, excluding symlinks and bytecode caches."""
    src = Path(src)
    out = set()
    for g in RUNTIME_GLOBS:
        for f in src.glob(g):
            if f.is_file() and not f.is_symlink() and "__pycache__" not in f.parts:
                out.add(f.relative_to(src).as_posix())
    return sorted(out)


def make_manifest(src, version):
    """Build a versioned manifest of runtime files and their SHA-256 digests."""
    return {"version": version, "files": {f: sha256(Path(src) / f) for f in runtime_files(src)}}


def ssh_keygen():
    """Find ssh-keygen on PATH or in Windows install locations; raise TrustError if absent."""
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
        """Resolve an archive member inside dest, raising TrustError if it escapes."""
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
    prev = read_json(active_path()).get("dir")
    write_json_atomic(active_path(), {"version": version, "dir": str(dest), "previous": prev, "signed": bool(signed),
                                      "manifest_sha256": sha256(dest / "manifest.json")})
    return dest


def verify_self(script_dir):
    """The running runtime must match its install-time manifest and be the active one. Returns a list of problems."""
    sd = Path(script_dir).resolve()
    problems = []
    act = read_json(active_path())
    if not act or Path(act.get("dir", "")).resolve() != sd:
        problems.append("this runtime is not the active story-gate runtime (%s)" % sd)
    mp = sd / "manifest.json"
    if act.get("manifest_sha256") and mp.is_file() and sha256(mp) != act["manifest_sha256"]:
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


def version_tuple(v):
    """Return up to three numeric version components, or (0,) when none exist."""
    return tuple(int(x) for x in re.findall(r"\d+", str(v))[:3]) or (0,)


# ------------------------------------------------------------------ repositories: enrollment and policy
def git_in(cwd, *args):
    """Run Git in cwd and return stripped stdout, or an empty string on failure."""
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.stdout.strip() if r.returncode == 0 else ""


def repo_identity(cwd):
    """(toplevel, identity) for the repository containing cwd. Worktrees share one identity (the common git dir)."""
    top = git_in(cwd, "rev-parse", "--show-toplevel")
    if not top:
        return None, None
    common = git_in(cwd, "rev-parse", "--git-common-dir")
    common = os.path.realpath(os.path.join(top, common)) if common and not os.path.isabs(common) else os.path.realpath(common or top)
    return os.path.realpath(top), os.path.normcase(common)


def default_policy_ref(cwd, base_branch="main"):
    """Find the origin default branch or an available main/master policy fallback."""
    head = git_in(cwd, "symbolic-ref", "-q", "refs/remotes/origin/HEAD")
    if head:
        return head.replace("refs/remotes/", "", 1)
    for ref in ("origin/" + base_branch, base_branch, "origin/master", "master"):
        if git_in(cwd, "rev-parse", "--verify", "-q", ref + "^{commit}"):
            return ref
    return None


def enrollment(cwd):
    """Return repository identity and enrollment data, or None outside a repository."""
    top, ident = repo_identity(cwd)
    if not ident:
        return None
    rec = read_json(enrolled_path()).get(ident)
    return dict(rec, toplevel=top, identity=ident) if rec else {"toplevel": top, "identity": ident, "enrolled": False}


def enroll(cwd, policy_ref=None):
    """Persist enrollment with an explicit or default policy ref and return the record."""
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
    """Remove the repository enrollment and return whether a record was deleted."""
    _, ident = repo_identity(cwd)
    data = read_json(enrolled_path())
    if ident in data:
        del data[ident]
        write_json_atomic(enrolled_path(), data)
        return True
    return False


def policy_text(top, ref, name):
    """Contents of .story-gate/<name> on the policy ref, pinned to the commit it resolves to right now. None if absent."""
    sha = git_in(top, "rev-parse", "--verify", "-q", ref + "^{commit}")
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
    """Map supported clients to their user-level hook configuration paths."""
    h = user_home()
    return {"claude": h / ".claude" / "settings.json", "codex": h / ".codex" / "hooks.json",
            "cursor": h / ".cursor" / "hooks.json", "gemini": h / ".gemini" / "settings.json",
            "windsurf": h / ".codeium" / "windsurf" / "hooks.json"}


def user_protected_paths():
    """Places an agent must never write: the runtime, enrollment, agent key, and the user-level hook files."""
    return [G.config_dir()] + list(user_hook_files().values())


def hook_entries(py, gate):
    """Build client hook configurations invoking the supplied gate with isolated Python."""
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
    """Return whether a serialized hook entry contains a story-gate hook command."""
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
    """Remove story-gate hook entries and empty events, returning formatted JSON."""
    data = json.loads(text) if text.strip() else {}
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if isinstance(hooks, dict):
        for ev in list(hooks):
            if isinstance(hooks[ev], list):
                hooks[ev] = [e for e in hooks[ev] if not ours(e)]
                if not hooks[ev]:
                    del hooks[ev]
    return json.dumps(data, indent=2) + "\n"


def apply_file(path, new_text, dry_run, out):
    """Write with a timestamped backup; print a diff; never clobber a file we couldn't parse."""
    path = Path(path)
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    if old == new_text:
        out.append("  %s: already up to date" % path)
        return False
    json.loads(new_text)  # round-trip check before anything touches disk
    out.extend("    " + l.rstrip("\n") for l in difflib.unified_diff(old.splitlines(True), new_text.splitlines(True),
                                                                      str(path), str(path) + " (new)", n=1))
    if dry_run:
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".story-gate-backup-" + time.strftime("%Y%m%d%H%M%S")))
    tmp = path.with_name(path.name + ".tmp-%d" % os.getpid())
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, path)
    return True


def register_user_hooks(py, gate, clients=USER_CLIENTS, dry_run=False):
    """Merge runtime hooks into selected user files and return change or error messages."""
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
    """Remove runtime hooks from user files, honoring dry_run, and return messages."""
    out = []
    for cl, p in user_hook_files().items():
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


def project_hook_findings(top, hook_files):
    """Project-level hook files that still run story-gate code from the repository (a branch could swap it)."""
    found = []
    for rel in hook_files:
        p = Path(top) / rel
        if p.is_file() and ".story-gate/gate.py" in p.read_text(encoding="utf-8", errors="ignore").replace("\\", "/"):
            found.append(rel)
    hd = Path(top) / ".grok" / "hooks"
    if hd.is_dir():
        found += [str(f.relative_to(top)).replace("\\", "/") for f in hd.glob("*.json")
                  if ".story-gate/gate.py" in f.read_text(encoding="utf-8", errors="ignore").replace("\\", "/")]
    return sorted(set(found))


def other_project_hooks(top, hook_files):
    """Commands from the repository's own project hook files (not story-gate's). story-gate can't vouch for them."""
    cmds = []
    paths = [Path(top) / r for r in hook_files] + (sorted((Path(top) / ".grok" / "hooks").glob("*.json")) if (Path(top) / ".grok" / "hooks").is_dir() else [])
    for p in paths:
        if not p.is_file() or p.suffix != ".json":
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        def walk(x):
            """Collect non-story-gate commands recursively from the current hook file."""
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
    """Remove repository story-gate hooks, honoring dry_run, and return change messages."""
    out = []
    for rel in project_hook_findings(top, hook_files):
        p = Path(top) / rel
        try:
            apply_file(p, removed_hook_json(p.read_text(encoding="utf-8")), dry_run, out)
        except (ValueError, TrustError) as e:
            out.append("  %s: NOT changed (%s) - remove the story-gate lines by hand" % (rel, e))
    return out
