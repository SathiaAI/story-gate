"""End-to-end check of CI isolation (sg_isolate) on a real Linux runner: run as the CI user (not root) with
password-free sudo, as on GitHub's ubuntu runners. tests.yml runs it in its own job.

It builds a throwaway repository whose test command, scenario and budget are hostile: each leaves a detached process
behind that tries to rewrite story-gate's results, its runtime copy and the checkout's .git, and reads the environment
for a planted secret. Then it checks that every attack failed, that honest work (a private HOME and cache, a local
server, a JUnit report) still worked, and that nothing of the throwaway account is left. Exit 0 = all checks passed.
"""
import json, os, shutil, subprocess, sys, tempfile, time
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / ".story-gate"
FAIL = []


def check(ok, what):
    """Record one check and print it."""
    print(("PASS  " if ok else "FAIL  ") + what, flush=True)
    if not ok:
        FAIL.append(what)


def git(repo, *a):
    """git in the throwaway repository, raising on failure."""
    subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)


ATTACK = r'''
import os, sys, time, json
targets = sys.argv[1:]
if os.fork(): sys.exit(0)
os.setsid()
if os.fork(): os._exit(0)
end = time.time() + 40
while time.time() < end:
    for t in targets:
        try:
            with open(t, "w") as f: f.write('{"exit_code": 0, "forged": true}')
        except OSError: pass
    time.sleep(0.05)
'''


