"""Running pull request code in CI as a separate, throwaway user (Linux runners only).

Why: in the CI jobs that run a pull request's code (tests, scenarios, budgets), that code used to run as the same user
as story-gate. A process it left behind could rewrite story-gate's result files before they were uploaded, and CI
would then report a forged result as checked. Here the pull request's code runs as a new account with no admin rights,
no groups and an allowlisted environment; story-gate's files stay with the runner's own account, which that code can't
write; and after each run every process of that account is stopped, and the stop verified, before results are read.

What "checked" means with this: CI ran the default branch's command on this code, and nothing changed the result
afterwards. It doesn't make a test that lies about itself honest.

Repository layout while isolated: the checkout's files belong to the throwaway account; the top folder and `.git` stay
with the runner's account (the top folder is group-writable and sticky, so the account can add files there but can't
replace `.git`, and story-gate's own git commands never read configuration that code wrote).

Where isolation can't be set up (not Linux, no root and no password-free sudo), `available()` says why. With
`ci_isolation: "strict"` (the default) CI then runs nothing and says so; with "relaxed" it runs as before and every
result counts as reported, not checked. Stdlib only.
"""
import hashlib, os, posixpath, re, secrets, shutil, socket, stat, subprocess, time

NAME_PREFIX = "sgpr-"
KILL_WAIT = 15            # seconds to wait for the account's processes to end before failing closed
ADMIN_GROUPS = {"sudo", "admin", "adm", "wheel", "docker", "root", "lxd", "libvirt", "kvm", "disk", "systemd-journal"}
# Environment the pull request's code gets: toolchain locations and plain CI facts, never tokens or credentials.
# Exact names (no open-ended families), as GitHub's ubuntu runner image sets them.
ENV_ALLOW = re.compile(r"^(?:PATH|LANG|LANGUAGE|LC_[A-Z]+|TZ|TERM|CI|DEBIAN_FRONTEND|RUNNER_(?:OS|ARCH|TOOL_CACHE)|"
                       r"GITHUB_(?:REPOSITORY|REPOSITORY_OWNER|SHA|REF|REF_NAME|HEAD_REF|BASE_REF|EVENT_NAME|SERVER_URL|RUN_ID|"
                       r"RUN_NUMBER|RUN_ATTEMPT|WORKFLOW|JOB|ACTIONS)|JAVA_HOME(?:_\d+_(?:X64|ARM64))?|GOROOT(?:_\d+_\d+_(?:X64|ARM64))?|"
                       r"ANDROID_(?:HOME|SDK_ROOT|NDK|NDK_HOME|NDK_ROOT|NDK_LATEST_HOME)|DOTNET_(?:ROOT|NOLOGO|MULTILEVEL_LOOKUP|"
                       r"SKIP_FIRST_TIME_EXPERIENCE)|CONDA|PIPX_(?:HOME|BIN_DIR)|NVM_DIR|CHROME_BIN|CHROMEWEBDRIVER|EDGEWEBDRIVER|"
                       r"GECKOWEBDRIVER|SELENIUM_JAR_PATH|BOOTSTRAP_HASKELL_NONINTERACTIVE|GHCUP_INSTALL_BASE_PREFIX|ACCEPT_EULA|"
                       r"POWERSHELL_DISTRIBUTION_CHANNEL|VCPKG_INSTALLATION_ROOT|SWIFT_PATH|LEIN_HOME|LEIN_JAR|M2_HOME|GRADLE_HOME|"
                       r"ANT_HOME|PLAYWRIGHT_BROWSERS_PATH|AGENT_TOOLSDIRECTORY)$")
SECRET_NAME = re.compile(r"(?i)token|secret|password|passwd|credential|key|auth|cookie|session|private|api|cert|conn")


class IsolationError(RuntimeError):
    """Isolation couldn't be set up or verified; the caller fails closed (or, with relaxed, reports instead of checks)."""


def _priv():
    """The prefix that runs a command as root here: nothing when we are root, else password-free sudo."""
    return [] if os.geteuid() == 0 else ["sudo", "-n"]


DIAG_TIMEOUT = 5          # seconds each probe may take when an administrative command timed out
DIAG_CAP = 2000           # characters of those probes' findings added to the error
LOCK_FILES = ("/etc/passwd.lock", "/etc/shadow.lock", "/etc/group.lock", "/etc/gshadow.lock", "/etc/.pwd.lock")
DIAG_PROCS = 8            # processes listed, so the probes after them always fit in DIAG_CAP
DIAG_PROGRAMS = re.compile(r"useradd|passwd|nscd|sssd\w*|systemd-userdbd|sudo")


