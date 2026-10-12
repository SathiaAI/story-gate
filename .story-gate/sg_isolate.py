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
import grp, os, pwd, re, secrets, shutil, subprocess, time

NAME_PREFIX = "sgpr-"
KILL_WAIT = 15            # seconds to wait for the account's processes to end before failing closed
ADMIN_GROUPS = {"sudo", "admin", "adm", "wheel", "docker", "root", "lxd", "libvirt", "kvm", "disk", "systemd-journal"}
# Environment the pull request's code gets: toolchain locations and plain CI facts, never tokens or credentials.
ENV_ALLOW = re.compile(r"^(?:PATH|LANG|LANGUAGE|LC_[A-Z]+|TZ|TERM|CI|DEBIAN_FRONTEND|RUNNER_(?:OS|ARCH|TOOL_CACHE|NAME|ENVIRONMENT)|"
                       r"GITHUB_(?:REPOSITORY|REPOSITORY_OWNER|SHA|REF|REF_NAME|HEAD_REF|BASE_REF|EVENT_NAME|SERVER_URL|RUN_ID|"
                       r"RUN_NUMBER|RUN_ATTEMPT|WORKFLOW|JOB|ACTIONS)|JAVA_HOME(?:_\w+)?|GOROOT(?:_\w+)?|ANDROID_\w+|DOTNET_\w+|"
                       r"CONDA|PIPX_\w+|NVM_DIR|CHROME_BIN|CHROMEWEBDRIVER|EDGEWEBDRIVER|GECKOWEBDRIVER|SELENIUM_JAR_PATH|"
                       r"BOOTSTRAP_HASKELL_NONINTERACTIVE|GHCUP_INSTALL_BASE_PREFIX|ACCEPT_EULA|POWERSHELL_\w+|VCPKG_\w+|"
                       r"SWIFT_PATH|LEIN_\w+|M2_HOME|GRADLE_HOME|ANT_HOME|CHROMEDRIVER_\w+|PLAYWRIGHT_\w+|AGENT_TOOLSDIRECTORY)$")
SECRET_NAME = re.compile(r"(?i)token|secret|password|passwd|credential|key|auth|cookie|session|private")


class IsolationError(RuntimeError):
    """Isolation couldn't be set up or verified; the caller fails closed (or, with relaxed, reports instead of checks)."""


def _priv():
    """The prefix that runs a command as root here: nothing when we are root, else password-free sudo."""
    return [] if os.geteuid() == 0 else ["sudo", "-n"]


def _run(argv, timeout=120, check=True):
    """Run an administrative command (no shell) and return it; IsolationError when it fails and check is set."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        raise IsolationError("%s couldn't run (%s)" % (argv[len(_priv())] if len(argv) > len(_priv()) else argv[0], e))
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
    """The allowlisted environment for the account: toolchain paths and plain CI facts; HOME and TMPDIR are its own."""
    env = os.environ if env is None else env
    out = {k: v for k, v in env.items() if ENV_ALLOW.match(k) and not SECRET_NAME.search(k)}
    out.update({"HOME": home, "TMPDIR": os.path.join(home, "tmp"), "USER": os.path.basename(home), "LOGNAME": os.path.basename(home)})
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
        self.runner = pwd.getpwuid(os.getuid()).pw_name
        self.opened, self.root_mode = [], None  # folders given o+x, and the checkout's own mode: put back at the end

    # ---------------------------------------------------------------- setting up
    def setup(self):
        """Create the account, hand it the checkout (not `.git`), and verify it can't write what it must not. When any
        step fails, everything done so far is undone (the checkout given back, the account removed) before it raises."""
        try:
            return self._setup()
        except Exception:
            if self.uid is not None:
                try:
                    self.teardown()
                except IsolationError:
                    pass  # the original reason is the one to report
            raise

    def _setup(self):
        """setup()'s steps; see there."""
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
        """Stop everything, give the checkout back to the runner's account and remove the throwaway account."""
        self.stop()
        P = _priv()
        _run(P + ["chown", "-hR", "%d:%d" % (os.getuid(), os.getgid()), self.root])
        _run(P + ["chmod", "%o" % (self.root_mode if self.root_mode is not None else 0o755), self.root])
        for d in reversed(self.opened):  # what this run opened, closed again (a self-hosted runner keeps its folders)
            _run(P + ["chmod", "o-x", d], check=False)
        _run(P + ["userdel", "--remove", self.name], check=False)  # best effort: the runner is thrown away after the job


def read_regular(path, cap):
    """The bytes of a regular file (never through a symbolic link), at most cap+1 of them; None when it isn't one."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    try:
        import stat
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        with os.fdopen(fd, "rb") as f:
            fd = None
            return f.read(cap + 1)
    finally:
        if fd is not None:
            os.close(fd)
