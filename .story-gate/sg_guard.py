"""story-gate guard: stop hooks shipped inside a branch from running on your computer.

Three layers (panel decision sg-repohooks, chosen by the owner):
  1. Checkout filter (per enrolled repository, no admin, on by default with enrollment): a git filter set in the
     repository's own .git folder (never committed). Whenever git writes an AI-tool hook file, it writes the version
     the default branch approved, minus any command that isn't approved. A branch's hook changes never reach disk.
  2. Lockdown (optional, OFF by default, needs your explicit permission and admin rights once): turns on the AI tool's
     own hard switch where one exists. Today that is Claude Code's `allowManagedHooksOnly`. It is computer-wide, so
     it's explained in full before anything changes, and it can be undone.
  3. story-gate's own pre-edit block and the CI label rule (in gate.py) for everything else.

`gate.py doctor --prove` plants a harmless canary hook on a throwaway commit and shows that it never lands on disk.
"""
import hashlib, json, os, re, shutil, subprocess, sys, tempfile, time
from pathlib import Path

import sg_trust as T

FILTER = "storygate-hooks"
FILTERED = (".claude/settings.json", ".claude/settings.local.json", ".codex/hooks.json", ".codex/config.toml", ".cursor/hooks.json",
            ".gemini/settings.json", ".devin/hooks.json", ".windsurf/hooks.json", ".grok/hooks/*.json")
EXEC_KEYS = {"command", "apiKeyHelper", "awsAuthRefresh", "awsCredentialExport", "otelHeadersHelper"}  # strings an AI tool runs
ATTR_MARK = "# story-gate: AI-tool hook files are filtered so a branch can't add commands (gate.py enroll / unenroll)"
CANARY = "story-gate-canary-should-never-run"


# ------------------------------------------------------------------ sanitizing hook files
def exec_strings(data):
    """Every command-like string in a hook/settings JSON value."""
    out = set()
    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if k in EXEC_KEYS and isinstance(v, str):
                    out.add(v)
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(data)
    return out


def strip_unapproved(data, approved):
    """Remove every command-like entry whose command isn't approved. Returns (new_data, removed_commands)."""
    removed = []
    def walk(x):
        if isinstance(x, dict):
            out = {}
            for k, v in x.items():
                if k in EXEC_KEYS and isinstance(v, str) and v not in approved:
                    removed.append(v)
                    continue
                nv = walk(v)
                if isinstance(v, (dict, list)) and v and not nv:
                    continue  # a hook entry that is now empty disappears
                out[k] = nv
            if "command" in x and "command" not in out and set(out) <= {"type", "timeout", "matcher", "show_output", "failClosed"}:
                return {}  # an entry whose only purpose was the removed command
            return out
        if isinstance(x, list):
            return [w for w in (walk(v) for v in x) if w not in ({}, [])]
        return x
    return walk(data), removed


def sanitize(branch_bytes, approved_bytes, allowed=()):
    """What git should write for a hook file: the branch's version minus any command the default branch didn't approve.
    Unparseable or non-JSON content (e.g. .codex/config.toml) is replaced by the approved version."""
    if branch_bytes == approved_bytes:
        return branch_bytes, []
    try:
        data = json.loads(branch_bytes.decode("utf-8")) if branch_bytes.strip() else {}
    except (ValueError, UnicodeDecodeError):
        return (approved_bytes or b""), ["<file replaced by the approved version: not JSON>"]
    approved = set(allowed)
    if approved_bytes:
        try:
            approved |= exec_strings(json.loads(approved_bytes.decode("utf-8")))
        except (ValueError, UnicodeDecodeError):
            pass
    new, removed = strip_unapproved(data, approved)
    if not removed:
        return branch_bytes, []
    return (json.dumps(new, indent=2) + "\n").encode("utf-8"), removed


# ------------------------------------------------------------------ the git filter
def _git(top, *args, data=None):
    r = subprocess.run(["git", *args], cwd=top, input=data, capture_output=True)
    return r.stdout if r.returncode == 0 else None


def approved_for(top, path):
    e = T.enrollment(top) or {}
    ref = e.get("policy_ref")
    approved = _git(top, "show", "%s:%s" % (ref, path)) if ref else None
    allowed = []
    if ref:
        try:
            allowed = json.loads(T.policy_text(top, ref, "config.json") or "{}").get("project_hooks_allowed") or []
        except ValueError:
            pass
    return approved, [a for a in allowed if isinstance(a, str)]