def _probe(argv):
    """One diagnostic command (no shell, own short timeout): (exit code, output), or (None, why it gave nothing)."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=DIAG_TIMEOUT)
    except subprocess.TimeoutExpired:
        return None, "timed out after %s s" % DIAG_TIMEOUT
    except (OSError, subprocess.SubprocessError) as e:
        return None, "couldn't run (%s)" % type(e).__name__
    return r.returncode, (r.stdout or "")


def _program(cmd):
    """The program a process runs (for sudo, also the program sudo runs), never its arguments: those can hold credentials."""
    words = cmd.split()
    name = posixpath.basename(words[0])
    target = next((w for w in words[1:] if not w.startswith("-")), None) if name == "sudo" else None
    return name + (" " + posixpath.basename(target) if target else "")


def _timeout_diagnostics():
    """Why an administrative command may have stalled, in one bounded line: lock files of the account database, the
    processes that touch it, how long sudo takes, whether this host's name resolves (slow sudo when it doesn't), and
    the name-service caches. Each probe has its own DIAG_TIMEOUT; never environment variables."""
    found = ["lock files present: %s" % (", ".join(p for p in LOCK_FILES if os.path.exists(p)) or "none")]
    code, out = _probe(["ps", "-eo", "pid,etime,stat,cmd"])
    if code is None:
        found.append("ps %s" % out)
    else:
        procs = []
        for line in out.splitlines()[1:]:
            f = line.split(None, 3)
            if len(f) == 4 and DIAG_PROGRAMS.fullmatch(posixpath.basename(f[3].split()[0])):
                procs.append(" ".join(f[:3] + [_program(f[3])])[:160])
        more = " | (%d more)" % (len(procs) - DIAG_PROCS) if len(procs) > DIAG_PROCS else ""
        found.append("processes: %s%s" % (" | ".join(procs[:DIAG_PROCS]) or "none", more))
    if shutil.which("sudo"):
        start = time.monotonic()
        code, out = _probe(["sudo", "-n", "true"])
        found.append("sudo -n true: %s" % (out if code is None else "exit %d in %.2f s" % (code, time.monotonic() - start)))
    host = socket.gethostname()
    code, out = _probe(["getent", "hosts", host])
    found.append("getent hosts %s: %s" % (host, out if code is None else "answers" if code == 0 and out.strip() else "no answer (exit %d)" % code))
    if shutil.which("systemctl"):
        code, out = _probe(["systemctl", "is-active", "nscd", "sssd"])
        states = ", ".join("%s %s" % kv for kv in zip(("nscd", "sssd"), out.split())) if code is not None else ""
        found.append("systemctl is-active: %s" % (out if code is None else states or "no answer (exit %d)" % code))
    text = "; ".join(found)
    return text if len(text) <= DIAG_CAP else text[:DIAG_CAP - 3] + "..."


def _run(argv, timeout=120, check=True):
    """Run an administrative command (no shell) and return it; IsolationError when it fails and check is set. When it
    times out, the error ends with a short, bounded diagnosis of what may have stalled it."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        msg = "%s couldn't run (%s)" % (argv[len(_priv())] if len(argv) > len(_priv()) else argv[0], e)
        if isinstance(e, subprocess.TimeoutExpired):
            try:
                msg += "; diagnostics: " + _timeout_diagnostics()
            except Exception as e2:  # never let the diagnosis replace the timeout itself
                msg += "; diagnostics failed (%s)" % type(e2).__name__
        raise IsolationError(msg)
    if check and r.returncode != 0:
        raise IsolationError("%s failed: %s" % (" ".join(argv[len(_priv()):][:3]), (r.stderr or r.stdout).strip()[:200]))
    return r


def available():
    """"" when this machine can run code as a separate account (Linux, root or password-free sudo), else why not."""
    if not hasattr(os, "geteuid") or not os.uname().sysname == "Linux":
        return "this isn't a Linux runner"
    for tool in ("useradd", "userdel", "pkill", "pgrep", "chown", "setpriv"):
        if not shutil.which(tool) and not os.path.exists("/usr/sbin/" + tool):
            return "the %s program isn't installed" % tool
    if os.geteuid() != 0:
        if not shutil.which("sudo"):
            return "sudo isn't installed and story-gate isn't running as root"
        r = subprocess.run(["sudo", "-n", "true"], capture_output=True, timeout=30)
        if r.returncode != 0:
            return "the runner's account can't use sudo without a password"
    return ""


