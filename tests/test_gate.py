import json, os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / ".story-gate"
PY = sys.executable


def run(repo, *args, stdin=None, env=None):
    e = dict(os.environ, STORY_GATE_ROOT=str(repo), HOME=str(repo / "_home"))
    for k in ("OPENROUTER_API_KEY", "TYPESAFE_API_KEY", "JUDGE_API_KEY", "STORY_GATE_ENV_FILE", "STORY_GATE_ID", "PR_TITLE",
              "GITHUB_BASE_REF", "GITHUB_HEAD_REF", "SG_BASE_REF", "SG_HEAD_REF", "GITHUB_EVENT_PATH", "GITHUB_TOKEN", "GITHUB_STEP_SUMMARY",
              "STORY_GATE_TRUSTED_DIR", "STORY_GATE_HOME", "CLAUDECODE", "CURSOR_TRACE_ID", "CODEX_SANDBOX", "GEMINI_CLI", "GITHUB_ACTIONS"):
        e.pop(k, None)  # tests never reach a real judge or read the CI runner's own pull request
    e.update(env or {})
    return subprocess.run([PY, str(repo / ".story-gate/gate.py"), *args], cwd=repo, input=stdin,
                          capture_output=True, text=True, env=e, timeout=120)


class Base(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        shutil.copytree(SRC, self.repo / ".story-gate", ignore=shutil.ignore_patterns("stories", "__pycache__", "*.jsonl", "judge-calibration.json"))
        g = lambda *a: subprocess.run(["git", *a], cwd=self.repo, capture_output=True, check=True)
        g("init", "-q", "-b", "main"); g("config", "user.email", "t@t"); g("config", "user.name", "t")
        (self.repo / "app.py").write_text("x = 1\n")
        g("add", "."); g("commit", "-qm", "init")
        g("checkout", "-qb", "feature/SAT-1-thing")

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def cfg(self, **kw):
        p = self.repo / ".story-gate/config.json"
        c = json.loads(p.read_text()); c.update(kw); p.write_text(json.dumps(c))

    def fill_ready(self, sid="SAT-1", self_score=0.9, drift="none"):
        sd = self.repo / ".story-gate/stories" / sid
        (sd / "story.md").write_text("---\nid: %s\ntitle: T\nsource: linear:%s\ndepends_on: []\n---\n" % (sid, sid) + "As a user I want X so that Y. " * 20)
        (sd / "context.md").write_text("## PRD\nPRD#2 says X\n## TRD\nTRD#3 rules\n## Upstream handoffs\nNone — no upstream dependencies\n## Prior learnings\nNone — searched: x y\n")
        (sd / "tests.json").write_text(json.dumps({"acceptance_criteria": [{"id": "AC-1", "text": "x", "positive": ["p"], "negative": ["n"], "edge": ["e"], "regression": [], "not_applicable": {"regression": "new module"}, "test_refs": ["test_ac1_x"]}]}))
        import importlib.util
        spec = importlib.util.spec_from_file_location("g", self.repo / ".story-gate/gate.py"); g = importlib.util.module_from_spec(spec); spec.loader.exec_module(g)
        (sd / "ready.self.json").write_text(json.dumps({"scores": {k: self_score for k in g.READY_Q}, "drift": drift}))
        (sd / "done.self.json").write_text(json.dumps({"scores": {k: self_score for k in g.DONE_Q}, "drift": "none"}))


class TestVerdict(Base):
    def test_self_judge_never_passes(self):
        run(self.repo, "start", "SAT-1"); self.fill_ready()
        r = run(self.repo, "score", "SAT-1", "ready")
        v = json.loads((self.repo / ".story-gate/stories/SAT-1/ready.json").read_text())
        self.assertEqual(v["judge"], "self")
        self.assertEqual(v["overall"], "CONCERNS", r.stdout)

    def test_skeleton_fails(self):
        run(self.repo, "start", "SAT-1")
        run(self.repo, "score", "SAT-1", "ready")
        v = json.loads((self.repo / ".story-gate/stories/SAT-1/ready.json").read_text())
        self.assertEqual(v["overall"], "FAIL")
        for k in ("story_present", "context_present", "test_matrix", "judge_available"):
            self.assertEqual(v["checks"][k]["status"], "FAIL", k)

    def test_missing_test_category_fails(self):
        run(self.repo, "start", "SAT-1"); self.fill_ready()
        p = self.repo / ".story-gate/stories/SAT-1/tests.json"
        t = json.loads(p.read_text()); t["acceptance_criteria"][0]["not_applicable"] = {}; p.write_text(json.dumps(t))
        run(self.repo, "score", "SAT-1", "ready")
        v = json.loads((self.repo / ".story-gate/stories/SAT-1/ready.json").read_text())
        self.assertEqual(v["checks"]["test_matrix"]["status"], "FAIL")

    def test_upstream_handoff_required(self):
        run(self.repo, "start", "SAT-1"); self.fill_ready()
        p = self.repo / ".story-gate/stories/SAT-1/story.md"
        p.write_text(p.read_text().replace("depends_on: []", "depends_on: [SAT-0]"))
        run(self.repo, "score", "SAT-1", "ready")
        v = json.loads((self.repo / ".story-gate/stories/SAT-1/ready.json").read_text())
        self.assertEqual(v["checks"]["upstream_handoffs"]["status"], "FAIL")
        (self.repo / ".story-gate/stories/SAT-0").mkdir(); (self.repo / ".story-gate/stories/SAT-0/handoff.md").write_text("x")
        run(self.repo, "score", "SAT-1", "ready")
        v = json.loads((self.repo / ".story-gate/stories/SAT-1/ready.json").read_text())
        self.assertEqual(v["checks"]["upstream_handoffs"]["status"], "PASS")

    def test_drift_escalates_then_decision(self):
        run(self.repo, "start", "SAT-1"); self.fill_ready(drift="spec_should_change")
        self.cfg(judge={"jev": False, "allow_self_judge_pass": True})
        run(self.repo, "score", "SAT-1", "ready")
        v = json.loads((self.repo / ".story-gate/stories/SAT-1/ready.json").read_text())
        self.assertEqual(v["overall"], "ESCALATED")
        self.assertIn("drift_escalated", (self.repo / ".story-gate/outbox.jsonl").read_text())
        run(self.repo, "decide", "SAT-1", "--drift", "spec", "--by", "Paul", "--note", "PRD outdated")
        run(self.repo, "score", "SAT-1", "ready")
        v = json.loads((self.repo / ".story-gate/stories/SAT-1/ready.json").read_text())
        self.assertEqual(v["overall"], "PASS")

    def test_waiver(self):
        run(self.repo, "start", "SAT-1"); self.fill_ready(self_score=0.5)
        self.cfg(judge={"jev": False, "allow_self_judge_pass": True})
        run(self.repo, "score", "SAT-1", "ready")
        self.assertEqual(json.loads((self.repo / ".story-gate/stories/SAT-1/ready.json").read_text())["overall"], "CONCERNS")
        import importlib.util
        spec = importlib.util.spec_from_file_location("g", self.repo / ".story-gate/gate.py"); g = importlib.util.module_from_spec(spec); spec.loader.exec_module(g)
        for k in g.READY_Q:
            run(self.repo, "waive", "SAT-1", k, "--by", "Paul", "--reason", "pilot")
        run(self.repo, "score", "SAT-1", "ready")
        self.assertEqual(json.loads((self.repo / ".story-gate/stories/SAT-1/ready.json").read_text())["overall"], "WAIVED")


class TestDone(Base):
    def test_done_flow(self):
        self.cfg(judge={"jev": False, "allow_self_judge_pass": True})
        run(self.repo, "start", "SAT-1"); self.fill_ready()
        self.assertEqual(run(self.repo, "score", "SAT-1", "ready").returncode, 0)
        (self.repo / "app.py").write_text("x = 2\n")
        r = run(self.repo, "score", "SAT-1", "done")
        v = json.loads((self.repo / ".story-gate/stories/SAT-1/done.json").read_text())
        self.assertEqual(v["overall"], "FAIL")
        for k in ("handoff_written", "learnings_recorded", "tests_ran_green"):
            self.assertEqual(v["checks"][k]["status"], "FAIL", k)
        self.assertEqual(v["checks"]["code_changed"]["status"], "PASS")
        self.assertEqual(run(self.repo, "record-tests", "SAT-1", "--", PY, "-c", "import sys; sys.exit(3)").returncode, 1)
        (self.repo / "test_app.py").write_text("def test_ac1_x():\n    assert True\n")
        run(self.repo, "record-tests", "SAT-1", "--", PY, "-c", "print('ok')")
        h = "# Handoff\n" + "".join("## %s\nreal content\n" % s for s in ("What changed", "Interfaces and contracts", "How to verify", "Known limits", "Downstream consumers", "Release and rollback", "Drift decisions"))
        (self.repo / ".story-gate/stories/SAT-1/handoff.md").write_text(h)
        self.assertNotEqual(run(self.repo, "learn", "SAT-1", "--type", "error", "--summary", "x").returncode, 0)  # errors need root cause + rule
        run(self.repo, "learn", "SAT-1", "--type", "error", "--summary", "pytest import path wrong", "--root-cause", "no conftest", "--rule", "add conftest.py at repo root", "--tags", "pytest")
        r = run(self.repo, "score", "SAT-1", "done")
        self.assertEqual(r.returncode, 0, r.stdout)
        (self.repo / "app.py").write_text("x = 5\n")  # code changed after green run -> stale
        r = run(self.repo, "score", "SAT-1", "done")
        self.assertIn("code changed after the last green test run", r.stdout)
        out = run(self.repo, "learnings", "pytest").stdout
        self.assertIn("conftest", out)


class TestHooks(Base):
    PAYLOADS = {
        "claude": {"hook_event_name": "PreToolUse", "tool_name": "Edit", "tool_input": {"file_path": "app.py"}, "cwd": "."},
        "codex": {"hook_event_name": "PreToolUse", "tool_name": "apply_patch", "tool_input": {"command": "*** Begin Patch\n*** Update File: app.py\n@@\n-x\n+y\n*** End Patch"}},
        "cursor": {"hook_event_name": "preToolUse", "tool_name": "Write", "tool_input": {"file_path": "app.py"}, "workspace_roots": ["."]},
        "gemini": {"hook_event_name": "BeforeTool", "tool_name": "write_file", "tool_input": {"file_path": "app.py"}},
        "windsurf": {"agent_action_name": "pre_write_code", "tool_info": {"file_path": "app.py"}},
        "grok": {"hookEventName": "PreToolUse", "toolName": "Edit", "toolInput": {"file_path": "app.py"}, "workspaceRoot": "."},
    }

    def test_warn_mode_never_blocks_and_is_visible(self):
        for cl, p in self.PAYLOADS.items():
            r = run(self.repo, "hook", "--client", cl, "--event", "pre", stdin=json.dumps(p))
            self.assertEqual(r.returncode, 0, cl)
            self.assertIn("SAT-1 READY gate is not run", r.stdout, cl)
            if cl != "windsurf":
                json.loads(r.stdout)  # valid JSON for JSON clients

    def test_enforce_blocks_with_exit_2(self):
        self.cfg(mode="enforce")
        for cl, p in self.PAYLOADS.items():
            r = run(self.repo, "hook", "--client", cl, "--event", "pre", stdin=json.dumps(p))
            self.assertEqual(r.returncode, 2, cl)
            self.assertIn("BLOCKED", r.stderr, cl)

    def test_enforce_point_only(self):
        self.cfg(enforce_points=["pre_edit"])
        r = run(self.repo, "hook", "--client", "claude", "--event", "pre", stdin=json.dumps(self.PAYLOADS["claude"]))
        self.assertEqual(r.returncode, 2)
        r = run(self.repo, "hook", "--client", "claude", "--event", "stop", stdin="{}")
        self.assertEqual(r.returncode, 0)

    def test_exempt_paths_allowed(self):
        self.cfg(mode="enforce")
        p = {"tool_name": "Write", "tool_input": {"file_path": str(self.repo / ".story-gate/stories/SAT-1/story.md")}}
        self.assertEqual(run(self.repo, "hook", "--client", "claude", "--event", "pre", stdin=json.dumps(p)).returncode, 0)
        p = {"tool_name": "Write", "tool_input": {"file_path": ".github/workflows/x.yml"}}
        self.assertEqual(run(self.repo, "hook", "--client", "claude", "--event", "pre", stdin=json.dumps(p)).returncode, 2)

    def test_pass_allows_edit(self):
        self.cfg(mode="enforce", judge={"jev": False, "allow_self_judge_pass": True})
        run(self.repo, "start", "SAT-1"); self.fill_ready(); run(self.repo, "score", "SAT-1", "ready")
        r = run(self.repo, "hook", "--client", "claude", "--event", "pre", stdin=json.dumps(self.PAYLOADS["claude"]))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_stop_blocks_until_done_and_has_loop_guard(self):
        self.cfg(mode="enforce", judge={"jev": False, "allow_self_judge_pass": True})
        run(self.repo, "start", "SAT-1"); self.fill_ready(); run(self.repo, "score", "SAT-1", "ready")
        (self.repo / "app.py").write_text("x = 3\n")
        self.assertEqual(run(self.repo, "hook", "--client", "claude", "--event", "stop", stdin="{}").returncode, 2)
        # enforce mode keeps blocking even on the client's retry; warn mode lets the retry through
        self.assertEqual(run(self.repo, "hook", "--client", "claude", "--event", "stop", stdin='{"stop_hook_active": true}').returncode, 2)
        self.assertEqual(run(self.repo, "hook", "--client", "claude", "--event", "stop", stdin='{"stop_hook_active": true}').returncode, 2)
        self.assertEqual(run(self.repo, "hook", "--client", "claude", "--event", "stop", stdin='{"stop_hook_active": true}').returncode, 0)  # loop cap
        self.cfg(mode="warn", enforce_points=[])
        self.assertEqual(run(self.repo, "hook", "--client", "claude", "--event", "stop", stdin='{"stop_hook_active": true}').returncode, 0)
        self.assertEqual(run(self.repo, "hook", "--client", "cursor", "--event", "stop", stdin='{"loop_count": 1}').returncode, 0)

    def test_garbage_stdin_does_not_crash(self):
        r = run(self.repo, "hook", "--client", "claude", "--event", "pre", stdin="not json")
        self.assertEqual(r.returncode, 0)


class TestInstallCI(Base):
    def test_install_idempotent_and_merges(self):
        (self.repo / ".claude").mkdir()
        (self.repo / ".claude/settings.json").write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]}, "hooks": {"PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo mine"}]},
            {"matcher": "Edit", "hooks": [{"type": "command", "command": "python3 .story-gate/gate.py hook --client claude --event pre"}]}]}}))
        (self.repo / "AGENTS.md").write_text("# Mine\nkeep me\n")
        for _ in range(2):
            self.assertEqual(run(self.repo, "install").returncode, 0)
        s = json.loads((self.repo / ".claude/settings.json").read_text())
        self.assertEqual(s["permissions"]["allow"], ["Bash(ls)"])
        self.assertEqual(s["hooks"]["PreToolUse"], [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo mine"}]}])  # old branch-code hook removed
        a = (self.repo / "AGENTS.md").read_text()
        self.assertIn("keep me", a); self.assertEqual(a.count("story-gate:start"), 1)
        self.assertFalse((self.repo / ".codex/hooks.json").exists())  # hooks live in user settings now
        for f in ("CLAUDE.md", "GEMINI.md", ".github/workflows/story-gate.yml"):
            self.assertTrue((self.repo / f).exists(), f)
        self.assertIn(".story-gate/.active", (self.repo / ".gitignore").read_text())

    def test_ci_warn_vs_enforce(self):
        (self.repo / "app.py").write_text("x = 9\n")
        r = run(self.repo, "ci")
        self.assertEqual(r.returncode, 0); self.assertIn("::warning", r.stdout)
        self.cfg(enforce_points=["ci"])
        r = run(self.repo, "ci")
        self.assertEqual(r.returncode, 1); self.assertIn("::error", r.stdout)

    def test_publish_command_sink(self):
        run(self.repo, "start", "SAT-1")
        out = self.repo / "sink.txt"
        self.cfg(sinks=[{"type": "repo"}, {"type": "command", "command": "%s -c \"import sys; open(r'%s','a').write(sys.stdin.read())\"" % (PY, out)}])
        self.assertEqual(run(self.repo, "publish").returncode, 0)
        self.assertIn("story_gate.started", out.read_text())
        self.assertIn("nothing new delivered", run(self.repo, "publish").stdout)
        self.assertEqual(out.read_text().count("story_gate.started"), 1)  # no duplicates


class TestHardening(Base):
    def test_traversal_and_gate_files_not_exempt(self):
        self.cfg(mode="enforce")
        for f in (".story-gate/../app.py", ".story-gate/stories/SAT-1/ready.json", ".story-gate/gate.py", ".story-gate/config.json", "../outside.py"):
            p = {"tool_name": "Write", "tool_input": {"file_path": f}}
            self.assertEqual(run(self.repo, "hook", "--client", "claude", "--event", "pre", stdin=json.dumps(p)).returncode, 2, f)
        p = {"tool_name": "apply_patch", "tool_input": {"command": "*** Update File: notes.md\n*** Move to: app.py\n"}}
        self.assertEqual(run(self.repo, "hook", "--client", "codex", "--event", "pre", stdin=json.dumps(p)).returncode, 2)

    def test_self_judge_cannot_unlock_edits(self):
        self.cfg(mode="enforce")
        run(self.repo, "start", "SAT-1"); self.fill_ready(self_score=1.0); run(self.repo, "score", "SAT-1", "ready")
        r = run(self.repo, "hook", "--client", "claude", "--event", "pre", stdin=json.dumps(TestHooks.PAYLOADS["claude"]))
        self.assertEqual(r.returncode, 2)

    def test_stale_ready_after_story_edit(self):
        self.cfg(mode="enforce", judge={"jev": False, "allow_self_judge_pass": True})
        run(self.repo, "start", "SAT-1"); self.fill_ready(); run(self.repo, "score", "SAT-1", "ready")
        p = self.repo / ".story-gate/stories/SAT-1/story.md"; p.write_text(p.read_text() + "\nnew scope")
        r = run(self.repo, "hook", "--client", "claude", "--event", "pre", stdin=json.dumps(TestHooks.PAYLOADS["claude"]))
        self.assertEqual(r.returncode, 2); self.assertIn("out of date", r.stderr)

    def test_structural_checks_cannot_be_waived(self):
        run(self.repo, "start", "SAT-1")
        r = run(self.repo, "waive", "SAT-1", "test_matrix", "--by", "agent", "--reason", "x")
        self.assertNotEqual(r.returncode, 0)

    def test_bad_payloads_and_files_never_crash_in_warn(self):
        for raw in ('"x"', "null", "[]", "{}"):
            self.assertEqual(run(self.repo, "hook", "--client", "claude", "--event", "pre", stdin=raw).returncode, 0, raw)
        run(self.repo, "start", "SAT-1")
        (self.repo / ".story-gate/stories/SAT-1/ready.json").write_text("{corrupt")
        (self.repo / ".story-gate/stories/SAT-1/decisions.jsonl").write_text("garbage\n")
        (self.repo / "app.py").write_text("x = 7\n")
        self.assertEqual(run(self.repo, "ci").returncode, 0)
        self.assertEqual(run(self.repo, "hook", "--client", "claude", "--event", "stop", stdin="{}").returncode, 0)
        run(self.repo, "score", "SAT-1", "ready")

    def test_master_base_fallback_and_title_ids(self):
        subprocess.run(["git", "branch", "-m", "main", "master"], cwd=self.repo, capture_output=True)
        (self.repo / "app.py").write_text("x = 8\n")
        self.assertIn("1 code files", run(self.repo, "ci").stdout)
        subprocess.run(["git", "checkout", "-qb", "fix-encoding"], cwd=self.repo, capture_output=True)
        out = run(self.repo, "ci", env={"PR_TITLE": "Fix UTF-8 handling"}).stdout
        self.assertIn("no story id", out)


class TestRegressions(Base):
    def test_green_run_survives_git_add_and_commit(self):
        self.cfg(judge={"jev": False, "allow_self_judge_pass": True})
        run(self.repo, "start", "SAT-1"); self.fill_ready(); run(self.repo, "score", "SAT-1", "ready")
        (self.repo / "new_mod.py").write_text("y = 1\n"); (self.repo / "test_new_mod.py").write_text("def test_ac1_x():\n    pass\n")
        run(self.repo, "record-tests", "SAT-1", "--", PY, "-c", "print(1)")
        h = "# H\n" + "".join("## %s\nreal\n" % x for x in ("What changed", "Interfaces and contracts", "How to verify", "Known limits", "Downstream consumers", "Release and rollback", "Drift decisions"))
        (self.repo / ".story-gate/stories/SAT-1/handoff.md").write_text(h)
        run(self.repo, "learn", "SAT-1", "--type", "none", "--summary", "no new learnings")
        self.assertEqual(run(self.repo, "score", "SAT-1", "done").returncode, 0)
        subprocess.run(["git", "add", "-A"], cwd=self.repo); subprocess.run(["git", "commit", "-qm", "work"], cwd=self.repo)
        r = run(self.repo, "hook", "--client", "claude", "--event", "stop", stdin="{}")
        self.assertEqual(r.stdout.strip(), "{}", r.stdout)

    def test_gate_files_blocked_even_after_ready(self):
        self.cfg(mode="enforce", judge={"jev": False, "allow_self_judge_pass": True})
        run(self.repo, "start", "SAT-1"); self.fill_ready(); run(self.repo, "score", "SAT-1", "ready")
        for f in (".story-gate/stories/SAT-1/done.json", ".story-gate/config.json", ".story-gate/gate.py", str(self.repo / ".story-gate/stories/SAT-1/test_results.json")):
            p = {"tool_name": "Write", "tool_input": {"file_path": f}}
            r = run(self.repo, "hook", "--client", "claude", "--event", "pre", stdin=json.dumps(p))
            self.assertEqual(r.returncode, 2, f)
        p = {"tool_name": "Write", "tool_input": {"file_path": ".story-gate/stories/SAT-1/handoff.md"}}
        self.assertEqual(run(self.repo, "hook", "--client", "claude", "--event", "pre", stdin=json.dumps(p)).returncode, 0)

    def test_ci_survives_corrupt_config_in_warn(self):
        (self.repo / ".story-gate/config.json").write_text("")
        (self.repo / "app.py").write_text("x = 4\n")
        r = run(self.repo, "ci")
        self.assertEqual(r.returncode, 1); self.assertIn("unreadable", r.stdout)  # fail closed: mode unknown


class TestCheckpointAndTrace(Base):
    def test_traceability_required(self):
        self.cfg(judge={"jev": False, "allow_self_judge_pass": True})
        run(self.repo, "start", "SAT-1"); self.fill_ready(); run(self.repo, "score", "SAT-1", "ready")
        (self.repo / "app.py").write_text("x = 2\n")  # code but no test named test_ac1_x
        run(self.repo, "score", "SAT-1", "done")
        v = json.loads((self.repo / ".story-gate/stories/SAT-1/done.json").read_text())
        self.assertEqual(v["checks"]["traceability"]["status"], "FAIL")
        self.assertIn("| AC-1 |", (self.repo / ".story-gate/stories/SAT-1/trace.md").read_text())

    def test_checkpoint_without_judge_is_unknown_and_post_hook_quiet(self):
        run(self.repo, "start", "SAT-1"); self.fill_ready()
        r = run(self.repo, "checkpoint", "SAT-1")
        self.assertIn("UNKNOWN", r.stdout); self.assertEqual(r.returncode, 1)
        self.cfg(checkpoint={"every_edits": 2})
        for _ in range(3):
            r = run(self.repo, "hook", "--client", "claude", "--event", "post", stdin=json.dumps(TestHooks.PAYLOADS["claude"]))
            self.assertEqual(r.returncode, 0); json.loads(r.stdout)
        self.assertEqual(json.loads((self.repo / ".story-gate/.edits").read_text())["SAT-1"], 3)

    def test_off_course_contains_edits_when_enforced(self):
        self.cfg(judge={"jev": False, "allow_self_judge_pass": True}, enforce_points=["checkpoint"])
        run(self.repo, "start", "SAT-1"); self.fill_ready(); run(self.repo, "score", "SAT-1", "ready")
        sd = self.repo / ".story-gate/stories/SAT-1"
        (sd / "checkpoints.jsonl").write_text(json.dumps({"at": "2026-10-02T10:00:00Z", "status": "OFF_COURSE", "why": ["drift: work_should_change"]}) + "\n")
        r = run(self.repo, "hook", "--client", "claude", "--event", "pre", stdin=json.dumps(TestHooks.PAYLOADS["claude"]))
        self.assertEqual(r.returncode, 2); self.assertIn("OFF COURSE", r.stderr)
        run(self.repo, "decide", "SAT-1", "--phase", "build", "--drift", "story", "--by", "Paul", "--note", "x")
        r = run(self.repo, "hook", "--client", "claude", "--event", "pre", stdin=json.dumps(TestHooks.PAYLOADS["claude"]))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_install_writes_skills_for_all_clients(self):
        run(self.repo, "install")
        for d in (".agents/skills/story-gate/SKILL.md", ".claude/skills/story-gate/SKILL.md"):
            self.assertIn("name: story-gate", (self.repo / d).read_text())


class TestRound1Fixes(Base):
    def test_story_files_never_satisfy_traceability(self):
        """CodeRabbit 4166059695: committed tests.json must not make its own test_refs 'found'."""
        self.cfg(judge={"jev": False, "allow_self_judge_pass": True})
        run(self.repo, "start", "SAT-1"); self.fill_ready(); run(self.repo, "score", "SAT-1", "ready")
        (self.repo / "app.py").write_text("x = 2\n")
        subprocess.run(["git", "add", "-A"], cwd=self.repo); subprocess.run(["git", "commit", "-qm", "w"], cwd=self.repo)
        run(self.repo, "score", "SAT-1", "done")
        v = json.loads((self.repo / ".story-gate/stories/SAT-1/done.json").read_text())
        self.assertEqual(v["checks"]["traceability"]["status"], "FAIL")

    def test_learnings_tolerates_bad_lines(self):
        (self.repo / ".story-gate/learnings.jsonl").write_text("<<<<<<< HEAD\n{\"summary\": \"no id\"}\n")
        r = run(self.repo, "learnings", "x")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_red_run_invalidates_done(self):
        """Codex 4166101862: a later red run on unchanged code must invalidate a DONE PASS."""
        self.cfg(judge={"jev": False, "allow_self_judge_pass": True})
        run(self.repo, "start", "SAT-1"); self.fill_ready(); run(self.repo, "score", "SAT-1", "ready")
        (self.repo / "test_app.py").write_text("def test_ac1_x():\n    assert True\n")
        run(self.repo, "record-tests", "SAT-1", "--", PY, "-c", "print(1)")
        h = "# H\n" + "".join("## %s\nreal\n" % x for x in ("What changed", "Interfaces and contracts", "How to verify", "Known limits", "Downstream consumers", "Release and rollback", "Drift decisions"))
        (self.repo / ".story-gate/stories/SAT-1/handoff.md").write_text(h)
        run(self.repo, "learn", "SAT-1", "--type", "none", "--summary", "none")
        self.assertEqual(run(self.repo, "score", "SAT-1", "done").returncode, 0)
        run(self.repo, "record-tests", "SAT-1", "--", PY, "-c", "import sys; sys.exit(1)")
        self.assertIn("DONE gate", run(self.repo, "hook", "--client", "claude", "--event", "stop", stdin="{}").stdout)

    def test_handoff_requires_drift_section_and_version(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("g", self.repo / ".story-gate/gate.py"); g = importlib.util.module_from_spec(spec); spec.loader.exec_module(g)
        self.assertIn("## Drift decisions", g.HANDOFF_SECTIONS)
        self.assertEqual(g.VERSION, "0.4.0")
        self.assertIn("head.sha", g.CI_YML); self.assertIn("persist-credentials: false", g.CI_YML)


class TestRound2Fixes(Base):
    def test_prior_learnings_none_needs_search_evidence(self):
        run(self.repo, "start", "SAT-1"); self.fill_ready()
        p = self.repo / ".story-gate/stories/SAT-1/context.md"
        p.write_text(p.read_text().replace("None — searched: x y", "None"))
        run(self.repo, "score", "SAT-1", "ready")
        v = json.loads((self.repo / ".story-gate/stories/SAT-1/ready.json").read_text())
        self.assertEqual(v["checks"]["context_present"]["status"], "FAIL")

    def test_exec_bit_change_invalidates_test_run(self):
        if os.name == "nt":
            self.skipTest("POSIX exec bit")
        run(self.repo, "start", "SAT-1")
        (self.repo / "tool.sh").write_text("echo hi\n"); os.chmod(self.repo / "tool.sh", 0o755)
        run(self.repo, "record-tests", "SAT-1", "--", PY, "-c", "print(1)")
        fp1 = json.loads((self.repo / ".story-gate/stories/SAT-1/test_results.json").read_text())["fingerprint"]
        os.chmod(self.repo / "tool.sh", 0o644)
        import importlib.util
        os.environ["STORY_GATE_ROOT"] = str(self.repo)
        try:
            spec = importlib.util.spec_from_file_location("g2", self.repo / ".story-gate/gate.py"); g = importlib.util.module_from_spec(spec); spec.loader.exec_module(g)
            self.assertNotEqual(fp1, g.work_fingerprint())
        finally:
            os.environ.pop("STORY_GATE_ROOT")

    def test_duplicate_sinks_get_own_cursor(self):
        run(self.repo, "start", "SAT-1")
        o1, o2 = self.repo / "s1.txt", self.repo / "s2.txt"
        cmd = lambda o: "%s -c \"import sys; open(r'%s','a').write(sys.stdin.read())\"" % (PY, o)
        self.cfg(sinks=[{"type": "command", "command": cmd(o1)}, {"type": "command", "command": cmd(o2)}])
        run(self.repo, "publish")
        self.assertIn("story_gate.started", o1.read_text()); self.assertIn("story_gate.started", o2.read_text())

    def test_cursor_stop_warning_visible(self):
        run(self.repo, "start", "SAT-1")
        (self.repo / "app.py").write_text("x = 11\n")
        r = run(self.repo, "hook", "--client", "cursor", "--event", "stop", stdin='{"loop_count": 0, "workspace_roots": ["."]}')
        self.assertEqual(r.returncode, 0); self.assertIn("followup_message", r.stdout)


def load_gate(repo):
    import importlib.util
    os.environ["STORY_GATE_ROOT"] = str(repo)
    sys.path.insert(0, str(repo / ".story-gate"))
    for m in ("sg_judges", "sg_github"):
        sys.modules.pop(m, None)
    spec = importlib.util.spec_from_file_location("gate_mod_%d" % id(repo), repo / ".story-gate/gate.py")
    g = importlib.util.module_from_spec(spec); spec.loader.exec_module(g)
    return g


class MockJudge:
    """Tiny local server that speaks the Jev decisions format and the OpenAI chat format."""
    def __init__(self, model="typesafe/jev-1.13", p=0.95, broken=False):
        import http.server, threading
        me = self
        self.model, self.p, self.broken, self.last = model, p, broken, None

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode())
                me.last = body
                if self.path.endswith("/chat/completions"):
                    props = body["response_format"]["json_schema"]["schema"]["properties"]
                    ans = {k: ({"noul": me.p} if "noul" in v["properties"] else {"choice": v["properties"]["choice"]["enum"][0], "confidence": 0.9}) for k, v in props.items()}
                    out = {"model": me.model, "choices": [{"message": {"content": json.dumps(ans)}}]}
                else:
                    ans = {}
                    for k, q in body["questions"].items():
                        ans[k] = {"noul": me.p} if q["type"] == "noul" else {"choice": "none", "confidence": 0.9}
                    if me.broken:
                        ans.pop(next(iter(ans)))
                    out = {"model": me.model, "answers": ans}
                raw = json.dumps(out).encode()
                self.send_response(200); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
        self.srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


class TestJudges(Base):
    def score_with(self, judge_cfg, p=0.95, model="typesafe/jev-1.13", broken=False):
        m = MockJudge(model=model, p=p, broken=broken)
        try:
            self.cfg(judge=dict(judge_cfg, base_url=m.url + ("/decisions" if judge_cfg["provider"] != "openai-compatible" else "")))
            run(self.repo, "start", "SAT-1"); self.fill_ready()
            for f in ("ready.self.json", "done.self.json"):
                (self.repo / ".story-gate/stories/SAT-1" / f).unlink()
            r = run(self.repo, "score", "SAT-1", "ready", env={"JUDGE_API_KEY": "k"})
            return json.loads((self.repo / ".story-gate/stories/SAT-1/ready.json").read_text()), r
        finally:
            m.close()

    def test_jev_via_proxy_can_pass(self):
        v, r = self.score_with({"provider": "decisions-proxy", "api_key_env": "JUDGE_API_KEY"})
        self.assertEqual((v["judge"], v["overall"]), ("jev", "PASS"), r.stdout)

    def test_non_jev_model_behind_proxy_is_emulated_and_capped(self):
        v, r = self.score_with({"provider": "decisions-proxy", "api_key_env": "JUDGE_API_KEY"}, model="some-other-model")
        self.assertEqual((v["judge"], v["overall"]), ("emulated", "CONCERNS"), r.stdout)

    def test_openai_compatible_is_capped(self):
        v, r = self.score_with({"provider": "openai-compatible", "api_key_env": "JUDGE_API_KEY", "model": "local-7b"})
        self.assertEqual((v["judge"], v["overall"]), ("emulated", "CONCERNS"), r.stdout)

    def test_incomplete_answers_fail_closed(self):
        v, r = self.score_with({"provider": "decisions-proxy", "api_key_env": "JUDGE_API_KEY"}, broken=True)
        self.assertNotEqual(v["judge"], "jev")
        self.assertIn("omitted", v.get("jev_error") or "")

    def test_validate_rejects_bad_probabilities(self):
        import importlib
        sys.path.insert(0, str(SRC)); J = importlib.import_module("sg_judges")
        qs = {"a": {"type": "noul", "instructions": "x"}}
        self.assertIsNone(J.validate({"a": {"noul": 1.7}}, qs)[0])
        self.assertIsNone(J.validate({"a": {"noul": float("nan")}}, qs)[0])
        self.assertIsNotNone(J.validate({"a": {"noul": 0.3}}, qs)[0])
        self.assertIsNone(J.validate({"a": {"noul": True}}, qs)[0])                      # bool is not a probability
        cq = {"c": {"type": "choice", "criteria": ["none", "minor"], "instructions": "x"}}
        self.assertIsNone(J.validate({"c": {"choice": ["none"], "confidence": 0.9}}, cq)[0])  # unhashable choice
        self.assertIsNotNone(J.validate({"c": {"choice": "none", "confidence": 0.9}}, cq)[0])

    def test_jev_model_id_is_matched_strictly(self):
        import importlib
        sys.path.insert(0, str(SRC)); J = importlib.import_module("sg_judges")
        for ok in ("typesafe/jev-1.13", "jev", "TypeSafe/Jev-1.13-20260901"):
            self.assertTrue(J.JEV_MODEL.match(ok), ok)
        for bad in ("jevil-7b", "my-jev", "typesafe/jev-1.13-evil", "", "gpt-jev"):
            self.assertFalse(J.JEV_MODEL.match(bad), bad)

    def test_missing_model_in_response_is_not_jev(self):
        v, r = self.score_with({"provider": "decisions-proxy", "api_key_env": "JUDGE_API_KEY"}, model="")
        self.assertEqual((v["judge"], v["overall"]), ("emulated", "CONCERNS"), r.stdout)

    def test_emulated_request_has_no_temperature_by_default(self):
        m = MockJudge(model="local-7b")
        try:
            self.cfg(judge={"provider": "openai-compatible", "api_key_env": "JUDGE_API_KEY", "model": "local-7b", "base_url": m.url})
            run(self.repo, "start", "SAT-1"); self.fill_ready()
            run(self.repo, "score", "SAT-1", "ready", env={"JUDGE_API_KEY": "k"})
            self.assertNotIn("temperature", m.last)
            self.assertNotIn("minimum", json.dumps(m.last["response_format"]))
        finally:
            m.close()


class TestV03Integrity(Base):
    def test_decision_bound_to_evidence(self):
        self.cfg(judge={"jev": False, "allow_self_judge_pass": True})
        run(self.repo, "start", "SAT-1"); self.fill_ready(drift="spec_should_change")
        run(self.repo, "score", "SAT-1", "ready")
        run(self.repo, "decide", "SAT-1", "--drift", "spec", "--by", "Paul", "--note", "ok")
        run(self.repo, "score", "SAT-1", "ready")
        self.assertEqual(json.loads((self.repo / ".story-gate/stories/SAT-1/ready.json").read_text())["overall"], "PASS")
        p = self.repo / ".story-gate/stories/SAT-1/story.md"; p.write_text(p.read_text() + "\nchanged scope")
        run(self.repo, "score", "SAT-1", "ready")
        self.assertEqual(json.loads((self.repo / ".story-gate/stories/SAT-1/ready.json").read_text())["overall"], "ESCALATED")

    def test_escalation_event_even_when_failing(self):
        self.cfg(judge={"jev": False, "allow_self_judge_pass": True})
        run(self.repo, "start", "SAT-1"); self.fill_ready(drift="architect_must_decide")
        (self.repo / ".story-gate/stories/SAT-1/tests.json").write_text("{}")  # also FAIL structurally
        run(self.repo, "score", "SAT-1", "ready")
        self.assertIn("drift_escalated", (self.repo / ".story-gate/outbox.jsonl").read_text())

    def test_concerns_do_not_pass_unless_accepted(self):
        self.cfg(judge={"jev": False, "allow_self_judge_pass": True})
        run(self.repo, "start", "SAT-1"); self.fill_ready(self_score=0.5)
        self.assertEqual(run(self.repo, "score", "SAT-1", "ready").returncode, 1)
        self.cfg(accept_concerns=True)
        self.assertEqual(run(self.repo, "score", "SAT-1", "ready").returncode, 0)

    def test_spec_change_makes_ready_stale(self):
        self.cfg(judge={"jev": False, "allow_self_judge_pass": True}, mode="enforce", spec_files=["docs/PRD.md"])
        (self.repo / "docs").mkdir(); (self.repo / "docs/PRD.md").write_text("v1")
        run(self.repo, "start", "SAT-1"); self.fill_ready(); run(self.repo, "score", "SAT-1", "ready")
        hook = lambda: run(self.repo, "hook", "--client", "claude", "--event", "pre", stdin=json.dumps(TestHooks.PAYLOADS["claude"]))
        self.assertEqual(hook().returncode, 0)
        (self.repo / "docs/PRD.md").write_text("v2 - different requirement")
        self.assertEqual(hook().returncode, 2)
        self.assertIn("OUT OF DATE", run(self.repo, "status").stdout)

    def test_unreadable_config_blocks_hooks(self):
        (self.repo / ".story-gate/config.json").write_text("{broken")
        r = run(self.repo, "hook", "--client", "claude", "--event", "pre", stdin=json.dumps(TestHooks.PAYLOADS["claude"]))
        self.assertEqual(r.returncode, 2); self.assertIn("unreadable", r.stderr)

    def test_shell_guard(self):
        self.cfg(mode="enforce")
        sh = lambda c: run(self.repo, "hook", "--client", "claude", "--event", "pre", stdin=json.dumps({"tool_name": "Bash", "tool_input": {"command": c}})).returncode
        self.assertEqual(sh("echo '{}' > .story-gate/config.json"), 2)
        self.assertEqual(sh("python3 -c \"open('.story-gate/gate.py','w').write('')\""), 2)
        self.assertEqual(sh("sed -i 's/1/2/' app.py"), 2)          # code write before READY
        self.assertEqual(sh("pytest -q"), 0)                        # running tests is fine
        self.assertEqual(sh("python .story-gate/gate.py start SAT-1"), 0)
        self.assertEqual(sh("python .story-gate/gate.py start SAT-1; echo x > .story-gate/config.json"), 2)

    def test_protected_via_symlink_and_case(self):
        self.cfg(mode="enforce", judge={"jev": False, "allow_self_judge_pass": True})
        run(self.repo, "start", "SAT-1"); self.fill_ready(); run(self.repo, "score", "SAT-1", "ready")
        os.symlink(self.repo / ".story-gate", self.repo / "alias")
        p = {"tool_name": "Write", "tool_input": {"file_path": "alias/gate.py"}}
        self.assertEqual(run(self.repo, "hook", "--client", "claude", "--event", "pre", stdin=json.dumps(p)).returncode, 2)

    def test_active_story_scoped_to_branch(self):
        run(self.repo, "start", "SAT-1")
        subprocess.run(["git", "checkout", "-qb", "chore-cleanup"], cwd=self.repo, capture_output=True)
        self.assertIn("no active story", run(self.repo, "status").stdout)

    def test_source_rejects_shell_metacharacters(self):
        self.cfg(sources=[{"type": "command", "command": "echo {id}"}])
        r = run(self.repo, "source", "X; touch pwned #")
        self.assertNotEqual(r.returncode, 0); self.assertFalse((self.repo / "pwned").exists())

    def test_deleted_test_does_not_count(self):
        self.cfg(judge={"jev": False, "allow_self_judge_pass": True})
        (self.repo / "test_app.py").write_text("def test_ac1_x():\n    assert True\n")
        subprocess.run(["git", "add", "-A"], cwd=self.repo); subprocess.run(["git", "commit", "-qm", "t"], cwd=self.repo)
        subprocess.run(["git", "checkout", "-qb", "feature/SAT-1-b"], cwd=self.repo)
        (self.repo / "test_app.py").unlink()
        (self.repo / "app.py").write_text("x = 3  # test_ac1_x mentioned in production code\n")
        run(self.repo, "start", "SAT-1"); self.fill_ready(); run(self.repo, "score", "SAT-1", "ready")
        run(self.repo, "score", "SAT-1", "done")
        v = json.loads((self.repo / ".story-gate/stories/SAT-1/done.json").read_text())
        self.assertEqual(v["checks"]["traceability"]["status"], "FAIL")

    def test_untracked_files_are_evidence(self):
        g = load_gate(self.repo)
        (self.repo / "brand_new.py").write_text("def feature():\n    return 42\n")
        _, files, diff = g.diff_against("main")
        self.assertIn("brand_new.py", files); self.assertIn("return 42", diff)
        os.environ.pop("STORY_GATE_ROOT")

    def test_ci_uses_ci_test_results(self):
        self.cfg(judge={"jev": False, "allow_self_judge_pass": True})
        run(self.repo, "start", "SAT-1"); self.fill_ready()
        (self.repo / "app.py").write_text("x = 5\n")
        td = self.repo / "_ci"; td.mkdir()
        (td / "results.json").write_text(json.dumps({"exit_code": 1, "command": "pytest"}))
        r = run(self.repo, "ci", "--tests", str(td))
        self.assertIn("tests failed or did not run in CI", r.stdout)

    def test_ci_missing_test_results_never_uses_agent_results(self):
        self.cfg(judge={"jev": False, "allow_self_judge_pass": True})
        run(self.repo, "start", "SAT-1"); self.fill_ready()
        (self.repo / ".story-gate/stories/SAT-1/test_results.json").write_text(json.dumps({"exit_code": 0, "command": "pytest"}))
        (self.repo / "app.py").write_text("x = 5\n")
        td = self.repo / "_ci"; td.mkdir()
        r = run(self.repo, "ci", "--tests", str(td))
        self.assertIn("CI test results are missing", r.stdout)
        tr = json.loads((self.repo / ".story-gate/stories/SAT-1/test_results.json").read_text())
        self.assertIsNone(tr["exit_code"])

    def test_ci_workflow_never_copies_pr_code_into_the_judge_job(self):
        g = load_gate(self.repo); os.environ.pop("STORY_GATE_ROOT")
        self.assertNotIn('cp ".story-gate/', g.CI_YML)
        self.assertIn("exit 0", g.TRUSTED_COPY)
        self.assertIn('export STORY_GATE_TRUSTED_DIR="$RUNNER_TEMP/sg"', g.TRUSTED_COPY)
        self.assertIn("if [ -L .story-gate ]", g.TRUSTED_COPY)
        self.assertNotIn("rm -f .story-gate/", g.TRUSTED_COPY)   # the PR's own settings files stay visible as changes
        self.assertIn("SG_BASE_REF: ${{ github.event.pull_request.base.ref }}", g.CI_YML)
        self.assertIn("BASE: ${{ github.event.pull_request.base.sha }}", g.AUDIT_YML)
        self.assertIn(".claude/settings.json", g.GATE_FILES)


def keep_github_fakes_local(test, G):
    """Tests replace sg_github's network functions; put the real ones back afterwards so test order never matters."""
    saved = {n: getattr(G, n) for n in ("call", "paged", "graphql")}
    test.addCleanup(lambda: [setattr(G, n, f) for n, f in saved.items()])


class TestGitHubLogic(unittest.TestCase):
    def setUp(self):
        import importlib
        sys.path.insert(0, str(SRC)); self.G = importlib.import_module("sg_github")
        keep_github_fakes_local(self, self.G)

    def test_codeowners(self):
        self.assertEqual(self.G.codeowners("# x\n*.js @web\n* @Paul @SathiaAI/core\n"), (["Paul"], ["SathiaAI/core"]))

    def test_junit_and_refs(self):
        d = Path(tempfile.mkdtemp())
        (d / "j.xml").write_text('<testsuite><testcase classname="tests.test_auth" name="test_ok"/><testcase classname="tests.test_auth" name="test_bad"><failure/></testcase><testcase name="test_skip"><skipped/></testcase></testsuite>')
        res = self.G.junit(d / "j.xml")
        self.assertEqual(self.G.ref_outcome("test_ok", res), "passed")
        self.assertEqual(self.G.ref_outcome("test_bad", res), "failed")
        self.assertEqual(self.G.ref_outcome("test_skip", res), "skipped")
        self.assertEqual(self.G.ref_outcome("test_nope", res), "missing")
        (d / "evil.xml").write_text('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><testsuite/>')
        with self.assertRaises(ValueError):
            self.G.junit(d / "evil.xml")

    def acc(self, reviews, author="agent-bot[bot]", head_author="agent-bot[bot]", owners="* @Paul\n"):
        G = self.G
        ctx = {"repo": "o/r", "number": 1, "head_sha": "abc1234", "author": author}
        G.call = lambda m, p, t=None, b=None, accept=None: (200, {"author": {"login": head_author}, "committer": {"login": "web-flow"}}, {})
        G.paged = lambda p, t: reviews
        return G.acceptance(ctx, "t", owners)

    def rv(self, login, state="APPROVED", sha="abc1234", typ="User"):
        return {"user": {"login": login, "type": typ}, "state": state, "commit_id": sha}

    def test_acceptance_rules(self):
        self.assertTrue(self.acc([self.rv("Paul")])["accepted"])
        self.assertFalse(self.acc([self.rv("Paul", sha="old0000")])["accepted"])          # stale approval
        self.assertFalse(self.acc([self.rv("someone")])["accepted"])                      # not a code owner
        self.assertFalse(self.acc([self.rv("Paul", typ="Bot")])["accepted"])              # bots never accept
        self.assertFalse(self.acc([self.rv("Paul")], author="Paul")["accepted"])          # own PR
        self.assertFalse(self.acc([self.rv("Paul")], head_author="Paul")["accepted"])     # approver pushed the last commit
        self.assertFalse(self.acc([self.rv("Paul"), self.rv("coderabbitai[bot]", "CHANGES_REQUESTED", typ="Bot")])["accepted"])
        self.assertFalse(self.acc([self.rv("Paul")], owners="")["accepted"])               # no code owners at all
        G = self.G
        ctx = {"repo": "o/r", "number": 1, "head_sha": "abc1234", "author": "agent-bot[bot]"}
        G.paged = lambda p, t: [self.rv("Paul")]
        G.call = lambda m, p, t=None, b=None, accept=None: (200, {"author": {"login": "agent-bot[bot]"}, "committer": {"login": "Paul"}}, {})
        self.assertFalse(G.acceptance(ctx, "t", "* @Paul\n")["accepted"])                # approver committed the last commit
        G.call = lambda m, p, t=None, b=None, accept=None: (404, {"message": "Not Found"}, {})
        self.assertFalse(G.acceptance(ctx, "t", "* @Paul\n")["accepted"])                # can't verify the head commit
        G.call = lambda m, p, t=None, b=None, accept=None: (200, {"author": None, "committer": {"login": "agent-bot[bot]"}}, {})
        self.assertFalse(G.acceptance(ctx, "t", "* @Paul\n")["accepted"])                # unlinked author email

    def test_unresolved_threads_follow_pages(self):
        G = self.G
        pages = [{"pageInfo": {"hasNextPage": True, "endCursor": "c1"}, "nodes": [{"isResolved": True, "comments": {"nodes": [{"author": {"login": "coderabbitai"}}]}}]},
                 {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [{"isResolved": False, "comments": {"nodes": [{"author": {"login": "coderabbitai"}}]}}]}]
        G.graphql = lambda q, v, t: {"repository": {"pullRequest": {"reviewThreads": pages[0 if v["a"] is None else 1]}}}
        self.assertEqual(G.unresolved_threads({"repo": "o/r", "number": 1}, "t", ["coderabbitai[bot]"]), 1)

    def test_private_key_file_mode(self):
        if os.name == "nt":
            self.skipTest("POSIX permissions")
        p = Path(tempfile.mkdtemp()) / "k.pem"
        self.G.write_private(p, "secret")
        self.assertEqual(p.stat().st_mode & 0o777, 0o600)

    def test_change_label_must_still_be_present(self):
        G = self.G
        ev = lambda kind, who: {"event": kind, "label": {"name": "story-gate-change"}, "actor": {"login": who}}
        G.paged = lambda p, t: [ev("labeled", "agent-bot[bot]"), ev("unlabeled", "Paul"), ev("labeled", "Paul")]
        self.assertEqual(G.label_added_by({"repo": "o/r", "number": 1}, "t", "story-gate-change"), "Paul")
        G.paged = lambda p, t: [ev("labeled", "Paul"), ev("unlabeled", "agent-bot[bot]")]
        self.assertIsNone(G.label_added_by({"repo": "o/r", "number": 1}, "t", "story-gate-change"))

    def test_ruleset_shape(self):
        r = self.G.ruleset_json()
        pr = [x for x in r["rules"] if x["type"] == "pull_request"][0]["parameters"]
        self.assertTrue(pr["require_code_owner_review"] and pr["dismiss_stale_reviews_on_push"] and pr["require_last_push_approval"])
        self.assertEqual(r["bypass_actors"], [])

    def test_manifest_has_no_dangerous_permissions(self):
        m = self.G.manifest("x", "http://127.0.0.1:1/callback")
        self.assertNotIn("workflows", m["default_permissions"]); self.assertNotIn("administration", m["default_permissions"])
        self.assertFalse(m["public"]); self.assertFalse(m["hook_attributes"]["active"])


class TestRound3Fixes(Base):
    def test_jev_false_turns_the_judge_off(self):
        import importlib
        sys.path.insert(0, str(SRC)); J = importlib.import_module("sg_judges")
        self.assertEqual(J.settings({"judge": {"provider": "openrouter", "jev": False}})["provider"], "none")
        self.assertEqual(J.settings({"judge": {"provider": "none"}})["provider"], "none")

    def test_quoted_free_text_in_gate_cli_is_allowed(self):
        g = load_gate(self.repo); os.environ.pop("STORY_GATE_ROOT")
        ok = 'python3 .story-gate/gate.py learn SAT-1 --type error --summary "x" --root-cause "y; rm -rf z" --rule "write the regression test first"'
        self.assertFalse(g.touches_gate(ok))
        self.assertTrue(g.touches_gate('python3 .story-gate/gate.py learn SAT-1 --summary "$(rm .story-gate/config.json)"'))
        self.assertTrue(g.touches_gate("python3 .story-gate/gate.py status; rm .story-gate/config.json"))

    def test_story_drift_decision_keeps_failing_until_applied(self):
        g = load_gate(self.repo); os.environ.pop("STORY_GATE_ROOT")
        v = g.verdict({}, {"judge": "jev", "scores": {}, "drift": "spec_should_change"}, [{"drift": "story", "by": "Paul"}], {}, g.cfg())
        self.assertEqual(v["checks"]["drift_decision"]["status"], "FAIL")
        v = g.verdict({}, {"judge": "jev", "scores": {}, "drift": "spec_should_change"}, [{"drift": "spec", "by": "Paul"}], {}, g.cfg())
        self.assertEqual(v["checks"]["drift_decision"]["status"], "PASS")

    def test_skill_files_stay_protected_with_md_exempt(self):
        g = load_gate(self.repo); os.environ.pop("STORY_GATE_ROOT")
        c = g.cfg()
        self.assertIn("*.md", c["exempt_globs"])
        for p in (".claude/skills/story-gate/SKILL.md", ".story-gate/PROTOCOL.md", ".agents/skills/story-gate/SKILL.md"):
            self.assertTrue(g.protected(str(self.repo / p), c), p)
        self.assertFalse(g.protected(str(self.repo / ".story-gate/stories/SAT-1/story.md"), c))

    def test_commits_on_base_branch_without_upstream_stay_visible(self):
        run(self.repo, "start", "SAT-1")
        (self.repo / "app.py").write_text("x = 9\n")
        subprocess.run(["git", "add", "app.py"], cwd=self.repo); subprocess.run(["git", "commit", "-qm", "work"], cwd=self.repo)
        g = load_gate(self.repo); os.environ.pop("STORY_GATE_ROOT")
        self.assertIn("app.py", g.diff_against("main")[1])

    def test_record_tests_refuses_partially_staged_files(self):
        (self.repo / "app.py").write_text("x = 5\n"); subprocess.run(["git", "add", "app.py"], cwd=self.repo)
        (self.repo / "app.py").write_text("x = 2\n")
        run(self.repo, "start", "SAT-1")
        r = run(self.repo, "record-tests", "SAT-1", "--", PY, "-c", "pass")
        self.assertNotEqual(r.returncode, 0); self.assertIn("staged differently", r.stdout + r.stderr)


class TestRound4Fixes(Base):
    def test_quoted_write_targets_are_still_seen(self):
        self.cfg(mode="enforce")
        sh = lambda c: run(self.repo, "hook", "--client", "claude", "--event", "pre", stdin=json.dumps({"tool_name": "Bash", "tool_input": {"command": c}})).returncode
        self.assertEqual(sh('echo x > ".github/workflows/story-gate.yml"'), 2)
        self.assertEqual(sh("echo x > '.story-gate/config.json'"), 2)
        self.assertEqual(sh('echo x > "app.py"'), 2)                        # code write before READY
        self.assertEqual(sh('git commit -m "move the notes > later"'), 0)   # quoted free text is not a target

    def test_symlinked_evidence_is_not_read(self):
        secret = Path(tempfile.mkdtemp()) / "environ"; secret.write_text("OPENROUTER_API_KEY=sk-secret")
        run(self.repo, "start", "SAT-1")
        st = self.repo / ".story-gate/stories/SAT-1/story.md"; st.unlink(); os.symlink(secret, st)
        g = load_gate(self.repo); os.environ.pop("STORY_GATE_ROOT")
        self.assertEqual(g.rd(st), "")

    def test_self_judge_override_never_widens_emulated(self):
        g = load_gate(self.repo); os.environ.pop("STORY_GATE_ROOT")
        c = {"judge": {"allow_self_judge_pass": True}}
        self.assertTrue(g.can_pass("self", c)); self.assertFalse(g.can_pass("emulated", c)); self.assertTrue(g.can_pass("jev", {}))

    def test_policy_change_makes_ready_stale(self):
        self.cfg(judge={"jev": False, "allow_self_judge_pass": True}, mode="enforce")
        run(self.repo, "start", "SAT-1"); self.fill_ready(); run(self.repo, "score", "SAT-1", "ready")
        hook = lambda: run(self.repo, "hook", "--client", "claude", "--event", "pre", stdin=json.dumps(TestHooks.PAYLOADS["claude"]))
        self.assertEqual(hook().returncode, 0)
        self.cfg(thresholds={"pass": 0.95, "concerns": 0.4})
        self.assertEqual(hook().returncode, 2)

    def test_junit_shared_name_takes_worst_outcome(self):
        import importlib
        sys.path.insert(0, str(SRC)); G = importlib.import_module("sg_github")
        d = Path(tempfile.mkdtemp()); (d / "j.xml").write_text('<testsuite><testcase classname="a" name="test_same"><skipped/></testcase><testcase classname="b" name="test_same"/></testsuite>')
        self.assertEqual(G.ref_outcome("test_same", G.junit(d / "j.xml")), "skipped")

    def test_review_must_be_on_head(self):
        import importlib
        sys.path.insert(0, str(SRC)); G = importlib.import_module("sg_github"); keep_github_fakes_local(self, G)
        G.paged = lambda p, t: [{"user": {"login": "coderabbitai[bot]"}, "commit_id": "old0000"}]
        self.assertEqual(G.reviewed_by({"repo": "o/r", "number": 1, "head_sha": "new1111"}, "t", ["coderabbitai[bot]"]), [])

    def test_setup_repo_matches_owner_exactly(self):
        import importlib
        sys.path.insert(0, str(SRC)); G = importlib.import_module("sg_github")
        (self.repo / ".github").mkdir(exist_ok=True); (self.repo / ".github/CODEOWNERS").write_text("* @alice2\n")
        out = G.setup_repo(self.repo, "o/r", ["alice"], "t", dry_run=True)
        self.assertIn("added", out[0])

    def test_private_key_is_private_on_every_os(self):
        import importlib
        sys.path.insert(0, str(SRC)); G = importlib.import_module("sg_github")
        p = Path(tempfile.mkdtemp()) / "k.pem"
        G.write_private(p, "one"); G.write_private(p, "two")   # rewriting replaces the file
        ok, detail = G.key_access(p)
        self.assertTrue(ok, detail); self.assertEqual(p.read_text(), "two")


class TestRound6Fixes(Base):
    def test_ci_reads_policy_from_trusted_dir_and_fails_closed(self):
        td = Path(tempfile.mkdtemp())
        (td / "config.json").write_text(json.dumps({"mode": "enforce"}))
        self.cfg(mode="warn")  # the PR's own copy says warn
        r = run(self.repo, "status", env={"STORY_GATE_TRUSTED_DIR": str(td)})
        self.assertIn("mode=enforce", r.stdout + r.stderr)
        (td / "config.json").unlink()
        r = run(self.repo, "ci", env={"STORY_GATE_TRUSTED_DIR": str(td)})
        self.assertNotEqual(r.returncode, 0); self.assertIn("missing", r.stdout)

    def test_depends_on_must_be_story_ids(self):
        fake = Path(tempfile.mkdtemp()); (fake / "handoff.md").write_text("x")
        run(self.repo, "start", "SAT-1"); self.fill_ready()
        st = self.repo / ".story-gate/stories/SAT-1/story.md"
        st.write_text(st.read_text().replace("depends_on: []", "depends_on: [%s]" % fake))
        g = load_gate(self.repo); os.environ.pop("STORY_GATE_ROOT")
        out, _ = g.struct_ready(self.repo / ".story-gate/stories/SAT-1")
        self.assertFalse(out["upstream_handoffs"][0])

    def test_junit_pass_needs_test_in_repo(self):
        self.cfg(judge={"jev": False, "allow_self_judge_pass": True})
        run(self.repo, "start", "SAT-1"); self.fill_ready()
        g = load_gate(self.repo); os.environ.pop("STORY_GATE_ROOT")
        missing = g.trace(self.repo / ".story-gate/stories/SAT-1", g.cfg(), {"exit_code": 0}, {"test_ac1_x": "passed"})
        self.assertIn("AC-1", missing)   # JUnit says passed, but no such test exists in the repository's test files


class TestJudgeIndependence(Base):
    def score_emulated(self, coder_model, judge_model, allow=False):
        m = MockJudge(model=judge_model)
        try:
            self.cfg(judge={"provider": "openai-compatible", "api_key_env": "JUDGE_API_KEY", "model": judge_model,
                            "base_url": m.url, "emulated_allow_pass": allow})
            args = ["start", "SAT-1"] + (["--model", coder_model] if coder_model else [])
            run(self.repo, *args); self.fill_ready()
            for f in ("ready.self.json", "done.self.json"):
                (self.repo / ".story-gate/stories/SAT-1" / f).unlink(missing_ok=True)
            if allow:  # pretend a calibration run passed for this judge
                g = load_gate(self.repo); os.environ.pop("STORY_GATE_ROOT")
                (self.repo / ".story-gate/judge-calibration.json").write_text(json.dumps({"ok": True, "identity": g.J.identity(g.cfg())}))
            r = run(self.repo, "score", "SAT-1", "ready", env={"JUDGE_API_KEY": "k"})
            return json.loads((self.repo / ".story-gate/stories/SAT-1/ready.json").read_text()), r
        finally:
            m.close()

    def test_family(self):
        import importlib
        sys.path.insert(0, str(SRC)); J = importlib.import_module("sg_judges")
        self.assertEqual(J.family("anthropic/claude-sonnet-4.5"), "anthropic")
        self.assertEqual(J.family("claude-opus-5-5"), "anthropic")
        self.assertEqual(J.family("gpt-5.1-codex"), "openai")
        self.assertEqual(J.family("x-ai/grok-4"), "xai")
        self.assertEqual(J.family("gemini-2.5-pro"), "google")

    def test_same_family_judge_is_self_review(self):
        v, r = self.score_emulated("claude-sonnet-4.5", "anthropic/claude-opus-4", allow=True)
        self.assertEqual(v["judge"], "self", r.stdout); self.assertIn("same model family", v["judge_note"])
        self.assertNotEqual(v["overall"], "PASS")

    def test_other_family_calibrated_judge_can_pass(self):
        v, r = self.score_emulated("claude-sonnet-4.5", "gpt-5.1", allow=True)
        self.assertEqual((v["judge"], v["overall"]), ("emulated-calibrated", "PASS"), r.stdout)

    def test_unknown_coder_keeps_emulated_capped(self):
        v, r = self.score_emulated("", "gpt-5.1", allow=True)
        self.assertEqual(v["judge"], "emulated"); self.assertIn("coder model not recorded", v["judge_note"])

    def test_presets(self):
        import importlib
        sys.path.insert(0, str(SRC)); J = importlib.import_module("sg_judges")
        self.assertEqual(J.settings({"judge": {"provider": "xai", "model": "grok-4"}})["url"], "https://api.x.ai/v1")
        self.assertEqual(J.settings({"judge": {"provider": "openai", "model": "gpt-5.1"}})["key_env"], "OPENAI_API_KEY")
        os.environ["XAI_API_KEY"] = "k"
        try:
            self.assertIn("model is required", J.ask({"judge": {"provider": "xai"}}, {}, {})["error"])
        finally:
            os.environ.pop("XAI_API_KEY")

    def test_agent_key_found_from_another_os(self):
        import importlib
        sys.path.insert(0, str(SRC)); G = importlib.import_module("sg_github")
        home = Path(tempfile.mkdtemp())
        (home / "agent.json").write_text(json.dumps({"id": 1, "slug": "x", "key": "F:\\ENV\\agent\\x.pem"}))
        (home / "x.pem").write_text("k")
        os.environ["STORY_GATE_HOME"] = str(home)
        try:
            self.assertEqual(G.agent_record()["key"], str(home / "x.pem"))
        finally:
            os.environ.pop("STORY_GATE_HOME")


class TestTrustedRuntime(Base):
    """The hooks run a pinned copy in the user's folder, with policy from the default branch (panel decision sg-hookpin)."""

    def setUp(self):
        super().setUp()
        g = lambda *a: subprocess.run(["git", *a], cwd=self.repo, capture_output=True, check=True)
        self.user = Path(tempfile.mkdtemp()); self.home = self.user / "sg-home"
        self.env = {"STORY_GATE_USER_HOME": str(self.user), "STORY_GATE_HOME": str(self.home)}
        self.remote = Path(tempfile.mkdtemp()) / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.remote)], check=True)
        g("checkout", "-q", "main")
        self.cfg(mode="enforce"); g("commit", "-qam", "policy: enforce")
        g("remote", "add", "origin", str(self.remote)); g("push", "-q", "origin", "main"); g("remote", "set-head", "origin", "main")
        g("checkout", "-qb", "feature/SAT-1-thing2")
        r = run(self.repo, "install", "--user", "--unsigned", env=self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.install_out = r.stdout
        cmd = json.loads((self.user / ".claude/settings.json").read_text())["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        import re
        self.py, self.gate = re.match(r'"(.*?)" -I "(.*?)" hook', cmd).groups()

    def tearDown(self):
        super().tearDown()
        shutil.rmtree(self.user, ignore_errors=True); shutil.rmtree(self.remote.parent, ignore_errors=True)

    def hook(self, event="pre", payload=None, cwd=None):
        e = dict(os.environ, HOME=str(self.user), **self.env)
        for k in ("STORY_GATE_ROOT", "OPENROUTER_API_KEY", "STORY_GATE_ENV_FILE", "STORY_GATE_TRUSTED_DIR", "CLAUDECODE"):
            e.pop(k, None)
        p = payload if payload is not None else {"tool_name": "Write", "tool_input": {"file_path": str(self.repo / "app.py")}}
        return subprocess.run([self.py, "-I", self.gate, "hook", "--client", "claude", "--event", event], cwd=cwd or self.repo,
                              input=json.dumps(p), capture_output=True, text=True, env=e, timeout=60)

    def test_install_writes_user_hooks_runtime_and_enrolls(self):
        for f in (".claude/settings.json", ".codex/hooks.json", ".cursor/hooks.json", ".gemini/settings.json"):
            self.assertIn("/runtime/", (self.user / f).read_text().replace("\\\\", "/"))
        self.assertTrue(Path(self.gate).is_file()); self.assertIn("UNSIGNED", self.install_out.upper())
        self.assertIn("origin/main", json.dumps(json.loads((self.home / "enrolled.json").read_text())))
        self.assertEqual(self.hook().returncode, 2)  # enforce from the default branch: no active story

    def test_branch_cannot_loosen_policy(self):
        self.cfg(mode="warn", enforce_points=[])  # the branch tries to switch to warn
        self.assertEqual(self.hook().returncode, 2)

    def test_branch_can_tighten_policy(self):
        g = lambda *a: subprocess.run(["git", *a], cwd=self.repo, capture_output=True, check=True)
        g("checkout", "-q", "main"); self.cfg(mode="warn"); g("commit", "-qam", "warn"); g("push", "-q", "origin", "main")
        g("checkout", "-q", "feature/SAT-1-thing2"); g("merge", "-q", "main")
        self.assertEqual(self.hook().returncode, 0)  # policy is warn
        self.cfg(mode="enforce")                     # the branch may tighten
        self.assertEqual(self.hook().returncode, 2)

    def test_swapped_gate_code_never_runs(self):
        marker = self.user / "pwned"
        evil = "open(%r, 'w').write('x')\n" % str(marker)
        (self.repo / ".story-gate/gate.py").write_text(evil)
        (self.repo / "sg_judges.py").write_text(evil); (self.repo / "sg_trust.py").write_text(evil)
        (self.repo / ".story-gate/sg_judges.py").write_text(evil)
        self.hook(); self.hook("stop")
        self.assertFalse(marker.exists())

    def test_deleting_story_gate_in_branch_does_not_disable(self):
        shutil.rmtree(self.repo / ".story-gate")
        self.assertEqual(self.hook().returncode, 2)

    def test_tampered_runtime_is_refused(self):
        with open(self.gate, "a") as f:
            f.write("\n# tampered\n")
        r = self.hook()
        self.assertEqual(r.returncode, 2); self.assertIn("integrity", r.stderr)

    def test_project_hook_running_branch_code_is_flagged(self):
        (self.repo / ".claude").mkdir(exist_ok=True)
        (self.repo / ".claude/settings.json").write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "python3 .story-gate/gate.py hook"}]}]}}))
        r = self.hook()
        self.assertEqual(r.returncode, 2); self.assertIn("project hook files", r.stderr)

    def test_unenrolled_repo_is_untouched(self):
        other = Path(tempfile.mkdtemp())
        subprocess.run(["git", "init", "-q", str(other)], check=True)
        self.assertEqual(self.hook(cwd=other).returncode, 0)
        self.assertEqual(self.hook(cwd=Path(tempfile.mkdtemp())).returncode, 0)  # not a git repository at all

    def test_worktree_shares_enrollment(self):
        wt = Path(tempfile.mkdtemp()) / "wt"
        subprocess.run(["git", "worktree", "add", "-q", "-b", "feature/SAT-2-x", str(wt)], cwd=self.repo, check=True, capture_output=True)
        self.assertEqual(self.hook(cwd=wt, payload={"tool_name": "Write", "tool_input": {"file_path": str(wt / "app.py")}}).returncode, 2)

    def test_agent_cannot_touch_user_files_or_run_admin_commands(self):
        sh = lambda c: self.hook(payload={"tool_name": "Bash", "tool_input": {"command": c}}).returncode
        self.assertEqual(sh("echo x > %s" % (self.user / ".claude/settings.json")), 2)
        self.assertEqual(sh("python3 .story-gate/gate.py install --user --unsigned"), 2)
        self.assertEqual(sh("python3 .story-gate/gate.py upgrade --from /tmp/evil"), 2)
        self.assertEqual(sh("rm -rf %s" % self.home), 2)
        w = {"tool_name": "Write", "tool_input": {"file_path": self.gate}}
        self.assertEqual(self.hook(payload=w).returncode, 2)

    def test_dry_run_uninstall_and_backups(self):
        own = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}]}, "theme": "dark"}
        p = self.user / ".claude/settings.json"
        data = json.loads(p.read_text()); data["theme"] = "dark"; data["hooks"]["Stop"].append(own["hooks"]["Stop"][0]); p.write_text(json.dumps(data))
        before = p.read_text()
        r = run(self.repo, "uninstall", "--user", "--dry-run", env=self.env)
        self.assertEqual(p.read_text(), before); self.assertIn("---", r.stdout)
        run(self.repo, "uninstall", "--user", env=self.env)
        after = json.loads(p.read_text())
        self.assertEqual(after["theme"], "dark"); self.assertEqual(after["hooks"]["Stop"], own["hooks"]["Stop"])
        self.assertTrue(list((self.user / ".claude").glob("settings.json.story-gate-backup-*")))
        self.assertFalse((self.home / "runtime").exists())

    def test_invalid_user_json_is_never_clobbered(self):
        p = self.user / ".gemini/settings.json"; p.write_text("{ // comments are not JSON\n}")
        r = run(self.repo, "install", "--user", "--unsigned", env=self.env)
        self.assertEqual(p.read_text(), "{ // comments are not JSON\n}"); self.assertIn("NOT changed", r.stdout)

    def test_detached_head_and_paths_with_spaces(self):
        spaced = Path(tempfile.mkdtemp()) / "my repo"
        subprocess.run(["git", "clone", "-q", str(self.remote), str(spaced)], check=True, capture_output=True)
        subprocess.run(["git", "checkout", "-q", "--detach"], cwd=spaced, check=True)
        e = dict(os.environ, HOME=str(self.user), **self.env); e.pop("STORY_GATE_ROOT", None)
        subprocess.run([self.py, "-I", self.gate, "enroll"], cwd=spaced, env=e, check=True, capture_output=True)
        self.assertEqual(self.hook(cwd=spaced, payload={"tool_name": "Write", "tool_input": {"file_path": str(spaced / "app.py")}}).returncode, 2)

    def test_repo_without_remote_uses_local_default_branch(self):
        solo = Path(tempfile.mkdtemp())
        shutil.copytree(self.repo / ".story-gate", solo / ".story-gate", ignore=shutil.ignore_patterns("stories"))
        g = lambda *a: subprocess.run(["git", *a], cwd=solo, capture_output=True, check=True)
        g("init", "-q", "-b", "main"); g("config", "user.email", "t@t"); g("config", "user.name", "t"); g("add", "."); g("commit", "-qm", "i")
        g("checkout", "-qb", "feature/SAT-9-x")
        e = dict(os.environ, HOME=str(self.user), **self.env); e.pop("STORY_GATE_ROOT", None)
        out = subprocess.run([self.py, "-I", self.gate, "enroll"], cwd=solo, env=e, capture_output=True, text=True).stdout
        self.assertIn("main", out)
        self.assertEqual(self.hook(cwd=solo, payload={"tool_name": "Write", "tool_input": {"file_path": str(solo / "a.py")}}).returncode, 2)

    def test_submodule_inside_enrolled_repo(self):
        sub_src = Path(tempfile.mkdtemp()) / "lib"
        subprocess.run(["git", "init", "-q", "-b", "main", str(sub_src)], check=True)
        (sub_src / "lib.py").write_text("y = 1\n")
        subprocess.run(["git", "-C", str(sub_src), "-c", "user.email=t@t", "-c", "user.name=t", "add", "."], check=True)
        subprocess.run(["git", "-C", str(sub_src), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"], check=True)
        subprocess.run(["git", "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub_src), "lib"], cwd=self.repo, check=True, capture_output=True)
        # editing the submodule's file from the enrolled parent is gated like any code
        self.assertEqual(self.hook(payload={"tool_name": "Write", "tool_input": {"file_path": str(self.repo / "lib/lib.py")}}).returncode, 2)
        # inside the submodule itself (its own, unenrolled repository) the hook stays out of the way
        self.assertEqual(self.hook(cwd=self.repo / "lib", payload={"tool_name": "Write", "tool_input": {"file_path": "lib.py"}}).returncode, 0)

    def test_failed_upgrade_keeps_current_runtime(self):
        bad = Path(tempfile.mkdtemp())
        shutil.copytree(SRC, bad / "rel", ignore=shutil.ignore_patterns("stories", "__pycache__", "*.jsonl"))
        (bad / "rel/release.json").write_text(json.dumps({"version": "9.9.9", "files": {"gate.py": "0" * 64}}))
        (bad / "rel/release.json.sig").write_text("-----BEGIN SSH SIGNATURE-----\nbogus\n-----END SSH SIGNATURE-----\n")
        before = (self.home / "runtime/active.json").read_text()
        e = dict(os.environ, HOME=str(self.user), **self.env); e.pop("STORY_GATE_ROOT", None)
        r = subprocess.run([self.py, "-I", self.gate, "upgrade", "--from", str(bad / "rel")], cwd=self.repo, env=e, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0); self.assertIn("NOT upgraded", r.stdout)
        self.assertEqual((self.home / "runtime/active.json").read_text(), before)

    def test_upgrade_refuses_to_run_from_repo_copy(self):
        r = run(self.repo, "upgrade", env=self.env)
        self.assertNotEqual(r.returncode, 0); self.assertIn("installed runtime", r.stdout)

    def test_concurrent_hooks_from_two_clients(self):
        import concurrent.futures as cf
        (self.repo / "app.py").write_text("x = 2\n")  # code changed with no story: pre and stop both block
        with cf.ThreadPoolExecutor(4) as ex:
            codes = list(ex.map(lambda ev: self.hook(ev).returncode, ["pre", "pre", "stop", "stop"]))
        self.assertTrue(all(c == 2 for c in codes), codes)

    def test_hook_selftest_runs_registered_hooks(self):
        e = dict(self.env); r = run(self.repo, "hook-selftest", env=e)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for cl in ("claude", "codex", "cursor", "gemini", "windsurf"):
            self.assertIn(cl, r.stdout)
        self.assertIn("blocks", r.stdout)

    def test_unapproved_project_hooks_are_blocked_until_policy_allows(self):
        (self.repo / ".cursor").mkdir(exist_ok=True)
        (self.repo / ".cursor/hooks.json").write_text(json.dumps({"version": 1, "hooks": {"stop": [{"command": "./scripts/lint.sh"}]}}))
        r = self.hook()
        self.assertEqual(r.returncode, 2); self.assertIn("lint.sh", r.stderr)
        self.cfg(project_hooks_allowed=["./scripts/lint.sh"])  # the branch can't approve its own hooks
        self.assertIn("lint.sh", self.hook().stderr)
        g = lambda *a: subprocess.run(["git", *a], cwd=self.repo, capture_output=True, check=True)
        g("stash", "-u"); g("checkout", "-q", "main"); self.cfg(project_hooks_allowed=["./scripts/lint.sh"]); g("commit", "-qam", "allow lint hook")
        g("push", "-q", "origin", "main"); g("checkout", "-q", "feature/SAT-1-thing2"); g("merge", "-q", "main"); g("stash", "pop")
        self.assertNotIn("lint.sh", self.hook().stderr)

    def test_unsigned_install_requires_the_flag(self):
        r = run(self.repo, "install", "--user", env=self.env)
        self.assertNotEqual(r.returncode, 0); self.assertIn("NOT installed", r.stdout)


@unittest.skipUnless(shutil.which("ssh-keygen"), "needs ssh-keygen")
class TestReleaseSignatures(unittest.TestCase):
    def test_embedded_key_matches_published_fingerprint(self):
        import importlib
        sys.path.insert(0, str(SRC)); T = importlib.import_module("sg_trust")
        self.assertEqual(T.key_fingerprint(), T.RELEASE_FINGERPRINT)

    def setUp(self):
        import importlib
        sys.path.insert(0, str(SRC)); self.T = importlib.import_module("sg_trust")
        self.d = Path(tempfile.mkdtemp())
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "test", "-f", str(self.d / "key")], check=True)
        self.signers = "story-gate-release " + " ".join((self.d / "key.pub").read_text().split()[:2])
        self.rel = self.d / "rel"; shutil.copytree(SRC, self.rel, ignore=shutil.ignore_patterns("stories", "__pycache__", "*.jsonl", "release.json*"))
        self.T.sign_release(self.rel, self.d / "key", "9.9.9")

    def test_valid_release_verifies(self):
        self.assertEqual(self.T.verify_release(self.rel, self.signers)["version"], "9.9.9")

    def test_changed_file_fails(self):
        with open(self.rel / "gate.py", "a") as f:
            f.write("# evil\n")
        with self.assertRaises(self.T.TrustError):
            self.T.verify_release(self.rel, self.signers)

    def test_added_file_fails(self):
        (self.rel / "evil.py").write_text("x")
        with self.assertRaises(self.T.TrustError):
            self.T.verify_release(self.rel, self.signers)

    def test_wrong_key_fails(self):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.d / "other")], check=True)
        other = "story-gate-release " + " ".join((self.d / "other.pub").read_text().split()[:2])
        with self.assertRaises(self.T.TrustError):
            self.T.verify_release(self.rel, other)

    def test_release_archive_cannot_escape_its_folder(self):
        import io, tarfile, urllib.request
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as t:
            info = tarfile.TarInfo("../../evil.txt"); payload = b"x"; info.size = len(payload)
            t.addfile(info, io.BytesIO(payload))
        orig = urllib.request.urlopen
        urllib.request.urlopen = lambda *a, **k: io.BytesIO(buf.getvalue())
        try:
            with self.assertRaises(self.T.TrustError):
                self.T.fetch_release("https://example.invalid/r.tgz", self.d / "dl")
            with self.assertRaises(self.T.TrustError):
                self.T.fetch_release("http://example.invalid/r.tgz", self.d / "dl")
        finally:
            urllib.request.urlopen = orig

    def test_fingerprint_matches_ssh_keygen(self):
        out = subprocess.run(["ssh-keygen", "-lf", str(self.d / "key.pub")], capture_output=True, text=True).stdout
        self.assertIn(self.T.key_fingerprint(self.signers), out)