def filter_main(mode, path):
    """git calls this (through the launcher) for every hook file it reads or writes. stdin -> stdout, never fails open."""
    data = sys.stdin.buffer.read()
    top = os.getcwd()
    try:
        approved, allowed = approved_for(top, path)
        if mode == "smudge":  # git is about to write `path` into the working tree
            out, removed = sanitize(data, approved, allowed)
            if removed:
                log(top, "filtered %s: removed %d unapproved command(s)" % (path, len(removed)))
                sys.stderr.write("story-gate: removed %d unapproved command(s) from %s (they came from this branch, not the default branch)\n"
                                 % (len(removed), path))
        else:  # clean: git is reading the working tree; map our filtered copy back to the stored version so status stays clean
            idx = _git(top, "cat-file", "blob", ":%s" % path) or _git(top, "show", "HEAD:%s" % path)
            out = idx if idx is not None and sanitize(idx, approved, allowed)[0] == data else data
            if out is data and idx is not None:  # you edited the filtered copy: say so if committing it would drop branch commands
                try:
                    lost = exec_strings(json.loads(idx.decode("utf-8"))) - exec_strings(json.loads(data.decode("utf-8") or "{}"))
                except (ValueError, UnicodeDecodeError):
                    lost = set()
                if lost:
                    sys.stderr.write("story-gate: %s on disk is the filtered copy, so committing it drops %d hook command(s) this branch "
                                     "added. Get new hook commands approved on the default branch first.\n" % (path, len(lost)))
    except Exception as ex:  # fail closed: never pass the branch's hook file through unchecked
        sys.stderr.write("story-gate filter error on %s: %r\n" % (path, ex))
        out = b"{}\n" if mode == "smudge" and path.endswith(".json") else (b"" if mode == "smudge" else data)
    sys.stdout.buffer.write(out)
    sys.stdout.flush()
    return 0