def main():
    """Build the hostile repository, run ci-tests and ci-measure isolated, and check the results."""
    if os.uname().sysname != "Linux":
        print("skipped: not Linux"); return 0
    if os.geteuid() == 0:
        print("run this as the CI user (not root), with password-free sudo"); return 2
    home = Path.home()  # like GitHub's checkout under /home/runner/work: the account must pass through a closed home
    modes = {p: p.stat().st_mode & 0o7777 for p in [home] + list(home.parents)}
    work = Path(tempfile.mkdtemp(prefix="sg-iso-", dir=str(home)))
    os.chmod(work, 0o700)
    repo, out, mout, rt = work / "repo", work / "out", work / "mout", work / "runtime"
    shutil.copytree(SRC, rt, ignore=shutil.ignore_patterns("__pycache__", "stories", "*.jsonl"))
    repo.mkdir()
    shutil.copytree(SRC, repo / ".story-gate", ignore=shutil.ignore_patterns("__pycache__", "stories", "*.jsonl"))
    attack = repo / "attack.py"
    attack.write_text(ATTACK)
    git_cfg = repo / ".git" / "config"
    targets = [str(out / "results.json"), str(out / "junit.xml"), str(rt / "gate.py"), str(git_cfg), str(mout / "measures.json")]
    tests = repo / "tests.sh"
    tests.write_text(r'''#!/bin/sh
set -u
echo "uid $(id -u) runner $SG_RUNNER_UID" ; [ "$(id -u)" != "$SG_RUNNER_UID" ] && echo uid-ok
[ -z "${SG_E2E_SECRET_TOKEN:-}" ] && echo env-ok
mkdir -p "$HOME/.cache/e2e" && echo cached > "$HOME/.cache/e2e/x" && echo home-ok
python3 -m http.server 8765 --bind 127.0.0.1 >/dev/null 2>&1 &
sleep 1
python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/', timeout=5); print('server-ok')"
mkdir -p reports && printf '<testsuite><testcase name="t1"/><testcase name="t2"><failure/></testcase></testsuite>' > reports/junit.xml
sudo -n true 2>/dev/null && echo SUDO-WORKED || echo sudo-denied
mv .git .git-moved 2>/dev/null && echo GIT-REPLACED || echo git-kept
ln -s /tmp .git 2>/dev/null && echo GIT-LINKED || true
cat "$SG_SECRET_FILE" 2>/dev/null && echo SECRET-READ || echo secret-unreadable
mkdir -p evil && (cd evil && git init -q && printf '#!/bin/sh\nid -u > "%s/fsmonitor-ran"\n' "$SG_MARK_DIR" > hook.sh && chmod +x hook.sh && \
  git config core.fsmonitor "$PWD/hook.sh" && git config core.hooksPath "$PWD" && git config diff.external "$PWD/hook.sh" && \
  git config alias.status '!sh hook.sh') && echo nested-repo-planted
''' + "python3 attack.py " + " ".join(targets) + "\nexit 1\n")
    tests.chmod(0o755)
    (repo / ".gitignore").write_text("reports/\n")
    cfg = json.loads((repo / ".story-gate/config.json").read_text())
    cfg.update({"test_command": "./tests.sh", "junit_path": "reports/junit.xml"})
    (repo / ".story-gate/config.json").write_text(json.dumps(cfg))
    (repo / ".story-gate/budgets.json").write_text(json.dumps({"version": 1, "budgets": [
        {"name": "widgets", "unit": "count", "limit": 5, "status": "decided", "source": "e2e", "measure": "number",
         "command": ["python3", "count.py"]},
        {"name": "fsize", "unit": "blocks", "limit": 10 ** 9, "status": "decided", "source": "e2e", "measure": "number",
         "command": ["python3", "-c", "import resource; print(resource.getrlimit(resource.RLIMIT_FSIZE)[1] // 512)"],
         "pins": ["count.py"]}]}))
    (repo / "count.py").write_text("import subprocess, sys\nsubprocess.Popen([sys.executable, 'attack.py'] + %r)\nprint(3)\n" % targets)
    git(repo, "init", "-q", "-b", "main"); git(repo, "config", "user.email", "e@e"); git(repo, "config", "user.name", "e")
    git(repo, "add", "."); git(repo, "commit", "-qm", "base")
    git(repo, "checkout", "-qb", "feat/SAT-1-x")
    sd = repo / ".story-gate/stories/SAT-1"; sd.mkdir(parents=True)
    (sd / "tests.json").write_text(json.dumps({"acceptance_criteria": [{"id": "AC-1", "text": "x"}]}))
    (sd / "scenarios.json").write_text(json.dumps({"story": "SAT-1", "scenarios": [
        {"name": "hostile", "acs": ["AC-1"], "argv": ["python3", "attack.py"] + targets, "expect_exit": 0,
         "expect_output": ".", "timeout": 30, "local_only": ""}]}))
    (repo / "app.py").write_text("x = 1\n")
    git(repo, "add", "."); git(repo, "commit", "-qm", "work")
    cfg_hash = git_cfg.read_bytes()
    gate_bytes = (rt / "gate.py").read_bytes()
    env = dict(os.environ, STORY_GATE_ROOT=str(repo), STORY_GATE_ISOLATE="1", STORY_GATE_ID="SAT-1",
               SG_E2E_SECRET_TOKEN="do-not-leak", SG_RUNNER_UID=str(os.getuid()))
    gcfg = work / "runner-gitconfig"  # like GitHub's runners: git trusts every folder, whoever owns it
    gcfg.write_text("[safe]\n\tdirectory = *\n[user]\n\temail = e@e\n\tname = e\n")
    env["GIT_CONFIG_GLOBAL"] = str(gcfg)
    for k in ("GITHUB_TOKEN", "GITHUB_BASE_REF", "GITHUB_HEAD_REF", "SG_BASE_REF", "SG_HEAD_REF"):
        env.pop(k, None)  # this throwaway repository is compared with its own main, not the pull request's
    env["PATH"] = env.get("PATH", "")
    # SG_RUNNER_UID isn't on the allowlist, so pass it through the test command itself
    secret = work / "runner-secret.txt"; secret.write_text("do-not-leak"); os.chmod(secret, 0o600)
    mark = Path(tempfile.mkdtemp(prefix="sg-mark-")); os.chmod(mark, 0o1777)  # where a hook run as anyone could leave a trace
    cfg["test_command"] = "SG_RUNNER_UID=%d SG_SECRET_FILE=%s SG_MARK_DIR=%s ./tests.sh" % (os.getuid(), secret, mark)
    (repo / ".story-gate/config.json").write_text(json.dumps(cfg))
    git(repo, "commit", "-qam", "uid")
    out.mkdir(); os.chmod(out, 0o777)  # as a careless runner set-up might leave it: story-gate must lock it down itself
    r = subprocess.run([sys.executable, str(rt / "gate.py"), "ci-tests", str(out)], cwd=repo, env=env, capture_output=True, text=True, timeout=600)
    print(r.stdout[-2000:], r.stderr[-2000:])
    time.sleep(2)  # any survivor would keep writing during this pause
    res = json.loads((out / "results.json").read_text())
    tail = res.get("output_tail", "")
    check(res.get("isolated") is True, "tests ran as a separate account")
    check(res.get("exit_code") == 1 and not res.get("forged"), "the real exit code (1) was kept, not the forged 0")
    for word in ("uid-ok", "env-ok", "home-ok", "server-ok", "sudo-denied", "git-kept", "secret-unreadable", "nested-repo-planted"):
        check(word in tail, "honest work and limits inside the run: %s" % word)
    for bad in ("SUDO-WORKED", "GIT-REPLACED", "GIT-LINKED", "SECRET-READ", "do-not-leak"):
        check(bad not in tail, "the account could not: %s" % bad)
    check((out / "junit.xml").is_file() and b"forged" not in (out / "junit.xml").read_bytes(), "the JUnit report was read and not forged")
    check(git_cfg.read_bytes() == cfg_hash, ".git/config was not changed")
    check((rt / "gate.py").read_bytes() == gate_bytes, "story-gate's runtime copy was not changed")
    ran = mark / "fsmonitor-ran"
    check(not ran.exists() or ran.stat().st_uid != os.getuid(), "story-gate's git never ran a hook the pull request planted")
    sc = json.loads((out / "scenarios.json").read_text())
    check(sc.get("isolated") is True and "hostile" in sc.get("results", {}), "the scenario ran isolated and was recorded")
    left = subprocess.run(["pgrep", "-f", "attack.py"], capture_output=True, text=True).stdout.split()
    check(not left, "no attacker process survived (%s)" % " ".join(left))
    users = [u.split(":")[0] for u in Path("/etc/passwd").read_text().splitlines() if u.startswith("sgpr-")]
    check(not users, "the throwaway account was removed (%s)" % ", ".join(users))
    check(all(p.lstat().st_uid == os.getuid() for p in [repo] + list(repo.rglob("*"))), "the checkout belongs to the CI user again")
    check(all(p.stat().st_mode & 0o7777 == m for p, m in modes.items()) and work.stat().st_mode & 0o7777 == 0o700,
          "the folders opened for the account were closed again")
    r = subprocess.run([sys.executable, str(rt / "gate.py"), "ci-measure", str(mout)], cwd=repo, env=env, capture_output=True, text=True, timeout=600)
    print(r.stdout[-1500:], r.stderr[-1500:])
    time.sleep(2)
    meta = json.loads((mout / "measures.json").read_text())
    check(meta.get("isolated") is True and not meta.get("forged"), "budgets were measured isolated and measures.json not forged")
    check((mout / "measures" / "widgets.raw").read_bytes().strip() == b"3", "the measured value is the real one")
    import importlib.util
    spec = importlib.util.spec_from_file_location("sgv", str(rt / "sg_validation.py")); V = importlib.util.module_from_spec(spec); spec.loader.exec_module(V)
    check((mout / "measures" / "fsize.raw").read_bytes().strip() == str(V.FILE_LIMIT // 512).encode(),
          "a measuring command's file-size limit is hard (it can't raise it)")
    # A set-up that can't be made safe fails closed and undoes itself: a results folder inside a folder anyone can write.
    loose = work / "loose"; loose.mkdir(); os.chmod(loose, 0o777)  # not sticky: the account could swap the results folder
    r = subprocess.run([sys.executable, str(rt / "gate.py"), "ci-tests", str(loose / "out")], cwd=repo, env=env, capture_output=True, text=True, timeout=600)
    res = json.loads((loose / "out" / "results.json").read_text())
    check(res.get("exit_code") is None and res.get("isolated") is False and "could write" in res.get("error", ""),
          "an unsafe set-up runs nothing (strict) and says why")
    users = [u.split(":")[0] for u in Path("/etc/passwd").read_text().splitlines() if u.startswith("sgpr-")]
    check(not users and all(p.lstat().st_uid == os.getuid() for p in [repo] + list(repo.rglob("*"))),
          "the failed set-up removed its account and gave the checkout back")
    left = subprocess.run(["pgrep", "-f", "attack.py"], capture_output=True, text=True).stdout.split()
    check(not left, "no attacker process survived the measure run")
    shutil.rmtree(work, ignore_errors=True)
    print("\n%d check(s) failed" % len(FAIL) if FAIL else "\nall isolation checks passed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