def env_for(home, env=None):
    """The allowlisted environment for the account: toolchain paths and plain CI facts; HOME and TMPDIR are its own
    (Linux paths, so built with posixpath on every platform)."""
    env = os.environ if env is None else env
    out = {k: v for k, v in env.items() if ENV_ALLOW.match(k) and not SECRET_NAME.search(k)}
    out.update({"HOME": home, "TMPDIR": posixpath.join(home, "tmp"), "USER": posixpath.basename(home), "LOGNAME": posixpath.basename(home)})
    return out


def _ancestors(path):
    """Every folder from / down to path's parent."""
    out, p = [], os.path.dirname(os.path.abspath(path))
    while True:
        out.append(p)
        if p == os.path.dirname(p):
            return out[::-1]
        p = os.path.dirname(p)


class Account:
    """One throwaway account for one CI job. setup() before running pull request code; stop() after each run;
    teardown() at the end. Every step is verified; anything unexpected raises IsolationError."""

    def __init__(self, root, protect):
        """root: the checkout. protect: folders the account must never be able to write (results, trusted runtime)."""
        self.root, self.protect = os.path.abspath(root), [os.path.abspath(p) for p in protect]
        self.name = NAME_PREFIX + secrets.token_hex(4)
        self.uid = self.gid = None
        self.home = "/home/" + self.name
        import pwd  # Unix only: imported here so the module (read_regular, available) loads on Windows too
        self.runner = pwd.getpwuid(os.getuid()).pw_name
        self.opened, self.root_mode = [], None  # folders given o+x, and the checkout's own mode: put back at the end

    # ---------------------------------------------------------------- setting up
    def setup(self):
        """Create the account, hand it the checkout (not `.git`), and verify it can't write what it must not. When any
        step fails, everything done so far is undone (the checkout given back, the account removed) before it raises."""
        try:
            return self._setup()
        except Exception as e:
            if self.uid is not None:
                try:
                    left = self.teardown()
                except IsolationError as e2:
                    left = [str(e2)]
                if left:  # say so: on a runner that is kept, the account or its processes would stay
                    raise IsolationError("%s; undoing the set-up didn't finish either: %s" % (e, "; ".join(left)))
            raise

    def _setup(self):
        """setup()'s steps; see there."""
        import grp, pwd
        P = _priv()
        _run(P + ["useradd", "--create-home", "--user-group", "--no-log-init", "--shell", "/bin/sh", self.name])
        pw = pwd.getpwnam(self.name)
        self.uid, self.gid, self.home = pw.pw_uid, pw.pw_gid, pw.pw_dir
        groups = {g.gr_name for g in grp.getgrall() if self.name in g.gr_mem} | {grp.getgrgid(self.gid).gr_name}
        if groups != {self.name}:
            raise IsolationError("the new account %s is in other groups (%s)" % (self.name, ", ".join(sorted(groups - {self.name}))))
        if shutil.which("sudo") and "may run" in _run(P + ["sudo", "-n", "-l", "-U", self.name], check=False).stdout:
            raise IsolationError("the new account %s could use sudo" % self.name)  # and no_new_privs below stops it anyway
        _run(P + ["mkdir", "-p", os.path.join(self.home, "tmp")])
        _run(P + ["chown", "-R", "%d:%d" % (self.uid, self.gid), self.home])
        for d in _ancestors(self.root):  # it may pass through the folders above the checkout, never list or write them
            if not os.stat(d).st_mode & 0o001:
                _run(P + ["chmod", "o+x", d])
                self.opened.append(d)
        for d in self.protect:
            os.makedirs(d, exist_ok=True)
            os.chmod(d, 0o700)
        # The checkout: everything but `.git` becomes the account's; the top folder stays ours, group-writable and sticky.
        for entry in os.listdir(self.root):
            if entry != ".git":
                _run(P + ["chown", "-hR", "%d:%d" % (self.uid, self.gid), os.path.join(self.root, entry)])
        self.root_mode = os.stat(self.root).st_mode & 0o7777
        _run(P + ["chown", "%d:%d" % (os.getuid(), self.gid), self.root])
        _run(P + ["chmod", "1775", self.root])
        for d in self.protect + [os.path.join(self.root, ".git")]:  # verified, not assumed
            for p in [d] + _ancestors(d):
                if p != d and os.stat(p).st_mode & 0o1000:
                    continue  # a sticky folder (the checkout's top, /tmp): it can't rename or remove what isn't its own
                if self.can_write(p):
                    raise IsolationError("the account %s could write %s" % (self.name, p))
        return self

    def can_write(self, path):
        """True when the account could create, change or remove something in path."""
        r = subprocess.run(_priv() + ["setpriv", "--reuid=%d" % self.uid, "--regid=%d" % self.gid, "--clear-groups",
                                      "--no-new-privs", "--", "/usr/bin/test", "-w", path], capture_output=True, timeout=30)
        return r.returncode == 0

    # ---------------------------------------------------------------- running and stopping
    def wrap(self, argv, max_file=None):
        """argv run as the account, with only the allowlisted environment (and a file-size limit in 512-byte blocks)."""
        env = ["%s=%s" % kv for kv in sorted(env_for(self.home).items())]
        inner = ["/bin/sh", "-c", 'ulimit -f %d && exec "$@"' % max_file, "sh"] if max_file else []
        return _priv() + ["setpriv", "--reuid=%d" % self.uid, "--regid=%d" % self.gid, "--clear-groups", "--no-new-privs",
                          "--", "/usr/bin/env", "-i"] + env + inner + list(argv)

    def stop(self):
        """Stop every process of the account and verify none is left (so nothing can write after we read)."""
        end = time.monotonic() + KILL_WAIT
        while True:
            _run(_priv() + ["pkill", "-KILL", "-U", str(self.uid)], check=False)
            left = _run(["pgrep", "-U", str(self.uid)], check=False).stdout.split()
            if not left:
                return
            if time.monotonic() > end:
                raise IsolationError("%d process(es) of %s were still running after %d seconds" % (len(left), self.name, KILL_WAIT))
            time.sleep(0.2)

    def teardown(self):
        """Stop everything, give the checkout back to the runner's account and remove the throwaway account.
        Every step is tried even when an earlier one fails (a kept runner must get its checkout and folders back).
        Returns warnings (plain words) for clean-up that didn't finish; results were already read after verified stops,
        so these don't change them. IsolationError, after the clean-up, when the account's processes can't be stopped."""
        try:
            self.stop()
            stopped = None
        except IsolationError as e:
            stopped = e
        P = _priv()
        warn = []
        steps = [(["chown", "-hR", "%d:%d" % (os.getuid(), os.getgid()), self.root], "couldn't give the checkout back to the runner's account"),
                 (["chmod", "%o" % (self.root_mode if self.root_mode is not None else 0o755), self.root], "couldn't restore the checkout folder's permissions")]
        steps += [(["chmod", "o-x", d], "couldn't close %s again" % d) for d in reversed(self.opened)]  # a kept runner keeps its folders
        for argv, problem in steps:
            try:
                ok = _run(P + argv, check=False).returncode == 0
            except IsolationError:
                ok = False
            if not ok:
                warn.append(problem)
        try:
            r = _run(P + ["userdel", "--remove", self.name], check=False)
            why = "" if r.returncode == 0 else (r.stderr or "").strip()[:120]
        except IsolationError as e:
            why = str(e)
        if why:  # on a runner that is kept, its home could hold what the code cached: say so, loudly
            warn.append("couldn't remove the throwaway account %s and its home folder; remove them by hand (%s)" % (self.name, why))
        if stopped is not None:
            raise IsolationError("; ".join([str(stopped)] + warn))
        return warn


def _open_regular(path):
    """A read-only descriptor for a regular file, never through a symbolic link; None when it isn't one."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    try:
        if stat.S_ISREG(os.fstat(fd).st_mode):
            return fd
    except OSError:
        pass
    os.close(fd)
    return None


def read_regular(path, cap):
    """The bytes of a regular file (never through a symbolic link), at most cap+1 of them; None when it isn't one."""
    fd = _open_regular(path)
    if fd is None:
        return None
    with os.fdopen(fd, "rb") as f:
        return f.read(cap + 1)


CHUNK = 1 << 20


def sha256_regular(path):
    """SHA-256 of a regular file's whole content (never through a symbolic link), read in chunks so any size fits in
    memory; None when it isn't a regular file or can't be read to the end."""
    fd = _open_regular(path)
    if fd is None:
        return None
    h = hashlib.sha256()
    try:
        with os.fdopen(fd, "rb") as f:
            for block in iter(lambda: f.read(CHUNK), b""):
                h.update(block)
    except OSError:
        return None
    return h.digest()