def log(top, line):
    try:
        p = Path(_git(top, "rev-parse", "--git-common-dir").decode().strip())
        p = p if p.is_absolute() else Path(top) / p
        with open(p / "story-gate-guard.log", "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), line))
    except Exception:
        pass


def info_attributes(top):
    raw = _git(str(top), "rev-parse", "--git-common-dir")
    if raw is None:
        raise T.TrustError("not inside a git repository")
    common = Path(raw.decode().strip())
    common = common if common.is_absolute() else Path(top) / common
    return common / "info" / "attributes"


def filter_conflicts(top):
    """Other attributes or filters already handling these paths (we never silently override them)."""
    out = []
    for p in FILTERED:
        probe = p.replace("*", "x")
        r = _git(top, "check-attr", "filter", "--", probe)
        val = (r or b"").decode().strip().rsplit(": ", 1)[-1]
        if val not in ("unspecified", FILTER, ""):
            out.append("%s already uses filter '%s'" % (p, val))
    return out


def enable_filter(top, py, launcher, dry_run=False):
    top = str(top)
    out = []
    conflicts = filter_conflicts(top)
    if conflicts:
        return False, ["Checkout filter NOT turned on: " + "; ".join(conflicts) + ". Remove that setting or ask for help; story-gate won't override it."]
    cmd = lambda mode: '"%s" -I "%s" hook-filter %s %%f' % (py.replace("\\", "/"), str(launcher).replace("\\", "/"), mode)  # git fills %f
    settings = {"filter.%s.smudge" % FILTER: cmd("smudge"), "filter.%s.clean" % FILTER: cmd("clean"), "filter.%s.required" % FILTER: "true"}
    attrs = info_attributes(top)
    lines = [ATTR_MARK] + ["%s filter=%s" % (p, FILTER) for p in FILTERED]
    old = attrs.read_bytes().decode("latin-1") if attrs.is_file() else ""  # latin-1: every byte round-trips exactly
    new = old if ATTR_MARK in old else (old + ("" if not old or old.endswith("\n") else "\n") + "\n".join(lines) + "\n")
    out.append("  %s: %s" % (attrs, "already set" if new == old else "adds %d lines (local to this computer, never committed)" % len(lines)))
    out += ["  git config --local %s = %s" % (k, v) for k, v in settings.items()]
    if dry_run:
        return True, out
    before = {k: (_git(top, "config", "--local", "--get", k) or b"").decode().strip() or None for k in settings}
    T.record("repos", os.path.normcase(os.path.realpath(top)), config_before=before,
             attributes_before=old if attrs.is_file() else None, attributes_path=str(attrs))
    attrs.parent.mkdir(parents=True, exist_ok=True)
    attrs.write_bytes(new.encode("latin-1"))
    for k, v in settings.items():
        _git(top, "config", "--local", k, v)
    out += rematerialize(top)
    return True, out


def tracked_hook_files(top):
    names = (_git(top, "ls-files", "-z", "--", *FILTERED) or b"").decode("utf-8", "replace").split("\0")
    return [n for n in names if n]


def rematerialize(top):
    """Re-write tracked hook files through the filter (skips files you have edited and not committed)."""
    out = []
    for n in tracked_hook_files(top):
        if _git(top, "diff", "--quiet", "--", n) is None:
            out.append("  %s: you have uncommitted edits here, so it wasn't re-checked. Commit or discard them, then run gate.py enroll again" % n)
            continue
        try:
            os.remove(os.path.join(top, n))
        except OSError:
            pass
        _git(top, "checkout", "--", n)
    return out


def disable_filter(top, dry_run=False):
    """Undo enable_filter exactly: attributes file and git config back to how they were, hook files re-checked out."""
    top = str(top)
    key = os.path.normcase(os.path.realpath(top))
    e = T.read_json(T.manifest_path()).get("repos", {}).get(key)
    out = []
    attrs = Path(e["attributes_path"]) if e and e.get("attributes_path") else info_attributes(top)
    if dry_run:
        return ["  would restore %s and the git filter settings, then re-check out the hook files" % attrs]
    if e is not None:
        if e.get("attributes_before") is not None:
            attrs.write_bytes(e["attributes_before"].encode("latin-1"))
        elif attrs.is_file():
            attrs.unlink()
        for k, v in (e.get("config_before") or {}).items():
            if v is None:
                _git(top, "config", "--local", "--unset-all", k)
            else:
                _git(top, "config", "--local", k, v)
        T.forget("repos", key)
    else:  # enabled before manifests existed: remove only our lines and keys
        if attrs.is_file():
            kept = [l for l in attrs.read_text(encoding="utf-8").splitlines() if l != ATTR_MARK and ("filter=%s" % FILTER) not in l]
            attrs.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        _git(top, "config", "--local", "--remove-section", "filter.%s" % FILTER)
    out.append("  %s and git filter settings restored" % attrs)
    out += rematerialize(top)
    return out


def filter_active(top):
    try:
        attrs = info_attributes(top)
    except T.TrustError:
        return False
    on = attrs.is_file() and ("filter=%s" % FILTER) in attrs.read_text(encoding="utf-8", errors="ignore")
    smudge = (_git(str(top), "config", "--local", "--get", "filter.%s.smudge" % FILTER) or b"").decode()
    return bool(on and "hook-filter smudge" in smudge)


# ------------------------------------------------------------------ proof
def prove(top):
    """Plant a canary hook in every filtered file on a throwaway commit, check it out in a throwaway worktree, and show
    whether the canary reached disk. Nothing in your branches or working tree changes."""
    top = str(top)
    lines, ok = [], True
    if not filter_active(top):
        return False, ["  checkout filter: OFF in this repository (gate.py enroll turns it on)"]
    canary = {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "echo %s" % CANARY}]}],
                        "stop": [{"command": "echo %s" % CANARY}]}, "apiKeyHelper": "echo %s" % CANARY}
    blob = (json.dumps(canary, indent=2) + "\n").encode()
    head = (_git(top, "rev-parse", "HEAD") or b"").decode().strip()
    if not head:
        return False, ["  can't prove: this repository has no commits yet"]
    work = Path(tempfile.mkdtemp(prefix="sg-prove-"))
    tmpidx = str(work / "index")
    who = {"GIT_AUTHOR_NAME": "story-gate", "GIT_AUTHOR_EMAIL": "story-gate@localhost", "GIT_COMMITTER_NAME": "story-gate",
           "GIT_COMMITTER_EMAIL": "story-gate@localhost"}
    env = dict(os.environ, GIT_INDEX_FILE=tmpidx, **who)
    def g(*a, data=None):
        r = subprocess.run(["git", *a], cwd=top, input=data, capture_output=True, env=env)
        return r.stdout.decode().strip() if r.returncode == 0 else None
    paths = [p.replace("*", "canary") for p in FILTERED if not p.endswith(".toml")]
    wt = work / "wt"
    try:
        g("read-tree", head)
        sha = g("hash-object", "-w", "--no-filters", "--stdin", data=blob)
        for p in paths:
            g("update-index", "--add", "--cacheinfo", "100644,%s,%s" % (sha, p))
        tree = g("write-tree")
        commit = g("commit-tree", tree, "-p", head, "-m", "story-gate canary (throwaway)")
        if not commit:
            return False, ["  can't prove: git couldn't create the throwaway commit"]
        (work / "nohooks").mkdir()
        r = subprocess.run(["git", "-c", "core.hooksPath=%s" % (work / "nohooks"), "worktree", "add", "--detach", str(wt), commit],
                           cwd=top, capture_output=True, text=True)
        if r.returncode != 0:
            return False, ["  can't prove: %s" % r.stderr.strip()[:200]]
        for p in paths:
            f = wt / p
            landed = f.is_file() and CANARY in f.read_text(encoding="utf-8", errors="ignore")
            ok &= not landed
            lines.append("  %-30s %s" % (p, "CANARY REACHED DISK - NOT protected" if landed else "canary removed before it reached disk"))
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=top, capture_output=True)
        subprocess.run(["git", "worktree", "prune"], cwd=top, capture_output=True)
        shutil.rmtree(work, ignore_errors=True)
    return ok, lines