class TestDashboard(Base):
    """Dashboard decision sg-dashboard: pinned issue + Tabler report, built from every branch's records as data."""

    def put(self, sid, *, title="T", feature="none", ready=None, done=None, coder=None, cp=None, trace=None):
        sd = self.repo / ".story-gate/stories" / sid; sd.mkdir(parents=True, exist_ok=True)
        (sd / "story.md").write_text("---\nid: %s\ntitle: %s\nfeature: %s\nsource: linear:%s\n---\nbody\n" % (sid, title, feature, sid))
        (sd / "tests.json").write_text(json.dumps({"acceptance_criteria": [{"id": "AC-1"}, {"id": "AC-2"}]}))
        if ready: (sd / "ready.json").write_text(json.dumps({"overall": ready, "drift": "none", "checks": {}, "cost": 0.001}))
        if done: (sd / "done.json").write_text(json.dumps({"overall": done, "drift": "none", "checks": {}, "cost": 0.002}))
        if coder: (sd / "coder.json").write_text(json.dumps(coder))
        if cp: (sd / "checkpoints.jsonl").write_text(json.dumps(cp) + "\n")
        if trace: (sd / "trace.md").write_text("| AC | x | 3 | t | 1/1 | %s |\n| AC-1 | a | 3 | t | 1/1 | GREEN |\n| AC-2 | b | 3 | t | 1/1 | %s |\n" % (trace, trace))

    def g(self, *a):
        return subprocess.run(["git", *a], cwd=self.repo, capture_output=True, check=True)

    def setUp(self):
        super().setUp()
        self.remote = Path(tempfile.mkdtemp()) / "r.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.remote)], check=True)
        self.g("checkout", "-q", "main")
        (self.repo / ".story-gate/features.json").write_text(json.dumps({"PAY": {"title": "Payments"}}))
        self.put("SAT-1", title="Merged story", feature="PAY", ready="PASS", done="PASS", coder={"client": "codex", "model": "gpt-5.1-codex", "branch": "f1", "claimed_at": "2026-09-01T00:00:00Z"}, trace="GREEN")
        self.put("SAT-2", title="Queued story", feature="PAY", ready="PASS")
        self.put("SAT-3", title="Draft <script>alert(1)</script> | **x** @everyone", ready="FAIL")
        (self.repo / ".story-gate/calibration.jsonl").write_text("\n".join(json.dumps(r) for r in [
            {"at": "1", "story": "SAT-1", "phase": "ready", "overall": "PASS"}, {"at": "2", "story": "SAT-1", "phase": "done", "overall": "FAIL"},
            {"at": "3", "story": "SAT-1", "phase": "done", "overall": "PASS"}, {"at": "4", "story": "SAT-2", "phase": "ready", "overall": "FAIL"},
            {"at": "5", "story": "SAT-1", "phase": "done", "label": "wrong"}]) + "\n")
        (self.repo / ".story-gate/learnings.jsonl").write_text(json.dumps({"id": "L1", "story": "SAT-1", "type": "error"}) + "\n")
        self.g("add", "-A"); self.g("commit", "-qm", "records")
        self.g("remote", "add", "origin", str(self.remote)); self.g("push", "-q", "origin", "main")
        for br, sid, kw in (("feat/SAT-4", "SAT-4", dict(title="Working", ready="PASS", coder={"client": "cursor", "model": "claude-sonnet", "branch": "feat/SAT-4"}, cp={"status": "AT_RISK", "percent": 60, "drift": "none"})),
                            ("feat/SAT-5", "SAT-5", dict(title="Review me", ready="PASS", done="PASS", coder={"client": "grok", "model": "grok-4"}, trace="FAIL")),
                            ("feat/SAT-6", "SAT-6", dict(title="Stuck", ready="ESCALATED", coder={"client": "claude-code", "model": "opus"})),
                            ("feat/SAT-6b", "SAT-6", dict(title="Stuck", ready="PASS", coder={"client": "codex", "model": "gpt-5", "branch": "feat/SAT-6b"}))):
            self.g("checkout", "-q", "-b", br, "main"); self.put(sid, **kw); self.g("add", "-A"); self.g("commit", "-qm", sid); self.g("push", "-q", "origin", br)
        self.g("checkout", "-q", "main")
        sys.path.insert(0, str(SRC))
        import importlib; self.D = importlib.import_module("sg_dashboard")
        self.data = self.D.build(self.repo, "origin/main", "[A-Z][A-Z0-9]+-[0-9]+", now="2026-10-02T00:00:00Z")

    def st(self, sid):
        return [s for s in self.data["stories"] if s["id"] == sid][0]

    def test_statuses(self):
        self.assertEqual(self.st("SAT-1")["status"], "done"); self.assertEqual(self.st("SAT-2")["status"], "queued")
        self.assertEqual(self.st("SAT-3")["status"], "draft"); self.assertEqual(self.st("SAT-4")["status"], "in_progress")
        self.assertEqual(self.st("SAT-5")["status"], "in_review")
        self.assertEqual(self.st("SAT-4")["coder"]["client"], "cursor")
        self.assertTrue(self.data["conflicts"]) # SAT-6 claimed on two branches by different agents

    def test_metrics(self):
        M = {m["key"]: m for m in self.data["metrics"]}
        self.assertEqual(M["features"]["value"], 1); self.assertEqual(M["queued"]["value"], 1)
        self.assertEqual(M["done"]["value"], 1); self.assertEqual(M["in_review"]["value"], 1)
        self.assertEqual(M["gate_catches"]["value"], 1); self.assertEqual(M["defects"]["value"], 1)
        self.assertEqual(M["false_alarms"]["value"], 1); self.assertEqual(M["first_try"]["value"], 50)
        self.assertTrue(M["ac_progress"]["estimate"]); self.assertEqual(M["ac_progress"]["value"], 60)
        self.assertEqual(M["ac_verified"]["value"], 75)  # SAT-1: 2/2 proven, SAT-5: 1/2
        for m in self.data["metrics"]:
            self.assertTrue(m["formula"])

    def test_merged_done_is_not_reopened_by_a_stale_branch(self):
        self.g("checkout", "-q", "-b", "feat/old-SAT-1", "main~1" if False else "main")
        self.put("SAT-1", title="Merged story", ready="FAIL"); self.g("add", "-A"); self.g("commit", "-qm", "stale"); self.g("push", "-q", "origin", "feat/old-SAT-1")
        d = self.D.build(self.repo, "origin/main", "[A-Z][A-Z0-9]+-[0-9]+")
        self.assertEqual([s for s in d["stories"] if s["id"] == "SAT-1"][0]["status"], "done")

    def test_markdown_is_escaped_and_fits(self):
        body = self.D.to_markdown(self.data, "https://example.com/a")
        self.assertNotIn("<script>", body); self.assertNotIn("@everyone", body); self.assertIn("&lt;script&gt;", body)
        self.assertIn("generated_at=2026-10-02T00:00:00Z", body); self.assertIn("How each number is calculated", body)
        many = dict(self.data, stories=[dict(self.data["stories"][0], id="SAT-%d" % i, title="x" * 50) for i in range(3000)])
        self.assertLessEqual(len(self.D.to_markdown(many)), self.D.ISSUE_LIMIT + 100)

    def test_html_is_self_contained_escaped_and_branded(self):
        page = self.D.to_html(self.data)
        self.assertNotIn("<script>alert", page); self.assertIn("default-src 'none'", page)
        self.assertNotIn("http", page.split("</style>")[-1].replace('xmlns="http://www.w3.org/2000/svg"', "")); self.assertNotRegex(page, r"<(script|link|img|iframe)[\s>]")
        self.assertIn("Tabler", page); self.assertIn("#FF4B20", page); self.assertIn('aria-label="Viaknox"', page)

    def test_record_folders_with_bad_names_and_huge_files_are_skipped(self):
        self.g("checkout", "-q", "-b", "feat/evil", "main")
        bad = self.repo / ".story-gate/stories/..evil"; bad.mkdir(parents=True); (bad / "story.md").write_text("x")
        (self.repo / ".story-gate/stories/SAT-2/trace.md").write_text("x" * (self.D.MAX_BLOB + 10))
        self.g("add", "-A"); self.g("commit", "-qm", "evil"); self.g("push", "-q", "origin", "feat/evil")
        d = self.D.build(self.repo, "origin/main", "[A-Z][A-Z0-9]+-[0-9]+")
        self.assertNotIn("..evil", [s["id"] for s in d["stories"]]); self.assertTrue(any("over" in o for o in d["omissions"]))

    def test_publish_creates_then_updates_and_never_goes_backwards(self):
        import importlib; G = importlib.import_module("sg_github"); keep_github_fakes_local(self, G)
        state = {"issues": []}
        def call(m, path, t=None, b=None, accept=None):
            if m == "GET" and "/labels/" in path: return 404, {}, {}
            if m == "POST" and path.endswith("/labels"): return 201, {}, {}
            if m == "GET" and "/issues?" in path: return 200, state["issues"], {}
            if m == "POST" and path.endswith("/issues"):
                state["issues"].append({"number": 7, "state": "open", "body": b["body"], "node_id": "N"}); return 201, state["issues"][-1], {}
            if m == "PATCH": state["issues"][0].update(b); return 200, {}, {}
            return 500, {}, {}
        G.call = call; G.graphql = lambda *a: {}
        self.assertIn("Created", self.D.publish_issue(G, "o/r", "t", "<!-- story-gate-dashboard generated_at=2026-10-02T01 -->", "2026-10-02T01"))
        state["issues"][0]["state"] = "closed"
        self.assertIn("Updated", self.D.publish_issue(G, "o/r", "t", "<!-- story-gate-dashboard generated_at=2026-10-02T02 -->", "2026-10-02T02"))
        self.assertEqual(state["issues"][0]["state"], "open")
        self.assertIn("Skipped", self.D.publish_issue(G, "o/r", "t", "old", "2026-10-02T00"))

    def test_ci_status_and_rate_limit_backoff(self):
        import importlib; G = importlib.import_module("sg_github"); keep_github_fakes_local(self, G)
        seen = {"n": 0}
        def call(m, path, t=None, b=None, accept=None):
            if "/pulls?" in path:
                seen["n"] += 1
                if seen["n"] == 1: return 429, {}, {"Retry-After": "1"}
                return 200, [{"number": 5, "head": {"ref": "feat/SAT-5", "sha": "abc"}}], {}
            if "/check-runs" in path: return 200, {"check_runs": [{"conclusion": "success"}]}, {}
            return 404, {}, {}
        G.call = call
        orig = self.D.time.sleep; self.D.time.sleep = lambda s: None
        try:
            self.D.ci_status(G, "o/r", "t", self.data)
        finally:
            self.D.time.sleep = orig
        self.assertEqual(self.st("SAT-5")["ci"], {"pr": 5, "result": "success"})
        self.assertIn("#5 success", self.D.to_markdown(self.data))

    def test_ready_verdicts_are_checked_against_the_story(self):
        self.g("checkout", "-q", "-b", "feat/SAT-7")
        self.cfg(judge={"jev": False, "allow_self_judge_pass": True}); self.g("commit", "-qam", "cfg")
        run(self.repo, "start", "SAT-7"); self.fill_ready("SAT-7")
        self.assertIn("READY: PASS", run(self.repo, "score", "SAT-7", "ready").stdout)
        self.g("add", "-A"); self.g("commit", "-qm", "scored"); self.g("push", "-q", "origin", "feat/SAT-7")
        g = load_gate(self.repo); os.environ.pop("STORY_GATE_ROOT")
        d = self.D.build(self.repo, "origin/main", "[A-Z][A-Z0-9]+-[0-9]+", gate=g)
        s = [x for x in d["stories"] if x["id"] == "SAT-7"][0]
        self.assertTrue(s["ready_fresh"], "dashboard and gate.py must compute the same READY hash")
        st = self.repo / ".story-gate/stories/SAT-7/story.md"; st.write_text(st.read_text() + "\nscope grew\n")
        self.g("commit", "-qam", "story changed after READY"); self.g("push", "-q", "origin", "feat/SAT-7")
        d = self.D.build(self.repo, "origin/main", "[A-Z][A-Z0-9]+-[0-9]+", gate=g)
        s = [x for x in d["stories"] if x["id"] == "SAT-7"][0]
        self.assertIs(s["ready_fresh"], False)
        self.assertEqual({m["key"]: m for m in d["metrics"]}["ready_stale"]["value"], 1)

    def test_ready_fresh_with_non_utf8_story_text(self):
        self.g("checkout", "-q", "-b", "feat/SAT-8")
        self.cfg(judge={"jev": False, "allow_self_judge_pass": True}); self.g("commit", "-qam", "cfg")
        run(self.repo, "start", "SAT-8"); self.fill_ready("SAT-8")
        ctx = self.repo / ".story-gate/stories/SAT-8/context.md"
        ctx.write_bytes(ctx.read_bytes().replace("—".encode("utf-8"), b"\x97") + b"line\r\n")  # an editor saving cp1252 with CRLF
        self.assertIn("READY: PASS", run(self.repo, "score", "SAT-8", "ready").stdout)
        self.g("add", "-A"); self.g("-c", "core.autocrlf=false", "commit", "-qm", "scored"); self.g("push", "-q", "origin", "feat/SAT-8")
        g = load_gate(self.repo); os.environ.pop("STORY_GATE_ROOT")
        s = [x for x in self.D.build(self.repo, "origin/main", "[A-Z][A-Z0-9]+-[0-9]+", gate=g)["stories"] if x["id"] == "SAT-8"][0]
        self.assertTrue(s["ready_fresh"])

    def test_ready_hash_ignores_line_endings(self):
        g = load_gate(self.repo); c = g.cfg()
        self.assertEqual(g.ready_hash_from("a\r\nb\r\n", "c\r\n", "{}", [("PRD.md", "x\r\n")], c),
                         g.ready_hash_from("a\nb\n", "c\n", "{}", [("PRD.md", "x\n")], c))

    def test_waivers_are_not_proof(self):
        s = self.st("SAT-1"); s["done"]["overall"] = "WAIVED"
        M = {m["key"]: m for m in self.D.metrics(self.data["stories"], [], [], [], "2026-10-02T00:00:00Z")}
        self.assertEqual(M["waived"]["value"], 1); self.assertEqual(M["ac_verified"]["value"], 50)  # only SAT-5's 1/2 counts

    def test_truncation_keeps_priorities_and_falls_back_from_the_chart(self):
        body = self.D.to_markdown(self.data, limit=1800)
        self.assertIn("## Features", body); self.assertLessEqual(len(body), 1800)
        self.assertIn("Stories", body.split("## Features")[0])  # summary first

    def test_failed_refresh_is_shown_and_last_snapshot_kept(self):
        import importlib; G = importlib.import_module("sg_github"); keep_github_fakes_local(self, G)
        issue = {"number": 3, "body": "<!-- x -->\n# Story-gate dashboard\n\nUpdated A.\n| Stories | 6 |\n"}
        def call(m, path, t=None, b=None, accept=None):
            if m == "GET": return 200, [issue], {}
            issue.update(b); return 200, {}, {}
        G.call = call
        self.D.report_failure(G, "o/r", "t", "2026-10-02T05:00:00Z", "https://github.com/o/r/actions/runs/1")
        self.D.report_failure(G, "o/r", "t", "2026-10-02T06:00:00Z", "https://github.com/o/r/actions/runs/2")
        self.assertEqual(issue["body"].count("refresh failed"), 1); self.assertIn("06:00", issue["body"])
        self.assertIn("| Stories | 6 |", issue["body"])

    def test_pin_only_when_a_slot_is_free(self):
        import importlib; G = importlib.import_module("sg_github"); keep_github_fakes_local(self, G)
        calls = []
        G.graphql = lambda q, v, t: (calls.append(q), {"repository": {"pinnedIssues": {"totalCount": 3}}})[1]
        self.assertIn("slots", self.D.pin(G, "t", "o/r", "N")); self.assertFalse(any("pinIssue" in q for q in calls))
        G.graphql = lambda q, v, t: (calls.append(q), {"repository": {"pinnedIssues": {"totalCount": 1}}})[1]
        self.assertEqual(self.D.pin(G, "t", "o/r", "N"), "pinned")

    def test_cli_builds_files_locally_offline(self):
        out = Path(tempfile.mkdtemp())
        r = run(self.repo, "dashboard", "--offline", "--out", str(out))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for f in ("dashboard.json", "dashboard.md", "dashboard.html"):
            self.assertTrue((out / f).is_file())
        self.assertIn("local snapshot", (out / "dashboard.json").read_text())

    def test_plan_and_feature_commands(self):
        self.g("checkout", "-q", "-b", "feat/plan")
        self.assertEqual(run(self.repo, "feature", "SHIP", "--title", "Shipping").returncode, 0)
        self.assertEqual(run(self.repo, "plan", "SAT-9", "--title", "Plan me", "--feature", "SHIP").returncode, 0)
        text = (self.repo / ".story-gate/stories/SAT-9/story.md").read_text()
        self.assertIn("title: Plan me", text); self.assertIn("feature: SHIP", text)
        self.assertFalse((self.repo / ".story-gate/stories/SAT-9/coder.json").exists())  # planned, not claimed
        self.assertIn("SHIP", (self.repo / ".story-gate/features.json").read_text())

    def test_install_writes_dashboard_workflow(self):
        run(self.repo, "install")
        y = (self.repo / ".github/workflows/story-gate-dashboard.yml").read_text()
        self.assertIn("issues: write", y); self.assertIn("persist-credentials: false", y)
        self.assertNotIn("pull_request_target", y); self.assertIn('branches: ["main"]', y)
        self.assertIn("default_branch || github.ref_name", y)


class TestInstallV03(Base):
    def test_managed_workflows(self):
        run(self.repo, "install")
        for f in (".github/workflows/story-gate.yml", ".github/workflows/story-gate-audit.yml"):
            self.assertTrue((self.repo / f).read_text().startswith("# managed by story-gate"))
        own = self.repo / ".github/workflows/story-gate.yml"; own.write_text("name: mine\n")
        out = run(self.repo, "install").stdout
        self.assertIn("not managed", out); self.assertEqual(own.read_text(), "name: mine\n")


if __name__ == "__main__":
    unittest.main()