# ------------------------------------------------------------------ lockdown (optional, admin, explicit consent)
# Claude Code merges managed-settings.json with every *.json in managed-settings.d/ (lists such as hooks combine), so
# story-gate adds ONE drop-in file of its own and never edits a managed-settings.json that IT may own.
# Source: https://code.claude.com/docs/en/managed-settings and https://code.claude.com/docs/en/hooks (October 2026).
LOCKDOWN_MARK = "storyGateLockdown"
DROPIN = "50-story-gate.json"


def claude_managed_dir():
    if os.environ.get("STORY_GATE_MANAGED_DIR"):  # tests, and IT teams staging files
        return Path(os.environ["STORY_GATE_MANAGED_DIR"]) / "claude"
    if sys.platform == "darwin":
        return Path("/Library/Application Support/ClaudeCode")
    if os.name == "nt":
        return Path(r"C:\Program Files\ClaudeCode")
    return Path("/etc/claude-code")


def claude_dropin():
    return claude_managed_dir() / "managed-settings.d" / DROPIN


def _user_claude_hooks(user_hooks_text):
    try:
        user = json.loads(user_hooks_text) if user_hooks_text.strip() else {}
    except ValueError:
        return {}
    hooks = {}
    for ev, entries in ((user.get("hooks") or {}) if isinstance(user, dict) else {}).items():
        kept = [e for e in (entries if isinstance(entries, list) else []) if not T.ours(e)]
        if kept:
            hooks[ev] = kept
    return hooks


def managed_body(py, launcher, user_hooks_text="", keep=()):
    """The drop-in file: the hard switch, story-gate's hooks, your own user-level Claude hooks (so they keep working),
    and any project hook command you chose to keep."""
    ours = T.hook_entries(py.replace("\\", "/"), str(launcher).replace("\\", "/"))["claude"]["hooks"]
    hooks = _user_claude_hooks(user_hooks_text)
    for ev, entries in ours.items():
        hooks.setdefault(ev, []).extend(entries)
    for cmd in keep:
        hooks.setdefault("PreToolUse", []).append({"matcher": "*", "hooks": [{"type": "command", "command": cmd}]})
    body = {LOCKDOWN_MARK: {"version": 1, "user_hooks_sha256": hashlib.sha256(json.dumps(_user_claude_hooks(user_hooks_text), sort_keys=True).encode()).hexdigest(),
                            "kept_project_hooks": list(keep)},
            "allowManagedHooksOnly": True, "hooks": hooks}
    return json.dumps(body, indent=2) + "\n"


def lockdown_status(user_hooks_text=None):
    p = claude_dropin()
    try:
        data = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None
    except (ValueError, OSError):
        data = None
    ours = bool(isinstance(data, dict) and data.get(LOCKDOWN_MARK))
    hooks = json.dumps((data or {}).get("hooks") or {}) if ours else ""
    on = bool(ours and data.get("allowManagedHooksOnly") is True and T.HOOK_SIGNATURE.search(hooks.replace('\\"', '"')))
    stale = False
    if on and user_hooks_text is not None:  # you changed your own Claude hooks since lockdown: they no longer run until refreshed
        now_sha = hashlib.sha256(json.dumps(_user_claude_hooks(user_hooks_text), sort_keys=True).encode()).hexdigest()
        stale = now_sha != data[LOCKDOWN_MARK].get("user_hooks_sha256")
    base = claude_managed_dir() / "managed-settings.json"
    return {"file": str(p), "exists": p.is_file(), "ours": ours, "on": on, "stale": stale,
            "name_taken": bool(p.is_file() and not ours), "it_managed_file": base.is_file()}


def explain(st=None):
    st = st or lockdown_status()
    now = "ON" if st["on"] else ("a file with story-gate's name exists but isn't story-gate's - not touched" if st["name_taken"] else "OFF")
    return """Lockdown is OFF by default. We recommend it, and it only turns on with your explicit permission.

What it does
  Claude Code has a switch, allowManagedHooksOnly, that IT teams use. When it's on, Claude Code ignores hooks from
  every repository and branch (and from your user settings) and runs only hooks in its system-wide settings.
  story-gate's lockdown file contains: the switch, story-gate's own hooks, and a copy of your own Claude hooks so they
  keep working.

Why
  The checkout filter already stops a branch's hook changes from reaching disk in the repositories story-gate covers.
  Lockdown is Claude Code's own hard switch: Claude Code won't run a repository's hooks however the file got there
  (a script, a download, a repository story-gate doesn't cover).

What changes on this computer
  One new file, owned by story-gate: %s
  Nothing else. An existing managed-settings.json (often your IT team's) is never edited; Claude Code merges both.
  Writing it needs admin rights once: story-gate prepares the files and shows you the one command to run.

What it affects
  Computer-wide in Claude Code: project hooks stop running in EVERY repository on this computer, not only the ones
  story-gate covers. To keep a project hook you rely on: gate.py lockdown --on --keep-project-hook "<its command>".
  If you change your own Claude hooks later, run gate.py lockdown --on again (doctor tells you when).
  If your company manages Claude Code through MDM or the Windows registry, those settings win over files: give the
  bundle (gate.py lockdown --bundle <folder>) to IT instead.
  Other tools: Codex re-asks you to trust any changed project hook. Cursor, Gemini, Windsurf and Grok have no such
  switch, so the checkout filter is their protection.

Proof
  gate.py doctor --prove plants a harmless canary hook on a throwaway commit, shows it never reaches disk, and shows
  whether lockdown is on.

Undo
  gate.py lockdown --off shows the one command that deletes story-gate's file. Nothing else needs restoring.

Status now: %s
""" % (st["file"], now)


def build_bundle(dest, py, launcher, user_hooks_text="", keep=()):
    """Write the lockdown drop-in plus install/uninstall scripts, an IT readme and checksums into `dest`.
    Nothing outside `dest` is touched."""
    dest = Path(dest)
    st = lockdown_status()
    if st["name_taken"]:
        raise T.TrustError("%s exists and wasn't written by story-gate. Rename or remove it first (or ask IT)." % st["file"])
    body = managed_body(py, launcher, user_hooks_text, keep)
    json.loads(body)  # Claude Code refuses to start on an unparseable managed file: never write one
    (dest / "claude").mkdir(parents=True, exist_ok=True)
    (dest / "claude" / DROPIN).write_bytes(body.encode("utf-8"))  # bytes: LF on every OS
    target = str(claude_dropin())
    (dest / "install.sh").write_bytes(("""#!/bin/sh
# story-gate lockdown for Claude Code (macOS / Linux / WSL). Run: sudo sh install.sh
# Adds ONE file; never edits managed-settings.json. Refuses to replace a file story-gate didn't write.
set -e
T="%s"
if [ -f "$T" ] && ! grep -q '"%s"' "$T"; then echo "Not changed: $T exists and isn't story-gate's."; exit 1; fi
mkdir -p "$(dirname "$T")"
cp "$(dirname "$0")/claude/%s" "$T"
chmod 644 "$T"
echo "story-gate lockdown is ON for Claude Code. Undo: sudo sh $(dirname "$0")/uninstall.sh"
""" % (target, LOCKDOWN_MARK, DROPIN)).encode("utf-8"))
    (dest / "uninstall.sh").write_bytes(("""#!/bin/sh
# Removes story-gate's lockdown file for Claude Code. Run: sudo sh uninstall.sh
set -e
T="%s"
if [ -f "$T" ] && grep -q '"%s"' "$T"; then rm -f "$T"; fi
rmdir "$(dirname "$T")" 2>/dev/null || true
echo "story-gate lockdown is OFF for Claude Code."
""" % (target, LOCKDOWN_MARK)).encode("utf-8"))
    (dest / "install.ps1").write_bytes(("""# story-gate lockdown for Claude Code (Windows). Run in PowerShell as Administrator.
# Adds ONE file; never edits managed-settings.json. Refuses to replace a file story-gate didn't write.
$ErrorActionPreference = "Stop"
$T = "%s"
if ((Test-Path $T) -and -not (Select-String -Path $T -SimpleMatch '"%s"' -Quiet)) { Write-Host "Not changed: $T exists and isn't story-gate's."; exit 1 }
New-Item -ItemType Directory -Force -Path (Split-Path $T) | Out-Null
Copy-Item (Join-Path $PSScriptRoot "claude\\%s") $T -Force
Write-Host "story-gate lockdown is ON for Claude Code. Undo: run uninstall.ps1 as Administrator"
""" % (target, LOCKDOWN_MARK, DROPIN)).encode("utf-8"))
    (dest / "uninstall.ps1").write_bytes(("""# Removes story-gate's lockdown file for Claude Code. Run in PowerShell as Administrator.
$T = "%s"
if ((Test-Path $T) -and (Select-String -Path $T -SimpleMatch '"%s"' -Quiet)) { Remove-Item $T -Force }
Write-Host "story-gate lockdown is OFF for Claude Code."
""" % (target, LOCKDOWN_MARK)).encode("utf-8"))
    (dest / "README-IT.md").write_bytes(("""# story-gate lockdown: files for IT

Turns on Claude Code's `allowManagedHooksOnly` switch with one drop-in file, so Claude Code runs only managed hooks and
ignores hooks shipped inside repositories. Claude Code merges `managed-settings.json` with every `*.json` file in
`managed-settings.d/` (hook lists combine), so this never edits a `managed-settings.json` you already deploy.
Source: https://code.claude.com/docs/en/managed-settings

| Platform | Deploy `claude/%s` to |
|---|---|
| macOS (Jamf, Kandji, ...) | `/Library/Application Support/ClaudeCode/managed-settings.d/%s` |
| Linux / WSL | `/etc/claude-code/managed-settings.d/%s` |
| Windows (Intune, GPO, ...) | `C:\\\\Program Files\\\\ClaudeCode\\\\managed-settings.d\\\\%s` |

If you deliver Claude Code settings through MDM profiles or the HKLM registry, those take precedence over files: put
the same keys (`allowManagedHooksOnly`, `hooks`) in that channel instead.

The hooks call the story-gate runtime each person installs with `gate.py install --user` (it checks its own signature
and fingerprint). The path in this file is for the person who generated it (%s). For a fleet, set STORY_GATE_HOME to
the same folder for everyone, or generate one file per person with `gate.py lockdown --bundle <folder>`.
The file also carries a copy of that person's own Claude hooks, because user-level hooks stop running under this switch.

Check the files against SHA256SUMS. Undo: delete the one file (uninstall.sh / uninstall.ps1).
""" % (DROPIN, DROPIN, DROPIN, DROPIN, str(launcher).replace("\\", "/"))).encode("utf-8"))
    sums = []
    for f in sorted(p for p in dest.rglob("*") if p.is_file() and p.name != "SHA256SUMS"):
        sums.append("%s  %s" % (hashlib.sha256(f.read_bytes()).hexdigest(), f.relative_to(dest).as_posix()))
    (dest / "SHA256SUMS").write_bytes(("\n".join(sums) + "\n").encode("utf-8"))
    return dest, body


def client_matrix(top=None):
    """Honest per-tool status for hooks shipped inside repositories: hard / hard-on-change / partial / none."""
    st = lockdown_status()
    filt = bool(top) and filter_active(top)
    soft = "partial: checkout filter" if filt else "none"
    return [
        ("claude", "hard: lockdown (allowManagedHooksOnly)" if st["on"] else soft, "" if st["on"] else "turn on lockdown for a hard stop"),
        ("codex", "hard-on-change: Codex re-asks you to trust any changed project hook" + (" + checkout filter" if filt else ""), ""),
        ("cursor", soft, "no hard switch in Cursor yet"),
        ("gemini", soft, "turn on Gemini folder trust (security.folderTrust.enabled) for untrusted folders"),
        ("windsurf", soft, "no hard switch documented"),
        ("grok", soft, "trust folders only with /hooks-trust when you mean it"),
    ]
